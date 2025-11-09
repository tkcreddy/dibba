# utils/containerd/schemas.py
from typing import List, Optional
from pydantic import BaseModel, ConfigDict

class ResourceSpec(BaseModel):
    cpu_millicores: int
    memory: str
    cpuset_cpus: Optional[str] = None

    model_config = ConfigDict(extra='forbid')

class ContainerSpec(BaseModel):
    name: str
    image: str
    args: Optional[List[str]] = None
    env: Optional[dict] = None
    resources: Optional[ResourceSpec] = None

    model_config = ConfigDict(extra='allow')
