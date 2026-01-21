# Deployment Scheduler Usage Guide

## Overview

The Deployment Scheduler is a comprehensive system that:
1. Parses Kubernetes-like deployment YAML specifications
2. Queries Redis for available resources on worker nodes
3. Uses intelligent distribution algorithms to place replicas
4. Automatically creates AWS nodes if resources are insufficient
5. Creates pods using containerd_tasks

---

## Architecture

```
┌─────────────────┐
│  YAML Deployment │
│     Spec         │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  YAML Parser    │
│  - Parse spec   │
│  - Extract      │
│    resources    │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Redis Query    │
│  - Get hosts    │
│  - Calculate    │
│    available    │
│    resources    │
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│  Distribution   │
│  Algorithm      │
│  - cluster_     │
│    worker_      │
│    distribution │
│  - initial_load │
│    _distribution │
└────────┬─────────┘
         │
         ├─► Sufficient Resources?
         │   │
         │   ├─► YES ──► Create Pods
         │   │
         │   └─► NO ──► Create AWS Nodes
         │                └─► Then Create Pods
         │
         ▼
┌─────────────────┐
│  Pod Creation   │
│  - containerd_  │
│    tasks        │
│  - Distributed  │
│    across hosts │
└─────────────────┘
```

---

## API Endpoint

### POST `/scheduler/deploy/`

Schedule a deployment from Kubernetes-like YAML.

**Request Body**:
```json
{
  "yaml_content": "metadata:\n  name: my-app-deployment\n  ..."
}
```

**Response**:
```json
{
  "status": "success",
  "message": "Deployment scheduled successfully",
  "data": {
    "status": "success",
    "deployment": "my-app-deployment",
    "namespace": "default",
    "placement": {
      "0": [("my-app", 0)],
      "1": [("my-app", 1)]
    },
    "pods_created": [
      {
        "hostname": "worker-01",
        "replica": 0,
        "task_id": "abc123",
        "status": "submitted"
      }
    ]
  }
}
```

---

## YAML Format

The scheduler accepts Kubernetes-like deployment YAML:

```yaml
metadata:
  name: my-app-deployment
  labels:
    app: my-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app-container
        image: polinux/stress
        resources:
          requests:
            memory: "100Mi"  # Minimum memory
            cpu: "250m"      # Minimum CPU (250 millicores)
          limits:
            memory: "200Mi"  # Maximum memory
            cpu: "500m"      # Maximum CPU (500 millicores)
        command: ["stress"]
        args: ["--vm", "1", "--vm-bytes", "150M", "--vm-hang", "1"]
        ports:
        - containerPort: 80
```

### Resource Units

**CPU**:
- `250m` = 250 millicores = 0.25 cores
- `1` = 1 core = 1000 millicores
- `0.5` = 0.5 cores = 500 millicores

**Memory**:
- `100Mi` = 100 Mebibytes = 104,857,600 bytes
- `1Gi` = 1 Gibibyte = 1,073,741,824 bytes
- `512M` = 512 Megabytes = 512,000,000 bytes

---

## Resource Calculation

The scheduler calculates available resources from Redis:

1. **Query All Hosts**: Gets all online hosts from Redis
2. **Get System Info**: Extracts CPU count and total memory
3. **Get Usage Metrics**: Gets current CPU and memory usage percentages
4. **Calculate Available**: 
   - Available CPU = Total CPU × (1 - CPU Usage %)
   - Available Memory = Total Memory × (1 - Memory Usage %)
5. **Reserve Resources**: Subtracts resources already reserved by existing pods

---

## Distribution Algorithm

The scheduler uses `ClusterWorkerDistribution` which:

1. **Sorts Instances**: Orders by total resource requirements (descending)
2. **Finds Best Node**: For each replica, finds the node with:
   - Sufficient available resources
   - Minimum resource usage (to balance load)
3. **Places Replicas**: Assigns each replica to the best node

**Algorithm**:
- Greedy bin-packing approach
- Minimizes resource fragmentation
- Balances load across nodes

---

## AWS Node Creation

If resources are insufficient, the scheduler:

1. **Calculates Required Nodes**: Determines how many nodes are needed
2. **Submits AWS Task**: Uses `create_worker_nodes` Celery task
3. **Returns Pending Status**: Indicates nodes are being created

**Configuration** (from `config/config.json`):
```json
{
  "aws_config": {
    "aws_access_key_id": "...",
    "aws_secret_access_key": "...",
    "region": "us-east-1",
    "instance_type": "t3.medium",
    "ami_id": "ami-...",
    "key_name": "my-key",
    "security_group_ids": ["sg-..."],
    "subnet_id": "subnet-..."
  }
}
```

---

## Pod Creation

Once placement is determined, the scheduler:

1. **Prepares Container Specs**: Converts YAML containers to containerd format
2. **Submits Tasks**: Uses `create_pod_task` for each replica
3. **Routes to Hosts**: Sends tasks to specific host queues
4. **Tracks Results**: Returns task IDs for monitoring

**Container Spec Format**:
```python
{
  'image': 'polinux/stress',
  'name': 'my-app-container',
  'command': ['stress'],
  'args': ['--vm', '1', '--vm-bytes', '150M'],
  'resources': {
    'cpu_millicores': 500,
    'memory': '200Mi'
  }
}
```

---

## Example Usage

### Python

```python
from server.sched.scheduler import schedule_deployment_from_yaml

yaml_content = """
metadata:
  name: my-app-deployment
  labels:
    app: my-app
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: my-app-container
        image: polinux/stress
        resources:
          requests:
            memory: "100Mi"
            cpu: "250m"
          limits:
            memory: "200Mi"
            cpu: "500m"
"""

result = schedule_deployment_from_yaml(yaml_content)
print(result)
```

### cURL

```bash
curl -X POST "http://localhost:8000/scheduler/deploy/" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "yaml_content": "metadata:\n  name: my-app-deployment\n  ..."
  }'
```

### Python Requests

```python
import requests

url = "http://localhost:8000/scheduler/deploy/"
headers = {
    "Authorization": "Bearer YOUR_TOKEN",
    "Content-Type": "application/json"
}
data = {
    "yaml_content": yaml_content
}

response = requests.post(url, json=data, headers=headers)
print(response.json())
```

---

## Error Handling

The scheduler handles various error scenarios:

1. **Invalid YAML**: Returns parsing error
2. **No Hosts Available**: Creates AWS nodes
3. **Insufficient Resources**: Creates AWS nodes
4. **Pod Creation Failure**: Returns error for specific replicas
5. **AWS Node Creation Failure**: Returns error with details

**Error Response Format**:
```json
{
  "status": "error",
  "error": "Error message",
  "details": {...}
}
```

---

## Monitoring

### Check Deployment Status

Use the task IDs returned to monitor pod creation:

```python
from celery.result import AsyncResult

task_id = result['pods_created'][0]['task_id']
async_result = AsyncResult(task_id, app=celery_app)
status = async_result.status
result_data = async_result.result
```

### Check Host Resources

Query Redis directly:

```python
from utils.redis.host_pod_store import HostPodStore
from utils.redis.redis_interface import RedisInterface

store = HostPodStore(RedisInterface())
hosts = store.get_all_hosts()
for host in hosts:
    print(f"{host['hostname']}: {host.get('usage_metrics', {})}")
```

---

## Best Practices

1. **Resource Requests**: Always specify both `requests` and `limits`
2. **Replica Count**: Start with fewer replicas and scale up
3. **Monitoring**: Monitor task IDs to track pod creation
4. **Error Handling**: Check for `pending` status when AWS nodes are created
5. **Resource Planning**: Ensure sufficient capacity or AWS node creation capability

---

## Troubleshooting

### No Hosts Found

**Issue**: Scheduler reports no online hosts

**Solution**:
- Check Redis connectivity
- Verify host sync is running
- Check host status in Redis

### Insufficient Resources

**Issue**: Deployment fails with insufficient resources

**Solution**:
- Check AWS credentials
- Verify AWS configuration
- Wait for AWS nodes to be ready
- Retry deployment

### Pod Creation Fails

**Issue**: Pods fail to create

**Solution**:
- Check containerd is running on hosts
- Verify image exists and is pullable
- Check host queue connectivity
- Review Celery worker logs

---

## Future Enhancements

- [ ] Support for multiple containers per pod
- [ ] Resource aggregation across containers
- [ ] Pod affinity/anti-affinity rules
- [ ] Node affinity rules
- [ ] Automatic scaling based on load
- [ ] Rolling updates
- [ ] Health checks and readiness probes

---

**Last Updated**: December 2024




