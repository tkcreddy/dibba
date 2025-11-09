# server/api_models.py  (Pydantic v2)
from pydantic import BaseModel, ConfigDict
from typing import List, Optional

class ResourceSpecModel(BaseModel):
    cpu_millicores: int
    memory: str
    cpuset_cpus: Optional[str] = None
    model_config = ConfigDict(extra='ignore')

class ContainerSpecModel(BaseModel):
    name: str
    image: str
    args: List[str] = []
    resources: ResourceSpecModel
    env: dict | None = None
    #mounts: list[dict] | None = None
    model_config = ConfigDict(extra='ignore')

class CreatePodsRequest(BaseModel):
    host_name: str
    namespace: str = "k8s.io"
    containers: List[ContainerSpecModel]
    model_config = ConfigDict(extra='ignore')
