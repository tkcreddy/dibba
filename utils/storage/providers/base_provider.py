"""
Base class for volume providers.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


class VolumeProvider(ABC):
    """Base class for volume providers."""
    
    @abstractmethod
    @log_to_file(logger)
    def create_volume(
        self,
        name: str,
        capacity: str,
        parameters: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Create a volume.
        
        Args:
            name: Volume name
            capacity: Volume capacity (e.g., "10Gi")
            parameters: Storage class parameters
            
        Returns:
            Dictionary with volume information:
            - host_path: Path on host (for hostPath)
            - aws_volume_id: EBS volume ID (for EBS)
            - aws_efs_id: EFS filesystem ID (for EFS)
            - mount_path: Where volume is mounted
            - node_name: Node where volume is attached
            - metadata: Additional metadata
        """
        pass
    
    @abstractmethod
    @log_to_file(logger)
    def attach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Attach volume to a node.
        
        Args:
            volume_id: Volume identifier
            node_name: Node to attach to
            mount_path: Optional mount path
            
        Returns:
            Dictionary with attachment information:
            - mount_path: Where volume is mounted
            - device: Device path (for block storage)
        """
        pass
    
    @abstractmethod
    @log_to_file(logger)
    def detach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> bool:
        """Detach volume from a node.
        
        Args:
            volume_id: Volume identifier
            node_name: Node to detach from
            mount_path: Optional mount path
            
        Returns:
            True if successful, False otherwise
        """
        pass
    
    @abstractmethod
    @log_to_file(logger)
    def delete_volume(
        self,
        volume_id: str,
        node_name: Optional[str] = None
    ) -> bool:
        """Delete a volume.
        
        Args:
            volume_id: Volume identifier
            node_name: Optional node name (for cleanup)
            
        Returns:
            True if successful, False otherwise
        """
        pass

