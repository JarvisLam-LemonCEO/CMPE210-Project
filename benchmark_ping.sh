#!/usr/bin/env bash
set -euo pipefail

VIP="${1:-10.0.0.100}"
COUNT="${2:-20}"

H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')

if [[ -z "${H1PID}" ]]; then
  echo "ERROR: h1 process not found."
  exit 1
fi

sudo mnexec -a "$H1PID" ping -c "$COUNT" "$VIP"
