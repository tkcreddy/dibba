#!/bin/bash
# Containerd - Cleanup Tasks by Pod Prefix
# Usage: ./18_cleanup_tasks_by_pod_prefix.sh [host_name] [namespace] [pod_id] [prefer_grpc]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"
POD_ID="${3:-cd83c6a7ac0f47c6}"
PREFER_GRPC="${4:-true}"

echo "=== Dibba API - Cleanup Tasks by Pod Prefix ==="
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo "Pod ID: $POD_ID"
echo "Prefer gRPC: $PREFER_GRPC"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE",
    "pod_id": "$POD_ID",
    "prefer_grpc": $PREFER_GRPC
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/cleanup_tasks_by_pod_prefix/" \
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

