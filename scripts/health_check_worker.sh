#!/bin/bash
# Start Celery Health Check Worker
#
# This script starts a dedicated Celery worker for health check tasks.
# Health check tasks run on a separate queue/node, isolated from scheduler and AWS tasks.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
DIBBA_ENV="/opt/dibba/dibba.env"
APP_ENV="$APP_DIR/.venv/bin/activate"
LOG_FILE="/var/log/$APP_NAME/health_check_worker.log"

# Function to cleanup on exit
cleanup() {
    echo "Shutting down Health Check Worker..."
    if [ -n "$WORKER_PID" ]; then
        kill "$WORKER_PID" 2>/dev/null
        wait "$WORKER_PID" 2>/dev/null
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

# Start Celery Health Check Worker
echo "Starting Celery Health Check Worker..."
echo "Log file: $LOG_FILE"
celery -A utils.celery.health_check_worker worker -l info >> "$LOG_FILE" 2>&1 &
WORKER_PID=$!

echo "Health Check Worker started (PID: $WORKER_PID)"
echo "Press Ctrl+C to stop"

# Wait for process
wait "$WORKER_PID"
