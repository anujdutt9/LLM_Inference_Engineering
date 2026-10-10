#!/usr/bin/env bash
# GPU box. Download the models (and the tokenizer the gateway uses) into the host cache the vLLM pods
# mount (/home/ubuntu/hf-cache), so pods only load weights, never download. ~19 GB (9B BF16);
# FLEET=27b-9b also downloads Qwen3.8-27B-FP8.
# Safe to re-run: finished files are skipped.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"

export HF_HOME="${HF_HOME:-/home/ubuntu/hf-cache}"
mkdir -p "$HF_HOME"

echo "== driver (vLLM v0.30 images default to CUDA 13: driver >= 580) =="
DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
echo "driver ${DRIVER}"
if [[ "${DRIVER%%.*}" -lt 580 ]]; then
  echo "WARNING: driver ${DRIVER} < 580. Use a CUDA 12 vLLM image: VLLM_IMAGE=<cu12 tag> bash setup/demo_apply.sh"
fi

python3 -m venv "$ROOT/.venv-hf"
"$ROOT/.venv-hf/bin/pip" install -q -U pip "huggingface_hub[hf_xet]>=0.34"
echo "== Qwen/Qwen3.5-9B =="
"$ROOT/.venv-hf/bin/hf" download Qwen/Qwen3.5-9B --exclude "*.pt" --exclude "*.bin" >/dev/null
if [[ "${FLEET:-two-9b}" == 27b-9b ]]; then  # Setup A also serves the 27B (~29 GB more)
  echo "== Qwen/Qwen3.8-27B-FP8 =="
  "$ROOT/.venv-hf/bin/hf" download Qwen/Qwen3.8-27B-FP8 --exclude "*.pt" --exclude "*.bin" >/dev/null
fi

du -sh "$HF_HOME/hub"/models--Qwen--* 2>/dev/null || true
echo "Models cached in $HF_HOME. Next: bash setup/demo_apply.sh"
