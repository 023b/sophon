"""Where Sophon keeps things, and the user's configuration.

File locations follow the XDG base directory spec, so nothing is written inside the source tree:

  config   $XDG_CONFIG_HOME/sophon   (~/.config/sophon)       config.toml, openrouter.key (0600), openrouter.json
  data     $XDG_DATA_HOME/sophon     (~/.local/share/sophon)  runs/, current -> latest run, profile.md,
                                                               personas/ and facts/ overrides
  state    $XDG_STATE_HOME/sophon    (~/.local/state/sophon)  OpenRouter ledger and model health, big-model lock
  cache    $XDG_CACHE_HOME/sophon    (~/.cache/sophon)        OpenRouter model catalogue

SOPHON_HOME=/some/dir puts all four under one folder as config/, data/, state/, cache/ (portable installs, tests).
Configuration: config.toml in the config folder, or the file named by SOPHON_CONFIG. Every key has a default, so
Sophon runs without a config file; see sophon.example.toml.
"""
import copy
import os
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
DEFAULT_DATA = PACKAGE / "data"


def _xdg(var, fallback, short):
    home = os.environ.get("SOPHON_HOME")
    if home:
        return Path(home).expanduser() / short
    return Path(os.environ.get(var) or Path.home() / fallback).expanduser() / "sophon"


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config", "config")
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share", "data")
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state", "state")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache", "cache")
RUNS_DIR = DATA_DIR / "runs"
CURRENT_NAME = "current"
CONFIG_FILE = Path(os.environ.get("SOPHON_CONFIG") or CONFIG_DIR / "config.toml").expanduser()

DEFAULTS = {
    "user": {
        "name": "the user",          # how agents refer to you in prompts
        "style": "British English, no emojis, no em dashes",
    },
    "ollama": {
        # Where the local worker models live. Use a direct URL, or set ssh_host and Sophon opens SSH tunnels.
        "url": "http://127.0.0.1:11434",
        "cpu_url": "",                # optional second Ollama (e.g. a CPU-only instance) for cpu_models
        "cpu_models": [],
        "ssh_host": "",               # e.g. "homeserver" (an ~/.ssh/config alias); empty = no tunnel
        "remote": "127.0.0.1:11434",  # Ollama's address as seen from ssh_host
        "remote_cpu": "",
        "tunnel_port": 11435,         # local end of the tunnel
        "tunnel_cpu_port": 11501,
        "host_label": "MODELS",       # name of the model host in the panel
    },
    "models": {
        "triage": "gemma3:4b",        # intake triage and questioners (fast, JSON)
        "worker": "gemma3:4b",        # default local worker
        "strong": "gemma3:27b",       # local lead, critics, final writer
        "available": ["gemma3:4b", "gemma3:12b", "gemma3:27b", "qwen3:8b", "qwen3:14b"],
        "roster": [],                 # idea writers for long runs; empty = [strong, worker]
        "critics": [],                # critic models; empty = [strong]
        "embed": "nomic-embed-text",  # embeddings for de-duplication
        "sizes_gb": {"gemma3:27b": 17, "qwen3:14b": 9.5},  # RAM when loaded; used to free memory first
    },
    "big": {
        # MoE models larger than RAM, served by llama.cpp's llama-server with the weights memory-mapped and the
        # process capped by systemd (MemoryMax). Pages beyond the cap are dropped and re-read from the SSD, so a
        # 120B MoE runs on a 16 GB machine, slowly. See docs/big-models-low-ram.md.
        "enabled": False,
        "host": "",                   # "" = this machine; else an SSH alias that has llama.cpp and the models
        "llama_server": "~/llama.cpp/build/bin/llama-server",
        "models_dir": "~/models",
        "ram_cap": "12G",             # systemd MemoryMax for the server: weights + KV cache + page cache
        "threads": 0,                 # 0 = llama.cpp default
        "ctx": 16384,
        "port": 11600,
        "extra_args": ["--no-repack", "--jinja"],
        "models": {
            "big-gptoss": {"file": "gpt-oss-120b-MXFP4.gguf", "label": "gpt-oss 120B",
                           "effort": "medium", "role": "both",
                           "repo": "ggml-org/gpt-oss-120b-GGUF"},
        },
    },
    "claude": {
        "budget": 2_000_000,          # fresh tokens per run before the lead is told to wrap up
        "editor": True,               # Sonnet editor pass after the local pipeline
    },
    "memory": {
        # Optional long-term memory through an MCP server the lead can use (read for everyone, write for the lead).
        "mcp_prefix": "",             # e.g. "mcp__notes__"; empty = no memory tools
        "read_tools": ["search_notes", "get_note", "list_notes"],
        "write_tools": ["create_note", "update_note"],
    },
    "openrouter": {"referer": ""},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path=None):
    p = Path(path or CONFIG_FILE)
    if p.exists():
        with p.open("rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    return copy.deepcopy(DEFAULTS)


CFG = load_config()


def data_file(kind, name):
    """A personas/facts/templates file: the user's copy in DATA_DIR wins over the packaged default."""
    mine = DATA_DIR / kind / name
    return mine if mine.exists() else DEFAULT_DATA / kind / name


def data_dir(kind):
    return DEFAULT_DATA / kind


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, CACHE_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
