#!/bin/bash
# Task Management - Get Task Status
# Usage: ./05_get_task_status.sh [task_id]

source "$(dirname "$0")/config.sh"

TASK_ID="${1}"

if [ -z "$TASK_ID" ]; then
    echo -e "${RED}Error: Task ID is required${NC}"
    echo "Usage: $0 <task_id>"
    exit 1
fi

echo "=== Dibba API - Get Task Status ==="
echo "Task ID: $TASK_ID"
echo ""

get_token

RESPONSE=$(curl -s -X GET "${BASE_URL}/task/${TASK_ID}" \
    -H "Authorization: Bearer ${TOKEN}")

if command -v jq &> /dev/null; then
    echo "$RESPONSE" | jq '.'
    
    # Extract status
    STATUS=$(echo "$RESPONSE" | jq -r '.status // empty')
    if [ -n "$STATUS" ]; then
        echo -e "\n${GREEN}Status: $STATUS${NC}"
        
        if [ "$STATUS" = "SUCCESS" ]; then
            echo -e "${GREEN}Task completed successfully!${NC}"
        elif [ "$STATUS" = "FAILURE" ]; then
            echo -e "${RED}Task failed!${NC}"
        elif [ "$STATUS" = "PROGRESS" ]; then
            echo -e "${YELLOW}Task in progress...${NC}"
        fi
    fi
else
    echo "$RESPONSE"
fi

