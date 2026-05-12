#!/bin/bash

# Usage: ./measure_drops.sh [requests]
# Packet Drop Measurement Script
# This script sends multiple HTTP requests to the SDN Virtual IP
# and counts how many requests fail.
#
# Usage:
#   ./measure_drops.sh [number_of_requests]
#
# Example:
#   ./measure_drops.sh 20
#
# If no argument is provided, the script defaults to 20 requests.


# Number of requests to send
# Uses the first command-line argument if provided,
# otherwise defaults to 20.
REQ=${1:-20}
# Counter for failed requests
FAIL=0

echo "Running packet drop test with $REQ requests..."

# Loop through the specified number of request
for i in $(seq 1 $REQ); do
    # Send an HTTP request to the SDN Virtual IP
    #
    # Options:
    # --no-keepalive : disables connection reuse
    # -m 3           : timeout after 3 seconds
    # -s             : silent mode (no progress output)
    # -o /dev/null   : discard response body
    curl --no-keepalive -m 3 -s -o /dev/null http://10.0.0.100:8000
    # Check the exit status of curl
    # Exit code 0 means success
    # Any non-zero value indicates failure
    if [ $? -ne 0 ]; then
        FAIL=$((FAIL+1))
    fi
done
# Calculate successful requests
SUCCESS=$((REQ-FAIL))

echo "----------------------------------"
echo "Total Requests: $REQ"
echo "Successful: $SUCCESS"
echo "Failed (Drops): $FAIL"

# Calculate packet drop percentage using bc for floating point math
PERCENT=$(echo "scale=2; ($FAIL/$REQ)*100" | bc)
echo "Drop Rate: $PERCENT %"
