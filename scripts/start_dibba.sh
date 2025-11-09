#!/bin/bash
# ===========================================
# start_server.sh — Start Dibba server
# ===========================================
APP_NAME="dibba"
APP_DIR="/opt/$APP_NAME"
APP_ENV="$APP_DIR/.venv/bin/activate"
APP_MOD="server.main_api"
LOG_FILE="/var/log/$APP_NAME/server.log"

echo "Starting Python server..."

cd $APP_DIR || exit 1
source $APP_ENV

nohup python3.13 -m $APP_MOD >> $LOG_FILE 2>&1 &

PID=$!
echo $PID > /var/run/$APP_NAME.pid
echo "Server started with PID $PID"

