#!/bin/bash
# Start Celery AWS Worker
#
# This script starts the Celery worker for AWS and scheduler tasks.
# AWS tasks and scheduler tasks run on dedicated queues.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
DIBBA_ENV="/opt/dibba/dibba.env"
APP_ENV="$APP_DIR/.venv/bin/activate"
LOG_FILE="/var/log/$APP_NAME/aws_worker.log"

# Function to cleanup on exit
cleanup() {
    echo "Shutting down AWS Worker..."
    if [ -n "$CELERY_PID" ]; then
        kill "$CELERY_PID" 2>/dev/null
        wait "$CELERY_PID" 2>/dev/null
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

# Start Celery AWS Worker
echo "Starting Celery AWS worker..."
celery -A utils.celery.aws_worker worker -l info >> "$LOG_FILE" 2>&1 &
CELERY_PID=$!

echo "Services started:"
echo "  - Celery AWS worker (PID: $CELERY_PID)"
echo ""
echo "Press Ctrl+C to stop all services"

# Wait for process
wait "$CELERY_PID"
