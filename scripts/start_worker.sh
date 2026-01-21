#!/bin/bash
# Start Celery worker and host/pod sync task in parallel
#
# This script starts:
# 1. Celery worker for processing tasks
# 2. Host/pod sync task (runs every 30 seconds in background)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
DIBBA_ENV="/opt/dibba/dibba.env"
APP_ENV="$APP_DIR/.venv/bin/activate"
APP_MODE="utils.celery.tasks.host_pod_sync_standalone"
LOG_FILE="/var/log/$APP_NAME/worker_node.log"

# Function to cleanup background processes on exit
cleanup() {
    echo "Shutting down services..."
    if [ -n "$CELERY_PID" ]; then
        kill "$CELERY_PID" 2>/dev/null
    fi
    if [ -n "$SYNC_PID" ]; then
        kill "$SYNC_PID" 2>/dev/null
    fi
    exit 0
}

# Setup signal handlers
trap cleanup SIGINT SIGTERM
cd $APP_DIR || exit 1
source $DIBBA_ENV
source $APP_ENV
# Start Celery worker in background
echo "Starting Celery worker..."
celery -A utils.celery.worker_node worker -l info >> $LOG_FILE 2>&1 &
CELERY_PID=$!

# Wait a moment for Celery to start
sleep 5

# Start host/pod sync task in background
#echo "Starting host/pod sync task (runs every 30 seconds)..."
#python -m $APP_MOD >> $LOG_FILE 2>&1 &
#SYNC_PID=$!

echo "Services started:"
echo "  - Celery worker (PID: $CELERY_PID)"
#echo "  - Host/pod sync task (PID: $SYNC_PID)"
echo ""
echo "Press Ctrl+C to stop all services"

# Wait for both processes
#wait $CELERY_PID $SYNC_PID
wait $CELERY_PID