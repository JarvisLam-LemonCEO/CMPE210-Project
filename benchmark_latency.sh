#!/usr/bin/env bash
set -u

OUTFILE="${1:-latency_results.csv}"
COUNT="${2:-20}"
VIP_HOST="${3:-10.0.0.100}"
VIP_PORT="${4:-8000}"

H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')

if [[ -z "${H1PID}" ]]; then
  echo "ERROR: h1 process not found. Is Mininet running?"
  exit 1
fi

echo "Using h1 PID: $H1PID"

# warm-up: 3 requests to let RR touch all backends
for i in 1 2 3; do
  sudo mnexec -a "$H1PID" curl --no-keepalive -s -o /dev/null -m 5 "http://${VIP_HOST}:${VIP_PORT}/" >/dev/null 2>&1
  sleep 0.2
done

echo "latency_sec" > "$OUTFILE"

for i in $(seq 1 "$COUNT"); do
  LAT=$(sudo mnexec -a "$H1PID" curl --no-keepalive -s -o /dev/null -m 8 -w '%{time_total}' "http://${VIP_HOST}:${VIP_PORT}/" 2>/dev/null)
  RC=$?

  if [[ $RC -ne 0 || -z "$LAT" ]]; then
    echo "nan" >> "$OUTFILE"
    echo "[$i/$COUNT] failed"
  else
    echo "$LAT" >> "$OUTFILE"
    echo "[$i/$COUNT] $LAT"
  fi

  sleep 0.1
done

echo "Saved $OUTFILE"
