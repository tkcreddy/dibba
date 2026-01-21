#!/bin/bash
# Containerd - List All Namespaces and Pods
# Usage: ./09_list_namespaces_and_pods.sh [host_name]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"

echo "=== Dibba API - List Namespaces and Pods ==="
echo "Host Name: $HOST_NAME"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME"
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/list_namespaces_and_pods/" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$REQUEST_BODY")

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

