#!/usr/bin/env bash
# Mac. Keep what one experiment's agents produced:
#   results/outputs/<name>/report/       workspace/reports/<label>/ (the lead's md / html)
#   results/outputs/<name>/team/         the analysts' reports the lead read (tinyharness tmp/)
#   results/outputs/<name>/transcripts/  the lead's session and its subagents' sidechains
# The session is found from the gateway's log (tenant th-<last 8 of the session id>, task <label>).
#   bash experiments/save_outputs.sh exp1a                 # name = label
#   bash experiments/save_outputs.sh exp1a exp1a-attempt1  # store under another name
#   bash experiments/save_outputs.sh exp1a exp1a th-1234abcd  # only this tenant's session
set -euo pipefail
source "$(dirname "$0")/_common.sh"
label="${1:?usage: save_outputs.sh <label> [name]}"
name="${2:-$label}"
tenant="${3:-}"
out="results/outputs/$name"
# tinyharness keeps each project's sessions in ~/.tinyharness/projects/<slug of the resolved path>/
TH="$HOME/.tinyharness/projects/$("$PY" -c 'import hashlib, re, sys
r = sys.argv[1]; s = re.sub(r"[^A-Za-z0-9._-]", "-", r)
print(s if len(s) <= 120 else f"{s[-100:]}-{hashlib.sha256(r.encode()).hexdigest()[:10]}")' "$(cd "$ROOT/workspace" && pwd -P)")"
sids=$(box "python3 - <<'EOF'
import glob, json
seen = set()
for f in sorted(glob.glob('/home/ubuntu/llm-inference/results/live/*/gateway/requests.jsonl')):
    for line in open(f):
        r = json.loads(line)
        if r.get('task') == '$label' and r['tenant'].startswith('th-') and r['tenant'] in ('$tenant' or r['tenant'],):
            seen.add(r['tenant'][3:])
print(' '.join(sorted(seen)))
EOF")
[[ -n "$sids" ]] || { echo "no tinyharness session found for task $label in the gateway's logs" >&2; exit 1; }
mkdir -p "$out/report" "$out/team" "$out/transcripts"
rsync -a "workspace/reports/$label/" "$out/report/" 2>/dev/null || true
for sid in $sids; do
  session=$(ls "$TH"/sessions/*-"$sid".jsonl 2>/dev/null | head -1)
  [[ -n "$session" ]] || { echo "session $sid: no transcript in $TH/sessions" >&2; continue; }
  cp "$session" "$out/transcripts/"
  for d in "$TH"/tmp/*-"$sid"; do [[ -d "$d" ]] && cp "$d"/*.md "$out/team/" 2>/dev/null || true; done
  # this session's subagent sidechains: by the session id their first line records (tinyharness
  # 5592166+); older ones carry none, so for those, the ones written while this session was active
  # (with several sessions at once that picks up the others' too)
  start=$(stat -f %B "$session"); end=$(stat -f %m "$session")
  for f in "$TH"/sidechains/*.jsonl; do
    owner=$(head -1 "$f" | "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("session", ""))' 2>/dev/null || true)
    if [[ -n "$owner" ]]; then
      [[ "$owner" == *"$sid" ]] && cp "$f" "$out/transcripts/"
    else
      m=$(stat -f %m "$f")
      (( m >= start && m <= end + 60 )) && cp "$f" "$out/transcripts/"
    fi
  done
  echo "session $sid → $out"
done
find "$out" -type d -empty -delete
ls -R "$out" | head -40
