"""
StorageClass definitions for Dibba.

StorageClasses define how volumes are provisioned and managed.
"""
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class StorageClassType(Enum):
    """Storage class types."""
    HOST_PATH = "hostPath"
    AWS_EBS = "aws-ebs"
    AWS_EFS = "aws-efs"
    LOCAL = "local"
    EMPTY_DIR = "emptyDir"


@dataclass
class StorageClass:
    """StorageClass definition.
    
    Similar to Kubernetes StorageClass, defines how volumes are provisioned.
    """
    name: str
    type: StorageClassType
    parameters: Dict[str, Any] = field(default_factory=dict)
    reclaim_policy: str = "Retain"  # Retain, Delete
    volume_binding_mode: str = "Immediate"  # Immediate, WaitForFirstConsumer
    allow_volume_expansion: bool = False
    
    @log_to_file(logger)
    def __post_init__(self):
        """Validate storage class configuration."""
        if self.reclaim_policy not in ["Retain", "Delete"]:
            raise ValueError(f"Invalid reclaim_policy: {self.reclaim_policy}. Must be 'Retain' or 'Delete'")
        
        if self.volume_binding_mode not in ["Immediate", "WaitForFirstConsumer"]:
            raise ValueError(f"Invalid volume_binding_mode: {self.volume_binding_mode}")
    
    @classmethod
    @log_to_file(logger)
    def from_dict(cls, data: Dict[str, Any]) -> 'StorageClass':
        """Create StorageClass from dictionary."""
        return cls(
            name=data.get('name', ''),
            type=StorageClassType(data.get('type', 'hostPath')),
            parameters=data.get('parameters', {}),
            reclaim_policy=data.get('reclaimPolicy', 'Retain'),
            volume_binding_mode=data.get('volumeBindingMode', 'Immediate'),
            allow_volume_expansion=data.get('allowVolumeExpansion', False)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert StorageClass to dictionary."""
        return {
            'name': self.name,
            'type': self.type.value,
            'parameters': self.parameters,
            'reclaimPolicy': self.reclaim_policy,
            'volumeBindingMode': self.volume_binding_mode,
            'allowVolumeExpansion': self.allow_volume_expansion
        }


# Default storage classes
DEFAULT_STORAGE_CLASSES = {
    'hostpath': StorageClass(
        name='hostpath',
        type=StorageClassType.HOST_PATH,
        parameters={'path': '/var/lib/dibba/volumes'},
        reclaim_policy='Retain',
        volume_binding_mode='Immediate'
    ),
    'aws-ebs': StorageClass(
        name='aws-ebs',
        type=StorageClassType.AWS_EBS,
        parameters={
            'type': 'gp3',  # gp3, gp2, io1, io2
            'fsType': 'ext4',
            'encrypted': False
        },
        reclaim_policy='Delete',
        volume_binding_mode='WaitForFirstConsumer',
        allow_volume_expansion=True
    ),
    'aws-efs': StorageClass(
        name='aws-efs',
        type=StorageClassType.AWS_EFS,
        parameters={
            'provisioningMode': 'efs-ap',  # efs-ap, efs-ia
            'fileSystemId': None,  # Optional: use existing EFS
        },
        reclaim_policy='Retain',
        volume_binding_mode='Immediate'
    ),
    'local': StorageClass(
        name='local',
        type=StorageClassType.LOCAL,
        parameters={'path': '/var/lib/dibba/local-volumes'},
        reclaim_policy='Retain',
        volume_binding_mode='WaitForFirstConsumer'
    ),
}

