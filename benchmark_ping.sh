#!/usr/bin/env bash

# -----------------------------------------
# Enable strict Bash error handling
# -----------------------------------------
# -e : exit immediately if a command fails
# -u : treat unset variables as errors
# -o pipefail : fail if any command in a pipe fails
set -euo pipefail

# -----------------------------------------
# Script Arguments
# -----------------------------------------
# $1 -> Virtual IP address (VIP)
# $2 -> Number of ping packets
#
# Default values:
# VIP   = 10.0.0.100
# COUNT = 20
VIP="${1:-10.0.0.100}"
COUNT="${2:-20}"

# -----------------------------------------
# Find PID of Mininet host h1
# -----------------------------------------
# Mininet hosts run as separate namespaces/processes.
# We locate the process ID of h1 so we can execute
# commands inside that namespace using mnexec.
H1PID=$(ps -eo pid,cmd | grep "mininet:h1" | grep -v grep | head -n1 | awk '{print $1}')

# -----------------------------------------
# Verify h1 exists
# -----------------------------------------
if [[ -z "${H1PID}" ]]; then
  echo "ERROR: h1 process not found."
  exit 1
fi

# -----------------------------------------
# Execute ping inside Mininet host h1
# -----------------------------------------
# ping options:
# -c COUNT : send COUNT ICMP echo requests
#
# This tests connectivity between h1 and
# the virtual IP address (VIP) used by
# the SDN load balancer.
sudo mnexec -a "$H1PID" ping -c "$COUNT" "$VIP"
