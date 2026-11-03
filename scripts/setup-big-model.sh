#!/usr/bin/env bash
# Build llama.cpp (CPU, optionally Vulkan or CUDA) and download one big MoE model for phantom mode.
#
#   scripts/setup-big-model.sh [gpt-oss-120b | qwen3-next-80b] [--vulkan | --cuda] [--dir ~/models]
#
# Downloads are large (63.4 GB / 42.6 GB). Nothing is downloaded without the confirmation prompt.
# Needs: git, cmake, a C++ compiler, curl or the `hf` CLI, about 70 GB free on an SSD (NVMe strongly preferred).
set -euo pipefail

MODEL="${1:-gpt-oss-120b}"; shift || true
BACKEND=""; MODELS_DIR="$HOME/models"; LLAMA_DIR="$HOME/llama.cpp"
while [ $# -gt 0 ]; do
  case "$1" in
    --vulkan) BACKEND="-DGGML_VULKAN=ON" ;;
    --cuda)   BACKEND="-DGGML_CUDA=ON" ;;
    --dir)    MODELS_DIR="$2"; shift ;;
    --llama)  LLAMA_DIR="$2"; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac; shift
done

case "$MODEL" in
  gpt-oss-120b)   REPO="ggml-org/gpt-oss-120b-GGUF";               FILE="gpt-oss-120b-MXFP4.gguf";                 SIZE="63.4 GB"; KEY="big-gptoss" ;;
  qwen3-next-80b) REPO="unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF"; FILE="Qwen3-Next-80B-A3B-Instruct-IQ4_XS.gguf"; SIZE="42.6 GB"; KEY="big-qwen3next" ;;
  *) echo "model must be gpt-oss-120b or qwen3-next-80b"; exit 2 ;;
esac

echo "== llama.cpp in $LLAMA_DIR"
if [ ! -x "$LLAMA_DIR/build/bin/llama-server" ]; then
  [ -d "$LLAMA_DIR/.git" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DCMAKE_BUILD_TYPE=Release $BACKEND
  cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc)" --target llama-server
else
  echo "already built"
fi

mkdir -p "$MODELS_DIR"
DEST="$MODELS_DIR/$FILE"
echo "== $FILE ($SIZE) from huggingface.co/$REPO"
if [ -f "$DEST" ]; then
  echo "already downloaded: $DEST"
else
  df -h "$MODELS_DIR" | tail -1
  read -r -p "Download $SIZE to $DEST? [y/N] " ok
  [ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "skipped"; exit 0; }
  if command -v hf >/dev/null; then
    hf download "$REPO" "$FILE" --local-dir "$MODELS_DIR"
  else
    curl -L --fail -C - -o "$DEST" "https://huggingface.co/$REPO/resolve/main/$FILE"
  fi
fi

RAM_GB=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo)
CAP=$(( RAM_GB <= 16 ? 8 : (RAM_GB <= 24 ? 12 : 16) ))
cat <<MSG

Done. Add this to ~/.config/sophon/config.toml (swarm init creates the file):

[big]
enabled = true
llama_server = "$LLAMA_DIR/build/bin/llama-server"
models_dir = "$MODELS_DIR"
ram_cap = "${CAP}G"        # this machine has ${RAM_GB} GB; see docs/big-models-low-ram.md

[big.models.$KEY]
file = "$FILE"

Then: swarm doctor, and `swarm go --mode phantom`.
MSG
