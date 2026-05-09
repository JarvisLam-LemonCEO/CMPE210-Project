#!/usr/bin/env bash

# Exit behavior:
# -u : treat unset variables as an error
set -u

# -----------------------------
# Script Arguments
# -----------------------------
# $1 -> Output CSV filename
# $2 -> Number of test requests
# $3 -> Virtual IP address (VIP)
# $4 -> Virtual IP port
#
# Default values are provided if arguments are omitted.
OUTFILE="${1:-latency_results.csv}"
COUNT="${2:-20}"
VIP_HOST="${3:-10.0.0.100}"
VIP_PORT="${4:-8000}"

# -----------------------------
# Find the PID of Mininet host h1
# -----------------------------
# Mininet creates namespaces/processes for each host.
# We locate the PID for host h1 so we can execute commands inside it.
H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')

# -----------------------------
# Validate that h1 exists
# -----------------------------
if [[ -z "${H1PID}" ]]; then
  echo "ERROR: h1 process not found. Is Mininet running?"
  exit 1
fi

echo "Using h1 PID: $H1PID"

# -----------------------------
# Warm-up Requests
# -----------------------------
# Send a few initial requests before measurement.
# This helps:
# 1. Populate ARP tables
# 2. Trigger switch flow installation
# 3. Allow Round Robin load balancer
#    to touch all backend servers
for i in 1 2 3; do
  sudo mnexec -a "$H1PID" \
    curl --no-keepalive -s -o /dev/null -m 5 \
    "http://${VIP_HOST}:${VIP_PORT}/" >/dev/null 2>&1

  sleep 0.2
done

# -----------------------------
# Create CSV file with header
# -----------------------------
echo "latency_sec" > "$OUTFILE"

# -----------------------------
# Main Benchmark Loop
# -----------------------------
# Send COUNT HTTP requests to the VIP
# and measure total response latency.
for i in $(seq 1 "$COUNT"); do

  # Execute curl inside Mininet host h1
  #
  # curl options:
  # --no-keepalive : disable persistent TCP connections
  # -s             : silent mode
  # -o /dev/null   : discard response body
  # -m 8           : maximum timeout of 8 seconds
  # -w '%{time_total}'
  #                : print total request latency
  LAT=$(sudo mnexec -a "$H1PID" \
    curl --no-keepalive -s -o /dev/null -m 8 \
    -w '%{time_total}' \
    "http://${VIP_HOST}:${VIP_PORT}/" 2>/dev/null)

  # Save curl exit status
  RC=$?

  # -----------------------------
  # Handle failed requests
  # -----------------------------
  if [[ $RC -ne 0 || -z "$LAT" ]]; then

    # Store NaN for failed requests
    echo "nan" >> "$OUTFILE"

    echo "[$i/$COUNT] failed"

  else

    # Store successful latency result
    echo "$LAT" >> "$OUTFILE"

    echo "[$i/$COUNT] $LAT"
  fi

  # Small delay between requests
  sleep 0.1
done

# -----------------------------
# Completion Message
# -----------------------------
echo "Saved $OUTFILE"

