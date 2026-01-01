"""
Storage abstraction layer for Dibba.

Provides:
- StorageClass definitions
- PersistentVolume (PV) management
- PersistentVolumeClaim (PVC) support
- Volume lifecycle management
- Cloud storage integration (EBS, EFS, etc.)
"""

from utils.storage.storage_classes import StorageClass, StorageClassType
from utils.storage.volume import PersistentVolume, PersistentVolumeClaim, VolumeStatus, VolumeAccessMode
from utils.storage.volume_store import VolumeStore
from utils.storage.volume_manager import VolumeManager

__all__ = [
    'StorageClass',
    'StorageClassType',
    'PersistentVolume',
    'PersistentVolumeClaim',
    'VolumeStatus',
    'VolumeAccessMode',
    'VolumeStore',
    'VolumeManager',
]

