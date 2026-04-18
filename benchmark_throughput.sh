#!/usr/bin/env bash
set -euo pipefail

VIP="${1:-10.0.0.100}"
PORT="${2:-5201}"
DURATION="${3:-10}"

H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')
H2PID=$(ps -eo pid,cmd | grep "mininet:h2" | grep -v grep | head -n1 | awk '{print $1}')
H3PID=$(ps -eo pid,cmd | grep "mininet:h3" | grep -v grep | head -n1 | awk '{print $1}')
H4PID=$(ps -eo pid,cmd | grep "mininet:h4" | grep -v grep | head -n1 | awk '{print $1}')

for PID in "$H2PID" "$H3PID" "$H4PID"; do
  sudo mnexec -a "$PID" pkill -f "iperf3 -s" || true
  sudo mnexec -a "$PID" iperf3 -s -p "$PORT" -D
done

sleep 1

sudo mnexec -a "$H1PID" iperf3 -c "$VIP" -p "$PORT" -t "$DURATION"
