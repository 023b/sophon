"""swarm init / doctor / paths / personas: first-run setup and health checks."""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from .settings import (
    CACHE_DIR,
    CFG,
    CONFIG_DIR,
    CONFIG_FILE,
    DATA_DIR,
    DEFAULT_DATA,
    PACKAGE,
    RUNS_DIR,
    STATE_DIR,
    ensure_dirs,
)

EXAMPLES = PACKAGE / "data" / "examples"
G, R, A, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def paths(_rest=()):
    rows = [("config", CONFIG_DIR, "config.toml, openrouter.key (0600), openrouter.json"),
            ("config file", CONFIG_FILE, "exists" if CONFIG_FILE.exists() else "not created yet (swarm init)"),
            ("data", DATA_DIR, "runs/, current -> latest run, profile.md, personas/ and facts/ overrides"),
            ("runs", RUNS_DIR, "one folder per run: BRIEF, board.log, out/, FINAL.md, state.json"),
            ("state", STATE_DIR, "OpenRouter spend ledger and model health, big-model lock"),
            ("cache", CACHE_DIR, "OpenRouter model catalogue"),
            ("packaged defaults", DEFAULT_DATA, "personas, facts, templates shipped with Sophon (read-only)")]
    for name, p, what in rows:
        print(f"{name:18} {p}\n{'':18} {D}{what}{X}")
    if os.environ.get("SOPHON_HOME"):
        print(f"\n{A}SOPHON_HOME is set: everything lives under {os.environ['SOPHON_HOME']}{X}")


def init(rest=()):
    """Create the folders, a commented config.toml and a profile.md to fill in. Never overwrites."""
    ensure_dirs()
    for d in ("personas", "facts"):
        (DATA_DIR / d).mkdir(exist_ok=True)
    made = []
    for src, dst in ((EXAMPLES / "config.toml", CONFIG_FILE), (EXAMPLES / "profile.md", DATA_DIR / "profile.md"),
                     (EXAMPLES / "machines.md", DATA_DIR / "facts" / "machines.md")):
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
            made.append(dst)
    for p in made:
        print(f"{G}created{X} {p}")
    if not made:
        print("nothing to create: config and profile already exist")
    print(f"\nNext: edit {CONFIG_FILE} (where your models live), fill in {DATA_DIR / 'profile.md'} "
          f"(optional, makes the judges judge like you), then run `swarm doctor`.")


def _ok(label, good, detail="", fix=""):
    mark = f"{G}ok  {X}" if good else (f"{A}warn{X}" if good is None else f"{R}FAIL{X}")
    print(f"  {mark} {label:28} {D}{detail}{X}")
    if not good and fix:
        print(f"       {'':28} fix: {fix}")
    return bool(good) or good is None


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def doctor(_rest=()):
    from .backends import ensure_tunnel
    from .config import (
        BIG_HOST,
        BIG_LLM,
        BIG_MODELS_DIR,
        BIG_SERVER,
        CLAUDE,
        LOCAL_MODELS,
        OLLAMA_URL,
        TUNNELS,
    )
    ok = True
    print("Sophon doctor\n\nbasics")
    ok &= _ok("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    ok &= _ok("config file", True if CONFIG_FILE.exists() else None, str(CONFIG_FILE), "swarm init")
    for tool, why, fix in (("tmux", "one session per agent", "sudo apt install tmux"),
                           ("kitty", "one window per agent", "sudo apt install kitty"),
                           ("wmctrl", "window tiling (X11)", "sudo apt install wmctrl"),
                           ("xrandr", "screen layout (X11)", "sudo apt install x11-xserver-utils")):
        ok &= _ok(tool, bool(shutil.which(tool)), why, fix)
    _ok("X11 session", True if os.environ.get("DISPLAY") else None,
        os.environ.get("XDG_SESSION_TYPE", "?"), "windows are placed with wmctrl, which needs X11 or XWayland")
    _ok("claude (Claude Code)", True if Path(CLAUDE).exists() or shutil.which("claude") else None, CLAUDE,
        "needed for claude/light/heavy modes and the editor pass: https://docs.claude.com/en/docs/claude-code")
    try:
        import textual  # noqa: F401
        _ok("textual (panel)", True, "installed")
    except ImportError:
        _ok("textual (panel)", None, "missing", "pip install 'sophon-swarm[panel]'")

    print("\nlocal models (Ollama)")
    if TUNNELS:
        _ok("ssh tunnel", ensure_tunnel(), ", ".join(TUNNELS), "check ollama.ssh_host and that you can ssh to it")
    try:
        have = {m["name"] for m in _get(f"{OLLAMA_URL}/api/tags")["models"]}
        _ok("ollama reachable", True, OLLAMA_URL)
        for m in LOCAL_MODELS + [CFG["models"]["embed"]]:
            hit = m in have or f"{m}:latest" in have
            ok &= _ok(f"model {m}", hit, "pulled" if hit else "not pulled", f"ollama pull {m}")
    except Exception as e:  # noqa: BLE001
        ok &= _ok("ollama reachable", False, f"{OLLAMA_URL}: {e}", "install Ollama (https://ollama.com) or set ollama.url")

    print("\nbig models (llama.cpp, phantom mode)")
    if not BIG_LLM:
        _ok("big models", None, "disabled (big.enabled = false)", "see docs/big-models-low-ram.md")
    else:
        def host(cmd):
            argv = ["sh", "-c", cmd] if not BIG_HOST else ["ssh", "-o", "BatchMode=yes", BIG_HOST, cmd]
            return subprocess.run(argv, capture_output=True, text=True, timeout=20)
        exe = BIG_SERVER.replace("~", "$HOME", 1)
        mdir = BIG_MODELS_DIR.replace("~", "$HOME", 1)
        ok &= _ok("llama-server", host(f'test -x "{exe}"').returncode == 0, BIG_SERVER,
                  "scripts/setup-big-model.sh builds it")
        ok &= _ok("systemd --user", host("systemctl --user is-system-running >/dev/null 2>&1 || "
                                         "systemctl --user list-units >/dev/null").returncode == 0,
                  "needed for the RAM cap (MemoryMax)", "a systemd user session (loginctl enable-linger $USER)")
        for name, spec in BIG_LLM.items():
            ok &= _ok(f"{name}", host(f'test -f "{mdir}/{spec["file"]}"').returncode == 0, spec["file"],
                      "scripts/setup-big-model.sh downloads it")
        free = host("free -g | awk '/Mem:/{print $2}'").stdout.strip()
        _ok("host RAM", True, f"{free or '?'} GB total, cap {CFG['big']['ram_cap']}")

    print("\nOpenRouter (optional)")
    from . import openrouter as OR
    _ok("mode", True, OR.settings().get("mode", "off"))
    _ok("key", True if OR.key() else None, OR.masked(), "swarm or key set")
    print(f"\n{G + 'all required checks passed' if ok else R + 'some checks failed'}{X}")
    return 0 if ok else 1


def personas(_rest=()):
    mine = DATA_DIR / "personas"
    names = sorted({p.stem for d in (DEFAULT_DATA / "personas", mine) if d.exists() for p in d.glob("*.md")})
    for n in names:
        if n.startswith("_"):
            continue
        p = mine / f"{n}.md" if (mine / f"{n}.md").exists() else DEFAULT_DATA / "personas" / f"{n}.md"
        first = p.read_text().splitlines()[0].lstrip("# ").replace("Role: ", "")
        print(f"{n:20} {first}{'  (yours)' if p.parent == mine else ''}")
    print(f"\n{D}Add or override: put NAME.md in {mine}{X}")
