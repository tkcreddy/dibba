# Redis Data Model Usage Guide

## Overview

This guide shows how to use the Redis data model for storing and querying host and pod information.

---

## Setup

```python
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.host_pod_integration import HostPodIntegration

# Initialize
redis_interface = RedisInterface()
store = HostPodStore(redis_interface)
integration = HostPodIntegration(redis_interface)
```

---

## Storing Host Information

### From Worker Node Tasks

```python
# After get_worker_node_info task completes
system_info = {
    'System': 'Linux',
    'Node Name': 'worker-01',
    'cpu_count': 4,
    'Memory': 16.0,
    # ... other system info
}

integration.update_host_from_task_result(
    hostname="worker-01",
    system_info=system_info
)

# After get_usage task completes
usage_metrics = {
    'Cpu_usage': [45.2, 50.1, 48.3, 52.0],
    'Virtual Memory': {...},
    'Swap Memory': {...}
}

integration.update_host_from_task_result(
    hostname="worker-01",
    usage_metrics=usage_metrics
)

# After get_host_ip task completes
ip_address = "192.168.1.10"

integration.update_host_from_task_result(
    hostname="worker-01",
    ip_address=ip_address
)
```

### Manual Storage

```python
store.save_host_info(
    hostname="worker-01",
    ip_address="192.168.1.10",
    system_info={
        "system": "Linux",
        "cpu_count": 4,
        "memory_gb": 16.0
    },
    usage_metrics={
        "cpu_usage": [45.2, 50.1],
        "virtual_memory": {...}
    },
    status="online"
)
```

---

## Storing Pod Information

### From Create Pod Task Result

```python
# After create_pod_task completes
pod_result = {
    "namespace": "production",
    "pod": {
        "name": "cd83c6a7ac0f47c6",
        "pause": {
            "cid": "pause-container-id",
            "pid": 12345
        }
    },
    "pod_ipv4": "10.244.1.5",
    "apps": [
        {
            "cid": "container-1-id",
            "name": "nginx",
            "image": "nginx:latest",
            "pid": 12346
        }
    ],
    "cni": {
        "network": "calico",
        "ifname": "eth0"
    },
    "labels": {
        "app": "my-application",
        "version": "v1.0"
    }
}

integration.update_pod_from_task_result(
    pod_result=pod_result,
    hostname="worker-01"
)
```

### From List Pods Result

```python
# After list_pods_by_namespace_task completes
pods_list = [
    {
        "pod_id": "cd83c6a7ac0f47c6",
        "pause": {"pid": 12345, "status": "running"},
        "apps": [
            {
                "id": "container-1-id",
                "name": "nginx",
                "image": "nginx:latest",
                "status": "running"
            }
        ]
    }
]

integration.update_pod_from_list_result(
    pods_list=pods_list,
    hostname="worker-01",
    namespace="production"
)
```

### Manual Storage

```python
store.save_pod(
    pod_id="cd83c6a7ac0f47c6",
    pod_name="my-app-pod",
    namespace="production",
    hostname="worker-01",
    ip_address="10.244.1.5",
    pause_container={
        "cid": "pause-container-id",
        "pid": 12345,
        "status": "running"
    },
    containers=[
        {
            "cid": "container-1-id",
            "name": "nginx",
            "image": "nginx:latest",
            "status": "running"
        }
    ],
    labels={
        "app": "my-application",
        "version": "v1.0"
    },
    status="running"
)
```

---

## Querying Data

### Query by Host

```python
# Get host information
host = store.get_host("worker-01")
print(f"Host IP: {host['ip_address']}")
print(f"CPU Count: {host['system_info']['cpu_count']}")

# Get all pods on a host
pods = store.get_pods_by_host("worker-01")
print(f"Pods on worker-01: {len(pods)}")

# Get applications on a host
apps = store.get_applications_by_host("worker-01")
print(f"Applications: {[app['name'] for app in apps]}")

# Get complete host information with pods and apps
host_details = store.get_host_with_pods_and_apps("worker-01")
print(f"Host: {host_details['host']['hostname']}")
print(f"Pods: {host_details['pod_count']}")
print(f"Applications: {host_details['application_count']}")
```

### Query by Namespace

```python
# Get all pods in namespace
pods = store.get_pods_by_namespace("production")
print(f"Pods in production: {len(pods)}")

# Get all hosts in namespace
hosts = store.get_hosts_by_namespace("production")
print(f"Hosts: {[h['hostname'] for h in hosts]}")

# Get all applications in namespace
apps = store.get_applications_by_namespace("production")
print(f"Applications: {[a['name'] for a in apps]}")

# Get namespace summary
summary = store.get_namespace_summary("production")
print(f"Namespace: {summary['namespace']}")
print(f"Hosts: {summary['host_count']}")
print(f"Pods: {summary['pod_count']}")
print(f"Applications: {summary['application_count']}")
print(f"Total Containers: {summary['total_containers']}")
```

### Query by Application

```python
# Get application information
app = store.get_application("my-application")
print(f"Application: {app['name']}")
print(f"Pods: {app['pods']}")
print(f"Hosts: {app['hosts']}")
print(f"Total Containers: {app['total_containers']}")

# Get all pods for application
pods = store.get_pods_by_application("my-application")
print(f"Pods: {len(pods)}")

# Get all hosts running application
hosts = store.get_hosts_by_application("my-application")
print(f"Hosts: {[h['hostname'] for h in hosts]}")
```

### Complex Queries

```python
# Get pods on a host in a specific namespace
pods = store.get_pods_by_host_and_namespace("worker-01", "production")
print(f"Pods on worker-01 in production: {len(pods)}")

# Get host by IP
host = store.get_host_by_ip("192.168.1.10")
if host:
    print(f"Hostname: {host['hostname']}")

# Get all hosts
all_hosts = store.get_all_hosts()
print(f"Total hosts: {len(all_hosts)}")
```

---

## Integration with Celery Tasks

### Update Host After Task Completion

```python
# In your task result handler
from utils.celery.tasks.worker_node_tasks import get_worker_node_info, get_usage, get_host_ip
from utils.redis.host_pod_integration import update_host_from_system_info

# After get_worker_node_info completes
task_result = get_worker_node_info.AsyncResult(task_id).get()
if task_result:
    update_host_from_system_info(
        redis_interface=rd,
        hostname="worker-01",
        system_info=task_result
    )

# After get_usage completes
usage_result = get_usage.AsyncResult(task_id).get()
if usage_result:
    update_host_from_usage(
        redis_interface=rd,
        hostname="worker-01",
        usage_metrics=usage_result
    )

# After get_host_ip completes
ip_result = get_host_ip.AsyncResult(task_id).get()
if ip_result:
    update_host_from_ip(
        redis_interface=rd,
        hostname="worker-01",
        ip_address=ip_result
    )
```

### Update Pod After Creation

```python
# In your pod creation handler
from utils.celery.tasks.containerd_tasks import create_pod_task
from utils.redis.host_pod_integration import update_pod_from_create_result

# After create_pod_task completes
pod_result = create_pod_task.AsyncResult(task_id).get()
if pod_result and "error" not in pod_result:
    update_pod_from_create_result(
        redis_interface=rd,
        pod_result=pod_result,
        hostname="worker-01"
    )
```

---

## Cleanup Operations

### Remove Pod

```python
# When pod is terminated
integration.remove_pod("cd83c6a7ac0f47c6")
```

### Mark Host Offline

```python
# When host goes offline
integration.mark_host_offline("worker-01")
```

### Delete Host

```python
# When host is removed
store.delete_host("worker-01")
```

---

## Example: Complete Workflow

```python
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_integration import HostPodIntegration

# Initialize
rd = RedisInterface()
integration = HostPodIntegration(rd)

# 1. Update host information
integration.update_host_from_task_result(
    hostname="worker-01",
    system_info={"cpu_count": 4, "memory_gb": 16.0},
    usage_metrics={"cpu_usage": [45.2, 50.1]},
    ip_address="192.168.1.10"
)

# 2. Create and store pod
pod_result = {
    "namespace": "production",
    "pod": {"name": "pod-123"},
    "pod_ipv4": "10.244.1.5",
    "apps": [{"name": "nginx", "image": "nginx:latest"}],
    "labels": {"app": "my-app"}
}
integration.update_pod_from_task_result(
    pod_result=pod_result,
    hostname="worker-01"
)

# 3. Query data
store = integration.store

# Get all pods on host
pods = store.get_pods_by_host("worker-01")
print(f"Pods: {len(pods)}")

# Get namespace summary
summary = store.get_namespace_summary("production")
print(f"Summary: {summary}")

# Get application info
app = store.get_application("my-app")
print(f"Application: {app}")
```

---

## Best Practices

1. **Update Host Info Regularly**: Call update functions after each task completes
2. **Handle Errors**: Wrap update calls in try-except blocks
3. **Use Integration Functions**: Prefer integration functions over direct store methods
4. **Clean Up**: Remove pods when terminated, mark hosts offline when unavailable
5. **Monitor TTLs**: Data expires automatically, refresh before expiration

---

## Performance Tips

1. **Use Pipelines**: For multiple operations, use Redis pipelines
2. **Batch Updates**: Update multiple items in a single transaction
3. **Cache Results**: Cache frequently accessed data in application memory
4. **Index Usage**: Always use indexes for queries, not full scans

---

**Last Updated**: December 2024





