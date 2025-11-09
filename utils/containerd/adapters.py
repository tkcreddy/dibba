# utils/containerd/adapters.py
import re

def parse_mem_bytes(s: str) -> int:
    # supports e.g. "64Mi", "256M", "1Gi", "512MB"
    m = re.fullmatch(r"(?i)(\d+)\s*([kmg]i?|mb|gb|kb)?", s.strip())
    if not m:
        raise ValueError(f"Bad memory string: {s}")
    val = int(m.group(1))
    unit = (m.group(2) or "").lower()
    mul = 1
    if unit in ("k", "kb"): mul = 1000
    elif unit in ("ki",):   mul = 1024
    elif unit in ("m", "mb"):  mul = 1000**2
    elif unit in ("mi",):      mul = 1024**2
    elif unit in ("g", "gb"):  mul = 1000**3
    elif unit in ("gi",):      mul = 1024**3
    return val * mul

def linux_resources_from_spec(cpu_millicores: int, memory: str, cpuset_cpus: str|None):
    """
    Minimal OCI LinuxResources mapping.
    - CPU: use 100000 period; quota = 100 * millicores (i.e., 500m -> 50000)
    - Memory: parse to bytes
    """
    cpu_quota = 100 * int(cpu_millicores)          # 100000 * (m/1000)
    cpu_period = 100_000
    mem_limit = parse_mem_bytes(memory)

    lr = {
        "cpu": {
            "shares": None,                         # optional
            "quota": cpu_quota,
            "period": cpu_period,
            "cpus": cpuset_cpus or None,           # cgroup v2 accepts string
        },
        "memory": {
            "limit": mem_limit,
            # "reservation": None,
            # "swap": None,
        },
    }
    return lr
