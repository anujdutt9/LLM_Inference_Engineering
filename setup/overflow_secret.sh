#!/usr/bin/env bash
# Mac. Put the overflow provider's key (OVERFLOW_API_KEY in your local .env) into a
# Kubernetes secret on the GPU box, then restart the gateway so it picks it up. The key goes
# over SSH on stdin: it is never on a command line, in a file on the box, or in the repo.
#   bash setup/overflow_secret.sh            # create / update the secret, restart the gateway
#   bash setup/overflow_secret.sh --delete   # remove it (the gate then only counts)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/.env"; set +a
SSH=(ssh -i "$LAMBDA_SSH_KEY" -o StrictHostKeyChecking=accept-new "$LAMBDA")
if [[ "${1:-}" == "--delete" ]]; then
  "${SSH[@]}" "kubectl delete secret orch-overflow --ignore-not-found && kubectl rollout restart deploy/orch-serve"
  exit 0
fi
[[ -n "${OVERFLOW_API_KEY:-}" ]] || { echo "Set OVERFLOW_API_KEY in .env first." >&2; exit 1; }
printf 'OVERFLOW_API_KEY=%s\n' "$OVERFLOW_API_KEY" | "${SSH[@]}" \
  "kubectl create secret generic orch-overflow --from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null && kubectl rollout restart deploy/orch-serve >/dev/null && echo 'overflow key stored; gateway restarting'"
