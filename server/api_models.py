# utils/containerd/schemas.py
from typing import List, Dict, Optional
from pydantic import BaseModel, Field, ConfigDict

def _parse_mem_bytes(s: str) -> int:
    # supports "64Mi", "256M", "1Gi", "512m", plain bytes as str, or int
    if isinstance(s, int):
        return s
    s = s.strip().lower()
    units = {
        "k": 1000, "kb": 1000,
        "m": 1000**2, "mb": 1000**2,
        "g": 1000**3, "gb": 1000**3,
        "ki": 1024, "kib": 1024,
        "mi": 1024**2, "mib": 1024**2,
        "gi": 1024**3, "gib": 1024**3,
    }
    # numeric only
    try:
        return int(s)
    except ValueError:
        pass
    # split number+unit
    num = ""
    unit = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            unit += ch
    num = float(num or "0")
    mul = units.get(unit, 1)
    return int(num * mul)

class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_millicores: int = 100
    memory: str = "64Mi"
    cpuset_cpus: Optional[str] = None

    def to_linux_resources_dict(self) -> Dict:
        # containerd/OCI expects memory/cpu in LinuxResources shape
        # cpu shares (relative weight): 1024 ~= 1 CPU. Approx from millicores.
        cpu_shares = max(2, int(self.cpu_millicores * 1024 / 1000))
        # cpu quota/period: use CFS 100000 period
        cpu_period = 100000
        cpu_quota = int(self.cpu_millicores * cpu_period / 1000)
        mem_limit = _parse_mem_bytes(self.memory)

        d: Dict = {
            "cpu": {
                "shares": cpu_shares,
                "quota": cpu_quota,
                "period": cpu_period,
            },
            "memory": {
                "limit": mem_limit
            }
        }
        if self.cpuset_cpus:
            d["cpu"]["cpus"] = self.cpuset_cpus
        return d

class ContainerSpec(BaseModel):
    model_config = ConfigDict(extra="allow")  # ignore unknowns to be resilient
    name: str
    image: str
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    mounts: Optional[List[Dict[str, str]]] = None  # adjust to your mount model

class CreatePodsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # allow extra fields
    host_name: str
    namespace: str = "k8s.io"
    containers: List[ContainerSpec]
