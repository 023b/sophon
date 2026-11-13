"""Local lead pipeline: requirements, angles, ideas, digest, critics, refine, judge, wow."""
import json
import math
import os
import re
import shutil
import sys
import time

from .agents import deliver, spawn
from .backends import big_stop, ollama_schema
from .config import BIG_LLM, CRITICS, LEADS, MODES, ROSTER, STYLE, C
from .editor import editor_pass
from .editor import enabled as editor_enabled
from .final import write_final
from .term import alive
from .util import latest, load, log, parse_msg, run_dir, set_status, stamp


def roster(deep, phantom=False):
    env = os.environ.get("SWARM_ROSTER")
    if env:
        return env.split(",")
    if phantom:  # in tests a faster MoE (Qwen3-Next 80B) writes 2-4x quicker than gpt-oss 120B; gpt-oss judges
        return [big_role("writer")]
    return (ROSTER * 3)[:6] if deep else ROSTER[:2]


def big_role(role):
    """The big model for a role ("writer" or "judge"): the first with that role in its config, else the first."""
    return next((k for k, v in BIG_LLM.items() if v.get("role", "both") in (role, "both")), next(iter(BIG_LLM)))


def critic_models(deep, phantom=False):
    env = os.environ.get("SWARM_CRITIC_MODELS")
    if env:
        return env.split(",")
    if phantom:  # gpt-oss reasons before judging and is harsh; Qwen3-Next as a judge flattered (20/20 'no objection')
        return [big_role("judge")] * 2
    return (CRITICS * 2)[:2]


ANGLES_SCHEMA = {"type": "object", "properties": {"angles": {"type": "array", "items": {"type": "object",
    "properties": {"name": {"type": "string"}, "persona": {"type": "string",
                   "enum": ["worker", "wildcard", "viral", "career", "feasibility", "researcher", "user-sim"]},
                   "angle": {"type": "string"}}, "required": ["name", "persona", "angle"]}}}, "required": ["angles"]}


def local_lead():
    """Fully local lead: a fixed pipeline (angles, ideas, critique, optional refine, final). The local model writes
    content; the code decides what happens next, so a small model cannot wander or loop."""
    import select
    run, me = run_dir(), os.environ["SWARM_ID"]
    model = os.environ.get("SWARM_MODEL", MODES["local"]["lead"][1])
    st = load(run)
    n = max(2, min(6, int(st.get("agents_target") or 3)))
    thorough = int(st.get("thorough") or 3)
    brief = (run / "context" / "BRIEF.md").read_text()[:5000] if (run / "context" / "BRIEF.md").exists() else ""
    notes = []  # the user's messages during the run, folded into later phases
    job = (run / "context" / "JOB.md").read_text() if (run / "context" / "JOB.md").exists() else brief
    from . import quality as Q
    req_f = run / "context" / "requirements.json"
    if req_f.exists():
        req = json.loads(req_f.read_text())
    else:  # F8: hard requirements extracted once, then enforced in code
        req = Q.requirements(job, ollama_schema, model)
        req_f.write_text(json.dumps(req, indent=1))
    demo = req["format"] == "demo"
    want = int(req.get("count") or 0)
    for fn in ("facts.md", "modules.md"):
        if Q.fact_file(fn).exists():
            shutil.copy(Q.fact_file(fn), run / "context" / fn)
    req_line = (f"Hard requirements (from the job, enforced in code): {want or 'no fixed'} {req['item_kind']}s; "
                f"order: {req['ordering'] or 'best first'}; forbidden: {', '.join(req['exclusions']) or 'nothing'}; "
                f"audience: {req['audience']}.")
    print(f"{C['dim']}{req_line}{C['x']}")

    def add_note(msg):
        """Keep only what the user actually typed: drop panel/system messages and terminal junk."""
        msg = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\[[A-D]|[^\x20-\x7e\u00a0-\uffff]", "", msg).strip()
        if not msg or re.match(r"(The user is (going to sleep|back)|\[swarm msg|REPORT:|sleep mode)", msg) \
                or "[swarm msg from" in msg:
            return False
        notes.append(msg[:500])
        return True

    def banner(t):
        print(f"\n{C['cyan']}{C['b']}━━ {t} {C['x']}")
        log(run, f"[{stamp()}] {me}: {t}")

    def wait_for(names, label):
        set_status(run, me, f"working: waiting for {label}")
        t0 = time.time()
        while True:
            busy = [x for x in names if alive(x) and (run / "status" / x).exists()
                    and (run / "status" / x).read_text().startswith("working")]
            if not busy:
                break
            print(f"{C['dim']}  waiting for {', '.join(busy)} ({int(time.time() - t0)}s){C['x']}   ", end="\r",
                  flush=True)
            try:
                r, _, _ = select.select([sys.stdin], [], [], 3)
            except KeyboardInterrupt:
                print(f"\n{C['dim']}(type a message for Sophon; `swarm end` stops the run){C['x']}")
                continue
            if r:
                line = sys.stdin.readline()
                if line.strip():
                    sender, msg = parse_msg(line)
                    if sender == "human" and add_note(msg):
                        print(f"\n{C['amber']}the user:{C['x']} {msg}  {C['dim']}(folded into the next phase){C['x']}")
        print(" " * 90, end="\r")
        set_status(run, me, "idle")

    def extra():
        return ("\nThe user added during the run: " + " | ".join(notes)) if notes else ""

    print(f"{C['cyan']}{C['b']}SOPHON{C['x']}  {C['dim']}local lead · {model} · {n} workers · thoroughness "
          f"{thorough}/5 · type here any time to steer{C['x']}")

    # 1. angles
    banner("PHASE 1  ANGLES")
    set_status(run, me, "working: choosing angles")
    try:
        n_ang = max(8, 2 * n)
        raw = ollama_schema(model, "You split a job into distinct angles for a team of workers. JSON only.",
                            f"BRIEF:\n{brief}\n\n{req_line}\n\nGive exactly {n_ang} clearly different angles for "
                            f"the job, each with a short lowercase id (letters only), a persona from the list, and a "
                            f"one-sentence angle. Every angle must differ from every other on at least two of: "
                            f"{', '.join(Q.AXES)}. Cover different ground (practical tool, research, career, "
                            f"{'filmable demo' if demo else 'what the user would pick'}, unusual). No two angles may "
                            f"share a mechanism.", ANGLES_SCHEMA)["angles"][:n_ang]
        angles = []
        for i in range(n):  # worker i owns angles i, i+n, ...; the others' angles are banned for it
            mine = raw[i::n]
            if mine:
                angles.append({"name": mine[0]["name"], "persona": mine[0]["persona"],
                               "angle": " / ".join(a["angle"] for a in mine),
                               "banned": [a["angle"] for j, a in enumerate(raw) if j % n != i]})
    except Exception as e:  # noqa: BLE001
        print(f"{C['red']}angle planning failed ({e}); using defaults{C['x']}")
        angles = []
    defaults = [("tool", "worker", "practical everyday tool"), ("research", "researcher", "measurable research project"),
                ("career", "career", "job portfolio value"), ("demo", "viral", "what a stranger gets in 10 seconds"),
                ("wild", "wildcard", "serious ideas nobody has tried"), ("taste", "user-sim", "what the user themselves would pick")]
    while len(angles) < n:
        d = defaults[len(angles)]
        angles.append({"name": d[0], "persona": d[1], "angle": d[2]})
    used = set()
    for a in angles:
        nm = re.sub(r"[^a-z]", "", a["name"].lower())[:14] or "w"
        while nm in used or nm in LEADS or nm in ("critic", "taste", "judge", "board", "panel", "q"):
            nm += "x"
        used.add(nm)
        a["name"] = nm
        print(f"  {C['green']}{nm}{C['x']} ({a['persona']}): {a['angle']}")
    set_status(run, me, "idle")

    # 2. ideas: models mixed for diversity, dispatched one model group at a time so each model loads once
    phantom = st.get("mode") == "phantom"
    deep = bool(st.get("sleep")) or thorough >= 4 or phantom
    rost = roster(deep, phantom)
    for i, a in enumerate(angles):
        a["model"] = rost[i % len(rost)]
    per = max(3, math.ceil(15 / n)) if thorough >= 3 else 3
    if want:
        per = max(per, math.ceil(want * 3 / n))  # F1: 3x the target raw, then code culls duplicates and the critics
    per = min(per, 8)
    samp = Q.sampling([a["name"] for a in angles], seed=int(time.time()))
    (run / "context" / "sampling.json").write_text(json.dumps(samp, indent=1))
    banner(f"PHASE 2  IDEAS ({per} per worker, models: {', '.join(sorted({a['model'] for a in angles}))})")
    fmt_general = ("Format for every item:\n### <name>\n- In one line: what it is\n- Why it fits the user: tie it to their "
                   "profile, course, machines or goals\n- How to do it: three concrete lines\n- Closest existing "
                   "work: name the paper, product or repo and what is different, or 'none known'\n- First step this "
                   "week: one concrete action\nStay strictly on the job's topic and type. " + STYLE + ".")
    fmt = ("Format for every idea:\n### <name>\n- Pitch: one line\n- 10-second demo: what a stranger sees, with the "
           "one physical action that triggers it\n- How it works: three lines of real engineering\n- Runs on: which "
           "of their machines\n- Closest existing thing: name the product, repo or paper it resembles and what is "
           "different, or 'none known'\n- First test this week: one concrete step\nNo anime or sci-fi names or "
           "framing. Avoid their kill words in names: benchmark, audit, dashboard, toolkit, framework, pipeline, "
           "assistant.")
    if not demo:
        fmt = fmt_general
    if req["format"] == "academic":  # F10: supervisor fit, not a wow test
        fmt += ("\n- Module: which programme module it builds on (from context/modules.md)\n- Research question: one "
                "sentence ending in '?'\n- Dataset: a real, named public benchmark (strongly preferred) or 'collect my "
                "own' plus a 'Data justification:' line; at most two topics in the whole run may collect their own (or 'collect my own: how')\n- Metric: how success is "
                "measured\n- 12-week plan: three lines")
    fmt += ("\n- Claims: every command, flag, API, model size or number you used, each tagged [F] if it is in "
            "context/facts.md or [NEW] if not. A capability not in the facts sheet is written as 'requires "
            "building X', never as a made-up flag.\nKeep each idea under 120 words.")
    groups = {}
    for a in angles:
        groups.setdefault(a["model"], []).append(a)
    for model, group in groups.items():
        for a in group:
            others = "; ".join(a.get("banned", [])[:8])
            ban = (f" Other workers own these angles, do not use them: {others}." if others else "")
            coll, allp = "", []
            # collision mashups only suit demo jobs; on study or practical jobs they produce noise
            if (req["format"] == "demo" or os.environ.get("SWARM_COLLIDE") == "1") and \
                    os.environ.get("SWARM_COLLIDE", "") != "0":
                allp = Q.collisions(per * len(angles), int(samp[angles[0]["name"]]["seed"]))
            if allp:  # needs facts/assets.md (your own things) in the data folder; skipped without it
                k0 = angles.index(a) * per
                pairs = allp[k0:k0 + per]
                coll = (" " + Q.COLLISION_RULES + " Slots: " + " ".join(
                    f"[{k + 1}] ASSET: {x} / MECHANISM: {y}." for k, (x, y) in enumerate(pairs)))
                samp[a["name"]]["collisions"] = pairs
                (run / "context" / "sampling.json").write_text(json.dumps(samp, indent=1))
            task = (f"Angle: {a['angle']}. Write {per} distinct ideas for the brief from this angle, each with a "
                    f"different mechanism. Read context/facts.md first. {req_line}{ban} "
                    f"{'' if coll else samp[a['name']]['card']}{coll} {fmt}{extra()}")
            if alive(a["name"]):
                deliver(run, me, a["name"], task)
            else:
                try:
                    spawn(run, a["name"], a["persona"], kickoff=task, engine="local", model=model)
                except SystemExit as e:
                    print(f"  {C['red']}{a['name']} refused: {e}{C['x']}")
        time.sleep(3)
        wait_for([a["name"] for a in group], f"ideas from {model}")
    names = [a["name"] for a in angles]

    def idea_files():
        return " ".join(f"out/{x}.md" for x in names if (run / "out" / f"{x}.md").exists())

    def ideas_digest():
        """F2 + F3: every idea (latest version), near-duplicates merged by embeddings, invented flags and forbidden
        topics found by code, so critics read less and judge with the mechanical facts in front of them."""
        ideas = []
        for x in names:
            f = run / "out" / f"{x}.md"
            if f.exists():
                ideas += Q.parse_ideas(latest(f.read_text(), 14000), x)
        try:
            kept, ndup = Q.dedupe(ideas)
        except Exception as e:  # noqa: BLE001
            print(f"  {C['red']}dedupe skipped ({e}){C['x']}")
            kept, ndup = ideas, 0
        bad_words = Q.exclusion_words(req)
        parts, meta = [], {}
        for i in kept:
            i["claims"] = Q.check_claims(i["body"])
            hits = Q.forbidden_hits(i["body"], bad_words)
            notes_ = []
            if i["claims"]:
                notes_.append("invented: " + "; ".join(i["claims"]))
            if hits:
                notes_.append("forbidden topic words: " + ", ".join(hits))
            if req["format"] == "academic":
                ds = Q.check_dataset(i["body"])
                if ds:
                    notes_.append(ds)
                if not re.search(r"research question|\?", i["body"], re.I):
                    notes_.append("no research question: FIX at best")
                try:
                    mod, ms, hs = Q.module_fit(i["body"])
                    i["module_miss"] = f"off-programme: closer to homelab infra ({hs:.2f}) than to {mod} ({ms:.2f})" \
                        if hs > ms else ""
                    if i["module_miss"]:
                        notes_.append(i["module_miss"])
                except Exception:  # noqa: BLE001
                    i["module_miss"] = ""
            twins = sorted(set(i.get("absorbed", [])) - {i["name"]})
            if twins:
                notes_.append("merged duplicates: " + ", ".join(twins)[:200])
            auto = ("\nAUTO-CHECK (code, trust it): " + " | ".join(notes_)) if notes_ else ""
            parts.append(f"{i['body'][:700]}\n(source worker: {i['src']}){auto}")
            meta[i["name"]] = {"src": i["src"], "vec": i.get("vec"), "claims": i["claims"], "forbidden": hits,
                               "body": i["body"][:900],
                               "module_miss": i.get("module_miss", ""),
                               "no_rq": req["format"] == "academic" and not re.search(r"research question|\?",
                                                                                     i["body"], re.I),
                               "text": Q.idea_text(i)}
        (run / "context" / "ideas.json").write_text(json.dumps(meta))
        (run / "context" / "ideas-digest.md").write_text(
            f"# All ideas (latest versions; {ndup} near-duplicates merged by code)\n\n" + "\n\n".join(parts) + "\n")
        (run / "context" / "ideas-digest-rev.md").write_text(  # C-004 C1: same ideas, reversed order (order bias)
            "# All ideas (reversed order)\n\n" + "\n\n".join(reversed(parts)) + "\n")
        print(f"  {C['dim']}digest: {len(kept)} ideas, {ndup} duplicates merged, "
              f"{sum(1 for i in kept if i['claims'])} with invented flags{C['x']}")
        return "context/ideas-digest.md"

    # 3. critique: two different models judge every idea
    def critique(round_name, judges):
        banner(f"{round_name}")
        task = (f"Read {ideas_digest()}. For EVERY idea give one row in a single Markdown table: idea name (exactly "
                f"as written), source worker, usefulness 1-5, {'wow in 10 seconds' if demo else ('research value' if req['format'] == 'academic' else 'fit for the user')} 1-5, feasibility on their hardware 1-5, novelty 1-5, "
                f"total, verdict KEEP / FIX / KILL, closest existing project, invented or unverified claims (made-up commands, "
                f"flags, APIs or hardware; 'none' if clean), one-line reason (quote at most 8 words). Every idea in the file needs a row. Be harsh: KILL "
                f"about a quarter (rehashes of existing products or papers, vague ideas, anything needing money or "
                f"hardware they do not have{', anything that fails their 10-second test' if demo else ', anything off the job topic'}). Give every idea a real "
                f"objection. Scores of 5 are rare. Use their profile as the standard. Any idea with an AUTO-CHECK 'invented' "
                f"line is FIX at best; any with forbidden topic words is KILL.\n\n"
                f"{Q.ANCHORS if demo else Q.ACADEMIC_ANCHORS if req['format'] == 'academic' else ''}{extra()}")
        for c, persona, model in judges:
            try:
                if alive(c):
                    deliver(run, me, c, task)
                else:
                    spawn(run, c, persona, kickoff=task, engine="local", model=model)
            except SystemExit as e:
                print(f"  {C['red']}{c} refused: {e}{C['x']}")
            time.sleep(3)
            wait_for([c], f"{c} ({model})")
            deliver(run, me, c, "PASS B (order check): same instructions, same table format, but the ideas are "
                                "in reversed order: Read context/ideas-digest-rev.md and score every idea "
                                "afresh. Do not look at your previous table." + extra())
            time.sleep(3)
            wait_for([c], f"{c} pass B")

    sp = json.loads((run / "context" / "sampling.json").read_text())
    sp.update({c: {"num_predict": 5000, "temperature": 0, "seed": 7} for c in ("critic", "taste", "judge")})
    (run / "context" / "sampling.json").write_text(json.dumps(sp, indent=1))
    cms = critic_models(deep, phantom)
    judges = [("critic", "skeptic", cms[0])] + ([("taste", "user-sim", cms[-1])] if thorough >= 3 else [])
    critique("PHASE 3  CRITIQUE", judges)

    # 4. refine, then an independent second judgement (long runs)
    if deep or thorough >= 5:
        banner("PHASE 4  REFINE")
        cfiles = " ".join(f"out/{c}.md" for c, _, _ in judges)
        for model, group in groups.items():
            for a in group:
                deliver(run, me, a["name"], f"Read {cfiles}. Rewrite only your ideas: fix every FIX idea so it "
                                            f"answers the critic's reason, replace each KILL idea with a new, "
                                            f"stronger one from your angle, keep the KEEP ones. Same format. Write "
                                            f"the full updated list.{extra()}")
            time.sleep(3)
            wait_for([a["name"] for a in group], f"refinements from {model}")
        critique("PHASE 5  SECOND JUDGEMENT", [("judge", "user-sim", cms[0])])

    # 6. final, then the wow pass on long runs: the one idea sharpened until it lands
    banner("FINAL")
    write_final(run, me, extra(), model=big_role("writer") if phantom else cms[0], demo=demo)
    if deep and demo:
        banner("WOW PASS")
        try:
            top = [ln.split("|")[2].strip() for ln in (run / "FINAL.md").read_text().splitlines()
                   if re.match(r"\| [1-3] \|", ln)]
            spawn(run, "wow", "wow", engine="local", model=cms[-1] if phantom else cms[0],
                  kickoff=(f"Read FINAL.md and context/facts.md. Do your job on ONE of the team's top ideas: "
                           f"{', '.join(top) or 'the first in the ranking'}. Do not switch to a different idea and "
                           f"do not fall back to 'unplug the charger and the model shrinks': the team already has "
                           f"that. Every model, flag and number must come from context/facts.md or be marked "
                           f"(estimate).{extra()}"))
            time.sleep(3)
            wait_for(["wow"], "the wow pass")
            w = run / "out" / "wow.md"
            if w.exists():
                final = run / "FINAL.md"
                wtxt = latest(w.read_text(), 9000).split("\n", 2)[-1].strip()
                bad = Q.check_claims(wtxt) + [f"{u} is not on the facts sheet" for u in Q.unit_claims(wtxt, job)]
                if bad:
                    wtxt += "\n\n> Code check: " + "; ".join(bad) + ". Treat those steps as needing a rewrite."
                final.write_text("# Start here\n\n" + wtxt + "\n\n---\n\n" + final.read_text())
                log(run, f"[{stamp()}] {me}: wow pass added to the top of FINAL.md")
        except SystemExit as e:
            print(f"  {C['red']}wow pass refused: {e}{C['x']}")
    if phantom:
        big_stop()  # give the model host its RAM back; a follow-up swaps a big model in again
    if editor_enabled(run):  # C-004 stop rule: one Sonnet editor pass over the top cards
        banner("SONNET EDITOR")
        editor_pass(run, job, req, me)
    log(run, f"[{stamp()}] {me}: pipeline complete. Waiting for the user's follow-ups.")

    # 6. follow-ups: route `name: task` to a worker, anything else rewrites FINAL.md with the request
    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            continue
        if not line:
            time.sleep(1)
            continue
        if not line.strip():
            continue
        sender, msg = parse_msg(line)
        if sender != "human":
            continue
        m = re.match(r"([a-z0-9-]+):\s*(.+)", msg)
        if m and alive(m[1]):
            deliver(run, me, m[1], m[2])
            print(f"  {C['cyan']}> {m[1]}{C['x']}: {m[2]}")
            continue
        if not add_note(msg):
            continue
        banner("REVISING FINAL.md")
        write_final(run, me, "\nThe user's follow-up request (apply it): " + notes[-1], demo=demo)
