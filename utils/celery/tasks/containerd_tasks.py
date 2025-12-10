from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, PodManager
from typing import Optional, Dict, List, Any,Tuple
from logpkg.log_kcld import LogKCld, log_to_file
from utils.ReadConfig import ReadConfig as rc
import uuid
import json
import subprocess

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

@log_to_file(logger)
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

@log_to_file(logger)
def _extract_ipv4_from_cni_result(cni_result: dict, ifname: str = "eth0") -> Optional[str]:
    if not isinstance(cni_result, dict):
        return None

    # CNI 0.4+ often puts entries under "ips": [{"address": "192.168.0.10/32", "interface": 0, ...}]
    ips = cni_result.get("ips") or []
    for ip in ips:
        # some plugins include "version": "4", some only have "address"
        addr = ip.get("address")
        version = ip.get("version")
        if addr and (version == "4" or ":" not in addr):
            return addr.split("/", 1)[0]

    # Some plugins put the IP on an interface-level "address" list (less common)
    ifaces = cni_result.get("interfaces") or []
    for itf in ifaces:
        if itf.get("name") == ifname:
            for addr in (itf.get("addresses") or itf.get("address") or []):
                # address may be "a.b.c.d/xx"
                if isinstance(addr, str) and ":" not in addr:
                    return addr.split("/", 1)[0]
                if isinstance(addr, dict) and "address" in addr and ":" not in addr["address"]:
                    return addr["address"].split("/", 1)[0]
    return None

@log_to_file(logger)
def _ipv4_from_netns(pid: int, ifname: str = "eth0") -> Optional[str]:
    """
    Fallback: query the pod netns directly.
    Requires `nsenter` and `ip` (from iproute2). No named netns needed.
    """
    try:
        # ip -j addr show dev eth0 inside the pause PID’s netns
        out = subprocess.check_output(
            ["nsenter", f"--target={pid}", "--net", "ip", "-j", "addr", "show", "dev", ifname],
            text=True
        )
        data = json.loads(out)
        if not data:
            return None
        for ifc in data:
            for addr in ifc.get("addr_info", []):
                if addr.get("family") == "inet" and addr.get("local"):
                    return addr["local"]
    except Exception:
        pass
    return None


@celery_app.task
@log_to_file(logger)
def create_pod_task(containers, app_namespace: Optional[str] = None, **extra_kwargs):
    ns = app_namespace or DEFAULT_NAMESPACE
    sock = DEFAULT_CONTAINERD_SOCKET
    cni_net = DEFAULT_CNI_NET_NAME
    cni_dev = DEFAULT_IFNAME

    try:
        client = ContainerdClient(socket=sock, namespace=ns)
        pods = PodManager(client)

        pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
        pod = pods.create_pod(
            name=f"{uuid.uuid4().hex[:16]}",
            pause_image="registry.k8s.io/pause:3.9",
            resources=pause_resources,
            cni_network=cni_net,
            cni_ifname=cni_dev,
        )

        # 1) Try IPv4 from CNI result
        pod_ipv4 = _extract_ipv4_from_cni_result(pod.get("cni_result"), cni_dev)

        # 2) Fallback: read from the live netns of the pause container
        if not pod_ipv4:
            pause = pod.get("pause") or {}
            pause_pid = pause.get("pid")
            if isinstance(pause_pid, int) and pause_pid > 0:
                pod_ipv4 = _ipv4_from_netns(pause_pid, cni_dev)

        # Start app containers
        container_specs = _rehydrate_containers(containers)
        apps = pods.add_containers(pod, container_specs)

        return {
            "namespace": ns,
            "socket": sock,
            "cni": {"network": cni_net, "ifname": cni_dev},
            "pod": pod,
            "pod_ipv4": pod_ipv4,     # <- now should be populated
            "apps": apps
        }

    except Exception as err:
        return {"error": str(err), "namespace": ns, "socket": sock}

