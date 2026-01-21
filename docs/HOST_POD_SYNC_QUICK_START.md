# Host/Pod Sync - Quick Start Guide

## Overview

This system automatically syncs host and pod information from worker nodes to Redis every 30 seconds.

---

## Architecture

```
Worker Nodes (Every 30s) → Redis Queue → Consumer Service → Redis Database
```

---

## Setup

### 1. Start Celery Beat (on each worker node)

Celery Beat will automatically schedule the collection task every 30 seconds.

```bash
celery -A utils.celery.celery_config beat --loglevel=info
```

**Note**: The task is already configured in `utils/celery/beat.py`

### 2. Start Consumer Service (on control plane)

```bash
# Option 1: Using the script
./scripts/start_host_pod_consumer.sh

# Option 2: Direct Python
python -m utils.redis.host_pod_consumer_service
```

---

## How It Works

### Collection Phase (Worker Nodes)

Every 30 seconds, each worker node:
1. Collects system information (CPU, memory, IP)
2. Collects pod information from containerd
3. Sends to Redis queue: `host_pod_info_queue`

### Processing Phase (Consumer)

The consumer service:
1. Polls the queue every 1 second
2. Processes messages in batches (10 at a time)
3. Updates Redis database using `HostPodStore`
4. Handles errors gracefully

---

## Monitoring

### Check Queue Size

```python
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
queue_size = rd.redis_client.llen("host_pod_info_queue")
print(f"Messages in queue: {queue_size}")
```

### Check Consumer Stats

```python
from utils.redis.host_pod_consumer import HostPodConsumer
from utils.redis.redis_interface import RedisInterface

rd = RedisInterface()
consumer = HostPodConsumer(rd)
stats = consumer.get_stats()
print(stats)
```

---

## Troubleshooting

### Queue Growing Too Large

- Check if consumer is running
- Check consumer logs
- Scale consumer if needed

### No Data in Database

- Verify Celery Beat is running
- Check task execution logs
- Verify queue has messages
- Check consumer logs

---

**See `HOST_POD_SYNC_ARCHITECTURE.md` for detailed documentation.**





