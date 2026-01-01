"""
Volume and PVC definitions for Dibba.
"""
from enum import Enum
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class VolumeStatus(Enum):
    """Volume status."""
    PENDING = "Pending"
    AVAILABLE = "Available"
    BOUND = "Bound"
    RELEASED = "Released"
    FAILED = "Failed"
    TERMINATING = "Terminating"


class VolumeAccessMode(Enum):
    """Volume access modes."""
    READ_WRITE_ONCE = "ReadWriteOnce"  # RWO - can be mounted as read-write by a single node
    READ_ONLY_MANY = "ReadOnlyMany"    # ROX - can be mounted read-only by many nodes
    READ_WRITE_MANY = "ReadWriteMany"  # RWX - can be mounted as read-write by many nodes


@dataclass
class PersistentVolume:
    """PersistentVolume (PV) definition.
    
    Represents a storage resource in the cluster.
    """
    name: str
    storage_class: str
    capacity: str  # e.g., "10Gi", "100Mi"
    access_modes: List[VolumeAccessMode] = field(default_factory=lambda: [VolumeAccessMode.READ_WRITE_ONCE])
    status: VolumeStatus = VolumeStatus.PENDING
    claim_ref: Optional[str] = None  # Name of bound PVC
    host_path: Optional[str] = None  # For hostPath volumes
    aws_volume_id: Optional[str] = None  # For EBS volumes
    aws_efs_id: Optional[str] = None  # For EFS volumes
    mount_path: Optional[str] = None  # Where volume is mounted on host
    node_name: Optional[str] = None  # Node where volume is attached
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    @log_to_file(logger)
    def from_dict(cls, data: Dict[str, Any]) -> 'PersistentVolume':
        """Create PersistentVolume from dictionary."""
        access_modes = [
            VolumeAccessMode(am) if isinstance(am, str) else am
            for am in data.get('access_modes', [VolumeAccessMode.READ_WRITE_ONCE])
        ]
        return cls(
            name=data.get('name', ''),
            storage_class=data.get('storage_class', 'hostpath'),
            capacity=data.get('capacity', '1Gi'),
            access_modes=access_modes,
            status=VolumeStatus(data.get('status', VolumeStatus.PENDING.value)),
            claim_ref=data.get('claim_ref'),
            host_path=data.get('host_path'),
            aws_volume_id=data.get('aws_volume_id'),
            aws_efs_id=data.get('aws_efs_id'),
            mount_path=data.get('mount_path'),
            node_name=data.get('node_name'),
            created_at=data.get('created_at', datetime.now(timezone.utc).isoformat()),
            updated_at=data.get('updated_at', datetime.now(timezone.utc).isoformat()),
            metadata=data.get('metadata', {})
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert PersistentVolume to dictionary."""
        return {
            'name': self.name,
            'storage_class': self.storage_class,
            'capacity': self.capacity,
            'access_modes': [am.value for am in self.access_modes],
            'status': self.status.value,
            'claim_ref': self.claim_ref,
            'host_path': self.host_path,
            'aws_volume_id': self.aws_volume_id,
            'aws_efs_id': self.aws_efs_id,
            'mount_path': self.mount_path,
            'node_name': self.node_name,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'metadata': self.metadata
        }
    
    @log_to_file(logger)
    def update_status(self, status: VolumeStatus, claim_ref: Optional[str] = None):
        """Update volume status."""
        self.status = status
        if claim_ref is not None:
            self.claim_ref = claim_ref
        self.updated_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"Volume {self.name} status updated to {status.value}")


@dataclass
class PersistentVolumeClaim:
    """PersistentVolumeClaim (PVC) definition.
    
    Represents a request for storage by a user.
    """
    name: str
    namespace: str
    storage_class: Optional[str] = None  # If None, uses default
    access_modes: List[VolumeAccessMode] = field(default_factory=lambda: [VolumeAccessMode.READ_WRITE_ONCE])
    resources: Dict[str, Any] = field(default_factory=lambda: {'requests': {'storage': '1Gi'}})
    volume_name: Optional[str] = None  # Name of bound PV
    status: VolumeStatus = VolumeStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def requested_storage(self) -> str:
        """Get requested storage size."""
        return self.resources.get('requests', {}).get('storage', '1Gi')
    
    @classmethod
    @log_to_file(logger)
    def from_dict(cls, data: Dict[str, Any]) -> 'PersistentVolumeClaim':
        """Create PersistentVolumeClaim from dictionary."""
        access_modes = [
            VolumeAccessMode(am) if isinstance(am, str) else am
            for am in data.get('access_modes', [VolumeAccessMode.READ_WRITE_ONCE])
        ]
        return cls(
            name=data.get('name', ''),
            namespace=data.get('namespace', 'default'),
            storage_class=data.get('storage_class'),
            access_modes=access_modes,
            resources=data.get('resources', {'requests': {'storage': '1Gi'}}),
            volume_name=data.get('volume_name'),
            status=VolumeStatus(data.get('status', VolumeStatus.PENDING.value)),
            created_at=data.get('created_at', datetime.now(timezone.utc).isoformat()),
            updated_at=data.get('updated_at', datetime.now(timezone.utc).isoformat()),
            metadata=data.get('metadata', {})
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert PersistentVolumeClaim to dictionary."""
        return {
            'name': self.name,
            'namespace': self.namespace,
            'storage_class': self.storage_class,
            'access_modes': [am.value for am in self.access_modes],
            'resources': self.resources,
            'volume_name': self.volume_name,
            'status': self.status.value,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'metadata': self.metadata
        }
    
    @log_to_file(logger)
    def bind_to_volume(self, volume_name: str):
        """Bind PVC to a PV."""
        self.volume_name = volume_name
        self.status = VolumeStatus.BOUND
        self.updated_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"PVC {self.name} bound to PV {volume_name}")

