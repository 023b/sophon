# Contributing

Bug reports, fixes and new personas are welcome.

## Set up

```bash
git clone https://github.com/023b/sophon && cd sophon
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check sophon tests bench swarm
.venv/bin/pytest -q
```

The tests need no models, network or windows, and run against a throwaway `SOPHON_HOME`, so they never touch your
config, runs or keys.

## Guidelines

- Keep modules acyclic in the order listed in [docs/architecture.md](docs/architecture.md).
- Anything that changes `state.json` goes through `with state(run) as st:`.
- New safety rails belong in code (hooks, caps, allowlists), not only in prompts.
- Numbers in docs must be measured; say on what hardware, or mark them as estimates.
- No personal data in the package: `tests/test_config.py::test_no_personal_data_in_package` checks for it.
- A new persona is one Markdown file in `sophon/data/personas/`; keep the idea or verdict format of the others.

## Reporting a bug

Include `swarm doctor` output, the mode, and the relevant lines of the run's `board.log`. Remove anything private
first. For a security problem (an agent acting outside its run folder, spending without a cap), use a private
security advisory instead of an issue.
