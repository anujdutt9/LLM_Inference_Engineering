# Laptop shortcuts. Everything on the GPU box is plain `bash setup/...` / `bash experiments/...` (see README).
VENV := .venv
PY := $(VENV)/bin/python
export PYTHONPATH := $(CURDIR)

.PHONY: setup data test local local-27b replay grafana grafana-stop dashboards charts sync ssh gate

setup:            ## Python environment for the gateway, tests, Locust and analysis
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements.txt

data:             ## unpack the NovaAI database (datasets/exp1a/nova_ai.db), checksum verified
	python3 datasets/unpack.py

test:             ## the test suite (no GPU, no network needed)
	$(PY) -m pytest tests

local:            ## the gateway + two fake 9B replicas on this laptop (Setup B); UI on :8080/ui/
	bash setup/local_fleet.sh

local-27b:        ## the gateway + fake 27B and 9B engines (Setup A)
	FLEET=27b-9b bash setup/local_fleet.sh

replay:           ## the UI replaying the recorded full-mode demo run (samples/)
	bash experiments/replay.sh samples/exp4-demo-full/replay.jsonl

grafana:          ## Grafana on :3000 replaying the same run's Prometheus data (needs Docker)
	bash observability/local/replay.sh samples/exp4-demo-full/prometheus

grafana-stop:
	bash observability/local/replay.sh stop

dashboards:       ## regenerate k8s-config/observability/dashboards/*.json
	$(PY) -m observability.dashboards

charts:           ## re-draw docs/presentation/charts from the run folders (needs results/ from the Drive folder)
	$(PY) -m analysis.charts
	$(PY) -m analysis.talk_charts

sync:             ## copy the code to the GPU box (~/llm-inference)
	bash setup/sync_to_lambda.sh

ssh:              ## the SSH session + tunnel to the GPU box (keep it open)
	bash setup/ssh.sh

gate:             ## Exp-0: the checks every experiment starts from (tunnel open)
	bash experiments/exp0_check.sh
