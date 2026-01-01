"""
Volume providers for different storage backends.
"""
from utils.storage.providers.base_provider import VolumeProvider
from utils.storage.providers.ebs_provider import EBSVolumeProvider
from utils.storage.providers.hostpath_provider import HostPathVolumeProvider

__all__ = [
    'VolumeProvider',
    'EBSVolumeProvider',
    'HostPathVolumeProvider',
]

