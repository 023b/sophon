# Running 80B to 120B MoE models on a 16 GB machine

Sophon's `phantom` mode runs mixture-of-experts models that are several times larger than the machine's RAM:
gpt-oss 120B (63.4 GB file) and Qwen3-Next 80B (42.6 GB file) both run on a 16 GB laptop or a 26 GB mini PC.
They are slow (2.5 to 7.5 tokens per second) but they work, without swapping and without freezing the machine.

## Why it works

A mixture-of-experts model only uses a small part of its weights per token: gpt-oss 120B activates about 5.1B of
its 117B parameters, Qwen3-Next 80B about 3B of 80B. The rest sit idle for that token.

llama.cpp memory-maps the GGUF file instead of reading it into RAM. The kernel loads pages of the file on first
touch and keeps them in the page cache. If the server is placed in a cgroup with a memory limit, the kernel drops
the least recently used pages once the limit is reached and reads them back from the SSD when they are needed
again. The result is a model whose hot set (attention layers, shared experts, the most used experts and the KV
cache) stays in RAM while the cold experts stream from the SSD.

Three settings make this safe and predictable:

| Setting | Why |
|---|---|
| `systemd-run --user -p MemoryMax=CAP` | hard cap on the server's RAM, page cache included; the rest of the machine keeps its memory |
| `-p MemorySwapMax=0` | the server never pushes anything to swap; over the cap, clean file pages are dropped, which is cheap |
| `--no-repack` | stops llama.cpp from copying weights into a repacked in-RAM layout, which would defeat the mmap |

Sophon builds this command for you (see `big_command` in `sophon/backends.py`):

```bash
systemd-run --user --unit=sophon-big --collect -p MemoryMax=12G -p MemorySwapMax=0 \
  nice -n 5 ~/llama.cpp/build/bin/llama-server -m ~/models/gpt-oss-120b-MXFP4.gguf \
  -c 16384 -np 1 --host 127.0.0.1 --port 11600 --no-repack --jinja
```

It then waits for `/health`, streams answers from `/v1/chat/completions`, swaps models when a different one is
asked for (24 to 42 s), and stops the unit before Ollama loads anything, so the two never fight over RAM. A file
lock serialises all big-model calls, so one agent can never swap the model out from under another.

## Measured speed

llama.cpp CPU build, 8-core Zen 4 mini PC, DDR5, PCIe 4 NVMe at about 3.3 GB/s, single request:

| Model | File | Cap 8 GB | Cap 16 GB | Cap 20 GB | Prompt reading |
|---|---|---|---|---|---|
| gpt-oss 120B MXFP4 | 63.4 GB | 2.5 tok/s | 4.1 tok/s | not measured | about 17 tok/s |
| Qwen3-Next 80B-A3B IQ4_XS | 42.6 GB | 3.8 tok/s | 5.5 tok/s | 7.5 tok/s | 36 to 64 tok/s |

End-to-end check through Sophon itself (same machine, gpt-oss 120B, 8 GB cap, cold page cache): the server was
ready in 52 s, and a short question with reasoning took 236 s in total, 92 s of it before the first token.

More cap means more experts stay cached, which means fewer SSD reads per token. Speed is roughly bounded by SSD
read speed at small caps and by memory bandwidth at large ones. A SATA SSD will be several times slower; a hard
disk is not usable.

Long prompts are the slow part: at 17 tok/s, a 6,000-token context takes about six minutes to read before the
first output token. Sophon keeps prompts to the big models short and uses `cache_prompt` so a follow-up reuses the
KV cache.

## Choosing the cap

| Machine RAM | `ram_cap` | Notes |
|---|---|---|
| 16 GB | `8G` | leaves about 8 GB for the desktop, the browser and Sophon itself |
| 24 GB | `12G` | |
| 32 GB | `16G` to `20G` | the 20G row in the table above |
| 64 GB+ | most of the file | expected to approach memory-bandwidth speed (not measured) |

Keep the cap at least 3 GB above what the KV cache needs for your `ctx` (16K context is about 1 GB for these
models). If the server dies at start-up with an out-of-memory message in `journalctl --user -u sophon-big`, raise
the cap or lower `ctx`.

## With a GPU

Even a GPU with little VRAM helps, because the parts used for every token (attention, shared tensors, KV cache)
are small. Keep those on the GPU and the experts on the CPU side, still memory-mapped:

```toml
[big]
extra_args = ["--no-repack", "--jinja", "-ngl", "99", "--cpu-moe"]
```

`--cpu-moe` keeps all expert tensors on the CPU; `--n-cpu-moe N` keeps only the first N layers' experts there, for
GPUs with more VRAM. Build llama.cpp with `--cuda` or `--vulkan` (see the setup script).

## Setup

```bash
scripts/setup-big-model.sh gpt-oss-120b            # or qwen3-next-80b; --vulkan / --cuda for a GPU build
```

The script clones and builds llama.cpp (`llama-server` only), asks before downloading the model from Hugging Face
(`ggml-org/gpt-oss-120b-GGUF` or `unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF`), and prints the `[big]` block for
your config with a cap chosen from your RAM. Then:

```bash
swarm doctor                 # checks llama-server, the model files, systemd --user and RAM
swarm go --mode phantom
```

The big models can also live on another machine: set `big.host` to an SSH alias. Sophon starts the server there
and reaches it through an SSH tunnel; it still listens on 127.0.0.1 only.

## Which model does what

In tests, Qwen3-Next wrote 2 to 4 times faster than gpt-oss but judged leniently (it found "no objection" in 20 of
20 ideas), while gpt-oss reasons before judging and is harsh. With both downloaded, set `role = "writer"` on
Qwen3-Next and `role = "judge"` on gpt-oss. With one, it does both.

A full phantom run took 2 h 33 min and scored 5.5 out of 10 against 8.5 for a Claude-led run on the same job. Use it
when Claude usage or privacy matters more than time: one long job, left running overnight.

A second recorded run, with both models on a 26 GB mini PC at a 16 GB cap, took 3 h 50 min and used 0 Claude tokens.
It was not scored. Host samples every 2 s showed the model peaking at the 16.0 GB cap, at least 9.2 GB of RAM still
available while a big model was loaded, and 17.8 TB read from the SSD across the whole machine, because pages
dropped under the cap are read again. A Claude-led `light` run of the same job took 5 min 10 s.

## Requirements and limits

- Linux with systemd user sessions (`systemctl --user` must work; on a headless server run
  `loginctl enable-linger $USER` once).
- cgroup v2 with the memory controller delegated to user sessions (the default on current Ubuntu, Debian and
  Fedora).
- An SSD with room for the model. NVMe recommended.
- One request at a time (`-np 1`). Parallel requests would each need their own KV cache and their own experts.
