"""Constants derived from settings: paths, modes, models, tool allowlists, colours, worker rules."""
import os
import shutil
import sys
from pathlib import Path

from .settings import (
    CFG,
    CURRENT_NAME,
    DATA_DIR,
    PACKAGE,
    RUNS_DIR,
    STATE_DIR,
    data_dir,
)

HOME = Path.home()
ROOT = PACKAGE.parent                      # the checkout (holds the `swarm` launcher)
_VENV_SWARM = Path(sys.executable).parent / "swarm"   # pip install into a venv that is not activated
SWARM = str(ROOT / "swarm") if (ROOT / "swarm").exists() else (
    str(_VENV_SWARM) if _VENV_SWARM.exists() else (shutil.which("swarm") or "swarm"))
SWARM_BIN = str(Path(SWARM).parent) if os.sep in SWARM else ""  # put on the agents' PATH
PERSONAS = data_dir("personas")
FACTS = data_dir("facts")
TEMPLATES = data_dir("templates")
RUNS = RUNS_DIR
CURRENT = DATA_DIR / CURRENT_NAME
PROFILE = DATA_DIR / "profile.md"
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
USER = CFG["user"]["name"]
STYLE = CFG["user"]["style"]

MAX_MSGS = int(os.environ.get("SWARM_MAX_MSGS", "60"))  # per agent, loop guard
FONT_BIG = os.environ.get("SWARM_FONT", "13")
FONT_LAPTOP = os.environ.get("SWARM_FONT_LAPTOP", "13")
BOARD = "board"  # pseudo-agent: the live message log window
PANEL = "panel"  # pseudo-agent: the control panel window
LEAD = "sophon"  # the lead: watches every agent, the only one with write access to long-term memory
LEADS = ("manager", LEAD)

# ---------- local models (Ollama) ----------
_O = CFG["ollama"]
TUNNELS = {}  # {ssh host: {local port: "addr:port" as seen from that host}}, opened by `swarm tunnel`
if _O["ssh_host"]:  # Ollama on another machine: reach it through an SSH tunnel
    OLLAMA_URL = f"http://127.0.0.1:{_O['tunnel_port']}"
    OLLAMA_CPU_URL = f"http://127.0.0.1:{_O['tunnel_cpu_port']}" if _O["remote_cpu"] else ""
    TUNNELS[_O["ssh_host"]] = {_O["tunnel_port"]: _O["remote"]}
    if _O["remote_cpu"]:
        TUNNELS[_O["ssh_host"]][_O["tunnel_cpu_port"]] = _O["remote_cpu"]
else:
    OLLAMA_URL, OLLAMA_CPU_URL = _O["url"].rstrip("/"), _O["cpu_url"].rstrip("/")
SSH_HOST = _O["ssh_host"]
HOST_LABEL = _O["host_label"]

_M = CFG["models"]
LOCAL_MODELS = list(dict.fromkeys(_M["available"] + [_M["triage"], _M["worker"], _M["strong"]]))
TRIAGE_MODEL = _M["triage"]
WORKER_MODEL = _M["worker"]
STRONG_MODEL = _M["strong"]
EMBED_MODEL = _M["embed"]
ROSTER = _M["roster"] or [STRONG_MODEL, WORKER_MODEL]
CRITICS = _M["critics"] or [STRONG_MODEL]
BIG = dict(_M["sizes_gb"])  # approx RAM GB when loaded, used to make room before loading


def ollama_url(model):
    """The Ollama that serves this model (the CPU instance for cpu_models, else the main one)."""
    return OLLAMA_CPU_URL if OLLAMA_CPU_URL and model in _M["cpu_models"] else OLLAMA_URL


def local_best():
    return os.environ.get("SWARM_LOCAL_BEST", STRONG_MODEL)


# ---------- big MoE models streamed from disk ----------
_B = CFG["big"]
BIG_ENABLED = bool(_B["enabled"])
BIG_LLM = {k: {"label": k, "effort": "", "role": "both", **v} for k, v in _B["models"].items()} if BIG_ENABLED else {}
BIG_PORT = int(_B["port"])
BIG_CAP = os.environ.get("SWARM_BIG_CAP", _B["ram_cap"])
BIG_HOST = _B["host"]
BIG_SERVER = _B["llama_server"]
BIG_MODELS_DIR = _B["models_dir"]
BIG_THREADS = int(_B["threads"])
BIG_CTX = int(_B["ctx"])
BIG_ARGS = list(_B["extra_args"])
BIG_LOCK = STATE_DIR / "big.lock"
BIG_URL = f"http://127.0.0.1:{BIG_PORT}"
if BIG_HOST and BIG_ENABLED:
    TUNNELS.setdefault(BIG_HOST, {})[BIG_PORT] = f"127.0.0.1:{BIG_PORT}"

MODES = {
    "claude": {"label": "fully Claude", "lead": ("claude", "opus"), "worker": ("claude", "sonnet"),
               "cap_claude": 3, "cap_local": 0, "digest": 0},
    "light": {"label": "Claude + light local", "lead": ("claude", "opus"),
              "worker": ("local", STRONG_MODEL), "cap_claude": 1, "cap_local": 4, "digest": 0},
    "heavy": {"label": "Claude + heavy local (overnight)", "lead": ("claude", "sonnet"),
              "worker": ("local", STRONG_MODEL), "cap_claude": 0, "cap_local": 12, "digest": 1800},
    "local": {"label": "fully local", "lead": ("local-lead", os.environ.get("SWARM_LOCAL_LEAD", STRONG_MODEL)),
              "worker": ("local", WORKER_MODEL), "cap_claude": 0, "cap_local": 12, "digest": 0},
}
if BIG_LLM:
    MODES["phantom"] = {"label": "fully local, big models swapped (overnight)", "lead": ("local-lead", STRONG_MODEL),
                        "worker": ("local", next(iter(BIG_LLM))), "cap_claude": 0, "cap_local": 12, "digest": 0}
NO_CLAUDE_MODES = ("local", "phantom")
CLAUDE_BUDGET = int(os.environ.get("SWARM_CLAUDE_BUDGET", CFG["claude"]["budget"]))
EDITOR_DEFAULT = bool(CFG["claude"]["editor"])

# ---------- Claude Code tool allowlists ----------
_MEM = CFG["memory"]
MEMORY_READ = [_MEM["mcp_prefix"] + t for t in _MEM["read_tools"]] if _MEM["mcp_prefix"] else []
MEMORY_WRITE = [_MEM["mcp_prefix"] + t for t in _MEM["write_tools"]] if _MEM["mcp_prefix"] else []
MEMORY_ALL = _MEM["mcp_prefix"] + "*" if _MEM["mcp_prefix"] else ""
READ_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch",
              "Bash(swarm say:*)", "Bash(swarm peek:*)", "Bash(swarm list)", "Bash(swarm list:*)",
              "Bash(swarm report:*)", "Bash(swarm memo-request:*)", "Bash(swarm ask:*)"]
MANAGER_TOOLS = READ_TOOLS + MEMORY_READ + ["Bash(swarm spawn:*)", "Bash(swarm tile)", "Bash(swarm end)",
                                            "Bash(swarm kill:*)", "Bash(swarm show)"]
LEAD_TOOLS = MANAGER_TOOLS + MEMORY_WRITE + ["Bash(swarm memo-done:*)", "Bash(swarm mode)", "Bash(swarm mode:*)"]

COLOURS = ["colour214", "colour45", "colour141", "colour203", "colour114", "colour220",
           "colour75", "colour176", "colour209", "colour156", "colour39", "colour211"]
CLAUDE_MODELS = ("opus", "sonnet", "haiku")

TRIAGE_SCHEMA = {"type": "object", "properties": {
    "understood": {"type": "boolean"}, "confidence": {"type": "number"},
    "stakes": {"type": "string", "enum": ["low", "high"]},
    "mode": {"type": "string", "enum": list(MODES)}, "agents": {"type": "integer"},
    "thorough": {"type": "integer"}, "questions": {"type": "array", "items": {"type": "string"}},
    "reason": {"type": "string"}},
    "required": ["understood", "confidence", "stakes", "mode", "agents", "thorough", "questions", "reason"]}

MODE_GUIDE = """Modes:
- claude: fully Claude (best quality per minute, uses the most Claude usage; 3 workers max). Short, high-stakes,
  accuracy-critical work.
- light: Claude lead plus 2-4 local workers. Default for normal research or design work.
- heavy: Claude checks in rarely (digest every 30 min), 6-10 local workers, slow but cheap; overnight jobs.
- local: everything on local models, then one short Claude editor pass; drafts, private work, brainstorming.
Thoroughness 1-5: 1 = quick pass, 3 = normal, 5 = exhaustive with verification of every claim.
Stakes are high if the job touches deadlines, coursework, money, other people, or anything irreversible."""

C = {"dim": "\033[2m", "b": "\033[1m", "cyan": "\033[38;2;79;209;255m", "amber": "\033[38;2;255;179;71m",
     "green": "\033[38;2;90;247;142m", "red": "\033[38;2;255;95;95m", "x": "\033[0m"}


def _machines():
    """The user's machine facts (facts/machines.md in the data folder), so workers stop guessing hardware."""
    p = DATA_DIR / "facts" / "machines.md"
    return p.read_text().strip() if p.exists() else ""


LOCAL_RULES = (f"You are one worker in a team of AI agents run by a lead called Sophon. {USER[0].upper() + USER[1:]} "
               f"(the human) may watch your terminal live. You receive one task at a time and answer it directly in "
               f"Markdown.\nRules: answer only the task, concretely, no preamble, no closing remarks. Never invent "
               f"sources, numbers or facts; write \"unverified\" when unsure. {STYLE}. Keep it under 700 words unless "
               f"the task asks for more. Never invent commands, flags or APIs; if unsure, write \"unverified\"."
               + (f"\nThe user's machines (get these right):\n{_machines()}" if _machines() else ""))
