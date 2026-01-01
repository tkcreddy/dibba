"""
HostPath volume provider.
"""
from typing import Dict, Any, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.storage.providers.base_provider import VolumeProvider
import os
import uuid

logger = LogKCld()


class HostPathVolumeProvider(VolumeProvider):
    """Provider for hostPath volumes."""
    
    DEFAULT_BASE_PATH = "/var/lib/dibba/volumes"
    
    @log_to_file(logger)
    def __init__(self, base_path: Optional[str] = None):
        """Initialize HostPath provider.
        
        Args:
            base_path: Base path for volumes. Defaults to /var/lib/dibba/volumes
        """
        self.base_path = base_path or self.DEFAULT_BASE_PATH
        # Ensure base path exists
        os.makedirs(self.base_path, mode=0o755, exist_ok=True)
        logger.info(f"HostPath provider initialized with base path: {self.base_path}")
    
    @log_to_file(logger)
    def create_volume(
        self,
        name: str,
        capacity: str,
        parameters: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Create a hostPath volume."""
        # Get custom path from parameters or use default
        custom_path = parameters.get('path', self.base_path)
        volume_path = os.path.join(custom_path, name)
        
        # Create directory
        try:
            os.makedirs(volume_path, mode=0o755, exist_ok=True)
            logger.info(f"Created hostPath volume directory: {volume_path}")
            
            return {
                'host_path': volume_path,
                'mount_path': volume_path,
                'metadata': {
                    'capacity': capacity,
                    'created_by': 'dibba-hostpath-provider'
                }
            }
        except Exception as e:
            logger.error(f"Failed to create hostPath volume {name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def attach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Attach hostPath volume (no-op, already available on host)."""
        # HostPath volumes are already "attached" since they're on the host filesystem
        # Just verify the path exists
        if os.path.exists(volume_id):
            logger.info(f"HostPath volume {volume_id} is available on node {node_name}")
            return {
                'mount_path': mount_path or volume_id,
                'device': None
            }
        else:
            logger.error(f"HostPath volume {volume_id} does not exist")
            return None
    
    @log_to_file(logger)
    def detach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> bool:
        """Detach hostPath volume (no-op)."""
        # HostPath volumes don't need explicit detachment
        logger.info(f"HostPath volume {volume_id} detached from node {node_name} (no-op)")
        return True
    
    @log_to_file(logger)
    def delete_volume(
        self,
        volume_id: str,
        node_name: Optional[str] = None
    ) -> bool:
        """Delete hostPath volume."""
        try:
            if os.path.exists(volume_id):
                import shutil
                shutil.rmtree(volume_id)
                logger.info(f"Deleted hostPath volume: {volume_id}")
                return True
            else:
                logger.warning(f"HostPath volume {volume_id} does not exist")
                return True  # Consider it successful if already gone
        except Exception as e:
            logger.error(f"Failed to delete hostPath volume {volume_id}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    def create_snapshot(
        self,
        volume_id: str,
        snapshot_name: str,
        node_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Create a hostPath snapshot (directory copy)."""
        try:
            if not os.path.exists(volume_id):
                logger.error(f"Volume {volume_id} does not exist")
                return None
            
            snapshot_path = f"{volume_id}.snapshot.{snapshot_name}"
            shutil.copytree(volume_id, snapshot_path)
            
            logger.info(f"Created hostPath snapshot {snapshot_path} for volume {volume_id}")
            
            return {
                'snapshot_path': snapshot_path,
                'ready': True,
                'metadata': {
                    'source_volume': volume_id
                }
            }
        except Exception as e:
            logger.error(f"Failed to create hostPath snapshot: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def restore_from_snapshot(
        self,
        snapshot_id: str,
        volume_name: str,
        capacity: str,
        parameters: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Restore a hostPath volume from a snapshot."""
        try:
            if not os.path.exists(snapshot_id):
                logger.error(f"Snapshot {snapshot_id} does not exist")
                return None
            
            # Get custom path from parameters or use default
            custom_path = parameters.get('path', self.base_path)
            restored_path = os.path.join(custom_path, volume_name)
            
            shutil.copytree(snapshot_id, restored_path)
            
            logger.info(f"Restored hostPath volume {restored_path} from snapshot {snapshot_id}")
            
            return {
                'host_path': restored_path,
                'mount_path': restored_path,
                'metadata': {
                    'restored_from_snapshot': snapshot_id
                }
            }
        except Exception as e:
            logger.error(f"Failed to restore hostPath volume from snapshot: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a hostPath snapshot."""
        try:
            if os.path.exists(snapshot_id):
                shutil.rmtree(snapshot_id)
                logger.info(f"Deleted hostPath snapshot {snapshot_id}")
                return True
            else:
                logger.warning(f"Snapshot {snapshot_id} does not exist")
                return True
        except Exception as e:
            logger.error(f"Failed to delete hostPath snapshot {snapshot_id}: {e}", exc_info=True)
            return False

