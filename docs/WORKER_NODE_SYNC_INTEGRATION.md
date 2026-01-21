# Worker Node Sync Integration

## Overview

The `worker_node.py` has been updated to automatically run the host/pod sync task in a separate thread alongside the Celery worker. This eliminates the need for Celery Beat or separate processes.

---

## Architecture

```
Celery Worker Process
    │
    ├─> Main Thread: Celery Worker
    │   ├─> Processes tasks from queue
    │   ├─> worker_node_tasks (available)
    │   └─> containerd_tasks (available)
    │
    └─> Background Thread: Host/Pod Sync
        └─> Runs every 30 seconds
            └─> Collects host/pod info
            └─> Sends to Redis queue
```

---

## Implementation Details

### File: `utils/celery/worker_node.py`

**Key Features**:

1. **Task Module Inclusion**:
   ```python
   celery_app.autodiscover_tasks(['utils.celery.tasks.worker_node_tasks'])
   celery_app.conf.include = [
       "utils.celery.tasks.containerd_tasks",
       "utils.celery.tasks.host_pod_sync_tasks"
   ]
   ```

2. **Automatic Thread Startup**:
   - Uses Celery worker signals (`worker_process_init`)
   - Automatically starts sync thread when worker starts
   - Automatically stops sync thread when worker shuts down

3. **Thread Management**:
   - Daemon thread (doesn't prevent shutdown)
   - Graceful shutdown with timeout
   - Error handling and logging

---

## How It Works

### 1. Worker Startup

When you start the Celery worker:

```bash
celery -A utils.celery.worker_node worker -l info
```

**What happens**:
1. Celery worker process starts
2. `worker_process_init` signal fires
3. Sync thread automatically starts
4. Both run in parallel

### 2. Sync Thread Execution

The sync thread:
- Runs in a continuous loop
- Executes sync every 30 seconds
- Calls `collect_and_send_host_pod_info()` directly
- Logs results and errors
- Handles shutdown gracefully

### 3. Worker Shutdown

When the worker shuts down:
1. `worker_process_shutdown` signal fires
2. Sync thread stops gracefully
3. Both processes exit cleanly

---

## Available Tasks

### ✅ worker_node_tasks
- `get_worker_node_info()` - Get system information
- `get_host_ip()` - Get host IP address
- `get_usage()` - Get system usage metrics

### ✅ containerd_tasks
- All containerd-related tasks for pod/container management

### ✅ host_pod_sync_tasks
- `collect_and_send_host_pod_info()` - Collect and sync host/pod info
- Available as Celery task (can be called via Beat or directly)
- Also runs automatically in background thread

---

## Configuration

### Sync Interval

Default: **30 seconds**

To change the interval, modify `SYNC_INTERVAL` in `worker_node.py`:

```python
SYNC_INTERVAL = 30.0  # seconds
```

### Containerd Socket

Default: `unix:///run/containerd/containerd.sock`

Can be customized when starting the thread (currently uses defaults).

### Namespace

Default: `k8s.io`

Can be customized when starting the thread (currently uses defaults).

---

## Usage

### Start Worker (Sync Included)

```bash
celery -A utils.celery.worker_node worker -l info
```

**That's it!** The sync thread starts automatically.

### Verify Sync Thread

Check logs for:
```
Worker process initialized, starting host/pod sync thread...
Started host/pod sync thread (PID: 12345)
Starting host/pod sync thread (interval=30.0s, socket=default, namespace=default)
```

### Check Sync Activity

Look for periodic log messages:
```
Sync cycle #1 completed: hostname=worker-01, pods=5, queue_size=10
Sync cycle #2 completed: hostname=worker-01, pods=5, queue_size=11
```

---

## Benefits

### ✅ Integrated
- No separate process needed
- No Celery Beat required
- Single command to start

### ✅ Automatic
- Starts with worker
- Stops with worker
- No manual intervention

### ✅ Efficient
- Runs in background thread
- Doesn't block worker tasks
- Minimal resource usage

### ✅ Reliable
- Graceful shutdown
- Error handling
- Comprehensive logging

---

## Comparison with Other Approaches

### Option 1: Thread in worker_node.py (Current) ✅
- ✅ Integrated with worker
- ✅ Automatic startup/shutdown
- ✅ No additional processes
- ✅ Simple deployment

### Option 2: Celery Beat
- ❌ Requires separate Beat process
- ❌ More complex setup
- ✅ Centralized scheduling
- ✅ Uses Celery infrastructure

### Option 3: Separate Script
- ❌ Requires separate process
- ❌ Manual management
- ✅ Independent control
- ✅ Can run standalone

---

## Troubleshooting

### Sync Thread Not Starting

**Check**:
1. Verify worker logs for initialization message
2. Check for errors in thread startup
3. Verify `host_pod_sync_tasks` module is importable

**Solution**:
```python
# In worker_node.py, verify the import works
from utils.celery.tasks.host_pod_sync_tasks import collect_and_send_host_pod_info
```

### Sync Thread Not Running

**Check**:
1. Look for sync cycle log messages
2. Check for thread errors
3. Verify Redis connectivity

**Solution**:
- Check Redis connection
- Verify queue name is correct
- Check thread is alive: `threading.enumerate()`

### Errors in Sync Cycle

**Check**:
1. Look for error messages in logs
2. Verify containerd socket is accessible
3. Check system info collection

**Solution**:
- Verify containerd is running
- Check socket permissions
- Check system info collection functions

---

## Thread Safety

### ✅ Safe Operations
- Redis queue operations (LPUSH)
- System info collection
- Pod info collection
- Logging

### ✅ Isolation
- Sync thread runs independently
- Doesn't interfere with Celery tasks
- Uses separate error handling

---

## Monitoring

### Check Thread Status

```python
import threading

# List all threads
for thread in threading.enumerate():
    if thread.name == "host-pod-sync":
        print(f"Sync thread: {thread.is_alive()}")
```

### Check Sync Activity

Monitor logs for:
- Sync cycle completion messages
- Error messages
- Queue size updates

### Check Queue Size

```python
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
queue_size = rd.redis_client.llen("host_pod_info_queue")
print(f"Queue size: {queue_size}")
```

---

## Code Structure

```
utils/celery/worker_node.py
    │
    ├─> Configuration
    │   ├─> Task queues
    │   ├─> Task modules
    │   └─> Sync interval
    │
    ├─> Thread Functions
    │   ├─> _run_sync_loop() - Main loop
    │   ├─> start_sync_thread() - Start thread
    │   └─> stop_sync_thread() - Stop thread
    │
    └─> Celery Signals
        ├─> worker_process_init - Start sync
        └─> worker_process_shutdown - Stop sync
```

---

## Example Log Output

```
[INFO] Worker node hostname: worker-01
[INFO] Worker process initialized, starting host/pod sync thread...
[INFO] Started host/pod sync thread (PID: 12345)
[INFO] Starting host/pod sync thread (interval=30.0s, socket=default, namespace=default)
[INFO] Sync cycle #1 completed: hostname=worker-01, pods=5, queue_size=10
[INFO] Sync cycle #2 completed: hostname=worker-01, pods=5, queue_size=11
...
[INFO] Worker process shutting down, stopping host/pod sync thread...
[INFO] Stopping host/pod sync thread...
[INFO] Sync thread stopped successfully
[INFO] Host/pod sync thread stopped after 100 cycles
```

---

**Last Updated**: December 2024





