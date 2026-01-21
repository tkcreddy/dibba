#!/bin/bash
# Containerd - Get Container Information
# Usage: ./15_get_container_info.sh [host_name] [namespace] [container_id]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"
CONTAINER_ID="${3:-container-id-123}"

echo "=== Dibba API - Get Container Info ==="
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo "Container ID: $CONTAINER_ID"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE",
    "cid": "$CONTAINER_ID"
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/get_container_info/" \
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

