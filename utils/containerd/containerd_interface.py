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
import time
from shutil import which
from dataclasses import dataclass,field
from typing import Optional, Dict, List, Tuple,Any
import signal
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld, log_to_file
from typing import Union
import base64
import urllib.request
import urllib.error
import urllib.parse
import threading
import queue
import stat
from datetime import datetime, timezone
from pathlib import Path


from google.protobuf import any_pb2
from google.protobuf.json_format import ParseDict

# ----- containerd native gRPC stubs -----
from generated.runtime.v1 import api_pb2, api_pb2_grpc
from generated.api.services.images.v1 import images_pb2, images_pb2_grpc
from generated.api.services.content.v1 import content_pb2, content_pb2_grpc
from generated.api.services.snapshots.v1 import snapshots_pb2, snapshots_pb2_grpc
from generated.api.services.containers.v1 import containers_pb2, containers_pb2_grpc
from generated.api.services.tasks.v1 import tasks_pb2, tasks_pb2_grpc
from generated.api.services.diff.v1 import diff_pb2, diff_pb2_grpc
from generated.api.services.leases.v1 import leases_pb2, leases_pb2_grpc
from generated.api.services.namespaces.v1 import namespace_pb2, namespace_pb2_grpc   # <-- add this
from generated.api.types import descriptor_pb2
#from generated.api.types import mount_pb2

#from generated.api.services.tasks.v1 import tasks_pb2 as tpb


# diff + leases for gRPC-only unpack

from utils.containerd.grpc_ns import _AddNamespaceInterceptor
from utils.containerd.models import ResourceSpec
#from google.protobuf import empty_pb2

logger = LogKCld()

read_conf = rc()


# ---- add this helper near your imports ----

# Map common containerd status ints (defensive; strings are left as-is)
_STATUS_MAP = {
    0: "UNKNOWN",
    1: "CREATED",
    2: "RUNNING",
    3: "STOPPED",
    4: "PAUSED",
}

# ---------- Config ----------
CONTAINERD_SOCKET = "unix:///run/containerd/containerd.sock"
NAMESPACE = os.environ.get("CONTAINERD_NAMESPACE", "default")
DEFAULT_SNAPSHOTTER = os.environ.get("CONTAINERD_SNAPSHOTTER", "overlayfs")
OCI_SPEC_TYPEURL = "types.containerd.io/opencontainers/runtime-spec/1/Spec"

# CNI defaults (override via env as needed)
CNI_BIN_DIR = os.environ.get("CNI_PATH", "/opt/cni/bin")
CNI_CONF_DIR = os.environ.get("CNI_CONF_DIR", "/etc/cni/net.d")
DEFAULT_CNI_NET_NAME = os.environ.get("CNI_NET_NAME", "calico")  # must match conflist "name"
DEFAULT_IFNAME = os.environ.get("CNI_IFNAME", "eth0")

# Kubernetes-style log defaults
LOG_DIR = os.environ.get("CONTAINER_LOG_DIR", "/var/log/pods")
DEFAULT_MAX_LOG_SIZE = int(os.environ.get("CONTAINER_MAX_LOG_SIZE", 10 * 1024 * 1024))  # 10MB default
DEFAULT_MAX_LOG_FILES = int(os.environ.get("CONTAINER_MAX_LOG_FILES", 5))  # Keep 5 rotated files
DEFAULT_LOG_TIMEOUT = 60  # seconds for log streaming timeout

# ----- Media types -----
OCI_INDEX   = "application/vnd.oci.image.index.v1+json"
OCI_MANIF   = "application/vnd.oci.image.manifest.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MAN  = "application/vnd.docker.distribution.manifest.v2+json"
ANNOTATION_UNCOMPRESSED = "containerd.io/uncompressed"


# Registry media types we accept
MT_OCI_INDEX   = "application/vnd.oci.image.index.v1+json"
MT_OCI_MANIF   = "application/vnd.oci.image.manifest.v1+json"
MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_DOCKER_MAN  = "application/vnd.docker.distribution.manifest.v2+json"

ACCEPT_MANIFEST = ", ".join([MT_OCI_INDEX, MT_OCI_MANIF, MT_DOCKER_LIST, MT_DOCKER_MAN])




@log_to_file(logger)
def _status_to_str(v):
    if v is None:
        return "UNKNOWN"
    if isinstance(v, int):
        return _STATUS_MAP.get(v, str(v))
    return str(v)

@log_to_file(logger)
def _read_pidfile(namespace: str, cid: str) -> int | None:
    """
    Fallback to containerd v2 runtime pidfile:
      /run/containerd/io.containerd.runtime.v2.task/<ns>/<cid>/init.pid
    """
    base = "/run/containerd/io.containerd.runtime.v2.task"
    path = os.path.join(base, namespace, cid, "init.pid")
    try:
        with open(path, "r") as f:
            s = f.read().strip()
        return int(s) if s else None
    except Exception:
        return None

@log_to_file(logger)
def _task_from_get_response(resp):
    """
    Some generated stubs return GetResponse{ task: Task }
    Others effectively return Task. Normalize here.
    """
    if hasattr(resp, "task"):
        return resp.task
    return resp  # assume already a Task-like message

@log_to_file(logger)
def _pick_list_req(tasks_pb2):
    """
    Try to locate the right List request class across versions.
    Known names: ListRequest, TasksListRequest, ListTasksRequest.
    """
    for name in ("ListRequest", "TasksListRequest", "ListTasksRequest"):
        Req = getattr(tasks_pb2, name, None)
        if Req is not None:
            return Req
    return None

@log_to_file(logger)
def _iter_list_tasks(client, tasks_pb2):
    """
    Yield Task-like items from Tasks.List(), coping with response shape differences.
    """
    Req = _pick_list_req(tasks_pb2)
    if Req is None:
        return  # give up on List path

    try:
        resp = client.tasks.List(Req())
    except grpc.RpcError:
        return

    # Try common response field names
    for field in ("tasks", "items", "list", "results"):
        maybe = getattr(resp, field, None)
        if maybe:
            for t in maybe:
                yield t
            return

    # Some stubs put the sequence directly on the response
    # (very rare, but this keeps it resilient)
    try:
        for t in resp:
            yield t
    except TypeError:
        pass

@log_to_file(logger)
def _task_id(t):
    # common field name is "id"
    return getattr(t, "id", None) or getattr(t, "container_id", None)

@log_to_file(logger)
def _task_pid(t):
    return getattr(t, "pid", None)

@log_to_file(logger)
def _task_status(t):
    # Common names: status, state
    raw = getattr(t, "status", None)
    if raw is None:
        raw = getattr(t, "state", None)
    return _status_to_str(raw)


@log_to_file(logger)
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




# --- platform auto-detect (overridden if FORCE_PLATFORM is set) ---
@log_to_file(logger)
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



@log_to_file(logger)
def _is_index(mt: str) -> bool:
    return mt.endswith("image.index.v1+json") or mt == DOCKER_LIST

@log_to_file(logger)
def _is_manifest(mt: str) -> bool:
    return mt.endswith("image.manifest.v1+json") or mt == DOCKER_MAN

@log_to_file(logger)
def ns_md(namespace: str|None,extra=None) -> Tuple[Tuple[str,str], ...]:
    md = [("containerd-namespace", namespace)]
    if extra:
        md.extend(extra)
    return tuple(md)

@log_to_file(logger)
def rtns_md(namespace: str,extra=None) -> Tuple[Tuple[str,str], ...]:
    md = [("containerd-namespace", namespace)]
    if extra:
        md.extend(extra)
    return tuple(md)


# ========== Utilities ==========
@log_to_file(logger)
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

# @log_to_file(logger)
# def _read_blob_json(content_stub, digest: str, extra_md=None) -> dict:
#     stream = content_stub.Read(content_pb2.ReadContentRequest(digest=digest))
#     data = b"".join(part.data for part in stream if part.data)
#     return json.loads(data.decode("utf-8"))

def _read_blob_json(content_stub, digest: str, md):
    req = content_pb2.ReadContentRequest(digest=digest)
    stream = content_stub.Read(req, metadata=md)  # <- MUST include namespace
    data = b"".join(part.data for part in stream if part.data)
    return json.loads(data.decode("utf-8"))


@log_to_file(logger)
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

@log_to_file(logger)
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

@log_to_file(logger)
def _mcores_to_quota_period(millicores: int, period_us: int = 100_000) -> Tuple[int, int]:
    if millicores <= 0:
        return (0, period_us)
    quota = int(period_us * (millicores / 1000.0))
    quota = max(quota, 1000)
    return (quota, period_us)

@log_to_file(logger)
def _mcores_to_shares(millicores: int) -> int:
    if millicores <= 0:
        return 2
    shares = int(1024 * (millicores / 1000.0))
    return max(2, shares)

@log_to_file(logger)
def _content_exists(content_stub, digest: str) -> bool:
    try:
        content_stub.Info(content_pb2.InfoRequest(digest=digest))
        return True
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            return False
        raise

@log_to_file(logger)
def _split_image_ref(ref: str) -> tuple[str, str, str]:
    """
    Return (registry, repo, reference) where reference is tag or digest.
    Very small parser:
      docker.io/library/nginx:latest -> registry=docker.io repo=library/nginx reference=latest
      registry.k8s.io/pause:3.9     -> registry=registry.k8s.io repo=pause reference=3.9
      docker.io/library/alpine@sha256:... -> reference=sha256:...
    """
    # normalize docker hub short names
    if "://" in ref:
        ref = ref.split("://", 1)[1]

    parts = ref.split("/")
    if len(parts) == 1:
        registry = "docker.io"
        repo = f"library/{parts[0]}"
    else:
        first = parts[0]
        if "." in first or ":" in first or first == "localhost":
            registry = first
            repo = "/".join(parts[1:])
        else:
            registry = "docker.io"
            repo = "/".join(parts)

    reference = "latest"
    if "@" in repo:
        repo, reference = repo.split("@", 1)
    elif ":" in repo.split("/")[-1]:
        # tag form: repo:tag but colon is in last segment
        repo, reference = repo.rsplit(":", 1)

    return registry, repo, reference

@log_to_file(logger)
def _list_namespaces(client: "ContainerdClient") -> list[str]:
    """
    Return namespaces known to containerd (best-effort).
    """
    try:
        resp = client.namespaces.List(namespace_pb2.ListNamespacesRequest())
        items = getattr(resp, "namespaces", None) or getattr(resp, "items", None) or []
        out = [n.name for n in items if getattr(n, "name", None)]
        # keep stable order
        return sorted(set(out))
    except Exception:
        # conservative fallback
        return ["k8s.io", "default", "moby"]





@log_to_file(logger)
def _find_image_in_any_namespace(
    socket: str,
    image_ref: str,
    candidates: list[str] | None = None,
    skip: set[str] | None = None,
    probe_namespace: str | None = None,   # <-- add
) -> tuple[str | None, str | None]:
    """
    Find (namespace, resolved_name) where Images.Get works for image_ref.
    Returns (None, None) if not found anywhere.
    """
    skip = skip or set()
    # candidates: if caller doesn't provide, use ref variants
    cand_refs = candidates or _candidates_for_ref(image_ref)

    # We need SOME client to list namespaces; use env default first.
    probe_client = ContainerdClient(socket=socket, namespace=NAMESPACE)
    namespaces = _list_namespaces(probe_client)

    # Scan: try each namespace, and for each candidate ref try Images.Get
    for ns in namespaces:
        if ns in skip:
            continue
        c = ContainerdClient(socket=socket, namespace=ns)
        for cand in cand_refs:
            try:
                c.images.Get(images_pb2.GetImageRequest(name=cand))
                return ns, cand
            except grpc.RpcError as e:
                if e.code() != grpc.StatusCode.NOT_FOUND:
                    # If containerd returns another error, bubble it up; it's not "not found"
                    raise
            except Exception:
                # ignore and continue scanning
                pass

    return None, None


@log_to_file(logger)
def _import_image_record_namespace_to_namespace(
    socket: str,
    src_ns: str,
    dst_ns: str,
    image_name_in_src: str,
    dst_name: str | None = None,
) -> str:
    """
    Copy an Image record (name + target descriptor) from src_ns to dst_ns.
    Returns the name used in dst namespace.
    """
    dst_name = dst_name or image_name_in_src

    src = ContainerdClient(socket=socket, namespace=src_ns)
    dst = ContainerdClient(socket=socket, namespace=dst_ns)

    img = src.images.Get(images_pb2.GetImageRequest(name=image_name_in_src)).image
    _images_create_or_update(dst.images, dst_name, img.target)
    return dst_name



class NamespaceInterceptor(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    def __init__(self, namespace: str):
        self.namespace = namespace

    def _augment(self, client_call_details):
        md = []
        if client_call_details.metadata is not None:
            md = list(client_call_details.metadata)
        md.append(("containerd-namespace", self.namespace))

        return client_call_details._replace(metadata=md)

    def intercept_unary_unary(self, continuation, client_call_details, request):
        return continuation(self._augment(client_call_details), request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        return continuation(self._augment(client_call_details), request)

    def intercept_stream_unary(self, continuation, client_call_details, request_iterator):
        return continuation(self._augment(client_call_details), request_iterator)

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        return continuation(self._augment(client_call_details), request_iterator)



class RegistryV2Client:
    """
    Minimal Docker Registry v2 client that supports:
      - GET manifest (tag or digest)
      - GET blob
    Supports Bearer token challenge and Basic auth if provided.
    """

    @log_to_file(logger)
    def __init__(self, registry: str, username: str | None = None, password: str | None = None):
        self.registry = registry
        self.username = username
        self.password = password
        self._bearer_token_cache: dict[str, str] = {}  # scope -> token

    @log_to_file(logger)
    def _basic_auth_header(self) -> str | None:
        if self.username is None or self.password is None:
            return None
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("utf-8")

    @log_to_file(logger)
    def _request(self, method: str, url: str, headers: dict[str, str], data: bytes | None = None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            return e.code, dict(e.headers), body

    @log_to_file(logger)
    def _parse_www_authenticate(self, header_val: str) -> dict[str, str]:
        # Bearer realm="...",service="...",scope="..."
        out = {}
        if not header_val:
            return out
        # split scheme + params
        try:
            scheme, rest = header_val.split(" ", 1)
        except ValueError:
            return out
        out["scheme"] = scheme
        for part in rest.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
        return out

    @log_to_file(logger)
    def _get_bearer_token(self, realm: str, service: str | None, scope: str | None) -> str | None:
        cache_key = scope or ""
        if cache_key in self._bearer_token_cache:
            return self._bearer_token_cache[cache_key]

        qs = {}
        if service:
            qs["service"] = service
        if scope:
            qs["scope"] = scope
        url = realm + ("?" + urllib.parse.urlencode(qs) if qs else "")

        headers = {}
        basic = self._basic_auth_header()
        if basic:
            headers["Authorization"] = basic

        code, _, body = self._request("GET", url, headers=headers)
        if code != 200:
            return None
        try:
            j = json.loads(body.decode("utf-8"))
            tok = j.get("token") or j.get("access_token")
            if tok:
                self._bearer_token_cache[cache_key] = tok
            return tok
        except Exception:
            return None

    @log_to_file(logger)
    def _authed_get(self, url: str, headers: dict[str, str], scope: str | None) -> tuple[int, dict, bytes]:
        # 1) try without token (or with Basic if set)
        h = dict(headers)
        basic = self._basic_auth_header()
        if basic:
            h["Authorization"] = basic

        code, resp_h, body = self._request("GET", url, headers=h)
        if code != 401:
            return code, resp_h, body

        # 2) bearer challenge
        wa = resp_h.get("Www-Authenticate") or resp_h.get("WWW-Authenticate") or ""
        parsed = self._parse_www_authenticate(wa)
        if parsed.get("scheme", "").lower() != "bearer":
            return code, resp_h, body

        realm = parsed.get("realm")
        service = parsed.get("service")
        # if server didn't include scope, use our requested one
        chal_scope = parsed.get("scope") or scope
        if not realm:
            return code, resp_h, body

        token = self._get_bearer_token(realm, service, chal_scope)
        if not token:
            return code, resp_h, body

        h2 = dict(headers)
        h2["Authorization"] = f"Bearer {token}"
        return self._request("GET", url, headers=h2)

    @log_to_file(logger)
    def get_manifest(self, repo: str, reference: str) -> tuple[dict, str]:
        url = f"https://{self.registry}/v2/{repo}/manifests/{reference}"
        headers = {"Accept": ACCEPT_MANIFEST}
        scope = f"repository:{repo}:pull"
        code, resp_h, body = self._authed_get(url, headers, scope)
        if code != 200:
            raise RuntimeError(f"manifest fetch failed {code} for {self.registry}/{repo}:{reference}: {body[:200]!r}")
        mt = (resp_h.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
        j = json.loads(body.decode("utf-8"))
        return j, mt

    @log_to_file(logger)
    def get_manifest_with_headers(self, repo: str, reference: str) -> tuple[dict, str, dict, bytes]:
        url = f"https://{self.registry}/v2/{repo}/manifests/{reference}"
        headers = {"Accept": ACCEPT_MANIFEST}
        scope = f"repository:{repo}:pull"
        code, resp_h, body = self._authed_get(url, headers, scope)
        if code != 200:
            raise RuntimeError(f"manifest fetch failed {code} for {self.registry}/{repo}:{reference}: {body[:200]!r}")
        mt = (resp_h.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
        j = json.loads(body.decode("utf-8"))
        return j, mt, resp_h, body

    @log_to_file(logger)
    def get_blob(self, repo: str, digest: str) -> bytes:
        url = f"https://{self.registry}/v2/{repo}/blobs/{digest}"
        headers = {}
        scope = f"repository:{repo}:pull"
        code, _, body = self._authed_get(url, headers, scope)
        if code != 200:
            raise RuntimeError(f"blob fetch failed {code} for {self.registry}/{repo} {digest}: {body[:200]!r}")
        return body



@dataclass
class ContainerSpec:
    name: str
    image: str
    args: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    resources: Optional[ResourceSpec] = None

@log_to_file(logger)
def _content_write_and_commit(content_stub, digest: str, data: bytes, labels: dict | None = None):
    """
    Stream blob to containerd content store:
      - WRITE frames with correct offsets
      - COMMIT frame includes expected + total + offset=total
    """
    if _blob_exists(content_stub, digest):
        return

    CHUNK = 1024 * 1024
    total = len(data)
    ref = f"ref-{digest.replace(':', '-')}-{uuid.uuid4().hex[:8]}"

    WriteReq = content_pb2.WriteContentRequest
    WriteAction = content_pb2.WriteAction

    @log_to_file(logger)
    def _req_iter():
        offset = 0
        while offset < total:
            chunk = data[offset: offset + CHUNK]
            r = WriteReq(
                action=WriteAction.WRITE,
                ref=ref,
                expected=digest if offset == 0 else "",
                total=total if offset == 0 else 0,
                offset=offset,
                data=chunk,
            )
            if labels and offset == 0:
                r.labels.update(labels)
            yield r
            offset += len(chunk)

        rc = WriteReq(
            action=WriteAction.COMMIT,
            ref=ref,
            expected=digest,
            total=total,
            offset=total,
            data=b"",
        )
        if labels:
            rc.labels.update(labels)
        yield rc

    try:
        resp = content_stub.Write(_req_iter())
        try:
            for _ in resp:
                pass
        except TypeError:
            # unary response, nothing to drain
            pass
    except Exception as e:
        try:
            content_stub.Abort(content_pb2.AbortRequest(ref=ref))
        except Exception:
            pass
        raise RuntimeError(f"Content.Write(commit) failed for {digest}: {e}")

    if not _blob_exists(content_stub, digest, retries=8, sleep_sec=0.25):
        raise RuntimeError(f"Write returned but blob not visible via Info(): {digest}")

@log_to_file(logger)
def _images_create_or_update(images_stub, name: str, target_desc: descriptor_pb2.Descriptor):
    """
    Ensure Images service has an Image record for 'name' in THIS namespace.
    """
    img = images_pb2.Image(
        name=name,
        target=target_desc,
        labels={"managed-by": "dibba"},
    )

    # Prefer Create; if already exists, Update
    try:
        images_stub.Create(images_pb2.CreateImageRequest(image=img))
        return
    except grpc.RpcError as e:
        if e.code() != grpc.StatusCode.ALREADY_EXISTS:
            raise

    # Update path
    try:
        images_stub.Update(images_pb2.UpdateImageRequest(image=img))
    except Exception:
        # Some stubs require update_mask; keep simplest if yours allows it.
        images_stub.Update(images_pb2.UpdateImageRequest(image=img))


# ---- Content / CRI helpers ----
@log_to_file(logger)
def _blob_exists(content_stub, dgst: str, retries: int = 3, sleep_sec: float = 0.25, md=None) -> bool:
    for i in range(retries + 1):
        try:
            content_stub.Info(content_pb2.InfoRequest(digest=dgst),metadata=md)
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                if i < retries:
                    time.sleep(sleep_sec)
                    continue
                return False
            raise

# class _CRIImageClient:
#     @log_to_file(logger)
#     def __init__(self, socket_target="/run/containerd/containerd.sock"):
#         # Accept either a path or a 'unix://...' target
#         target = _normalize_unix_target(socket_target)
#         self.channel = grpc.insecure_channel(target)
#         self.stub = api_pb2_grpc.ImageServiceStub(self.channel)
#
#     @log_to_file(logger)
#     def pull(self, image_ref: str) -> str | None:
#         try:
#             resp = self.stub.PullImage(api_pb2.PullImageRequest(
#                 image=api_pb2.ImageSpec(image=image_ref)
#             ))
#             return resp.image_ref
#         except grpc.RpcError as e:
#             print(f"[cri] PullImage error: {e.code().name}: {e.details()}")
#             return None
#
#     @log_to_file(logger)
#     def image_status(self, image_ref: str) -> str | None:
#         try:
#             st = self.stub.ImageStatus(api_pb2.ImageStatusRequest(
#                 image=api_pb2.ImageSpec(image=image_ref)
#             ))
#             if st.image and st.image.id:
#                 return st.image.id
#         except grpc.RpcError as e:
#             print(f"[cri] ImageStatus error: {e.code().name}: {e.details()}")
#         return None

class _CRIImageClient:
    @log_to_file(logger)
    def __init__(self, socket_target="/run/containerd/containerd.sock"):
        # Accept either a path or a 'unix://...' target
        target = _normalize_unix_target(socket_target)
        self.channel = grpc.insecure_channel(target)
        self.stub = api_pb2_grpc.ImageServiceStub(self.channel)

    @log_to_file(logger)
    def image_exists(self, image_ref: str) -> bool:
        """
        Check if an image already exists in containerd via CRI ImageService.ListImages.
        Matches against repo_tags and repo_digests.
        """
        try:
            list_response = self.stub.ListImages(api_pb2.ListImagesRequest())
            for img in list_response.images:
                # tags like 'docker.io/library/alpine:latest'
                if img.repo_tags and image_ref in img.repo_tags:
                    logger.info(f"Image already exists: {image_ref}")
                    return True
                # digests like 'sha256:abcd...'
                if img.repo_digests and image_ref in img.repo_digests:
                    logger.info(f"Image already exists by digest: {image_ref}")
                    return True
            logger.debug(f"Image not found locally: {image_ref}")
            return False
        except grpc.RpcError as e:
            logger.warning(f"Error checking image existence: {e.code().name} - {e.details()}")
            return False

    @log_to_file(logger)
    def pull(self, image_ref: str) -> str | None:
        """
        Pull an image via CRI if it doesn't already exist.
        Returns the final image_ref (tag or digest) on success.
        """
        # Short-circuit if already present
        if self.image_exists(image_ref):
            return image_ref

        logger.info(f"Pulling image: {image_ref}")
        request = api_pb2.PullImageRequest(
            image=api_pb2.ImageSpec(image=image_ref)
        )
        try:
            response = self.stub.PullImage(request)
            logger.info(f"Pulled image: {response.image_ref}")
            return response.image_ref
        except grpc.RpcError as e:
            logger.error(f"Pull failed: {e.code().name} - {e.details()}", exc_info=True)
            return None

    @log_to_file(logger)
    def image_status(self, image_ref: str) -> str | None:
        """
        Return the resolved image ID/digest for the given ref, if known.
        """
        try:
            st = self.stub.ImageStatus(api_pb2.ImageStatusRequest(
                image=api_pb2.ImageSpec(image=image_ref)
            ))
            if st.image and st.image.id:
                return st.image.id
        except grpc.RpcError as e:
            logger.error(f"[cri] ImageStatus error: {e.code().name}: {e.details()}", exc_info=True)
        return None


# ========== Client ==========
class ContainerdClient:
    @log_to_file(logger)
    def __init__(self,socket: str = CONTAINERD_SOCKET,
                 namespace: Optional[str] = None):

        target = _normalize_unix_target(socket)
        self.socket = target
        self.namespace = namespace or NAMESPACE
        #ch = grpc.insecure_channel(target)
        self._raw_ch = grpc.insecure_channel(target)
        self._ich = grpc.intercept_channel(self._raw_ch, _AddNamespaceInterceptor(self.namespace))


        #self.channel = grpc.insecure_channel(socket)
        self.images = images_pb2_grpc.ImagesStub(self._ich)
        self.content = content_pb2_grpc.ContentStub(self._ich)
        self.snapshots = snapshots_pb2_grpc.SnapshotsStub(self._ich)
        self.containers = containers_pb2_grpc.ContainersStub(self._ich)
        self.tasks = tasks_pb2_grpc.TasksStub(self._ich)
        self.diff = diff_pb2_grpc.DiffStub(self._ich)
        self.leases = leases_pb2_grpc.LeasesStub(self._ich)
        self.namespaces = namespace_pb2_grpc.NamespacesStub(self._raw_ch)

    @log_to_file(logger)
    # def md(self, extra: tuple[tuple[str, str], ...] = ()) -> tuple[tuple[str, str], ...]:
    #     base = (("containerd-namespace", self.namespace),)
    #     return base + tuple(extra)
    @log_to_file(logger)
    def md(self):
        # containerd uses this metadata key
        return [("containerd-namespace", self.namespace)]

    @log_to_file(logger)
    def content_exists(self, digest: str) -> bool:
        """
        True if the blob exists in this namespace's content store.
        """
        try:
            self.content.Info(
                content_pb2.InfoRequest(digest=digest),
                metadata=self.md(),
            )
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

# ========== Image Resolution ==========
class ImageResolver:
    @log_to_file(logger)
    def __init__(self, client: ContainerdClient):
        self.c = client

    @log_to_file(logger)
    def resolve_image_name(self, wanted: str) -> str:
        for cand in _candidates_for_ref(wanted):
            try:
                self.c.images.Get(images_pb2.GetImageRequest(name=cand))
                return cand
            except grpc.RpcError as e:
                if e.code() != grpc.StatusCode.NOT_FOUND:
                    raise
        raise RuntimeError(f"Image {wanted} not found in namespace {self.c.namespace}")

    @log_to_file(logger)
    def delete_image_record(self, image_ref: str) -> None:
        try:
            self.c.images.Delete(
                images_pb2.DeleteImageRequest(name=image_ref),
                metadata=self.c.md(),
            )
            logger.info(f"[images] deleted image record: {image_ref}")
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise

    @log_to_file(logger)
    def pull_image_ctr(self, image_ref: str, platform: str = "linux/amd64") -> None:
        ns = self.c.namespace
        cmd = [
            "ctr", "-n", ns,
            "images", "pull",
            "--platform", platform,
            image_ref
        ]
        logger.info(f"[ctr] pulling image into ns={ns}: {' '.join(cmd)}")
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ctr pull failed ({p.returncode}): {p.stderr or p.stdout}")

    @log_to_file(logger)
    def resolve_manifest(self, image_ref: str, extra_md=None) -> descriptor_pb2.Descriptor:
        resolved = self.resolve_image_name(image_ref)
        md = self.c.md()  # <- ALWAYS
        img = self.c.images.Get(images_pb2.GetImageRequest(name=resolved)).image
        tgt = img.target
        if _is_index(tgt.media_type):
            idx = _read_blob_json(self.c.content, tgt.digest, md)
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

    @log_to_file(logger)
    def load_manifest_and_config(self, manifest_desc, extra_md=None):
        manifest = _read_blob_json(self.c.content, manifest_desc.digest, extra_md)
        cfg_digest = manifest["config"]["digest"]
        config = _read_blob_json(self.c.content, cfg_digest, extra_md)
        return manifest, config

    @log_to_file(logger)
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
    @log_to_file(logger)
    def __init__(self, client: ContainerdClient, default_snapshotter: str = DEFAULT_SNAPSHOTTER):
        self.c = client
        self._snapshotter_value_cache: Optional[str] = None
        self.default_snapshotter = default_snapshotter

    # --- helper (top-level or inside SnapshotManager) ---
    @staticmethod
    @log_to_file(logger)
    def _lazy_umount_mounts_under(path: str):
        try:
            import subprocess, shlex
            # leaf-first unmount; findmnt is present on most distros
            cmd = f"findmnt -Rno TARGET {shlex.quote(path)} | tac"
            out = subprocess.check_output(cmd, shell=True, text=True).strip().splitlines()
            for tgt in out:
                if tgt:
                    subprocess.run(["umount", "-l", tgt], check=False)
        except Exception:
            pass

    @log_to_file(logger)
    def _snapshotter_candidates(self) -> List[str]:
        raw = []
        if self.default_snapshotter:
            raw.append(self.default_snapshotter)
        raw += ["overlayfs", "native", "btrfs", "zfs", "stargz"]
        seen = set(); raw = [x for x in raw if not (x in seen or seen.add(x))]
        full = [f"io.containerd.snapshotter.v1.{name}" for name in raw]
        return raw + full

    @log_to_file(logger)
    def _snap_stat_exists(self, snapshotter: str, key_or_name: str, extra_md=None) -> bool:
        try:
            self.c.snapshots.Stat(
                snapshots_pb2.StatSnapshotRequest(snapshotter=snapshotter, key=key_or_name)
            )
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    @log_to_file(logger)
    def _snap_remove_active(self, snapshotter: str, key: str, extra_md=None):
        try:
            self.c.snapshots.Remove(
                snapshots_pb2.RemoveSnapshotRequest(snapshotter=snapshotter, key=key)
            )
        except grpc.RpcError:
            pass

    @log_to_file(logger)
    def _new_lease(self, id_hint: str = "unpack") -> leases_pb2.Lease:
        lid = f"{id_hint}-{uuid.uuid4().hex[:8]}"
        resp = self.c.leases.Create(
            leases_pb2.CreateRequest(id=lid, labels={"containerd.io/gc.root": "true"}))
        return resp.lease

    @log_to_file(logger)
    def _delete_lease(self, lease_id: str):
        try:
            self.c.leases.Delete(leases_pb2.DeleteRequest(id=lease_id))
        except grpc.RpcError:
            pass

    @log_to_file(logger)
    def snapshotter(self) -> str:
        return self._snapshotter_value_cache or self.default_snapshotter or DEFAULT_SNAPSHOTTER or "overlayfs"

    @log_to_file(logger)
    def ensure_snapshotter_discovered(self) -> str:
        """
        Discover a working snapshotter string early, so unpack + prepare use the same one.
        """
        if self._snapshotter_value_cache:
            return self._snapshotter_value_cache

        # Try List() first (cheap) across candidates
        for snap_val in self._snapshotter_candidates():
            try:
                _ = self.c.snapshots.List(
                    snapshots_pb2.ListSnapshotsRequest(snapshotter=snap_val)
                )
                self._snapshotter_value_cache = snap_val
                logger.info(f"Discovered snapshotter (via List): '{snap_val}'")
                return snap_val
            except grpc.RpcError:
                continue

        # If List() isn't supported/behaves differently, fall back to a tiny Prepare/Remove probe
        key = f"probe-{uuid.uuid4().hex[:8]}"
        for snap_val in self._snapshotter_candidates():
            try:
                self.c.snapshots.Prepare(
                    snapshots_pb2.PrepareSnapshotRequest(
                        snapshotter=snap_val,
                        key=key,
                        parent="",
                        labels={"managed-by": "dibba", "probe": "true"},
                    )
                )
                # remove the active probe key
                try:
                    self.c.snapshots.Remove(
                        snapshots_pb2.RemoveSnapshotRequest(snapshotter=snap_val, key=key)
                    )
                except Exception:
                    pass

                self._snapshotter_value_cache = snap_val
                logger.info(f"Discovered snapshotter (via probe): '{snap_val}'")
                return snap_val
            except grpc.RpcError:
                continue

        raise RuntimeError("Unable to discover a working snapshotter via Snapshots API.")

    @log_to_file(logger)
    def prepare_rw_snapshot(
            self,
            parent_chain_id: str,
            key_hint: str,
            extra_md=None,
            labels: Optional[Dict[str, str]] = None,  # <--- add
    ) -> Tuple[List[Any], str]:
        key = f"{key_hint}-{uuid.uuid4().hex[:8]}"
        snap_labels = {"containerd.io/gc.root": "true"}
        if labels:
            snap_labels.update(labels)

        # cached snapshotter path
        if self._snapshotter_value_cache:
            try:
                req = snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=self._snapshotter_value_cache,
                    key=key,
                    parent=parent_chain_id,
                    labels=snap_labels,  # <--- use labels
                )
                resp = self.c.snapshots.Prepare(req)
                logger.debug(f"Using snapshotter '{self._snapshotter_value_cache}'")
                return list(resp.mounts), key
            except grpc.RpcError:
                pass

        # discovery loop
        for snap_val in self._snapshotter_candidates():
            try:
                req = snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=snap_val,
                    key=key,
                    parent=parent_chain_id,
                    labels=snap_labels,  # <--- use labels
                )
                resp = self.c.snapshots.Prepare(req)
                self._snapshotter_value_cache = snap_val
                logger.info(f"Discovered snapshotter '{snap_val}'")
                return list(resp.mounts), key
            except grpc.RpcError:
                continue
        raise RuntimeError("Unable to select snapshotter for containerd Snapshots API.")

    @log_to_file(logger)
    def grpc_unpack(self, image_ref: str, manifest: dict, cfg: dict, snapshotter: str):
        layers = manifest.get("layers", [])
        diff_ids = (cfg.get("rootfs") or {}).get("diff_ids", [])
        if len(diff_ids) != len(layers):
            raise RuntimeError("layers vs diff_ids length mismatch; cannot compute chainIDs.")

        parent_chain = ""
        for i, layer in enumerate(layers):
            cur_chain = _compute_chain_id(diff_ids[:i + 1])

            if self._snap_stat_exists(snapshotter, cur_chain, None):
                parent_chain = cur_chain
                continue

            # Ensure blob exists (namespaced content store)
            dg = layer["digest"]
            if not _blob_exists(self.c.content, dg, retries=5, sleep_sec=0.2, md=self.c.md()):
                raise RuntimeError(
                    f"Missing layer blob in content store: {dg} (image={image_ref}, ns={self.c.namespace})")

            prep_key = f"unpack-{uuid.uuid4().hex[:8]}-{i}"
            prep = self.c.snapshots.Prepare(
                snapshots_pb2.PrepareSnapshotRequest(
                    snapshotter=snapshotter,
                    key=prep_key,
                    parent=parent_chain or "",
                    labels={"containerd.io/gc.root": "true", "managed-by": "dibba"},
                )
            )
            mounts = list(prep.mounts)

            d = descriptor_pb2.Descriptor()
            ParseDict({
                "media_type": layer.get("mediaType") or layer.get("media_type"),
                "digest": dg,
                "size": layer.get("size", 0),
                "annotations": {ANNOTATION_UNCOMPRESSED: diff_ids[i]}
            }, d)

            try:
                self.c.diff.Apply(diff_pb2.ApplyRequest(diff=d, mounts=mounts))
            except grpc.RpcError as e:
                raise RuntimeError(
                    f"diff.Apply failed: {e.code().name} {e.details()} | "
                    f"image={image_ref} layer={i} digest={dg} diff_id={diff_ids[i]} "
                    f"snapshotter={snapshotter} parent={parent_chain or '<none>'}"
                )

            try:
                self.c.snapshots.Commit(
                    snapshots_pb2.CommitSnapshotRequest(
                        snapshotter=snapshotter,
                        name=cur_chain,
                        key=prep_key,
                        labels={"containerd.io/gc.root": "true", "managed-by": "dibba"},
                    )
                )
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.ALREADY_EXISTS:
                    self._snap_remove_active(snapshotter, prep_key, None)
                else:
                    self._snap_remove_active(snapshotter, prep_key, None)
                    raise

            parent_chain = cur_chain

        return parent_chain

    @log_to_file(logger)
    def list_infos(self, snapshotter: Optional[str] = None) -> List[snapshots_pb2.Info]:
        """Return snapshot infos (both committed names and active keys)."""
        snap = snapshotter or self._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs"
        resp = self.c.snapshots.List(snapshots_pb2.ListSnapshotsRequest(snapshotter=snap))
        # stubs vary: entries/snapshots/items...
        for field in ("entries", "snapshots", "items", "list", "results"):
            seq = getattr(resp, field, None)
            if seq:
                return list(seq)
        return []

    @log_to_file(logger)
    def remove_active_by_label(self, match: Dict[str, str], snapshotter: Optional[str] = None) -> Dict[str, Any]:
        """
        Remove active snapshots whose labels include all key=val in 'match'.
        Deletes leaf-first if there is a parent/child relation among the matches.
        """
        snap = snapshotter or self._snapshotter_value_cache or DEFAULT_SNAPSHOTTER or "overlayfs"

        def _match_labels(labels: Dict[str, str]) -> bool:
            for k, v in (match or {}).items():
                if labels.get(k) != v:
                    return False
            return True

        removed, kept = [], []
        while True:
            infos = self.list_infos(snap)
            # Filter to our label set
            mine = []
            name_to_parent = {}
            for i in infos:
                # info fields vary: prefer .labels, .name (committed) or .key (active)
                labels = dict(getattr(i, "labels", {}) or {})
                ident = getattr(i, "key", "") or getattr(i, "name", "")
                parent = getattr(i, "parent", "")
                if not ident:
                    continue
                if _match_labels(labels):
                    mine.append((ident, parent))
                    name_to_parent[ident] = parent

            if not mine:
                break

            # Compute leaves within our filtered set
            names = {n for (n, _) in mine}
            parents = {p for (_, p) in mine if p}
            leaves = [n for (n, p) in mine if n not in parents]

            if not leaves:
                # nothing removable; likely pinned or selection includes only parents
                kept = list(names)
                break

            # Try to remove leaves
            progress = False
            for ident in leaves:
                try:
                    self.c.snapshots.Remove(
                        snapshots_pb2.RemoveSnapshotRequest(snapshotter=snap, key=ident)
                    )
                    removed.append(ident)
                    progress = True
                except grpc.RpcError as e:
                    kept.append(f"{ident} ({e.code().name})")
            if not progress:
                break

        return {"removed": removed, "kept": kept}


# ========== OCI Spec Builder ==========
class OciSpecBuilder:
    @log_to_file(logger)
    def __init__(self, hostname: Optional[str] = None):
        self.hostname = hostname or ""

    @log_to_file(logger)
    def build(self,
              process_args: List[str],
              env: Optional[Dict[str, str]] = None,
              namespaces: Optional[List[Dict]] = None,
              resources: Optional[Union[dict, "ResourceSpec"]] = None,
              cwd: str = "/",
              root_readonly: bool = False,
              volume_mounts: Optional[List[Dict[str, Any]]] = None) -> any_pb2.Any:

        if hasattr(resources, "to_linux_resources_dict"):
            linux_res = resources.to_linux_resources_dict()  # ResourceSpec -> dict
        elif isinstance(resources, dict):
            linux_res = resources
        else:
            linux_res = {}

        default_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        }
        merged_env = dict(default_env)
        if env:
            merged_env.update(env)

        # Default system mounts
        default_mounts = [
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
        ]
        
        # Merge user-provided volume mounts with default mounts
        # User mounts take precedence if they have the same destination
        all_mounts = list(default_mounts)
        if volume_mounts:
            # Try to resolve PVC references if storage system is available
            try:
                from utils.storage.containerd_integration import resolve_volume_mounts_for_containerd
                # If volume_mounts contains PVC references, resolve them
                # Note: volumes list is not available here, so we'll handle direct mounts
                # PVC resolution should happen at the scheduler level before reaching containerd
                resolved_mounts = []
                for user_mount in volume_mounts:
                    # If mount already has hostPath/source, use it directly
                    if user_mount.get('hostPath') or user_mount.get('source'):
                        resolved_mounts.append(user_mount)
                    else:
                        # This might be a PVC reference that wasn't resolved
                        # Log a warning but continue
                        logger.warning(f"Volume mount {user_mount.get('name')} may need PVC resolution")
                        resolved_mounts.append(user_mount)
                volume_mounts = resolved_mounts
            except ImportError:
                # Storage system not available, use mounts as-is
                logger.debug("Storage integration not available, using volume mounts as-is")
            
            for user_mount in volume_mounts:
                # Convert user mount format to OCI mount format
                mount_dest = user_mount.get("mountPath") or user_mount.get("destination") or user_mount.get("containerPath")
                if not mount_dest:
                    logger.warning(f"Skipping mount without destination: {user_mount}")
                    continue
                
                # Check if this destination already exists in default mounts
                existing_idx = None
                for idx, existing in enumerate(all_mounts):
                    if existing.get("destination") == mount_dest:
                        existing_idx = idx
                        break
                
                # Get host path (source)
                host_path = user_mount.get("hostPath") or user_mount.get("source")
                if not host_path:
                    # If no host path specified, use destination as source (for tmpfs or similar)
                    host_path = mount_dest
                    logger.warning(f"No hostPath specified for mount {mount_dest}, using destination as source")
                
                # Ensure host path exists (for directories)
                mount_type = user_mount.get("type") or "bind"
                if mount_type == "bind" and host_path and not host_path.startswith(("/proc", "/sys", "/dev")):
                    try:
                        # Create directory if it doesn't exist
                        if not os.path.exists(host_path):
                            os.makedirs(host_path, mode=0o755, exist_ok=True)
                            logger.info(f"Created host path directory: {host_path}")
                        elif not os.path.isdir(host_path):
                            logger.warning(f"Host path {host_path} exists but is not a directory")
                    except Exception as e:
                        logger.warning(f"Failed to create/verify host path {host_path}: {e}")
                
                # Build OCI mount entry
                oci_mount = {
                    "destination": mount_dest,
                    "type": mount_type,
                    "source": host_path,
                }
                
                # Add options
                options = []
                if user_mount.get("readOnly") or user_mount.get("readonly"):
                    options.append("ro")
                else:
                    options.append("rw")
                
                # Add propagation mode if specified
                propagation = user_mount.get("propagation") or user_mount.get("mountPropagation")
                if propagation:
                    if propagation.upper() in ["PRIVATE", "SHARED", "SLAVE", "RSLAVE", "RUNBINDABLE"]:
                        options.append(propagation.lower())
                
                # Add bind mount option for bind type
                if mount_type == "bind":
                    options.append("bind")
                
                if options:
                    oci_mount["options"] = options
                
                # Replace existing or append new mount
                if existing_idx is not None:
                    all_mounts[existing_idx] = oci_mount
                    logger.info(f"Replaced default mount at {mount_dest} with user mount from {host_path}")
                else:
                    all_mounts.append(oci_mount)
                    logger.info(f"Added volume mount: {mount_dest} -> {host_path} (type: {mount_type})")

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
            "mounts": all_mounts,
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

        if linux_res:
            spec["linux"]["resources"].update(linux_res)

        a = any_pb2.Any()
        a.type_url = OCI_SPEC_TYPEURL
        a.value = json.dumps(spec).encode("utf-8")
        return a

# ========== Container Log Manager (FIFO-based, Kubernetes-style) ==========
def _rfc3339_now() -> str:
    """Get current timestamp in RFC3339 format with microseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ContainerLogManager:
    """
    Kubernetes-like FIFO-based logging:
      - containerd shim writes raw stdout/stderr into FIFOs (CreateTaskRequest stdout/stderr)
      - Dibba reads FIFOs and writes CRI-style lines into 0.log
      - optional rotation
    """
    
    @log_to_file(logger)
    def __init__(self, log_dir: str = LOG_DIR, 
                 max_log_size: int = DEFAULT_MAX_LOG_SIZE,
                 max_log_files: int = DEFAULT_MAX_LOG_FILES):
        self.log_dir = log_dir
        self.max_bytes = max_log_size
        self.max_files = max_log_files
        self._started = set()  # keys we've started streaming for
        
        # Ensure log directory exists
        os.makedirs(self.log_dir, mode=0o755, exist_ok=True)
    
    @log_to_file(logger)
    def _mkfifo(self, path: str):
        """Create FIFO (named pipe) for container logging."""
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        if os.path.exists(path):
            try:
                st = os.stat(path)
                if not stat.S_ISFIFO(st.st_mode):
                    os.remove(path)
                    os.mkfifo(path, 0o600)
            except Exception:
                try:
                    os.remove(path)
                except Exception:
                    pass
                os.mkfifo(path, 0o600)
        else:
            os.mkfifo(path, 0o600)
    
    @log_to_file(logger)
    def _rotate_if_needed(self, log_file: str):
        """Rotate log file if it exceeds max_bytes."""
        try:
            if os.path.getsize(log_file) < self.max_bytes:
                return
        except FileNotFoundError:
            return
        except Exception:
            return
        
        # Rotate: (max_files-1).log -> max_files.log, ..., 0.log -> 1.log
        for i in range(self.max_files, 0, -1):
            src = log_file.replace("0.log", f"{i-1}.log") if i > 1 else log_file
            dst = log_file.replace("0.log", f"{i}.log")
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except Exception:
                    pass
        
        # Recreate 0.log
        try:
            with open(log_file, "a", encoding="utf-8"):
                pass
        except Exception:
            pass
    
    @log_to_file(logger)
    def _write_cri(self, log_file: str, stream: str, flag: str, msg: str):
        """Write CRI-formatted log entry: '<ts> <stdout|stderr> <F|P> <message>'"""
        ts = _rfc3339_now()
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{ts} {stream} {flag} {msg}")
        except Exception:
            pass
    
    @log_to_file(logger)
    def _pump_fifo(self, fifo_path: str, log_file: str, stream_name: str):
        """Read bytes from FIFO and write CRI formatted lines to log file."""
        while True:
            try:
                # Blocks until writer opens the FIFO (containerd shim)
                with open(fifo_path, "rb", buffering=0) as r:
                    buf = b""
                    while True:
                        chunk = r.read(4096)
                        if not chunk:
                            time.sleep(0.05)
                            continue
                        buf += chunk
                        
                        while True:
                            nl = buf.find(b"\n")
                            if nl == -1:
                                # no full line yet
                                if buf:
                                    self._write_cri(
                                        log_file, stream_name, "P",
                                        buf.decode("utf-8", "replace")
                                    )
                                    buf = b""
                                    self._rotate_if_needed(log_file)
                                break
                            
                            line = buf[:nl + 1]
                            buf = buf[nl + 1:]
                            self._write_cri(
                                log_file, stream_name, "F",
                                line.decode("utf-8", "replace")
                            )
                            self._rotate_if_needed(log_file)
            except Exception:
                time.sleep(0.2)
    
    @log_to_file(logger)
    def prepare_paths(self, namespace: str, pod: str, container_name: str, cid: str) -> Dict[str, str]:
        """
        Prepare FIFO paths and log file paths for container logging.
        
        Returns:
            Dict with 'dir', 'stdout_fifo', 'stderr_fifo', 'log_file', 'symlink'
        """
        # Kubernetes uses /var/log/pods/<ns>_<pod>_<uid>/<container>/0.log
        # Use cid to keep uniqueness (since we don't have a separate UID)
        base = os.path.join(self.log_dir, f"{namespace}_{pod}_{cid}", container_name)
        stdout_fifo = os.path.join(base, "stdout.fifo")
        stderr_fifo = os.path.join(base, "stderr.fifo")
        log_file = os.path.join(base, "0.log")
        
        self._mkfifo(stdout_fifo)
        self._mkfifo(stderr_fifo)
        
        # Optional /containers view (handy for tailing)
        containers_dir = os.path.join(self.log_dir, "..", "containers")
        os.makedirs(containers_dir, mode=0o755, exist_ok=True)
        symlink = os.path.join(containers_dir, f"{pod}_{namespace}_{container_name}-{cid}.log")
        try:
            if not os.path.exists(symlink):
                os.symlink(log_file, symlink)
        except Exception:
            pass
        
        return {
            "dir": base,
            "stdout_fifo": stdout_fifo,
            "stderr_fifo": stderr_fifo,
            "log_file": log_file,
            "symlink": symlink,
        }
    
    @log_to_file(logger)
    def start_streaming(self, key: str, stdout_fifo: str, stderr_fifo: str, log_file: str):
        """Start background threads to read from FIFOs and write to log file."""
        if key in self._started:
            return
        self._started.add(key)
        
        t1 = threading.Thread(target=self._pump_fifo, args=(stdout_fifo, log_file, "stdout"), daemon=True)
        t2 = threading.Thread(target=self._pump_fifo, args=(stderr_fifo, log_file, "stderr"), daemon=True)
        t1.start()
        t2.start()
    
    @log_to_file(logger)
    def _get_pod_log_path(self, namespace: str, pod_name: str, pod_uid: str, 
                          container_name: str, instance: int = 0) -> str:
        """
        Generate Kubernetes-style log path (for compatibility with read_logs):
        /var/log/pods/<namespace>_<pod-name>_<pod-uid>/<container-name>/<instance>.log
        """
        pod_dir_name = f"{namespace}_{pod_name}_{pod_uid}"
        pod_path = os.path.join(self.log_dir, pod_dir_name, container_name)
        os.makedirs(pod_path, mode=0o755, exist_ok=True)
        return os.path.join(pod_path, f"{instance}.log")
    
    @log_to_file(logger)
    def _sanitize_for_filename(self, name: str) -> str:
        """Sanitize name for use in file paths (Kubernetes-style)."""
        invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        sanitized = name
        for char in invalid_chars:
            sanitized = sanitized.replace(char, '_')
        return sanitized
    
    @log_to_file(logger)
    def stop_logging(self, container_id: str) -> None:
        """Stop logging for a container (for backward compatibility)."""
        # With FIFO-based logging, streams stop automatically when container exits
        # This method is kept for backward compatibility but doesn't need to do anything
        pass
    
    @log_to_file(logger)
    def get_log_path(self, namespace: str, pod_name: str, pod_uid: str, 
                    container_name: str, instance: int = 0) -> Optional[str]:
        """Get the log file path for a container (without starting logging)."""
        sanitized_pod_name = self._sanitize_for_filename(pod_name)
        sanitized_container_name = self._sanitize_for_filename(container_name)
        log_path = self._get_pod_log_path(namespace, sanitized_pod_name, pod_uid, 
                                          sanitized_container_name, instance)
        return log_path if os.path.exists(log_path) else None
    
    @log_to_file(logger)
    def read_logs(self, namespace: str, pod_name: str, pod_uid: str,
                  container_name: str, instance: int = 0,
                  tail_lines: Optional[int] = None,
                  follow: bool = False,
                  since: Optional[datetime] = None,
                  limit_bytes: Optional[int] = None) -> Dict[str, Any]:
        """
        Read container logs (similar to kubectl logs).
        
        Args:
            namespace: Kubernetes namespace
            pod_name: Pod name
            pod_uid: Pod UID
            container_name: Container name
            instance: Container instance number
            tail_lines: Number of lines to tail (like --tail)
            follow: If True, follow logs (like --follow)
            since: Only return logs after this timestamp (like --since-time)
            limit_bytes: Maximum bytes to return (like --limit-bytes)
            
        Returns:
            Dict with 'logs' (list of log entries) and 'metadata'
        """
        sanitized_pod_name = self._sanitize_for_filename(pod_name)
        sanitized_container_name = self._sanitize_for_filename(container_name)
        log_path = self._get_pod_log_path(namespace, sanitized_pod_name, pod_uid, 
                                          sanitized_container_name, instance)
        
        if not os.path.exists(log_path):
            # Check rotated logs
            rotated_paths = [f"{log_path}.{i}" for i in range(1, self.max_files + 1)]
            all_paths = [log_path] + rotated_paths
        else:
            all_paths = [log_path]
        
        logs = []
        total_bytes = 0
        
        # Read from rotated files in reverse order (newest first)
        for path in reversed(all_paths):
            if not os.path.exists(path):
                continue
            
            try:
                with open(path, 'rb') as f:
                    # For tail_lines, we need to read from end
                    if tail_lines is not None:
                        # Seek to end, then read backwards
                        f.seek(0, 2)  # Seek to end
                        file_size = f.tell()
                        if file_size == 0:
                            continue
                        
                        # Read in chunks from end
                        chunk_size = min(8192, file_size)
                        chunks = []
                        lines_read = 0
                        pos = max(0, file_size - chunk_size)
                        
                        while pos >= 0 and lines_read < tail_lines:
                            f.seek(pos)
                            chunk = f.read(min(chunk_size, file_size - pos))
                            if pos > 0:
                                # Skip partial line at start
                                newline_pos = chunk.find(b'\n')
                                if newline_pos >= 0:
                                    chunk = chunk[newline_pos + 1:]
                            
                            # Count lines in chunk
                            chunk_lines = chunk.count(b'\n')
                            lines_read += chunk_lines
                            chunks.insert(0, chunk)
                            
                            if pos == 0:
                                break
                            pos = max(0, pos - chunk_size)
                        
                        # Combine chunks and split into lines
                        content = b''.join(chunks)
                        lines = content.split(b'\n')
                        # Take last tail_lines
                        lines = lines[-tail_lines:] if len(lines) > tail_lines else lines
                    else:
                        # Read entire file
                        content = f.read()
                        lines = content.split(b'\n')
                        # Remove empty last line if file ends with newline
                        if lines and not lines[-1]:
                            lines.pop()
                    
                    # Filter by timestamp if since is provided
                    if since:
                        filtered_lines = []
                        since_str = since.strftime('%Y-%m-%dT%H:%M:%S').encode('utf-8')
                        for line in lines:
                            if len(line) > len(since_str) and line[:len(since_str)] >= since_str:
                                filtered_lines.append(line)
                        lines = filtered_lines
                    
                    # Apply limit_bytes
                    for line in lines:
                        line_bytes = len(line) + 1  # +1 for newline
                        if limit_bytes and total_bytes + line_bytes > limit_bytes:
                            break
                        logs.append(line)
                        total_bytes += line_bytes
                    
                    # If we've read enough, stop
                    if tail_lines and len(logs) >= tail_lines:
                        break
                    if limit_bytes and total_bytes >= limit_bytes:
                        break
                        
            except Exception as e:
                logger.warning(f"Failed to read log file {path}: {e}")
                continue
        
        # Sort by timestamp (logs should already be mostly sorted, but be safe)
        def extract_timestamp(log_entry: bytes) -> str:
            # Extract timestamp from log entry: "<timestamp> <stream> <data>"
            parts = log_entry.split(b' ', 2)
            if len(parts) >= 2:
                return parts[0].decode('utf-8', errors='ignore')
            return ''
        
        logs.sort(key=extract_timestamp)
        
        # Take only tail_lines if specified
        if tail_lines and len(logs) > tail_lines:
            logs = logs[-tail_lines:]
        
        return {
            'logs': logs,
            'total_bytes': total_bytes,
            'log_path': log_path,
            'follow': follow,  # Note: follow mode requires separate streaming implementation
        }


class _ContainerLogWriter:
    """
    Internal class that handles actual log writing for a container.
    Uses background threads to capture stdout/stderr from containerd shim.
    """
    
    def __init__(self, container_id: str, log_path: str, namespace: str,
                 pod_name: str, container_name: str, pid: Optional[int],
                 max_log_size: int, max_log_files: int,
                 combine_streams: bool,
                 format_entry: callable,
                 rotate_log: callable):
        self.container_id = container_id
        self.log_path = log_path
        self.namespace = namespace
        self.pod_name = pod_name
        self.container_name = container_name
        self.pid = pid
        self.max_log_size = max_log_size
        self.max_log_files = max_log_files
        self.combine_streams = combine_streams
        self.format_entry = format_entry
        self.rotate_log = rotate_log
        
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._file_lock = threading.Lock()
        self._log_file = None
    
    def start(self):
        """Start background threads to capture logs."""
        # Open log file
        os.makedirs(os.path.dirname(self.log_path), mode=0o755, exist_ok=True)
        self._log_file = open(self.log_path, 'ab', buffering=0)  # Unbuffered for real-time
        
        # Try to capture from containerd shim stdout/stderr
        # Method 1: Read from shim's log files (if available)
        stdout_path = os.path.join(self.shim_base_path, "stdout")
        stderr_path = os.path.join(self.shim_base_path, "stderr")
        
        # Method 2: Use containerd Tasks.IO API if available (would require gRPC streaming)
        # For now, we'll use a polling approach with the PID's file descriptors
        
        if self.pid and os.path.exists(f"/proc/{self.pid}"):
            # Capture from /proc/<pid>/fd/1 (stdout) and /proc/<pid>/fd/2 (stderr)
            self._start_fd_capture()
        elif os.path.exists(stdout_path) or os.path.exists(stderr_path):
            # Capture from shim log files
            self._start_shim_file_capture(stdout_path, stderr_path)
        else:
            # Fallback: Periodic log rotation check only (logs will be captured by other means)
            self._start_rotation_monitor()
    
    def _start_fd_capture(self):
        """Capture logs from container's file descriptors."""
        if not self.pid:
            return
        
        def capture_fd(fd_num: int, stream_name: str):
            fd_path = f"/proc/{self.pid}/fd/{fd_num}"
            if not os.path.exists(fd_path):
                logger.warning(f"FD {fd_path} does not exist for container {self.container_id}")
                return
            
            try:
                # Open the fd (read-only)
                with open(fd_path, 'rb') as fd_file:
                    buffer = b''
                    while not self._stop_event.is_set():
                        try:
                            data = fd_file.read(4096)
                            if not data:
                                time.sleep(0.1)
                                continue
                            
                            buffer += data
                            # Process complete lines
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                self._write_log(stream_name, line + b'\n')
                        except (IOError, OSError) as e:
                            if "No such file" in str(e) or "Bad file descriptor" in str(e):
                                # Process/container has exited
                                break
                            time.sleep(0.1)
                    
                    # Write remaining buffer
                    if buffer:
                        self._write_log(stream_name, buffer)
            except Exception as e:
                logger.warning(f"Failed to capture {stream_name} for {self.container_id}: {e}")
        
        # Start capture threads for stdout and stderr
        if not self.combine_streams:
            t1 = threading.Thread(target=capture_fd, args=(1, 'stdout'), daemon=True)
            t2 = threading.Thread(target=capture_fd, args=(2, 'stderr'), daemon=True)
            t1.start()
            t2.start()
            self._threads = [t1, t2]
        else:
            t1 = threading.Thread(target=capture_fd, args=(1, 'stdout'), daemon=True)
            t1.start()
            self._threads = [t1]
    
    def _start_shim_file_capture(self, stdout_path: str, stderr_path: str):
        """Capture logs from containerd shim log files."""
        def tail_file(file_path: str, stream_name: str):
            if not os.path.exists(file_path):
                return
            
            try:
                with open(file_path, 'rb') as f:
                    # Seek to end (tail mode)
                    f.seek(0, 2)
                    while not self._stop_event.is_set():
                        line = f.readline()
                        if line:
                            self._write_log(stream_name, line)
                        else:
                            time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Failed to tail {file_path} for {self.container_id}: {e}")
        
        if os.path.exists(stdout_path):
            t1 = threading.Thread(target=tail_file, args=(stdout_path, 'stdout'), daemon=True)
            t1.start()
            self._threads.append(t1)
        
        if os.path.exists(stderr_path) and not self.combine_streams:
            t2 = threading.Thread(target=tail_file, args=(stderr_path, 'stderr'), daemon=True)
            t2.start()
            self._threads.append(t2)
    
    @property
    def shim_base_path(self):
        """Get shim base path."""
        return f"/run/containerd/io.containerd.runtime.v2.task/{self.namespace}/{self.container_id}"
    
    def _start_rotation_monitor(self):
        """Monitor log file for rotation needs."""
        def monitor():
            while not self._stop_event.is_set():
                time.sleep(60)  # Check every minute
                try:
                    with self._file_lock:
                        self.rotate_log(self.log_path)
                except Exception:
                    pass
        
        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        self._threads.append(t)
    
    def _write_log(self, stream: str, data: bytes):
        """Write log entry to file (thread-safe)."""
        if not data:
            return
        
        with self._file_lock:
            try:
                # Check if rotation is needed
                if os.path.exists(self.log_path) and os.path.getsize(self.log_path) >= self.max_log_size:
                    self._log_file.close()
                    self.rotate_log(self.log_path)
                    self._log_file = open(self.log_path, 'ab', buffering=0)
                
                # Format and write log entry
                entry = self.format_entry(stream, data)
                self._log_file.write(entry)
                self._log_file.flush()
            except Exception as e:
                logger.warning(f"Failed to write log for {self.container_id}: {e}")
    
    def stop(self):
        """Stop log capture."""
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        
        with self._file_lock:
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
    
    def info(self) -> Dict[str, Any]:
        """Get writer information."""
        return {
            'container_id': self.container_id,
            'log_path': self.log_path,
            'namespace': self.namespace,
            'pod_name': self.pod_name,
            'container_name': self.container_name,
            'pid': self.pid,
        }


# ========== CNI Manager ==========
class CniManager:
    """
    Minimal CNI runner.
    - Uses 'cnitool' if present to execute the conflist by name (preferred).
    - Fallback: accepts both *.conflist and *.conf; if *.conf is found it is wrapped
      into an in-memory conflist and we execute the FIRST plugin (commonly 'calico').
      For multi-plugin chains, install cnitool or extend this to iterate plugins.
    """

    @log_to_file(logger)
    def __init__(self, cni_bin_dir: str = CNI_BIN_DIR, cni_conf_dir: str = CNI_CONF_DIR):
        self.cni_bin_dir = cni_bin_dir
        self.cni_conf_dir = cni_conf_dir
        self.cnitool = which("cnitool")

    # ----- shared env for CNI calls -----
    @log_to_file(logger)
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
    @log_to_file(logger)
    def _cnitool_add(self, network_name: str, netns_path: str, env: dict, timeout: int) -> dict:
        cmd = [self.cnitool, "add", network_name, netns_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"cnitool add failed: {res.stderr.strip() or res.stdout.strip()}")
        try:
            return json.loads(res.stdout)
        except Exception:
            return {"raw": res.stdout}

    @log_to_file(logger)
    def _cnitool_del(self, network_name: str, netns_path: str, env: dict, timeout: int):
        cmd = [self.cnitool, "del", network_name, netns_path]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            logger.warning(f"[cni] delete warning: {res.stderr.strip() or res.stdout.strip()}")

    # ======== config discovery supporting .conflist and .conf ========
    @log_to_file(logger)
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

    @log_to_file(logger)
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
    @log_to_file(logger)
    def _plugin_bin(self, plugin_type: str) -> str:
        path = os.path.join(self.cni_bin_dir, plugin_type)
        if not os.path.exists(path):
            raise FileNotFoundError(f"CNI plugin binary '{plugin_type}' not found in {self.cni_bin_dir}")
        return path

    @log_to_file(logger)
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

    @log_to_file(logger)
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
    @log_to_file(logger)
    def add(self, network_name: str, container_id: str, netns_path: str, ifname: str = DEFAULT_IFNAME,
            timeout: int = 20) -> dict:
        env = self._base_env(container_id, netns_path, ifname)
        if self.cnitool:
            return self._cnitool_add(network_name, netns_path, env, timeout)
        return self._direct_add_first_plugin(network_name, container_id, netns_path, ifname, timeout)

    @log_to_file(logger)
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
            logger.warning(f"[cni] delete fallback warning: {e}", exc_info=True)

# ========== Container/Task ==========
class RuntimeManager:
    @log_to_file(logger)
    def __init__(self, client: ContainerdClient, snapshot_mgr: SnapshotManager, logs: Optional[ContainerLogManager] = None):
        self.c = client
        self.snapshots = snapshot_mgr
        self.logs = logs or ContainerLogManager()

    @log_to_file(logger)
    def _any_to_dict(self, a: any_pb2.Any) -> dict:
        """
        Decode an Any (we store the OCI spec here) into a dict when possible.
        """
        if not a or not a.value:
            return {}
        try:
            # Your builder encodes the spec as JSON bytes
            return json.loads(a.value.decode("utf-8"))
        except Exception:
            return {}


    # --- inside RuntimeManager ---

    @log_to_file(logger)
    def _client_for_ns(self, namespace: str) -> "ContainerdClient":
        # new helper to open a namespaced client
        return ContainerdClient(socket=self.c.socket, namespace=namespace)

    @log_to_file(logger)
    def _list_active_snapshot_keys(self, snapshotter: str) -> list[str]:
        """
        Best-effort enumerate active snapshot keys for a snapshotter.
        Different stubs name this call slightly differently; we try common ones.
        """
        keys: list[str] = []
        try:
            # Preferred: List (returns .entries or .snapshots depending on stub)
            req = snapshots_pb2.ListSnapshotsRequest(snapshotter=snapshotter)
            resp = self.c.snapshots.List(req)
            for field in ("entries", "snapshots", "items", "list", "results"):
                seq = getattr(resp, field, None)
                if seq:
                    for it in seq:
                        k = getattr(it, "key", "") or getattr(it, "name", "")
                        if k:
                            keys.append(k)
                    return keys
        except Exception:
            pass

        # Fallback: nothing available
        return keys

    @log_to_file(logger)
    def list_all_namespaces(self) -> list[str]:
    #     """Best-effort: try well-known namespaces by probing a simple call."""
    #     guesses = ["k8s.io", "default", "moby", "testKCR"]
    #     found = []
    #     seen = set()
    #     for ns in guesses:
    #         try:
    #             _ = self._client_for_ns(ns).containers.List(containers_pb2.ListContainersRequest())
    #             if ns not in seen:
    #                 found.append(ns);
    #                 seen.add(ns)
    #         except grpc.RpcError:
    #             pass
    #     return found
        try:
            resp = self.c.namespaces.List(namespace_pb2.ListNamespacesRequest())
            items = getattr(resp, "namespaces", None) or getattr(resp, "items", None) or []
            return [n.name for n in items if getattr(n, "name", None)]
        except Exception:
            # fallback if API differs
            return ["k8s.io", "default", "moby"]

    @log_to_file(logger)
    def _build_tasks_index(self, client) -> dict[str, dict]:
        """
        Return {cid: {"pid": int|None, "status": "..."}} for the client's namespace.
        Uses Tasks.List when available; falls back to one-by-one Get and pidfile.
        """
        index: dict[str, dict] = {}

        # ---- Fast path: List ----
        try:
            from generated.api.services.tasks.v1 import tasks_pb2
            any_list = False
            for t in _iter_list_tasks(client, tasks_pb2):
                any_list = True
                cid = _task_id(t)
                pid = _task_pid(t)
                stat = _task_status(t)
                index[cid] = {"pid": pid, "status": stat}
            if any_list:
                return index
        except Exception:
            pass

        # ---- Fallback: Walk container ids; try Get; fallback to pidfile ----
        try:
            from generated.api.services.containers.v1 import containers_pb2
        except Exception:
            containers_pb2 = None

        cids = []
        if containers_pb2 is not None:
            try:
                clist = client.containers.List(containers_pb2.ListContainersRequest()).containers
                cids = [c.id for c in clist]
            except Exception:
                cids = []

        # If we can’t list containers, nothing else to do here
        for cid in cids:
            pid = None
            stat = "UNKNOWN"
            try:
                # Try Get
                from generated.api.services.tasks.v1 import tasks_pb2 as tpb
                greq = getattr(tpb, "GetRequest")(container_id=cid)
                gresp = client.tasks.Get(greq)
                task = _task_from_get_response(gresp)
                pid = _task_pid(task)
                stat = _task_status(task)
            except grpc.RpcError as e:
                # Not found -> try pidfile
                if e.code().name == "NOT_FOUND":
                    pid = _read_pidfile(client.namespace, cid)
                    stat = "RUNNING" if (pid and os.path.exists(f"/proc/{pid}")) else "NOTFOUND"
                else:
                    # Unknown error -> still try pidfile
                    pid = _read_pidfile(client.namespace, cid)
                    stat = "RUNNING" if (pid and os.path.exists(f"/proc/{pid}")) else "UNKNOWN"
            except Exception:
                pid = _read_pidfile(client.namespace, cid)
                stat = "RUNNING" if (pid and os.path.exists(f"/proc/{pid}")) else "UNKNOWN"

            index[cid] = {"pid": pid, "status": stat}

        return index

    @log_to_file(logger)
    def _task_snapshot(self, client, cid: str) -> dict:
        """
        Robust single-task view. Uses the tasks index first; then a quick Get; then pidfile.
        """
        idx = getattr(self, "_last_tasks_index", None)
        if not isinstance(idx, dict) or getattr(self, "_last_index_ns", None) != client.namespace:
            idx = self._build_tasks_index(client)
            self._last_tasks_index = idx
            self._last_index_ns = client.namespace

        snap = idx.get(cid)
        if snap:
            # if RUNNING but /proc/<pid> disappeared, downgrade to UNKNOWN
            pid = snap.get("pid")
            if snap.get("status") == "RUNNING" and (not pid or not os.path.exists(f"/proc/{pid}")):
                return {"pid": pid, "status": "UNKNOWN"}
            return {"pid": snap.get("pid"), "status": snap.get("status")}

        # Fallback: try Get just for this one
        try:
            from generated.api.services.tasks.v1 import tasks_pb2 as tpb
            g = client.tasks.Get(tpb.GetRequest(container_id=cid))
            task = _task_from_get_response(g)
            return {"pid": _task_pid(task), "status": _task_status(task)}
        except grpc.RpcError as e:
            if e.code().name == "NOT_FOUND":
                pid = _read_pidfile(client.namespace, cid)
                return {"pid": pid, "status": "RUNNING" if (pid and os.path.exists(f"/proc/{pid}")) else "NOTFOUND"}
            return {"pid": None, "status": "UNKNOWN"}
        except Exception:
            pid = _read_pidfile(client.namespace, cid)
            return {"pid": pid, "status": "RUNNING" if (pid and os.path.exists(f"/proc/{pid}")) else "UNKNOWN"}

    @log_to_file(logger)
    def _container_brief(self, client: ContainerdClient, c) -> dict:
        """Small, robust summary for any container c in a namespace."""
        cid = getattr(c, "id", "")
        img = getattr(c, "image", "")
        # labels is a map<string,string> in proto; make a plain dict defensively
        labels = {}
        try:
            labels = dict(getattr(c, "labels", {}) or {})
        except Exception:
            pass

        name = labels.get("app") or labels.get("name") or cid
        snap = self._task_snapshot(client, cid)  # {'pid':..., 'status':...}

        return {
            "id": cid,
            "name": name,
            "image": img,
            "labels": labels,
            "pid": snap.get("pid"),
            "status": snap.get("status"),
        }

    @log_to_file(logger)
    def list_pods_in_namespace(self, namespace: str) -> list[dict]:
        """
        Return a list of pause/pod summaries in a namespace.
        We mark pause with labels {"pod": <name>, "role": "pause"} during creation.
        """
        client = self._client_for_ns(namespace)
        try:
            clist = client.containers.List(containers_pb2.ListContainersRequest()).containers
        except grpc.RpcError as e:
            raise RuntimeError(f"ListContainers failed in ns={namespace}: {e}")

        pods = []
        for c in clist:
            labels = dict(getattr(c, "labels", {}))
            if labels.get("role") == "pause" and "pod" in labels:
                cid = c.id
                pod_name = labels["pod"]
                snap = self._task_snapshot(client, cid)
                pods.append({
                    "name": pod_name,
                    "pause_cid": cid,
                    "image": c.image,
                    "task": snap,
                })
        return pods


    @log_to_file(logger)
    def stop_and_delete_task_in_client(self, client: ContainerdClient, cid: str,
                                       kill_signal: int = 15,
                                       timeouts: Tuple[float, float] = (3.0, 10.0)) -> None:
        """
        Same as stop_and_delete_task(), but operates on an explicit namespaced client.
        This avoids accidentally using the wrong namespace stub.
        """
        # Kill
        try:
            client.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=kill_signal),
                              timeout=timeouts[0])
        except grpc.RpcError:
            pass

        # Wait
        try:
            client.tasks.Wait(tasks_pb2.WaitRequest(container_id=cid), timeout=2.0)
        except Exception:
            pass

        # Delete task
        try:
            del_req_cls = getattr(tasks_pb2, "DeleteTaskRequest", None) or getattr(tasks_pb2, "DeleteRequest", None)
            if del_req_cls:
                client.tasks.Delete(del_req_cls(container_id=cid), timeout=timeouts[1])
        except grpc.RpcError:
            # fallback SIGKILL + delete
            try:
                client.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=9), timeout=timeouts[0])
            except grpc.RpcError:
                pass
            try:
                del_req_cls = getattr(tasks_pb2, "DeleteTaskRequest", None) or getattr(tasks_pb2, "DeleteRequest", None)
                if del_req_cls:
                    client.tasks.Delete(del_req_cls(container_id=cid), timeout=timeouts[1])
            except grpc.RpcError:
                pass

        # Delete container object
        try:
            client.containers.Delete(containers_pb2.DeleteContainerRequest(id=cid))
        except grpc.RpcError:
            pass

    @log_to_file(logger)
    def list_pods_and_apps_in_namespace(self, namespace: str) -> list[dict]:
        """
        Return a list of pod summaries in the given namespace.
        - If containers are labeled with role=pause + pod=<name>, we group apps under that pod.
        - If a container lacks those labels (standalone), we emit a 'pseudo-pod' with that single app.
        """
        client = self._client_for_ns(namespace)

        # enumerate all containers in the namespace
        try:
            clist = client.containers.List(containers_pb2.ListContainersRequest()).containers
        except grpc.RpcError as e:
            logger.error(f"[{namespace}] containers.List error: {e}")
            return []

        pods: dict[str, dict] = {}
        standalone: list[dict] = []

        for c in clist:
            info = self._container_brief(client, c)
            labels = info["labels"]
            cid = info["id"]

            pod_label = labels.get("pod")
            role = labels.get("role", "")

            if pod_label:
                # ensure pod bucket exists
                p = pods.setdefault(pod_label, {
                    "pod_id": pod_label,
                    "pause": {"pid": None, "status": "NOTFOUND"},
                    "apps": [],
                })
                if role == "pause":
                    # pause container represents the pod
                    p["pause"] = {"pid": info["pid"], "status": info["status"]}
                    # If you prefer the literal pause container id as pod_id, uncomment:
                    # p["pod_id"] = cid
                else:
                    p["apps"].append({
                        "id": cid,
                        "name": info["name"],
                        "image": info["image"],
                        "pid": info["pid"],
                        "status": info["status"],
                    })
            else:
                # No pod label: treat as a standalone pseudo-pod so it shows up (e.g., calico-node)
                standalone.append({
                    "pod_id": cid,  # pseudo-pod uses its own id
                    "pause": {"pid": None, "status": "STANDALONE"},
                    "apps": [{
                        "id": cid,
                        "name": info["name"],
                        "image": info["image"],
                        "pid": info["pid"],
                        "status": info["status"],
                    }],
                })

        # Build final list: real pods first (stable order), then standalone
        out: list[dict] = []
        for pod_id in sorted(pods.keys()):
            out.append(pods[pod_id])
        out.extend(sorted(standalone, key=lambda x: x["pod_id"]))

        return out

    @log_to_file(logger)
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
                    snapshotter=self.snapshots.snapshotter(),
                )
            )
        )

    @log_to_file(logger)
    def start_task(self,
                   cid: str,
                   mounts,
                   tty: bool = False,
                   create_timeout=15.0,
                   start_timeout=30.0,
                   # NEW: metadata for log directory layout
                   namespace: str | None = None,
                   pod: str | None = None,
                   container_name: str | None = None) -> int:
        # If tty=True, stdout/stderr are generally merged; keep old behavior.
        stdout_path = ""
        stderr_path = ""
        log_info = None
        
        # Enable Dibba-style logging if we have enough info and tty is off
        if (not tty) and pod and container_name:
            ns = namespace or getattr(self.c, "namespace", "default")
            log_info = self.logs.prepare_paths(ns, pod, container_name, cid)
            
            # IMPORTANT:
            # containerd shim expects paths; FIFOs work well here.
            stdout_path = log_info["stdout_fifo"]
            stderr_path = log_info["stderr_fifo"]
            
            key = f"{ns}/{pod}/{container_name}/{cid}"
            self.logs.start_streaming(key, stdout_path, stderr_path, log_info["log_file"])
            logger.info(f"[logs] {cid} -> {log_info['log_file']} (symlink={log_info.get('symlink')})")
        
        create_req = tasks_pb2.CreateTaskRequest(
            container_id=cid,
            terminal=tty,
            rootfs=mounts,
            stdout=stdout_path,
            stderr=stderr_path,
        )
        
        self.c.tasks.Create(create_req, timeout=create_timeout)
        resp = self.c.tasks.Start(tasks_pb2.StartRequest(container_id=cid), timeout=start_timeout)
        return resp.pid

    @log_to_file(logger)
    def stop_and_delete_task(self, cid: str, kill_signal: int = 15,
                             timeouts: Tuple[float, float] = (3.0, 10.0)) -> None:
        """
        Best-effort: signal -> wait -> delete task -> delete container
        """
        # 1) Kill (TERM by default)
        try:
            self.c.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=kill_signal),
                              timeout=timeouts[0])
        except grpc.RpcError:
            pass

        # 2) Wait briefly (helps remove shim cleanly)
        try:
            self.c.tasks.Wait(tasks_pb2.WaitRequest(container_id=cid), timeout=2.0)
        except Exception:
            pass

        # 3) Delete task
        try:
            del_req_cls = getattr(tasks_pb2, "DeleteTaskRequest", None) or getattr(tasks_pb2, "DeleteRequest", None)
            if del_req_cls:
                self.c.tasks.Delete(del_req_cls(container_id=cid), timeout=timeouts[1])
        except grpc.RpcError:
            # fallback to SIGKILL then delete
            try:
                self.c.tasks.Kill(tasks_pb2.KillRequest(container_id=cid, signal=9), timeout=timeouts[0])
            except grpc.RpcError:
                pass
            try:
                del_req_cls = getattr(tasks_pb2, "DeleteTaskRequest", None) or getattr(tasks_pb2, "DeleteRequest", None)
                if del_req_cls:
                    self.c.tasks.Delete(del_req_cls(container_id=cid), timeout=timeouts[1])
            except grpc.RpcError:
                pass

        # 4) Delete container object
        try:
            self.c.containers.Delete(containers_pb2.DeleteContainerRequest(id=cid))
        except grpc.RpcError:
            pass

    @log_to_file(logger)
    def delete_task_only(self, cid: str, timeouts: tuple[float, float] = (3.0, 10.0)) -> None:
        """
        Delete a task even if its container object no longer exists.
        Try the common DeleteTaskRequest; fall back to DeleteRequest if needed.
        """
        try:
            # Prefer DeleteTaskRequest if present
            from generated.api.services.tasks.v1 import tasks_pb2 as tpb
            req_cls = getattr(tpb, "DeleteTaskRequest", None) or getattr(tpb, "DeleteRequest")
            if req_cls is None:
                return
            self.c.tasks.Delete(req_cls(container_id=cid), timeout=timeouts[1])
        except grpc.RpcError:
            # If it fails, try a last-chance Kill then Delete again
            try:
                from generated.api.services.tasks.v1 import tasks_pb2 as tpb
                self.c.tasks.Kill(tpb.KillRequest(container_id=cid, signal=9), timeout=timeouts[0])
                req_cls = getattr(tpb, "DeleteTaskRequest", None) or getattr(tpb, "DeleteRequest")
                self.c.tasks.Delete(req_cls(container_id=cid), timeout=timeouts[1])
            except grpc.RpcError:
                pass

    @log_to_file(logger)
    def prune_orphan_tasks(self, namespace: str, aggressive: bool = False) -> dict:
        """
        Remove tasks that have no container or are STOPPED.
        aggressive=True also force-cleans shim dirs if Delete doesn't clear them.
        """
        client = self._client_for_ns(namespace)
        tstub = client.tasks
        cstub = client.containers

        removed, kept = [], []

        # list tasks (streaming or simple list depending on your _iter_list_tasks helper)
        from generated.api.services.tasks.v1 import tasks_pb2 as _tpb
        for t in _iter_list_tasks(client, _tpb):
            tid = _task_id(t)
            # 1) Is there a container?
            container_exists = True
            try:
                cstub.Get(containers_pb2.GetContainerRequest(id=tid))
            except grpc.RpcError as e:
                if e.code().name in ("NOT_FOUND", "INVALID_ARGUMENT"):
                    container_exists = False
                else:
                    # Unknown error: keep it for safety.
                    kept.append({"id": tid, "reason": f"containers.Get error: {e.code().name}"})
                    continue

            # 2) Decide if we should remove the task
            #should_remove = (not container_exists) or (t.status == tasks_pb2.STOPPED)
            status_val = getattr(t, "status", None)
            is_stopped = (status_val == getattr(_tpb, "STOPPED", None)) or (
                        str(status_val).upper() == "STOPPED")
            should_remove = (not container_exists) or is_stopped


            if not should_remove:
                kept.append({"id": tid, "reason": "running and has container"})
                continue

            # 3) Best-effort kill -> wait -> delete
            try:
                # If container doesn't exist, Kill will always be NOT_FOUND — skip it.
                if container_exists:
                    try:
                        tstub.Kill(_tpb.KillRequest(container_id=tid, signal=signal.SIGKILL))
                    except grpc.RpcError:
                        pass


                # Short wait so exit is recorded
                try:
                    #tstub.Wait(tasks_pb2.WaitRequest(container_id=tid), timeout=1)
                    tstub.Wait(_tpb.WaitRequest(container_id=tid), timeout=1)
                except Exception:
                    pass

                # Delete the task (this should remove the shim)
                #tstub.Delete(tasks_pb2.DeleteTaskRequest(container_id=tid))
                del_req = getattr(_tpb, "DeleteTaskRequest", None)
                if del_req is None:
                    del_req = getattr(_tpb, "DeleteRequest")  # older stubs
                tstub.Delete(del_req(container_id=tid))
                removed.append({"id": tid, "action": "Tasks.Delete"})
            except grpc.RpcError as e:
                # Last resort: shim dir cleanup if requested
                if aggressive:
                    shim = f"/run/containerd/io.containerd.runtime.v2.task/{namespace}/{tid}"
                    try:
                        # only remove empty or obviously stale dirs; be cautious
                        if os.path.isdir(shim):
                            for root, dirs, files in os.walk(shim, topdown=False):
                                for name in files:
                                    try:
                                        os.remove(os.path.join(root, name))
                                    except Exception:
                                        pass
                                for name in dirs:
                                    try:
                                        os.rmdir(os.path.join(root, name))
                                    except Exception:
                                        pass
                            os.rmdir(shim)
                        removed.append({"id": tid, "action": f"shim_dir_rm ({shim})"})
                    except Exception as ee:
                        kept.append({"id": tid, "reason": f"Delete failed: {e.code().name}, shim rm err: {ee}"})
                else:
                    kept.append({"id": tid, "reason": f"Delete failed: {e.code().name}"})

        return {"removed": removed, "kept": kept}

    def prune_namespace(self, namespace: str) -> dict:
        """
        Convenience: remove STOPPED/orphan tasks, then you can also call your snapshot cleanup.
        """
        res = self.prune_orphan_tasks(namespace, aggressive=True)
        # optionally call your existing _snap_remove_active(...) patterns after:
        # self.snapshot_mgr._snap_remove_active(self._snapshotter_name(), "<prefix>")
        return res


    @log_to_file(logger)
    def get_container_info(self, cid: str) -> Dict:
        """
        Return merged info about a container and its (optional) task.
        - From Containers API: image, labels, runtime, snapshotter, OCI spec
        - From Tasks API: pid + a best-effort status (if task exists)
        """
        info: Dict = {"id": cid, "task": {}}

        # --- Containers API ---
        try:
            resp = self.c.containers.Get(
                containers_pb2.GetContainerRequest(id=cid)
            )
            c = resp.container
            info.update({
                "image": c.image,
                "labels": dict(c.labels),
                "runtime": c.runtime.name if c.runtime and c.runtime.name else "",
                "snapshotter": c.snapshotter,
                "spec": self._any_to_dict(c.spec),
            })
        except grpc.RpcError as e:
            # Not found or another error — return minimal info
            info["error"] = f"containers.Get: {e.code().name}: {e.details()}"
            return info

        # --- Tasks API (optional) ---
        # Try State first (commonly available), fall back to Get if needed.
        task_pid = None
        task_status = None

        # Try State
        try:
            st = self.c.tasks.State(
                tasks_pb2.StateRequest(container_id=cid)
            )
            # StateResponse usually has pid and status enum/string
            if hasattr(st, "pid"):
                task_pid = st.pid
            if hasattr(st, "status"):
                # status can be an enum int or string depending on generated stubs
                task_status = getattr(st, "status", None)
        except grpc.RpcError:
            # Fallback: Get
            try:
                gt = self.c.tasks.Get(
                    tasks_pb2.GetRequest(container_id=cid)
                )
                if hasattr(gt, "task") and getattr(gt.task, "pid", 0):
                    task_pid = gt.task.pid
                # 'Get' may not include status; leave as None if not present
            except grpc.RpcError:
                pass

        if task_pid is not None:
            info["task"]["pid"] = task_pid
        if task_status is not None:
            # normalize to string for readability if it’s an enum/int
            info["task"]["status"] = str(task_status)

        return info

# ========== Pod Manager ==========
class PodManager:
    @log_to_file(logger)
    def __init__(self, client: ContainerdClient, log_manager: Optional[ContainerLogManager] = None):
        self.c = client
        self.images = ImageResolver(client)
        self.snaps = SnapshotManager(client)
        log_mgr = log_manager or ContainerLogManager()
        self.runtime = RuntimeManager(client, self.snaps, logs=log_mgr)
        self.cni = CniManager()
        self.log_manager = log_mgr

    @log_to_file(logger)
    def pull_image(self, image_ref: str, username: str | None = None, password: str | None = None ) -> dict:
        """
        Ensure an image is available for this namespace.

        1) First check containerd's native Images service (namespaced).
        2) Only if not found, use CRI PullImage as a fallback.
        """
        ns = getattr(self.c, "namespace", None) or NAMESPACE
        # ----- 1) Native containerd check in this namespace -----
        try:
            resolved = self.images.resolve_image_name(image_ref)
            # If this didn't raise NOT_FOUND, the image is already present in this ns.
            msg = f"Image available in containerd namespace '{self.c.namespace}': {resolved}"
            logger.info(msg)
            return {
                "ok": True,
                "image_ref": resolved,
                "namespace": ns,
                "message": f"Image available: {resolved}",
                "source": "containerd",
            }

        except Exception as e:
            # Defensive: don't blow up here, just log and try CRI.
            logger.warning(
                f"[pull_image] unexpected error while resolving {image_ref} "
                f"in ns={self.c.namespace}: {e}"
            )
            pass

        # 2) Native pull (Option B)
        try:
            res = self.pull_image_native(image_ref, username=username, password=password)
            if res.get("ok"):
                return res
            # if it returned ok=False, fall through (or return directly if you want)
            logger.warning(f"[pull_image] native pull returned not ok: {res}")
        except Exception as e:
            logger.warning(f"[pull_image] native pull failed: {e}", exc_info=True)

        # ----- 3) CRI pull fallback (works against CRI plugin namespace, unknown which) -----
        cri_sock = os.environ.get("CRI_SOCKET", "/run/containerd/containerd.sock")
        cri = _CRIImageClient(socket_target=cri_sock)

        pulled = cri.pull(image_ref)
        if not pulled:
            msg = f"Failed to pull image via CRI: {image_ref}"
            logger.error(msg)
            return {"ok": False, "image_ref": None, "namespace": ns, "source": "cri",
                    "message": f"pull failed: {image_ref}"}

        # IMPORTANT:
        # CRI may have stored the image into some namespace that is NOT self.c.namespace.
        # So: find it anywhere, then import the Image record into our destination namespace.
        dst_ns = self.c.namespace
        sock = self.c.socket

        try:
            src_ns, src_name = _find_image_in_any_namespace(
                socket=sock,
                image_ref=pulled,
                candidates=_candidates_for_ref(pulled),
                skip={dst_ns},
                probe_namespace=dst_ns,  # <-- important
            )

            # If not found by pulled, try by original ref too
            if not src_ns:
                src_ns, src_name = _find_image_in_any_namespace(
                    socket=sock,
                    image_ref=image_ref,
                    candidates=_candidates_for_ref(image_ref),
                    skip={dst_ns},
                )

            if not src_ns or not src_name:
                # CRI said success but containerd Images API can't find it anywhere (rare but possible)
                logger.warning(
                    f"CRI pulled {pulled}, but Images API can't locate it in any namespace; returning pulled ref anyway")
                return {
                    "ok": True,
                    "image_ref": pulled,
                    "namespace": dst_ns,
                    "message": f"Image pulled via CRI but not visible via Images API; using {pulled}",
                    "source": "cri",
                    "cri_returned": pulled,
                    "imported_from_namespace": None,
                }

            # Import record into destination namespace
            imported_name = _import_image_record_namespace_to_namespace(
                socket=sock,
                src_ns=src_ns,
                dst_ns=dst_ns,
                image_name_in_src=src_name,
                dst_name=image_ref,  # keep the name you requested in dst namespace
            )

            # Now resolve in our namespace (should succeed)
            final_ref = self.images.resolve_image_name(imported_name)

            # CRITICAL: import copied only the Image record; blobs may be missing in dst namespace.
            # Force a native pull into THIS namespace to populate content store.
            try:
                fill = self.pull_image_native(final_ref, username=username, password=password)
                if not fill.get("ok"):
                    logger.warning(f"[pull_image] native fill after CRI+import returned not ok: {fill}")
            except Exception as e:
                logger.warning(f"[pull_image] native fill after CRI+import failed: {e}", exc_info=True)

            logger.info(f"Imported image record from ns={src_ns} -> ns={dst_ns} as {final_ref}")
            return {
                "ok": True,
                "image_ref": final_ref,
                "namespace": dst_ns,
                "message": f"Image pulled via CRI, imported, and content populated in namespace: {final_ref}",
                "source": "cri+import+filled",
                "cri_returned": pulled,
                "imported_from_namespace": src_ns,
                "imported_from_name": src_name,
            }

        except Exception as e:
            logger.warning(f"CRI pull succeeded but import/discovery failed: {e}", exc_info=True)
            # Still return pulled ref so caller can decide next step
            return {
                "ok": True,
                "image_ref": pulled,
                "namespace": dst_ns,
                "message": f"Image pulled via CRI but import failed: {e}",
                "source": "cri",
                "cri_returned": pulled,
                "imported_from_namespace": None,
            }

    @log_to_file(logger)
    def pull_image_native(self, image_ref: str, username: str | None = None, password: str | None = None) -> dict:
        ns = self.c.namespace

        # already present?
        try:
            resolved = self.images.resolve_image_name(image_ref)
            return {"ok": True, "image_ref": resolved, "namespace": ns, "source": "containerd",
                    "message": "already present"}
        except Exception:
            pass

        registry, repo, reference = _split_image_ref(image_ref)
        reg = RegistryV2Client(registry, username=username, password=password)

        # 1) fetch tag (could be index or manifest)
        tag_doc, tag_mt, tag_headers, tag_body = reg.get_manifest_with_headers(repo, reference)
        tag_digest = (tag_headers.get("Docker-Content-Digest") or "").strip() or None

        # IMPORTANT: if server returned a digest for the tag object, store that blob too
        # (This is the index/list blob when tag points to an index)
        if tag_digest and tag_body:
            _content_write_and_commit(self.c.content, tag_digest, tag_body, labels={
                "containerd.io/gc.root": "true",
                "managed-by": "dibba",
                "containerd.io/distribution.source.ref": image_ref,
                "kind": "tag-manifest-or-index",
            })

        # 2) if index/list -> pick platform manifest and fetch THAT digest
        if _is_index(tag_mt) or bool(tag_doc.get("manifests")):
            manifests = tag_doc.get("manifests") or []
            chosen = None
            for m in manifests:
                plat = (m.get("platform") or {})
                if plat.get("os") == PLATFORM_OS and plat.get("architecture") == PLATFORM_ARCH:
                    chosen = m
                    break
            chosen = chosen or (manifests[0] if manifests else None)
            if not chosen:
                return {"ok": False, "namespace": ns, "message": f"no manifests found in index for {image_ref}"}

            manifest_ref = chosen["digest"]  # digest to fetch
            man_doc, man_mt, man_headers, man_body = reg.get_manifest_with_headers(repo, manifest_ref)
            manifest_digest = (man_headers.get("Docker-Content-Digest") or "").strip() or manifest_ref
        else:
            # concrete manifest already
            man_doc, man_mt, man_headers, man_body = tag_doc, tag_mt, tag_headers, tag_body
            # For a normal manifest, Docker-Content-Digest is authoritative
            manifest_digest = (man_headers.get("Docker-Content-Digest") or "").strip() or tag_digest
            if not manifest_digest:
                return {"ok": False, "namespace": ns, "message": f"could not determine manifest digest for {image_ref}"}

        # 2b) ALWAYS store the selected manifest blob itself
        if manifest_digest and man_body:
            _content_write_and_commit(self.c.content, manifest_digest, man_body, labels={
                "containerd.io/gc.root": "true",
                "managed-by": "dibba",
                "containerd.io/distribution.source.ref": image_ref,
                "kind": "manifest",
            })

        # 3) fetch config + layers
        cfg_desc = man_doc["config"]
        cfg_digest = cfg_desc["digest"]
        cfg_bytes = reg.get_blob(repo, cfg_digest)

        layer_descs = man_doc.get("layers") or []
        layer_digests = [l["digest"] for l in layer_descs]

        # 4) write config + layers into content store (namespaced)
        _content_write_and_commit(self.c.content, cfg_digest, cfg_bytes, labels={
            "containerd.io/gc.root": "true",
            "managed-by": "dibba",
            "containerd.io/distribution.source.ref": image_ref,
        })
        for dg in layer_digests:
            blob = reg.get_blob(repo, dg)
            _content_write_and_commit(self.c.content, dg, blob, labels={
                "containerd.io/gc.root": "true",
                "managed-by": "dibba",
                "containerd.io/distribution.source.ref": image_ref,
            })

        # 5) create image record pointing at MANIFEST digest (authoritative)
        target = descriptor_pb2.Descriptor()
        ParseDict({
            "media_type": man_mt,
            "digest": manifest_digest,
            "size": 0
        }, target)

        _images_create_or_update(self.c.images, image_ref, target)

        resolved = self.images.resolve_image_name(image_ref)
        return {"ok": True, "image_ref": resolved, "namespace": ns, "source": "native",
                "message": "pulled into namespace"}

    @log_to_file(logger)
    def _ensure_unpacked(self, image_ref: str) -> None:
        """
        Ensure the image is:
          1) present as an Images record in THIS namespace
          2) has config+layer blobs present in THIS namespace content store
          3) unpacked into snapshots (chain IDs) for THIS namespace snapshotter

        This method is defensive about the common “pulled via CRI but blobs/record are in a
        different namespace” problem by *always* doing a native fill pull into the target
        namespace when blobs are missing.
        """

        @log_to_file(logger)
        def _layers_from_manifest(m: dict) -> list[str]:
            return [l.get("digest") for l in (m.get("layers") or []) if l.get("digest")]

        @log_to_file(logger)
        def _config_digest_from_manifest(m: dict) -> str | None:
            cfg = (m.get("config") or {})
            return cfg.get("digest")

        # 0) Make sure snapshotter is stable before any unpack work
        snap = self.snaps.ensure_snapshotter_discovered()

        # 1) Ensure Images record exists in THIS namespace (and attempt to import if CRI was used)
        pres = self.pull_image(image_ref)
        if not pres.get("ok"):
            raise RuntimeError(pres.get("message") or f"pull_image failed: {image_ref}")

        # Use the best ref we have (often normalized / fully qualified)
        image_ref = pres.get("image_ref") or image_ref

        # 2A) Guard: ensure the image target (manifest list/index or manifest) blob exists in THIS namespace.
        #     Your error is: content digest <target.digest> not found during resolve_manifest() -> Content.Read().
        try:
            img = self.c.images.Get(
                images_pb2.GetImageRequest(name=image_ref),
                metadata=self.c.md(),
            ).image
            tgt = img.target  # descriptor: digest + mediaType
            if tgt and tgt.digest and not self.c.content_exists(tgt.digest):
                logger.warning(
                    f"[ensure_unpacked] image target blob missing in ns={self.c.namespace} "
                    f"(digest={tgt.digest}). Doing native pull to populate content store."
                )
                fill = self.pull_image_native(image_ref)
                if not fill.get("ok"):
                    raise RuntimeError(f"Native fill pull failed while fixing missing target blob: {fill}")
                image_ref = fill.get("image_ref") or image_ref
        except Exception as e:
            # If we can't even read image metadata, fall back to your existing native fill path.
            logger.warning(f"[ensure_unpacked] could not validate target digest for {image_ref}: {e}; trying native fill")
            fill = self.pull_image_native(image_ref)
            if not fill.get("ok"):
                raise RuntimeError(f"Native fill pull failed after target digest validation error: {fill}")
            image_ref = fill.get("image_ref") or image_ref


        # 2) Resolve manifest+config descriptors via Images API (namespaced)
        #    If this fails after pull_image, we try one more native fill.
        # 2) Resolve manifest+config descriptors via Images API
        try:
            manifest_desc = self.images.resolve_manifest(image_ref)
            manifest, cfg = self.images.load_manifest_and_config(manifest_desc)

        except grpc.RpcError as e:
            # If the *target digest* is missing in content store, the image record is stale/broken.
            msg = str(e)
            if "content digest" in msg and "not found" in msg:
                logger.warning(
                    f"[ensure_unpacked] resolve_manifest failed due to missing content in ns={self.c.namespace}. "
                    f"Deleting image record and forcing ctr pull: {image_ref}"
                )
                self.images.delete_image_record(image_ref)   # add helper belowa
                self.images.pull_image_ctr(image_ref)               # add helper below

                # retry after hard repair
                manifest_desc = self.images.resolve_manifest(image_ref)
                manifest, cfg = self.images.load_manifest_and_config(manifest_desc)
            else:
                raise


        # 3) Verify content store has config + all layers (namespaced content store!)
        layer_digests = _layers_from_manifest(manifest)
        cfg_digest = _config_digest_from_manifest(manifest) or (cfg.get("rootfs") and None)

        missing = []
        if cfg_digest and not _blob_exists(self.c.content, cfg_digest, retries=2, sleep_sec=0.2,md=self.c.md()):
            missing.append(cfg_digest)
        for dg in layer_digests:
            if not _blob_exists(self.c.content, dg, retries=2, sleep_sec=0.2,md=self.c.md()):
                missing.append(dg)

        if missing:
            # The single most important fix: do a native pull into THIS namespace to populate blobs.
            logger.info(
                f"[ensure_unpacked] missing {len(missing)} blob(s) in ns={self.c.namespace}; "
                f"native pulling to populate content store (example={missing[0]})"
            )
            fill = self.pull_image_native(image_ref)
            if not fill.get("ok"):
                raise RuntimeError(f"Native pull failed while fixing missing blobs: {fill}")

            # Re-resolve and re-load after native pull
            image_ref = fill.get("image_ref") or image_ref
            manifest_desc = self.images.resolve_manifest(image_ref)
            manifest, cfg = self.images.load_manifest_and_config(manifest_desc)

            # Re-check (with small retries)
            layer_digests = _layers_from_manifest(manifest)
            cfg_digest = _config_digest_from_manifest(manifest)

            still_missing = []
            if cfg_digest and not _blob_exists(self.c.content, cfg_digest, retries=6, sleep_sec=0.25,md=self.c.md()):
                still_missing.append(cfg_digest)
            for dg in layer_digests:
                if not _blob_exists(self.c.content, dg, retries=6, sleep_sec=0.25,md=self.c.md()):
                    still_missing.append(dg)

            if still_missing:
                raise RuntimeError(
                    f"Content still missing after native pull in ns={self.c.namespace}. "
                    f"Example missing digest: {still_missing[0]}"
                )

        # 4) Unpack into snapshots (chain IDs) for THIS namespace snapshotter
        #    grpc_unpack() will skip layers whose chain snapshot already exists.
        self.snaps.grpc_unpack(image_ref, manifest, cfg, snapshotter=snap)

    @log_to_file(logger)
    def _build_runtime_pod_struct(self, namespace: str, pod_name: str,
                                  cni_network: str = DEFAULT_CNI_NET_NAME,
                                  ifname: str = DEFAULT_IFNAME) -> tuple[Optional[Dict], List[Dict]]:
        """
        Inspect the namespace to reconstruct a minimal 'pod' dict (as expected by PodManager.delete_pod)
        and the list of app dicts for that pod.

        Returns: (pod_dict | None, apps_list)
        """
        # namespaced client for queries
        client = self.runtime._client_for_ns(namespace)

        try:
            clist = client.containers.List(containers_pb2.ListContainersRequest()).containers
        except Exception as e:
            logger.error(f"[{namespace}] containers.List error: {e}", exc_info=True)
            return None, []

        pause_cid = None
        pause_pid = None

        apps: List[Dict] = []
        # First pass: find pause container for this pod
        for c in clist:
            labels = dict(getattr(c, "labels", {}) or {})
            if labels.get("pod") == pod_name and labels.get("role") == "pause":
                pause_cid = c.id
                snap = self.runtime._task_snapshot(client, pause_cid)
                pause_pid = snap.get("pid")
                break

        if not pause_cid:
            # Pod not present (or never labeled as pause)
            return None, []

        # Build ns paths if we have a live pid
        ns_paths = {}
        if pause_pid and os.path.exists(f"/proc/{pause_pid}"):
            ns_base = f"/proc/{pause_pid}/ns"
            ns_paths = {
                "pid": f"{ns_base}/pid",
                "net": f"{ns_base}/net",
                "ipc": f"{ns_base}/ipc",
                "uts": f"{ns_base}/uts",
            }
        else:
            # delete_pod() will gracefully handle missing netns by using "" for DEL
            ns_paths = {"pid": "", "net": "", "ipc": "", "uts": ""}

        pod = {
            "name": pod_name,
            "pause": {"cid": pause_cid, "pid": pause_pid},
            "ns": ns_paths,
            "cni": {"network": cni_network, "ifname": ifname},
            # snapshot_key is optional; if unknown we omit and cleanup will skip removing it
        }

        # Second pass: collect app containers for this pod
        for c in clist:
            labels = dict(getattr(c, "labels", {}) or {})
            if labels.get("pod") == pod_name and labels.get("role") != "pause":
                apps.append({
                    "cid": c.id,
                    # pid/snapshot_key optional; delete_container() handles missing keys safely
                })

        return pod, apps

    @log_to_file(logger)
    def _tasks_with_prefix(self, namespace: str, prefix: str) -> list[str]:
        """
        Return task IDs starting with <prefix> (e.g., pauseCID-).
        Works even if container objects are gone.
        """
        client = self.runtime._client_for_ns(namespace)
        ids = []
        try:
            from generated.api.services.tasks.v1 import tasks_pb2
            for t in _iter_list_tasks(client, tasks_pb2):
                tid = _task_id(t)
                if isinstance(tid, str) and tid.startswith(prefix):
                    ids.append(tid)
        except Exception:
            pass
        return ids

    @log_to_file(logger)
    def _guess_apps_by_prefix(self, namespace: str, pause_cid: str) -> list[str]:
        """
        First try by container labels; if none found, fall back to task prefix scan.
        """
        client = self.runtime._client_for_ns(namespace)
        app_ids = []
        try:
            clist = client.containers.List(containers_pb2.ListContainersRequest()).containers
            for c in clist:
                labels = dict(getattr(c, "labels", {}) or {})
                if labels.get("pod") == pause_cid or (labels.get("pod") and pause_cid in labels.get("pod", "")):
                    # your previous heuristic; keep if you need it
                    pass
                # common pattern: <pauseCID>-<appname>
                if getattr(c, "id", "").startswith(pause_cid + "-"):
                    app_ids.append(c.id)
        except Exception:
            pass

        if not app_ids:
            app_ids = self._tasks_with_prefix(namespace, pause_cid + "-")
        return app_ids

    @log_to_file(logger)
    def _cni_del_best_effort(self, pause_cid: str, pause_pid: int | None,
                             network_name: str, ifname: str):
        netns_path = f"/proc/{pause_pid}/ns/net" if pause_pid and os.path.exists(f"/proc/{pause_pid}/ns/net") else ""
        try:
            logger.debug(
                f"[cleanup] CNI DEL network={network_name} ifname={ifname} netns={'present' if netns_path else 'missing'}")
            self.cni.delete(network_name=network_name, container_id=pause_cid,
                            netns_path=netns_path, ifname=ifname)
        except Exception as e:
            logger.warning(f"[cleanup] CNI DEL warning: {e}", exc_info=True)


    @log_to_file(logger)
    def _remove_active_snapshots_matching(self, namespace: str, candidates: list[str]):
        """
        Try to remove any active snapshot keys that look like they belong to these containers.
        We remove any key that starts with one of the candidate prefixes.
        """
        snap = self._snapshotter_name()
        # enumerate keys if possible
        keys = self.runtime._list_active_snapshot_keys(snap)
        if not keys:
            # Blind attempts using common patterns
            for cid in candidates:
                for prefix in (f"{cid}-", f"{cid}"):
                    try:
                        self.snaps._snap_remove_active(snap, prefix)  # if exact match
                    except Exception:
                        pass
            return

        for key in keys:
            for cid in candidates:
                if key.startswith(cid):
                    try:
                        self.snaps._snap_remove_active(snap, key)
                        logger.debug(f"[cleanup] removed snapshot key: {key}")
                    except Exception as e:
                        logger.warning(f"[cleanup] snapshot remove warning ({key}): {e}", exc_info=True)



    @log_to_file(logger)
    def create_pod(self, name: str, pause_image: str = "registry.k8s.io/pause:3.9",
                   resources: Optional[ResourceSpec] = None,
                   cni_network: str = DEFAULT_CNI_NET_NAME,
                   cni_ifname: str = DEFAULT_IFNAME) -> Dict:
        logger.info(f"Using platform: {PLATFORM_OS}/{PLATFORM_ARCH}")
        self._ensure_unpacked(pause_image)

        chain_id = self.images.chain_id_for_image(pause_image)
        mounts, snap_key = self.snaps.prepare_rw_snapshot(chain_id, f"{name}-pause-rootfs",labels={"pod": name, "role": "pause"})

        mdesc = self.images.resolve_manifest(pause_image)
        _, cfg = self.images.load_manifest_and_config(mdesc)
        args_cfg = list((cfg.get("config") or {}).get("Entrypoint") or [])
        args_cfg += list((cfg.get("config") or {}).get("Cmd") or [])
        args = args_cfg or ["/pause"]
        logger.debug(f"[pause] args={args}")

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
        cid = f"{name}"
        self.runtime.create_container(cid, pause_image, spec_any, labels={"pod": name, "role": "pause"})
        pid = self.runtime.start_task(
            cid, mounts,
            namespace=self.c.namespace,
            pod=name,
            container_name="pause"
        )

        ns_base = f"/proc/{pid}/ns"
        ns_paths = {k: f"{ns_base}/{k}" for k in ["pid", "net", "ipc", "uts"]}
        logger.info(f"Pause pod up: cid={cid}, pid={pid}")
        cni_result = {}
        # Attach Calico via CNI (prefers cnitool, falls back to direct first-plugin exec)
        try:
            cni_result = self.cni.add(network_name=cni_network, container_id=cid,
                                      netns_path=ns_paths["net"], ifname=cni_ifname)
            logger.info(f"CNI attached: {cni_result if isinstance(cni_result, dict) else 'ok'}")
        except Exception as e:
            logger.error(f"CNI attach failed: {e}", exc_info=True)
            self.runtime.stop_and_delete_task(cid)
            try:
                self.snaps._snap_remove_active(self._snapshotter_name(), snap_key)
            except Exception:
                pass
            return {"ok": False, "error": f"CNI attach failed: {e}"}

        return {"name": name, "pause": {"cid": cid, "pid": pid}, "ns": ns_paths,
                "cni": {"network": cni_network, "ifname": cni_ifname},
                "snapshot_key": snap_key, "cni_result": cni_result }

    @log_to_file(logger)
    def add_container(self, pod: Dict, name: str,
                      image: str,
                      args: Optional[List[str]] = None,
                      env: Optional[Dict[str, str]] = None,
                      resources: Optional[ResourceSpec] = None,
                      volume_mounts: Optional[List[Dict[str, Any]]] = None) -> Dict:

        pod_name = pod["name"]
        pod_ns = pod["ns"]

        self._ensure_unpacked(image)

        chain_id = self.images.chain_id_for_image(image)
        mounts, snap_key = self.snaps.prepare_rw_snapshot(chain_id, f"{pod_name}-{name}-rootfs",labels={"pod": pod_name, "app": name})

        # Handle empty args - normalize empty list to None
        # Empty args [] would fail at runc level with "args must not be empty"
        # This ensures we use image Entrypoint/Cmd as fallback
        if args is None or (isinstance(args, list) and len(args) == 0):
            logger.debug(f"Container {name}: args is None or empty, extracting Entrypoint/Cmd from image {image}")
            mdesc = self.images.resolve_manifest(image)
            _, cfg = self.images.load_manifest_and_config(mdesc)
            args = list((cfg.get("config") or {}).get("Entrypoint") or [])
            args += list((cfg.get("config") or {}).get("Cmd") or [])
            if not args:
                # Fallback: use a safe default command if image has no Entrypoint/Cmd
                args = ["/bin/sh", "-c", "trap : TERM INT; sleep infinity & wait"]
                logger.warning(f"Container {name}: image {image} has no Entrypoint/Cmd, using fallback: {args}")
            else:
                logger.debug(f"Container {name}: extracted args from image: {args}")
        
        # Final validation: ensure args is not empty before creating container
        if not args or (isinstance(args, list) and len(args) == 0):
            raise ValueError(f"Container {name} cannot be created: args must not be empty. Image: {image}")

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
            resources=resources,
            volume_mounts=volume_mounts
        )
        cid = f"{pod_name}-{name}"
        self.runtime.create_container(cid, image, spec_any, labels={"pod": pod_name, "app": name,"role": "app"})
        pid = self.runtime.start_task(
            cid, mounts,
            namespace=self.c.namespace,
            pod=pod_name,
            container_name=name
        )
        logger.info(f"App started: cid={cid}, pid={pid}, image={image}, mounts={len(volume_mounts) if volume_mounts else 0}")
        
        return {"cid": cid, "pid": pid, "snapshot_key": snap_key}

    @log_to_file(logger)
    def add_containers(self, pod: Dict, specs: List[ContainerSpec]) -> Dict[str, Dict]:
        """
        Launch multiple containers (apps/sidecars) into the same pod namespaces.
        Returns a dict: { <name>: {"cid":..., "pid":..., "snapshot_key":...}, ... }
        """
        results: Dict[str, Dict] = {}
        for spec in specs:
            res = self.add_container(
                pod=pod,
                name=spec.name,
                image=spec.image,
                args=spec.args,
                env=spec.env,
                resources=spec.resources,
                volume_mounts=spec.mounts
            )
            results[spec.name] = res
        return results

    @log_to_file(logger)
    def _snapshotter_name(self) -> str:
        return self.snaps.snapshotter()

    @log_to_file(logger)
    def delete_container(self, app: Dict) -> None:
        """
        Delete an app container:
          - stop logging
          - stop & delete task
          - delete container object
          - remove active snapshot key
        Expected app dict shape: {"cid": ..., "pid": ..., "snapshot_key": ...}
        """
        cid = app.get("cid")
        snap_key = app.get("snapshot_key")
        
        # Stop logging for this container
        if self.log_manager and cid:
            try:
                self.log_manager.stop_logging(cid)
            except Exception as e:
                logger.warning(f"Failed to stop logging for container {cid}: {e}")
        
        if not cid:
            logger.warning("[cleanup] app has no 'cid'; skipping task/container delete")
        else:
            logger.debug(f"[cleanup] stopping app container: {cid}")
            self.runtime.stop_and_delete_task(cid)

        if snap_key:
            try:
                self.snaps._snap_remove_active(self._snapshotter_name(), snap_key)
                logger.debug(f"[cleanup] removed snapshot key: {snap_key}")
            except Exception as e:
                logger.warning(f"[cleanup] snapshot remove warning ({snap_key}): {e}", exc_info=True)

        # --- in PodManager.delete_container(...) (end of method), after stop/delete:
        # we already remove the explicit 'snap_key' if present; now also sweep any other matching actives
        try:
            _ = self.snaps.remove_active_by_label(
                {"pod": app.get("cid", "").split("-")[0], "app": app.get("cid", "").split("-", 1)[-1]})
        except Exception:
            pass


    @log_to_file(logger)
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
                logger.debug(f"[cleanup] CNI DEL network={network_name}, ifname={ifname}, netns={'present' if netns_for_del else 'missing'}")
                self.cni.delete(network_name=network_name, container_id=pause_cid, netns_path=netns_for_del, ifname=ifname)
                logger.debug("[cleanup] CNI released")
            except Exception as e:
                logger.warning(f"[cleanup] CNI DEL warning: {e}", exc_info=True)
        else:
            logger.debug("[cleanup] skip CNI DEL (missing pause cid or network name)")

        # 3) Stop logging for pause container
        if self.log_manager and pause_cid:
            try:
                self.log_manager.stop_logging(pause_cid)
            except Exception as e:
                logger.warning(f"Failed to stop logging for pause container {pause_cid}: {e}")
        
        # 4) Stop & delete the pause task/container
        if pause_cid:
            logger.debug(f"[cleanup] stopping pause container: {pause_cid}")
            self.runtime.stop_and_delete_task(pause_cid)

        # 5) Remove the pause snapshot key (stored as pod['snapshot_key'])
        snap_key = pod.get("snapshot_key")
        if snap_key:
            try:
                self.snaps._snap_remove_active(self._snapshotter_name(), snap_key)
                logger.debug(f"[cleanup] removed pause snapshot key: {snap_key}")
            except Exception as e:
                logger.warning(f"[cleanup] pause snapshot remove warning ({snap_key}): {e}", exc_info=True)

        # 6) Sweep any remaining active snapshots for this pod (even if we lost individual keys)
        try:
            res = self.snaps.remove_active_by_label({"pod": pod["name"]})
            if res["removed"]:
                logger.debug(f"[cleanup] removed active snapshots by label pod={pod['name']}: {res['removed']}")
            if res["kept"]:
                logger.debug(f"[cleanup] could not remove some snapshots (likely parents/pinned): {res['kept']}")
        except Exception as e:
            logger.warning(f"[cleanup] snapshot label sweep warning: {e}", exc_info=True)
    
    @log_to_file(logger)
    def read_container_logs(self, namespace: str, pod_name: str, pod_uid: str,
                           container_name: str, instance: int = 0,
                           tail_lines: Optional[int] = None,
                           follow: bool = False,
                           since: Optional[datetime] = None,
                           limit_bytes: Optional[int] = None) -> Dict[str, Any]:
        """
        Read container logs (similar to kubectl logs).
        
        Args:
            namespace: Kubernetes namespace
            pod_name: Pod name
            pod_uid: Pod UID
            container_name: Container name
            instance: Container instance number (default: 0)
            tail_lines: Number of lines to tail (like --tail)
            follow: If True, follow logs (like --follow) - Note: requires separate streaming implementation
            since: Only return logs after this timestamp (like --since-time)
            limit_bytes: Maximum bytes to return (like --limit-bytes)
            
        Returns:
            Dict with 'logs' (list of log entries) and 'metadata'
        """
        return self.log_manager.read_logs(
            namespace=namespace,
            pod_name=pod_name,
            pod_uid=pod_uid,
            container_name=container_name,
            instance=instance,
            tail_lines=tail_lines,
            follow=follow,
            since=since,
            limit_bytes=limit_bytes
        )



    @log_to_file(logger)
    def terminate_pod_by_cid(self, namespace: str, pause_cid: str,
                             cni_network: str = DEFAULT_CNI_NET_NAME,
                             ifname: str = DEFAULT_IFNAME) -> dict:
        """
        Hard-destroy a pod when you know the pause CID.
        - CNI DEL best-effort (even if netns is gone)
        - Stop/Delete app tasks (by labels or prefix fallback)
        - Stop/Delete pause task/container
        - Remove active snapshot keys that match the IDs
        """
        client = self.runtime._client_for_ns(namespace)

        # read pause pid from pidfile if task is gone
        pause_pid = _read_pidfile(namespace, pause_cid)

        # 1) CNI DEL (best-effort)
        try:
            netns = f"/proc/{pause_pid}/ns/net" if (pause_pid and os.path.exists(f"/proc/{pause_pid}")) else ""
            logger.debug(f"[cleanup] CNI DEL network={cni_network} ifname={ifname} netns={'present' if netns else 'missing'}")
            self.cni.delete(network_name=cni_network, container_id=pause_cid, netns_path=netns, ifname=ifname)
        except Exception as e:
            logger.warning(f"[cleanup] CNI DEL warning: {e}", exc_info=True)

        # 2) Delete app containers/tasks first
        app_ids = self._guess_apps_by_prefix(namespace, pause_cid)
        apps_terminated = []
        for aid in app_ids:
            # Try full path: stop+delete (task + container), then fall back to task-only
            try:
                self.runtime.stop_and_delete_task(aid)
            except Exception:
                pass
            # Always try task-only delete too (covers “task without container”)
            self.runtime.delete_task_only(aid)
            # Best-effort container object delete
            try:
                self.c.containers.Delete(containers_pb2.DeleteContainerRequest(id=aid))
            except grpc.RpcError:
                pass
            apps_terminated.append(aid)

        # 3) Stop & delete pause task/container
        try:
            self.runtime.stop_and_delete_task(pause_cid)
        finally:
            # task-only delete in case the first call hit a proto mismatch
            self.runtime.delete_task_only(pause_cid)
            try:
                self.c.containers.Delete(containers_pb2.DeleteContainerRequest(id=pause_cid))
            except grpc.RpcError:
                pass

        # 4) Remove active snapshot keys that look like these CIDs
        try:
            snap = self._snapshotter_name()
            # try both exact keys and common suffixes
            for k in (pause_cid, pause_cid + "-", *(aid for aid in apps_terminated),
                      *(aid + "-" for aid in apps_terminated)):
                try:
                    self.snaps._snap_remove_active(snap, k)
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "ok": True,
            "message": f"terminated (by cid) in ns='{namespace}'",
            "pause": {"cid": pause_cid, "pid": pause_pid},
            "apps_terminated": apps_terminated,
        }

    @log_to_file(logger)
    def terminate_pod(self,namespace: str, pod_name: str,
                      cni_network: str = DEFAULT_CNI_NET_NAME,
                      ifname: str = DEFAULT_IFNAME) -> dict:
        """
        Gracefully terminate a pod (pause + its apps) in the given containerd namespace.
        """
        client = ContainerdClient(socket=self.c.socket, namespace=namespace)
        pm = PodManager(client)

        pod, apps = self._build_runtime_pod_struct(namespace, pod_name, cni_network, ifname)

        if not pod:
            return {"ok": False, "message": f"pod '{pod_name}' not found in ns='{namespace}'"}

        try:
            pm.delete_pod(pod, apps=apps)
            return {
                "ok": True,
                "message": f"terminated pod '{pod_name}' in ns='{namespace}'",
                "apps_terminated": [a["cid"] for a in apps],
                "pause": pod.get("pause", {}),
            }
        except Exception as e:
            return {"ok": False, "message": f"delete_pod failed: {e}"}

    @log_to_file(logger)
    def terminate_pods_in_namespace(self,namespace: str) -> dict:
        """
        Convenience: terminate ALL labeled pods in a namespace.
        """
        client = ContainerdClient(namespace=namespace)
        pm = PodManager(client)
        summaries = pm.runtime.list_pods_and_apps_in_namespace(namespace)

        results = []
        for s in summaries:
            # We only terminate "real" pods that came from pause/app labeling
            # (skip STANDALONE pseudo-pods like calico-node)
            is_real_pod = s["pause"]["status"] != "STANDALONE" and "pod_id" in s
            if not is_real_pod:
                continue
            pod_name = s["pod_id"]
            res = self.terminate_pod(namespace, pod_name)
            results.append(res)

        return {"namespace": namespace, "results": results}

    # Add these near the bottom of PodManager
    @log_to_file(logger)
    def destroy_pod(self, namespace: str, pod_name: str) -> dict:
        """
        Alias for terminate_pod(); kept for readability with 'destroy' wording.
        """
        return self.terminate_pod(namespace, pod_name)

    @log_to_file(logger)
    def destroy_all_pods(self, namespace: str) -> dict:
        """
        Alias for terminate_pods_in_namespace(); destroys all labeled pods.
        """
        return self.terminate_pods_in_namespace(namespace)

    @log_to_file(logger)
    def destroy_container_by_id(self, namespace: str, cid: str) -> dict:
        """
        Stop/delete a single container by id in a namespace.
        Namespace-safe (does NOT mutate self.runtime.c).
        """
        client = self.runtime._client_for_ns(namespace)
        try:
            self.runtime.stop_and_delete_task_in_client(client, cid)
            return {"ok": True, "message": f"container {cid} removed from ns='{namespace}'"}
        except Exception as e:
            return {"ok": False, "message": f"failed to remove {cid}: {e}"}

    @log_to_file(logger)
    def purge_stopped_tasks_and_containers(self, namespace: str) -> dict:
        """
        Remove any lingering STOPPED tasks and their containers in a namespace.
        """
        client = self.runtime._client_for_ns(namespace)
        purged = []
        try:
            from generated.api.services.tasks.v1 import tasks_pb2
            stopped = []
            for t in _iter_list_tasks(client, tasks_pb2):
                tid = _task_id(t)
                status = _task_status(t)
                if tid and status and str(status).upper() == "STOPPED":
                    stopped.append(tid)
            for tid in stopped:
                self.runtime.delete_task_only(tid)
                try:
                    client.containers.Delete(containers_pb2.DeleteContainerRequest(id=tid))
                except grpc.RpcError:
                    pass
                purged.append(tid)
        except Exception as e:
            return {"ok": False, "error": f"purge failed: {e}", "purged": purged}
        return {"ok": True, "purged": purged}


# -------------------- Demo / Example --------------------
# if __name__ == "__main__":
#     client = ContainerdClient()
#     pods = PodManager(client)
#
#     # Create a pod with CPU/memory for the pause sandbox + CNI attach
#     pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
#     pod = pods.create_pod(
#         "pause",
#         pause_image="registry.k8s.io/pause:3.9",
#         resources=pause_resources,
#         cni_network=os.environ.get("CNI_NET_NAME", DEFAULT_CNI_NET_NAME),
#         cni_ifname=os.environ.get("CNI_IFNAME", DEFAULT_IFNAME),
#     )
#
#     # Add nginx with CPU/memory and a CPU set
#     app_resources = ResourceSpec(cpu_millicores=500, memory="256Mi", cpuset_cpus="0-1")
#     app = pods.add_container(
#         pod, name="nginx",
#         image="docker.io/library/nginx:latest",
#         args=[
#         "/bin/sh", "-c",
#         (
#             "rm -f /etc/nginx/conf.d/default.conf && "
#             "printf 'server { listen 8080; location / { root /usr/share/nginx/html; index index.html; } }' "
#             "> /etc/nginx/conf.d/custom.conf && "
#             "exec nginx -g 'daemon off;'"
#         )
#     ],
#         resources=app_resources
#     )
#
#     print("\nSummary:")
#     print(json.dumps({
#         "pause": pod["pause"],
#         "nginx": app
#     }, indent=2))

# if __name__ == "__main__":
#     client = ContainerdClient()
#     pods = PodManager(client)
#
#     # 1) Create the pod (pause sandbox + CNI)
#     pause_resources = ResourceSpec(cpu_millicores=100, memory="64Mi")
#     pod = pods.create_pod(
#         "demo-pod",
#         pause_image="registry.k8s.io/pause:3.9",
#         resources=pause_resources,
#         cni_network=os.environ.get("CNI_NET_NAME", DEFAULT_CNI_NET_NAME),
#         cni_ifname=os.environ.get("CNI_IFNAME", DEFAULT_IFNAME),
#     )
#
#     # 2) Define containers: one main app + two sidecars
#     main_resources = ResourceSpec(cpu_millicores=500, memory="256Mi", cpuset_cpus="0-1")
#     sidecar_small = ResourceSpec(cpu_millicores=100, memory="64Mi")
#
#     containers: List[ContainerSpec] = [
#         # Main app (nginx) — listening on 8080 rather than 80
#         ContainerSpec(
#             name="nginx",
#             image="docker.io/library/nginx:latest",
#             args=[
#                 "/bin/sh", "-c",
#                 (
#                     "rm -f /etc/nginx/conf.d/default.conf && "
#                     "printf 'server { listen 8080; location / { root /usr/share/nginx/html; index index.html; } }' "
#                     "> /etc/nginx/conf.d/custom.conf && "
#                     "exec nginx -g \"daemon off;\""
#                 )
#             ],
#             resources=main_resources
#         ),
#
#         # Sidecar: simple log tailer (follows nginx access logs)
#         ContainerSpec(
#             name="log-tailer",
#             image="docker.io/library/busybox:latest",
#             args=["/bin/sh", "-c", "mkdir -p /var/log/nginx; touch /var/log/nginx/access.log; tail -F /var/log/nginx/access.log"],
#             resources=sidecar_small,
#             # If you want to share the log path from nginx rootfs you’d need a shared volume/mount;
#             # for a pure demo we just tail an empty file.
#         ),
#
#         # Sidecar: tiny “metrics” loop
#         ContainerSpec(
#             name="metrics",
#             image="docker.io/library/alpine:latest",
#             args=["/bin/sh", "-c", "while true; do echo metrics_ok $(date +%s); sleep 5; done"],
#             env={"METRICS_PORT": "9090"},
#             resources=sidecar_small
#         ),
#     ]
#
#     # 3) Launch them
#     apps = pods.add_containers(pod, containers)
#
#     print("\nSummary:")
#     print(json.dumps({
#         "pause": pod["pause"],
#         "apps": apps
#     }, indent=2))
#
#     # Example: cleanup all (uncomment if you want auto-clean)
#     # pods.delete_pod(pod, apps=list(apps.values()))

if __name__ == "__main__":
    #client = ContainerdClient()
    #pod_mgr = PodManager(client)

    client = ContainerdClient(namespace="testKCR1")
    pm = PodManager(client)
    print(pm.pull_image("docker.io/library/alpine:latest"))
    print(pm.images.resolve_image_name("docker.io/library/alpine:latest"))
    #check_status = pod_mgr.runtime.prune_namespace("testKCR")