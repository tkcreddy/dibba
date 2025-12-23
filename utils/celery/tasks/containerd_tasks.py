from utils.celery.celery_config import celery_app
from utils.containerd.containerd_interface import ContainerdClient, PodManager
from typing import Optional, Dict, List, Any
from logpkg.log_kcld import LogKCld, log_to_file
from utils.ReadConfig import ReadConfig as rc
import uuid
import json
import subprocess
import os
import shlex

from utils.containerd.schemas import ContainerSpec, ResourceSpec

logger = LogKCld()

# Defaults (can be overridden by task args or env)
DEFAULT_CONTAINERD_SOCKET = os.environ.get("CONTAINERD_SOCKET", "unix:///run/containerd/containerd.sock")
DEFAULT_NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "k8s.io")

DEFAULT_CNI_NET_NAME = os.environ.get("CNI_NET_NAME", "calico")
DEFAULT_IFNAME = os.environ.get("CNI_IFNAME", "eth0")

read_config = rc("./")
key_read = read_config.encryption_config


# ----------------- helpers you already started using -----------------

@log_to_file(logger)
def _rehydrate_containers(containers_json):
    specs = []
    if not isinstance(containers_json, list):
        raise TypeError(f"'containers' must be a list of dicts, got {type(containers_json)}")

    for idx, item in enumerate(containers_json):
        if not isinstance(item, dict):
            raise TypeError(f"containers[{idx}] must be dict, got {type(item)}")

        d = dict(item)
        if "resources" in d and isinstance(d["resources"], dict):
            d["resources"] = ResourceSpec(**d["resources"])

        d.setdefault("env", None)
        d.setdefault("mounts", None)
        d.setdefault("args", None)

        specs.append(ContainerSpec(**d))
    return specs


@log_to_file(logger)
def _extract_ipv4_from_cni_result(cni_result: dict, ifname: str = "eth0") -> Optional[str]:
    if not isinstance(cni_result, dict):
        return None
    ips = cni_result.get("ips") or []
    for ip in ips:
        addr = ip.get("address")
        version = ip.get("version")
        if addr and (version == "4" or ":" not in addr):
            return addr.split("/", 1)[0]
    ifaces = cni_result.get("interfaces") or []
    for itf in ifaces:
        if itf.get("name") == ifname:
            for addr in (itf.get("addresses") or itf.get("address") or []):
                if isinstance(addr, str) and ":" not in addr:
                    return addr.split("/", 1)[0]
                if isinstance(addr, dict) and "address" in addr and ":" not in addr["address"]:
                    return addr["address"].split("/", 1)[0]
    return None


@log_to_file(logger)
def _ipv4_from_netns(pid: int, ifname: str = "eth0") -> Optional[str]:
    try:
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


@log_to_file(logger)
def _safe_json(obj):
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {str(k): _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe_json(v) for v in obj]
    return str(obj)


# ----------------- CTR fallback (optional but super useful) -----------------

@log_to_file(logger)
def _ctr_task_list(namespace: str) -> List[Dict[str, str]]:
    """
    Return list of tasks from `ctr -n <ns> task list`.
    """
    cmd = f"ctr -n {namespace} task list"
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())

    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return []

    out = []
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) >= 3:
            out.append({"id": parts[0], "pid": parts[1], "status": parts[2]})
    return out


@log_to_file(logger)
def _ctr_task_kill_rm(namespace: str, task_id: str) -> Dict[str, Any]:
    """
    Kill (TERM,KILL) then rm. Works even when gRPC stubs differ.
    """
    killed = False
    for sig in ("SIGTERM", "SIGKILL"):
        p = subprocess.run(["ctr", "-n", namespace, "task", "kill", "--signal", sig, task_id],
                           capture_output=True, text=True)
        if p.returncode == 0:
            killed = True
    p = subprocess.run(["ctr", "-n", namespace, "task", "rm", task_id],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return {"ok": False, "killed": killed, "error": (p.stderr.strip() or p.stdout.strip())}
    return {"ok": True, "killed": killed}


# =========================
#   TASKS (missing set)
# =========================

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

        # -------------------------------------------------
        # 1) Pre-pull pause image via CRI (idempotent)
        # -------------------------------------------------
        pause_image = "registry.k8s.io/pause:3.9"
        pause_pull = pods.pull_image(pause_image)
        if not pause_pull.get("ok"):
            return {
                "error": pause_pull.get("message", f"Failed to pull pause image: {pause_image}"),
                "namespace": ns,
                "socket": sock,
            }

        # -------------------------------------------------
        # 2) Rehydrate container specs and pre-pull app images
        # -------------------------------------------------
        container_specs = _rehydrate_containers(containers)

        for spec in container_specs:
            img = getattr(spec, "image", None)
            if not img:
                continue
            img_pull = pods.pull_image(img)
            if not img_pull.get("ok"):
                return {
                    "error": img_pull.get("message", f"Failed to pull app image: {img}"),
                    "namespace": ns,
                    "socket": sock,
                    "failing_image": img,
                }

        # -------------------------------------------------
        # 3) Create pod (pause container + CNI attach)
        #    create_pod() will still call _ensure_unpacked(),
        #    which uses the same CRI pull path as a safety net.
        # -------------------------------------------------
        pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
        pod = pods.create_pod(
            name=f"{uuid.uuid4().hex[:16]}",
            pause_image=pause_image,
            resources=pause_resources,
            cni_network=cni_net,
            cni_ifname=cni_dev,
        )

        # -------------------------------------------------
        # 4) Extract pod IPv4 (from CNI result or via netns)
        # -------------------------------------------------
        pod_ipv4 = _extract_ipv4_from_cni_result(pod.get("cni_result"), cni_dev)
        if not pod_ipv4:
            pause = pod.get("pause") or {}
            pause_pid = pause.get("pid")
            if isinstance(pause_pid, int) and pause_pid > 0:
                pod_ipv4 = _ipv4_from_netns(pause_pid, cni_dev)

        # -------------------------------------------------
        # 5) Add app containers into the same pod namespaces
        # -------------------------------------------------
        apps = pods.add_containers(pod, container_specs)

        return _safe_json({
            "namespace": ns,
            "socket": sock,
            "cni": {"network": cni_net, "ifname": cni_dev},
            "pod": pod,
            "pod_ipv4": pod_ipv4,
            "apps": apps,
        })

    except Exception as err:
        return {
            "error": str(err),
            "namespace": ns,
            "socket": sock,
        }



@celery_app.task
@log_to_file(logger)
def list_namespaces_and_pods_task(
    containerd_socket: Optional[str] = None,
    bootstrap_namespace: Optional[str] = None,
) -> Dict[str, Any]:
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    ns = bootstrap_namespace or DEFAULT_NAMESPACE

    try:
        client = ContainerdClient(socket=sock)
        pod_mgr = PodManager(client)

        namespaces: List[str] = pod_mgr.runtime.list_all_namespaces()
        inventory: Dict[str, Any] = {}

        for n in namespaces:
            try:
                summaries = pod_mgr.runtime.list_pods_and_apps_in_namespace(n)
                inventory[n] = _safe_json(summaries)
            except Exception as e:
                inventory[n] = {"error": str(e)}

        return _safe_json({"namespaces": namespaces, "inventory": inventory})
    except Exception as err:
        return {"error": str(err), "socket": sock, "bootstrap_namespace": ns}

@celery_app.task
@log_to_file(logger)
def list_pods_by_namespace_task(namespace: str) -> Dict[str, Any]:
    sock =  DEFAULT_CONTAINERD_SOCKET
    ns =  DEFAULT_NAMESPACE

    try:
        client = ContainerdClient(socket=sock, namespace=namespace)
        pod_mgr = PodManager(client)

        namespaces: List[str] = pod_mgr.runtime.list_all_namespaces()
        inventory: Dict[str, Any] = {}
        if namespace not in namespaces:
            return {"error": f"Namespace {namespace} not found "}


        #for n in namespaces:
        n = namespace
        try:
            summaries = pod_mgr.runtime.list_pods_and_apps_in_namespace(n)
            inventory[n] = _safe_json(summaries)
        except Exception as e:
            inventory[n] = {"error": str(e)}

        return _safe_json({"inventory": inventory})
    except Exception as err:
        return {"error": str(err), "socket": sock, "bootstrap_namespace": ns}



@celery_app.task
@log_to_file(logger)
def terminate_pod_task(
    namespace: str,
    pod_name: str,
    cni_network: Optional[str] = None,
    ifname: Optional[str] = None,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls PodManager.terminate_pod(namespace, pod_name)
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    cni_net = cni_network or DEFAULT_CNI_NET_NAME
    dev = ifname or DEFAULT_IFNAME

    try:
        client = ContainerdClient(socket=sock)  # PodManager will open namespaced clients internally
        pods = PodManager(client)
        res = pods.terminate_pod(namespace=namespace, pod_name=pod_name, cni_network=cni_net, ifname=dev)
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "pod_name": pod_name}


@celery_app.task
@log_to_file(logger)
def terminate_pod_by_pause_cid_task(
    namespace: str,
    pause_cid: str,
    cni_network: Optional[str] = None,
    ifname: Optional[str] = None,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls PodManager.terminate_pod_by_cid(namespace, pause_cid) (hard cleanup).
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    cni_net = cni_network or DEFAULT_CNI_NET_NAME
    dev = ifname or DEFAULT_IFNAME

    try:
        client = ContainerdClient(socket=sock)
        pods = PodManager(client)
        res = pods.terminate_pod_by_cid(namespace=namespace, pause_cid=pause_cid,
                                        cni_network=cni_net, ifname=dev)
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "pause_cid": pause_cid}


@celery_app.task
@log_to_file(logger)
def destroy_all_pods_task(
    namespace: str,
    containerd_socket: Optional[str] = None,
    cni_network: Optional[str] = None,
    ifname: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls PodManager.terminate_pods_in_namespace(namespace)
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    cni_net = cni_network or DEFAULT_CNI_NET_NAME
    dev = ifname or DEFAULT_IFNAME

    try:
        client = ContainerdClient(socket=sock)
        pods = PodManager(client)
        # terminate_pods_in_namespace uses defaults; if you want to enforce cni args
        # you can loop list_pods_and_apps_in_namespace and call terminate_pod_task per pod.
        res = pods.terminate_pods_in_namespace(namespace)
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace}


@celery_app.task
@log_to_file(logger)
def destroy_container_by_id_task(
    namespace: str,
    cid: str,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    try:
        client = ContainerdClient(socket=sock)
        pods = PodManager(client)
        res = pods.destroy_container_by_id(namespace=namespace, cid=cid)
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "cid": cid}


@celery_app.task
@log_to_file(logger)
def purge_stopped_tasks_and_containers_task(
    namespace: str,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    try:
        client = ContainerdClient(socket=sock)
        pods = PodManager(client)
        res = pods.purge_stopped_tasks_and_containers(namespace)
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace}


@celery_app.task
@log_to_file(logger)
def prune_namespace_task(
    namespace: str,
    aggressive: bool = True,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls RuntimeManager.prune_namespace(namespace) which calls prune_orphan_tasks(aggressive=True).
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    try:
        client = ContainerdClient(socket=sock)
        pods = PodManager(client)
        res = pods.runtime.prune_namespace(namespace)  # your method already uses aggressive=True internally
        return _safe_json(res)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "aggressive": aggressive}


@celery_app.task
@log_to_file(logger)
def get_container_info_task(
    namespace: str,
    cid: str,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls RuntimeManager.get_container_info(cid)
    Note: get_container_info uses self.c (client namespace) so create client in that namespace.
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    try:
        client = ContainerdClient(socket=sock, namespace=namespace)
        pods = PodManager(client)
        info = pods.runtime.get_container_info(cid)
        return _safe_json(info)
    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "cid": cid}


@celery_app.task
@log_to_file(logger)
def cleanup_tasks_by_pod_prefix_task(
    namespace: str,
    pod_id: str,
    prefer_grpc: bool = True,
    containerd_socket: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Remove TASKS for pod_id in a namespace:
      - Matches pod_id and pod_id-*
    This is specifically for your problem where STOPPED tasks remain in `ctr task list`.

    prefer_grpc=True: try your gRPC delete_task_only() first, then CTR fallback.
    """
    sock = containerd_socket or DEFAULT_CONTAINERD_SOCKET
    removed, errors = [], []

    try:
        # 1) Try gRPC (fast / consistent with your code)
        if prefer_grpc:
            client = ContainerdClient(socket=sock)
            pods = PodManager(client)
            # Try delete_task_only on exact + by scanning Tasks.List prefix
            ids = [pod_id]
            try:
                ids.extend(pods._tasks_with_prefix(namespace, pod_id + "-"))
            except Exception:
                pass

            for tid in sorted(set(ids)):
                try:
                    # open a namespaced client then delete task
                    ns_client = pods.runtime._client_for_ns(namespace)
                    pods.runtime.c = ns_client
                    pods.runtime.delete_task_only(tid)
                    removed.append({"id": tid, "method": "grpc"})
                except Exception as e:
                    errors.append({"id": tid, "error": str(e), "method": "grpc"})

        # 2) CTR fallback: ensure they are gone from `ctr task list`
        try:
            tasks = _ctr_task_list(namespace)
            for t in tasks:
                tid = t["id"]
                if tid == pod_id or tid.startswith(pod_id + "-"):
                    r = _ctr_task_kill_rm(namespace, tid)
                    if r.get("ok"):
                        removed.append({"id": tid, "method": "ctr", "killed": r.get("killed", False)})
                    else:
                        errors.append({"id": tid, "method": "ctr", "error": r.get("error")})
        except Exception as e:
            errors.append({"method": "ctr_list", "error": str(e)})

        ok = len(errors) == 0
        return _safe_json({"ok": ok, "namespace": namespace, "pod_id": pod_id, "removed": removed, "errors": errors})

    except Exception as e:
        return {"ok": False, "error": str(e), "namespace": namespace, "pod_id": pod_id}
