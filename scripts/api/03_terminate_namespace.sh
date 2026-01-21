#!/bin/bash
# AWS Management - Terminate All Instances in Namespace
# Usage: ./03_terminate_namespace.sh [namespace]

source "$(dirname "$0")/config.sh"

NAMESPACE="${1:-production}"

echo "=== Dibba API - Terminate Namespace ==="
echo "Namespace: $NAMESPACE"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "namespace": "$NAMESPACE"
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/terminate-namespace/" \
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

