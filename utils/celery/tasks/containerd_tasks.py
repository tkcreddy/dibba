from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, PodManager
from typing import Optional, Dict, List, Any,Tuple
from logpkg.log_kcld import LogKCld, log_to_file
from utils.ReadConfig import ReadConfig as rc
import uuid
#from utils.containerd.models import ResourceSpec

from utils.containerd.schemas import ContainerSpec, ResourceSpec
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


def _rehydrate_containers(containers_json):
    """
    containers_json: List[dict] coming from FastAPI (JSON-serializable)
    Return: List[ContainerSpec] with nested ResourceSpec properly cast.
    """
    specs = []
    if not isinstance(containers_json, list):
        raise TypeError(f"'containers' must be a list of dicts, got {type(containers_json)}")

    for idx, item in enumerate(containers_json):
        if not isinstance(item, dict):
            raise TypeError(f"containers[{idx}] must be dict, got {type(item)}")

        d = dict(item)  # shallow copy
        # fix nested resources
        if "resources" in d and isinstance(d["resources"], dict):
            d["resources"] = ResourceSpec(**d["resources"])

        # (optional) normalize missing fields
        d.setdefault("env", None)
        d.setdefault("mounts", None)
        d.setdefault("args", None)

        specs.append(ContainerSpec(**d))
    return specs




@celery_app.task
@log_to_file(logger)
def create_pod_task(
                    containers,
                    app_namespace: Optional[str] = None,
                    **extra_kwargs):

    ns = app_namespace or "k8s.io"
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
            name=f"{uuid.uuid4().hex[:16]}",
            pause_image="registry.k8s.io/pause:3.9",
            resources=pause_resources,
            cni_network=cni_net,
            cni_ifname=cni_dev,
        )

        # app_defs = []
        # for c in containers:
        #     rs = c.get("resources", {})
        #     lr = linux_resources_from_spec(
        #         cpu_millicores=rs.get("cpu_millicores", 0),
        #         memory=rs.get("memory", "64Mi"),
        #         cpuset_cpus=rs.get("cpuset_cpus"),
        #     )
        #     app_defs.append({
        #         "name": c["name"],
        #         "image": c["image"],
        #         "args": c.get("args", []),
        #         "env": c.get("env") or {},
        #         "mounts": c.get("mounts") or [],
        #         "linux_resources": lr,             # <- give builder what it needs
        #     })

        #containers = [ContainerSpec(**c) for c in (containers or [])]
        #host_name = kwargs.get("host_name")  # avoid .get on strings
        container_specs = _rehydrate_containers(containers)
        # Create the application container in the pod
        apps = pods.add_containers(pod, container_specs)

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

