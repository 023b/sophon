"""Shell helpers and run state (locked state.json, board log, status files)."""
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import CURRENT, LEAD


def sh(*args, check=False, capture=True):
    r = subprocess.run(args, text=True, capture_output=capture)
    if check and r.returncode:
        sys.exit(f"swarm: {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout if capture else ""


def tmux_out(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def run_dir():
    d = os.environ.get("SWARM_RUN")
    if d:
        return Path(d)
    if CURRENT.exists():
        return CURRENT.resolve()
    sys.exit("swarm: no active run (swarm start first)")


def state_path(run):
    return run / "state.json"


def load(run):
    p = state_path(run)
    return json.loads(p.read_text()) if p.exists() else {"agents": {}}


def save(run, st):
    """Atomic write (unique temp file per process). For read-modify-write use `with state(run) as st:`."""
    tmp = state_path(run).with_name(f".state.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(state_path(run))


@contextlib.contextmanager
def state(run):
    """Locked read-modify-write of state.json: every process (CLI, panel, workers) goes through this lock,
    so concurrent updates (message counters, alive flags, slots) are not lost."""
    with open(run / ".state.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = load(run)
            yield st
            save(run, st)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def stamp():
    return time.strftime("%H:%M:%S")


def log(run, line):
    with open(run / "board.log", "a") as f:
        f.write(line.rstrip() + "\n")


def lead_of(st):
    return LEAD if LEAD in st["agents"] else "manager"


def keep_awake(run):
    """While sleep mode is on and the run is live, stop the laptop suspending (idle, sleep or lid close).
    Uses a logind block inhibitor that ends by itself when sleep mode goes off, STOP is set or Sophon exits."""
    if subprocess.run(["pgrep", "-f", f"swarm-keepawake {run}"], capture_output=True).returncode == 0:
        return
    check = (f'while [ ! -f {shlex.quote(str(run))}/STOP ] && tmux has-session -t =sw-{LEAD} 2>/dev/null && '
             f'python3 -c "import json,sys;sys.exit(0 if json.load(open(\'{state_path(run)}\')).get(\'sleep\') '
             f'else 1)"; do sleep 20; done')
    subprocess.Popen(["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", "--who=swarm",
                      "--why=Sophon is running while the user sleeps", "--mode=block",
                      "bash", "-c", f"exec -a 'swarm-keepawake {run}' bash -c {shlex.quote(check)}"],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def set_status(run, name, text):
    (run / "status").mkdir(exist_ok=True)
    (run / "status" / name).write_text(text)


def latest(text, limit):
    """The newest task section of an out/ file (workers append one section per task), capped at limit chars."""
    parts = text.split("\n## Task from ")
    last = parts[-1] if len(parts) > 1 else text
    return last[-limit:] if len(last) > limit else last


def parse_msg(line):
    # search, not match: stray terminal bytes before the marker made a real message look like junk
    m = re.search(r"\[swarm msg from ([\w-]+)\]\s*(.*)", line)
    return (m[1], m[2]) if m else ("human", line.strip())
