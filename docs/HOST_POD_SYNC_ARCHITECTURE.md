# Host/Pod Information Sync Architecture

## Overview

This document describes the architecture for automatically syncing host and pod information from worker nodes to the Redis database.

---

## Architecture Diagram

```
┌─────────────────┐
│  Worker Node 1  │
│                 │
│  ┌───────────┐  │
│  │ Beat Task │  │  Every 30s
│  │ (Periodic)│  │
│  └─────┬─────┘  │
│        │        │
│        ▼        │
│  ┌───────────┐  │
│  │ Collect   │  │
│  │ Host/Pod  │  │
│  │ Info      │  │
│  └─────┬─────┘  │
│        │        │
└────────┼────────┘
         │
         │ LPUSH
         ▼
┌─────────────────────┐
│  Redis Queue        │
│  host_pod_info_queue│
└──────────┬──────────┘
           │
           │ RPOP (Batch)
           ▼
┌─────────────────────┐
│  Consumer Service   │
│  (Standalone)       │
│                     │
│  - Batch Processing │
│  - Error Handling   │
│  - Retry Logic      │
└──────────┬──────────┘
           │
           │ Update
           ▼
┌─────────────────────┐
│  Redis Database     │
│  (HostPodStore)     │
│                     │
│  - Host Info        │
│  - Pod Info         │
│  - Indexes          │
└─────────────────────┘
```

---

## Components

### 1. Periodic Collection Task

**File**: `utils/celery/tasks/host_pod_sync_tasks.py`

**Task**: `collect_and_send_host_pod_info`

**Functionality**:
- Runs on each worker node every 30 seconds (via Celery Beat)
- Collects host system information (CPU, memory, IP)
- Collects pod information from containerd
- Packages and sends to Redis queue

**Data Collected**:
```json
{
  "hostname": "worker-01",
  "timestamp": "2024-12-15T10:30:00Z",
  "host_info": {
    "hostname": "worker-01",
    "system_info": {...},
    "usage_metrics": {...},
    "ip_address": "192.168.1.10"
  },
  "pod_info": {
    "hostname": "worker-01",
    "namespaces": ["production", "staging"],
    "pods": {
      "production": [...],
      "staging": [...]
    }
  }
}
```

---

### 2. Redis Queue

**Queue Name**: `host_pod_info_queue`

**Type**: Redis List (LPUSH/RPOP)

**Purpose**: 
- Decouple data collection from database updates
- Allow for batch processing
- Handle backpressure gracefully

**Queue Management**:
- Messages expire after 1 hour (prevents unbounded growth)
- FIFO processing (RPOP for consumer)
- Error queue for failed messages

---

### 3. Consumer Service

**File**: `utils/redis/host_pod_consumer.py`

**Class**: `HostPodConsumer`

**Functionality**:
- Listens to Redis queue continuously
- Processes messages in batches (configurable)
- Updates database using `HostPodStore`
- Handles errors gracefully
- Maintains statistics

**Features**:
- Batch processing (default: 10 messages)
- Configurable poll interval (default: 1 second)
- Error queue for failed messages
- Statistics tracking
- Graceful shutdown

---

## Configuration

### Celery Beat Schedule

**File**: `utils/celery/beat.py`

```python
'collect-host-pod-info-every-30-seconds': {
    'task': 'utils.celery.tasks.host_pod_sync_tasks.collect_and_send_host_pod_info',
    'schedule': 30.0,  # Every 30 seconds
    'options': {
        'queue': 'host_pod_sync',
        'exchange': secure_exchange,
        'routing_key': 'host_pod_sync',
        'delivery_mode': 2,
        'expires': 60,
    }
}
```

### Consumer Configuration

**File**: `utils/redis/host_pod_consumer.py`

```python
BATCH_SIZE = 10  # Messages per batch
POLL_INTERVAL = 1.0  # Seconds between polls
MAX_RETRIES = 3  # Retry attempts
```

---

## Deployment

### 1. Start Celery Beat (on each worker node)

```bash
celery -A utils.celery.celery_config beat --loglevel=info
```

This will schedule the `collect_and_send_host_pod_info` task to run every 30 seconds on each worker node.

### 2. Start Consumer Service (on control plane)

```bash
# Option 1: Using the script
./scripts/start_host_pod_consumer.sh

# Option 2: Direct Python
python -m utils.redis.host_pod_consumer_service

# Option 3: As a systemd service (see below)
```

### 3. Systemd Service (Optional)

Create `/etc/systemd/system/dibba-consumer.service`:

```ini
[Unit]
Description=Dibba Host/Pod Info Consumer
After=network.target redis.service

[Service]
Type=simple
User=dibba
WorkingDirectory=/opt/dibba
Environment="PATH=/opt/dibba/venv/bin"
ExecStart=/opt/dibba/venv/bin/python -m utils.redis.host_pod_consumer_service
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable dibba-consumer
sudo systemctl start dibba-consumer
```

---

## Data Flow

### 1. Collection Phase (Every 30s on Worker Nodes)

```
Worker Node
    │
    ├─> Collect System Info (CPU, Memory, IP)
    ├─> Collect Pod Info from Containerd
    └─> Package and Send to Queue
```

### 2. Queue Phase

```
Redis Queue (host_pod_info_queue)
    │
    ├─> Message 1: worker-01 info
    ├─> Message 2: worker-02 info
    ├─> Message 3: worker-01 info (next cycle)
    └─> ...
```

### 3. Processing Phase (Consumer)

```
Consumer Service
    │
    ├─> Poll Queue (RPOP batch)
    ├─> Process Each Message
    │   ├─> Update Host Info
    │   └─> Update Pod Info
    └─> Update Statistics
```

### 4. Database Update Phase

```
Redis Database
    │
    ├─> host:worker-01 (updated)
    ├─> pod:pod-123 (updated)
    ├─> Indexes (updated)
    └─> Application metadata (updated)
```

---

## Error Handling

### Collection Errors

- **Host info collection fails**: Log error, send partial data
- **Pod info collection fails**: Log error, send host info only
- **Queue send fails**: Retry with exponential backoff

### Processing Errors

- **Invalid message format**: Send to error queue
- **Database update fails**: Retry up to MAX_RETRIES
- **Persistent failures**: Send to error queue for analysis

### Error Queue

**Queue Name**: `host_pod_info_queue_errors`

**Purpose**: Store failed messages for analysis and debugging

**Retention**: 24 hours (auto-expire)

---

## Performance Considerations

### Batch Processing

- Process multiple messages in one operation
- Reduces Redis round-trips
- Improves throughput

### Queue Size Management

- Monitor queue size regularly
- Alert if queue grows beyond threshold
- Scale consumer if needed

### Database Updates

- Use Redis pipelines for batch updates
- Update indexes atomically
- Minimize write operations

---

## Monitoring

### Consumer Statistics

```python
from utils.redis.host_pod_consumer import HostPodConsumer

consumer = HostPodConsumer(redis_interface)
stats = consumer.get_stats()

# Returns:
# {
#     "processed": 150,
#     "errors": 2,
#     "queue_size": 5,
#     "error_queue_size": 1,
#     "last_processed": "2024-12-15T10:30:00Z",
#     "start_time": "2024-12-15T09:00:00Z"
# }
```

### Queue Monitoring

```python
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
queue_size = rd.redis_client.llen("host_pod_info_queue")
print(f"Queue size: {queue_size}")
```

---

## Troubleshooting

### Queue Growing Too Large

**Symptoms**: Queue size continuously increasing

**Solutions**:
1. Check if consumer is running
2. Check consumer logs for errors
3. Scale consumer (run multiple instances)
4. Increase batch size

### Messages Not Processing

**Symptoms**: Queue has messages but consumer not processing

**Solutions**:
1. Check consumer service status
2. Check Redis connectivity
3. Review error queue for patterns
4. Check consumer logs

### Stale Data

**Symptoms**: Database not updating

**Solutions**:
1. Verify Celery Beat is running on worker nodes
2. Check task execution logs
3. Verify queue is receiving messages
4. Check consumer processing logs

---

## Best Practices

1. **Run Consumer on Control Plane**: Keep consumer close to database
2. **Monitor Queue Size**: Set up alerts for queue growth
3. **Error Queue Analysis**: Regularly review error queue
4. **Consumer Scaling**: Run multiple consumers if needed
5. **Graceful Shutdown**: Always shutdown consumer gracefully

---

## Example Usage

### Start Consumer Service

```bash
# Development
python -m utils.redis.host_pod_consumer_service

# Production (with script)
./scripts/start_host_pod_consumer.sh

# Production (systemd)
sudo systemctl start dibba-consumer
```

### Check Queue Status

```python
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
queue_size = rd.redis_client.llen("host_pod_info_queue")
print(f"Messages in queue: {queue_size}")
```

### Get Consumer Statistics

```python
from utils.redis.host_pod_consumer import HostPodConsumer
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
consumer = HostPodConsumer(rd)
stats = consumer.get_stats()
print(f"Processed: {stats['processed']}, Errors: {stats['errors']}")
```

---

**Last Updated**: December 2024





