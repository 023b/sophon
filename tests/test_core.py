"""Model-free tests: parsing, guards, state locking, usage counting."""
import json
import multiprocessing as mp
import threading


def test_triage_cleaner_fills_and_coerces(sw):
    t = sw.clean_triage({"understood": "yes", "confidence": "0.8", "agents": "12", "stakes": "LOW", "questions": "x"})
    assert t["understood"] is True and t["confidence"] == 0.8 and t["agents"] == 10
    assert t["stakes"] == "low" and t["questions"] == []
    assert sw.clean_triage("junk") is None
    assert sw.clean_triage({})["stakes"] == "high"


def test_bash_write_blocker(sw):
    blocked = ["cat > out/x.md <<EOF\nhi\nEOF", "echo hi > f.txt", "printf 'a' > f", "python3 - <<'PY'\nx\nPY",
               "ls | tee out.txt"]
    allowed = ["swarm say sophon \"done, see out/x.md\"", "ls -la | grep md", "python3 a.py 2>&1 | tail -5",
               "grep -c x a.md > /dev/null", "echo done"]
    assert all(sw.BASH_WRITE.search(c) for c in blocked)
    assert not any(sw.BASH_WRITE.search(c) for c in allowed[:3])


def test_ask_risky_backstop(sw):
    for q in ("Should I spend $5 on credits?", "Can I delete the old runs?", "publish the post now?",
              "edit ~/notes/a.md?", "use sudo to set NUM_PARALLEL?"):
        assert sw.ASK_RISKY.search(q), q
    for q in ("Which of the three formats does he prefer?", "Should the list be ranked by novelty first?"):
        assert not sw.ASK_RISKY.search(q), q


def test_external_profile_keeps_only_judging_rules(sw):
    prof = ("# Me\n## Who I am\nprivate stuff\n## Decision rules\n- rule one\n## Voice\nmy voice\n"
            "## Judging\n- be harsh\n## Current project seed\nsecret plan\n")
    out = sw.external_profile(prof)
    assert "rule one" in out and "be harsh" in out
    assert "private" not in out and "secret" not in out and "voice" not in out


def test_usage_dedupes_by_message_id(sw, tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    proj = tmp_path / "home" / ".claude" / "projects"
    d = proj / __import__("re").sub(r"[^A-Za-z0-9]", "-", str(run))
    d.mkdir(parents=True)
    u = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}
    lines = [{"message": {"id": "m1", "usage": u}}] * 3 + [{"message": {"id": "m2", "usage": u}}]
    (d / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    import sophon.budget as b
    monkeypatch.setattr(b, "HOME", tmp_path / "home")
    r = b.claude_usage(run)
    assert r == {"fresh": 230, "cache_read": 2000, "messages": 2}


def _bump(run, n):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from sophon.util import state
    for _ in range(n):
        with state(run) as st:
            st["n"] = st.get("n", 0) + 1


def test_state_lock_no_lost_updates(tmp_path):
    from sophon.util import load
    (tmp_path / "state.json").write_text(json.dumps({"agents": {}}))
    ps = [mp.Process(target=_bump, args=(tmp_path, 50)) for _ in range(4)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    assert load(tmp_path)["n"] == 200


def test_openrouter_reservations_respect_run_cap(tmp_path, monkeypatch):
    import sophon.openrouter as O
    monkeypatch.setattr(O, "STATE", tmp_path)
    monkeypatch.setattr(O, "LEDGER", tmp_path / "l.jsonl")
    monkeypatch.setattr(O, "settings", lambda: dict(O.DEFAULTS, mode="paid", run_cap_usd=0.10))
    monkeypatch.setattr(O, "key_info", lambda: {"remaining": None, "balance": 30.0})
    m = {"id": "x/fake", "free": False, "in": 1.0, "out": 10.0}
    ok = []

    def go():
        try:
            ok.append(O.guard(m, 3500, 4000, "r1"))
        except O.ORError:
            pass
    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 2
    for est, rid in ok:
        O.settle("r1", "x/fake", {"cost": 0.005}, est, rid)
    assert abs(O.spent(run="r1") - 0.01) < 1e-9


def test_openrouter_free_mode_refuses_paid(monkeypatch):
    import sophon.openrouter as O
    monkeypatch.setattr(O, "settings", lambda: dict(O.DEFAULTS, mode="free"))
    try:
        O.guard({"id": "x", "free": False, "in": 1, "out": 1}, 10, 10, "r")
        raise AssertionError("should refuse")
    except O.ORError as e:
        assert "paid" in str(e)


def test_memo_request_ids_unique(sw, tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({"agents": {}}))
    monkeypatch.setenv("SWARM_ID", "w1")
    import sophon.agents as a
    monkeypatch.setattr(a, "report", lambda run, words: None)
    for i in range(3):
        a.memo_request(tmp_path, [f"t{i}", "body https://example.com/x"])
    files = sorted(p.name for p in (tmp_path / "requests").glob("*.md"))
    assert files == ["001.md", "002.md", "003.md"]
    assert "sources: https://example.com/x" in (tmp_path / "requests" / "001.md").read_text()


def test_say_counts_messages_on_fresh_state(sw, tmp_path, monkeypatch):
    """Regression: the counter used to read state['sent'] before creating it (KeyError on the first message)."""
    import sophon.agents as a
    (tmp_path / "state.json").write_text(json.dumps({"agents": {"w1": {}, "sophon": {}}}))
    monkeypatch.setenv("SWARM_ID", "w1")
    monkeypatch.setattr(a, "alive", lambda n: True)
    monkeypatch.setattr(a, "deliver", lambda run, s, t, m: "delivered")
    a.say(tmp_path, "sophon", ["hello"])
    a.say(tmp_path, "sophon", ["again"])
    from sophon.util import load
    assert load(tmp_path)["sent"]["w1"] == 2


def test_editor_splits_final_cards():
    from sophon.editor import split_cards
    text = ("# Start here\n\nwow\n\n---\n\n# FINAL\n\n## Ranking\n| 1 | a |\n\n# FINAL.md\n\n### 1. Alpha\n- one\n\n"
            "### 2. Beta\n- two\n\n### Killed Ideas\n- x\n")
    cards = split_cards(text)
    assert [c["title"] for c in cards] == ["Alpha", "Beta"]
    assert cards[1]["body"] == "- two"


def test_practical_jobs_get_no_collisions():
    import sophon.quality as Q
    fail = lambda *a, **k: (_ for _ in ()).throw(Exception())  # noqa: E731
    assert Q.requirements("Give me 8 ways to make my home server faster, with the exact command", fail, "x")["format"] == "practical"
    assert Q.requirements("10 dissertation topic ideas for my MSc", fail, "x")["format"] == "academic"
    assert Q.requirements("ideas for a viral 10-second Short", fail, "x")["format"] == "demo"
