#!/usr/bin/env bash
# Mac, no GPU or gateway needed. Replay recorded runs in the pipeline UI for a presentation:
# serves ui/ and results/ locally and opens the page with the file(s) loaded.
#
#   bash experiments/replay.sh results/replays/exp1a.jsonl
#   bash experiments/replay.sh results/replays/exp2-full-a.jsonl results/replays/exp2-full-b.jsonl
#   bash experiments/replay.sh                       # the page; drag files onto it
#
# results/replays/ comes from experiments/collect.sh (python -m analysis.replays results).
# Stop with Ctrl-C.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8090}"
q=""
for f in "$@"; do
  [[ -f "$f" ]] || { echo "no such file: $f" >&2; exit 1; }
  rel="$("${PY:-$ROOT/.venv/bin/python}" -c 'import os,sys; print(os.path.relpath(os.path.abspath(sys.argv[1]), sys.argv[2]))' "$f" "$ROOT")"
  [[ "$rel" == ..* ]] && { echo "keep replay files inside the project (e.g. results/replays/): $f" >&2; exit 1; }
  q="${q:+$q,}/$rel"
done
url="http://127.0.0.1:$PORT/ui/${q:+?replay=$q}"
echo "Replay: $url"
(sleep 1; open "$url" 2>/dev/null || true) &
exec "${PY:-$ROOT/.venv/bin/python}" -m http.server "$PORT" --bind 127.0.0.1
