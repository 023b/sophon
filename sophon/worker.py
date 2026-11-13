"""Local worker loop: context gathering and the task loop for Ollama and OpenRouter models."""
import json
import os
import re
import sys
import time

from .agents import report_as
from .backends import stream_chat
from .config import HOST_LABEL, LOCAL_RULES, MODES, PERSONAS, C
from .util import latest, load, parse_msg, run_dir, set_status, stamp


def role_text(persona, name):
    f = PERSONAS / f"{persona}.md"
    if f.exists():
        return f.read_text()[:3500]
    return f"Your role: {persona}"


EXTERNAL_PROFILE_SECTIONS = ("Decision rules", "Judging")


def external_profile(text):
    """Only the generic judging rules from the profile, for models outside the user's machines (OpenRouter).
    Who they are, hardware, rejections, voice, budget and project notes never leave."""
    keep, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = any(line[3:].startswith(s) for s in EXTERNAL_PROFILE_SECTIONS)
        if on:
            keep.append(line)
    return "\n".join(keep)[:2500]


def gather_context(run, text, own=None, external=False):
    """Brief + profile summary + every run file the text mentions (out/x.md, context/y.md, msgs/z.md, FINAL.md).
    external=True (OpenRouter): the profile is cut to its generic judging rules."""
    parts = []
    brief = run / "context" / "BRIEF.md"
    if brief.exists():
        parts.append("# BRIEF\n" + brief.read_text()[:5000])
    prof = run / "context" / "profile.md"
    if prof.exists() and external:
        rules = external_profile(prof.read_text())
        if rules:
            parts.append("# JUDGING RULES (the user's taste, judge everything by this)\n" + rules)
    elif prof.exists():
        parts.append("# USER PROFILE (the user's taste, rules and rejections: judge everything by this)\n" + prof.read_text()[:7500])
    seen = set()
    for ref in re.findall(r"((?:out|context|msgs)/[\w./-]+\.md|FINAL\.md)", text):
        f = run / ref
        if ref in seen or not f.exists() or f.name in ("BRIEF.md", "profile.md"):
            continue
        seen.add(ref)
        parts.append(f"# FILE {ref} (latest version)\n" + latest(f.read_text(), 24000 if "digest" in ref else 7000))
    return "\n\n".join(parts)


def local_worker():
    run, me = run_dir(), os.environ["SWARM_ID"]
    model = os.environ.get("SWARM_MODEL", MODES["local"]["worker"][1])
    st = load(run)
    persona = st["agents"].get(me, {}).get("persona", "worker")
    system = LOCAL_RULES + "\n\n" + role_text(persona, me)
    history = []
    print(f"{C['cyan']}{C['b']}{me.upper()}{C['x']}  {C['dim']}{persona} · {model} on {'OpenRouter' if model.startswith('or:') else HOST_LABEL} · tasks arrive here; "
          f"Ctrl+C stops the current answer{C['x']}\n")

    def do(sender, task):
        set_status(run, me, f"working: {task[:60]}")
        print(f"\n{C['amber']}{C['b']}TASK{C['x']} {C['dim']}from {sender}{C['x']}  {task}\n")
        external = model.startswith("or:")
        if external and load(run).get("private"):
            print(f"{C['red']}refused: this run is private, OpenRouter is blocked{C['x']}")
            set_status(run, me, "idle")
            report_as(run, me, f"{me} refused a task: the run is private, so nothing goes to OpenRouter. "
                               f"Use a local worker.")
            return
        ctx = gather_context(run, task, me, external=external)
        msgs = [{"role": "system", "content": system}]
        if ctx:
            msgs.append({"role": "user", "content": "Context for all tasks:\n\n" + ctx})
            msgs.append({"role": "assistant", "content": "I have read the context. Send the task and I will do it in full."})
        msgs += ([] if task.startswith("PASS B") else history[-4:]) + [{"role": "user", "content": task}]
        print(f"{C['dim']}thinking on the model host ...{C['x']}", end="\r", flush=True)
        samp = run / "context" / "sampling.json"  # F1: per-worker temperature and seed set by the lead
        opts = json.loads(samp.read_text()).get(me) if samp.exists() else None
        npred = int((opts or {}).get("num_predict") or (2600 if opts else 1800))
        opts = {k: v for k, v in (opts or {}).items() if k in ("temperature", "seed")}
        try:
            text, wait, rate = stream_chat(model, msgs, out=sys.stdout, num_predict=npred, options=opts)
            if len(text.split()) < 25 and len(task.split()) > 12:  # an acknowledgement, not the work: ask once more
                print(f"\n{C['amber']}reply too short ({len(text.split())} words), asking again{C['x']}")
                text, wait, rate = stream_chat(model, msgs + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "That was only an acknowledgement. Do the task now, in full, in the "
                                                "requested format. No preamble."}],
                    out=sys.stdout, num_predict=npred, options=opts)
        except KeyboardInterrupt:
            print(f"\n{C['red']}stopped{C['x']}")
            set_status(run, me, "idle")
            return
        except Exception as e:  # noqa: BLE001
            print(f"\n{C['red']}model error: {e}{C['x']}")
            set_status(run, me, "idle")
            report_as(run, me, f"{me} failed on a task: model error {e}")
            return
        print(f"\n\n{C['green']}done{C['x']} {C['dim']}first token {wait:.0f}s · {rate:.1f} tok/s{C['x']}")
        history.extend([{"role": "user", "content": task}, {"role": "assistant", "content": text}])
        out = run / "out" / f"{me}.md"
        with open(out, "a") as f:
            f.write(f"\n## Task from {sender} ({stamp()})\n> {task}\n\n{text.strip()}\n")
        set_status(run, me, "idle")
        words = len(text.split())
        summary = re.sub(r"\s+", " ", re.sub(r"\|?\s*:?-{3,}:?\s*|\||[#*`>]", " ", text))[:240]
        label = f"{me} pass B (reversed-order check)" if task.startswith("PASS B") else me
        report_as(run, me, f"{label} done ({words} words) in out/{me}.md. Starts: {summary}")

    kick = os.environ.get("SWARM_KICKOFF", "").strip()
    set_status(run, me, "idle")
    if kick:
        do("sophon", kick)
    while True:
        try:
            line = input()
        except EOFError:
            time.sleep(1)
            continue
        except KeyboardInterrupt:
            continue
        if not line.strip():
            continue
        sender, task = parse_msg(line)
        if task.lower().startswith(("resume", "settings changed")):
            print(f"{C['dim']}{sender}: {task}{C['x']}")
            continue
        do(sender, task)
