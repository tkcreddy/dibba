# Host Worker with Parallel Sync Task

## Overview

The `host_worker.sh` script has been updated to run both the Celery worker and the host/pod sync task in parallel.

---

## Updated Script

**File**: `scripts/host_worker.sh`

### What It Does

1. **Starts Celery Worker**: Processes Celery tasks as before
2. **Starts Sync Task**: Runs host/pod sync every 30 seconds in background
3. **Graceful Shutdown**: Both processes stop together on Ctrl+C

---

## Architecture

```
host_worker.sh
    │
    ├─> Celery Worker (Background)
    │   └─> Processes Celery tasks
    │
    └─> Sync Task (Background)
        └─> Collects host/pod info every 30s
            └─> Sends to Redis queue
```

---

## Usage

### Start Both Services

```bash
./scripts/host_worker.sh
```

Or from `/opt/dibba/scripts/`:

```bash
/opt/dibba/scripts/host_worker.sh
```

### What Happens

1. Script activates virtual environment (if present)
2. Starts Celery worker in background
3. Starts sync task in background
4. Waits for both processes
5. On Ctrl+C, gracefully shuts down both

### Output

```
Starting Celery worker...
Starting host/pod sync task (runs every 30 seconds)...
Services started:
  - Celery worker (PID: 12345)
  - Host/pod sync task (PID: 12346)

Press Ctrl+C to stop all services
```

---

## Standalone Sync Script

**File**: `utils/celery/tasks/host_pod_sync_standalone.py`

### Run Independently

If you want to run the sync task separately:

```bash
# Default (30 second interval)
python -m utils.celery.tasks.host_pod_sync_standalone

# Custom interval
python -m utils.celery.tasks.host_pod_sync_standalone --interval 60

# Custom socket and namespace
python -m utils.celery.tasks.host_pod_sync_standalone \
    --socket "unix:///run/containerd/containerd.sock" \
    --namespace "k8s.io" \
    --interval 30
```

### Options

- `--socket`: Containerd socket path
- `--namespace`: Containerd namespace
- `--interval`: Sync interval in seconds (default: 30)

---

## Benefits

### ✅ Parallel Execution
- Both services run simultaneously
- No interference between them
- Independent error handling

### ✅ Single Script
- One command to start everything
- Easier deployment
- Unified logging

### ✅ Graceful Shutdown
- Both processes stop together
- Clean exit on signals
- No orphaned processes

---

## Alternative: Celery Beat

If you prefer using Celery Beat instead of the standalone script:

1. **Remove sync task from `host_worker.sh`**
2. **Start Celery Beat separately**:

```bash
celery -A utils.celery.celery_config beat --loglevel=info
```

The sync task is already configured in `utils/celery/beat.py` to run every 30 seconds.

---

## Comparison

### Option 1: Parallel in `host_worker.sh` (Current)
- ✅ Single script
- ✅ No Celery Beat needed
- ✅ Direct control
- ❌ Requires Python script running

### Option 2: Celery Beat
- ✅ Uses Celery infrastructure
- ✅ Centralized scheduling
- ❌ Requires separate Beat process
- ❌ More complex setup

---

## Troubleshooting

### Sync Task Not Running

**Check**:
1. Is Python script running? (`ps aux | grep host_pod_sync_standalone`)
2. Check logs for errors
3. Verify Redis connectivity

### Celery Worker Not Running

**Check**:
1. Is Celery process running? (`ps aux | grep celery`)
2. Check Celery logs
3. Verify Redis broker connectivity

### Both Services Stopped

**Check**:
1. Check script output for errors
2. Verify virtual environment is activated
3. Check file permissions

---

## Systemd Service (Optional)

Create `/etc/systemd/system/dibba-host-worker.service`:

```ini
[Unit]
Description=Dibba Host Worker with Sync
After=network.target redis.service

[Service]
Type=simple
User=dibba
WorkingDirectory=/opt/dibba
Environment="PATH=/opt/dibba/venv/bin"
ExecStart=/opt/dibba/scripts/host_worker.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable dibba-host-worker
sudo systemctl start dibba-host-worker
```

---

## Monitoring

### Check Running Processes

```bash
# Check Celery worker
ps aux | grep "celery.*worker_node"

# Check sync task
ps aux | grep "host_pod_sync_standalone"

# Check both
ps aux | grep -E "(celery|host_pod_sync)"
```

### Check Logs

The sync task logs to the standard logging system. Check your log files or journal:

```bash
# If using systemd
journalctl -u dibba-host-worker -f

# If using file logging
tail -f /var/log/dibba/host_worker.log
```

---

**Last Updated**: December 2024





