# File locations

Sophon never writes inside its source tree or the installed package. It follows the
[XDG base directory spec](https://specifications.freedesktop.org/basedir-spec/latest/); `swarm paths` prints the
folders on your machine.

| Folder | Default | Holds | Back it up? |
|---|---|---|---|
| config | `$XDG_CONFIG_HOME/sophon` (`~/.config/sophon`) | `config.toml`, `openrouter.key` (mode 600), `openrouter.json` (mode, caps, data policy) | yes, except the key |
| data | `$XDG_DATA_HOME/sophon` (`~/.local/share/sophon`) | `runs/`, `current` (link to the latest run), `profile.md`, your `personas/` and `facts/` | yes |
| state | `$XDG_STATE_HOME/sophon` (`~/.local/state/sophon`) | `openrouter-ledger.jsonl` (every reservation and charge), `openrouter-health.json`, `big.lock` | the ledger, if you track spending |
| cache | `$XDG_CACHE_HOME/sophon` (`~/.cache/sophon`) | `openrouter-models.json` (catalogue, refreshed every 6 h) | no |

Overrides:

- `SOPHON_HOME=/path` puts everything under one folder as `config/`, `data/`, `state/` and `cache/`. Useful for a
  portable install, a second profile, or tests (the test suite uses a temporary one).
- `SOPHON_CONFIG=/path/file.toml` uses a different config file.

## A run folder

`swarm go` creates `runs/<name>-<YYYYMMDD-HHMM>/` and points `current` at it:

```
context/        BRIEF.md, JOB.md (your words), profile.md, facts.md, requirements.json, sampling.json
agents/         each agent's assembled system prompt (NAME.sys.md)
out/            each agent's work (NAME.md), questions.md, needs-you.md
msgs/           long messages between agents
inbox/          batched reports for the lead (heavy mode digest)
requests/       memory requests (NNN.md) and their decisions
status/         one line per agent: working, idle, dialog, done
board.log       every message, report and decision, in order
state.json      mode, agents, budget, settings; changed only under a file lock
hooks.json      the Claude Code hooks for this run
FINAL.md        the result (FINAL-local.md: the version before the Sonnet editor pass)
```

Runs are plain text and self-contained: delete a folder to forget a run, copy it to share one.

## Personas and facts

Packaged defaults live in `sophon/data/` and are read-only. To change one, copy it into the data folder with the
same name and edit the copy; yours wins:

```bash
cp "$(python3 -c 'import sophon,os;print(os.path.dirname(sophon.__file__))')/data/personas/skeptic.md" \
   ~/.local/share/sophon/personas/skeptic.md
```

New personas work the same way: `~/.local/share/sophon/personas/NAME.md`, then `swarm spawn w1 NAME`.
`swarm personas` lists them all and marks yours.

Optional facts files (in `~/.local/share/sophon/facts/`):

| File | Effect |
|---|---|
| `machines.md` | your machines; added to every local worker's rules so they stop guessing hardware |
| `assets.md` | your things, one per line; enables the "collision" generator for open idea jobs |
| `modules.md` | your course or programme modules, one per line; research topics are checked against them |
| `anchors.md`, `anchors-academic.md` | your own scoring calibration; replaces the generic one |
| `facts.md` | replaces the packaged facts sheet of real llama.cpp and Ollama flags |
