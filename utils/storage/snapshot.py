"""
Volume snapshot support for Dibba.
"""
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class SnapshotStatus(Enum):
    """Snapshot status."""
    PENDING = "Pending"
    READY = "Ready"
    ERROR = "Error"
    DELETING = "Deleting"


@dataclass
class VolumeSnapshot:
    """Volume snapshot definition."""
    name: str
    namespace: str
    pvc_name: str  # Source PVC
    pv_name: Optional[str] = None  # Source PV
    storage_class: Optional[str] = None
    status: SnapshotStatus = SnapshotStatus.PENDING
    size: Optional[str] = None  # Snapshot size
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Provider-specific fields
    aws_snapshot_id: Optional[str] = None  # For EBS snapshots
    snapshot_path: Optional[str] = None  # For hostPath snapshots
    
    @classmethod
    @log_to_file(logger)
    def from_dict(cls, data: Dict[str, Any]) -> 'VolumeSnapshot':
        """Create VolumeSnapshot from dictionary."""
        return cls(
            name=data.get('name', ''),
            namespace=data.get('namespace', 'default'),
            pvc_name=data.get('pvc_name', ''),
            pv_name=data.get('pv_name'),
            storage_class=data.get('storage_class'),
            status=SnapshotStatus(data.get('status', SnapshotStatus.PENDING.value)),
            size=data.get('size'),
            created_at=data.get('created_at', datetime.now(timezone.utc).isoformat()),
            metadata=data.get('metadata', {}),
            aws_snapshot_id=data.get('aws_snapshot_id'),
            snapshot_path=data.get('snapshot_path')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert VolumeSnapshot to dictionary."""
        return {
            'name': self.name,
            'namespace': self.namespace,
            'pvc_name': self.pvc_name,
            'pv_name': self.pv_name,
            'storage_class': self.storage_class,
            'status': self.status.value,
            'size': self.size,
            'created_at': self.created_at,
            'metadata': self.metadata,
            'aws_snapshot_id': self.aws_snapshot_id,
            'snapshot_path': self.snapshot_path
        }


@dataclass
class VolumeSnapshotContent:
    """VolumeSnapshotContent - represents the actual snapshot data."""
    name: str
    snapshot_name: str  # Reference to VolumeSnapshot
    volume_handle: str  # Reference to source volume
    driver: str  # Storage driver (e.g., 'ebs', 'hostpath')
    size: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ready_to_use: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    @log_to_file(logger)
    def from_dict(cls, data: Dict[str, Any]) -> 'VolumeSnapshotContent':
        """Create VolumeSnapshotContent from dictionary."""
        return cls(
            name=data.get('name', ''),
            snapshot_name=data.get('snapshot_name', ''),
            volume_handle=data.get('volume_handle', ''),
            driver=data.get('driver', ''),
            size=data.get('size'),
            created_at=data.get('created_at', datetime.now(timezone.utc).isoformat()),
            ready_to_use=data.get('ready_to_use', False),
            metadata=data.get('metadata', {})
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert VolumeSnapshotContent to dictionary."""
        return {
            'name': self.name,
            'snapshot_name': self.snapshot_name,
            'volume_handle': self.volume_handle,
            'driver': self.driver,
            'size': self.size,
            'created_at': self.created_at,
            'ready_to_use': self.ready_to_use,
            'metadata': self.metadata
        }

