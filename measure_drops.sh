#!/bin/bash

# Usage: ./measure_drops.sh [requests]
# Default = 50 requests

REQ=${1:-50}
FAIL=0

echo "Running packet drop test with $REQ requests..."

for i in $(seq 1 $REQ); do
    curl --no-keepalive -m 3 -s -o /dev/null http://10.0.0.100:8000
    if [ $? -ne 0 ]; then
        FAIL=$((FAIL+1))
    fi
done

SUCCESS=$((REQ-FAIL))

echo "----------------------------------"
echo "Total Requests: $REQ"
echo "Successful: $SUCCESS"
echo "Failed (Drops): $FAIL"

PERCENT=$(echo "scale=2; ($FAIL/$REQ)*100" | bc)
echo "Drop Rate: $PERCENT %"