# Dibba - Lightweight Container Orchestration Platform

<div align="center">

![Dibba](https://img.shields.io/badge/Dibba-Container%20Orchestration-blue)
![Python](https://img.shields.io/badge/Python-3.8+-green)
![License](https://img.shields.io/badge/License-Apache%202.0-yellow)
![containerd](https://img.shields.io/badge/containerd-2.0+-orange)
![Status](https://img.shields.io/badge/Status-Production%20Ready-brightgreen)

**A production-ready, Python-based container orchestration platform built on containerd, Celery, and Redis**

[Website](https://www.dibba.cloud/) • [Documentation](#documentation) • [Features](#features) • [Quick Start](#quick-start) • [API Reference](#api-reference)

</div>

---

## 🌟 Overview

**Dibba** is a lightweight, high-performance container orchestration platform that provides Kubernetes-like capabilities without the complexity. Built with Python and leveraging containerd's native gRPC APIs, Dibba offers:

- **Native containerd integration** - Direct gRPC interface, no CRI overhead
- **Automatic pod scheduling** - Intelligent placement across worker nodes
- **Deployment management** - Auto-scaling, replica management, health checks
- **Cloud integration** - AWS EC2 instance provisioning and management
- **CNI networking** - Calico integration for pod networking
- **RESTful API** - FastAPI-based API with OpenAPI documentation
- **Web UI** - Modern dashboard for managing deployments and pods

## ✨ Key Features

### 🚀 Container Management
- **Pod-based architecture** with pause containers (Kubernetes-style)
- **Multi-container pods** with shared namespaces
- **Resource limits** - CPU and memory quotas
- **Volume mounts** - Persistent and ephemeral storage
- **Container logs** - Kubernetes-style log aggregation and rotation

### 📦 Deployment Management
- **Auto-scaling** - Automatic replica management (min/max replicas)
- **Health checks** - Liveness and readiness probes
- **Rolling updates** - Zero-downtime deployments
- **Deployment recovery** - Automatic pod recreation on node failures
- **Replica distribution** - Intelligent pod placement across nodes

### ☁️ Cloud Integration
- **AWS EC2 integration** - Automatic instance provisioning
- **Dynamic node management** - Scale worker nodes up/down
- **Multi-region support** - Deploy across AWS regions
- **Instance lifecycle** - Automatic cleanup of terminated instances

### 🔧 Networking
- **CNI integration** - Calico network plugin support
- **Pod networking** - Automatic IP assignment
- **Service discovery** - Internal DNS and service mesh ready
- **Network policies** - Calico network policy support

### 📊 Monitoring & Operations
- **Real-time metrics** - CPU, memory, and network stats
- **Host/pod synchronization** - Automatic state synchronization
- **Health monitoring** - Worker node health tracking
- **Log aggregation** - Centralized logging with rotation
- **etcd integration** - Calico node and IPAM management

### 🛡️ Reliability
- **Automatic recovery** - Self-healing deployments
- **Graceful shutdown** - Clean pod termination
- **Error handling** - Comprehensive error recovery
- **Task queuing** - Celery-based distributed task execution
- **State persistence** - Redis-based state management

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Dibba Control Plane                      │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   FastAPI    │  │   Celery     │  │   Celery     │      │
│  │   REST API   │  │   Beat       │  │   Workers    │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                  │                  │               │
│         └──────────────────┼──────────────────┘               │
│                            │                                   │
│                    ┌───────▼────────┐                         │
│                    │     Redis      │                         │
│                    │  State Store   │                         │
│                    └───────┬────────┘                         │
│                            │                                   │
└────────────────────────────┼───────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌──────▼──────────┐
│  Worker Node 1 │  │  Worker Node 2  │  │  Worker Node N  │
├────────────────┤  ├─────────────────┤  ├─────────────────┤
│  containerd    │  │   containerd    │  │   containerd    │
│  Calico CNI    │  │   Calico CNI    │  │   Calico CNI    │
│  Pods          │  │   Pods          │  │   Pods          │
└────────────────┘  └─────────────────┘  └─────────────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                    ┌────────▼─────────┐
                    │    etcd (Calico) │
                    │   Network State  │
                    └──────────────────┘
```

### Core Components

1. **API Server** (`server/main_api.py`)
   - FastAPI-based REST API
   - Authentication (OAuth2)
   - Deployment management
   - Pod operations
   - AWS integration

2. **Scheduler** (`server/sched/scheduler.py`)
   - Pod placement logic
   - Resource distribution
   - Deployment scheduling
   - Node selection

3. **Containerd Interface** (`utils/containerd/containerd_interface.py`)
   - Direct gRPC communication
   - Pod/container lifecycle
   - Image management
   - CNI networking integration

4. **Celery Tasks** (`utils/celery/tasks/`)
   - Deployment recovery
   - Health checks
   - Host/pod sync
   - AWS instance management
   - etcd cleanup

5. **Redis Store** (`utils/redis/`)
   - Host/pod information
   - Deployment state
   - Task tracking
   - Configuration cache

## 🚀 Quick Start

### Prerequisites

- **Python 3.8+**
- **containerd 2.0+** (running with gRPC socket at `/run/containerd/containerd.sock`)
- **Redis** (for state management)
- **etcd** (for Calico networking)
- **Calico CNI** (installed and configured)
- **AWS credentials** (optional, for EC2 integration)

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/tkcreddy/dibba.git
cd dibba
```

2. **Create virtual environment**
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Configure Dibba**
```bash
cp config/config.json.example config/config.json
# Edit config.json with your settings
```

5. **Start Redis**
```bash
redis-server
```

6. **Start containerd**
```bash
containerd
```

7. **Start Dibba services**

```bash
# Terminal 1: API Server
uvicorn server.main_api:app --host 0.0.0.0 --port 8080

# Terminal 2: Celery Beat (scheduler)
celery -A utils.celery.celery_config beat --loglevel=info

# Terminal 3: Celery Worker
celery -A utils.celery.worker_node worker --loglevel=info

# Terminal 4: AWS Worker (optional)
celery -A utils.celery.aws_worker worker --loglevel=info
```

8. **Access the Web UI**
```
http://localhost:8080/dibba/
```

### Using Docker Compose

```bash
docker-compose up -d
```

## 📖 Documentation

### API Documentation

Once the API server is running, access the interactive API documentation:

- **Swagger UI**: http://localhost:8080/docs
- **ReDoc**: http://localhost:8080/redoc
- **OpenAPI JSON**: http://localhost:8080/openapi.json

### Key Endpoints

#### Authentication
```bash
# Get access token
curl -X POST "http://localhost:8080/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin"
```

#### Deployments
```bash
# Create deployment from YAML
curl -X POST "http://localhost:8080/scheduler/deploy/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d @deployment.yaml

# List deployments
curl -X GET "http://localhost:8080/deployments/" \
  -H "Authorization: Bearer <token>"

# Scale deployment
curl -X PUT "http://localhost:8080/deployment/replicas/" \
  -H "Authorization: Bearer <token>" \
  -d '{"namespace": "production", "deployment": "my-app", "replicas": 5}'
```

#### Pods
```bash
# Create pod
curl -X POST "http://localhost:8080/containerd/create-pods/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"namespace": "default", "pod_name": "my-pod", ...}'

# List pods
curl -X POST "http://localhost:8080/containerd/list_namespaces_and_pods/" \
  -H "Authorization: Bearer <token>"
```

#### AWS Integration
```bash
# Create EC2 instances
curl -X POST "http://localhost:8080/create-instances/" \
  -H "Authorization: Bearer <token>" \
  -d '{"count": 3, "namespace": "production"}'

# List EC2 instances
curl -X GET "http://localhost:8080/aws/instances/" \
  -H "Authorization: Bearer <token>"
```

### Configuration

Configuration is managed via `config/config.json`:

```json
{
  "redis_db_config": {
    "redis_host": "localhost",
    "redis_port": 6379,
    "redis_db": 0
  },
  "aws_config": {
    "aws_access_key_id": "YOUR_KEY",
    "aws_secret_access_key": "YOUR_SECRET",
    "region": "us-east-1",
    "instance_type": "t3.medium",
    "ami_id": "ami-xxxxx",
    "key_name": "my-key",
    "security_group_ids": ["sg-xxxxx"],
    "subnet_id": "subnet-xxxxx"
  },
  "etcd_config": {
    "etcd_endpoints": "http://localhost:2379"
  },
  "encryption_config": {
    "key": "your-encryption-key"
  }
}
```

**Note**: AWS configuration can also be stored in Redis for dynamic updates using the `scripts/update_aws_config.py` script.

### Deployment Example

Create a deployment YAML file:

```yaml
apiVersion: v1
kind: Deployment
metadata:
  name: nginx-deployment
  namespace: production
spec:
  replicas: 3
  minReplicas: 2
  maxReplicas: 10
  selector:
    app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:latest
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
        livenessProbe:
          httpGet:
            path: /
            port: 80
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /
            port: 80
          initialDelaySeconds: 5
          periodSeconds: 5
```

Deploy it:
```bash
curl -X POST "http://localhost:8080/scheduler/deploy/" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d @deployment.yaml
```

## 🔧 Advanced Features

### Automatic Pod Recovery

Dibba automatically monitors deployments and recovers missing replicas:

- Runs every 10 seconds via Celery Beat
- Checks replica counts against min/max requirements
- Terminates excess pods if count exceeds max_replicas
- Creates missing pods if count is below min_replicas
- Distributes pods intelligently across available nodes

### Health Checks

Dibba supports Kubernetes-style health checks:

- **Liveness probes** - Restart unhealthy containers
- **Readiness probes** - Route traffic only to ready pods
- **HTTP probes** - Check HTTP endpoints
- **Periodic checking** - Configurable check intervals
- **Failure tracking** - Automatic retry and restart logic

### Worker Node Management

- **Automatic discovery** - Workers register automatically
- **Health monitoring** - Node health tracked in Redis
- **Resource tracking** - CPU and memory usage per node
- **Dynamic scaling** - Add/remove nodes on demand
- **AWS integration** - Automatic EC2 instance provisioning

### Storage Management

- **Persistent Volumes (PV)** - Long-term storage
- **Persistent Volume Claims (PVC)** - Dynamic storage requests
- **Storage Classes** - Different storage backends
- **Volume Mounts** - Attach storage to pods
- **Snapshot support** - Volume snapshots for backups

### Calico Integration

- **Automatic IP assignment** - Calico IPAM integration
- **Network policies** - Pod-to-pod communication rules
- **etcd synchronization** - Automatic Calico node cleanup
- **IPAM block management** - Automatic cleanup of orphaned IP blocks

## 🧪 Testing

Run the test suite:

```bash
# All tests
pytest

# With coverage
pytest --cov=. --cov-report=html

# Specific test file
pytest tests/test_api_endpoints.py
```

## 📦 Project Structure

```
dibba/
├── server/                 # API server and business logic
│   ├── main_api.py        # FastAPI application
│   ├── sched/             # Scheduler logic
│   └── nodes/             # Node distribution logic
├── utils/                  # Core utilities
│   ├── containerd/        # containerd interface
│   ├── celery/            # Celery tasks and workers
│   ├── redis/             # Redis stores and interfaces
│   ├── aws/               # AWS integration
│   ├── etcd/              # etcd interface
│   └── storage/           # Storage management
├── config/                 # Configuration files
├── scripts/                # Utility scripts
├── docs/                   # Documentation
├── tests/                  # Test suite
└── dibba-ui/              # Web UI (React/Vue)
```

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **containerd** - Container runtime foundation
- **Calico** - CNI networking plugin
- **FastAPI** - Modern Python web framework
- **Celery** - Distributed task queue
- **Redis** - In-memory data store

## 📞 Support

- **Documentation**: https://www.dibba.cloud/docs
- **Issues**: https://github.com/tkcreddy/dibba/issues
- **Discussions**: https://github.com/tkcreddy/dibba/discussions

## 🌐 Links

- **Website**: https://www.dibba.cloud/
- **GitHub**: https://github.com/tkcreddy/dibba
- **Documentation**: https://github.com/tkcreddy/dibba#readme

---

<div align="center">

**Built with ❤️ by the Dibba Team**

[⭐ Star us on GitHub](https://github.com/tkcreddy/dibba) | [🐛 Report Bug](https://github.com/tkcreddy/dibba/issues) | [💡 Request Feature](https://github.com/tkcreddy/dibba/issues)

</div>

