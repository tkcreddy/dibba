#!/usr/bin/env python3
import argparse, json, sys, re
from typing import Any, Dict, List, Optional
import yaml
import urllib.request

# --------------------------
# Helpers: CPU / Memory
# --------------------------
def parse_cpu_to_millicores(v: Any) -> int:
    """
    K8s cpu can be: "500m", "1", "0.5", 500m-style strings.
    Return integer millicores.
    """
    if v is None:
        return 0
    s = str(v).strip().lower()
    if s.endswith("m"):
        return int(float(s[:-1]))
    # plain numbers: cores (can be float)
    cores = float(s)
    return int(round(cores * 1000))

_MI_FACTORS = {
    "ki": 1/1024,  # Ki -> Mi
    "mi": 1,       # Mi -> Mi
    "gi": 1024,    # Gi -> Mi
    "ti": 1024*1024,
    "k": 1/1024,   # K  -> Mi (not standard, but seen)
    "m": 1/ (1024*1024),  # M bytes-ish; avoided, but safeguard
    "g": 1/ (1024),       # G bytes-ish; avoided, but safeguard
}

def parse_mem_to_mi_string(v: Any) -> str:
    """
    K8s memory examples: "256Mi", "1Gi", "512M", "1048576Ki", "268435456".
    Return "<Mi>Mi" rounded up at least to 1Mi if non-zero.
    """
    if v is None:
        return "0Mi"
    s = str(v).strip()
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)?\s*$", s)
    if not m:
        # bytes numeric fallback
        try:
            b = int(s)
            mi = max(0, int(round(b / (1024*1024))))
            return f"{mi}Mi"
        except:
            return "0Mi"
    qty = float(m.group(1))
    unit = (m.group(2) or "").lower()

    if unit in _MI_FACTORS:
        mi = qty * _MI_FACTORS[unit]
    elif unit == "" :
        # assume bytes
        mi = qty / (1024*1024)
    else:
        # Prefer power-of-two suffixes; for unknown units fallback as bytes
        mi = qty / (1024*1024)

    mi_int = int(round(mi))
    if qty > 0 and mi_int == 0:
        mi_int = 1
    return f"{mi_int}Mi"

# --------------------------
# K8s → Dibba ContainerSpec
# --------------------------
def k8s_containers_to_dibba(conts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in conts:
        name  = c.get("name")
        image = c.get("image")
        cmd   = c.get("command") or []
        args  = c.get("args") or []
        env   = c.get("env")     # optional: pass through as-is (list of {name,value})

        # resources -> ResourceSpec
        res   = c.get("resources", {})
        limits   = res.get("limits",   {})
        requests = res.get("requests", {})

        # prefer limits, fallback to requests, else None/0
        cpu_raw = limits.get("cpu", requests.get("cpu"))
        mem_raw = limits.get("memory", requests.get("memory"))

        cpu_millicores = parse_cpu_to_millicores(cpu_raw) if cpu_raw is not None else 0
        memory_mi_str  = parse_mem_to_mi_string(mem_raw)  if mem_raw is not None else "0Mi"

        # your schema earlier used args=[...]; combine command+args if both exist
        combined_args = []
        if cmd:  # K8s "command" is entrypoint
            combined_args.extend(cmd)
        if args:
            combined_args.extend(args)

        # minimal ContainerSpec dict your Celery task expects
        spec = {
            "name": name,
            "image": image,
            "args": combined_args if combined_args else None,
            "env": env,  # pass through (or map to dict if your schema requires)
            "resources": {
                "cpu_millicores": cpu_millicores,
                "memory": memory_mi_str,
                "cpuset_cpus": None,  # optional: can be set by policy
            },
            "mounts": None,  # TODO: map from volumeMounts if you need it
        }
        out.append(spec)
    return out

def extract_k8s_containers(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = (doc.get("kind") or "").lower()
    spec = doc.get("spec") or {}
    if kind == "pod":
        return spec.get("containers", []) or []
    if kind in ("deployment", "replicaset", "statefulset", "daemonset", "job", "cronjob"):
        tmpl = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") if kind == "cronjob" \
               else (spec.get("template") or {})
        return ((tmpl.get("spec") or {}).get("containers", [])) or []
    # fallback: try .spec.containers at root
    return spec.get("containers", []) or []

def extract_namespace(doc: Dict[str, Any]) -> Optional[str]:
    meta = doc.get("metadata") or {}
    return meta.get("namespace")

# --------------------------
# CLI / POST
# --------------------------
def main():
    p = argparse.ArgumentParser(description="Convert K8s manifest → Dibba main_api payload")
    p.add_argument("manifest", help="Path to Kubernetes YAML (Pod/Deployment/etc.)")
    p.add_argument("--host-name", required=True, help="Target host name used by your routing/queue")
    p.add_argument("--api-url", help="POST to FastAPI, e.g. http://127.0.0.1:8000/create_pods/")
    p.add_argument("--namespace", help="Override namespace (else use manifest metadata.namespace or 'k8s.io')")
    p.add_argument("--auth", help="Optional Authorization header value, e.g. 'Bearer <token>'")
    args = p.parse_args()

    with open(args.manifest, "r") as f:
        docs = list(yaml.safe_load_all(f))

    if not docs:
        print("No YAML documents found.", file=sys.stderr)
        sys.exit(1)

    # Take the first workload doc (you can extend to merge multiple)
    doc = docs[0]

    containers = extract_k8s_containers(doc)
    dibba_containers = k8s_containers_to_dibba(containers)

    ns = args.namespace or extract_namespace(doc) or "k8s.io"

    payload = {
        "namespace": ns,
        "containers": dibba_containers,
        "host_name": args.host_name,  # carried via extra_kwargs to your task
    }

    if not args.api_url:
        print(json.dumps(payload, indent=2))
        return

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(args.api_url, data=data, headers={"Content-Type": "application/json"})
    if args.auth:
        req.add_header("Authorization", args.auth)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(body)
    except Exception as e:
        print(f"POST failed: {e}", file=sys.stderr)
        print("Payload was:\n" + json.dumps(payload, indent=2), file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()


# python k8s_to_dibba.py ./nginx-deploy.yaml \
#   --host-name ip-172-31-17-19 \
#   --api-url http://127.0.0.1:8000/create_pods/ \
#   --auth "Bearer <YOUR_TOKEN>"
