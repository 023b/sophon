"""C-004 F: automatic metrics for a run's FINAL.md. Usage: metrics.py RUN_DIR [RUN_DIR ...]"""
import itertools
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sophon.quality as Q  # noqa: E402
from sophon.config import RUNS  # noqa: E402


def cards(text):
    body = text.split("\n---\n")
    body = max(body, key=lambda b: len(re.findall(r"^###\s", b, re.M)))
    return [i for i in Q.parse_ideas(body, "final") if not i["name"].lower().startswith(("killed", "items killed"))]


def metrics(run):
    run = Path(run)
    t = (run / "FINAL.md").read_text()
    job = (run / "context" / "JOB.md").read_text() if (run / "context" / "JOB.md").exists() else ""
    cs = cards(t)
    vecs = Q.embed([Q.idea_text(c) for c in cs]) if cs else []
    pairs = [Q.cos(a, b) for a, b in itertools.combinations(vecs, 2)]
    clusters = []
    for v in vecs:
        if not any(Q.cos(v, c) > 0.86 for c in clusters):
            clusters.append(v)
    prev = sorted((p for p in RUNS.glob("*/FINAL.md") if p.parent != run and
                   p.stat().st_mtime < (run / "FINAL.md").stat().st_mtime), key=lambda p: p.stat().st_mtime)[-15:]
    corpus = [Q.idea_text(i) for p in prev for i in Q.parse_ideas(p.read_text(), "p")][:400]
    cv = Q.embed(corpus) if corpus else []
    fresh = (sum(max(Q.cos(v, c) for c in cv) for v in vecs) / len(vecs)) if vecs and cv else 0
    return {"run": run.name, "cards": len(cs), "unknown_flags": len(Q.check_claims(t)),
            "unit_claims": len(Q.unit_claims(t, job)), "clusters": len(clusters),
            "max_pair_cos": round(max(pairs), 3) if pairs else 0, "staleness": round(fresh, 3)}


if __name__ == "__main__":
    for r in sys.argv[1:]:
        print(metrics(r))
