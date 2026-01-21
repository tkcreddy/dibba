#!/bin/bash
# Containerd - Destroy All Pods in Namespace
# Usage: ./13_destroy_all_pods.sh [host_name] [namespace]
# WARNING: This is a destructive operation!

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"

echo -e "${YELLOW}=== WARNING: This will destroy ALL pods in the namespace! ===${NC}"
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo ""
read -p "Are you sure? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Operation cancelled."
    exit 0
fi

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE"
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/destroy_all_pods/" \
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

