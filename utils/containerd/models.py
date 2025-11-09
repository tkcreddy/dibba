# utils/containerd/models.py (or wherever you define ResourceSpec)
from dataclasses import dataclass
from typing import Optional

@dataclass
class ResourceSpec:
    cpu_millicores: int                    # e.g. 100 (0.1 CPU), 500 (0.5 CPU), 1000 (1 CPU)
    memory: str                            # e.g. "64Mi", "256Mi", "1Gi", or plain bytes string like "268435456"
    cpuset_cpus: Optional[str] = None      # e.g. "0-1" or "0,2"

    def to_linux_resources_dict(self) -> dict:
        """
        Convert to an OCI Runtime 'LinuxResources' dict shape.
        For cgroups v1: period = 100000 µs; quota = CPUcores * period
        millicores -> quota: (millicores/1000) * period
        """
        period = 100_000  # 100ms in microseconds (typical default)
        quota = int(self.cpu_millicores * period / 1000)

        mem_bytes = parse_bytes(self.memory)

        cpu = {"period": period, "quota": quota}
        if self.cpuset_cpus:
            cpu["cpuset_cpus"] = self.cpuset_cpus

        return {
            "cpu": cpu,
            "memory": {"limit": mem_bytes}
        }

def parse_bytes(s: str) -> int:
    """
    Very small parser for K8s-style quantities:
    64Mi, 256Mi, 1Gi, 512Ki, or raw integer string.
    """
    ss = s.strip().lower()
    if ss.endswith("ki"):
        return int(float(ss[:-2]) * 1024)
    if ss.endswith("mi"):
        return int(float(ss[:-2]) * 1024**2)
    if ss.endswith("gi"):
        return int(float(ss[:-2]) * 1024**3)
    # plain number (assume bytes)
    return int(float(ss))
