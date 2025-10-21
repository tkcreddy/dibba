"""
containerd_mod.py
A modular containerd-native gRPC orchestrator with CPU/Memory resources + Calico CNI attach.

Requires:
  - generated/ stubs for containerd v2 services (images, content, snapshots, containers, tasks, dif
f, leases)
  - CNI binaries installed (e.g. /opt/cni/bin) and a Calico conflist in /etc/cni/net.d
  - Optional: 'cnitool' in PATH to drive the CNI conflist by name
"""

import os
import json
import uuid
import hashlib
import grpc
import subprocess
from shutil import which
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

from google.protobuf import any_pb2
from google.protobuf.json_format import ParseDict

# ----- containerd native gRPC stubs -----
from generated.api.services.images.v1 import images_pb2, images_pb2_grpc
from generated.api.services.content.v1 import content_pb2, content_pb2_grpc
from generated.api.services.snapshots.v1 import snapshots_pb2, snapshots_pb2_grpc
from generated.api.services.containers.v1 import containers_pb2, containers_pb2_grpc
from generated.api.services.tasks.v1 import tasks_pb2, tasks_pb2_grpc
from generated.api.types import descriptor_pb2
from generated.runtime.v1 import api_pb2, api_pb2_grpc

# diff + leases for gRPC-only unpack
from generated.api.services.diff.v1 import diff_pb2, diff_pb2_grpc
from generated.api.services.leases.v1 import leases_pb2, leases_pb2_grpc


# ---- add this helper near your imports ----
def _normalize_unix_target(sock: str) -> str:
    """
    Accepts either:
      - '/run/containerd/containerd.sock' (plain path)
      - 'unix:///run/containerd/containerd.sock' (already normalized)
      - 'unix://run/containerd/containerd.sock' (rare)
    and returns a valid gRPC target 'unix:///run/containerd/containerd.sock'
    """
    if not sock:
        raise ValueError("socket path/target is empty")

    if sock.startswith("unix://"):
        # Make sure it has three slashes total (scheme + absolute path)
        # 'unix:///...' is correct; 'unix://run/...' is not (missing leading /)
        after = sock[len("unix://"):]
        if after.startswith("/"):
            return sock  # already 'unix:///...'
        return "unix:///" + after  # fix missing slash
    else:
        # Treat as filesystem path
        if not sock.startswith("/"):
            # Defensive: if someone passed 'run/containerd/containerd.sock'
            sock = "/" + sock
        return "unix://" + sock  # will yield 'unix:///...'


# ---------- Config ----------
CONTAINERD_SOCKET = "unix:///run/containerd/containerd.sock"
NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "k8s.io")
DEFAULT_SNAPSHOTTER = os.environ.get("CONTAINERD_SNAPSHOTTER", "overlayfs")
OCI_SPEC_TYPEURL = "types.containerd.io/opencontainers/runtime-spec/1/Spec"

# CNI defaults (override via env as needed)
CNI_BIN_DIR = os.environ.get("CNI_PATH", "/opt/cni/bin")
CNI_CONF_DIR = os.environ.get("CNI_CONF_DIR", "/etc/cni/net.d")
DEFAULT_CNI_NET_NAME = os.environ.get("CNI_NET_NAME", "calico")  # must match conflist "name"
DEFAULT_IFNAME = os.environ.get("CNI_IFNAME", "eth0")

# --- platform auto-detect (overridden if FORCE_PLATFORM is set) ---
def _detect_platform() -> Tuple[str, str]:
    m = os.uname().machine.lower()
    arch_map = {
        "x86_64": "amd64", "amd64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
        "armv7l": "arm", "armv6l": "arm",
        "ppc64le": "ppc64le", "s390x": "s390x",
    }
    return ("linux", arch_map.get(m, m or "amd64"))

PLATFORM_OS, PLATFORM_ARCH = (
    os.environ.get("FORCE_PLATFORM_OS", None),
    os.environ.get("FORCE_PLATFORM_ARCH", None),
)
if not PLATFORM_OS or not PLATFORM_ARCH:
    PLATFORM_OS, PLATFORM_ARCH = _detect_platform()

# ----- Media types -----
OCI_INDEX   = "application/vnd.oci.image.index.v1+json"
OCI_MANIF   = "application/vnd.oci.image.manifest.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MAN  = "application/vnd.docker.distribution.manifest.v2+json"
ANNOTATION_UNCOMPRESSED = "containerd.io/uncompressed"

def _is_index(mt: str) -> bool:
    return mt.endswith("image.index.v1+json") or mt == DOCKER_LIST

def _is_manifest(mt: str) -> bool:
    return mt.endswith("image.manifest.v1+json") or mt == DOCKER_MAN

def ns_md(extra=None) -> Tuple[Tuple[str,str], ...]:
    md = [("containerd-namespace", NAMESPACE)]
    if extra:
        md.extend(extra)
    return tuple(md)

# ========== Utilities ==========
def _candidates_for_ref(ref: str) -> List[str]:
    out = {ref}
    last = ref.split("/")[-1]
    if "@" not in last and ":" not in last:
        out.add(ref + ":latest")
        ref = ref + ":latest"
    parts = ref.split("/")
    if len(parts) == 1:
        out.add(f"docker.io/library/{ref}")
    elif "." not in parts[0] and ":" not in parts[0]:
        out.add(f"docker.io/{ref}")
    out.add(ref.replace("registry.k8s.io/", "k8s.gcr.io/"))
    out.add(ref.replace("k8s.gcr.io/", "registry.k8s.io/"))
    return list(out)

def _read_blob_json(content_stub, digest: str, extra_md=None) -> dict:
    stream = content_stub.Read(content_pb2.ReadContentRequest(digest=digest), metadata=ns_md(extra_md))
    data = b"".join(part.data for part in stream if part.data)
    return json.loads(data.decode("utf-8"))

def _compute_chain_id(diff_ids: List[str]) -> str:
    if not diff_ids:
        raise ValueError("chainID needs at least one diff_id")
    chain = diff_ids[0]
    for d in diff_ids[1:]:
        h = hashlib.sha256()
        h.update(chain.encode("utf-8"))
        h.update(b" ")
        h.update(d.encode("utf-8"))
        chain = f"sha256:{h.hexdigest()}"
    return chain

def _parse_bytes(value: str | int | None) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4,
             "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
             "ki": 1024, "mi": 1024**2, "gi": 1024**3, "ti": 1024**4}
    num = ""; suf = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            suf += ch
    if not num:
        return None
    if not suf:
        return int(float(num))
    mul = units.get(suf, None)
    if mul is None:
        mul = units.get(suf[-1], 1)
    return int(float(num) * mul)

def _mcores_to_quota_period(millicores: int, period_us: int = 100_000) -> Tuple[int, int]:
    if millicores <= 0:
        return (0, period_us)
    quota = int(period_us * (millicores / 1000.0))
    quota = max(quota, 1000)
    return (quota, period_us)

def _mcores_to_shares(millicores: int) -> int:
    if millicores <= 0:
        return 2
    shares = int(1024 * (millicores / 1000.0))
    return max(2, shares)

# ========== Data specs ==========
@dataclass
class ResourceSpec:
    """
    CPU/memory constraints for a container.
    - cpu_millicores: e.g., 500 -> ~0.5 CPU. Sets both shares and CFS quota.
    - memory: bytes OR a string like "256Mi"
    - cpuset_cpus: e.g., "0-1,3"
    """
    cpu_millicores: Optional[int] = None
    memory: Optional[str | int] = None
    cpuset_cpus: Optional[str] = None

    def to_linux_resources_dict(self) -> Dict:
        cpu: Dict = {}
        mem: Dict = {}
        if self.cpu_millicores is not None:
            shares = _mcores_to_shares(self.cpu_millicores)
            quota, period = _mcores_to_quota_period(self.cpu_millicores, 100_000)
            cpu["shares"] = shares
            cpu["quota"] = quota
            cpu["period"] = period
        if self.cpuset_cpus:
            cpu["cpus"] = self.cpuset_cpus
        mem_bytes = _parse_bytes(self.memory) if self.memory is not None else None
        if mem_bytes is not None and mem_bytes > 0:
            mem["limit"] = mem_bytes
        res = {}
        if cpu: res["cpu"] = cpu
        if mem: res["memory"] = mem
        return res
# ---- Content / CRI helpers ----
def _blob_exists(content_stub, dgst: str, retries: int = 3, sleep_sec: float = 0.25) -> bool:
    # Use Content.Info which returns NOT_FOUND if the blob is absent under the current namespace.
    for i in range(retries + 1):
        try:
            content_stub.Info(content_pb2.InfoRequest(digest=dgst), metadata=ns_md())
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                if i < retries:
                    time.sleep(sleep_sec)
                    continue
                return False
            # for other errors, surface them
            raise

class _CRIImageClient:
    def __init__(self, socket_target="/run/containerd/containerd.sock"):
        # Accept either a path or a 'unix://...' target
        target = _normalize_unix_target(socket_target)
        self.channel = grpc.insecure_channel(target)
        self.stub = api_pb2_grpc.ImageServiceStub(self.channel)

    def pull(self, image_ref: str) -> str | None:
        try:
            resp = self.stub.PullImage(api_pb2.PullImageRequest(
                image=api_pb2.ImageSpec(image=image_ref)
            ))
            return resp.image_ref
        except grpc.RpcError as e:
            print(f"[cri] PullImage error: {e.code().name}: {e.details()}")
            return None

    def image_status(self, image_ref: str) -> str | None:
        try:
            st = self.stub.ImageStatus(api_pb2.ImageStatusRequest(
                image=api_pb2.ImageSpec(image=image_ref)
            ))
            if st.image and st.image.id:
                return st.image.id
        except grpc.RpcError as e:
            print(f"[cri] ImageStatus error: {e.code().name}: {e.details()}")
        return None
# ========== Client ==========
class ContainerdClient:
    def __init__(self,
                 socket: str = CONTAINERD_SOCKET,
                 namespace: str = NAMESPACE):
        self.socket = socket
        self.namespace = namespace
        self.channel = grpc.insecure_channel(socket)
        self.images = images_pb2_grpc.ImagesStub(self.channel)
        self.content = content_pb2_grpc.ContentStub(self.channel)
        self.snapshots = snapshots_pb2_grpc.SnapshotsStub(self.channel)
        self.containers = containers_pb2_grpc.ContainersStub(self.channel)
        self.tasks = tasks_pb2_grpc.TasksStub(self.channel)
        self.diff = diff_pb2_grpc.DiffStub(self.channel)
        self.leases = leases_pb2_grpc.LeasesStub(self.channel)

# ========== Image Resolution ==========
class ImageResolver:
    def __init__(self, client: ContainerdClient):
        self.c = client

    def resolve_image_name(self, wanted: str) -> str:
        for cand in _candidates_for_ref(wanted):
            try:
                self.c.images.Get(images_pb2.GetImageRequest(name=cand), metadata=ns_md())
                return cand
            except grpc.RpcError as e:
                if e.code() != grpc.StatusCode.NOT_FOUND:
                    raise
        raise RuntimeError(f"Image {wanted} not found in namespace {NAMESPACE}")

    def resolve_manifest(self, image_ref: str, extra_md=None) -> descriptor_pb2.Descriptor:
        resolved = self.resolve_image_name(image_ref)
        img = self.c.images.Get(images_pb2.GetImageRequest(name=resolved), metadata=ns_md(extra_md)
).image
        tgt = img.target
        if _is_index(tgt.media_type):
            idx = _read_blob_json(self.c.content, tgt.digest, extra_md)
            for m in idx.get("manifests", []):
                plat = m.get("platform", {}) or {}
                if plat.get("os") == PLATFORM_OS and plat.get("architecture") == PLATFORM_ARCH:
                    d = descriptor_pb2.Descriptor()
                    ParseDict({
                        "media_type": m.get("mediaType") or m.get("media_type"),
                        "digest": m["digest"],
                        "size": m["size"]
                    }, d)
                    return d
            m = (idx.get("manifests") or [])[0]
            d = descriptor_pb2.Descriptor()
            ParseDict({
                "media_type": m.get("mediaType") or m.get("media_type"),
                "digest": m["digest"],
                "size": m["size"]
            }, d)
            return d
        if _is_manifest(tgt.media_type):
            return tgt
        raise RuntimeError(f"Unsupported target media type: {tgt.media_type}")

    def load_manifest_and_config(self, manifest_desc, extra_md=None):
        manifest = _read_blob_json(self.c.content, manifest_desc.digest, extra_md)
        cfg_digest = manifest["config"]["digest"]
        config = _read_blob_json(self.c.content, cfg_digest, extra_md)
        return manifest, config

    def chain_id_for_image(self, image_ref: str) -> str:
        md = None
        manifest_desc = self.resolve_manifest(image_ref, md)
        _, cfg = self.load_manifest_and_config(manifest_desc, md)
        diff_ids = (cfg.get("rootfs") or {}).get("diff_ids", [])
        if not diff_ids:
            raise RuntimeError(f"No diff_ids in config for {image_ref}")
        return _compute_chain_id(diff_ids)

# ========== Snapshot / Unpack ==========
class SnapshotManager:
    def __init__(self, client: ContainerdClient, default_snapshotter: str = DEFAULT_SNAPSHOTTER):
        self.c = client
        self._snapshotter_value_cache: Optional[str] = None
        self.default_snapshotter = default_snapshotter

    def _snapshotter_candidates(self) -> List[str]:
        raw = []
        if self.default_snapshotter:
            raw.append(self.default_snapshotter)
        raw += ["overlayfs", "native", "btrfs", "zfs", "stargz"]
        seen = set(); raw = [x for x in raw if not (x in seen or seen.add(x))]
        full = [f"io.containerd.snapshotter.v1.{name}" for name in raw]
        return raw + full

    def prepare_rw_snapshot(self, parent_chain_id: str, key_hint: str, extra_md=None) -> Tuple[List
, str]:
        key = f"{key_hint}-{uuid.uuid4().hex[:8]}"

        if self._snapshotter_value_cache:
            try:
                req = snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=self._snapshotter_value_cache, key=key, parent=parent_chain_id,
                    labels={"containerd.io/gc.root": "true"},
                )
                resp = self.c.snapshots.Prepare(req, metadata=ns_md(extra_md))
                print(f"Using snapshotter '{self._snapshotter_value_cache}'")
                return list(resp.mounts), key
            except grpc.RpcError:
                pass

        for snap_val in self._snapshotter_candidates():
            try:
                req = snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=snap_val, key=key, parent=parent_chain_id,
                    labels={"containerd.io/gc.root": "true"},
                )
                resp = self.c.snapshots.Prepare(req, metadata=ns_md(extra_md))
                self._snapshotter_value_cache = snap_val
                print(f"Discovered snapshotter '{snap_val}'")
                return list(resp.mounts), key
            except grpc.RpcError:
                continue
        raise RuntimeError("Unable to select snapshotter for containerd Snapshots API.")

    def _snap_stat_exists(self, snapshotter: str, key_or_name: str, extra_md=None) -> bool:
        try:
            self.c.snapshots.Stat(
                snapshots_pb2.StatSnapshotRequest(snapshotter=snapshotter, key=key_or_name),
                metadata=ns_md(extra_md)
            )
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    def _snap_remove_active(self, snapshotter: str, key: str, extra_md=None):
        try:
            self.c.snapshots.Remove(
                snapshots_pb2.RemoveSnapshotRequest(snapshotter=snapshotter, key=key),
                metadata=ns_md(extra_md)
            )
        except grpc.RpcError:
            pass

    def grpc_unpack(self, image_ref: str, manifest: dict, cfg: dict, snapshotter: str):
        layers = manifest.get("layers", [])
        diff_ids = (cfg.get("rootfs") or {}).get("diff_ids", [])
        if len(diff_ids) != len(layers):
            raise RuntimeError("layers vs diff_ids length mismatch; cannot compute chainIDs.")

        parent_chain = ""
        for i, layer in enumerate(layers):
            cur_chain = _compute_chain_id(diff_ids[:i+1])

            if self._snap_stat_exists(snapshotter, cur_chain, None):
                parent_chain = cur_chain
                continue

            prep_key = f"unpack-{uuid.uuid4().hex[:8]}-{i}"
            prep = self.c.snapshots.Prepare(
                snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=snapshotter,
                    key=prep_key,
                    parent=parent_chain or "",
                    labels={"containerd.io/gc.root": "true"},
                ),
                metadata=ns_md()
            )
            mounts = list(prep.mounts)

            d = descriptor_pb2.Descriptor()
            ParseDict({
                "media_type": layer.get("mediaType") or layer.get("media_type"),
                "digest": layer["digest"],
                "size": layer.get("size", 0),
                "annotations": { ANNOTATION_UNCOMPRESSED: diff_ids[i] }
            }, d)

            self.c.diff.Apply(diff_pb2.ApplyRequest(diff=d, mounts=mounts), metadata=ns_md())

            try:
                self.c.snapshots.Commit(
                    snapshots_pb2.CommitSnapshotRequest(
                        snapshotter=snapshotter, name=cur_chain, key=prep_key,
                        labels={"containerd.io/gc.root": "true"},
                    ),
                    metadata=ns_md()
                )
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                    self._snap_remove_active(snapshotter, prep_key, None)
                else:
                    self._snap_remove_active(snapshotter, prep_key, None)
                    raise

            parent_chain = cur_chain

        return parent_chain
    def _new_lease(self, id_hint: str = "unpack") -> leases_pb2.Lease:
        lid = f"{id_hint}-{uuid.uuid4().hex[:8]}"
        resp = self.c.leases.Create(
            leases_pb2.CreateRequest(id=lid, labels={"containerd.io/gc.root": "true"}),
            metadata=ns_md())
        return resp.lease

    def _delete_lease(self, lease_id: str):
        try:
            self.c.leases.Delete(leases_pb2.DeleteRequest(id=lease_id), metadata=ns_md())
        except grpc.RpcError:
            pass


# ========== OCI Spec Builder ==========
class OciSpecBuilder:
    def __init__(self, hostname: Optional[str] = None):
        self.hostname = hostname or ""

    def build(self,
              process_args: List[str],
              env: Optional[Dict[str, str]] = None,
              namespaces: Optional[List[Dict]] = None,
              resources: Optional[ResourceSpec] = None,
              cwd: str = "/",
              root_readonly: bool = False) -> any_pb2.Any:

        default_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        }
        merged_env = dict(default_env)
        if env:
            merged_env.update(env)

        spec = {
            "ociVersion": "1.1.0",
            "process": {
                "terminal": False,
                "cwd": cwd,
                "args": process_args,
                "env": [f"{k}={v}" for k, v in merged_env.items()],
                "capabilities": {
                    "bounding": [
                        "CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FSETID","CAP_FOWNER","CAP_MKNOD",
                        "CAP_NET_RAW","CAP_SETGID","CAP_SETUID","CAP_SETFCAP","CAP_SETPCAP",
                        "CAP_NET_BIND_SERVICE","CAP_SYS_CHROOT","CAP_KILL","CAP_AUDIT_WRITE"
                    ]
                }
            },
            "root": {"path": "rootfs", "readonly": root_readonly},
            "hostname": self.hostname,
            "mounts": [
                {"destination": "/proc", "type": "proc", "source": "proc"},
                {"destination": "/dev", "type": "tmpfs", "source": "tmpfs",
                 "options": ["nosuid","strictatime","mode=755","size=65536k"]},
                {"destination": "/dev/pts", "type": "devpts", "source": "devpts",
                 "options": ["nosuid","noexec","newinstance","ptmxmode=0666","mode=0620","gid=5"]},
                {"destination": "/dev/shm", "type": "tmpfs", "source": "shm",
                 "options": ["nosuid","noexec","nodev","mode=1777","size=65536k"]},
                {"destination": "/sys", "type": "sysfs", "source": "sysfs",
                 "options": ["nosuid","noexec","nodev","ro"]},
                {"destination": "/sys/fs/cgroup", "type": "cgroup", "source": "cgroup",
                 "options": ["nosuid","noexec","nodev","relatime","ro"]},
            ],
            "linux": {
                "namespaces": [],
                "resources": {}
            }
        }

        if namespaces:
            for ns in namespaces:
                entry = {"type": ns["type"]}
                if ns.get("path"):
                    entry["path"] = ns["path"]
                spec["linux"]["namespaces"].append(entry)

        if resources:
            res = resources.to_linux_resources_dict()
            if res:
                spec["linux"]["resources"].update(res)

        a = any_pb2.Any()
        a.type_url = OCI_SPEC_TYPEURL
        a.value = json.dumps(spec).encode("utf-8")
        return a

# ========== CNI Manager ==========
class CniManager:
    """
    Minimal CNI runner.
    - Uses 'cnitool' if present to execute the conflist by name (preferred).
    - Fallback: accepts both *.conflist and *.conf; if *.conf is found it is wrapped
      into an in-memory conflist and we execute the FIRST plugin (commonly 'calico').
      For multi-plugin chains, install cnitool or extend this to iterate plugins.
    """
    def __init__(self, cni_bin_dir: str = CNI_BIN_DIR, cni_conf_dir: str = CNI_CONF_DIR):
        self.cni_bin_dir = cni_bin_dir
        self.cni_conf_dir = cni_conf_dir
        self.cnitool = which("cnitool")

    # ----- shared env for CNI calls -----
    def _base_env(self, container_id: str, netns_path: str, ifname: str, extra_env: dict | None = None):
        env = os.environ.copy()
        env.update({
            "CNI_PATH": self.cni_bin_dir,
            "CNI_NETNS": netns_path,
            "CNI_CONTAINERID": container_id,
            "CNI_IFNAME": ifname,
            "CNI_ARGS": "IgnoreUnknown=1",
        })
        env.setdefault("CNI_CONF_DIR", self.cni_conf_dir)
        if extra_env:
            env.update(extra_env)
        return env

    # ======== cnitool fast path ========
    def _cnitool_add(self, network_name: str, netns_path: str, env: dict, timeout: int) -> dict:
        cmd = [self.cnitool, "add", network_name, netns_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"cnitool add failed: {res.stderr.strip() or res.stdout.strip()}")
        try:
            return json.loads(res.stdout)
        except Exception:
            return {"raw": res.stdout}

    def _cnitool_del(self, network_name: str, netns_path: str, env: dict, timeout: int):
        cmd = [self.cnitool, "del", network_name, netns_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            print(f"[cni] delete warning: {res.stderr.strip() or res.stdout.strip()}")

    # ======== config discovery supporting .conflist and .conf ========
    def _load_conf_or_conflist(self, path: str) -> dict | None:
        try:
            with open(path, "r") as f:
                conf = json.load(f)
            if path.endswith(".conf"):
                # Wrap single-plugin .conf into a conflist so we can treat uniformly
                cni_version = conf.get("cniVersion", "0.4.0")
                name = conf.get("name", os.path.splitext(os.path.basename(path))[0])
                return {
                    "cniVersion": cni_version,
                    "name": name,
                    "plugins": [conf],
                }
            return conf  # already conflist
        except Exception:
            return None

    def _find_conflist(self, network_name: str) -> dict:
        try:
            files = sorted(
                fn for fn in os.listdir(self.cni_conf_dir)
                if fn.endswith(".conflist") or fn.endswith(".conf")
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"CNI conf dir not found: {self.cni_conf_dir}")

        for fn in files:
            path = os.path.join(self.cni_conf_dir, fn)
            conf = self._load_conf_or_conflist(path)
            if not conf:
                continue
            if conf.get("name") == network_name:
                return conf

        raise FileNotFoundError(
            f"No CNI conf/conflist named '{network_name}' under {self.cni_conf_dir}"
        )

    # ======== plugin execution helpers (fallback path) ========
    def _plugin_bin(self, plugin_type: str) -> str:
        path = os.path.join(self.cni_bin_dir, plugin_type)
        if not os.path.exists(path):
            raise FileNotFoundError(f"CNI plugin binary '{plugin_type}' not found in {self.cni_bin_dir}")
        return path

    def _exec_plugin(self, plugin_type: str, command: str, netns_path: str, container_id: str,
                     ifname: str, config_obj: dict, timeout: int = 20) -> str:
        env = os.environ.copy()
        env.update({
            "CNI_COMMAND": command,               # "ADD" or "DEL"
            "CNI_CONTAINERID": container_id,
            "CNI_NETNS": netns_path,
            "CNI_IFNAME": ifname,
            "CNI_PATH": self.cni_bin_dir,
        })
        env.setdefault("CNI_ARGS", "IgnoreUnknown=1")

        plugin = self._plugin_bin(plugin_type)
        stdin_bytes = json.dumps(config_obj).encode("utf-8")
        res = subprocess.run([plugin], input=stdin_bytes, env=env,
                             capture_output=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(
                f"CNI {plugin_type} {command} failed: {res.stderr.decode() or res.stdout.decode()}"
            )
        return res.stdout.decode()

    def _direct_add_first_plugin(self, network_name: str, container_id: str, netns_path: str,
                                 ifname: str, timeout: int) -> dict:
        conflist = self._find_conflist(network_name)
        plugins = conflist.get("plugins") or []
        if not plugins:
            raise RuntimeError(f"Conflist '{network_name}' has no 'plugins' array")

        first = dict(plugins[0])
        plugin_type = first.get("type")
        if not plugin_type:
            raise RuntimeError(f"First plugin in '{network_name}' has no 'type'")

        plugin_cfg = {
            "cniVersion": conflist.get("cniVersion", "0.4.0"),
            "name": conflist.get("name", network_name),
            **first
        }
        out = self._exec_plugin(plugin_type, "ADD", netns_path, container_id, ifname, plugin_cfg, timeout)
        try:
            return json.loads(out)
        except Exception:
            return {"raw": out}

    # ---------- Public API ----------
    def add(self, network_name: str, container_id: str, netns_path: str, ifname: str = DEFAULT_IFNAME,
            timeout: int = 20) -> dict:
        env = self._base_env(container_id, netns_path, ifname)
        if self.cnitool:
            return self._cnitool_add(network_name, netns_path, env, timeout)
        return self._direct_add_first_plugin(network_name, container_id, netns_path, ifname, timeout)

    def delete(self, network_name: str, container_id: str, netns_path: str, ifname: str = DEFAULT_IFNAME,
               timeout: int = 20):
        env = self._base_env(container_id, netns_path, ifname)
        if self.cnitool:
            return self._cnitool_del(network_name, netns_path, env, timeout)
        # fallback DEL on first plugin if cnitool missing
        try:
            conflist = self._find_conflist(network_name)
            first = (conflist.get("plugins") or [])[0]
            plugin_type = first.get("type")
            if not plugin_type:
                return
            plugin_cfg = {
                "cniVersion": conflist.get("cniVersion", "0.4.0"),
                "name": conflist.get("name", network_name),
                **first
            }
            self._exec_plugin(plugin_type, "DEL", netns_path, container_id, ifname, plugin_cfg, timeout)
        except Exception as e:
            print(f"[cni] delete fallback warning: {e}")

# ========== Container/Task ==========
class RuntimeManager:
    def __init__(self, client: ContainerdClient, snapshot_mgr: SnapshotManager):
        self.c = client
        self.snapshots = snapshot_mgr

    def create_container(self, cid: str, image_ref: str, spec_any: any_pb2.Any,
                         labels: Optional[Dict[str, str]] = None):
        self.c.containers.Create(
            containers_pb2.CreateContainerRequest(
                container=containers_pb2.Container(
                    id=cid,
                    image=image_ref,
                    labels=labels or {},
                    spec=spec_any,
                    runtime=containers_pb2.Container.Runtime(name="io.containerd.runc.v2"),
                    snapshotter=self.snapshots._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs",
                )
            ),
            metadata=ns_md()
        )

    def start_task(self, cid: str, mounts, tty: bool = False, create_timeout=15.0, start_timeout=30.0) -> int:
        create_req = tasks_pb2.CreateTaskRequest(
            container_id=cid,
            terminal=tty,
            rootfs=mounts
        )
        self.c.tasks.Create(create_req, metadata=ns_md(), timeout=create_timeout)
        resp = self.c.tasks.Start(tasks_pb2.StartRequest(container_id=cid),
                                  metadata=ns_md(), timeout=start_timeout)
        return resp.pid

    def stop_and_delete_task(self, cid: str, kill_signal: int = 15, timeouts: Tuple[float,float] = (3.0, 10.0)) -> None:
        """
        Best-effort: send signal, then delete the task; finally delete the container.
        - cid: container ID
        - kill_signal: 15 (TERM) by default; fallback to 9 (KILL) if needed
        """
        # Kill
        try:
            self.c.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=kill_signal), metadata=ns_md(), timeout=timeouts[0])
        except grpc.RpcError as e:
            # If already stopped or not found, we'll continue
            pass

        # Try delete task
        try:
            self.c.tasks.Delete(tasks_pb2.DeleteTaskRequest(container_id=cid), metadata=ns_md(), timeout=timeouts[1])
        except grpc.RpcError:
            # Try a harder kill then delete again
            try:
                self.c.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=9), metadata=ns_md(), timeout=timeouts[0])
                self.c.tasks.Delete(tasks_pb2.DeleteTaskRequest(container_id=cid), metadata=ns_md(), timeout=timeouts[1])
            except grpc.RpcError:
                pass

        # Delete container object
        try:
            self.c.containers.Delete(containers_pb2.DeleteContainerRequest(id=cid), metadata=ns_md())
        except grpc.RpcError:
            pass

# ========== Pod Manager ==========
class PodManager:
    def __init__(self, client: ContainerdClient):
        self.c = client
        self.images = ImageResolver(client)
        self.snaps = SnapshotManager(client)
        self.runtime = RuntimeManager(client, self.snaps)
        self.cni = CniManager()

    # def _ensure_unpacked(self, image: str):
    #     manifest_desc = self.images.resolve_manifest(image)
    #     manifest, cfg = self.images.load_manifest_and_config(manifest_desc)
    #     self.snaps.grpc_unpack(
    #         image, manifest, cfg,
    #         self.snaps._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs"
    #     )

    def _ensure_unpacked(self, image: str):
        """
        Ensure blobs exist in content store and unpack chain into snapshots.

        Flow:
          - resolve manifest for requested ref
          - if any layer blob missing, CRI PullImage(image)
          - ask CRI for manifest digest via ImageStatus; re-resolve via digest
          - re-check blobs (with a tiny retry), then grpc_unpack
        """

        def _layers_from_manifest(m: dict) -> list[str]:
            return [l["digest"] for l in (m.get("layers") or [])]

        # 1) Resolve current manifest+config for the tag/ref we were given
        manifest_desc = self.images.resolve_manifest(image)
        manifest, cfg = self.images.load_manifest_and_config(manifest_desc)
        layer_digests = _layers_from_manifest(manifest)

        missing = [dg for dg in layer_digests if not _blob_exists(self.c.content, dg)]
        if missing:
            print("ℹ️  Some blobs missing in content store; invoking CRI pull...")
            cri = _CRIImageClient(
                socket_target=os.environ.get("CRI_SOCKET", "/run/containerd/containerd.sock")
                # You can also reuse your CONTAINERD_SOCKET env/config here; the normalizer handles both.
            )
            pulled_ref = cri.pull(image)  # digest-like ref, e.g., 'sha256:07ccdb...'
            if not pulled_ref:
                raise RuntimeError(f"CRI PullImage failed; missing blobs start with: {missing[0]}")

            # 2) Ask CRI what manifest digest it actually resolved (more authoritative than the tag)
            digest_ref = cri.image_status(pulled_ref) or pulled_ref

            # 3) Re-resolve manifest using the digest ref (then fall back to original if needed)
            last_err = None
            for ref in (digest_ref, pulled_ref, image):
                try:
                    manifest_desc = self.images.resolve_manifest(ref)
                    manifest, cfg = self.images.load_manifest_and_config(manifest_desc)
                    layer_digests = _layers_from_manifest(manifest)
                    break
                except Exception as e:
                    last_err = e
            else:
                raise RuntimeError(f"After CRI pull, failed to resolve manifest: {last_err}")

            # 4) Re-check presence with a short backoff to avoid tiny commit races
            still_missing = [dg for dg in layer_digests if
                             not _blob_exists(self.c.content, dg, retries=5, sleep_sec=0.3)]
            if still_missing:
                ns = NAMESPACE
                example = still_missing[0]
                raise RuntimeError(
                    "Content still missing after CRI pull.\n"
                    f"- Namespace: {ns}\n"
                    f"- CRI manifest: {digest_ref}\n"
                    f"- Example missing digest: {example}\n"
                    "Checks:\n"
                    "  • Ensure your gRPC calls use the same namespace as CRI (usually k8s.io).\n"
                    "  • `ctr -n k8s.io content ls | grep <digest>` should show the blob.\n"
                    "  • Make sure unpack runs under a lease (prevents GC during apply).\n"
                    "  • Very rarely, a mirror returns a variant manifest—retry with the digest ref above."
                )

        # 5) Unpack into the snapshotter you discovered
        self.snaps.grpc_unpack(
            image, manifest, cfg,
            self.snaps._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs"
        )

    def create_pod(self, name: str, pause_image: str = "registry.k8s.io/pause:3.9",
                   resources: Optional[ResourceSpec] = None,
                   cni_network: str = DEFAULT_CNI_NET_NAME,
                   cni_ifname: str = DEFAULT_IFNAME) -> Dict:
        print(f"Using platform: {PLATFORM_OS}/{PLATFORM_ARCH}")
        self._ensure_unpacked(pause_image)

        chain_id = self.images.chain_id_for_image(pause_image)
        mounts, snap_key = self.snaps.prepare_rw_snapshot(chain_id, f"{name}-pause-rootfs")

        mdesc = self.images.resolve_manifest(pause_image)
        _, cfg = self.images.load_manifest_and_config(mdesc)
        args_cfg = list((cfg.get("config") or {}).get("Entrypoint") or [])
        args_cfg += list((cfg.get("config") or {}).get("Cmd") or [])
        args = args_cfg or ["/pause"]
        print(f"[pause] args={args}")

        ns = [
            {"type": "pid"},
            {"type": "network"},
            {"type": "ipc"},
            {"type": "uts"},
            {"type": "mount"},
        ]
        spec_any = OciSpecBuilder(hostname=name).build(
            process_args=args,
            namespaces=ns,
            resources=resources,
        )
        cid = f"{uuid.uuid4().hex[:6]}-{name}"
        self.runtime.create_container(cid, pause_image, spec_any, labels={"pod": name, "role": "pause"})
        pid = self.runtime.start_task(cid, mounts)

        ns_base = f"/proc/{pid}/ns"
        ns_paths = {k: f"{ns_base}/{k}" for k in ["pid", "net", "ipc", "uts"]}
        print(f"✅ Pause pod up: cid={cid}, pid={pid}")

        # Attach Calico via CNI (prefers cnitool, falls back to direct first-plugin exec)
        try:
            cni_result = self.cni.add(network_name=cni_network, container_id=cid,
                                      netns_path=ns_paths["net"], ifname=cni_ifname)
            print(f"🌐 CNI attached: {cni_result if isinstance(cni_result, dict) else 'ok'}")
        except Exception as e:
            print(f"❗ CNI attach failed: {e}")

        return {"name": name, "pause": {"cid": cid, "pid": pid}, "ns": ns_paths,
                "cni": {"network": cni_network, "ifname": cni_ifname},
                "snapshot_key": snap_key}

    def add_container(self, pod: Dict, name: str,
                      image: str,
                      args: Optional[List[str]] = None,
                      env: Optional[Dict[str, str]] = None,
                      resources: Optional[ResourceSpec] = None) -> Dict:
        pod_name = pod["name"]
        pod_ns = pod["ns"]

        self._ensure_unpacked(image)

        chain_id = self.images.chain_id_for_image(image)
        mounts, snap_key = self.snaps.prepare_rw_snapshot(chain_id, f"{pod_name}-{name}-rootfs")

        if args is None:
            mdesc = self.images.resolve_manifest(image)
            _, cfg = self.images.load_manifest_and_config(mdesc)
            args = list((cfg.get("config") or {}).get("Entrypoint") or [])
            args += list((cfg.get("config") or {}).get("Cmd") or [])
            if not args:
                args = ["/bin/sh", "-c", "trap : TERM INT; sleep infinity & wait"]

        namespaces = [
            {"type": "pid", "path": pod_ns["pid"]},
            {"type": "network", "path": pod_ns["net"]},
            {"type": "ipc", "path": pod_ns["ipc"]},
            {"type": "uts", "path": pod_ns["uts"]},
            {"type": "mount"},
        ]

        spec_any = OciSpecBuilder(hostname=pod_name).build(
            process_args=args,
            env=env or {},
            namespaces=namespaces,
            resources=resources
        )
        cid = f"{uuid.uuid4().hex[:6]}-{pod_name}-{name}"
        self.runtime.create_container(cid, image, spec_any, labels={"pod": pod_name, "app": name})
        pid = self.runtime.start_task(cid, mounts)
        print(f"🚀 App started: cid={cid}, pid={pid}, image={image}")
        return {"cid": cid, "pid": pid, "snapshot_key": snap_key}


    def _snapshotter_name(self) -> str:
        return self.snaps._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs"

    def delete_container(self, app: Dict) -> None:
        """
        Delete an app container:
          - stop & delete task
          - delete container object
          - remove active snapshot key
        Expected app dict shape: {"cid": ..., "pid": ..., "snapshot_key": ...}
        """
        cid = app.get("cid")
        snap_key = app.get("snapshot_key")
        if not cid:
            print("[cleanup] app has no 'cid'; skipping task/container delete")
        else:
            print(f"[cleanup] stopping app container: {cid}")
            self.runtime.stop_and_delete_task(cid)

        if snap_key:
            try:
                self.snaps._snap_remove_active(self._snapshotter_name(), snap_key)
                print(f"[cleanup] removed snapshot key: {snap_key}")
            except Exception as e:
                print(f"[cleanup] snapshot remove warning ({snap_key}): {e}")

    def delete_pod(self, pod: Dict, apps: Optional[List[Dict]] = None) -> None:
        """
        Delete a pod and release its Calico IP:
          - delete app containers first (if provided)
          - CNI DEL on the pause netns (while it still exists)
          - stop & delete the pause task/container
          - remove pause snapshot key (if stored), otherwise skip

        Expected pod dict shape (from create_pod):
          {
            "name": ...,
            "pause": {"cid": ..., "pid": ...},
            "ns": {"pid": "...", "net": "...", "ipc": "...", "uts": "..."},
            "cni": {"network": <name>, "ifname": <ifname>},
            "snapshot_key": <pause_snapshot_key>
          }
        """
        # 1) Delete app containers first (so they don’t keep file descriptors in the pod ns)
        if apps:
            for app in apps:
                self.delete_container(app)

        # 2) CNI DEL for the pod (pause) while netns still exists
        pause = (pod or {}).get("pause", {})
        pod_ns = (pod or {}).get("ns", {})
        cni_cfg = (pod or {}).get("cni", {})

        pause_cid = pause.get("cid")
        netns_path = pod_ns.get("net")
        network_name = cni_cfg.get("network", DEFAULT_CNI_NET_NAME)
        ifname = cni_cfg.get("ifname", DEFAULT_IFNAME)

        if pause_cid and network_name:
            # Best-effort: if /proc/<pid>/ns/net is gone, try empty NETNS (some plugins accept it)
            netns_for_del = netns_path if (netns_path and os.path.exists(netns_path)) else ""
            try:
                print(f"[cleanup] CNI DEL network={network_name}, ifname={ifname}, netns={'present' if netns_for_del else 'missing'}")
                self.cni.delete(network_name=network_name, container_id=pause_cid, netns_path=netns_for_del, ifname=ifname)
                print("[cleanup] CNI released")
            except Exception as e:
                print(f"[cleanup] CNI DEL warning: {e}")
        else:
            print("[cleanup] skip CNI DEL (missing pause cid or network name)")

        # 3) Stop & delete the pause task/container
        if pause_cid:
            print(f"[cleanup] stopping pause container: {pause_cid}")
            self.runtime.stop_and_delete_task(pause_cid)

        # 4) Remove the pause snapshot key (stored as pod['snapshot_key'])
        snap_key = pod.get("snapshot_key")
        if snap_key:
            try:
                self.snaps._snap_remove_active(self._snapshotter_name(), snap_key)
                print(f"[cleanup] removed pause snapshot key: {snap_key}")
            except Exception as e:
                print(f"[cleanup] pause snapshot remove warning ({snap_key}): {e}")


# -------------------- Demo / Example --------------------
if __name__ == "__main__":
    client = ContainerdClient()
    pods = PodManager(client)

    # Create a pod with CPU/memory for the pause sandbox + CNI attach
    pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
    pod = pods.create_pod(
        "pause",
        pause_image="registry.k8s.io/pause:3.9",
        resources=pause_resources,
        cni_network=os.environ.get("CNI_NET_NAME", DEFAULT_CNI_NET_NAME),
        cni_ifname=os.environ.get("CNI_IFNAME", DEFAULT_IFNAME),
    )

    # Add nginx with CPU/memory and a CPU set
    app_resources = ResourceSpec(cpu_millicores=500, memory="256Mi", cpuset_cpus="0-1")
    app = pods.add_container(
        pod, name="nginx",
        image="docker.io/library/nginx:latest",
        args=None,
        resources=app_resources
    )

    print("\nSummary:")
    print(json.dumps({
        "pause": pod["pause"],
        "nginx": app
    }, indent=2))