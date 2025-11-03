from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, PodManager, ResourceSpec,ContainerSpec
from typing import Optional, Dict, List, Tuple
from logpkg.log_kcld import LogKCld, log_to_file
from utils.ReadConfig import ReadConfig as rc
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.singleton import Singleton
import argparse
import os

# Defaults (can be overridden by task args or env)
DEFAULT_CONTAINERD_SOCKET = os.environ.get("CONTAINERD_SOCKET", "unix:///run/containerd/containerd.sock")
DEFAULT_NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "k8s.io")
DEFAULT_SNAPSHOTTER = os.environ.get("CONTAINERD_SNAPSHOTTER", "overlayfs")

CNI_BIN_DIR = os.environ.get("CNI_PATH", "/opt/cni/bin")
CNI_CONF_DIR = os.environ.get("CNI_CONF_DIR", "/etc/cni/net.d")
DEFAULT_CNI_NET_NAME = os.environ.get("CNI_NET_NAME", "calico")
DEFAULT_IFNAME = os.environ.get("CNI_IFNAME", "eth0")

parser = argparse.ArgumentParser(description='A Python CLI application')
parser.add_argument('--configDir', type=str, help='Please specify ConfigDir')
args = parser.parse_args()
read_config = rc(args.configDir)
key_read = read_config.encryption_config

logger = LogKCld()


@celery_app.task
@log_to_file(logger)
def create_pods(containers: List[ContainerSpec] = None,
    app_namespace: str = None,           # <- dynamic namespace
    cni_network: str = None,             # <- optional override
    cni_ifname: str = None,              # <- optional override
    containerd_socket: str = None,       # <- optional socket override
    **kwargs
):
    """
    Creates a pause 'pod' and one application container inside it.

    Dynamic knobs:
      - app_namespace: containerd namespace (defaults to env CONTAINERD_NAMESPACE or 'k8s.io')
      - containerd_socket: containerd gRPC target (defaults to env or unix:///run/containerd/containerd.sock)
      - cni_network / cni_ifname: override Calico network name and interface (defaults from env)
    """
    # Resolve dynamic values
    ns = app_namespace or DEFAULT_NAMESPACE
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    cni_net = cni_network or DEFAULT_CNI_NET_NAME
    cni_dev = cni_ifname or DEFAULT_IFNAME

    # # Basic validation
    # if not app_name:
    #     raise ValueError("app_name is required")
    # if not app_image:
    #     raise ValueError("app_image is required")
    # # app_cpu_limit here is treated as cpuset (e.g., "0-1"); keep behavior but clarify
    # app_cpuset = app_cpu_limit

    try:
        # Create a client bound to the requested namespace/socket
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

        # Create the application container in the pod
        apps = pods.add_containers(pod, containers)

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

