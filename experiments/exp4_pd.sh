#!/usr/bin/env bash
# Mac, tunnel open (ports 8001, 8002). Exp-4 P/D probe: can the two 9B replicas hand a request's KV cache from a
# prefill engine to a decode engine (vLLM's NixlConnector) on one GPU, with a hybrid model? A go / no-go probe,
# straight against the two vLLM pods (the gateway is not in the path), with a CHANGED fleet config, restored after.
#
#   bash experiments/exp4_pd.sh                  (~20 min: two fleet redeploys + the probe; NIXL)
#   CONNECTOR=mooncake bash experiments/exp4_pd.sh   (the same with vLLM's MooncakeConnector, TCP transport)
#
#   1. both replicas redeployed with --kv-transfer-config NixlConnector (kv_both), the NIXL side channel on the pod
#      IP, hostIPC (CUDA IPC between the two containers on the one GPU); KV 4.5 GiB, 8 slots as before
#   2. experiments/pd_probe.py: per document, (a) a cold full request on the decode replica (qwen-9b-b); (b) P/D:
#      prefill on qwen-9b-a (max_tokens 1, do_remote_decode) → decode on qwen-9b-b with the returned kv_transfer_params;
#      TTFT, prompt tokens computed on the decode side, and whether the answers match (temperature 0)
#   3. the fleet back to the normal config (KV_GIB=4.5, MAX_SEQS=8), Exp-0 gate
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
CONNECTOR="${CONNECTOR:-nixl}"
export EXP4_RUN="${EXP4_RUN:-exp4pd-$CONNECTOR-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
exec > >(tee -a "$RUN/run.log") 2>&1
K='export KUBECONFIG=/etc/rancher/k3s/k3s.yaml;'
echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC  P/D probe $EXP4_RUN =="
restore() {  # whatever happened, the fleet goes back to its normal config (the first attempt left a replica crash-looping)
  echo "== $(date -u +%H:%M:%S) fleet → back to the normal config (KV_GIB=4.5, MAX_SEQS=8) =="
  KV_GIB=4.5 MAX_SEQS=8 bash experiments/redeploy.sh fleet | grep -E "KV per replica|GPU KV cache size|Fleet up" || true
  for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
  if bash experiments/exp0_check.sh > "$RUN/exp0-restored.log" 2>&1; then tail -1 "$RUN/exp0-restored.log"; else grep FAIL "$RUN/exp0-restored.log" | head; echo "Exp-0 failed after restoring" >&2; fi
  bash experiments/snapshot.sh "$EXP4_RUN-after" | tail -1
}
trap restore EXIT
bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1

# the P/D fleet manifest: the demo fleet + NIXL on both replicas (rendered here, applied on the box)
CONNECTOR="$CONNECTOR" "$PY" - "$RUN" <<'PY'
import json, os, sys, yaml
connector = os.environ["CONNECTOR"]
kv_config = ({"kv_connector": "NixlConnector", "kv_role": "kv_both"} if connector == "nixl" else
             {"kv_connector": "MooncakeConnector", "kv_role": "kv_both",
              "kv_connector_extra_config": {"mooncake_protocol": "tcp"}})  # no RDMA on this box
# Mooncake's caller (a proxy) names the prefill engine and its bootstrap address: pin both
docs = [d for d in yaml.safe_load_all(open("k8s-config/hami/hami-demo.yaml")) if d]
for d in docs:
    if d.get("kind") != "Deployment":
        continue
    spec = d["spec"]["template"]["spec"]
    spec["hostIPC"] = True  # CUDA IPC between the two containers on the one GPU
    c = spec["containers"][0]
    c["image"] = c["image"].replace("__VLLM_IMAGE__", "vllm/vllm-openai:v0.30.0")
    args = [a for a in c["args"]]
    cfg = dict(kv_config, engine_id=d["metadata"]["name"]) if connector == "mooncake" else kv_config
    args += ["--kv-cache-memory-bytes", str(int(4.5 * 2**30)),
             "--kv-transfer-config", json.dumps(cfg)]
    c["args"] = args
    c["env"] = c.get("env", []) + [
        {"name": "VLLM_NIXL_SIDE_CHANNEL_HOST", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
        {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5600"},
        {"name": "UCX_TLS", "value": "cuda_ipc,cuda_copy,tcp"},
        # a hybrid model's Mamba/linear-attention state must be laid out DS for NIXL's 3-read conv transfer
        # (vLLM asserts it at start: the first attempt crash-looped without it)
        {"name": "VLLM_SSM_CONV_STATE_LAYOUT", "value": "DS"},
        {"name": "VLLM_MOONCAKE_BOOTSTRAP_PORT", "value": "8998"},
    ]
for name in ("vllm-9b-a", "vllm-9b-b"):  # one file per replica: they start one after the other
    mine = [d for d in docs if d["metadata"]["name"] == name]
    yaml.safe_dump_all(mine, open(f"{sys.argv[1]}/pd-{name}.yaml", "w"), sort_keys=False)
print("rendered", sys.argv[1] + "/pd-vllm-9b-{a,b}.yaml")
PY
scp -q -i "$LAMBDA_SSH_KEY" "$RUN/pd-vllm-9b-a.yaml" "$RUN/pd-vllm-9b-b.yaml" "$LAMBDA:/tmp/"

echo "== $(date -u +%H:%M:%S) fleet → P/D ($CONNECTOR on both replicas) =="
for d in vllm-9b-a vllm-9b-b; do
  if ! box "$K kubectl apply -f /tmp/pd-$d.yaml >/dev/null && kubectl rollout status deploy/$d --timeout=12m" | tail -1; then
    box "$K kubectl logs deploy/$d --tail=300 2>&1 | grep -E 'Error|assert|raise' | tail -15" | tee "$RUN/failure-$d.txt"
    echo "P/D: NO-GO ($d did not start with NIXL; see $RUN/failure-$d.txt)" >&2
    exit 1
  fi
done
box "$K kubectl logs deploy/vllm-9b-b 2>&1 | grep -iE 'nixl|mooncake|transfer engine|protocol|kv_transfer|kv connector|ucx' | tail -10" | tee "$RUN/connector-boot.log" || true

echo "== $(date -u +%H:%M:%S) probe =="
PREFILL_IP=$(box "$K kubectl get pod -l app=vllm-9b-a -o jsonpath='{.items[0].status.podIP}'")
"$PY" experiments/pd_probe.py --out "$RUN/pd_probe.json" --connector "$CONNECTOR" --prefill-bootstrap "http://$PREFILL_IP:8998" \
  || echo "probe failed (see above): no-go recorded"
box "$K kubectl logs deploy/vllm-9b-b --since=10m 2>&1 | grep -iE 'nixl|mooncake|transfer|remote|error' | tail -30" > "$RUN/decode-log.txt" || true
box "$K kubectl logs deploy/vllm-9b-a --since=10m 2>&1 | grep -iE 'nixl|mooncake|transfer|remote|error' | tail -30" > "$RUN/prefill-log.txt" || true

echo "== done: $RUN/pd_probe.json"
