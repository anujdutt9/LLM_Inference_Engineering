if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "This script is for the GPU box (run it there: bash setup/ssh.sh)."
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi missing: this script needs the GPU box."
  exit 1
fi
