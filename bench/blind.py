"""C-004 F: blind comparison of two FINAL.md files. Strips mode, tables and timestamps, shuffles to X and Y,
shows the top 5 cards of each. Usage: blind.py RUN_A RUN_B   (the key is appended to blind-key.txt in the state
folder, so you cannot see it while scoring)"""
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from metrics import cards  # noqa: E402

from sophon.settings import STATE_DIR  # noqa: E402

runs = sys.argv[1:3]
order = runs[:]
random.shuffle(order)
for label, r in zip("XY", order):
    print(f"\n{'=' * 30} {label} {'=' * 30}\n")
    for i, c in enumerate(cards((Path(r) / "FINAL.md").read_text())[:5], 1):
        body = re.sub(r"\n\s*[-*]*\s*\**(Worker|Critic Scores?|Strongest Objection|Critic)\**:.*", "", c["body"])
        body = re.sub(r"\(source worker:.*?\)|\[F\]|\[NEW\]", "", body)
        print(re.sub(r"^#+\s*\d*[.)]?\s*", f"### {label}{i}. ", body, count=1).strip(), "\n")
print("\nScore each card: Pursue 0/1/2 (would build or film it) and Surprise 0/1. Total /15 per side. "
      "Then: which would you rather receive, X or Y?")
STATE_DIR.mkdir(parents=True, exist_ok=True)
(STATE_DIR / "blind-key.txt").open("a").write(f"X={order[0]} Y={order[1]}\n")
