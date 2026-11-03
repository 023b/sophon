"""Configuration, file locations and the big-model command: no models, network or windows needed."""
import importlib
import os
from pathlib import Path


def test_everything_lives_under_sophon_home():
    import sophon.settings as s
    home = Path(os.environ["SOPHON_HOME"])
    for d in (s.CONFIG_DIR, s.DATA_DIR, s.STATE_DIR, s.CACHE_DIR, s.RUNS_DIR):
        assert home in d.parents, d


def test_xdg_layout_without_sophon_home(monkeypatch, tmp_path):
    import sophon.settings as s
    monkeypatch.delenv("SOPHON_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    try:
        importlib.reload(s)
        assert s.CONFIG_DIR == tmp_path / "c" / "sophon"
        assert s.RUNS_DIR == tmp_path / "d" / "sophon" / "runs"
    finally:
        monkeypatch.undo()
        importlib.reload(s)


def test_user_config_merges_over_defaults(tmp_path):
    from sophon.settings import load_config
    f = tmp_path / "c.toml"
    f.write_text('[models]\nstrong = "qwen3:14b"\n[big]\nenabled = true\nram_cap = "8G"\n'
                 '[big.models.big-x]\nfile = "x.gguf"\nlabel = "X"\neffort = ""\n')
    c = load_config(f)
    assert c["models"]["strong"] == "qwen3:14b"
    assert c["models"]["worker"] == "gemma3:4b"          # untouched default
    assert c["big"]["ram_cap"] == "8G"
    assert set(c["big"]["models"]) == {"big-gptoss", "big-x"}


def test_example_config_parses():
    import tomllib

    from sophon.settings import PACKAGE
    with open(PACKAGE / "data" / "examples" / "config.toml", "rb") as fh:
        tomllib.load(fh)


def test_big_command_caps_memory_and_never_listens_publicly():
    from sophon.backends import big_command
    cmd = big_command({"file": "gpt-oss-120b-MXFP4.gguf"})
    assert "systemd-run --user" in cmd and "MemoryMax=" in cmd and "MemorySwapMax=0" in cmd
    assert "--host 127.0.0.1" in cmd and "0.0.0.0" not in cmd
    assert '$HOME/models/gpt-oss-120b-MXFP4.gguf' in cmd


def test_user_persona_overrides_packaged(tmp_path):
    from sophon.settings import DATA_DIR, data_file
    mine = DATA_DIR / "personas" / "skeptic.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("# Role: MY SKEPTIC\n")
    try:
        assert data_file("personas", "skeptic.md") == mine
    finally:
        mine.unlink()
    assert data_file("personas", "skeptic.md").parent.name == "personas"


def test_no_personal_data_in_package():
    """Guard for the public repo: no personal names, hosts, addresses or keys anywhere in the shipped files."""
    import re
    root = Path(__file__).resolve().parent.parent
    bad = re.compile(r"/home/[a-z]|100\.\d+\.\d+\.\d+|sk-or-v1-[0-9a-f]{8}|@gmail\.com", re.I)
    for p in list((root / "sophon").rglob("*")) + list((root / "docs").rglob("*")) + [root / "README.md"]:
        if p.is_file() and p.suffix in (".py", ".md", ".toml", ".help", ".txt", ".sh"):
            assert not bad.search(p.read_text(errors="ignore")), p
