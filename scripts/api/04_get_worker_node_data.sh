#!/bin/bash
# Worker Nodes - Get System Information
# Usage: ./04_get_worker_node_data.sh [host_name]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"

echo "=== Dibba API - Get Worker Node Data ==="
echo "Host Name: $HOST_NAME"
echo ""

get_token

RESPONSE=$(curl -s -X GET "${BASE_URL}/get_worker_node_data/?host_name=${HOST_NAME}" \
    -H "Authorization: Bearer ${TOKEN}")

if command -v jq &> /dev/null; then
    echo "$RESPONSE" | jq '.'
    
    # Extract task_id
    TASK_ID=$(echo "$RESPONSE" | jq -r '.data.task_id // empty')
    if [ -n "$TASK_ID" ] && [ "$TASK_ID" != "null" ]; then
        echo -e "\n${GREEN}Task ID: $TASK_ID${NC}"
        echo "Check status with: ./05_get_task_status.sh $TASK_ID"
    fi
else
    echo "$RESPONSE"
fi

