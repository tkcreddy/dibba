from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, PodManager
from typing import Optional, Dict, List, Tuple
from logpkg.log_kcld import LogKCld, log_to_file
from utils.ReadConfig import ReadConfig as rc
from utils.containerd.models import ResourceSpec
from utils.containerd.schemas import ContainerSpec
from utils.containerd.adapters import linux_resources_from_spec
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.singleton import Singleton
import os

# Defaults (can be overridden by task args or env)
DEFAULT_CONTAINERD_SOCKET = os.environ.get("CONTAINERD_SOCKET", "unix:///run/containerd/containerd.sock")
DEFAULT_NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "k8s.io")
DEFAULT_SNAPSHOTTER = os.environ.get("CONTAINERD_SNAPSHOTTER", "overlayfs")

CNI_BIN_DIR = os.environ.get("CNI_PATH", "/opt/cni/bin")
CNI_CONF_DIR = os.environ.get("CNI_CONF_DIR", "/etc/cni/net.d")
DEFAULT_CNI_NET_NAME = os.environ.get("CNI_NET_NAME", "calico")
DEFAULT_IFNAME = os.environ.get("CNI_IFNAME", "eth0")

# parser = argparse.ArgumentParser(description='A Python CLI application')
# parser.add_argument('--configDir', type=str, help='Please specify ConfigDir')
# args = parser.parse_args()
read_config = rc("./")
key_read = read_config.encryption_config

logger = LogKCld()



@celery_app.task
@log_to_file(logger)
def create_pod_task(self,  containers: list[dict], app_namespace: str = None, **kwargs):
    # Rebuild objects only if your PodManager expects typed objects

    # typed = []
    # for c in containers:
    #     # Defensive: handle missing resources
    #     if "resources" in c and isinstance(c["resources"], dict):
    #         c["resources"] = ResourceSpec(**c["resources"])
    #     typed.append(ContainerSpec(**c))
    # Resolve dynamic values
    ns = app_namespace or DEFAULT_NAMESPACE
    sock =  DEFAULT_CONTAINERD_SOCKET
    cni_net = DEFAULT_CNI_NET_NAME
    cni_dev =  DEFAULT_IFNAME

    # # Basic validation
    # if not app_name:
    #     raise ValueError("app_name is required")
    # if not app_image:
    #     raise ValueError("app_image is required")
    # # app_cpu_limit here is treated as cpuset (e.g., "0-1"); keep behavior but clarify
    # app_cpuset = app_cpu_limit

    try:
        client = ContainerdClient(socket=sock, namespace=ns)
        pods = PodManager(client)

        # Create the pause sandbox (pod)
        pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
        pod = pods.create_pod(
            name="pause",
            pause_image="registry.k8s.io/pause:3.9",
            resources=pause_resources,
            cni_network=cni_net,
            cni_ifname=cni_dev,
        )

        app_defs = []
        for c in containers:
            rs = c.get("resources", {})
            lr = linux_resources_from_spec(
                cpu_millicores=rs.get("cpu_millicores", 0),
                memory=rs.get("memory", "64Mi"),
                cpuset_cpus=rs.get("cpuset_cpus"),
            )
            app_defs.append({
                "name": c["name"],
                "image": c["image"],
                "args": c.get("args", []),
                "env": c.get("env") or {},
                "mounts": c.get("mounts") or [],
                "linux_resources": lr,             # <- give builder what it needs
            })


        # Create the application container in the pod
        apps = pods.add_containers(pod, app_defs)

        # Return simple, JSON-serializable data for Celery
        return {
            "namespace": ns,
            "socket": sock,
            "cni": {"network": cni_net, "ifname": cni_dev},
            "pod": pod,
            "apps": apps
        }

    except Exception as err:
        # Let decorator log; still return a structured error for callers
        return {"error": str(err), "namespace": ns, "socket": sock}

