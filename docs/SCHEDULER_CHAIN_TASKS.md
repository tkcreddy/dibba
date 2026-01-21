# Scheduler Chain Tasks

## Overview

The deployment scheduler has been refactored to use Celery chains, breaking the scheduling process into discrete, chainable tasks that can be executed asynchronously and monitored independently.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              API Endpoint                                   │
│         POST /scheduler/deploy/                             │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│         Celery Chain                                         │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Task 1: evaluate_deployment_requirements_task     │  │
│  │  - Parse YAML                                        │  │
│  │  - Query Redis for resources                         │  │
│  │  - Calculate placement                               │  │
│  │  - Determine if AWS nodes needed                     │  │
│  └───────────────────────┬──────────────────────────────┘  │
│                            │                                  │
│                            ▼                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Task 2: create_aws_nodes_if_needed_task             │  │
│  │  - Check if AWS nodes needed                         │  │
│  │  - Create AWS nodes if needed                        │  │
│  │  - Return updated evaluation result                   │  │
│  └───────────────────────┬──────────────────────────────┘  │
│                            │                                  │
│                            ▼                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Task 3: place_and_create_pods_task                  │  │
│  │  - Get all hosts (existing + new)                    │  │
│  │  - Recalculate placement if needed                   │  │
│  │  - Create pods using containerd_tasks                │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## Chain Tasks

### 1. `evaluate_deployment_requirements_task`

**Purpose**: First task in the chain that evaluates deployment requirements.

**Input**: `yaml_content` (string)

**Output**: Dictionary with:
```python
{
    'status': 'evaluated' | 'needs_aws_nodes' | 'error',
    'deployment': {
        'name': str,
        'namespace': str,
        'app_label': str,
        'replicas': int,
        'containers': List[Dict],
        'resource_requirements': {
            'cpu_millicores': int,
            'memory_mb': float,
            'cpu_cores': float,
            'memory_bytes': int,
        }
    },
    'available_hosts': List[Dict],
    'needs_aws_nodes': bool,
    'required_nodes': int,
    'placement': Dict[int, List[Tuple]] | None,
}
```

**What it does**:
1. Parses deployment YAML
2. Queries Redis for available resources
3. Calculates placement using `ClusterWorkerDistribution`
4. Determines if AWS nodes are needed
5. Returns evaluation results

---

### 2. `create_aws_nodes_if_needed_task`

**Purpose**: Second task that creates AWS nodes if needed.

**Input**: Result from `evaluate_deployment_requirements_task`

**Output**: Updated evaluation result with AWS node creation info

**What it does**:
1. Checks if AWS nodes are needed
2. If needed, submits `create_worker_nodes` task
3. Updates evaluation result with AWS task ID
4. Returns updated result

**Output additions**:
```python
{
    ... (previous result fields) ...,
    'aws_task_id': str,
    'aws_nodes_created': int,
    'aws_status': 'submitted' | 'error',
    'aws_error': str (if error),
}
```

---

### 3. `place_and_create_pods_task`

**Purpose**: Third task that places and creates pods.

**Input**: Result from `create_aws_nodes_if_needed_task`

**Output**: Final scheduling results

**What it does**:
1. Gets all available hosts (existing + newly created)
2. Recalculates placement if AWS nodes were created
3. Creates pods on assigned hosts using `create_pod_task`
4. Returns final results

**Output**:
```python
{
    'status': 'success' | 'error',
    'deployment': str,
    'namespace': str,
    'placement': Dict[int, List[Tuple]],
    'pods_created': List[Dict],
    'pods_failed': List[Dict],
    'total_replicas': int,
    'pods_created_count': int,
    'pods_failed_count': int,
    'message': str,
    'aws_nodes_created': int,
}
```

---

## Usage

### API Endpoint

```bash
POST /sched/deploy/
```

**Request**:
```json
{
  "yaml_content": "metadata:\n  name: my-app-deployment\n  ..."
}
```

**Response**:
```json
{
  "status": "success",
  "message": "Deployment scheduling chain submitted",
  "data": {
    "task_id": "abc123-def456-...",
    "message": "Use /task/{task_id} to check status"
  }
}
```

### Python

```python
from utils.celery.tasks.scheduler_tasks import schedule_deployment_chain

# Create and execute chain
result = schedule_deployment_chain(yaml_content)

# Get task ID
task_id = result.id

# Check status later
from celery.result import AsyncResult
from utils.celery.celery_config import celery_app

async_result = AsyncResult(task_id, app=celery_app)
status = async_result.status  # PENDING, SUCCESS, FAILURE
final_result = async_result.result  # Final result from place_and_create_pods_task
```

---

## Task Flow

### Scenario 1: Sufficient Resources

```
1. evaluate_deployment_requirements_task
   └─> Finds sufficient resources
   └─> Calculates placement
   └─> needs_aws_nodes = False

2. create_aws_nodes_if_needed_task
   └─> Skips AWS node creation
   └─> Passes through result

3. place_and_create_pods_task
   └─> Uses existing placement
   └─> Creates pods on assigned hosts
   └─> Returns success
```

### Scenario 2: Insufficient Resources

```
1. evaluate_deployment_requirements_task
   └─> Finds insufficient resources
   └─> needs_aws_nodes = True
   └─> required_nodes = 2

2. create_aws_nodes_if_needed_task
   └─> Submits create_worker_nodes task
   └─> Returns with aws_task_id

3. place_and_create_pods_task
   └─> Gets all hosts (including new ones)
   └─> Recalculates placement
   └─> Creates pods on assigned hosts
   └─> Returns success
```

---

## Benefits

### ✅ Asynchronous Execution
- Tasks run in background
- API returns immediately with task ID
- Can monitor progress separately

### ✅ Error Handling
- Each task can handle errors independently
- Failed tasks don't block the chain
- Can retry individual tasks

### ✅ Monitoring
- Track progress of each step
- Check status of chain execution
- Get detailed results from each task

### ✅ Scalability
- Tasks can be distributed across workers
- Parallel execution where possible
- Better resource utilization

---

## Monitoring

### Check Chain Status

```python
from celery.result import AsyncResult
from utils.celery.celery_config import celery_app

task_id = "abc123-def456-..."
async_result = AsyncResult(task_id, app=celery_app)

# Check status
print(f"Status: {async_result.status}")

# Get result (waits if pending)
if async_result.ready():
    result = async_result.result
    print(f"Final result: {result}")
```

### Check Individual Task Results

The chain result contains the final result from `place_and_create_pods_task`, but you can also check intermediate results by inspecting the chain's task results.

---

## Error Handling

### Task 1 Fails
- Chain stops
- Returns error in result
- No AWS nodes created
- No pods created

### Task 2 Fails
- AWS node creation fails
- Chain continues to Task 3
- Task 3 may still succeed if existing nodes are sufficient

### Task 3 Fails
- Pod creation fails
- Returns error with partial results
- Shows which pods were created/failed

---

## Configuration

### Task Routing

Tasks are routed to appropriate queues:
- `evaluate_deployment_requirements_task`: Control plane queue
- `create_aws_nodes_if_needed_task`: Control plane queue
- `place_and_create_pods_task`: Control plane queue
- `create_pod_task`: Host-specific queues (from Task 3)

### Retry Configuration

Configure retries in `celery_config.py`:

```python
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True
celery_app.conf.task_default_retry_delay = 60  # seconds
```

---

## Example: Full Workflow

```python
from utils.celery.tasks.scheduler_tasks import schedule_deployment_chain
from celery.result import AsyncResult
from utils.celery.celery_config import celery_app
import time

# Submit chain
yaml_content = """
metadata:
  name: my-app-deployment
  ...
"""

result = schedule_deployment_chain(yaml_content)
task_id = result.id

# Monitor progress
while True:
    async_result = AsyncResult(task_id, app=celery_app)
    status = async_result.status
    
    print(f"Status: {status}")
    
    if status == 'SUCCESS':
        final_result = async_result.result
        print(f"Pods created: {final_result['pods_created_count']}")
        break
    elif status == 'FAILURE':
        print(f"Error: {async_result.result}")
        break
    
    time.sleep(2)
```

---

## Comparison: Chain vs Synchronous

### Chain (Recommended)
- ✅ Asynchronous
- ✅ Better error handling
- ✅ Can monitor progress
- ✅ Scalable
- ❌ More complex

### Synchronous
- ✅ Simple
- ✅ Immediate results
- ❌ Blocks API
- ❌ No progress tracking
- ❌ Less scalable

---

**Last Updated**: December 2024




