"""
Volume Manager for handling volume lifecycle and provisioning.
"""
from typing import Optional, Dict, Any, List
from logpkg.log_kcld import LogKCld, log_to_file
from utils.storage.volume_store import VolumeStore
from utils.storage.volume import PersistentVolume, PersistentVolumeClaim, VolumeStatus, VolumeAccessMode
from utils.storage.storage_classes import StorageClass, StorageClassType, DEFAULT_STORAGE_CLASSES
from utils.storage.providers.ebs_provider import EBSVolumeProvider
from utils.storage.providers.hostpath_provider import HostPathVolumeProvider
from utils.storage.snapshot import VolumeSnapshot, VolumeSnapshotContent, SnapshotStatus
import os
import uuid
import shutil

logger = LogKCld()


class VolumeManager:
    """Manages volume lifecycle: provisioning, binding, attachment, deletion."""
    
    def __init__(self, volume_store: Optional[VolumeStore] = None):
        """Initialize VolumeManager.
        
        Args:
            volume_store: Optional VolumeStore instance. If None, creates a new one.
        """
        if volume_store is None:
            volume_store = VolumeStore()
        self.store = volume_store
        self.storage_classes = DEFAULT_STORAGE_CLASSES.copy()
        
        # Initialize volume providers
        self.providers = {
            StorageClassType.AWS_EBS: EBSVolumeProvider(),
            StorageClassType.HOST_PATH: HostPathVolumeProvider(),
            StorageClassType.LOCAL: HostPathVolumeProvider(),
        }
    
    @log_to_file(logger)
    def register_storage_class(self, storage_class: StorageClass) -> None:
        """Register a custom storage class."""
        self.storage_classes[storage_class.name] = storage_class
        logger.info(f"Registered storage class: {storage_class.name}")
    
    @log_to_file(logger)
    def get_storage_class(self, name: str) -> Optional[StorageClass]:
        """Get storage class by name."""
        return self.storage_classes.get(name)
    
    @log_to_file(logger)
    def create_pvc(self, pvc: PersistentVolumeClaim) -> PersistentVolumeClaim:
        """Create a PersistentVolumeClaim.
        
        This will:
        1. Find or create a matching PersistentVolume
        2. Bind the PVC to the PV
        3. Update status
        
        Returns:
            Updated PVC with binding information
        """
        logger.info(f"Creating PVC {pvc.namespace}/{pvc.name}")
        
        # Determine storage class (use default if not specified)
        storage_class_name = pvc.storage_class or 'hostpath'
        storage_class = self.get_storage_class(storage_class_name)
        if not storage_class:
            raise ValueError(f"Storage class '{storage_class_name}' not found")
        
        # Try to find an available PV
        requested_storage = pvc.requested_storage
        pv = self.store.find_available_pv(
            storage_class=storage_class_name,
            capacity=requested_storage,
            access_modes=pvc.access_modes
        )
        
        if not pv:
            # Provision a new PV
            logger.info(f"No available PV found, provisioning new PV for PVC {pvc.namespace}/{pvc.name}")
            pv = self._provision_volume(
                storage_class=storage_class,
                capacity=requested_storage,
                access_modes=pvc.access_modes,
                pvc_name=pvc.name,
                namespace=pvc.namespace
            )
        
        if not pv:
            raise RuntimeError(f"Failed to provision volume for PVC {pvc.namespace}/{pvc.name}")
        
        # Bind PVC to PV
        pvc.bind_to_volume(pv.name)
        pv.update_status(VolumeStatus.BOUND, claim_ref=f"{pvc.namespace}/{pvc.name}")
        
        # Save to store
        self.store.save_pvc(pvc)
        self.store.save_pv(pv)
        
        logger.info(f"PVC {pvc.namespace}/{pvc.name} bound to PV {pv.name}")
        return pvc
    
    @log_to_file(logger)
    def _provision_volume(
        self,
        storage_class: StorageClass,
        capacity: str,
        access_modes: List[VolumeAccessMode],
        pvc_name: str,
        namespace: str
    ) -> Optional[PersistentVolume]:
        """Provision a new PersistentVolume based on storage class."""
        logger.info(f"Provisioning volume: class={storage_class.name}, capacity={capacity}")
        
        # Generate PV name
        pv_name = f"pv-{uuid.uuid4().hex[:8]}"
        
        # Get provider for storage class type
        provider = self.providers.get(storage_class.type)
        if not provider:
            logger.error(f"No provider found for storage class type: {storage_class.type}")
            return None
        
        try:
            # Provision volume using provider
            volume_info = provider.create_volume(
                name=pv_name,
                capacity=capacity,
                parameters=storage_class.parameters
            )
            
            if not volume_info:
                logger.error(f"Provider failed to create volume: {pv_name}")
                return None
            
            # Create PV object
            pv = PersistentVolume(
                name=pv_name,
                storage_class=storage_class.name,
                capacity=capacity,
                access_modes=access_modes,
                status=VolumeStatus.AVAILABLE,
                host_path=volume_info.get('host_path'),
                aws_volume_id=volume_info.get('aws_volume_id'),
                aws_efs_id=volume_info.get('aws_efs_id'),
                mount_path=volume_info.get('mount_path'),
                node_name=volume_info.get('node_name'),
                metadata=volume_info.get('metadata', {})
            )
            
            # Save to store
            self.store.save_pv(pv)
            logger.info(f"Provisioned PV {pv_name} using {storage_class.name}")
            return pv
            
        except Exception as e:
            logger.error(f"Failed to provision volume: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def attach_volume_to_node(self, pv_name: str, node_name: str) -> bool:
        """Attach volume to a node.
        
        Args:
            pv_name: Name of the PersistentVolume
            node_name: Name of the node to attach to
            
        Returns:
            True if attachment successful, False otherwise
        """
        pv = self.store.get_pv(pv_name)
        if not pv:
            logger.error(f"PV {pv_name} not found")
            return False
        
        if pv.status != VolumeStatus.BOUND:
            logger.warning(f"PV {pv_name} is not bound (status: {pv.status})")
            return False
        
        storage_class = self.get_storage_class(pv.storage_class)
        if not storage_class:
            logger.error(f"Storage class {pv.storage_class} not found")
            return False
        
        provider = self.providers.get(storage_class.type)
        if not provider:
            logger.error(f"No provider for storage class type: {storage_class.type}")
            return False
        
        try:
            # Attach volume using provider
            mount_info = provider.attach_volume(
                volume_id=pv.aws_volume_id or pv.name,
                node_name=node_name,
                mount_path=pv.mount_path
            )
            
            if mount_info:
                pv.node_name = node_name
                pv.mount_path = mount_info.get('mount_path', pv.mount_path)
                self.store.save_pv(pv)
                logger.info(f"Attached PV {pv_name} to node {node_name}")
                return True
            else:
                logger.error(f"Provider failed to attach volume {pv_name}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to attach volume {pv_name} to node {node_name}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    def detach_volume_from_node(self, pv_name: str) -> bool:
        """Detach volume from its current node."""
        pv = self.store.get_pv(pv_name)
        if not pv:
            logger.error(f"PV {pv_name} not found")
            return False
        
        if not pv.node_name:
            logger.warning(f"PV {pv_name} is not attached to any node")
            return True  # Already detached
        
        storage_class = self.get_storage_class(pv.storage_class)
        if not storage_class:
            logger.error(f"Storage class {pv.storage_class} not found")
            return False
        
        provider = self.providers.get(storage_class.type)
        if not provider:
            logger.error(f"No provider for storage class type: {storage_class.type}")
            return False
        
        try:
            # Detach volume using provider
            success = provider.detach_volume(
                volume_id=pv.aws_volume_id or pv.name,
                node_name=pv.node_name,
                mount_path=pv.mount_path
            )
            
            if success:
                pv.node_name = None
                self.store.save_pv(pv)
                logger.info(f"Detached PV {pv_name} from node")
                return True
            else:
                logger.error(f"Provider failed to detach volume {pv_name}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to detach volume {pv_name}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    def delete_pvc(self, namespace: str, name: str, delete_volume: bool = False) -> bool:
        """Delete a PersistentVolumeClaim.
        
        Args:
            namespace: PVC namespace
            name: PVC name
            delete_volume: If True, also delete the bound PV
            
        Returns:
            True if deletion successful, False otherwise
        """
        pvc = self.store.get_pvc(namespace, name)
        if not pvc:
            logger.warning(f"PVC {namespace}/{name} not found")
            return False
        
        # If PVC is bound, handle the PV
        if pvc.volume_name:
            pv = self.store.get_pv(pvc.volume_name)
            if pv:
                # Release the PV
                pv.update_status(VolumeStatus.RELEASED)
                pv.claim_ref = None
                self.store.save_pv(pv)
                
                # Delete PV if requested
                if delete_volume:
                    self.delete_pv(pvc.volume_name)
        
        # Delete PVC
        self.store.delete_pvc(namespace, name)
        logger.info(f"Deleted PVC {namespace}/{name}")
        return True
    
    @log_to_file(logger)
    def delete_pv(self, name: str) -> bool:
        """Delete a PersistentVolume.
        
        This will:
        1. Detach volume from node if attached
        2. Delete volume using provider
        3. Remove from store
        """
        pv = self.store.get_pv(name)
        if not pv:
            logger.warning(f"PV {name} not found")
            return False
        
        if pv.status == VolumeStatus.BOUND:
            logger.warning(f"Cannot delete bound PV {name}. Release it first.")
            return False
        
        # Detach if attached
        if pv.node_name:
            self.detach_volume_from_node(name)
        
        # Delete using provider
        storage_class = self.get_storage_class(pv.storage_class)
        if storage_class:
            provider = self.providers.get(storage_class.type)
            if provider:
                try:
                    provider.delete_volume(
                        volume_id=pv.aws_volume_id or pv.name,
                        node_name=pv.node_name
                    )
                except Exception as e:
                    logger.warning(f"Provider failed to delete volume {name}: {e}")
        
        # Remove from store
        self.store.delete_pv(name)
        logger.info(f"Deleted PV {name}")
        return True
    
    @log_to_file(logger)
    def get_pvc_mount_path(self, namespace: str, name: str) -> Optional[str]:
        """Get the mount path for a PVC.
        
        Returns the path where the volume is mounted on the host.
        """
        pvc = self.store.get_pvc(namespace, name)
        if not pvc or not pvc.volume_name:
            return None
        
        pv = self.store.get_pv(pvc.volume_name)
        if not pv:
            return None
        
        return pv.mount_path or pv.host_path
    
    @log_to_file(logger)
    def create_snapshot(self, namespace: str, pvc_name: str, snapshot_name: str) -> Optional[VolumeSnapshot]:
        """Create a snapshot of a PVC.
        
        Args:
            namespace: PVC namespace
            pvc_name: PVC name
            snapshot_name: Name for the snapshot
            
        Returns:
            VolumeSnapshot object if successful, None otherwise
        """
        logger.info(f"Creating snapshot {snapshot_name} for PVC {namespace}/{pvc_name}")
        
        # Get PVC
        pvc = self.store.get_pvc(namespace, pvc_name)
        if not pvc:
            logger.error(f"PVC {namespace}/{pvc_name} not found")
            return None
        
        if not pvc.volume_name:
            logger.error(f"PVC {namespace}/{pvc_name} is not bound to a volume")
            return None
        
        # Get PV
        pv = self.store.get_pv(pvc.volume_name)
        if not pv:
            logger.error(f"PV {pvc.volume_name} not found")
            return None
        
        # Create snapshot using provider
        storage_class = self.get_storage_class(pv.storage_class)
        if not storage_class:
            logger.error(f"Storage class {pv.storage_class} not found")
            return None
        
        provider = self.providers.get(storage_class.type)
        if not provider:
            logger.error(f"No provider for storage class type: {storage_class.type}")
            return None
        
        try:
            # Create snapshot (if provider supports it)
            snapshot_info = None
            if hasattr(provider, 'create_snapshot'):
                snapshot_info = provider.create_snapshot(
                    volume_id=pv.aws_volume_id or pv.name,
                    snapshot_name=snapshot_name,
                    node_name=pv.node_name
                )
            else:
                # Fallback: create directory-based snapshot for hostPath
                if pv.host_path and os.path.exists(pv.host_path):
                    snapshot_path = f"{pv.host_path}.snapshot.{snapshot_name}"
                    shutil.copytree(pv.host_path, snapshot_path)
                    snapshot_info = {
                        'snapshot_path': snapshot_path,
                        'ready': True
                    }
            
            if not snapshot_info:
                logger.error(f"Provider failed to create snapshot {snapshot_name}")
                return None
            
            # Create snapshot object
            snapshot = VolumeSnapshot(
                name=snapshot_name,
                namespace=namespace,
                pvc_name=pvc_name,
                pv_name=pv.name,
                storage_class=pv.storage_class,
                status=SnapshotStatus.READY if snapshot_info.get('ready', True) else SnapshotStatus.PENDING,
                size=pv.capacity,
                aws_snapshot_id=snapshot_info.get('aws_snapshot_id'),
                snapshot_path=snapshot_info.get('snapshot_path'),
                metadata=snapshot_info.get('metadata', {})
            )
            
            # Save to store
            self.store.save_snapshot(snapshot)
            logger.info(f"Created snapshot {snapshot_name} for PVC {namespace}/{pvc_name}")
            return snapshot
            
        except Exception as e:
            logger.error(f"Failed to create snapshot {snapshot_name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def restore_from_snapshot(self, snapshot_name: str, namespace: str, new_pvc_name: str) -> Optional[PersistentVolumeClaim]:
        """Restore a PVC from a snapshot.
        
        Args:
            snapshot_name: Name of the snapshot
            namespace: Namespace for the new PVC
            new_pvc_name: Name for the new PVC
            
        Returns:
            New PersistentVolumeClaim if successful, None otherwise
        """
        logger.info(f"Restoring PVC {namespace}/{new_pvc_name} from snapshot {snapshot_name}")
        
        # Get snapshot
        snapshot = self.store.get_snapshot(namespace, snapshot_name)
        if not snapshot:
            logger.error(f"Snapshot {namespace}/{snapshot_name} not found")
            return None
        
        if snapshot.status != SnapshotStatus.READY:
            logger.error(f"Snapshot {snapshot_name} is not ready (status: {snapshot.status})")
            return None
        
        # Get original PVC to copy settings
        original_pvc = self.store.get_pvc(snapshot.namespace, snapshot.pvc_name)
        if not original_pvc:
            logger.error(f"Original PVC {snapshot.namespace}/{snapshot.pvc_name} not found")
            return None
        
        # Create new PVC from snapshot
        new_pvc = PersistentVolumeClaim(
            name=new_pvc_name,
            namespace=namespace,
            storage_class=original_pvc.storage_class,
            access_modes=original_pvc.access_modes,
            resources=original_pvc.resources
        )
        
        # Provision volume from snapshot
        storage_class = self.get_storage_class(new_pvc.storage_class or 'hostpath')
        if not storage_class:
            logger.error(f"Storage class {new_pvc.storage_class} not found")
            return None
        
        provider = self.providers.get(storage_class.type)
        if not provider:
            logger.error(f"No provider for storage class type: {storage_class.type}")
            return None
        
        try:
            # Restore volume from snapshot
            volume_info = None
            if hasattr(provider, 'restore_from_snapshot'):
                volume_info = provider.restore_from_snapshot(
                    snapshot_id=snapshot.aws_snapshot_id or snapshot.snapshot_path,
                    volume_name=f"pv-restored-{uuid.uuid4().hex[:8]}",
                    capacity=snapshot.size or original_pvc.requested_storage,
                    parameters=storage_class.parameters
                )
            else:
                # Fallback: restore from directory snapshot
                if snapshot.snapshot_path and os.path.exists(snapshot.snapshot_path):
                    restored_path = os.path.join(self.providers[StorageClassType.HOST_PATH].base_path, f"pv-restored-{uuid.uuid4().hex[:8]}")
                    shutil.copytree(snapshot.snapshot_path, restored_path)
                    volume_info = {
                        'host_path': restored_path,
                        'mount_path': restored_path
                    }
            
            if not volume_info:
                logger.error(f"Provider failed to restore volume from snapshot")
                return None
            
            # Create PV from restored volume
            pv = PersistentVolume(
                name=volume_info.get('name', f"pv-restored-{uuid.uuid4().hex[:8]}"),
                storage_class=storage_class.name,
                capacity=snapshot.size or original_pvc.requested_storage,
                access_modes=new_pvc.access_modes,
                status=VolumeStatus.AVAILABLE,
                host_path=volume_info.get('host_path'),
                aws_volume_id=volume_info.get('aws_volume_id'),
                mount_path=volume_info.get('mount_path'),
                metadata={
                    'restored_from_snapshot': snapshot_name,
                    'original_pvc': f"{snapshot.namespace}/{snapshot.pvc_name}"
                }
            )
            
            # Bind PVC to PV
            new_pvc.bind_to_volume(pv.name)
            pv.update_status(VolumeStatus.BOUND, claim_ref=f"{namespace}/{new_pvc_name}")
            
            # Save to store
            self.store.save_pv(pv)
            self.store.save_pvc(new_pvc)
            
            logger.info(f"Restored PVC {namespace}/{new_pvc_name} from snapshot {snapshot_name}")
            return new_pvc
            
        except Exception as e:
            logger.error(f"Failed to restore from snapshot {snapshot_name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def delete_snapshot(self, namespace: str, name: str) -> bool:
        """Delete a volume snapshot."""
        snapshot = self.store.get_snapshot(namespace, name)
        if not snapshot:
            logger.warning(f"Snapshot {namespace}/{name} not found")
            return False
        
        # Delete using provider if needed
        if snapshot.pv_name:
            pv = self.store.get_pv(snapshot.pv_name)
            if pv:
                storage_class = self.get_storage_class(pv.storage_class)
                if storage_class:
                    provider = self.providers.get(storage_class.type)
                    if provider and hasattr(provider, 'delete_snapshot'):
                        try:
                            provider.delete_snapshot(
                                snapshot_id=snapshot.aws_snapshot_id or snapshot.snapshot_path
                            )
                        except Exception as e:
                            logger.warning(f"Provider failed to delete snapshot: {e}")
                    elif snapshot.snapshot_path and os.path.exists(snapshot.snapshot_path):
                        # Delete directory snapshot
                        shutil.rmtree(snapshot.snapshot_path)
        
        # Remove from store
        self.store.delete_snapshot(namespace, name)
        logger.info(f"Deleted snapshot {namespace}/{name}")
        return True

