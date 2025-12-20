# Dibba API Documentation

**Version**: 1.0.0  
**Base URL**: `http://localhost:8000`  
**Authentication**: OAuth2 Password Flow with JWT Bearer Token

---

## Table of Contents

1. [Authentication](#authentication)
2. [AWS Management](#aws-management)
3. [Worker Nodes](#worker-nodes)
4. [Task Management](#task-management)
5. [Containerd - Pods](#containerd---pods)
6. [Containerd - Containers](#containerd---containers)
7. [Containerd - Maintenance](#containerd---maintenance)
8. [Error Responses](#error-responses)
9. [Response Format](#response-format)

---

## Authentication

### POST /token

Authenticate a user and obtain a JWT access token.

**Authentication**: Not required

**Request**:

**Content-Type**: `application/x-www-form-urlencoded`

**Request Body** (Form Data):
```
username: string (required) - Username
password: string (required) - Password
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/token" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "username=admin&password=secret123"
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Authentication successful",
    "data": {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbiIsImV4cCI6MTY4MDAwMDAwMH0.abc123...",
        "token_type": "bearer"
    }
}
```

**Error Response** (400 Bad Request):
```json
{
    "error": true,
    "error_code": "INVALID_CREDENTIALS",
    "message": "Invalid username or password",
    "details": {
        "username": "admin"
    }
}
```

**Token Usage**:
Include the token in the Authorization header for all protected endpoints:
```
Authorization: Bearer <access_token>
```

**Token Expiration**: 30 minutes

---

## AWS Management

### POST /create-instances/

Create AWS EC2 worker instances.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "instance_type": "string (required) - EC2 instance type (e.g., 't2.micro', 't3.medium')",
    "ami_id": "string (required) - AMI ID to use for instances",
    "key_name": "string (required) - AWS key pair name for SSH access",
    "security_group_ids": ["string"] (required) - Array of security group IDs,
    "subnet_id": "string (required) - Subnet ID where instances will be launched",
    "namespace": "string (required) - Namespace to associate instances with",
    "min_count": "integer (required) - Minimum number of instances to create",
    "max_count": "integer (required) - Maximum number of instances to create"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/create-instances/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "instance_type": "t2.micro",
       "ami_id": "ami-0c55b159cbfafe1f0",
       "key_name": "my-key-pair",
       "security_group_ids": ["sg-12345678"],
       "subnet_id": "subnet-12345678",
       "namespace": "production",
       "min_count": 1,
       "max_count": 3
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "abc123-def456-ghi789"
    }
}
```

**Error Response** (500 Internal Server Error):
```json
{
    "error": true,
    "error_code": "CREATE_INSTANCES_TASK_ERROR",
    "message": "Failed to submit create instances task",
    "details": {
        "namespace": "production",
        "instance_type": "t2.micro"
    }
}
```

**Note**: Use the `task_id` to check task status via `/task/{task_id}` endpoint.

---

### POST /terminate-namespace/

Terminate all EC2 instances in a namespace.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "namespace": "string (required) - Namespace containing instances to terminate"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/terminate-namespace/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "namespace": "production"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "xyz789-abc123-def456",
        "instances_count": 3
    }
}
```

**Error Response** (404 Not Found):
```json
{
    "error": true,
    "error_code": "NO_INSTANCES_FOUND",
    "message": "No instances found for the given namespace",
    "details": {
        "namespace": "production"
    }
}
```

---

## Worker Nodes

### GET /get_worker_node_data/

Get comprehensive system information from a worker node.

**Authentication**: Required (Bearer Token)

**Query Parameters**:
- `host_name`: string (required) - Hostname of the worker node

**Example Request**:
```bash
curl -X GET "http://localhost:8000/get_worker_node_data/?host_name=worker-01" \
     -H "Authorization: Bearer <token>"
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "sys-info-task-123",
        "host_name": "worker-01"
    }
}
```

**Task Result** (retrieved via `/task/{task_id}`):
```json
{
    "hostname": "worker-01",
    "os": "Linux",
    "kernel": "5.4.0",
    "architecture": "x86_64",
    "cpu_count": 4,
    "memory_total": 8192,
    "disk_total": 100000
}
```

---

### GET /get_worker_node_ip/

Get the IP address of a worker node.

**Authentication**: Required (Bearer Token)

**Query Parameters**:
- `host_name`: string (required) - Hostname of the worker node

**Example Request**:
```bash
curl -X GET "http://localhost:8000/get_worker_node_ip/?host_name=worker-01" \
     -H "Authorization: Bearer <token>"
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "ip-task-456",
        "host_name": "worker-01"
    }
}
```

**Task Result**:
```json
{
    "ip_address": "10.0.1.5"
}
```

---

### GET /get_worker_usage_data/

Get resource usage metrics (CPU, memory, disk) from a worker node.

**Authentication**: Required (Bearer Token)

**Query Parameters**:
- `host_name`: string (required) - Hostname of the worker node

**Example Request**:
```bash
curl -X GET "http://localhost:8000/get_worker_usage_data/?host_name=worker-01" \
     -H "Authorization: Bearer <token>"
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "usage-task-789",
        "host_name": "worker-01"
    }
}
```

**Task Result**:
```json
{
    "cpu": 45.5,
    "memory": 60.2,
    "disk": 75.0
}
```

---

## Task Management

### GET /task/{task_id}

Get the status of a Celery task.

**Authentication**: Required (Bearer Token)

**Path Parameters**:
- `task_id`: string (required) - Celery task ID

**Example Request**:
```bash
curl -X GET "http://localhost:8000/task/abc123-def456-ghi789" \
     -H "Authorization: Bearer <token>"
```

**Success Response** (200 OK):

**Pending Task**:
```json
{
    "task_id": "abc123-def456-ghi789",
    "status": "PENDING",
    "result": null,
    "progress": null
}
```

**In Progress Task**:
```json
{
    "task_id": "abc123-def456-ghi789",
    "status": "PROGRESS",
    "result": null,
    "progress": {
        "current": 2,
        "total": 3,
        "percent": 66.67
    }
}
```

**Completed Task**:
```json
{
    "task_id": "abc123-def456-ghi789",
    "status": "SUCCESS",
    "result": {
        "instances": [
            {
                "InstanceId": "i-1234567890abcdef0",
                "PrivateIpAddress": "10.0.1.5",
                "PrivateDnsName": "ip-10-0-1-5.ec2.internal",
                "InstanceType": "t2.micro"
            }
        ],
        "count": 1
    },
    "progress": null
}
```

**Failed Task**:
```json
{
    "task_id": "abc123-def456-ghi789",
    "status": "FAILURE",
    "result": {
        "error": "Instance creation failed",
        "traceback": "..."
    },
    "progress": null
}
```

**Task Status Values**:
- `PENDING`: Task is waiting to be executed
- `PROGRESS`: Task is currently running
- `SUCCESS`: Task completed successfully
- `FAILURE`: Task failed with an error
- `REVOKED`: Task was cancelled

---

## Containerd - Pods

### POST /containerd/create-pods

Create a pod with one or more containers on a worker node.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (optional, default: 'k8s.io') - Containerd namespace",
    "containers": [
        {
            "name": "string (required) - Container name",
            "image": "string (required) - Container image (e.g., 'nginx:latest')",
            "args": ["string"] (optional) - Command arguments,
            "env": {
                "KEY": "value"
            } (optional) - Environment variables,
            "resources": {
                "cpu_millicores": "integer (optional, default: 100) - CPU allocation in millicores (1000 = 1 CPU)",
                "memory": "string (optional, default: '64Mi') - Memory limit (supports: '64Mi', '256M', '1Gi', etc.)",
                "cpuset_cpus": "string (optional) - CPU set (e.g., '0-3' or '0,2,4')"
            } (optional),
            "mounts": [
                {
                    "type": "bind",
                    "source": "/host/path",
                    "destination": "/container/path",
                    "options": ["ro"]
                }
            ] (optional) - Volume mounts
        }
    ] (required) - Array of container specifications
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/create-pods" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "containers": [
         {
           "name": "nginx",
           "image": "nginx:latest",
           "resources": {
             "cpu_millicores": 500,
             "memory": "256Mi"
           },
           "env": {
             "NGINX_HOST": "example.com",
             "NGINX_PORT": "80"
           },
           "args": ["nginx", "-g", "daemon off;"]
         },
         {
           "name": "redis",
           "image": "redis:7-alpine",
           "resources": {
             "cpu_millicores": 200,
             "memory": "128Mi"
           }
         }
       ]
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "pod-create-task-123",
        "host_name": "worker-01",
        "namespace": "production",
        "containers_count": 2
    }
}
```

**Task Result** (when completed):
```json
{
    "pod_id": "cd83c6a7ac0f47c6",
    "pause_cid": "abc123def456",
    "containers": [
        {
            "name": "nginx",
            "cid": "container-nginx-123",
            "status": "running"
        },
        {
            "name": "redis",
            "cid": "container-redis-456",
            "status": "running"
        }
    ],
    "network": {
        "ip": "10.244.1.5",
        "network": "calico"
    }
}
```

---

### POST /containerd/list_namespaces_and_pods/

List all containerd namespaces and their associated pods on a worker node.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/list_namespaces_and_pods/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "list-pods-task-123",
        "host_name": "worker-01"
    }
}
```

**Task Result**:
```json
{
    "namespaces": {
        "k8s.io": {
            "pods": [
                {
                    "pod_id": "cd83c6a7ac0f47c6",
                    "pause_cid": "abc123def456",
                    "containers": ["nginx", "redis"],
                    "status": "running"
                }
            ]
        },
        "production": {
            "pods": [
                {
                    "pod_id": "ef45g8h9ij01kl23",
                    "pause_cid": "xyz789abc123",
                    "containers": ["app"],
                    "status": "running"
                }
            ]
        }
    }
}
```

---

### POST /containerd/list_pods_by_namespace/

List all pods in a specific containerd namespace on a worker node.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/list_pods_by_namespace/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "list-ns-pods-task-456",
        "host_name": "worker-01",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "namespace": "production",
    "pods": [
        {
            "pod_id": "cd83c6a7ac0f47c6",
            "pause_cid": "abc123def456",
            "containers": [
                {
                    "name": "nginx",
                    "cid": "container-nginx-123",
                    "status": "running"
                }
            ],
            "status": "running"
        }
    ]
}
```

---

### POST /containerd/terminate_pod/

Terminate a pod and all its containers by pod name.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "pod_name": "string (required) - Name of the pod to terminate",
    "cni_network": "string (required) - CNI network name (e.g., 'calico')",
    "ifname": "string (required) - Network interface name (e.g., 'eth0')"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/terminate_pod/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "pod_name": "my-pod",
       "cni_network": "calico",
       "ifname": "eth0"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "terminate-pod-task-789",
        "pod_name": "my-pod",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "pod_name": "my-pod",
    "status": "terminated",
    "containers_terminated": 2,
    "network_removed": true
}
```

---

### POST /containerd/terminate_pod_by_pause_cid/

Terminate a pod by its pause container ID.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "pause_cid": "string (required) - Pause container ID",
    "cni_network": "string (required) - CNI network name",
    "ifname": "string (required) - Network interface name"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/terminate_pod_by_pause_cid/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "pause_cid": "abc123def456",
       "cni_network": "calico",
       "ifname": "eth0"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "terminate-pod-cid-task-101",
        "pause_cid": "abc123def456",
        "namespace": "production"
    }
}
```

---

### POST /containerd/destroy_all_pods/

Destroy all pods in a specific namespace on a worker node.

**⚠️ Warning**: This operation is destructive and will remove all pods in the specified namespace.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/destroy_all_pods/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "destroy-all-pods-task-202",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "namespace": "production",
    "pods_destroyed": 5,
    "containers_destroyed": 12
}
```

---

## Containerd - Containers

### POST /containerd/destroy_container/

Destroy a specific container by its container ID.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "cid": "string (required) - Container ID"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/destroy_container/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "cid": "container-id-123"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "destroy-container-task-303",
        "container_id": "container-id-123",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "container_id": "container-id-123",
    "status": "destroyed"
}
```

---

### POST /containerd/get_container_info/

Get detailed information about a specific container.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "cid": "string (required) - Container ID"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/get_container_info/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "cid": "container-id-123"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "get-container-info-task-404",
        "container_id": "container-id-123",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "container_id": "container-id-123",
    "image": "nginx:latest",
    "status": "running",
    "created_at": "2024-01-15T10:30:00Z",
    "resources": {
        "cpu_millicores": 500,
        "memory": "256Mi"
    },
    "env": {
        "NGINX_HOST": "example.com"
    },
    "mounts": []
}
```

---

## Containerd - Maintenance

### POST /containerd/purge_stopped/

Remove all stopped containers and tasks from a namespace.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/purge_stopped/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production"
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "purge-stopped-task-505",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "namespace": "production",
    "containers_purged": 3,
    "tasks_purged": 5
}
```

---

### POST /containerd/prune_namespace/

Prune unused resources in a namespace (containers, snapshots, images).

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "aggressive": "boolean (optional, default: false) - If true, also removes stopped containers and unused snapshots"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/prune_namespace/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "aggressive": true
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "prune-namespace-task-606",
        "namespace": "production",
        "aggressive": true
    }
}
```

**Task Result**:
```json
{
    "namespace": "production",
    "containers_removed": 2,
    "snapshots_removed": 5,
    "images_removed": 1,
    "space_freed_bytes": 1073741824
}
```

---

### POST /containerd/cleanup_tasks_by_pod_prefix/

Clean up stopped tasks that match a pod ID prefix.

**Authentication**: Required (Bearer Token)

**Request Body**:
```json
{
    "host_name": "string (required) - Hostname of the worker node",
    "namespace": "string (required) - Containerd namespace",
    "pod_id": "string (required) - Pod ID prefix to match",
    "prefer_grpc": "boolean (optional, default: true) - Use gRPC API if available"
}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/containerd/cleanup_tasks_by_pod_prefix/" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{
       "host_name": "worker-01",
       "namespace": "production",
       "pod_id": "cd83c6a7ac0f47c6",
       "prefer_grpc": true
     }'
```

**Success Response** (200 OK):
```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "cleanup-tasks-task-707",
        "pod_id": "cd83c6a7ac0f47c6",
        "namespace": "production"
    }
}
```

**Task Result**:
```json
{
    "pod_id": "cd83c6a7ac0f47c6",
    "tasks_cleaned": 3,
    "tasks_removed": [
        "cd83c6a7ac0f47c6-nginx",
        "cd83c6a7ac0f47c6-redis",
        "cd83c6a7ac0f47c6"
    ]
}
```

**Use Case**: When you see STOPPED tasks in `ctr -n <ns> task list` that should be cleaned up, use this endpoint with the pod ID prefix.

---

## Error Responses

All endpoints follow a consistent error response format:

### Standard Error Response Format

```json
{
    "error": true,
    "error_code": "ERROR_CODE",
    "message": "Human-readable error message",
    "details": {
        "field1": "value1",
        "field2": "value2"
    }
}
```

### Common Error Codes

| Error Code | HTTP Status | Description |
|------------|-------------|-------------|
| `INVALID_CREDENTIALS` | 400 | Invalid username or password |
| `INVALID_TOKEN` | 401 | Invalid or expired JWT token |
| `TOKEN_EXPIRED` | 401 | JWT token has expired |
| `VALIDATION_ERROR` | 400 | Request validation failed |
| `NO_INSTANCES_FOUND` | 404 | No instances found for namespace |
| `CREATE_INSTANCES_TASK_ERROR` | 500 | Failed to submit create instances task |
| `TASK_SUBMISSION_ERROR` | 500 | Failed to submit Celery task |
| `REDIS_ERROR` | 500 | Redis operation failed |
| `CONTAINERD_ERROR` | 500 | Containerd operation failed |

### Validation Error Response

When request validation fails:

```json
{
    "error": true,
    "error_code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": {
        "validation_errors": [
            {
                "field": "instance_type",
                "message": "field required",
                "type": "value_error.missing"
            },
            {
                "field": "min_count",
                "message": "ensure this value is greater than 0",
                "type": "value_error.number.not_gt"
            }
        ]
    }
}
```

---

## Response Format

### Success Response Format

All successful responses follow this format:

```json
{
    "error": false,
    "message": "Success message",
    "data": {
        // Response-specific data
    }
}
```

### Task-Based Endpoints

Most endpoints return a `task_id` that can be used to check task status:

```json
{
    "error": false,
    "message": "Task submitted successfully",
    "data": {
        "task_id": "abc123-def456-ghi789"
    }
}
```

Use the `/task/{task_id}` endpoint to retrieve the actual result.

---

## Rate Limiting

Currently, no rate limiting is implemented. Consider implementing rate limiting for production use.

---

## Best Practices

1. **Always check task status**: For async operations, use the returned `task_id` to check completion status
2. **Handle errors gracefully**: Check the `error` field in responses
3. **Token management**: Store and refresh tokens appropriately (30-minute expiration)
4. **Resource cleanup**: Use maintenance endpoints to clean up unused resources
5. **Namespace organization**: Use meaningful namespace names for better organization

---

## Interactive Documentation

Access interactive API documentation at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **OpenAPI JSON**: `http://localhost:8000/openapi.json`

---

*Last Updated: 2024*  
*API Version: 1.0.0*

