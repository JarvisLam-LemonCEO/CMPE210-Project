#!/usr/bin/env bash
set -euo pipefail

SUDO_PASSWORD="${SUDO_PASSWORD:-toor}"

sudo_cmd() {
  printf "%s\n" "$SUDO_PASSWORD" | sudo -S "$@"
}

pkill -f "/home/toor/anaconda3/envs/ryu-lab/bin/ryu-manager /home/toor/Desktop/maxwell/CMPE210-Project-main/ml_lb.py" >/dev/null 2>&1 || true
pkill -f "/home/toor/anaconda3/envs/ryu-lab/bin/ryu-manager /home/toor/Desktop/maxwell/CMPE210-Project-main/lb_nat_rr.py" >/dev/null 2>&1 || true
pkill -f "/home/toor/anaconda3/envs/ryu-lab/bin/ryu-manager /home/toor/Desktop/maxwell/CMPE210-Project-main/lb_least_loaded.py" >/dev/null 2>&1 || true
sudo_cmd mn -c >/dev/null 2>&1 || true
echo "Environment cleaned."
