#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi
exec ssh -i "$LAMBDA_SSH_KEY" \
  -o StrictHostKeyChecking=accept-new \
  -o ExitOnForwardFailure=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=6 \
  -L 8000:127.0.0.1:8000 \
  -L 8001:127.0.0.1:8001 \
  -L 8002:127.0.0.1:8002 \
  -L 8080:127.0.0.1:8080 \
  -L 31495:127.0.0.1:31495 \
  -L 31490:127.0.0.1:31490 \
  -t "$LAMBDA" \
  'mkdir -p ~/llm-inference; cd ~/llm-inference; exec bash -l'
