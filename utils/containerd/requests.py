# utils/containerd/requests.py
from typing import List, Optional
from pydantic import BaseModel, ConfigDict
from .schemas import ContainerSpec

class CreatePodsRequest(BaseModel):
    host_name: str
    namespace: Optional[str] = None
    containers: List[ContainerSpec]

    model_config = ConfigDict(extra='allow')  # so extra kwargs go to extra_kwargs
