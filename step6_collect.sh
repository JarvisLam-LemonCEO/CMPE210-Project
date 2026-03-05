#!/usr/bin/env bash
set -euo pipefail

POLICY="$1"      # round_robin | least_loaded | ml_pred
TRIALS="${2:-5}"
N="${3:-100}"

RAW_DIR="raw_trials"
mkdir -p "$RAW_DIR"

# Find h1 PID (mininet namespace)
H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')
if [[ -z "${H1PID}" ]]; then
  echo "ERROR: could not find mininet:h1 PID. Is Mininet running?"
  exit 1
fi

VIP_URL="http://10.0.0.100:8000/"

echo "Policy=$POLICY Trials=$TRIALS SamplesPerTrial=$N h1pid=$H1PID"

for t in $(seq 1 "$TRIALS"); do
  OUT="$RAW_DIR/${POLICY}_trial${t}.csv"
  echo "latency_sec" > "$OUT"

  # Warmup (avoid first-request ARP/flow install effects)
  sudo mnexec -a "$H1PID" curl --no-keepalive -s -o /dev/null -m 2 "$VIP_URL" >/dev/null || true
  sleep 0.2

  for i in $(seq 1 "$N"); do
    set +e
    LAT=$(sudo mnexec -a "$H1PID" curl --no-keepalive -s -o /dev/null -m 2 -w '%{time_total}' "$VIP_URL")
    RC=$?
    set -e

    if [[ $RC -ne 0 || -z "$LAT" ]]; then
      echo "nan" >> "$OUT"
    else
      echo "$LAT" >> "$OUT"
    fi
    sleep 0.05
  done

  echo "Saved $OUT"
done