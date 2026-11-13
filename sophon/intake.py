"""Intake: triage, clarifying questions, mode choice, answering for the user while they sleep."""
import json
import os
import re
import subprocess
import sys
import time

from .agents import agent_command, kill, report, spawn
from .backends import claude_json, ollama_json
from .config import (
    CLAUDE,
    LEAD,
    MODE_GUIDE,
    MODES,
    NO_CLAUDE_MODES,
    SWARM,
    TRIAGE_MODEL,
    C,
    local_best,
)
from .term import alive
from .util import lead_of, load, log, run_dir, stamp, state

TRIAGE_DEFAULTS = {"understood": False, "confidence": 0.0, "stakes": "high", "mode": "light", "agents": 3,
                   "thorough": 3, "questions": [], "reason": ""}


def clean_triage(d):
    """Fill missing keys and coerce types, so a partial or sloppy model reply cannot crash the intake."""
    if not isinstance(d, dict):
        return None
    out = dict(TRIAGE_DEFAULTS, **{k: v for k, v in d.items() if v is not None})
    try:
        out["confidence"] = max(0.0, min(1.0, float(out["confidence"])))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    u = out["understood"]
    out["understood"] = u if isinstance(u, bool) else str(u).strip().lower() in ("true", "yes", "1")
    out["stakes"] = "low" if str(out["stakes"]).strip().lower() == "low" else "high"
    out["mode"] = str(out["mode"]).strip().lower()
    for k, lo, hi, dflt in (("agents", 1, 10, 3), ("thorough", 1, 5, 3)):
        try:
            out[k] = max(lo, min(hi, int(float(out[k]))))
        except (TypeError, ValueError):
            out[k] = dflt
    out["questions"] = [str(q) for q in out["questions"]] if isinstance(out["questions"], list) else []
    return out


class Cancel(Exception):
    pass


def ask(prompt):
    try:
        a = input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancel
    if a.strip().lower() in ("/quit", "/exit", "/cancel"):
        raise Cancel
    return a


def cancel_run(run):
    print("\n")
    try:
        a = input("Cancel this run and close all its windows? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        a = "y"
    if a in ("", "y", "yes"):
        log(run, f"[{stamp()}] human: run cancelled from intake")
        (run / "STOP").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        os.environ["SWARM_ID"] = "human"
        kill(run, "all")  # closes this window too
        sys.exit(0)
    return False


def intake():
    run = run_dir()
    while True:
        try:
            return intake_flow(run)
        except Cancel:
            cancel_run(run)
            print("Not cancelled. Starting the intake again.\n")


def intake_flow(run):
    import select
    bar = "=" * 70
    print(f"{bar}\n SOPHON  (local-first lead; the only agent with memory access)\n{bar}")
    st0 = load(run)
    sleeping = bool(st0.get("sleep"))
    preset = (run / "context" / "JOB.md").read_text().strip() if (run / "context" / "JOB.md").exists() else ""
    if sleeping:
        print(f"{C['amber']}SLEEP MODE{C['x']}: The user is away. Questions are answered on their behalf, nothing waits for them.\n")
    lines = preset.splitlines() if preset else []
    if preset:
        print("Job (given at start):\n  " + preset.replace("\n", "\n  ") + "\n")
    else:
        print("Type the job. Finish with an empty line.   Ctrl+C or /quit cancels the run and closes its windows.\n")
    while not preset:
        line = ask("> " if not lines else "  ")
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    job = "\n".join(lines)
    (run / "context" / "JOB.md").write_text(job + "\n")
    log(run, f"[{stamp()}] human -> {LEAD}: job received ({len(job)} chars)")
    pf = run / "context" / "profile.md"
    profile = pf.read_text()[:3000] if pf.exists() else "(no profile: judge by the job text)"

    # 1. local triage: did the local model understand the job?
    print("\n[1/4] Local triage on the model host ...")
    tri, who = None, "local"
    try:
        tri = ollama_json(f"You triage jobs for a swarm of AI agents. Read the job and answer as JSON.\n{MODE_GUIDE}\n"
                          f"Ask up to 4 short questions only if they would change the plan.\n\nJob:\n{job}")
    except Exception as e:  # noqa: BLE001
        print(f"  local model unavailable ({e}); asking Claude instead")
    tri = clean_triage(tri)
    if (not tri or not tri["understood"] or tri["confidence"] < 0.5) and load(run).get("mode_hint") in NO_CLAUDE_MODES:
        print("  local model is unsure, but this run is local-only: continuing with local defaults")
        tri = tri or dict(TRIAGE_DEFAULTS, understood=True, reason="local-only defaults")
        tri["understood"], tri["confidence"] = True, max(tri["confidence"], 0.5)
    if not tri or not tri["understood"] or tri["confidence"] < 0.5:
        print("  local model is unsure what the job is; escalating to Claude for triage")
        who = "claude"
        try:
            tri = claude_json(f"Triage this job for a multi-agent swarm. Reply with ONLY a JSON object with keys "
                              f"understood, confidence (0-1), stakes (low|high), mode, agents, thorough, questions "
                              f"(list, max 4), reason.\n{MODE_GUIDE}\n\nJob:\n{job}")
        except Exception as e:  # noqa: BLE001
            print(f"  Claude unavailable too ({e}); continuing with defaults")
            tri = None
        tri = clean_triage(tri) or dict(TRIAGE_DEFAULTS, understood=True, reason="defaults")
    print(f"  triage by {who}: understood={tri['understood']} confidence={tri['confidence']:.2f} stakes={tri['stakes']}")
    (run / "context" / "triage.json").write_text(json.dumps(dict(tri, by=who), indent=1))

    # 2. clarification: two local questioners debate and keep only the important questions
    print("\n[2/4] Two questioners on the model host (q1 drafts, q2 cuts to what matters). Type s + Enter to skip.")
    os.environ["SWARM_ID"] = "human"

    def run_q(q, task):
        out = run / "out" / f"{q}.md"
        try:
            spawn(run, q, "questioner", engine="local", model=TRIAGE_MODEL, kickoff=task)
        except SystemExit as e:
            print(f"  could not start {q}: {e}")
            return ""
        deadline = time.time() + 300
        time.sleep(2)
        while time.time() < deadline:
            busy = (run / "status" / q).exists() and (run / "status" / q).read_text().startswith("working")
            if not busy and out.exists():
                break
            try:
                r, _, _ = select.select([sys.stdin], [], [], 2)
            except KeyboardInterrupt:
                raise Cancel
            if r:
                line = sys.stdin.readline().strip().lower()
                if line in ("/quit", "/exit", "/cancel"):
                    raise Cancel
                if line == "s":
                    return ""
        return out.read_text().split("\n## Task")[-1] if out.exists() else ""

    draft = run_q("q1", "Read the job in context/JOB.md. List the 6 questions for the user whose answers would most change "
                        "how this job is done (scope, what done looks like, constraints, depth, audience). Each "
                        "question must offer 2 to 4 concrete options in brackets so he can answer in one word, for "
                        "example: 'Who is it for? (you only / recruiters / Shorts viewers)'. Skip anything already "
                        "answered by the job text or their profile. Numbered lines only.")
    final_q = run_q("q2", "Read out/q1.md. Keep only the 4 questions whose answers would change the work the most, "
                          "rewrite each to be short and clear, keep the options in brackets. Output exactly 4 "
                          "numbered lines and nothing else.") if draft else ""
    os.environ["SWARM_ID"] = LEAD
    qfile = run / "out" / "questions.md"
    picked = [l for l in final_q.splitlines() if re.match(r"\s*\d+[.)]", l)] or \
             [l for l in draft.splitlines() if re.match(r"\s*\d+[.)]", l)][:4]
    qfile.write_text("\n".join(picked) + "\n")
    questions = [re.sub(r"^\s*\d+[.)]\s*", "", l).strip() for l in picked]
    if not questions:
        questions = [q for q in tri.get("questions", []) if q][:4]
    for q in ("q1", "q2"):
        if alive(q):
            kill(run, q)
    answers = []
    if questions and sleeping:
        print("\nQuestions (answered on the user's behalf, they are asleep):")
        for i, q in enumerate(questions[:6], 1):
            a, who = answer_as_user(run, q, allow_claude=load(run).get("mode_hint") not in NO_CLAUDE_MODES)
            print(f"  {i}. {q}\n     > {a}  {C['dim']}({who}){C['x']}")
            answers.append((q, f"{a} (answered by {who} while the user slept)"))
        questions = []
    if questions:
        print("\nQuestions (Enter to skip one):")
        for i, q in enumerate(questions[:6], 1):
            a = ask(f"  {i}. {q}\n     > ")
            if a.strip():
                answers.append((q, a.strip()))

    # 3. decide the mode: local decides only when sure and low-stakes, otherwise Claude decides
    print("\n[3/4] Choosing the mode ...")
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in answers) or "(no answers)"
    ctx = f"{MODE_GUIDE}\n\nThe user (short profile):\n{profile}\n\nJob:\n{job}\n\nClarifications:\n{qa}"
    dec, who = None, "local"
    try:
        dec = ollama_json("Choose how to run this job. Answer as JSON. Set confidence honestly (0-1).\n" + ctx)
    except Exception as e:  # noqa: BLE001
        print(f"  local model unavailable ({e})")
    hint = load(run).get("mode_hint")
    dec = clean_triage(dec)
    local_ok = (dec and dec["understood"] and dec["confidence"] >= 0.75 and dec["stakes"] == "low"
                and dec["mode"] != "claude")
    if hint in MODES:
        dec = dict(dec or tri, mode=hint, reason=f"mode forced with swarm go --mode {hint}")
        who = "user-sim"
    elif not local_ok:
        why = ("no local answer" if not dec else "local model not confident" if dec["confidence"] < 0.75 else
               "high stakes" if dec["stakes"] == "high" else "local model chose fully Claude")
        print(f"  escalating to Claude ({why})")
        who = "claude"
        try:
            dec = claude_json("You decide how a multi-agent swarm runs a job for the user. Reply with ONLY a JSON object "
                              "with keys understood, confidence, stakes, mode, agents, thorough, questions ([]), "
                              f"reason (one sentence).\nLocal model's proposal: {json.dumps(dec)}\n\n" + ctx)
        except Exception as e:  # noqa: BLE001
            print(f"  Claude unavailable ({e}); using the local proposal or defaults")
            who = "local (fallback)"
            dec = clean_triage(dec) or dict(TRIAGE_DEFAULTS, understood=True, reason="defaults")
    mode = dec["mode"] if dec.get("mode") in MODES else "light"
    agents = max(1, min(10, int(dec.get("agents", 3))))
    thorough = max(1, min(5, int(dec.get("thorough", 3))))
    print(f"  decided by {who}: mode {mode} ({MODES[mode]['label']}), {agents} agents, thoroughness {thorough}")
    (run / "context" / "decision.json").write_text(json.dumps({"mode": mode, "agents": agents, "thorough": thorough,
                                                               "by": who, "reason": dec.get("reason", "")}, indent=1))
    print(f"  reason: {dec.get('reason', '')}")
    hints = load(run)
    if hints.get("agents_hint"):
        agents = int(hints["agents_hint"])
    if hints.get("thorough_hint"):
        thorough = int(hints["thorough_hint"])
    from . import openrouter as OR
    print(f"OpenRouter: {OR.status_line()}")
    o = [] if sleeping else ask("\nEnter to accept, or type e.g. 'heavy 8 4' (mode agents thoroughness),\n"
                                "add 'or off|free|paid' to change OpenRouter: ").split()
    if "or" in o:
        i = o.index("or")
        want = o[i + 1] if i + 1 < len(o) else ""
        del o[i:i + 2]
        if want not in ("off", "free", "paid"):
            print("  OpenRouter unchanged (use or off, or free or or paid).")
        elif want != "off" and not OR.key():
            print("  OpenRouter unchanged: no key saved (swarm or key set, or the panel).")
        else:
            OR.save_settings(mode=want)
            print(f"  OpenRouter: {OR.status_line()}")
    if o:
        proposed = (mode, agents, thorough)
        mode = o[0] if o[0] in MODES else mode
        agents = int(o[1]) if len(o) > 1 and o[1].isdigit() else agents
        thorough = int(o[2]) if len(o) > 2 and o[2].isdigit() else thorough
        who = "the user"
        if (mode, agents, thorough) != proposed:  # the model's reason argued for its own pick, not the user's
            dec["reason"] = (f"the user chose {mode}, {agents} agents, thoroughness {thorough} (proposed: "
                             f"{proposed[0]}, {proposed[1]}, {proposed[2]}; {dec.get('reason', '')})")
            (run / "context" / "decision.json").write_text(json.dumps(
                {"mode": mode, "agents": agents, "thorough": thorough, "by": who, "reason": dec["reason"]}, indent=1))

    # 4. write the brief and become the lead
    brief = (f"# Brief (the lead intake, {time.strftime('%Y-%m-%d %H:%M')})\n\n## Job (The user's words)\n{job}\n\n"
             f"## Clarifications\n{qa}\n\n## Run settings\n- mode: {mode} ({MODES[mode]['label']}), decided by {who}\n"
             f"- target agents: {agents}\n- thoroughness: {thorough} of 5\n- reason: {dec.get('reason', '')}\n"
             f"- OpenRouter: {OR.settings()['mode']} (off: never; free: free models only; paid: within caps)\n"
             + ("\n## the user is asleep\nThey will read the result in the morning. Never wait for them and never stop to "
                "ask them. If you need a decision they would make, run `swarm ask --type TYPE \"question\"` "
                "with TYPE one of preference, scope, priority, format or clarify (answered on their behalf from their "
                "profile; any other question is refused and goes to out/needs-you.md) or decide yourself and write the assumption into FINAL.md. Anything that needs their "
                "explicit consent (spending, deleting, publishing, sending, downloads over 500 MB, GPU jobs beyond "
                "this run) goes into a list at the end of FINAL.md instead. Use the hours: more rounds of critique "
                "and refinement beat stopping early.\n" if sleeping else ""))
    if load(run).get("test"):
        brief += ("\n## Test run\nThis is an overnight test of the swarm itself. Do the job for real, but do not "
                  "write to memory (approve or reject memo requests in requests/ only) and do not change anything "
                  "outside this run folder. Finish with FINAL.md, then `swarm end`.\n")
    (run / "context" / "BRIEF.md").write_text(brief)
    with state(run) as st:
        st.update(mode=mode, agents_target=agents, thorough=thorough)
        engine, model = MODES[mode]["lead"]
        if engine == "claude" and st.get("lead_model"):
            model = st["lead_model"]
        st["agents"][LEAD].update(engine=engine, model=model)
    log(run, f"[{stamp()}] {LEAD}: mode {mode}, {agents} agents, thoroughness {thorough} (decided by {who})")
    print(f"\n[4/4] Sophon takes over ({engine} {model}) ...\n")
    kickoff = ("You are Sophon, the lead. Read context/BRIEF.md and the rest of context/, then build the team for "
               "the run settings with `swarm spawn` and run the job. The user is watching every terminal.")
    queued = run / "inbox" / f"{LEAD}.md"
    if queued.exists():  # reports that arrived during intake
        kickoff += f" Messages that arrived during intake: {queued.read_text().strip()}"
        queued.unlink()
    cmd, env = agent_command(run, st, LEAD, "sophon", engine, model, kickoff)
    os.environ.update(env)
    if engine == "claude":  # nobody else is watching this session, so accept the folder trust prompt ourselves
        subprocess.Popen([sys.executable, SWARM, "trust-watch", LEAD], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.execvp(cmd[0], cmd)


def answer_as_user(run, question, allow_claude=True):
    """Answer for the user while they are away. Claude Sonnet with their profile when the mode allows Claude, else local."""
    pf = run / "context" / "profile.md"
    prof = pf.read_text()[:7000] if pf.exists() else "(no profile)"
    job = (run / "context" / "JOB.md").read_text()[:2000] if (run / "context" / "JOB.md").exists() else ""
    prompt = (f"You answer on behalf of the user, who is asleep. Use their profile and the job to give the answer they would "
              f"most likely give: short, decisive, in their interest, no hedging. If it needs their explicit consent "
              f"(spending, deleting, publishing, sending, downloads over 500 MB), answer 'no, wait for the user'.\n\n"
              f"PROFILE:\n{prof}\n\nJOB:\n{job}\n\nQUESTION: {question}\n\nAnswer in one to three sentences.")
    st = load(run)
    if allow_claude and st.get("mode") not in NO_CLAUDE_MODES:
        try:
            r = subprocess.run([CLAUDE, "-p", "--model", "sonnet", prompt], capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip(), "claude"
        except Exception:  # noqa: BLE001
            pass
    try:
        import urllib.request
        body = json.dumps({"model": local_best(), "stream": False, "think": False,
                           "options": {"temperature": 0.3, "num_predict": 200},
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11435/api/chat", body),
                                             timeout=600))
        return r["message"]["content"].strip(), "local user-sim"
    except Exception as e:  # noqa: BLE001
        return f"(no answer available: {e}); decide yourself and note the assumption", "none"


ASK_TYPES = {"preference": "taste or style", "scope": "which of the listed options", "priority": "which first",
             "format": "length or layout", "clarify": "what the job text meant"}


# backstop: even a typed question is not answered for them if it touches money, data, the outside or other folders
ASK_RISKY = re.compile(r"\b(spend|pay|buy|purchas|cost|credit|delet|remov|rm\b|wipe|overwrit|send|email|post|publish|"
                       r"push|upload|share|download|install|sudo|root|password|key|token|account|subscri|sign ?up)"
                       r"|https?://|~/|/home/|/etc/", re.I)


def ask_cmd(run, question, qtype=None):
    if not question:
        sys.exit('usage: swarm ask --type preference|scope|priority|format|clarify "question for the user"')
    st = load(run)
    sender = os.environ.get("SWARM_ID", "human")
    if not st.get("sleep"):
        report(run, [f"QUESTION for the user from {sender}: {question}"]) if sender != lead_of(st) else None
        print("The user is awake: question posted to the board for them (and the lead). Keep working meanwhile.")
        log(run, f"[{stamp()}] {sender} asks the user: {question}")
        return
    if qtype not in ASK_TYPES or ASK_RISKY.search(question):
        why = (f"type '{qtype}' is not one they let me answer" if qtype not in ASK_TYPES
               else "it touches spending, deleting, sending, accounts or files outside the run")
        with open(run / "out" / "needs-you.md", "a") as f:
            f.write(f"- [{stamp()}] {sender}: {question}\n")
        log(run, f"[{stamp()}] {sender} asked (The user asleep, not answered: {why}): {question}")
        print(f"Not answered: {why}. Added to out/needs-you.md for the morning; copy it into FINAL.md's "
              f"needs-you list. Carry on without it. Answerable types: "
              + ", ".join(f"{k} ({v})" for k, v in ASK_TYPES.items()))
        return
    ans, who = answer_as_user(run, question)
    log(run, f"[{stamp()}] {sender} asked (The user asleep): {question}  -> {who}: {ans}")
    with open(run / "out" / "answers-while-asleep.md", "a") as f:
        f.write(f"- [{stamp()}] {sender}: {question}\n  - {who}: {ans}\n")
    print(f"Answer ({who}, on the user's behalf while they sleep): {ans}")
