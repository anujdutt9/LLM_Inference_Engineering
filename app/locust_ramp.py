"""Exp-3's load shape: ramp simulated users 1 → 64 in steps, holding each step long enough
for steady-state percentiles. Use with the NovaUser class:

    locust -f app/locustfile.py,app/locust_ramp.py NovaUser --headless --host http://127.0.0.1:8080 \
        --csv results/exp3/<run>/locust --csv-full-history --html results/exp3/<run>/locust.html

RAMP_STEPS (users per step) and RAMP_HOLD_S (seconds per step) override the defaults.
"""

from __future__ import annotations

import os

from locust import LoadTestShape

STEPS = [int(x) for x in os.environ.get("RAMP_STEPS", "1,2,4,8,16,32,48,64").split(",")]
HOLD_S = int(os.environ.get("RAMP_HOLD_S", "60"))


class StepRamp(LoadTestShape):
    def tick(self):
        step = int(self.get_run_time() // HOLD_S)
        if step >= len(STEPS):
            return None  # done
        users = STEPS[step]
        return users, max(1, users)
