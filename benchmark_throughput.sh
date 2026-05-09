#!/usr/bin/env bash

# -----------------------------------------
# Enable strict Bash error handling
# -----------------------------------------
# -e : exit immediately if a command fails
# -u : treat unset variables as errors
# -o pipefail : fail if any command in a pipeline fails
set -euo pipefail

# -----------------------------------------
# Script Arguments
# -----------------------------------------
# $1 -> Virtual IP address (VIP)
# $2 -> iperf3 server port
# $3 -> Test duration in seconds
#
# Default values:
# VIP      = 10.0.0.100
# PORT     = 5201
# DURATION = 10 seconds
VIP="${1:-10.0.0.100}"
PORT="${2:-5201}"
DURATION="${3:-10}"

# -----------------------------------------
# Find Mininet host PIDs
# -----------------------------------------
# h1 = client host
# h2, h3, h4 = backend server hosts
#
# We locate each Mininet host process ID
# so commands can be executed inside the
# corresponding network namespace.
H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')
H2PID=$(ps -eo pid,cmd | grep "mininet:h2" | grep -v grep | head -n1 | awk '{print $1}')
H3PID=$(ps -eo pid,cmd | grep "mininet:h3" | grep -v grep | head -n1 | awk '{print $1}')
H4PID=$(ps -eo pid,cmd | grep "mininet:h4" | grep -v grep | head -n1 | awk '{print $1}')

# -----------------------------------------
# Start iperf3 servers on backend hosts
# -----------------------------------------
# For each backend server:
# 1. Stop any existing iperf3 server
# 2. Start a new iperf3 server in daemon mode
#
# iperf3 options:
# -s       : run as server
# -p PORT  : listen on specified port
# -D       : run as daemon/background process
for PID in "$H2PID" "$H3PID" "$H4PID"; do

  # Stop old iperf3 servers if they exist
  sudo mnexec -a "$PID" pkill -f "iperf3 -s" || true

  # Start new iperf3 server
  sudo mnexec -a "$PID" iperf3 -s -p "$PORT" -D
done

# -----------------------------------------
# Wait for servers to initialize
# -----------------------------------------
sleep 1

# -----------------------------------------
# Run iperf3 client from h1
# -----------------------------------------
# Connect to the load balancer VIP
# to measure throughput performance.
#
# iperf3 options:
# -c VIP        : connect to server at VIP
# -p PORT       : server port
# -t DURATION   : test duration in seconds
sudo mnexec -a "$H1PID" \
  iperf3 -c "$VIP" -p "$PORT" -t "$DURATION"
