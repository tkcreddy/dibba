#!/bin/bash
# Containerd - Prune Namespace Resources
# Usage: ./17_prune_namespace.sh [host_name] [namespace] [aggressive]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"
AGGRESSIVE="${3:-true}"

echo "=== Dibba API - Prune Namespace ==="
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo "Aggressive: $AGGRESSIVE"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE",
    "aggressive": $AGGRESSIVE
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/prune_namespace/" \
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

