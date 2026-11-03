# Safety

Sophon runs several autonomous agents with tools on your machine, sometimes overnight. The rails below are enforced
in code; the prompts say the same things, but nothing depends on a model obeying them.

## Claude Code agents

- Each run writes `hooks.json` and starts every agent with `--settings` pointing at it. A `PreToolUse` hook on Bash
  blocks file writes through the shell (heredocs, `cat`/`echo`/`printf` redirected to a file, `tee`): agents
  must use Write and Edit, which are visible and confined by Claude Code's own permission rules. Other Bash
  commands are not allowed at all unless a run grants them with `--allow`.
- Agents get an explicit `--allowedTools` list: read and search tools, Write and Edit, web tools, and only the
  `swarm` subcommands their role needs. The lead alone may spawn, kill, change the mode and write memory.
- `Notification`, `Stop` and `UserPromptSubmit` hooks report each agent's state (working, idle, waiting on a
  dialog) to the run folder, so the panel and lead never have to scrape the screen.
- `--add-dir` is opt-in per run; by default agents work only inside the run folder.

## While you sleep

- `swarm go --sleep` (or the panel's sleep button) tells every agent never to wait for you.
- `swarm ask --type preference|scope|priority|format|clarify` is answered from your profile. Any other question,
  and any question that mentions money, deleting, sending, publishing, accounts, URLs or paths outside the run, is
  refused by a regex backstop and written to `out/needs-you.md`.
- A permission dialog the panel cancels is reported as "cancelled by the panel, not by the user", and the agent is
  told to carry on and list the action for you.
- Memory requests from workers that read the web are held for you.
- The machine is kept awake with `systemd-inhibit` for the length of the run only.

## OpenRouter

- Off by default. The key is stored in the config folder with mode 600, or read from `OPENROUTER_API_KEY`; it is
  never written to a run, a log or a prompt.
- Provider routing uses `data_collection: deny` by default (only providers that do not store or train on prompts),
  or `zdr` (zero data retention endpoints only). `swarm or policy allow` must be chosen explicitly.
- Models outside your machines see only the "Decision rules" and "Judging" sections of your profile.
- Paid calls reserve their worst-case cost (prompt plus maximum output at the listed price) in a ledger under a file
  lock before the request, then settle to the real cost. A call that would exceed the run cap, the day cap, the
  account credit or the price ceiling is refused before it is sent.
- `swarm private on` blocks OpenRouter for the current run.

## Big models

- `llama-server` binds to 127.0.0.1 only. With `big.host` set, it is reached through an SSH tunnel.
- It runs as a transient systemd user unit with `MemoryMax` and `MemorySwapMax=0`, and is stopped at the end of a
  run and before Ollama loads anything.

## Reporting a problem

Open an issue. For anything that could let an agent act outside its run folder or spend money without a cap,
please open a private security advisory on GitHub instead of a public issue.
