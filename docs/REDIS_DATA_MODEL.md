# Redis Data Model for Host and Pod Information

## Overview

This document describes the Redis data model for storing and querying:
- **Host Information**: IP address, CPU, memory, system metrics from worker nodes
- **Pod Information**: Pod details, containers, namespaces, applications
- **Query Capabilities**: Query by host, namespace, or application

---

## Data Model Schema

### 1. Host Information Storage

#### Primary Host Data (Hash)
**Key Pattern**: `host:{hostname}`
**Type**: Hash
**TTL**: 3600 seconds (1 hour) - auto-refresh on update

```json
{
  "hostname": "worker-01",
  "ip_address": "192.168.1.10",
  "system_info": {
    "system": "Linux",
    "node_name": "worker-01",
    "release": "5.4.0",
    "version": "#1 SMP",
    "machine": "x86_64",
    "processor": "Intel",
    "cpu_count": 4,
    "memory_gb": 16.0,
    "logical_cpu_count": 8,
    "physical_cpu_count": 4,
    "cpu_frequency": {"current": 2400.0, "min": 800.0, "max": 3200.0}
  },
  "usage_metrics": {
    "cpu_usage": [45.2, 50.1, 48.3, 52.0],
    "virtual_memory": {
      "total": 17179869184,
      "available": 8589934592,
      "percent": 50.0,
      "used": 8589934592,
      "free": 8589934592
    },
    "swap_memory": {
      "total": 4294967296,
      "used": 0,
      "free": 4294967296,
      "percent": 0.0
    }
  },
  "last_updated": "2024-12-15T10:30:00Z",
  "status": "online"
}
```

#### Host Indexes

**Host by IP** (Hash)
- Key: `host:index:ip`
- Field: `{ip_address}` → `{hostname}`
- Purpose: Quick lookup by IP address

**Host by Namespace** (Set)
- Key: `host:index:namespace:{namespace}`
- Members: `{hostname}` values
- Purpose: Find all hosts in a namespace

**Host by Application** (Set)
- Key: `host:index:app:{application_name}`
- Members: `{hostname}` values
- Purpose: Find all hosts running a specific application

**All Hosts** (Set)
- Key: `host:index:all`
- Members: All `{hostname}` values
- Purpose: List all known hosts

---

### 2. Pod Information Storage

#### Primary Pod Data (Hash)
**Key Pattern**: `pod:{pod_id}`
**Type**: Hash
**TTL**: 7200 seconds (2 hours) - auto-refresh on update

```json
{
  "pod_id": "cd83c6a7ac0f47c6",
  "pod_name": "my-app-pod",
  "namespace": "production",
  "hostname": "worker-01",
  "ip_address": "10.244.1.5",
  "pause_container": {
    "cid": "pause-container-id",
    "pid": 12345,
    "status": "running"
  },
  "containers": [
    {
      "cid": "container-1-id",
      "name": "nginx",
      "image": "nginx:latest",
      "pid": 12346,
      "status": "running"
    },
    {
      "cid": "container-2-id",
      "name": "app",
      "image": "myapp:v1.0",
      "pid": 12347,
      "status": "running"
    }
  ],
  "cni_network": {
    "network": "calico",
    "ifname": "eth0",
    "result": {...}
  },
  "resources": {
    "cpu_millicores": 500,
    "memory": "256Mi"
  },
  "labels": {
    "app": "my-application",
    "version": "v1.0",
    "environment": "production"
  },
  "created_at": "2024-12-15T10:00:00Z",
  "last_updated": "2024-12-15T10:30:00Z",
  "status": "running"
}
```

#### Pod Indexes

**Pods by Host** (Set)
- Key: `pod:index:host:{hostname}`
- Members: `{pod_id}` values
- Purpose: Find all pods on a specific host

**Pods by Namespace** (Set)
- Key: `pod:index:namespace:{namespace}`
- Members: `{pod_id}` values
- Purpose: Find all pods in a namespace

**Pods by Application** (Set)
- Key: `pod:index:app:{application_name}`
- Members: `{pod_id}` values
- Purpose: Find all pods for an application

**Pods by Host and Namespace** (Set)
- Key: `pod:index:host:{hostname}:namespace:{namespace}`
- Members: `{pod_id}` values
- Purpose: Find pods on a host in a specific namespace

**All Pods** (Set)
- Key: `pod:index:all`
- Members: All `{pod_id}` values
- Purpose: List all known pods

---

### 3. Application Information Storage

#### Application Metadata (Hash)
**Key Pattern**: `app:{application_name}`
**Type**: Hash
**TTL**: 86400 seconds (24 hours)

```json
{
  "name": "my-application",
  "namespace": "production",
  "pods": ["pod-id-1", "pod-id-2"],
  "hosts": ["worker-01", "worker-02"],
  "total_containers": 4,
  "status": "running",
  "created_at": "2024-12-15T09:00:00Z",
  "last_updated": "2024-12-15T10:30:00Z"
}
```

#### Application Indexes

**Applications by Namespace** (Set)
- Key: `app:index:namespace:{namespace}`
- Members: `{application_name}` values

**Applications by Host** (Set)
- Key: `app:index:host:{hostname}`
- Members: `{application_name}` values

**All Applications** (Set)
- Key: `app:index:all`
- Members: All `{application_name}` values

---

## Redis Key Patterns Summary

### Host Keys
```
host:{hostname}                          # Host data
host:index:ip                            # IP to hostname mapping
host:index:namespace:{namespace}         # Hosts in namespace
host:index:app:{app_name}                # Hosts running app
host:index:all                           # All hosts
```

### Pod Keys
```
pod:{pod_id}                             # Pod data
pod:index:host:{hostname}                # Pods on host
pod:index:namespace:{namespace}          # Pods in namespace
pod:index:app:{app_name}                 # Pods for app
pod:index:host:{hostname}:namespace:{namespace}  # Pods on host in namespace
pod:index:all                            # All pods
```

### Application Keys
```
app:{app_name}                           # Application data
app:index:namespace:{namespace}          # Apps in namespace
app:index:host:{hostname}                # Apps on host
app:index:all                            # All applications
```

---

## Query Patterns

### 1. Query by Host
```python
# Get all pods on a host
pods = get_pods_by_host("worker-01")

# Get host information
host_info = get_host("worker-01")

# Get applications on a host
apps = get_applications_by_host("worker-01")
```

### 2. Query by Namespace
```python
# Get all pods in namespace
pods = get_pods_by_namespace("production")

# Get all hosts in namespace
hosts = get_hosts_by_namespace("production")

# Get all applications in namespace
apps = get_applications_by_namespace("production")
```

### 3. Query by Application
```python
# Get all pods for application
pods = get_pods_by_application("my-application")

# Get all hosts running application
hosts = get_hosts_by_application("my-application")

# Get application details
app_info = get_application("my-application")
```

### 4. Complex Queries
```python
# Get pods on a host in a namespace
pods = get_pods_by_host_and_namespace("worker-01", "production")

# Get host with pods and applications
host_with_details = get_host_with_pods_and_apps("worker-01")

# Get namespace summary
namespace_summary = get_namespace_summary("production")
```

---

## Data Update Strategy

### Host Updates
1. When `get_worker_node_info` task completes:
   - Update `host:{hostname}` with system_info
   - Update `host:index:all`
   - Refresh TTL

2. When `get_usage` task completes:
   - Update `host:{hostname}` with usage_metrics
   - Update `last_updated` timestamp

3. When `get_host_ip` task completes:
   - Update `host:{hostname}` with ip_address
   - Update `host:index:ip` mapping

### Pod Updates
1. When pod is created:
   - Create `pod:{pod_id}` entry
   - Add to all relevant indexes
   - Link to host and namespace

2. When pod is updated:
   - Update `pod:{pod_id}` entry
   - Update `last_updated` timestamp
   - Refresh TTL

3. When pod is deleted:
   - Remove from all indexes
   - Delete `pod:{pod_id}` entry

### Application Updates
1. When pod is created/updated:
   - Extract application name from labels
   - Update `app:{app_name}` entry
   - Update application indexes

---

## Performance Considerations

### Indexing Strategy
- Use Redis Sets for indexes (O(1) add/remove, O(N) iteration)
- Use Redis Hashes for data storage (efficient memory usage)
- Set appropriate TTLs to auto-cleanup stale data

### Query Optimization
- Use pipeline for multiple operations
- Use SCAN for large result sets instead of SMEMBERS
- Cache frequently accessed data

### Memory Management
- Set TTLs on all keys
- Use Redis expiration policies
- Monitor memory usage

---

## Example Data Flow

### 1. Host Registration
```
1. Worker node starts
2. get_worker_node_info task runs
3. Store host data: host:worker-01
4. Update indexes: host:index:all
5. Set TTL: 3600 seconds
```

### 2. Pod Creation
```
1. create_pod_task completes
2. Store pod data: pod:cd83c6a7ac0f47c6
3. Update indexes:
   - pod:index:host:worker-01
   - pod:index:namespace:production
   - pod:index:app:my-application
   - pod:index:all
4. Update application: app:my-application
5. Update host: host:worker-01 (add pod reference)
```

### 3. Query Execution
```
1. User queries: "Get all pods on worker-01"
2. Read index: pod:index:host:worker-01
3. Get pod IDs: [pod-id-1, pod-id-2]
4. Pipeline get: pod:pod-id-1, pod:pod-id-2
5. Return combined results
```

---

## Migration from Current Model

### Current State
- Hosts stored in `nodes` hash
- Containers stored in `containers` hash
- No pod-specific storage
- Limited indexing

### Migration Steps
1. Create new key patterns alongside existing
2. Populate new structure from existing data
3. Update write operations to use new structure
4. Update read operations gradually
5. Deprecate old structure after full migration

---

## Implementation Notes

- All JSON data stored as strings in Redis
- Use `json.dumps()` for storage, `json.loads()` for retrieval
- Handle missing keys gracefully (return None/empty)
- Log all write operations for debugging
- Use transactions (MULTI/EXEC) for atomic updates

---

**Last Updated**: December 2024





