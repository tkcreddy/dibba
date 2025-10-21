import grpc
import sys
import os
import subprocess
from google.protobuf import empty_pb2
sys.path.insert(0, './generated')

from generated.github.com.containerd.containerd.api.services.containers.v1 import containers_pb2
from generated.containerd.api.services.containers.v1 import containers_pb2_grpc
from generated.containerd.api.services.tasks.v1 import tasks_pb2_grpc
from generated.github.com.containerd.containerd.api.services.tasks.v1 import tasks_pb2
from generated.containerd.api.services.snapshots.v1 import  snapshots_pb2_grpc
from generated.github.com.containerd.containerd.api.services.snapshots.v1 import snapshots_pb2
from generated.containerd.api.services.images.v1 import images_pb2_grpc
from generated.github.com.containerd.containerd.api.services.images.v1 import images_pb2
from generated.github.com.containerd.containerd.api.types import mount_pb2
from google.protobuf import any_pb2
from pkg.apis.runtime.v1 import api_pb2, api_pb2_grpc
import uuid
import json


class CombinedContainerdInterface:
    def __init__(self, socket_path="/run/containerd/containerd.sock", namespace="default"):
        self.channel = grpc.insecure_channel(f"unix://{socket_path}")
        self.cri_stub = api_pb2_grpc.RuntimeServiceStub(self.channel)
        self.images = images_pb2_grpc.ImagesStub(self.channel)
        self.containers = containers_pb2_grpc.ContainersStub(self.channel)
        self.tasks = tasks_pb2_grpc.TasksStub(self.channel)
        self.snapshots = snapshots_pb2_grpc.SnapshotsStub(self.channel)
        self.metadata = (("containerd-namespace", namespace),)
        self.snapshotter = "overlayfs"

    def pull_image(self, image_name):
        try:
            pull_req = api_pb2.PullImageRequest(image=api_pb2.ImageSpec(image=image_name))
            pull_resp = self.cri_stub.PullImage(pull_req)
            print("[CRI] Pulled image:", pull_resp.image_ref)
        except grpc.RpcError as e:
            print("[CRI] Pull failed:", e.details(), e.code())
            raise

    def ensure_image(self, image_name):
        try:
            self.images.Get(images_pb2.GetImageRequest(name=image_name), metadata=self.metadata)
            print(f"[Containerd] Image already exists: {image_name}")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                self.pull_image(image_name)
            else:
                raise

    def create_pod_sandbox(self, name="demo", uid="demo123", namespace="default"):
        config = api_pb2.PodSandboxConfig(
            metadata=api_pb2.PodSandboxMetadata(name=name, uid=uid, namespace=namespace, attempt="1"),
            hostname=f"{name}-host"
        )
        req = api_pb2.RunPodSandboxRequest(config=config)
        resp = self.cri_stub.RunPodSandbox(req)
        print("[CRI] Pod Sandbox ID:", resp.pod_sandbox_id)
        return resp.pod_sandbox_id

    def stop_and_remove_pod(self, pod_id):
        try:
            self.cri_stub.StopPodSandbox(api_pb2.StopPodSandboxRequest(pod_sandbox_id=pod_id))
            self.cri_stub.RemovePodSandbox(api_pb2.RemovePodSandboxRequest(pod_sandbox_id=pod_id))
            print("[CRI] Pod sandbox stopped and removed.")
        except grpc.RpcError as e:
            print("[CRI] Error removing pod:", e.details())

    def create_container(self, container_id, image, spec=None, extra_mounts=None):
        self.ensure_image(image)
        snapshot_key = f"{container_id}-rootfs"
        prep_req = snapshots_pb2.PrepareSnapshotRequest(snapshotter=self.snapshotter, key=snapshot_key, parent=image)
        mounts = self.snapshots.PrepareSnapshot(prep_req, metadata=self.metadata).mounts

        container = containers_pb2.Container(
            id=container_id,
            image=image,
            snapshotter=self.snapshotter,
            snapshot_key=snapshot_key,
            runtime=containers_pb2.Runtime(name="io.containerd.runc.v2"),
            spec=spec or self._generate_minimal_spec()
        )
        self.containers.Create(containers_pb2.CreateContainerRequest(container=container), metadata=self.metadata)

        task_req = tasks_pb2.CreateTaskRequest(container_id=container_id)
        task_req.rootfs.extend(mounts)

        if extra_mounts:
            for host_path, container_path in extra_mounts:
                task_req.rootfs.add().CopyFrom(mounts_pb2.Mount(
                    type="bind",
                    source=host_path,
                    target=container_path,
                    options=["rbind", "rw"]
                ))

        self.tasks.Create(task_req, metadata=self.metadata)
        self.tasks.Start(tasks_pb2.StartRequest(container_id=container_id), metadata=self.metadata)
        print(f"[Containerd] Started container: {container_id}")
        return container_id

    def get_container_pid(self, container_id):
        task = self.tasks.Get(tasks_pb2.GetRequest(container_id=container_id), metadata=self.metadata)
        return task.process.pid

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
        packed = any_pb2.Any()
        packed.Pack(any_pb2.Any(value=json.dumps(spec_dict).encode("utf-8")))
        return packed
