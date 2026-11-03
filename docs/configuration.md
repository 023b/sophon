# Configuration

Everything is optional. `swarm init` writes a commented `~/.config/sophon/config.toml`; the full example is
[sophon/data/examples/config.toml](../sophon/data/examples/config.toml). Unknown keys are ignored, missing ones use
the defaults in `sophon/settings.py`.

## Sections

| Section | Keys | Notes |
|---|---|---|
| `[user]` | `name`, `style` | how agents refer to you, and the writing style every prompt asks for |
| `[ollama]` | `url`, `ssh_host`, `remote`, `tunnel_port`, `cpu_url`, `cpu_models`, `host_label` | local or remote Ollama; with `ssh_host`, Sophon opens the tunnel |
| `[models]` | `triage`, `worker`, `strong`, `available`, `roster`, `critics`, `embed`, `sizes_gb` | Ollama model names |
| `[big]` | `enabled`, `host`, `llama_server`, `models_dir`, `ram_cap`, `threads`, `ctx`, `port`, `extra_args` | phantom mode, see [big-models-low-ram.md](big-models-low-ram.md) |
| `[big.models.NAME]` | `file`, `label`, `effort`, `role`, `repo`, `ctx`, `args` | one table per big model |
| `[claude]` | `budget`, `editor` | fresh-token budget per run; Sonnet editor pass on or off |
| `[memory]` | `mcp_prefix`, `read_tools`, `write_tools` | optional long-term memory through an MCP server |
| `[openrouter]` | `referer` | OpenRouter mode, caps and policy are set with `swarm or ...` |

## Environment variables

| Variable | Effect |
|---|---|
| `SOPHON_HOME`, `SOPHON_CONFIG` | file locations, see [file-locations.md](file-locations.md) |
| `OPENROUTER_API_KEY` | used instead of the saved key |
| `SWARM_BIG_CAP` | overrides `big.ram_cap` for one run |
| `SWARM_CLAUDE_BUDGET` | overrides `claude.budget` |
| `SWARM_EDITOR=off` | skips the Sonnet editor pass |
| `SWARM_ROSTER`, `SWARM_CRITIC_MODELS` | comma-separated model lists for the local pipeline |
| `SWARM_LOCAL_LEAD`, `SWARM_LOCAL_BEST` | the local lead model, and the model that answers for you while you sleep |
| `SWARM_COLLIDE=0` | turns off the collision generator |
| `SWARM_MAX_MSGS` | per-agent message cap (default 60) |
| `SWARM_FONT`, `SWARM_FONT_LAPTOP` | kitty font sizes on the large and the small screen |
| `SWARM_WINDOWS=all` | open every worker window even with one screen |

## Long-term memory

If you have an MCP server for notes added to Claude Code (for example one exposing `search_notes`, `get_note`,
`create_note`), set `mcp_prefix` to its tool prefix (`mcp__<server>__`). Every Claude agent may then read through
it, only the lead may write, and workers ask for writes with `swarm memo-request`. With `mcp_prefix = ""` the
memory tools are not offered at all.
