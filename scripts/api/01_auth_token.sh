#!/bin/bash
# Authentication - Get JWT Token
# Usage: ./01_auth_token.sh [username] [password]

source "$(dirname "$0")/config.sh"

USERNAME="${1:-$USERNAME}"
PASSWORD="${2:-$PASSWORD}"

echo "=== Dibba API - Authentication ==="
echo "Base URL: $BASE_URL"
echo "Username: $USERNAME"
echo ""

RESPONSE=$(curl -s -X POST "${BASE_URL}/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=${USERNAME}&password=${PASSWORD}")

# Check if jq is available
if command -v jq &> /dev/null; then
    echo "$RESPONSE" | jq '.'
    
    # Extract and save token
    TOKEN=$(echo "$RESPONSE" | jq -r '.data.access_token // empty')
    if [ -n "$TOKEN" ] && [ "$TOKEN" != "null" ]; then
        echo "$TOKEN" > "$TOKEN_FILE"
        echo -e "\n${GREEN}Token saved to: $TOKEN_FILE${NC}"
    fi
else
    echo "$RESPONSE"
fi

echo ""
echo "=== Response Status ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE_URL}/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=${USERNAME}&password=${PASSWORD}")

if [ "$HTTP_CODE" -eq 200 ]; then
    echo -e "${GREEN}Success (HTTP $HTTP_CODE)${NC}"
else
    echo -e "${RED}Failed (HTTP $HTTP_CODE)${NC}"
    exit 1
fi

