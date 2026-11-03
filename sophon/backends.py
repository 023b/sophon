"""Model backends: Ollama (local or over SSH), big llama.cpp models streamed from disk, OpenRouter, Claude JSON."""
import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import (
    BIG,
    BIG_ARGS,
    BIG_CAP,
    BIG_CTX,
    BIG_HOST,
    BIG_LLM,
    BIG_LOCK,
    BIG_MODELS_DIR,
    BIG_PORT,
    BIG_SERVER,
    BIG_THREADS,
    BIG_URL,
    CLAUDE,
    OLLAMA_CPU_URL,
    OLLAMA_URL,
    TRIAGE_MODEL,
    TRIAGE_SCHEMA,
    TUNNELS,
    ollama_url,
)

UNIT = "sophon-big"


def _up(port):
    with socket.socket() as so:
        so.settimeout(1)
        return so.connect_ex(("127.0.0.1", port)) == 0


def ensure_tunnel():
    """Open the configured SSH tunnels that are not up yet. True when everything needed is reachable."""
    for host, ports in TUNNELS.items():
        fwd = [x for p, dst in ports.items() if not _up(p) for x in ("-L", f"{p}:{dst}")]
        if fwd:
            subprocess.run(["ssh", "-f", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                            *fwd, host], capture_output=True, timeout=30)
    if TUNNELS:
        time.sleep(1)
    return all(_up(p) for ports in TUNNELS.values() for p in ports)


def _host_sh(cmd, timeout=60):
    """Run a shell command on the big-model host: this machine when big.host is empty, else over SSH."""
    argv = ["sh", "-c", cmd] if not BIG_HOST else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                                   BIG_HOST, cmd]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _ollama_urls():
    return [u for u in dict.fromkeys([OLLAMA_URL, OLLAMA_CPU_URL]) if u]


def ollama_json(prompt, model=TRIAGE_MODEL, timeout=240):
    body = json.dumps({"model": model, "stream": False, "think": False, "format": TRIAGE_SCHEMA,
                       "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0.2}}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(f"{ollama_url(model)}/api/chat", body),
                                         timeout=timeout))
    return json.loads(r["message"]["content"])


def claude_json(prompt):
    r = subprocess.run([CLAUDE, "-p", "--model", "sonnet", prompt], capture_output=True, text=True, timeout=300)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError((r.stdout or r.stderr).strip()[:300])
    return json.loads(m.group(0))


_BIG_CACHE = [0.0, None]


def big_current(fresh=False):
    """File name of the model llama-server is serving, or None. Cached 30 s, so a broken tunnel does
    not add a 5 s timeout to every local call."""
    if not fresh and time.time() - _BIG_CACHE[0] < 30:
        return _BIG_CACHE[1]
    try:
        props = json.load(urllib.request.urlopen(f"{BIG_URL}/props", timeout=5))
        name = Path(props.get("model_path", "")).name or None
    except Exception:  # noqa: BLE001
        name = None
    _BIG_CACHE[:] = [time.time(), name]
    return name


def big_stop():
    _host_sh(f"systemctl --user stop {UNIT} 2>/dev/null; systemctl --user reset-failed {UNIT} 2>/dev/null")


def unload_ollama(keep=None):
    """Ask every configured Ollama to unload its models (except `keep`) so their RAM is free."""
    for url in _ollama_urls():
        try:
            for m in json.load(urllib.request.urlopen(f"{url}/api/ps", timeout=5))["models"]:
                if keep and m["name"].split(":")[0] == keep.split(":")[0]:
                    continue
                urllib.request.urlopen(urllib.request.Request(
                    f"{url}/api/generate",
                    json.dumps({"model": m["name"], "keep_alive": 0}).encode()), timeout=30).read()
        except Exception:  # noqa: BLE001
            pass


def big_command(spec):
    """The shell command that starts llama-server for one big model under a systemd memory cap."""
    exe = BIG_SERVER if not BIG_SERVER.startswith("~/") else "$HOME/" + BIG_SERVER[2:]
    mdir = BIG_MODELS_DIR if not BIG_MODELS_DIR.startswith("~/") else "$HOME/" + BIG_MODELS_DIR[2:]
    args = [*BIG_ARGS, *spec.get("args", [])]
    if BIG_THREADS:
        args += ["-t", str(BIG_THREADS)]
    return (f"systemd-run --user --unit={UNIT} --collect -p MemoryMax={shlex.quote(BIG_CAP)} -p MemorySwapMax=0 "
            f"nice -n 5 \"{exe}\" -m \"{mdir}/{spec['file']}\" -c {int(spec.get('ctx', BIG_CTX))} -np 1 "
            f"--host 127.0.0.1 --port {BIG_PORT} " + " ".join(shlex.quote(a) for a in args))


def big_server(model, say=print):
    """Serve `model` with llama-server (swapping out the other big model and Ollama's models first).

    The server runs as a transient systemd user unit with MemoryMax, so the kernel keeps at most `ram_cap` of the
    memory-mapped weights resident and re-reads the rest from the SSD. That is what lets a 120B MoE run on a
    machine with far less RAM than the model file, at a few tokens per second."""
    spec = BIG_LLM[model]
    if big_current(fresh=True) == spec["file"]:
        return
    say(f"swapping in {spec['label']} (RAM cap {BIG_CAP}, experts stream from disk) ...")
    big_stop()
    unload_ollama()
    r = _host_sh(big_command(spec))
    if r.returncode:
        raise RuntimeError(f"could not start {model}: {(r.stderr or r.stdout).strip()[:200]}")
    ensure_tunnel()
    t0, checked = time.time(), 0
    while time.time() - t0 < 600:
        try:
            if urllib.request.urlopen(f"{BIG_URL}/health", timeout=5).status == 200 \
                    and big_current(fresh=True) == spec["file"]:
                say(f"{spec['label']} ready in {int(time.time() - t0)} s")
                return
        except Exception:  # noqa: BLE001
            pass
        if time.time() - checked > 10:  # fail fast if the server died instead of waiting the full ten minutes
            checked = time.time()
            a = _host_sh(f"systemctl --user is-active {UNIT} || journalctl --user -u {UNIT} -n 5 --no-pager -o cat",
                         timeout=30)
            if not a.stdout.startswith("active") and not a.stdout.startswith("activating"):
                raise RuntimeError(f"{model} failed to start: {a.stdout.strip()[-300:]}")
        time.sleep(3)
    raise RuntimeError(f"{model} did not become ready in 10 minutes")


def stream_big(model, messages, out=None, num_predict=1800):
    """Like stream_chat, through llama-server's OpenAI API. A lock serialises all big-model calls on this machine,
    so one agent can never swap the model out from under another."""
    BIG_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = open(BIG_LOCK, "w")
    if out:
        out.write("\033[2m(waiting for the big-model slot ...)\033[0m\r")
        out.flush()
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        big_server(model, say=lambda m: out and (out.write(f"\033[2m{m}\033[0m\n"), out.flush()))
        spec = BIG_LLM[model]
        body = {"messages": messages, "stream": True, "temperature": 0.6, "cache_prompt": True,
                "max_tokens": num_predict + (3000 if spec["effort"] else 0)}
        if spec["effort"]:
            body["chat_template_kwargs"] = {"reasoning_effort": spec["effort"]}
        req = urllib.request.Request(f"http://127.0.0.1:{BIG_PORT}/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        text, t0, first, n, thinking = [], time.time(), None, 0, False
        with urllib.request.urlopen(req, timeout=3600) as r:
            for raw in r:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                ch = json.loads(line[5:]).get("choices") or [{}]
                d = ch[0].get("delta", {})
                think_piece, piece = d.get("reasoning_content") or "", d.get("content") or ""
                if think_piece and out:
                    if not thinking:
                        out.write("\033[2m[thinking] ")
                        thinking = True
                    out.write(think_piece)
                    out.flush()
                if piece:
                    if thinking and out:
                        out.write("\033[0m\n\n")
                        thinking = False
                    first = first or time.time()
                    n += 1
                    text.append(piece)
                    if out:
                        out.write(piece)
                        out.flush()
        gen = time.time() - (first or time.time())
        return "".join(text), (first or time.time()) - t0, (n / gen if gen > 0 else 0)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def stream_chat(model, messages, out=None, think=False, num_predict=1800, options=None):
    """Stream a chat from Ollama (or a big model, or OpenRouter), print live, return
    (text, seconds to first token, tokens per second). Ctrl+C stops generation."""
    if model in BIG_LLM:
        return stream_big(model, messages, out, num_predict)
    if model.startswith("or:"):
        from . import openrouter as OR
        rd = os.environ.get("SWARM_RUN")
        text, wait, rate, used = OR.stream(model, messages, out=out, num_predict=num_predict,
                                           temperature=(options or {}).get("temperature", 0.6),
                                           run=Path(rd).name if rd else None)
        return text, wait, rate
    if big_current():  # a big model holds the RAM: free it before Ollama loads anything
        BIG_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(BIG_LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            big_stop()
    body = json.dumps({"model": model, "messages": messages, "stream": True, "think": think,
                       "options": {"temperature": 0.6, "num_predict": num_predict, **(options or {})}}).encode()
    if model in BIG:
        make_room(model)
    req = urllib.request.Request(f"{ollama_url(model)}/api/chat", body)
    text, t0, first, n = [], time.time(), None, 0
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            if not line.strip():
                continue
            ev = json.loads(line)
            piece = ev.get("message", {}).get("content", "")
            if piece:
                first = first or time.time()
                n += 1
                text.append(piece)
                if out:
                    out.write(piece)
                    out.flush()
            if ev.get("done"):
                break
    gen = time.time() - (first or time.time())
    rate = n / gen if gen > 0 else 0
    return "".join(text), (first or time.time()) - t0, rate


def host_free_gb():
    """Available RAM (GB) on the machine that runs Ollama: local, or ollama.ssh_host over SSH."""
    from .config import SSH_HOST
    argv = ["free", "-m"] if not SSH_HOST else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                                 SSH_HOST, "free", "-m"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return int(r.stdout.split("\n")[1].split()[6]) / 1024
    except Exception:  # noqa: BLE001
        return None


def make_room(model):
    """Before a large model loads, unload the others if RAM is short, so the model host never swaps."""
    need = BIG.get(model, 6)
    free = host_free_gb()
    if free is not None and free > need + 4:
        return
    unload_ollama(keep=model)
    time.sleep(3)


def ollama_schema(model, system, user, schema, timeout=600):
    body = json.dumps({"model": model, "stream": False, "think": False, "format": schema,
                       "options": {"temperature": 0.4, "num_predict": 900},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(f"{ollama_url(model)}/api/chat", body),
                                         timeout=timeout))
    return json.loads(r["message"]["content"])
