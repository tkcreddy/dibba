# Dibba

A lightweight, Python-based container orchestration layer built on:
- Celery for distributed task execution
- Redis for messaging and state
- Protobuf for typed RPC/data contracts
- containerd 2.0 for runtime management
- Calico for CNI networking

Dibba provides simple, fast, and secure orchestration primitives for pods/containers, networking, and worker lifecycle management with optional AWS integration.

## Features
- Pod and container lifecycle via containerd 2.0
- Calico-backed CNI networking (configurable)
- Celery task queue architecture with per-host routing
- Redis-backed state and coordination
- Protobuf-based interfaces and models
- Optional AWS EC2 worker provisioning and teardown
- REST API (FastAPI) for control-plane operations
- Structured logging and secure message routing

## Architecture
- Control Plane: FastAPI + Celery producers for task submission and monitoring
- Data Plane: Celery workers on hosts performing containerd and network operations
- Messaging: Redis as broker/DB; Kombu exchanges/queues for secure routing
- Networking: Calico CNI for pod networking
- Runtime: containerd (pause sandbox + app containers)
- Optional Cloud: AWS EC2 provisioning for worker nodes

## Prerequisites
- Python 3.13.x
- virtualenv
- Redis instance accessible to control plane and workers
- containerd (2.x) installed on worker nodes
- Calico CNI installed and configured on worker nodes
- Optional: AWS credentials for EC2 management

## Installation
- Create and activate a virtualenv
- Install dependencies from requirements.txt

Example:
- python -m venv .venv
- source .venv/bin/activate
- pip install -r requirements.txt

Note: This project uses virtualenv. Avoid other package managers unless explicitly required.

## Configuration
Common configuration areas:
- Redis: host, port, database
- AWS: access key, secret, region (if using cloud workers)
- Security: shared key for encoding/decoding secure routing tokens
- containerd: socket, namespace, snapshotter
- CNI: CNI_PATH, CNI_CONF_DIR, CNI_NET_NAME, CNI_IFNAME

Environment variables:
- CONTAINERD_SOCKET (default: unix:///run/containerd/containerd.sock)
- CONTAINERD_NAMESPACE (default: k8s.io)
- CONTAINERD_SNAPSHOTTER (default: overlayfs)
- CNI_PATH (default: /opt/cni/bin)
- CNI_CONF_DIR (default: /etc/cni/net.d)
- CNI_NET_NAME (default: calico)
- CNI_IFNAME (default: eth0)

## Running
Typical processes:
- API server (FastAPI)
- Celery workers (control/worker nodes)
- Optional Celery Beat for periodic tasks
- Optional Flower for monitoring

Examples:
- API: uvicorn main_api:app --host 0.0.0.0 --port 8000
- Workers/Beat/Flower: use provided scripts in the repository

Ensure:
- Redis is running and reachable
- Workers can reach Redis
- containerd and Calico are healthy on worker hosts

## API
Authentication:
- OAuth2 Password flow
- Token endpoint: POST /token
- Include bearer token for subsequent requests

Endpoints (summary):
- POST /create-instances: provision AWS EC2 workers (requires AWS config)
- POST /terminate-namespace: tear down all workers for a namespace
- GET /task/{task_id}: task status introspection
- GET /get_worker_node_data: request host info (routed to a specific worker)
- GET /get_worker_node_ip: request host IP (routed)
- GET /get_worker_usage_data: request host usage metrics (routed)

Notes:
- Per-host routing encodes the target host name into a secure queue name.
- Long-running actions execute via Celery workers; the API returns task IDs.

## Orchestration
Pods/Containers:
- Pause sandbox pod creation
- One or more application containers within the pod
- Resource hints (cpu, memory) via resource specifications
- CNI network attachment through Calico

Runtime controls:
- Namespace-scoped containerd client configuration
- Override containerd socket/namespace/snapshotter via env or task args
- Per-request network overrides (CNI net name, interface)

## AWS Workers (Optional)
- Create EC2 instances with custom AMI, instance type, networking
- Register instances in Redis under a logical namespace
- Terminate all instances for a given namespace

## Security
- API JWT for control-plane access
- Secure queue naming/routing using a shared key
- Restrict containerd socket access on workers
- Recommend TLS/auth for Redis in production

## Observability
- Structured logging across API and workers
- Celery task IDs for progress tracking
- Optional Flower dashboard
- Hooks available to export metrics

## Development
- Use the provided virtualenv for isolation
- Tasks live under utils/celery/tasks
- Utilities for containerd, Redis, Calico, and AWS are organized under utils/
- Follow existing task patterns for new operations
- Prefer protobuf contracts for cross-component compatibility

## Production Checklist
- Redis in HA mode or managed service with TLS/auth
- Harden API: proper identity provider, rotate secrets
- Lock down worker hosts and containerd socket
- Validate Calico policies per namespace
- Monitor Celery workers and queue depths
- Use dedicated VPC subnets/security groups for AWS workers

## Roadmap
- Multi-network attachment and IPAM customization
- Advanced scheduling and placement constraints
- Pluggable storage backends
- gRPC control-plane interface
- Fine-grained RBAC and audit logging

## License
Apache 2.0 (update to your actual license as needed)

## Support
- Issues: open a ticket in the repository
- Discussions: use the project’s discussion board
- Security: report vulnerabilities privately to maintainers

About
- Maintained by the Dibba contributors
- Contact: via repository issues
- Assistant: AI Assistant (software development assistant)