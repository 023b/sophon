"""Claude usage from Claude Code transcripts (deduplicated by message id)."""
import json
import re

from .config import HOME


def claude_usage(run):
    """Claude usage for every session whose working directory is this run, from Claude Code transcripts.

    Claude Code writes one transcript line per content block and repeats the message's usage on each,
    so usage is keyed by message.id (last value wins). Returns {"fresh": input + output + cache write,
    "cache_read": cache reads, "messages": n}. The budget uses "fresh"; cache reads are reported apart
    because whether they count against plan limits is not documented."""
    d = HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(run))
    per = {}
    for f in d.glob("**/*.jsonl"):
        for n, line in enumerate(f.open()):
            try:
                m = json.loads(line).get("message", {})
            except ValueError:
                continue
            u = m.get("usage") if isinstance(m, dict) else None
            if isinstance(u, dict):
                per[m.get("id") or f"{f.name}:{n}"] = u
    fresh = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                for u in per.values())
    return {"fresh": fresh, "cache_read": sum(u.get("cache_read_input_tokens", 0) for u in per.values()),
            "messages": len(per)}


def claude_tokens(run):
    """Fresh Claude tokens (deduplicated by message id); see claude_usage."""
    return claude_usage(run)["fresh"]
