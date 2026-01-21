#!/bin/bash
# Start Celery Flower Monitoring
#
# This script starts the Celery Flower monitoring tool which provides
# a web-based tool for monitoring and administrating Celery clusters.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
DIBBA_ENV="/opt/dibba/dibba.env"
APP_ENV="$APP_DIR/.venv/bin/activate"
LOG_FILE="/var/log/$APP_NAME/flower.log"
FLOWER_DB="/var/log/dibba/flower_db"

# Function to cleanup on exit
cleanup() {
    echo "Shutting down Celery Flower..."
    if [ -n "$FLOWER_PID" ]; then
        kill "$FLOWER_PID" 2>/dev/null
        wait "$FLOWER_PID" 2>/dev/null
    fi
    exit 0
}

# Setup signal handlers
trap cleanup SIGINT SIGTERM

# Change to project directory
cd "$APP_DIR" || exit 1

# Source environment files if they exist
[ -f "$DIBBA_ENV" ] && source "$DIBBA_ENV"
[ -f "$APP_ENV" ] && source "$APP_ENV"

# Create log directory if it doesn't exist
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$FLOWER_DB")"

# Import flower SSL patch to fix Redis SSL connections
# This patches Redis connections before Flower starts
python3 -c "import sys; sys.path.insert(0, '$APP_DIR'); from utils.flower.flower_ssl_patch import patch_redis_ssl; patch_redis_ssl()" 2>/dev/null || true

# Import flower config to set up SSL environment variables
# This ensures SSL certificate verification is properly configured
python3 -c "import sys; sys.path.insert(0, '$APP_DIR'); from utils.flower.flower_config import *" 2>/dev/null || true

# Start Celery Flower
echo "Starting Celery Flower monitoring..."
echo "Log file: $LOG_FILE"
echo "Flower DB: $FLOWER_DB"
echo "Access at: http://localhost:5555/flower"
celery -A utils.celery.celery_app flower \
    --port=5555 \
    --url_prefix=flower \
    --persistent=True \
    --db="$FLOWER_DB" \
    --state_save_interval=10000 \
    >> "$LOG_FILE" 2>&1 &
FLOWER_PID=$!

echo "Celery Flower started (PID: $FLOWER_PID)"
echo "Press Ctrl+C to stop"

# Wait for process
wait "$FLOWER_PID"