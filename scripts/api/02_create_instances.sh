#!/bin/bash
# AWS Management - Create EC2 Instances
# Usage: ./02_create_instances.sh [instance_type] [ami_id] [key_name] [security_group_ids] [subnet_id] [namespace] [min_count] [max_count]

source "$(dirname "$0")/config.sh"

INSTANCE_TYPE="${1:-t2.micro}"
AMI_ID="${2:-ami-0c55b159cbfafe1f0}"
KEY_NAME="${3:-my-key-pair}"
SECURITY_GROUP_IDS="${4:-sg-12345678}"
SUBNET_ID="${5:-subnet-12345678}"
NAMESPACE="${6:-production}"
MIN_COUNT="${7:-1}"
MAX_COUNT="${8:-3}"

echo "=== Dibba API - Create EC2 Instances ==="
echo "Instance Type: $INSTANCE_TYPE"
echo "AMI ID: $AMI_ID"
echo "Key Name: $KEY_NAME"
echo "Security Groups: $SECURITY_GROUP_IDS"
echo "Subnet ID: $SUBNET_ID"
echo "Namespace: $NAMESPACE"
echo "Count: $MIN_COUNT - $MAX_COUNT"
echo ""

# Convert security group IDs to array format
IFS=',' read -ra SG_ARRAY <<< "$SECURITY_GROUP_IDS"
SG_JSON="["
for i in "${SG_ARRAY[@]}"; do
    SG_JSON+="\"$i\","
done
SG_JSON="${SG_JSON%,}]"

REQUEST_BODY=$(cat <<EOF
{
    "instance_type": "$INSTANCE_TYPE",
    "ami_id": "$AMI_ID",
    "key_name": "$KEY_NAME",
    "security_group_ids": $SG_JSON,
    "subnet_id": "$SUBNET_ID",
    "namespace": "$NAMESPACE",
    "min_count": $MIN_COUNT,
    "max_count": $MAX_COUNT
}
EOF
)

get_token

RESPONSE=$(curl -s -X POST "${BASE_URL}/create-instances/" \
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

