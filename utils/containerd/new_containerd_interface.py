import grpc
import uuid
import json
import os
import sys
import subprocess


sys.path.insert(0, './generated')

from generated.github.com.containerd.containerd.api.services.containers.v1 import containers_pb2
from generated.containerd.api.services.containers.v1 import containers_pb2_grpc
from generated.containerd.api.services.tasks.v1 import tasks_pb2_grpc
from generated.github.com.containerd.containerd.api.services.tasks.v1 import tasks_pb2
from generated.containerd.api.services.snapshots.v1 import  snapshots_pb2_grpc
from generated.github.com.containerd.containerd.api.services.snapshots.v1 import snapshots_pb2





class CNIClient:
    def __init__(self, plugin_dir="/opt/cni/bin", config_dir="/etc/cni/net.d"):
        self.plugin_dir = plugin_dir
        self.config_dir = config_dir
        self.plugin_bin = os.path.join(plugin_dir, "calico")
        self.config_file = self._load_config()

    def _load_config(self):
        files = [f for f in os.listdir(self.config_dir) if f.endswith(".conf") or f.endswith(".conflist")]
        if not files:
            raise FileNotFoundError("No CNI config found")
        return os.path.join(self.config_dir, files[0])

    def add(self, container_id, netns_path, ifname="eth0", extra_args=""):
        env = {
            "CNI_COMMAND": "ADD",
            "CNI_CONTAINERID": container_id,
            "CNI_NETNS": netns_path,
            "CNI_IFNAME": ifname,
            "CNI_PATH": self.plugin_dir,
            "CNI_ARGS": extra_args or f"K8S_POD_NAME={container_id};K8S_POD_NAMESPACE=default"
        }
        with open(self.config_file) as f:
            config = json.load(f)
        return self._exec_plugin(env, config)

    def del_(self, container_id, netns_path, ifname="eth0"):
        env = {
            "CNI_COMMAND": "DEL",
            "CNI_CONTAINERID": container_id,
            "CNI_NETNS": netns_path,
            "CNI_IFNAME": ifname,
            "CNI_PATH": self.plugin_dir
        }
        with open(self.config_file) as f:
            config = json.load(f)
        return self._exec_plugin(env, config)

    def _exec_plugin(self, env_vars, config):
        env = os.environ.copy()
        env.update(env_vars)
        proc = subprocess.Popen(
            [self.plugin_bin],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env
        )
        stdout, stderr = proc.communicate(json.dumps(config).encode())
        if proc.returncode != 0:
            raise RuntimeError(f"CNI plugin failed: {stderr.decode()}")
        return json.loads(stdout.decode())

class ContainerdInterface:
    def __init__(self, socket_path="/run/containerd/containerd.sock", namespace="default"):
        self.channel = grpc.insecure_channel(f"unix://{socket_path}")
        self.containers = containers_pb2_grpc.ContainersStub(self.channel)
        self.tasks = tasks_pb2_grpc.TasksStub(self.channel)
        self.snapshots = snapshots_pb2_grpc.SnapshotsStub(self.channel)
        self.images = images_pb2_grpc.ImagesStub(self.channel)
        self.metadata = (("containerd-namespace", namespace),)
        self.snapshotter = "overlayfs"
        self.cni = CNIClient()

    def ensure_image(self, image_name: str):
        try:
            self.images.Get(images_pb2.GetImageRequest(name=image_name), metadata=self.metadata)
            print(f"Image already exists locally: {image_name}")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                print(f"Pulling image: {image_name}")
                subprocess.run(["ctr", "images", "pull", image_name], check=True)  # Can be replaced with containerd gRPC pull if supported
            else:
                raise

    def create_pod(self, pod_id: str, image: str = "registry.k8s.io/pause:3.9", shared_volume: str = None) -> str:
        self.ensure_image(image)
        snapshot_key = f"{pod_id}-rootfs"
        prep_req = snapshots_pb2.PrepareSnapshotRequest(snapshotter=self.snapshotter, key=snapshot_key, parent=image)
        prep_resp = self.snapshots.PrepareSnapshot(prep_req, metadata=self.metadata)
        rootfs_mounts = prep_resp.mounts

        spec = self._generate_minimal_spec()

        container = containers_pb2.Container(
            id=pod_id,
            image=image,
            snapshotter=self.snapshotter,
            snapshot_key=snapshot_key,
            runtime=containers_pb2.Runtime(name="io.containerd.runc.v2"),
            spec=spec
        )
        self.containers.Create(containers_pb2.CreateContainerRequest(container=container), metadata=self.metadata)

        task_req = tasks_pb2.CreateTaskRequest(container_id=pod_id)
        task_req.rootfs.extend(rootfs_mounts)
        if shared_volume:
            task_req.rootfs.add().CopyFrom(mounts_pb2.Mount(
                type="bind",
                source=shared_volume,
                target=shared_volume,
                options=["rbind", "rw"]
            ))
        self.tasks.Create(task_req, metadata=self.metadata)

        task_info = self.tasks.Get(tasks_pb2.GetRequest(container_id=pod_id), metadata=self.metadata)
        pid = task_info.process.pid
        netns_path = f"/var/run/netns/{pod_id}"
        os.makedirs("/var/run/netns", exist_ok=True)
        if not os.path.exists(netns_path):
            os.symlink(f"/proc/{pid}/ns/net", netns_path)

        result = self.cni.add(container_id=pod_id, netns_path=netns_path)
        print("CNI Result:", result)

        self.tasks.Start(tasks_pb2.StartRequest(container_id=pod_id), metadata=self.metadata)
        return pod_id

    def add_container_to_pod(self, pod_id: str, image: str, container_id: str = None, shared_volume: str = None,
                              cpu: int = 0, memory: int = 0) -> str:
        self.ensure_image(image)
        container_id = container_id or str(uuid.uuid4())
        snapshot_key = f"{container_id}-rootfs"
        prep_req = snapshots_pb2.PrepareSnapshotRequest(snapshotter=self.snapshotter, key=snapshot_key, parent=image)
        prep_resp = self.snapshots.PrepareSnapshot(prep_req, metadata=self.metadata)
        rootfs_mounts = prep_resp.mounts

        spec = self._generate_shared_ns_spec(pod_id, cpu, memory)

        container = containers_pb2.Container(
            id=container_id,
            image=image,
            snapshotter=self.snapshotter,
            snapshot_key=snapshot_key,
            runtime=containers_pb2.Runtime(name="io.containerd.runc.v2"),
            spec=spec
        )
        self.containers.Create(containers_pb2.CreateContainerRequest(container=container), metadata=self.metadata)

        task_req = tasks_pb2.CreateTaskRequest(container_id=container_id)
        task_req.rootfs.extend(rootfs_mounts)
        if shared_volume:
            task_req.rootfs.add().CopyFrom(mounts_pb2.Mount(
                type="bind",
                source=shared_volume,
                target=shared_volume,
                options=["rbind", "rw"]
            ))
        self.tasks.Create(task_req, metadata=self.metadata)
        self.tasks.Start(tasks_pb2.StartRequest(container_id=container_id), metadata=self.metadata)
        return container_id

    def get_pod_ip(self, container_id: str) -> str:
        return "192.168.0.1"  # Placeholder

    def _generate_minimal_spec(self):
        spec_dict = {
            "ociVersion": "1.0.2",
            "process": {
                "args": ["/pause"],
                "cwd": "/",
                "terminal": False,
                "user": {"uid": 0, "gid": 0},
                "env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
            },
            "root": {"path": "."},
            "hostname": "pause-container",
            "mounts": [
                {"destination": "/proc", "type": "proc", "source": "proc"},
                {"destination": "/dev", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "strictatime", "mode=755"]}
            ]
        }
        any_spec = any_pb2.Any()
        any_spec.Pack(any_pb2.Any(value=json.dumps(spec_dict).encode("utf-8")))
        return any_spec

    def _generate_shared_ns_spec(self, pod_id: str, cpu: int, memory: int):
        return self._generate_minimal_spec()
