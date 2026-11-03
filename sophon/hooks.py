"""Claude Code hooks: agents report prompts, turns and state themselves."""
import json
import os
import re
import shlex
import sys
from pathlib import Path

from .config import CLAUDE_BUDGET, SWARM
from .term import dialog_flag
from .util import load, log, stamp


def write_hook_settings(run):
    me = shlex.quote(SWARM)
    cmd = lambda ev: {"type": "command", "command": f"{me} hook {ev}", "timeout": 10}  # noqa: E731
    cfg = {"hooks": {
        "Notification": [{"hooks": [cmd("notify")]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [cmd("pretool")]}],
        "PostToolUse": [{"hooks": [cmd("posttool")]}],
        "UserPromptSubmit": [{"hooks": [cmd("prompt")]}],
        "Stop": [{"hooks": [cmd("stop")]}],
        "SessionStart": [{"hooks": [cmd("session")]}],
    }}
    p = run / "hooks.json"
    p.write_text(json.dumps(cfg, indent=1))
    return p


BASH_WRITE = re.compile(r"<<-?\s*['\"]?\w+|(^|[;&|]\s*)(cat|echo|printf|tee)\b[^|;&]*(>|\btee\b)|\btee\s+-?a?\s*\S", re.M)


def hook(event):
    """Entry point for Claude Code hooks (stdin: the hook's JSON). Never breaks the agent: errors exit 0."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    name, rd = os.environ.get("SWARM_ID"), os.environ.get("SWARM_RUN")
    if not name or not rd:
        return
    run = Path(rd)
    (run / "status").mkdir(exist_ok=True)
    flag = dialog_flag(run, name)
    try:
        if event == "notify":
            msg = str(data.get("message", ""))
            kind = str(data.get("notification_type", ""))
            if "permission" in kind or "permission" in msg.lower():
                flag.write_text(msg)
                log(run, f"[{stamp()}] {name} waits on a permission prompt: {msg[:160]}")
        elif event == "pretool":
            c = str((data.get("tool_input") or {}).get("command", ""))
            if BASH_WRITE.search(c):
                print("Blocked by the swarm: write files with the Write or Edit tool, never Bash heredocs, cat >, "
                      "echo > or tee (those stall on a permission prompt while the user is away).", file=sys.stderr)
                sys.exit(2)
        elif event in ("posttool", "prompt"):
            flag.unlink(missing_ok=True)
            if event == "prompt":
                (run / "status" / name).write_text("working")
        elif event == "stop":
            flag.unlink(missing_ok=True)
            (run / "status" / name).write_text("idle")
        elif event == "session":
            st = load(run)
            from . import openrouter as OR
            used = st.get("_tokens", 0)
            print(f"Swarm state at session start: mode {st.get('mode', '?')}, the user "
                  f"{'ASLEEP (never wait for them)' if st.get('sleep') else 'awake'}, Claude budget used "
                  f"{used:,} of {CLAUDE_BUDGET:,} tokens, OpenRouter {OR.settings()['mode']}"
                  f"{', BLOCKED (private run)' if st.get('private') else ''}.")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
