# Redis Data Model Summary

## Quick Overview

A comprehensive Redis data model for storing and querying:
- **Host Information**: IP address, CPU, memory, system metrics
- **Pod Information**: Pods, containers, namespaces, applications
- **Efficient Queries**: By host, namespace, or application

---

## Key Features

✅ **Structured Storage**: Organized data with indexes for fast queries  
✅ **Automatic Indexing**: Indexes updated automatically on save  
✅ **TTL Management**: Automatic expiration of stale data  
✅ **Integration Ready**: Easy integration with worker_node_tasks  
✅ **Query Flexibility**: Multiple query patterns supported  

---

## Files Created

1. **`docs/REDIS_DATA_MODEL.md`** - Complete schema documentation
2. **`utils/redis/host_pod_store.py`** - Core storage and query implementation
3. **`utils/redis/host_pod_integration.py`** - Integration with task results
4. **`docs/REDIS_DATA_MODEL_USAGE.md`** - Usage guide with examples

---

## Quick Start

```python
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from utils.redis.host_pod_integration import HostPodIntegration

# Initialize
rd = RedisInterface()
store = HostPodStore(rd)
integration = HostPodIntegration(rd)

# Store host info
integration.update_host_from_task_result(
    hostname="worker-01",
    system_info={"cpu_count": 4},
    usage_metrics={"cpu_usage": [45.2]},
    ip_address="192.168.1.10"
)

# Query by host
pods = store.get_pods_by_host("worker-01")

# Query by namespace
summary = store.get_namespace_summary("production")

# Query by application
app = store.get_application("my-app")
```

---

## Data Structure

### Host Keys
- `host:{hostname}` - Host data
- `host:index:ip` - IP to hostname mapping
- `host:index:all` - All hosts

### Pod Keys
- `pod:{pod_id}` - Pod data
- `pod:index:host:{hostname}` - Pods on host
- `pod:index:namespace:{namespace}` - Pods in namespace
- `pod:index:app:{app_name}` - Pods for application

### Application Keys
- `app:{app_name}` - Application data
- `app:index:namespace:{namespace}` - Apps in namespace
- `app:index:host:{hostname}` - Apps on host

---

## Query Methods

### By Host
- `get_host(hostname)` - Get host info
- `get_pods_by_host(hostname)` - Get all pods on host
- `get_applications_by_host(hostname)` - Get apps on host
- `get_host_with_pods_and_apps(hostname)` - Complete host info

### By Namespace
- `get_pods_by_namespace(namespace)` - Get all pods
- `get_hosts_by_namespace(namespace)` - Get all hosts
- `get_applications_by_namespace(namespace)` - Get all apps
- `get_namespace_summary(namespace)` - Complete summary

### By Application
- `get_application(app_name)` - Get app info
- `get_pods_by_application(app_name)` - Get all pods
- `get_hosts_by_application(app_name)` - Get all hosts

### Complex Queries
- `get_pods_by_host_and_namespace(hostname, namespace)` - Combined query
- `get_host_by_ip(ip_address)` - Lookup by IP

---

## Integration Points

### Worker Node Tasks
- `get_worker_node_info` → Update system_info
- `get_usage` → Update usage_metrics
- `get_host_ip` → Update ip_address

### Containerd Tasks
- `create_pod_task` → Store pod information
- `list_pods_by_namespace_task` → Sync pod list
- `terminate_pod_task` → Remove pod

---

## Next Steps

1. **Add API Endpoints**: Create FastAPI endpoints to query this data
2. **Background Sync**: Set up periodic sync from task results
3. **Monitoring**: Add metrics and monitoring for data freshness
4. **Migration**: Migrate existing data to new structure

---

**See `REDIS_DATA_MODEL.md` for complete documentation.**  
**See `REDIS_DATA_MODEL_USAGE.md` for usage examples.**





