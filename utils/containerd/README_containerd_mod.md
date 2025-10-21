# containerd_mod.py

A **modular containerd-native orchestrator** built in Python, leveraging gRPC APIs and OCI specs — no CRI or `ctr` CLI required.  
It supports pod-style grouping (pause container as sandbox), shared namespaces, Calico CNI network attachment, and CPU/memory resource limits.

---

## 🧩 Features

- **Direct gRPC interface to containerd**
  - Uses official protobuf APIs (`images`, `content`, `snapshots`, `containers`, `tasks`, `leases`, `diff`).
- **Pod model with pause container**
  - Mimics Kubernetes sandbox creation via pause image (`registry.k8s.io/pause:3.9`).
- **CNI Networking Integration**
  - Supports both `cnitool` and direct plugin invocation (Calico or others).
- **Resource Management**
  - CPU shares, quotas, and memory limits via OCI `resources` field.
- **Snapshot + Unpack logic**
  - Implements full layer unpack using `Diff.Apply` and `Snapshots.Commit`.
- **Platform auto-detection**
  - Works on both `linux/amd64` and `linux/arm64`.

---

## 🧠 Architecture Overview

```text
+---------------------+
|  PodManager         |
|  - create_pod()     |
|  - add_container()  |
+----------+----------+
           |
           v
+----------+----------+
| RuntimeManager      |   <-- handles CreateContainer + CreateTask + Start
+----------+----------+
           |
           v
+----------+----------+
| SnapshotManager     |   <-- handles Prepare/Commit snapshots, unpack layers
+----------+----------+
           |
           v
+----------+----------+
| ImageResolver       |   <-- resolves manifests, configs, diffIDs, chainIDs
+----------+----------+
           |
           v
+----------+----------+
| CniManager          |   <-- attaches pod via Calico / cnitool
+---------------------+
```

---

## ⚙️ Requirements

### 1. containerd gRPC environment
- containerd v2.0.5+ running
- `/run/containerd/containerd.sock` available

### 2. Python dependencies
```bash
pip install grpcio googleapis-common-protos protobuf
```

### 3. Generated gRPC stubs
Place containerd proto stubs under:
```
generated/api/services/{images,content,snapshots,containers,tasks,diff,leases}/v1/
generated/api/types/
```

### 4. CNI setup
Ensure:
```
CNI_PATH=/opt/cni/bin
CNI_CONF_DIR=/etc/cni/net.d
```
and that `/etc/cni/net.d/10-calico.conflist` exists with `"name": "calico"`.

---

## 🚀 Usage Example

```bash
python containerd_mod.py
```

Example output:
```text
Using platform: linux/amd64
Discovered snapshotter 'overlayfs'
[pause] args=['/pause']
✅ Pause pod up: cid=demo-pod-pause-a8c1b2, pid=3421
🌐 CNI attached: {'ips': ['192.168.0.12']}
🚀 App started: cid=demo-pod-nginx-77f912, pid=3460, image=docker.io/library/nginx:latest
```

---

## 🧾 Programmatic Usage

```python
from containerd_mod import ContainerdClient, PodManager, ResourceSpec

client = ContainerdClient()
pods = PodManager(client)

# Create pod with resource limits
pause_res = ResourceSpec(cpu_millicores=100, memory="64Mi")
pod = pods.create_pod("demo-pod", resources=pause_res)

# Add container inside pod
app_res = ResourceSpec(cpu_millicores=500, memory="256Mi")
pods.add_container(pod, name="nginx", image="docker.io/library/nginx:latest", resources=app_res)
```

---

## 🧱 Key Classes

| Class | Responsibility |
|--------|----------------|
| `ContainerdClient` | Connects to containerd gRPC services |
| `ImageResolver` | Reads manifests/configs, computes chainIDs |
| `SnapshotManager` | Creates, commits, and unpacks snapshots |
| `OciSpecBuilder` | Generates OCI runtime spec JSON |
| `CniManager` | Handles network attachment via Calico or cnitool |
| `RuntimeManager` | Creates containers and tasks |
| `PodManager` | Combines all managers to create pod-like sandboxes |

---

## 🧩 Extending

You can extend this orchestrator by adding:
- Volume mounts (under `mounts` in `OciSpecBuilder`)
- Additional namespaces (e.g., `user`, `cgroup`)
- Log streaming (`TasksService::Attach` gRPC stream)
- Cross-host pod scheduling with Redis or Celery workers

---

## 🧰 Environment Variables

| Variable | Default | Description |
|-----------|----------|-------------|
| `CONTAINERD_NAMESPACE` | `k8s.io` | Namespace for all containers |
| `CONTAINERD_SNAPSHOTTER` | `overlayfs` | Snapshot driver |
| `CNI_PATH` | `/opt/cni/bin` | Directory of CNI binaries |
| `CNI_CONF_DIR` | `/etc/cni/net.d` | Directory of CNI conflists |
| `CNI_NET_NAME` | `calico` | Network name to attach to |
| `CNI_IFNAME` | `eth0` | Interface name inside container |

---

## 📚 References

- [containerd gRPC API](https://github.com/containerd/containerd/tree/main/api)
- [OCI Runtime Spec](https://github.com/opencontainers/runtime-spec)
- [CNI Specification](https://github.com/containernetworking/cni)
- [Calico Networking Docs](https://projectcalico.docs.tigera.io/)

---

## 🧑‍💻 Author
**Krishna Reddy**  
Cloud-Native Systems & SRE Engineering  
`containerd` • `Calico` • `Python` • `OCI` • `gRPC`
