"""FINAL.md: calibrated ranking, gates, caps, de-duplication and the writer."""
import json
import os
import re
import sys

from .backends import stream_chat
from .config import LOCAL_RULES, RUNS, C
from .util import latest, log, set_status, stamp


def write_final(run, me, extra="", model=None, demo=True):
    model = model or os.environ.get("SWARM_WRITER", "swarm-gemma4-26b")
    files = sorted((run / "out").glob("*.md"))
    files = [f for f in files if f.stem not in ("q1", "q2", "questions")]
    files.sort(key=lambda f: f.stem in ("critic", "taste", "judge"))  # critics last, closest to the instruction
    ctx = "\n\n".join(f"# FILE out/{f.name}\n{latest(f.read_text(), 6000)}" for f in files)
    scores, table = {}, ""

    def key(name):
        return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)", "", name.lower())).strip()
    passes = {}  # C-004 D: numbers only, z-scored per critic pass; the critics' KEEP/KILL words are ignored
    for c in ("critic", "taste", "judge"):
        f = run / "out" / f"{c}.md"
        if not f.exists():
            continue
        secs = f.read_text().split("\n## Task from ")
        use = [secs[-1]] + ([secs[-2]] if len(secs) > 2 and "PASS B" in secs[-1][:600] else [])
        for p_i, sec in enumerate(use):
            rows = []
            for row in sec[-24000:].splitlines():
                cells = [x.strip(" *") for x in row.strip().strip("|").split("|")]
                nums = [int(x) for x in cells if re.fullmatch(r"[1-5]", x)]
                verdict = next((x.upper() for x in cells if x.upper() in ("KEEP", "FIX", "KILL")), None)
                if len(nums) >= 4 and verdict and cells[0] and not cells[0].lower().startswith("idea"):
                    rows.append((cells, nums[:4], verdict))
            if rows:
                passes[f"{c}{'B' if p_i == 0 and len(use) > 1 else ''}"] = rows
    for pid, rows in passes.items():
        cols = list(zip(*[r[1] for r in rows]))
        stats = [(sum(col) / len(col), (sum((x - sum(col) / len(col)) ** 2 for x in col) / len(col)) ** 0.5)
                 for col in cols]
        for cells, nums, verdict in rows:
            z = sum((x - m) / sd if sd > 0 else 0.0 for x, (m, sd) in zip(nums, stats)) / 4
            d = scores.setdefault(key(cells[0]), {"name": cells[0], "src": cells[1] if len(cells) > 1 else "",
                                                   "total": 0, "n": 0, "zs": [], "by": [], "why": ""})
            d["total"] += sum(nums)
            d["n"] += 1
            d["zs"].append(z)
            d["by"].append(f"{pid} {sum(nums)}")
            if pid.startswith(("critic", "judge")) or not d["why"]:
                d["why"] = cells[-1][:160]
    if scores:
        from . import quality as Q
        meta_f = run / "context" / "ideas.json"
        meta = {key(k): m for k, m in json.loads(meta_f.read_text()).items()} if meta_f.exists() else {}
        rq = run / "context" / "requirements.json"
        fmt_ = json.loads(rq.read_text()).get("format") if rq.exists() else ""
        for v in scores.values():  # hard kills come only from code gates
            v["z"] = sum(v["zs"]) / len(v["zs"])
            v["dead"] = False
            m = meta.get(key(v["name"])) or {}
            v["vec"] = m.get("vec")
            if m.get("forbidden"):
                v["dead"], v["why"] = True, "forbidden by the job: " + ", ".join(m["forbidden"])
            elif m.get("no_rq"):
                v["dead"], v["why"] = True, "no research question"
            elif m.get("claims"):  # C-004 G4: an invented flag that survived the refine round is a gate, not -2
                v["dead"], v["why"] = True, ("invented: " + "; ".join(m["claims"]))[:150]
            elif m.get("module_miss"):
                v["dead"], v["why"] = True, m["module_miss"]
        try:  # C-004 B novelty gate: close to a card in a recent FINAL = familiar, one band down (not a kill)
            prev = sorted((p for p in (RUNS).glob("*/FINAL.md") if p.parent != run),
                          key=lambda p: p.stat().st_mtime)[-15:]
            corpus = [Q.idea_text(i) for p in prev for i in Q.parse_ideas(p.read_text(), p.parent.name)][:400]
            cvecs = Q.embed(corpus) if corpus else []
            for v in scores.values():
                m = meta.get(key(v["name"])) or {}
                if not v["dead"] and m.get("vec") and Q.familiar(m["vec"], cvecs):
                    v["z"] -= 0.5
                    v["why"] = "familiar from earlier runs / " + v["why"]
        except Exception as e:  # noqa: BLE001
            log(run, f"[{stamp()}] {me}: novelty gate skipped ({e})")
        live = sorted([v for v in scores.values() if not v["dead"]], key=lambda v: v["z"])
        k30 = int(len(live) * 0.3)
        for i, v in enumerate(live):  # forced rates: bottom 30% KILL, next 30% FIX, top 40% KEEP
            v["rate"] = "KILL" if i < k30 else "FIX" if i < 2 * k30 else "KEEP"
            if v["rate"] == "KILL":
                v["dead"], v["why"] = True, "bottom 30% after calibration / " + v["why"]
        ranked = sorted(scores.values(), key=lambda v: (v["dead"], -v["z"]))
        alive_ = [v for v in ranked if not v["dead"]]
        cut = []
        caps = []  # C-004 E: general family caps, applied after dedupe, best-ranked members survive
        if fmt_ == "academic":
            caps += [("homelab", Q.homelab, 2),
                     ("collect-my-own data", lambda t: bool(re.search(r"collect my own|collect our own", t, re.I)), 2)]
        for fam, test, limit in caps:
            kept_ = 0
            for v in list(alive_):
                m = meta.get(key(v["name"])) or {}
                if test(v["name"] + " " + m.get("text", "") + " " + m.get("body", "")):
                    kept_ += 1
                    if kept_ > limit:
                        alive_.remove(v)
                        v["why"] = f"{fam} cap ({limit}) / " + v["why"]
                        cut.append(v)
        try:
            dead_ = [v for v in ranked if v["dead"]]
            need = [v for v in alive_ + dead_ if not v.get("vec")]
            for v, vec in zip(need, Q.embed([v["name"] for v in need])):
                v["vec"] = vec
            back = [v for v in alive_ if any(Q.cos(v["vec"], d["vec"]) > 0.88 for d in dead_)]
            for v in back:  # same idea, renamed: the kill stands
                v["dead"], v["why"] = True, "same as a killed idea / " + v["why"]
            alive_ = [v for v in alive_ if v not in back]
        except Exception as e:  # noqa: BLE001
            log(run, f"[{stamp()}] {me}: killed-twin check skipped ({e})")
        want_n = int((json.loads((run / "context" / "requirements.json").read_text()).get("count") or 0)
                     if (run / "context" / "requirements.json").exists() else 0)
        if want_n and len(alive_) > want_n:  # F2: final pick by max-marginal relevance, not score alone
            try:
                miss = [v for v in alive_ if not v.get("vec")]
                for v, vec in zip(miss, Q.embed([v["name"] for v in miss])):
                    v["vec"] = vec
                zs_ = sorted(v["z"] for v in alive_)
                for v in alive_:  # rank-normalised calibrated score for MMR
                    v["score"] = zs_.index(v["z"]) / max(1, len(zs_) - 1)
                uniq = []
                for v in alive_:  # alive_ is score-ordered: a near-copy of a higher-ranked idea is cut outright
                    (cut if any(Q.cos(v["vec"], u["vec"]) > 0.92 or Q.name_twin(v["name"], u["name"])
                                for u in uniq) else uniq).append(v)
                alive_ = uniq
                picked = Q.mmr(alive_, want_n)
                cut += [v for v in alive_ if v not in picked]
                alive_ = sorted(picked, key=lambda v: -v["z"])
            except Exception as e:  # noqa: BLE001
                log(run, f"[{stamp()}] {me}: MMR skipped ({e})")
        table = ("| # | Idea | From | Calibrated | Avg /20 | Scores | Main objection |\n|---|---|---|---|---|---|---|\n" + "\n".join(
            f"| {i} | {v['name']} | {v['src']} | {v['z']:+.2f} {v.get('rate', '')} | {v['total'] / v['n']:.1f} | "
            f"{', '.join(v['by'])} | {v['why']} |"
            for i, v in enumerate(alive_, 1)))
        killed = [v for v in ranked if v["dead"]]
        if cut:
            table += ("\n\n**Cut (near-copy of a higher-ranked idea, theme cap, or not picked for variety):** "
                      + "; ".join(f"{v['name']} ({v['why'].split(' / ')[0][:40] if 'cap' in v['why'] else 'similar'})"
                                  for v in cut))
        if killed:
            table += "\n\n**Killed (code gates or bottom 30%):** " + "; ".join(f"{v['name']} ({v['why']})" for v in killed)
        ctx += ("\n\n# RANKING (computed in code from the critics' tables; it is printed above your text verbatim. "
                "Write one card per non-killed idea in exactly this order, with these numbers only)\n" + table)
    brief = (run / "context" / "BRIEF.md").read_text()[:4000] if (run / "context" / "BRIEF.md").exists() else ""
    rq = run / "context" / "requirements.json"
    academic = rq.exists() and json.loads(rq.read_text()).get("format") == "academic"
    print(f"\n{C['amber']}{C['b']}WRITING FINAL.md{C['x']}\n")
    set_status(run, me, "working: writing FINAL.md")
    msgs = [{"role": "system", "content": LOCAL_RULES + "\nYou are Sophon writing the final result for the user."},
            {"role": "user", "content": f"BRIEF:\n{brief}\n\nTEAM RESULTS:\n{ctx}\n\nWrite FINAL.md: the answer to "
                                         "the brief first: the best ideas ranked by the critics' totals (drop "
                                         "KILL verdicts), each with " + ("its pitch, 10-second demo, how it works, "
                                         "first test" if demo else "research question, dataset, metric, 12-week "
                                         "plan and first experiment (copy them from the team files; write 'not "
                                         "given' if missing, never invent)" if academic else "the fields the team "
                                         "used (no demo or video sections unless the job asked for them)") + ", the critic's scores and "
                                         "strongest objection, and which worker it "
                                         "came from. Then a short list of what was killed and why. Up to 1800 "
                                         "words. Drop any command, flag, API or hardware a critic marked as invented or unverified, or label it "
                                         "'(unverified)'. If the brief asks for a number of items, deliver exactly that "
                                         "number in the main list (fill from FIX verdicts, marked as such, if too "
                                         "few survived)." + extra}]
    text, _, _ = stream_chat(model, msgs, out=sys.stdout, num_predict=3000)
    job = (run / "context" / "JOB.md").read_text() if (run / "context" / "JOB.md").exists() else brief
    m = re.search(r"\b(\d{1,2})\s*(?:to|-|–)\s*(\d{1,2})\b[^.\n]{0,40}?\b(?:ideas|items|topics|drafts|options)", job) \
        or re.search(r"\b(\d{1,2})\s+(?:[a-zA-Z-]+\s+){0,4}?(?:ideas|items|topics|drafts|options)\b", job)
    want = int(m.group(1)) if m else 0
    have = len(re.findall(r"^#{2,4}\s*\d+[.)]", text, re.M)) or len(re.findall(r"^###\s", text, re.M))
    survivors = len([r for r in table.split("**Killed")[0].splitlines() if re.match(r"\| \d+ \|", r)]) if table else 0
    if want and table and survivors < want:  # never pad with ideas the critics killed: say so honestly instead
        log(run, f"[{stamp()}] {me}: only {survivors} of the ideas survived the critics (job asks for {want}); "
                 f"not padding with killed ideas")
        text = (f"> Only {survivors} idea(s) survived the critics; the job asked for {want}. The rest were killed for "
                f"the reasons listed. Ask Sophon for another idea round (type: `more ideas`) rather than trusting "
                f"padded ones.\n\n" + text)
    elif want and have < want:
        log(run, f"[{stamp()}] {me}: FINAL has {have} items, job asks for {want}: topping up")
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"The job asks for at least {want} items but the main list has {have}. "
                  "Rewrite FINAL.md in full: merge near-duplicates (same mechanism, same trigger) into one, then "
                  f"bring the main list to {want} clearly different items using the team files (FIX verdicts and "
                  "unused ideas allowed, marked). Same format, numbered headings like '### 1. Name'."}]
        text2, _, _ = stream_chat(model, msgs, out=sys.stdout, num_predict=3600)
        if len(text2.split()) > 300:
            text = text2
    if table:  # the ranking comes from the critics' numbers, never from the writer's memory of them
        text = f"# FINAL\n\n## Ranking (computed from the critics' scores)\n\n{table}\n\n---\n\n{text.strip()}"
    (run / "FINAL.md").write_text(text.strip() + "\n")
    set_status(run, me, "idle")
    log(run, f"[{stamp()}] {me}: FINAL.md written ({len(text.split())} words)")
    print(f"\n\n{C['green']}FINAL.md written.{C['x']} Type here to ask for changes, or `swarm end`.")
