# Facts sheet (use these, never invent others)

Tag every command, flag, API or number in your ideas: [F] if it is on this sheet, [NEW] if it is your own claim.
If a capability is not here, write "requires building X" instead of inventing a flag or command.
The user's own machines are in facts/machines.md in their data folder, if they wrote one.

## Reference measurements (one 8-core Zen 4 mini PC, 32 GB DDR5, PCIe 4 NVMe at about 3.3 GB/s)
- Qwen3-Next 80B-A3B IQ4_XS (42.6 GB file), llama-server with the weights memory-mapped and a systemd
  MemoryMax cap: 3.8 / 5.5 / 7.5 tok/s generation at 8 / 16 / 20 GB cap; prompt reading 36 to 64 tok/s.
- gpt-oss 120B MXFP4 (63.4 GB file), same method: 2.5 / 4.1 tok/s at 8 / 16 GB cap; prompt reading about 17 tok/s.
- Swapping one big model for another takes 24 to 42 s.

## Real llama.cpp flags (llama-server / llama-cli)
- Offload and MoE: `-ngl N`, `--n-cpu-moe N`, `--cpu-moe`, `-ot / --override-tensor PATTERN=DEVICE`,
  `--device`, `--split-mode`, `--tensor-split`, `--rpc host:port` (spread layers over machines).
- Memory: `-lm / --load-mode` (mmap), `--no-repack`, `-c / --ctx-size`, `--cache-type-k`, `--cache-type-v`,
  `--kv-unified`, `--swa-full`, `--flash-attn`.
- Serving: `--host`, `--port`, `-np / --parallel`, `--jinja`, `--reasoning-budget`, `--cache-reuse N`,
  `--slot-save-path DIR` (save and restore a slot's KV cache over HTTP), `--metrics`.
- Speculative decoding: `-md / --model-draft`, `--spec-type` (none, draft-simple, ngram-simple, ngram-mod,
  ngram-cache, and more), `--draft-max`, `--spec-draft-n-max`.
- HTTP API: `/v1/chat/completions`, `/completion`, `/props`, `/slots`, `/health`, `/metrics`.
(Full `--help` output is in llama-server.help, llama-cli.help and llama-bench.help next to this file.)

## Real Ollama
- Commands: serve, create, show, run, stop, pull, push, signin, signout, list, ps, cp, rm, launch, help.
- API: /api/generate, /api/chat, /api/embed, /api/tags, /api/show, /api/pull, /api/create, /api/copy,
  /api/delete, /api/ps, /api/version. Options include num_ctx, num_gpu, temperature, seed, keep_alive.
- Ollama refuses models larger than available memory; it cannot stream experts from disk.

## Does NOT exist (never write these)
- `ollama quantize`, `ollama resize`, any `--bits` flag, partial-load or "load only some layers" API in Ollama.
- llama.cpp `--state`, `--load-state`, `--save-state`, "live resize" or "hot add layers" flags.
- Changing a model's parameter count at runtime without reloading. Moving a running model between machines
  without restarting it. (Both would need building.)
