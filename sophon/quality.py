"""Quality layer for the local swarm pipeline (chat Claude's K-005 answer, 2 Nov 2026).

The editor's jobs moved into code: requirement schema (F8), facts sheet + mechanical flag check (F3), forced
diversity (F1), embedding dedupe + MMR selection (F2), anchored rubric + forced distribution (F5).
"""
import json
import math
import random
import re
import urllib.request

from .config import EMBED_MODEL, USER, ollama_url
from .settings import DATA_DIR, DEFAULT_DATA

FACTS = DEFAULT_DATA / "facts"


def fact_file(name):
    """facts/<name>: the user's copy in the data folder wins over the packaged one."""
    mine = DATA_DIR / "facts" / name
    return mine if mine.exists() else FACTS / name

# ---------- F8 requirement compiler ----------

REQ_SCHEMA = {"type": "object", "properties": {
    "count": {"type": "integer", "description": "number of items asked for (lower bound of a range), 0 if none"},
    "item_kind": {"type": "string", "description": "what one item is, e.g. 'idea', 'dissertation topic'"},
    "ordering": {"type": "string", "description": "how to order items, from the job text"},
    "exclusions": {"type": "array", "items": {"type": "string"}, "description": "themes or things the job forbids"},
    "format": {"type": "string", "enum": ["demo", "general", "academic", "technical", "writing"]},
    "audience": {"type": "string"},
    "ranking_criteria": {"type": "array", "items": {"type": "string"}}},
    "required": ["count", "item_kind", "ordering", "exclusions", "format", "audience", "ranking_criteria"]}


def regex_count(job):
    m = re.search(r"\b(\d{1,2})\s*(?:to|-|–)\s*\d{1,2}\b[^.\n]{0,40}?\b(?:ideas|items|topics|drafts|options)", job) \
        or re.search(r"\b(\d{1,2})\s+(?:[a-zA-Z-]+\s+){0,4}?(?:ideas|items|topics|drafts|options)\b", job)
    return int(m.group(1)) if m else 0


def requirements(job, schema_call, model):
    """Extract the job's hard requirements once; code enforces them afterwards."""
    try:
        req = schema_call(model, "You extract the hard requirements from a job description. JSON only. Only what "
                                 "the job text actually says; empty list or empty string when it says nothing.",
                          f"JOB:\n{job}", REQ_SCHEMA)
    except Exception:  # noqa: BLE001
        req = {"count": 0, "item_kind": "item", "ordering": "", "exclusions": [], "format": "general",
               "audience": USER, "ranking_criteria": []}
    mk = re.search(r"\b\d{1,2}\b[^.\n]{0,60}?\b((?:[a-z-]+\s+)?(?:ideas|items|topics|drafts|options|posts))\b", job, re.I)
    if mk:
        req["item_kind"] = mk.group(1).rstrip("s")
    neg = [m.end() for m in re.finditer(r"\b(no|not|without|avoid|never|nothing)\b", job, re.I)]

    def stated(x):  # an exclusion counts only if one of its words follows a negation in the job text itself
        return any(w in job.lower()[p:p + 50] for p in neg for w in re.findall(r"[a-z][a-z-]{3,}", x.lower()))
    req["exclusions"] = [x for x in req.get("exclusions", []) if stated(x)]
    rc = regex_count(job)
    if rc and req.get("count") != rc:  # the regex reads the literal number; trust it over the model
        req["count"] = rc
    if re.search(r"\bshorts?\b|viral|10-second|reel|tiktok|film", job, re.I):
        req["format"] = "demo"
    elif req.get("format") == "demo":  # the model may say demo for any 'impressive' job; demand a real cue
        req["format"] = "general"
    if req["format"] != "demo" and re.search(
            r"exact command|config(uration)? change|\bfaster\b|speed ?up|optimi[sz]e|\btune\b|benchmark|"
            r"how (do|can|would) i|measure|step[- ]by[- ]step|troubleshoot|\bfix\b|set ?up|install", job, re.I):
        req["format"] = "practical"  # concrete how-to answers: no collision mashups, commands from the facts sheet
    if re.search(r"dissertation|thesis|research question|msc project", job, re.I):
        req["format"] = "academic"
    if re.search(r"\banime\b", job, re.I) and not any("anime" in x.lower() for x in req["exclusions"]):
        if re.search(r"not\s+(?:\w+\s+){0,3}anime|no anime", job, re.I):
            req["exclusions"].append("anime or sci-fi theming")
    return req


def exclusion_words(req):
    words = set()
    for x in req.get("exclusions", []):
        for w in re.findall(r"[a-z][a-z-]{3,}", x.lower()):
            if w not in ("theme", "themed", "theming", "things", "anything", "framing", "ideas", "style"):
                words.add(w)
    return words

def forbidden_hits(text, words):
    """Forbidden words used as content, not in a denial like 'no anime' or 'not sci-fi themed'."""
    hits = set()
    for w in words:
        for m in re.finditer(rf"\b{re.escape(w)}", text, re.I):
            before = text[max(0, m.start() - 30):m.start()].lower()
            if not re.search(r"\b(no|not|non|without|avoid|zero|nor)\b[\s-]*([\w,/-]+[\s-]+){0,3}$", before):
                hits.add(w)
    return sorted(hits)


def check_dataset(body):
    """F10: the named dataset must be a known public one, or explicitly 'collect my own'."""
    m = re.search(r"dataset\W{0,6}:?\s*\**\s*([^\n]+)", body, re.I)
    if not m:
        return "no dataset named"
    val = m.group(1).strip(" *")
    if re.search(r"collect|own|custom|record|self-made|my own", val, re.I):
        return ""
    known = [d.strip().lower() for d in fact_file("datasets.md").read_text().split(",")] if fact_file("datasets.md").exists() else []
    if any(k and k in val.lower() for k in known):
        return ""
    return f"dataset '{val[:60]}' is not on the known list (check it exists)"


HOMELAB_TEXT = ("homelab server orchestration, Docker containers, Tailscale networking, self-hosted services, node "
                "monitoring dashboard, remote machine scheduling, backups, uptime")
_MOD = {}


def module_fit(text):
    """C-004 E: (best module, its cosine, cosine to homelab infra). A topic closer to homelab infra than to any
    programme module is off-programme."""
    if not _MOD:
        if not fact_file("modules.md").exists():
            raise FileNotFoundError("no facts/modules.md (optional: your programme's modules, one per line)")
        lines = [ln for ln in fact_file("modules.md").read_text().splitlines() if ln.strip()]
        vecs = embed(lines + [HOMELAB_TEXT])
        _MOD.update({"lines": lines, "vecs": vecs[:-1], "home": vecs[-1]})
    v = embed([text])[0]
    sims = [cos(v, m) for m in _MOD["vecs"]]
    i = max(range(len(sims)), key=sims.__getitem__)
    return _MOD["lines"][i].split(":")[0], sims[i], cos(v, _MOD["home"])


UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(GB|GiB|MB|TB|tok/s|tokens/s|ms|W|°C|%|B(?=\b))", re.I)


def unit_claims(text, allowed_text=""):
    """C-004 G3: numbers with units that appear neither on the facts sheet nor in the job."""
    known = fact_file("facts.md").read_text() if fact_file("facts.md").exists() else ""
    known += " " + allowed_text
    have = {(m.group(1), m.group(2).lower()) for m in UNIT_RE.finditer(known)}
    bad = []
    for m in UNIT_RE.finditer(text):
        if (m.group(1), m.group(2).lower()) not in have:
            bad.append(m.group(0))
    return sorted(set(bad))


# ---------- F3 facts sheet + mechanical claim check ----------


def _help_text():
    names = {p.name for d in (FACTS, DATA_DIR / "facts") if d.exists() for p in d.glob("*.help")}
    return "\n".join(fact_file(n).read_text(errors="ignore") for n in sorted(names))


def known_flags():
    return set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", _help_text()))


def ollama_subcommands():
    f = fact_file("ollama.help")
    if not f.exists():
        return set()
    t = f.read_text()
    sec = t.split("Available Commands:", 1)[-1].split("Flags:", 1)[0]
    return {ln.split()[0] for ln in sec.splitlines() if ln.strip()} | {"help"}


OLLAMA_API = {"generate", "chat", "embed", "embeddings", "tags", "show", "pull", "push", "create", "copy", "delete",
              "ps", "version", "blobs"}
TOOLS = re.compile(r"llama[-.]?(?:server|cli|bench|cpp)|ollama", re.I)


def check_claims(text):
    """Unknown llama.cpp / ollama flags, subcommands and API endpoints in a piece of text. Deterministic."""
    flags, subs = known_flags(), ollama_subcommands()
    if not flags:
        return []
    bad = []
    segs = re.findall(r"`[^`\n]+`", text) + [ln for ln in text.splitlines() if TOOLS.search(ln)]
    for seg in segs:
        if not TOOLS.search(seg) and not seg.startswith("`-"):
            continue
        for fl in re.findall(r"(?<![\w-])(--[A-Za-z][\w-]*)", seg):
            if fl not in flags:
                bad.append(f"`{fl}` is not a llama.cpp or ollama flag")
        for sc in re.findall(r"^`?ollama\s+([a-z][a-z-]+)", seg.strip()):
            if subs and sc not in subs and sc not in ("model", "models", "server", "api", "process", "daemon", "service", "instance", "command", "flag", "container", "endpoint", "library", "on", "and", "with",
                                                      "to", "is", "can", "the", "or", "for", "running", "serves"):
                if "`" in seg:
                    bad.append(f"`ollama {sc}` is not an ollama command")
    for ep in re.findall(r"/api/([a-z]+)", text):
        if ep not in OLLAMA_API and re.search(r"ollama|11434|11435", text, re.I):
            bad.append(f"`/api/{ep}` is not an Ollama endpoint")
    return sorted(set(bad))

# ---------- ideas: parse, embed, dedupe, MMR ----------


def parse_ideas(text, src):
    ideas = []
    for chunk in re.split(r"\n(?=#{2,4} )", "\n" + text):
        chunk = chunk.strip()
        head = chunk.split("\n", 1)[0]
        if not re.match(r"#{2,4} ", head) or head.lower().startswith(("## task from", "# ")):
            continue
        name = re.sub(r"^#{2,4}\s*(\d+[.)]\s*)?", "", head).strip(" *")
        if len(chunk) < 60 or not name or "<" in name or re.search(r"^\s*SKIP\s*$", chunk, re.M):
            continue
        ideas.append({"name": name[:90], "body": chunk[:900], "src": src})
    return ideas


def embed(texts):
    if not texts:
        return []
    body = json.dumps({"model": EMBED_MODEL, "input": ["clustering: " + t[:1500] for t in texts]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(f"{ollama_url(EMBED_MODEL)}/api/embed", body), timeout=300))
    out = []
    for v in r["embeddings"]:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def idea_text(i):
    lines = [ln for ln in i["body"].splitlines()[1:] if ln.strip()][:3]
    return i["name"] + ". " + " ".join(lines)


def dedupe(ideas, thresh=0.86):  # nomic: same-topic ideas sit ~0.78, true dupes 0.86+
    """Cluster near-duplicates (same mechanism). Keeps the first of each cluster, records what it absorbed."""
    vecs = embed([idea_text(i) for i in ideas])
    kept = []
    for i, v in zip(ideas, vecs):
        i["vec"] = v
        twin = next((k for k in kept if cos(k["vec"], v) > thresh), None)
        if twin:
            twin.setdefault("absorbed", []).append(i["name"])
        else:
            kept.append(i)
    return kept, len(ideas) - len(kept)


STOP = {"the", "a", "an", "of", "for", "in", "on", "to", "and", "via", "with", "as", "based", "aware", "driven"}


def name_twin(a, b):
    """Same idea renamed: every content word of the shorter name appears in the longer one (needs 2+ words)."""
    wa, wb = ({w for w in re.findall(r"[a-z0-9]+", x.lower()) if w not in STOP} for x in (a, b))
    small, big = sorted((wa, wb), key=len)
    return len(small) >= 2 and small <= big


def mmr(items, n, lam=0.5):
    """Greedy max-marginal relevance: score minus lam x max similarity to already picked (scores normalised 0-1)."""
    picked, pool = [], list(items)
    sims = sorted(cos(a["vec"], b["vec"]) for i, a in enumerate(items) for b in items[i + 1:]) or [0.0]
    base = sims[len(sims) // 2]  # same-topic baseline; only similarity above it counts as redundancy

    def red(it):
        return max((max(0.0, cos(it["vec"], p["vec"]) - base) / (1 - base + 1e-9) for p in picked), default=0)
    while pool and len(picked) < n:
        best = max(pool, key=lambda it: it["score"] - lam * red(it))
        picked.append(best)
        pool.remove(best)
    return picked

# ---------- F1 forced diversity ----------

CONSTRAINT_CARDS = [
    "Constraint card: at least one idea must run fully offline on a phone.",
    "Constraint card: at least one idea must be demoable from a single terminal window, no GUI.",
    "Constraint card: at least one idea must produce a measurable graph a reviewer could check.",
    "Constraint card: at least one idea must use something on the user's machines nobody has measured before.",
    "Constraint card: at least one idea must involve two machines talking to each other.",
    "Constraint card: at least one idea must be useful to the user every day, not only as a demo.",
    "Constraint card: at least one idea must fit in a single 300-line Python file.",
    "Constraint card: at least one idea must be something a university research lab would want to cite.",
]

HOMELAB_RE = re.compile(r"homelab|home server|Tailscale|tailnet|self-host|Docker|cross-continent", re.I)


def homelab(text):
    return len(HOMELAB_RE.findall(text)) >= 2


AXES = ("what triggers it", "which resource is scaled or used", "what the user sees", "who it is for")


def sampling(names, seed=None):
    rnd = random.Random(seed)
    return {n: {"temperature": 1.0, "seed": rnd.randrange(1, 2**31), "card": rnd.choice(CONSTRAINT_CARDS)}
            for n in names}

# ---------- C-004 B: collisions (asset x outside mechanism) ----------


def collisions(n_slots, seed):
    """One (asset, card) pair per idea slot, no card reused within a run, assets cycled."""
    if not fact_file("assets.md").exists():
        return []
    assets = [ln.strip() for ln in fact_file("assets.md").read_text().splitlines() if ln.strip()]
    cards = [ln.strip() for ln in fact_file("cards.md").read_text().splitlines() if ln.strip()]
    rnd = random.Random(seed)
    rnd.shuffle(assets)
    rnd.shuffle(cards)
    return [(assets[i % len(assets)], cards[i % len(cards)]) for i in range(n_slots)]


COLLISION_RULES = ("Each idea slot below pairs one of the user's real ASSETS with a MECHANISM from outside AI. The idea "
                   "must be impossible to describe without BOTH. Start each idea with 'Collision: X because Y' (one "
                   "sentence), then the usual fields. If a pairing is forced or silly, write the heading and 'SKIP' "
                   "only; do not pad.")


def familiar(vec, corpus_vecs, thresh=0.84):
    """C-004 B novelty gate: too close to something already in a previous FINAL."""
    return bool(corpus_vecs) and max(cos(vec, c) for c in corpus_vecs) > thresh


# ---------- F5 anchored rubric + forced distribution ----------

def _anchors(name, default):
    """Scoring calibration: facts/<name> in the data folder (your own past reactions) replaces the generic one."""
    mine = DATA_DIR / "facts" / name
    return mine.read_text().strip() if mine.exists() else default


ANCHORS = _anchors("anchors.md", """Calibration (score against these, quote the sentence in the idea that justifies
each score, and cap any score you cannot justify with a quote at 2):
- 5/5: something a viewer understands in ten seconds, visibly real engineering on the user's own hardware, with a
  measured number on screen, at no cost.
- 3/5: a fresh angle with a weak payoff, or a strong payoff that needs hardware or money the user does not have.
- 1/5: worthy but dull: someone else's paper, a benchmark, a dashboard or a wrapper with no ten-second moment.""")


ACADEMIC_ANCHORS = _anchors("anchors-academic.md", """Calibration for research topics (a supervisor's view; quote
the sentence that justifies each score, cap any score you cannot justify with a quote at 2):
- 5/5: a sharp research question with a named public dataset, a named metric, a baseline to beat, and a first
  experiment that fits in two weeks on the user's hardware.
- 3/5: a real question, but the dataset or metric is vague, or it needs hardware the user does not have.
- 1/5: a demo, product or visual effect with no research question; anything judged only on how impressive it looks.
Do NOT reward spectacle, virality or names here: this is a dissertation, judged on research value and feasibility.""")


def lenient(rows, frac=0.40):
    """True when the critic gave 4+ on every axis to more than frac of the ideas (the 20/20 behaviour)."""
    if len(rows) < 4:
        return False
    high = sum(1 for r in rows if r["nums"] and min(r["nums"][:4]) >= 4)
    return high / len(rows) > frac


def table_rows(text):
    rows = []
    for row in text.splitlines():
        cells = [x.strip(" *") for x in row.strip().strip("|").split("|")]
        nums = [int(x) for x in cells if re.fullmatch(r"[1-5]", x)]
        verdict = next((x.upper() for x in cells if x.upper() in ("KEEP", "FIX", "KILL")), None)
        if len(nums) >= 4 and verdict and cells[0] and not cells[0].lower().startswith("idea"):
            rows.append({"name": cells[0], "nums": nums, "verdict": verdict})
    return rows
