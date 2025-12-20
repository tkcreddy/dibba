# Dibba API Quick Reference

## Base URL
```
http://localhost:8000
```

## Authentication
```bash
# Get token
curl -X POST "http://localhost:8000/token" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "username=admin&password=secret"

# Use token
Authorization: Bearer <token>
```

---

## Endpoints Summary

### Authentication
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/token` | No | Get JWT access token |

### AWS Management
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/create-instances/` | Yes | Create EC2 instances |
| POST | `/terminate-namespace/` | Yes | Terminate all instances in namespace |

### Worker Nodes
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/get_worker_node_data/` | Yes | Get system information |
| GET | `/get_worker_node_ip/` | Yes | Get IP address |
| GET | `/get_worker_usage_data/` | Yes | Get resource usage |

### Task Management
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/task/{task_id}` | Yes | Get task status |

### Containerd - Pods
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/containerd/create-pods` | Yes | Create pod with containers |
| POST | `/containerd/list_namespaces_and_pods/` | Yes | List all namespaces and pods |
| POST | `/containerd/list_pods_by_namespace/` | Yes | List pods in namespace |
| POST | `/containerd/terminate_pod/` | Yes | Terminate pod by name |
| POST | `/containerd/terminate_pod_by_pause_cid/` | Yes | Terminate pod by pause CID |
| POST | `/containerd/destroy_all_pods/` | Yes | Destroy all pods in namespace |

### Containerd - Containers
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/containerd/destroy_container/` | Yes | Destroy container by ID |
| POST | `/containerd/get_container_info/` | Yes | Get container information |

### Containerd - Maintenance
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/containerd/purge_stopped/` | Yes | Purge stopped containers |
| POST | `/containerd/prune_namespace/` | Yes | Prune namespace resources |
| POST | `/containerd/cleanup_tasks_by_pod_prefix/` | Yes | Cleanup tasks by pod prefix |

---

## Common Request Patterns

### Create EC2 Instances
```bash
curl -X POST "http://localhost:8000/create-instances/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "instance_type": "t2.micro",
       "ami_id": "ami-12345678",
       "key_name": "my-key",
       "security_group_ids": ["sg-123"],
       "subnet_id": "subnet-123",
       "namespace": "prod",
       "min_count": 1,
       "max_count": 3
     }'
```

### Create Pod
```bash
curl -X POST "http://localhost:8000/containerd/create-pods" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "containers": [{
         "name": "nginx",
         "image": "nginx:latest",
         "resources": {
           "cpu_millicores": 500,
           "memory": "256Mi"
         }
       }]
     }'
```

### Check Task Status
```bash
curl -X GET "http://localhost:8000/task/<task_id>" \
     -H "Authorization: Bearer <token>"
```

---

## Response Format

### Success
```json
{
    "error": false,
    "message": "Success message",
    "data": { ... }
}
```

### Error
```json
{
    "error": true,
    "error_code": "ERROR_CODE",
    "message": "Error message",
    "details": { ... }
}
```

---

## Task Status Values

- `PENDING` - Waiting to execute
- `PROGRESS` - Currently running
- `SUCCESS` - Completed successfully
- `FAILURE` - Failed with error
- `REVOKED` - Cancelled

---

## Interactive Docs

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

*For detailed documentation, see `API_DOCUMENTATION.md`*

