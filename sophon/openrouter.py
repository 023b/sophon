"""OpenRouter for the swarm: one more engine next to Claude, Ollama and the big streamed models.

Modes (saved in openrouter.json in the config folder, ~/.config/sophon by default):
  off   never call OpenRouter
  free  only models priced at 0, health-checked before use, automatic fallback when one fails
  paid  free first; paid models when asked for, inside a per-run and a daily spending cap and the account's credit

The API key lives in openrouter.key in the config folder (mode 600), or in $OPENROUTER_API_KEY,, never in runs, logs or memory.
"""
import contextlib
import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.request

from .settings import CACHE_DIR, CFG, CONFIG_DIR, STATE_DIR

API = "https://openrouter.ai/api/v1"
CONF = CONFIG_DIR
KEYF = CONF / "openrouter.key"
SETF = CONF / "openrouter.json"
STATE = STATE_DIR
LEDGER = STATE / "openrouter-ledger.jsonl"
HEALTH = STATE / "openrouter-health.json"
CATALOGUE = CACHE_DIR / "openrouter-models.json"
# data_collection "deny": only providers that do not store or train on prompts (OpenRouter provider routing);
# zdr: only zero-data-retention endpoints. auto_price_cap: automatic paid picks stay at or under this
# $/M output tokens; dearer models only when named explicitly (up to max_price_per_mtok).
DEFAULTS = {"mode": "off", "run_cap_usd": 0.50, "day_cap_usd": 2.00, "max_price_per_mtok": 20.0,
            "auto_price_cap": 5.0, "data_collection": "deny", "zdr": False}
HEADERS = {"X-Title": "Sophon swarm", **({"HTTP-Referer": CFG["openrouter"]["referer"]} if CFG["openrouter"]["referer"] else {})}
# not chat models even when priced at 0
NOT_CHAT = re.compile(r"lyria|content-safety|guard|embed|whisper|tts|image|rerank|moderation|scaledown/|summarize|extract", re.I)
# strong families for automatic picks; unknown models go after these. Paid picks are by value: the cheapest
# model from a strong family under auto_price_cap, never "most expensive first".
STRONG = 8  # the first STRONG entries count as strong families
PREFER = ["openai/gpt-5", "google/gemini", "deepseek/deepseek", "qwen/qwen3", "moonshotai/kimi",
          "z-ai/glm", "x-ai/grok", "nvidia/nemotron-3-ultra", "meta-llama/llama-4", "mistralai/mistral-large",
          "google/gemma-4-31b", "google/gemma-4-26b", "openai/gpt-oss", "nvidia/nemotron-3.5-super"]


class ORError(RuntimeError):
    pass


def provider_prefs(s=None):
    s = s or settings()
    p = {"data_collection": s.get("data_collection", "deny")}
    if s.get("zdr"):
        p["zdr"] = True
    return p


def _policy(s=None):
    p = provider_prefs(s)
    return f"{p['data_collection']}{'+zdr' if p.get('zdr') else ''}"


# ---------- settings and key ----------

def settings():
    s = dict(DEFAULTS)
    if SETF.exists():
        try:
            s.update(json.loads(SETF.read_text()))
        except json.JSONDecodeError:
            pass
    return s


def save_settings(**kw):
    CONF.mkdir(parents=True, exist_ok=True)
    s = settings()
    s.update(kw)
    SETF.write_text(json.dumps(s, indent=1))
    return s


def key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"].strip()
    return KEYF.read_text().strip() if KEYF.exists() else ""


def masked(k=None):
    k = key() if k is None else k
    return f"{k[:8]}…{k[-4:]}" if len(k) > 16 else ("set" if k else "none")


def set_key(k):
    """Check the key against OpenRouter before saving it; a bad key is never stored."""
    k = k.strip()
    if not re.fullmatch(r"sk-or-[A-Za-z0-9_-]{20,}", k):
        raise ORError("that does not look like an OpenRouter key (they start with sk-or-)")
    info = key_info(k)
    CONF.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        KEYF.write_text(k + "\n")
    finally:
        os.umask(old)
    KEYF.chmod(0o600)
    return info


def delete_key():
    if KEYF.exists():
        KEYF.unlink()
    save_settings(mode="off")


# ---------- HTTP ----------

def _req(path, body=None, k=None, timeout=30):
    k = k or key()
    if not k:
        raise ORError("no OpenRouter key saved (panel OPENROUTER box, or: swarm or key set)")
    h = {"Authorization": f"Bearer {k}", "Content-Type": "application/json", **HEADERS}
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{API}{path}", data, h), timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ORError(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ORError(f"network: {e}") from None


def key_info(k=None):
    """Usage, limit and remaining credit for this key (OpenRouter /key)."""
    d = _req("/key", k=k).get("data", {})
    out = {"label": d.get("label"), "usage": float(d.get("usage") or 0), "limit": d.get("limit"),
           "remaining": d.get("limit_remaining"), "free_tier": d.get("is_free_tier")}
    try:  # account balance; not every key may read it
        c = _req("/credits", k=k).get("data", {})
        out["balance"] = float(c.get("total_credits", 0)) - float(c.get("total_usage", 0))
    except ORError:
        out["balance"] = None
    return out


# ---------- catalogue ----------

def models(refresh=False):
    if CATALOGUE.exists() and not refresh and time.time() - CATALOGUE.stat().st_mtime < 6 * 3600:
        return json.loads(CATALOGUE.read_text())
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{API}/models", headers=HEADERS), timeout=30) as r:
            data = json.load(r)["data"]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
        if CATALOGUE.exists():  # stale beats nothing
            return json.loads(CATALOGUE.read_text())
        raise ORError(f"cannot read the OpenRouter model list: {e}") from None
    out = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            pin, pout = float(p.get("prompt") or 0), float(p.get("completion") or 0)
        except ValueError:
            continue
        mods = (m.get("architecture") or {}).get("output_modalities") or ["text"]
        if "text" not in mods or NOT_CHAT.search(m["id"]) or m["id"].endswith(":batch") \
                or m["id"] in ("openrouter/auto", "openrouter/free"):
            continue
        if pin < 0 or pout < 0:  # variable-priced routers
            continue
        out.append({"id": m["id"], "name": m.get("name", m["id"]), "ctx": m.get("context_length") or 0,
                    "in": pin * 1e6, "out": pout * 1e6, "free": pin == 0 and pout == 0,
                    "reasoning": "reasoning" in (m.get("supported_parameters") or [])})
    CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE.write_text(json.dumps(out))
    return out


def family(m):
    return next((k for k, p in enumerate(PREFER) if m["id"].startswith(p)),
                len(PREFER) + (5 if m["id"].startswith("anthropic/") else 0))  # Claude last: Claude Code covers it directly


def rank(m):
    """Free picks: preferred families first, then bigger. Paid picks: strong family first, then CHEAPEST
    (value, not price as a proxy for strength), then bigger."""
    size = max([int(x) for x in re.findall(r"(?<![\d.])(\d{1,4})b(?![a-z])", m["id"].lower())] or [0])
    if m["free"]:
        return (family(m), -size, -m["ctx"])
    small = 0 < size < 30 or bool(re.search(r"-(mini|nano|micro|tiny|lite)\b", m["id"]))
    return (0 if family(m) < STRONG else 1, small, m["in"] + 3 * m["out"], family(m), -size)


def rank_best(m):
    """or:best: strong family, not small, then the dearest under auto_price_cap (price as a strength proxy)."""
    r = rank(m)
    return (r[0], r[1], -(m["in"] + 3 * m["out"]))

# ---------- health: free models often fail, so every pick is probed and failures are remembered ----------


def _health():
    try:
        return json.loads(HEALTH.read_text()) if HEALTH.exists() else {}
    except json.JSONDecodeError:
        return {}


def _mark(mid, ok, why="", cool=0):
    h = _health()
    h[mid] = {"ok": ok, "t": time.time(), "why": why[:160], "until": time.time() + cool if cool else 0,
              "policy": _policy()}
    STATE.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(h, indent=1))


def usable(mid):
    e = _health().get(mid)
    if e and e.get("policy", "allow") != _policy():  # checked under another data policy: check again
        return True
    return not e or e["ok"] or time.time() > e.get("until", 0)


def cooldown_for(err):
    s = str(err)
    if "free-models-per-day" in s or "per-day" in s:
        return 6 * 3600, "daily free limit reached"
    if "429" in s:
        return 10 * 60, "rate limited"
    if "data policy" in s or "data_collection" in s:
        return 6 * 3600, "no provider meets the data policy"
    if re.search(r"HTTP 40[034]", s) or "No endpoints" in s or "agentic harness" in s:
        return 24 * 3600, "not available"
    if "thinking used the budget" in s:
        return 5 * 60, "thought too long"
    if "402" in s:
        return 3600, "needs credit"
    return 15 * 60, "failed"


def probe(mid):
    """A tiny real request: does this model answer right now?"""
    t0 = time.time()
    try:
        # reasoning models think first: give room and ask for low effort, or a working model looks empty
        r = _req("/chat/completions", {"model": mid, "max_tokens": 600, "temperature": 0,
                                       "reasoning": {"effort": "low"}, "provider": provider_prefs(),
                                       "messages": [{"role": "user", "content": "Reply with the single word OK."}]},
                 timeout=60)
        msg = (r.get("choices") or [{}])[0].get("message") or {}
        txt = msg.get("content") or ""
        if not txt.strip():
            raise ORError("empty reply" + (" (only reasoning, no answer)" if msg.get("reasoning") else ""))
        _mark(mid, True)
        return True, round(time.time() - t0, 1)
    except ORError as e:
        cool, why = cooldown_for(e)
        _mark(mid, False, f"{why}: {e}", cool)
        return False, str(e)[:120]


def candidates(tier="free", min_ctx=8000):
    """Models allowed by the current mode for this tier, best first, known-bad ones skipped."""
    mode = settings()["mode"]
    if mode == "off":
        raise ORError("OpenRouter is off (panel OPENROUTER box, or: swarm or mode free|paid)")
    ms = [m for m in models() if m["ctx"] >= min_ctx and usable(m["id"])]
    if tier == "free" or mode == "free":
        pool = [m for m in ms if m["free"]]
    else:
        cap = settings()["auto_price_cap"]
        pool = [m for m in ms if not m["free"] and m["out"] <= cap
                and not re.search(r"-pro\b|-pro$|deep-research", m["id"])]  # slow reasoning tiers: explicit only
        if tier == "cheap":
            return sorted(pool, key=lambda m: (m["in"] + m["out"], rank(m)))
    return sorted(pool, key=rank)


def pick(tier="free", n=1, min_ctx=8000):
    """First n candidates that pass a live probe (recently healthy ones are trusted for 20 minutes)."""
    good = []
    for m in candidates(tier, min_ctx):
        e = _health().get(m["id"])
        if e and e["ok"] and time.time() - e["t"] < 1200 and e.get("policy") == _policy():
            good.append(m["id"])
        elif probe(m["id"])[0]:
            good.append(m["id"])
        if len(good) >= n:
            break
    if not good:
        raise ORError(f"no {tier} OpenRouter model is answering right now")
    return good

# ---------- spending ----------


def spent(since=0.0, run=None):
    if not LEDGER.exists():
        return 0.0
    tot = 0.0
    for ln in LEDGER.read_text().splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e["t"] >= since and (run is None or e.get("run") == run):
            tot += e.get("cost", 0)
    return tot


def today():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


@contextlib.contextmanager
def _ledger_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "openrouter-ledger.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def guard(m, prompt_chars, max_tokens, run):
    """Refuse a paid call that could break a cap, the price ceiling or the account's credit; otherwise RESERVE
    the worst case in the ledger (under a lock, so parallel workers cannot all pass the same check).
    Returns (estimate, reservation id or None). Settle with settle() after the call."""
    if m["free"]:
        return 0.0, None
    s = settings()
    if s["mode"] != "paid":
        raise ORError(f"{m['id']} is a paid model and OpenRouter is set to {s['mode']}")
    if m["out"] > s["max_price_per_mtok"]:
        raise ORError(f"{m['id']} costs ${m['out']:.2f}/M output tokens, over the ${s['max_price_per_mtok']:.2f} ceiling")
    est = (prompt_chars / 3.5) * m["in"] / 1e6 + max_tokens * m["out"] / 1e6
    try:  # network call outside the lock
        info = key_info()
        left = info["remaining"] if info["remaining"] is not None else info.get("balance")
    except ORError:
        left = None
    with _ledger_lock():
        run_left = s["run_cap_usd"] - spent(run=run)
        day_left = s["day_cap_usd"] - spent(since=today())
        if est > run_left:
            raise ORError(f"{m['id']} could cost ${est:.3f}; only ${run_left:.3f} left of this run's cap")
        if est > day_left:
            raise ORError(f"{m['id']} could cost ${est:.3f}; only ${day_left:.3f} left of today's cap")
        if left is not None and est > float(left):
            raise ORError(f"{m['id']} could cost ${est:.3f}; the account has ${float(left):.3f}")
        rid = f"{os.getpid()}-{time.time():.6f}"
        _append({"t": time.time(), "run": run, "model": m["id"], "cost": est, "kind": "reserve", "rid": rid})
    return est, rid


def _append(entry):
    STATE.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _ledger(run, mid, usage):
    _append({"t": time.time(), "run": run, "model": mid, "in": usage.get("prompt_tokens", 0),
             "out": usage.get("completion_tokens", 0), "cost": float(usage.get("cost") or 0)})


def settle(run, mid, usage, est, rid):
    """Record the real cost and release the reservation. A crash between guard and settle leaves the
    reservation in place, which over-counts (safe side) until the day or run ends."""
    with _ledger_lock():
        if usage:
            _ledger(run, mid, usage)
        if rid:
            _append({"t": time.time(), "run": run, "model": mid, "cost": -est, "kind": "release", "rid": rid})

# ---------- chat with fallback ----------


def resolve(model):
    """'or:auto' / 'or:free' -> best healthy free; 'or:paid' / 'or:best' -> best paid within caps;
    'or:cheap' -> cheapest paid; 'or:<id>' -> that model, with free fallbacks if it fails."""
    name = model[3:] if model.startswith("or:") else model
    if name in ("auto", "free"):
        return candidates("free"), True
    if name == "paid":
        return candidates("paid") + candidates("free"), True
    if name == "best":
        return sorted(candidates("paid"), key=rank_best) + candidates("free"), True
    if name == "cheap":
        return candidates("cheap") + candidates("free"), True
    cat = {m["id"]: m for m in models()}
    if name not in cat:
        raise ORError(f"unknown OpenRouter model {name} (see: swarm or models)")
    return [cat[name]] + [m for m in candidates("free") if m["id"] != name], False


def stream(model, messages, out=None, num_predict=1800, temperature=0.6, run=None, tries=4):
    """Stream a chat from OpenRouter. On an error, an empty reply or no first token within 90 s, the model is
    marked unhealthy and the next candidate is tried. Returns (text, first_token_wait, tok_per_s, model_used)."""
    pool, auto = resolve(model)
    chars = sum(len(m.get("content") or "") for m in messages)
    pool = [m for m in pool if m["ctx"] == 0 or m["ctx"] * 3.5 > chars + num_predict * 4]
    errors = []
    for m in pool[:tries]:
        max_tokens = num_predict + (2500 if m.get("reasoning") else 0)
        try:
            est, rid = guard(m, chars, max_tokens, run)
        except ORError as e:
            errors.append(str(e))
            continue
        if out:
            out.write(f"\033[2m(OpenRouter: {m['id']}{' free' if m['free'] else ''})\033[0m\n")
            out.flush()
        body = {"model": m["id"], "messages": messages, "stream": True,
                "max_tokens": max_tokens, "temperature": temperature, "usage": {"include": True},
                "provider": provider_prefs()}
        if m.get("reasoning"):
            body["reasoning"] = {"max_tokens": 1500, "exclude": True}  # a hard thinking budget, then the answer
        h = {"Authorization": f"Bearer {key()}", "Content-Type": "application/json", **HEADERS}
        text, t0, first, n, usage = [], time.time(), None, 0, {}
        try:
            req = urllib.request.Request(f"{API}/chat/completions", json.dumps(body).encode(), h)
            with urllib.request.urlopen(req, timeout=90) as r:
                for raw in r:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    ev = json.loads(line[5:])
                    if ev.get("error"):
                        raise ORError(json.dumps(ev["error"])[:300])
                    usage = ev.get("usage") or usage
                    piece = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                    if piece:
                        first = first or time.time()
                        n += 1
                        text.append(piece)
                        if out:
                            out.write(piece)
                            out.flush()
            txt = "".join(text)
            if len(txt.strip()) < 2:
                raise ORError("empty reply (thinking used the budget)" if usage.get("completion_tokens") else
                              "empty reply")
            _mark(m["id"], True)
            settle(run, m["id"], usage, est, rid)
            gen = time.time() - (first or time.time())
            return txt, (first or time.time()) - t0, (n / gen if gen > 0 else 0), m["id"]
        except urllib.error.HTTPError as e:
            err = ORError(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")
        except (ORError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        settle(run, m["id"], usage, est, rid)
        cool, why = cooldown_for(err)
        _mark(m["id"], False, f"{why}: {err}", cool)
        errors.append(f"{m['id']}: {why}")
        if out:
            out.write(f"\n\033[2m({m['id']} {why}; trying the next model)\033[0m\n")
        if not auto and m is pool[0] and not pool[1:]:
            break
    raise ORError("OpenRouter: no model completed the request. " + " | ".join(errors[-4:]))


def status_line():
    s = settings()
    k = key()
    if not k:
        return f"{s['mode'].upper()} · no key"
    sp = spent(since=today())
    return (f"{s['mode'].upper()} · data {_policy(s)} · key {masked(k)} · today ${sp:.3f} of ${s['day_cap_usd']:.2f}"
            f" · run cap ${s['run_cap_usd']:.2f}")
