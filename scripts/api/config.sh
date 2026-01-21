#!/bin/bash
# Configuration file for API curl scripts
# Source this file in other scripts: source config.sh

# Base URL
BASE_URL="${BASE_URL:-http://localhost:8000}"

# Credentials (set these or pass as environment variables)
USERNAME="${USERNAME:user}"
PASSWORD="${PASSWORD:password}"

# Token file location
TOKEN_FILE="${TOKEN_FILE:-/tmp/dibba_token.txt}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to get or refresh token
get_token() {
    if [ -f "$TOKEN_FILE" ]; then
        # Check if token is still valid (simple check - token file age)
        TOKEN_AGE=$(($(date +%s) - $(stat -f %m "$TOKEN_FILE" 2>/dev/null || echo 0)))
        # Tokens expire in 30 minutes (1800 seconds), refresh if older than 25 minutes
        if [ $TOKEN_AGE -lt 1500 ]; then
            TOKEN=$(cat "$TOKEN_FILE")
            return 0
        fi
    fi
    
    echo "Getting new token..."
    RESPONSE=$(curl -s -X POST "${BASE_URL}/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${USERNAME}&password=${PASSWORD}")
    
    TOKEN=$(echo "$RESPONSE" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
    
    if [ -z "$TOKEN" ]; then
        echo -e "${RED}Error: Failed to get token${NC}"
        echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
        exit 1
    fi
    
    echo "$TOKEN" > "$TOKEN_FILE"
    echo -e "${GREEN}Token obtained successfully${NC}"
}

# Function to make authenticated request
make_request() {
    local METHOD=$1
    local ENDPOINT=$2
    local DATA=$3
    
    get_token
    
    if [ -z "$DATA" ]; then
        curl -s -X "$METHOD" "${BASE_URL}${ENDPOINT}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" | jq '.'
    else
        curl -s -X "$METHOD" "${BASE_URL}${ENDPOINT}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$DATA" | jq '.'
    fi
}

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo -e "${YELLOW}Warning: jq is not installed. JSON output will not be formatted.${NC}"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
fi

