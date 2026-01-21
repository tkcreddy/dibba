#!/bin/bash
# Containerd - Create Pod with Containers
# Usage: ./08_create_pods.sh [host_name] [namespace] [container_name] [image] [cpu_millicores] [memory]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"
CONTAINER_NAME="${3:-nginx}"
IMAGE="${4:-nginx:latest}"
CPU_MILLICORES="${5:-500}"
MEMORY="${6:-256Mi}"

echo "=== Dibba API - Create Pod ==="
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo "Container: $CONTAINER_NAME"
echo "Image: $IMAGE"
echo "CPU: ${CPU_MILLICORES}m"
echo "Memory: $MEMORY"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE",
    "containers": [
        {
            "name": "$CONTAINER_NAME",
            "image": "$IMAGE",
            "resources": {
                "cpu_millicores": $CPU_MILLICORES,
                "memory": "$MEMORY"
            }
        }
    ]
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/create-pods" \
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

