#!/bin/bash
# Start the host/pod information consumer service
#
# This script starts the consumer service that processes messages from
# the Redis queue and updates the database.
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
SCRIPT_DIR="/opt/dibba/scripts"
APP_MODE="utils.redis.host_pod_consumer_service"
cd "$APP_DIR" || exit 1
APP_ENV="$APP_DIR/.venv/bin/activate"
LOG_FILE="/var/log/$APP_NAME/host_pod_consumer.log"
# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source $APP_ENV
fi


# Setup signal handlers
trap cleanup SIGINT SIGTERM
cd $APP_DIR || exit 1
source $APP_ENV
# Start Celery worker in background
echo "Starting Celery AWS worker..."
python -m $APP_MODE >> $LOG_FILE 2>&1 &
APP_PID=$!

# Wait a moment for Celery to start
#sleep 5

# Start host/pod sync task in background
#echo "Starting host/pod sync task (runs every 30 seconds)..."
#python -m $APP_MOD >> $LOG_FILE 2>&1 &
#SYNC_PID=$!

echo "Services started:"
echo "  - Host POD Consumer worker (PID: $APP_PID)"
echo ""
echo "Press Ctrl+C to stop all services"