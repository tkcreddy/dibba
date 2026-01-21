#!/bin/bash
# Containerd - Terminate Pod by Pause Container ID
# Usage: ./12_terminate_pod_by_pause_cid.sh [host_name] [namespace] [pause_cid] [cni_network] [ifname]

source "$(dirname "$0")/config.sh"

HOST_NAME="${1:-worker-01}"
NAMESPACE="${2:-production}"
PAUSE_CID="${3:-abc123def456}"
CNI_NETWORK="${4:-calico}"
IFNAME="${5:-eth0}"

echo "=== Dibba API - Terminate Pod by Pause CID ==="
echo "Host Name: $HOST_NAME"
echo "Namespace: $NAMESPACE"
echo "Pause CID: $PAUSE_CID"
echo "CNI Network: $CNI_NETWORK"
echo "Interface: $IFNAME"
echo ""

REQUEST_BODY=$(cat <<EOF
{
    "host_name": "$HOST_NAME",
    "namespace": "$NAMESPACE",
    "pause_cid": "$PAUSE_CID",
    "cni_network": "$CNI_NETWORK",
    "ifname": "$IFNAME"
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/containerd/terminate_pod_by_pause_cid/" \
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

