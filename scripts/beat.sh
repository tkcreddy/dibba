#!/bin/bash
# Start Celery Beat scheduler
#
# This script starts the Celery Beat scheduler which handles periodic task scheduling
# for health checks, deployment recovery, and other scheduled tasks.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
DIBBA_ENV="/opt/dibba/dibba.env"
APP_ENV="$APP_DIR/.venv/bin/activate"
LOG_FILE="/var/log/$APP_NAME/beat.log"

# Function to cleanup on exit
cleanup() {
    echo "Shutting down Celery Beat..."
    if [ -n "$BEAT_PID" ]; then
        kill "$BEAT_PID" 2>/dev/null
        wait "$BEAT_PID" 2>/dev/null
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

# Start Celery Beat
echo "Starting Celery Beat scheduler..."
echo "Log file: $LOG_FILE"
celery -A utils.celery.beat beat --loglevel=info >> "$LOG_FILE" 2>&1 &
BEAT_PID=$!

echo "Celery Beat started (PID: $BEAT_PID)"
echo "Press Ctrl+C to stop"

# Wait for process
wait "$BEAT_PID"
