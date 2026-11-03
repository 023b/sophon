# Changelog

## 0.1.0 (2026-11-03)

First public release.

- Lead agent with intake (triage, clarifying questions, mode choice) and five modes: claude, light, heavy, local,
  phantom.
- Watchable agents: one tmux session and kitty window each, a live board and a Textual control panel.
- Local pipeline driven by code: requirement schema, angles, fact and flag gate, embedding de-duplication,
  anchored critics, calibrated ranking, optional Sonnet editor pass.
- Phantom mode: 80B to 120B MoE models served by llama.cpp under a systemd memory cap, streamed from the SSD.
- OpenRouter engine with data-collection policy, health checks and reserve-then-settle spending caps.
- XDG file locations, TOML configuration, `swarm init`, `doctor`, `paths`, `personas`.
