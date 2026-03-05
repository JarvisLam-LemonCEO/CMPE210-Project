import csv
import os
import subprocess
import time

DECISIONS = "decisions.csv"
DATASET = "dataset.csv"
VIP_URL = "http://10.0.0.100:8000/"
N = 80              # number of requests to collect
SLEEP = 0.2         # small pause between requests

def find_h1_pid():
    # Mininet usually runs a shell with cmd containing "mininet:h1"
    out = subprocess.check_output(["bash", "-lc", "ps -eo pid,cmd | grep 'mininet:h1' | grep -v grep | head -n1"])
    pid = int(out.decode().strip().split(None, 1)[0])
    return pid

def curl_from_h1(h1_pid):
    # Use curl timing output; force new connection to reduce keep-alive effects
    cmd = f"sudo mnexec -a {h1_pid} curl --no-keepalive -s -o /dev/null -w '%{{time_total}}' {VIP_URL}"
    out = subprocess.check_output(["bash", "-lc", cmd])
    return float(out.decode().strip())

def read_last_decision():
    with open(DECISIONS, "r") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return None
    return rows[-1]  # last decision row

def ensure_dataset_header():
    if os.path.exists(DATASET):
        return
    with open(DATASET, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "decision_id", "ts",
            "client_ip", "client_tcp_src",
            "chosen_backend",
            "tx_rate_bps", "rx_rate_bps", "drop_delta", "active",
            "latency_sec"
        ])

def append_dataset(decision_row, latency):
    # decision_row format:
    # decision_id, ts, client_ip, client_tcp_src, chosen_backend,
    # b1 features(4), b2 features(4), b3 features(4), policy
    decision_id = decision_row[0]
    ts = decision_row[1]
    client_ip = decision_row[2]
    client_tcp_src = decision_row[3]
    chosen_backend = decision_row[4]
    policy = decision_row[-1]

    # Select the correct feature group based on chosen backend
    # b1 starts at idx 5, b2 at idx 9, b3 at idx 13
    if chosen_backend.endswith(".2"):
        base = 5
    elif chosen_backend.endswith(".3"):
        base = 9
    else:
        base = 13

    tx_rate = float(decision_row[base + 0])
    rx_rate = float(decision_row[base + 1])
    drop_delta = int(float(decision_row[base + 2]))
    active = int(float(decision_row[base + 3]))

    with open(DATASET, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([decision_id, ts, client_ip, client_tcp_src, chosen_backend,
                    tx_rate, rx_rate, drop_delta, active,
                    latency])

    print(f"[{policy}] backend={chosen_backend} latency={latency:.4f}s features=({tx_rate:.1f},{rx_rate:.1f},{drop_delta},{active})")

def main():
    ensure_dataset_header()
    h1_pid = find_h1_pid()
    print("h1 pid:", h1_pid)

    last_seen_decision = None

    for i in range(N):
        latency = curl_from_h1(h1_pid)

        # controller logs decision per new flow; wait briefly and read it
        time.sleep(0.05)
        d = read_last_decision()
        if d is None:
            print("No decision yet; did Ryu start? did you run pingall?")
            time.sleep(SLEEP)
            continue

        if last_seen_decision is not None and d[0] == last_seen_decision:
            # same decision id means curl may have reused connection; force by sleeping more
            print("Same decision_id (connection reuse). Sleeping to force new flow...")
            time.sleep(0.6)
            continue

        last_seen_decision = d[0]
        append_dataset(d, latency)
        time.sleep(SLEEP)

if __name__ == "__main__":
    main()