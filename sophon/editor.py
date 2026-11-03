"""Sonnet editor pass (C-004 stop rule): the local pipeline writes FINAL.md for 0 Claude tokens, then one short
Sonnet call reads only the top cards, the job's hard requirements, the facts sheet and the judging rules, and
returns KEEP / REWRITE / DROP per card plus at most two new cards. Code applies the decisions. The local version
is kept as FINAL-local.md for comparison.

Off with SWARM_EDITOR=off or `swarm editor off` (state key "editor")."""
import json
import os
import re
import subprocess
import time

from .config import CLAUDE, LEAD, STYLE
from .util import load, log, stamp
from .worker import external_profile

SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"},
            "action": {"type": "string", "enum": ["keep", "rewrite", "drop"]},
            "why": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"}}, "required": ["id", "action", "why"]}},
        "new": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "text": {"type": "string"}, "why": {"type": "string"}},
            "required": ["title", "text", "why"]}},
        "order": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}},
    "required": ["cards", "order", "summary"]}

SYSTEM = ("You are the final editor of a list a local model team wrote. You never add facts: every model, command, "
          "flag, API and number must come from the FACTS sheet or the job text, otherwise mark it (unverified) or "
          f"remove it. {STYLE}. Reply only through the JSON schema.")

FORMAT_RULES = {
    "academic": "Each topic must have: a research question ending in '?', a named public dataset (or 'collect my own' "
                "with one line of justification, at most 2 topics), a metric, a 12-week plan, and the programme module it "
                "fits. A topic missing these is REWRITE if fixable, else DROP. Homelab plumbing is not a dissertation.",
    "practical": "Each item must be a concrete change that answers the job: the exact command or config (from the "
                 "facts sheet), the expected gain (marked estimate unless measured), and how to measure it. DROP "
                 "gimmicks and metaphors that do not make anything faster or better. Prefer the obvious high-value "
                 "changes over clever ones.",
    "demo": "Each idea needs a 10-second demo a viewer understands without narration, and a first test this week. "
            "DROP near-duplicates and ideas that only work as a metaphor.",
    "general": "Each item must directly answer the job. DROP items that do not, and near-duplicates.",
}


def enabled(run):
    if os.environ.get("SWARM_EDITOR", "").lower() == "off":
        return False
    return load(run).get("editor", "sonnet") != "off"


def split_cards(text):
    """Cards are '## N. Title' or '### N. Title' sections of the written FINAL (after the ranking table)."""
    body = text.split("\n# FINAL.md", 1)[-1] if "\n# FINAL.md" in text else text
    heads = list(re.finditer(r"^#{2,3} (\d+)[.)]\s*(.+)$", body, re.M))
    cards = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        chunk = body[h.end():end]
        chunk = re.split(r"^(?:#{1,3} (?!\d)|---)", chunk, maxsplit=1, flags=re.M)[0]
        cards.append({"id": len(cards) + 1, "title": h.group(2).strip(), "body": chunk.strip()})
    return cards


def call_sonnet(prompt, timeout=420):
    r = subprocess.run([CLAUDE, "-p", "--model", "sonnet", "--tools", "", "--system-prompt", SYSTEM,
                        "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    out = json.loads(r.stdout)
    data = out.get("structured_output")
    if not isinstance(data, dict):
        m = re.search(r"\{.*\}", str(out.get("result", "")), re.S)
        data = json.loads(m.group(0)) if m else None
    if not isinstance(data, dict):
        raise RuntimeError("the editor returned no JSON")
    return data, out.get("usage", {})


def editor_pass(run, job, req, me=LEAD, max_cards=14):
    final = run / "FINAL.md"
    if not final.exists() or not enabled(run):
        return False
    text = final.read_text()
    cards = split_cards(text)[:max_cards]
    if not cards:
        log(run, f"[{stamp()}] {me}: editor skipped (no cards found in FINAL.md)")
        return False
    want = req.get("count") or len(cards)
    fmt = req.get("format", "general")
    facts = (run / "context" / "facts.md")
    prof = run / "context" / "profile.md"
    killed = re.search(r"\*\*Killed[^\n]*", text)
    deck = "\n\n".join(f"[{c['id']}] {c['title']}\n{' '.join(c['body'].split()[:170])}" for c in cards)
    prompt = (f"JOB (the user's words):\n{job}\n\nHARD REQUIREMENTS: {json.dumps(req)}\n\nFORMAT RULES: "
              f"{FORMAT_RULES.get(fmt, FORMAT_RULES['general'])}\n\nJUDGING RULES (the user's taste):\n"
              f"{external_profile(prof.read_text()) if prof.exists() else '-'}\n\nFACTS sheet:\n"
              f"{facts.read_text()[:6000] if facts.exists() else '-'}\n\n"
              f"{(killed.group(0)[:1500] + chr(10) + chr(10)) if killed else ''}"
              f"CARDS (ranked by the local critics, best first):\n{deck}\n\n"
              f"Decide per card: keep (good as is), rewrite (fix it; give a title that says what the card now "
              f"is, and the full card text without the title line), or drop (say why). Add at most 2 new cards only if the list misses "
              f"something obviously better for this job; they will be marked [Sonnet]. Then give the final order as "
              f"ids and new titles ('n1', 'n2'), best first, exactly {want} entries if that many are good enough, "
              f"fewer if not (never pad). summary: 2 sentences on what you changed and why.")
    t0 = time.time()
    try:
        data, usage = call_sonnet(prompt)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        log(run, f"[{stamp()}] {me}: editor failed ({str(e)[:160]}); FINAL.md stays the local version")
        return False
    by_id = {c["id"]: c for c in cards}
    dec = {d["id"]: d for d in data.get("cards", []) if d.get("id") in by_id}
    new = {f"n{i}": n for i, n in enumerate(data.get("new", [])[:2], 1)}
    out, changes = [], []
    for key in data.get("order", []):
        k = key.strip()
        if k.startswith("n") and k in new:
            n = new[k]
            out.append((f"{n['title']} [Sonnet]", n["text"]))
            changes.append(f"- NEW [Sonnet] {n['title']}: {n['why']}")
            continue
        try:
            cid = int(re.sub(r"\D", "", k))
        except ValueError:
            continue
        c, d = by_id.get(cid), dec.get(cid)
        if not c or (d and d["action"] == "drop") or any(x[0] == c["title"] for x in out):
            continue
        if d and d["action"] == "rewrite" and d.get("text", "").strip():
            out.append(((d.get("title") or "").strip() or c["title"], d["text"].strip()))
            changes.append(f"- REWRITE {c['title']}: {d['why']}")
        else:
            out.append((c["title"], c["body"]))
    for d in dec.values():
        if d["action"] == "drop":
            changes.append(f"- DROP {by_id[d['id']]['title']}: {d['why']}")
    if not out:
        log(run, f"[{stamp()}] {me}: editor kept nothing; FINAL.md stays the local version")
        return False
    (run / "FINAL-local.md").write_text(text)
    start = text.split("\n---\n", 1)[0] + "\n\n---\n\n" if text.startswith("# Start here") else ""
    fresh = usage.get("input_tokens", 0) + usage.get("output_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    md = [start + "# FINAL", "", f"Local pipeline, then one Sonnet editor pass ({fresh:,} fresh tokens, "
          f"{time.time() - t0:.0f} s). The unedited local version is FINAL-local.md.", "",
          f"> {data.get('summary', '').strip()}", ""]
    for i, (title, body) in enumerate(out, 1):
        md += [f"## {i}. {title}", "", body.strip(), ""]
    md += ["## Editor's changes", ""] + (changes or ["- none"])
    final.write_text("\n".join(md) + "\n")
    (run / "context" / "editor.json").write_text(json.dumps({"usage": usage, "decisions": data}, indent=1))
    log(run, f"[{stamp()}] {me}: editor pass done: {len(out)} cards, {sum(c.startswith('- DROP') for c in changes)} "
             f"dropped, {sum(c.startswith('- REWRITE') for c in changes)} rewritten, "
             f"{sum(c.startswith('- NEW') for c in changes)} new ({fresh:,} fresh tokens)")
    return True
