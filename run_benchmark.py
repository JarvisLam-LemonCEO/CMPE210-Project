from __future__ import annotations

import csv
import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DECISIONS_CSV = BASE_DIR / "decisions.csv"
DATASET_CSV = BASE_DIR / "dataset.csv"
VIP_URL = os.environ.get("VIP_URL", "http://10.0.0.100:8000/")
NUM_REQUESTS = int(os.environ.get("NUM_REQUESTS", "50"))
WARMUP_REQUESTS = int(os.environ.get("WARMUP_REQUESTS", "3"))


def sudo_prefix() -> list[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-S"]


def run_cmd(cmd: list[str]) -> str:
    kwargs = {"text": True}
    if cmd[:2] == ["sudo", "-S"]:
        kwargs["input"] = os.environ.get("SUDO_PASSWORD", "") + "\n"
    return subprocess.check_output(cmd, **kwargs).strip()


def find_h1_pid() -> str:
    cmd = r"ps -eo pid,cmd | grep 'mininet:h1' | grep -v grep | head -n1 | awk '{print $1}'"
    result = subprocess.check_output(cmd, shell=True, text=True).strip()
    if not result:
        raise RuntimeError("Could not find h1 PID. Is Mininet running?")
    return result


def curl_from_h1(h1_pid: str) -> float:
    cmd = sudo_prefix() + [
        "mnexec", "-a", h1_pid,
        "curl", "--no-keepalive", "-s", "-o", "/dev/null",
        "-w", "%{time_total}", VIP_URL
    ]
    out = run_cmd(cmd)
    return float(out)


def latest_decision():
    if not DECISIONS_CSV.exists():
        return None

    with DECISIONS_CSV.open("r", newline="") as f:
        rows = list(csv.DictReader(f))
        if not rows:
            return None
        return rows[-1]


def decision_key(decision: dict | None):
    if not decision:
        return None
    return (
        decision.get("timestamp"),
        decision.get("client_ip"),
        decision.get("client_tcp_src"),
        decision.get("backend_ip"),
    )


def init_dataset():
    if not DATASET_CSV.exists():
        with DATASET_CSV.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "backend_index",
                "tx_rate_bps",
                "rx_rate_bps",
                "drop_delta",
                "active_flows_assigned",
                "total_tx_rate_bps",
                "total_rx_rate_bps",
                "total_active_flows",
                "tx_share",
                "rx_share",
                "active_flow_share",
                "tx_imbalance_bps",
                "rx_imbalance_bps",
                "flow_imbalance",
                "latency_sec",
                "backend_ip",
                "policy",
            ])


def append_dataset_row(decision: dict, latency_sec: float):
    with DATASET_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            int(decision["backend_index"]),
            float(decision["tx_rate_bps"]),
            float(decision["rx_rate_bps"]),
            float(decision["drop_delta"]),
            int(decision["active_flows_assigned"]),
            float(decision["total_tx_rate_bps"]),
            float(decision["total_rx_rate_bps"]),
            int(decision["total_active_flows"]),
            float(decision["tx_share"]),
            float(decision["rx_share"]),
            float(decision["active_flow_share"]),
            float(decision["tx_imbalance_bps"]),
            float(decision["rx_imbalance_bps"]),
            float(decision["flow_imbalance"]),
            latency_sec,
            decision["backend_ip"],
            decision["policy"],
        ])


def main():
    if not DECISIONS_CSV.exists():
        raise RuntimeError("decisions.csv not found. Start ml_lb.py first.")

    init_dataset()
    h1_pid = find_h1_pid()

    print(f"Using h1 PID: {h1_pid}")

    print("Warm-up requests...")
    for _ in range(WARMUP_REQUESTS):
        try:
            _ = curl_from_h1(h1_pid)
        except Exception:
            pass
        time.sleep(0.2)

    print("Collecting dataset...")
    for i in range(1, NUM_REQUESTS + 1):
        decision_before = latest_decision()
        before_key = decision_key(decision_before)

        try:
            latency_sec = curl_from_h1(h1_pid)
        except Exception as exc:
            print(f"[{i}/{NUM_REQUESTS}] failed: {exc}")
            time.sleep(0.2)
            continue

        time.sleep(0.1)
        decision_after = latest_decision()
        after_key = decision_key(decision_after)
        decision = decision_after if after_key != before_key else decision_before

        if decision is None:
            print(f"[{i}/{NUM_REQUESTS}] skipped: no decision logged yet")
            time.sleep(0.2)
            continue

        append_dataset_row(decision, latency_sec)
        print(
            f"[{i}/{NUM_REQUESTS}] backend={decision['backend_ip']} "
            f"policy={decision['policy']} latency={latency_sec:.6f}s"
        )
        time.sleep(0.2)

    print(f"Done. Dataset saved to {DATASET_CSV}")


if __name__ == "__main__":
    main()
