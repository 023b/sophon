"""swarm: Sophon, a lead agent and its team of Claude Code sessions, local models and optional OpenRouter models.

Every agent runs in its own tmux session shown in its own kitty window. Agents talk with `swarm say`, which types the
message into the target's terminal and logs it to board.log (shown live in its own window).

setup:
  swarm init                             create the config, data and state folders, config.toml and profile.md
  swarm doctor                           check every dependency and model backend, with fixes
  swarm paths                            where Sophon keeps config, runs, logs and caches
  swarm personas                         list the role prompts (yours override the packaged ones)

  swarm start BRIEF.md [--name NAME] [--manager PERSONA] [--context FILE]... [--add-dir DIR]...
                                         new run: board window + manager on laptop screen
                                         (--manager manager-brainstorm = idea swarm template;
                                          --add-dir lets every agent edit a real project dir;
                                          --allow "Bash(pytest:*)" grants extra tools to all agents)
  swarm spawn NAME [PERSONA|"text"] [--model sonnet|opus]
                                         new worker in its own terminal (manager uses this)
  start also takes --worker-model M and --manager-model M (defaults for spawned agents);
  --context DIR copies a whole folder (read-only snapshot) into context/
  swarm say TO|a,b,c|all MESSAGE...      message agents (sender = $SWARM_ID or human)
  swarm peek NAME [LINES]                read another agent's screen
  swarm list                             agents, state, message counts
  swarm tile                             re-tile windows
  swarm show                             reopen any closed windows
  swarm end                              STOP flag: no more messages, agents wrap up
  swarm kill [NAME|all]                  close sessions and windows

the lead (local-first lead with sole memory access):
  swarm go                               new run: the lead intake on the laptop screen, board + panel on the big one
  swarm report MESSAGE...                worker -> lead (batched into a digest in overnight mode)
  swarm memo-request "TITLE" CONTENT...  ask the lead to store something in memory (lead approves)
  swarm memo-done ID approved|rejected [NOTE]   lead closes a memo request
  swarm mode [claude|light|heavy|local] [--agents N] [--thorough 1-5]   show or change the run mode
  swarm panel                            control panel (opened by go)
  swarm tunnel                           make sure the SSH tunnel to the remote Ollama is up
  swarm or [status|mode off|free|paid|key set|key delete|cap run|day USD|models [free|paid]|test [MODEL]|spent]
                                         OpenRouter: key, mode, caps, health-checked model list
  swarm spawn NAME PERSONA --engine openrouter --model auto|paid|cheap|or:<id>   an OpenRouter worker
"""
import os
import sys
from pathlib import Path

from .agents import (
    accept_trust,
    end,
    go,
    kill,
    list_agents,
    memo_done,
    memo_request,
    report,
    say,
    set_mode,
    show,
    spawn,
    start,
)
from .backends import ensure_tunnel
from .config import ROOT
from .hooks import hook
from .intake import ask_cmd, intake
from .pipeline import local_lead
from .term import screen_text, tile
from .util import keep_awake, load, log, run_dir, stamp, state
from .worker import local_worker


def panel():
    """The Textual control panel: this interpreter if it has Textual, else the checkout's .venv."""
    import importlib.util
    py = sys.executable
    if importlib.util.find_spec("textual") is None:
        venv_py = ROOT / ".venv" / "bin" / "python"
        if not venv_py.exists():
            sys.exit("swarm: the panel needs Textual: pip install 'sophon-swarm[panel]' (or pip install textual)")
        py = str(venv_py)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    os.execve(py, [py, "-m", "sophon.panel"], env)


def or_cmd(rest):
    """swarm or [status | mode off|free|paid | policy deny|allow|zdr | key [set [KEY]] | key delete | cap run|day USD | models [free|paid]
    | test [MODEL] | spent]"""
    import getpass

    from . import openrouter as OR
    a = rest[0] if rest else "status"
    try:
        if a == "status":
            print(OR.status_line())
            if OR.key():
                i = OR.key_info()
                bal = i.get("balance")
                print(f"account: used ${i['usage']:.3f}" + (f", limit ${i['limit']}" if i["limit"] is not None else "")
                      + (f", balance ${bal:.2f}" if bal is not None else "") + (" (free tier)" if i["free_tier"] else ""))
        elif a == "mode":
            m = rest[1] if len(rest) > 1 else ""
            if m not in ("off", "free", "paid"):
                sys.exit("usage: swarm or mode off|free|paid")
            if m != "off" and not OR.key():
                sys.exit("save a key first: swarm or key set")
            OR.save_settings(mode=m)
            print(OR.status_line())
        elif a == "policy":
            v = rest[1] if len(rest) > 1 else ""
            if v not in ("deny", "allow", "zdr"):
                sys.exit("usage: swarm or policy deny|allow|zdr   (deny: no providers that store or train on prompts; "
                         "zdr: zero data retention only; allow: any provider, more free models)")
            OR.save_settings(data_collection="allow" if v == "allow" else "deny", zdr=v == "zdr")
            print(OR.status_line())
        elif a == "key":
            sub = rest[1] if len(rest) > 1 else "show"
            if sub == "show":
                print("key:", OR.masked())
            elif sub == "set":
                k = rest[2] if len(rest) > 2 else getpass.getpass("OpenRouter key (hidden): ")
                i = OR.set_key(k)
                print(f"saved {OR.masked(k)} (checked with OpenRouter: used ${i['usage']:.3f}"
                      f"{', free tier' if i['free_tier'] else ''}). Mode: {OR.settings()['mode']}")
            elif sub in ("delete", "rm", "remove"):
                OR.delete_key()
                print("key deleted, OpenRouter off")
        elif a == "cap":
            which, usd = rest[1], float(rest[2])
            OR.save_settings(**{("run_cap_usd" if which == "run" else "day_cap_usd"): usd})
            print(OR.status_line())
        elif a == "models":
            tier = rest[1] if len(rest) > 1 else "free"
            for m in OR.candidates(tier)[:25]:
                h = OR._health().get(m["id"])
                state = "?" if not h else ("ok" if h["ok"] else "bad: " + h["why"][:40])
                price = "free" if m["free"] else f"${m['in']:.2f}/${m['out']:.2f} per Mtok"
                print(f"{m['id']:<55} {m['ctx'] // 1000:>5}k  {price:<26} {state}")
        elif a == "test":
            if len(rest) > 1:
                print(rest[1], OR.probe(rest[1]))
            else:
                for mid in OR.pick("free", n=3):
                    print("healthy:", mid)
        elif a == "spent":
            print(f"today ${OR.spent(since=OR.today()):.4f}, all time ${OR.spent():.4f}")
        else:
            print(or_cmd.__doc__)
    except OR.ORError as e:
        sys.exit(f"openrouter: {e}")


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = a[0], a[1:]
    if cmd == "start":
        opts = {"--name": [], "--manager": [], "--context": [], "--add-dir": [], "--allow": [],
                "--worker-model": [], "--manager-model": []}
        pos = []
        while rest:
            tok = rest.pop(0)
            if tok in opts and rest:
                opts[tok].append(rest.pop(0))
            else:
                pos.append(tok)
        if not pos:
            sys.exit("usage: swarm start BRIEF.md [--name N] [--manager P] [--context F]... [--add-dir D]... [--allow TOOL]...")
        return start(Path(pos[0]), (opts["--name"] or ["swarm"])[-1], (opts["--manager"] or ["manager"])[-1],
                     opts["--context"], opts["--add-dir"], opts["--allow"],
                     (opts["--worker-model"] or [None])[-1], (opts["--manager-model"] or [None])[-1])
    if cmd == "keep-awake":
        return keep_awake(Path(os.environ["SWARM_RUN"]))
    if cmd == "trust-watch":
        return accept_trust(rest[0], wait=40)
    if cmd == "go":
        def opt(flag):
            return rest[rest.index(flag) + 1] if flag in rest and rest.index(flag) + 1 < len(rest) else None
        job = opt("--job")
        if opt("--job-file"):
            job = Path(opt("--job-file")).expanduser().read_text()
        return go(opt("--mode"), job=job, sleep="--sleep" in rest,
                  agents=int(opt("--agents")) if opt("--agents") else None,
                  thorough=int(opt("--thorough")) if opt("--thorough") else None,
                  test="--test" in rest, lead_model=opt("--lead-model"))
    if cmd == "hook":
        return hook(rest[0] if rest else "")
    if cmd == "editor":
        v = (rest[0] if rest else "").lower()
        run = run_dir()
        if v not in ("on", "off"):
            print(f"editor: {load(run).get('editor', 'sonnet')}  (swarm editor on|off: one Sonnet pass over the local "
                  f"FINAL, about 20K tokens)")
            return
        with state(run) as st:
            st["editor"] = "sonnet" if v == "on" else "off"
        log(run, f"[{stamp()}] human: Sonnet editor {v.upper()}")
        print(f"editor {v}")
        return
    if cmd == "private":
        v = (rest[0] if rest else "").lower()
        run = run_dir()
        if v not in ("on", "off"):
            print(f"private: {'on' if load(run).get('private') else 'off'}  (swarm private on|off: on blocks OpenRouter)")
            return
        with state(run) as st:
            st["private"] = v == "on"
        log(run, f"[{stamp()}] human: private run {v.upper()} (OpenRouter {'blocked' if v == 'on' else 'allowed'})")
        print(f"private {v}")
        return
    if cmd == "ask":
        qtype = None
        if "--type" in rest:
            i = rest.index("--type")
            qtype = rest[i + 1] if i + 1 < len(rest) else None
            del rest[i:i + 2]
        return ask_cmd(run_dir(), " ".join(rest), qtype)
    if cmd in ("init", "doctor", "paths", "personas"):
        from . import setup
        return getattr(setup, cmd)(rest)
    if cmd in ("or", "openrouter"):
        return or_cmd(rest)
    if cmd == "tunnel":
        print("tunnel up" if ensure_tunnel() else "tunnel DOWN")
        return
    run = run_dir()
    if cmd == "intake":
        return intake()
    if cmd == "lw":
        return local_worker()
    if cmd == "lead-loop":
        return local_lead()
    if cmd == "panel":
        return panel()
    if cmd == "report":
        return report(run, rest)
    if cmd == "memo-request":
        return memo_request(run, rest)
    if cmd == "memo-done":
        if len(rest) < 2:
            sys.exit("usage: swarm memo-done ID approved|rejected [NOTE]")
        return memo_done(run, rest[0], rest[1], " ".join(rest[2:]))
    if cmd == "mode":
        return set_mode(run, rest)
    if cmd == "spawn":
        if len(rest) < 1:
            sys.exit("usage: swarm spawn NAME [PERSONA|\"role text\"]")
        flags = {}
        for f in ("--model", "--engine"):
            if f in rest:
                i = rest.index(f)
                if i + 1 >= len(rest):
                    sys.exit(f"swarm: {f} needs a value")
                flags[f] = rest[i + 1]
                del rest[i:i + 2]
        return spawn(run, rest[0], " ".join(rest[1:]) or None, model=flags.get("--model"), engine=flags.get("--engine"))
    if cmd == "say":
        if len(rest) < 2:
            sys.exit("usage: swarm say TO|all MESSAGE")
        return say(run, rest[0], rest[1:])
    if cmd == "peek":
        print(screen_text(rest[0], int(rest[1]) if len(rest) > 1 else 60))
        return
    if cmd == "list":
        return list_agents(run)
    if cmd == "tile":
        return tile(run)
    if cmd == "show":
        return show(run)
    if cmd == "end":
        return end(run)
    if cmd == "kill":
        return kill(run, rest[0] if rest else "all")
    sys.exit(f"swarm: unknown command {cmd}")
