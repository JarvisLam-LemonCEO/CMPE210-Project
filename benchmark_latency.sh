#!/usr/bin/env bash
set -euo pipefail

OUTFILE="${1:-latency_results.csv}"
COUNT="${2:-50}"
VIP="${3:-10.0.0.100:8000}"

H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')

if [[ -z "${H1PID}" ]]; then
  echo "ERROR: h1 process not found. Is Mininet running?"
  exit 1
fi

echo "latency_sec" > "$OUTFILE"

for i in $(seq 1 "$COUNT"); do
  set +e
  LAT=$(sudo mnexec -a "$H1PID" curl --no-keepalive -s -o /dev/null -m 5 -w '%{time_total}' "http://${VIP}/")
  RC=$?
  set -e

  if [[ $RC -ne 0 || -z "$LAT" ]]; then
    echo "nan" >> "$OUTFILE"
  else
    echo "$LAT" >> "$OUTFILE"
  fi

  sleep 0.05
done

echo "Saved $OUTFILE"
