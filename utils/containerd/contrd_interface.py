import grpc
from generated.github.com.containerd.containerd.api.services.containers.v1 import containers_pb2
from generated.containerd.api.services.containers.v1 import containers_pb2_grpc
#from containerd.services.containers.v1 import containers_pb2, containers_pb2_grpc
from generated.containerd.api.services.tasks.v1 import tasks_pb2_grpc
from generated.containerd.api.services.images.v1 import images_pb2_grpc
from generated.github.com.containerd.containerd.api.services.tasks.v1 import tasks_pb2
from generated.containerd.api.services.snapshots.v1 import  snapshots_pb2_grpc
from generated.github.com.containerd.containerd.api.services.snapshots.v1 import snapshots_pb2
from generated.github.com.containerd.containerd.api.services.images.v1 import images_pb2
from generated.github.com.containerd.containerd.api.types import mount_pb2
from generated.github.com.containerd.containerd.api.types.task import task_pb2

# from containerd.services.tasks.v1 import tasks_pb2, tasks_pb2_grpc
# from containerd.services.snapshots.v1 import snapshots_pb2, snapshots_pb2_grpc
# from containerd.types import mounts_pb2
from google.protobuf import any_pb2

class ContainerdInterface:
    def __init__(self, socket_path="/run/containerd/containerd.sock", namespace="default"):
        self.channel = grpc.insecure_channel(f"unix://{socket_path}")
        self.containers = containers_pb2_grpc.ContainersStub(self.channel)
        self.tasks = tasks_pb2_grpc.TasksStub(self.channel)
        self.snapshots = snapshots_pb2_grpc.SnapshotsStub(self.channel)
        self.metadata = (("containerd-namespace", namespace),)
        self.snapshotter = "overlayfs"

    def create_pod(self, pod_id: str, image: str = "registry.k8s.io/pause:3.9", shared_volume: str = None) -> str:
        snapshot_key = f"{pod_id}-rootfs"
        prep_req = snapshots_pb2.PrepareSnapshotRequest(snapshotter=self.snapshotter, key=snapshot_key, parent=image)
        prep_resp = self.snapshots.PrepareSnapshot(prep_req, metadata=self.metadata)
        rootfs_mounts = prep_resp.mounts

        spec = self._generate_pause_spec()

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
        self.tasks.Start(tasks_pb2.StartRequest(container_id=pod_id), metadata=self.metadata)
        return pod_id

    def add_container_to_pod(self, pod_id: str, image: str, container_id: str, shared_volume: str = None,
                              cpu: int = 0, memory: int = 0) -> str:
        snapshot_key = f"{container_id}-rootfs"
        prep_req = snapshots_pb2.PrepareSnapshotRequest(snapshotter=self.snapshotter, key=snapshot_key, parent=image)
        prep_resp = self.snapshots.PrepareSnapshot(prep_req, metadata=self.metadata)
        rootfs_mounts = prep_resp.mounts

        spec = self._generate_container_spec(pod_id, cpu, memory)

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
        # You can implement this using netns inspection or Calico CNI state
        return "192.168.0.1"  # Stub for example

    def _generate_pause_spec(self):
        # Returns a google.protobuf.Any object with minimal pause container OCI spec
        spec = any_pb2.Any()
        # Fill with actual OCI spec bytes (stubbed)
        return spec

    def _generate_container_spec(self, join_to: str, cpu: int, memory: int):
        spec = any_pb2.Any()
        # Fill with actual OCI spec bytes for a container joining namespaces of join_to
        return spec