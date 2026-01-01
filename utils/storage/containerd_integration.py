"""
Integration between Dibba storage system and containerd operations.

This module provides helper functions to:
- Resolve PVC references in volume mounts
- Ensure volumes are attached before container creation
- Validate and prepare volume mounts for containerd
"""
from typing import List, Dict, Any, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.storage.volume_manager import VolumeManager
from utils.storage.volume_store import VolumeStore

logger = LogKCld()


@log_to_file(logger)
def resolve_volume_mounts_for_containerd(
    volume_mounts: Optional[List[Dict[str, Any]]],
    volumes: Optional[List[Dict[str, Any]]],
    namespace: str,
    hostname: str
) -> List[Dict[str, Any]]:
    """Resolve volume mounts for containerd, handling PVC references.
    
    This function:
    1. Resolves PVC references to actual mount paths
    2. Handles hostPath and emptyDir volumes
    3. Ensures volumes are attached to the node
    4. Returns mounts in containerd-compatible format
    
    Args:
        volume_mounts: List of volumeMount definitions (from containers)
        volumes: List of volume definitions (from pod spec)
        namespace: Pod namespace
        hostname: Node hostname where pod will be created
        
    Returns:
        List of resolved mount dictionaries in containerd format:
        {
            "destination": "/path/in/container",
            "source": "/path/on/host",
            "type": "bind",
            "options": ["rw"] or ["ro"]
        }
    """
    if not volume_mounts:
        return []
    
    volume_manager = VolumeManager()
    volume_store = VolumeStore()
    
    # Build volume name to mount path mapping
    volume_mount_map = {}
    
    if volumes:
        for volume in volumes:
            volume_name = volume.get('name')
            if not volume_name:
                continue
            
            # Handle different volume types
            if 'persistentVolumeClaim' in volume:
                pvc_spec = volume['persistentVolumeClaim']
                pvc_name = pvc_spec.get('claimName')
                
                if pvc_name:
                    # Get PVC and resolve to mount path
                    pvc = volume_store.get_pvc(namespace, pvc_name)
                    if pvc and pvc.volume_name:
                        # Get PV to find mount path
                        from utils.storage.volume import PersistentVolume
                        pv = volume_store.get_pv(pvc.volume_name)
                        if pv:
                            mount_path = pv.mount_path or pv.host_path
                            if mount_path:
                                volume_mount_map[volume_name] = mount_path
                                
                                # Ensure volume is attached to node
                                if pv.node_name != hostname:
                                    logger.info(f"Attaching volume {pv.name} to node {hostname}")
                                    volume_manager.attach_volume_to_node(pv.name, hostname)
                                
                                logger.info(f"Resolved PVC {namespace}/{pvc_name} to mount path: {mount_path}")
                            else:
                                logger.warning(f"PV {pv.name} has no mount path")
                        else:
                            logger.warning(f"PV {pvc.volume_name} not found for PVC {namespace}/{pvc_name}")
                    else:
                        logger.warning(f"PVC {namespace}/{pvc_name} not found or not bound")
            
            elif 'hostPath' in volume:
                # Direct hostPath volume
                host_path = volume['hostPath'].get('path')
                if host_path:
                    volume_mount_map[volume_name] = host_path
                    logger.info(f"Resolved hostPath volume {volume_name} to: {host_path}")
            
            elif 'emptyDir' in volume:
                # EmptyDir volume - create temporary directory
                import tempfile
                temp_dir = tempfile.mkdtemp(prefix=f"dibba-emptydir-{volume_name}-")
                volume_mount_map[volume_name] = temp_dir
                logger.info(f"Created emptyDir volume {volume_name} at: {temp_dir}")
    
    # Resolve volume mounts
    resolved_mounts = []
    for mount in volume_mounts:
        mount_name = mount.get('name') or mount.get('volumeName')
        mount_path = mount.get('mountPath') or mount.get('containerPath') or mount.get('destination')
        
        if not mount_path:
            logger.warning(f"Volume mount missing mountPath: {mount}")
            continue
        
        # Check if this is a PVC reference
        if mount_name and mount_name in volume_mount_map:
            # Resolve PVC or volume reference
            host_path = volume_mount_map[mount_name]
            
            resolved_mount = {
                'destination': mount_path,
                'source': host_path,
                'type': 'bind',
            }
            
            # Add read-only option if specified
            options = []
            if mount.get('readOnly') or mount.get('readonly'):
                options.append('ro')
            else:
                options.append('rw')
            
            # Add propagation mode if specified
            propagation = mount.get('propagation') or mount.get('mountPropagation')
            if propagation:
                if propagation.upper() in ['PRIVATE', 'SHARED', 'SLAVE', 'RSLAVE', 'RUNBINDABLE']:
                    options.append(propagation.lower())
            
            resolved_mount['options'] = options
            resolved_mounts.append(resolved_mount)
            logger.info(f"Resolved volume mount: {mount_name} -> {mount_path} (from {host_path})")
        
        elif mount.get('hostPath') or mount.get('source'):
            # Direct mount (already has hostPath/source)
            resolved_mount = {
                'destination': mount_path,
                'source': mount.get('hostPath') or mount.get('source'),
                'type': mount.get('type', 'bind'),
            }
            
            options = []
            if mount.get('readOnly') or mount.get('readonly'):
                options.append('ro')
            else:
                options.append('rw')
            
            if mount.get('options'):
                if isinstance(mount['options'], list):
                    options.extend(mount['options'])
                else:
                    options.append(mount['options'])
            
            resolved_mount['options'] = options
            resolved_mounts.append(resolved_mount)
        
        else:
            logger.warning(f"Volume mount {mount_name} not found in volumes, skipping")
    
    return resolved_mounts


@log_to_file(logger)
def ensure_volumes_attached(
    volumes: Optional[List[Dict[str, Any]]],
    namespace: str,
    hostname: str
) -> bool:
    """Ensure all PVC volumes are attached to the node before container creation.
    
    Args:
        volumes: List of volume definitions from pod spec
        namespace: Pod namespace
        hostname: Node hostname
        
    Returns:
        True if all volumes are attached, False otherwise
    """
    if not volumes:
        return True
    
    volume_manager = VolumeManager()
    volume_store = VolumeStore()
    all_attached = True
    
    for volume in volumes:
        if 'persistentVolumeClaim' not in volume:
            continue
        
        pvc_spec = volume['persistentVolumeClaim']
        pvc_name = pvc_spec.get('claimName')
        
        if not pvc_name:
            continue
        
        # Get PVC
        pvc = volume_store.get_pvc(namespace, pvc_name)
        if not pvc or not pvc.volume_name:
            logger.warning(f"PVC {namespace}/{pvc_name} not found or not bound")
            all_attached = False
            continue
        
        # Get PV
        pv = volume_store.get_pv(pvc.volume_name)
        if not pv:
            logger.warning(f"PV {pvc.volume_name} not found")
            all_attached = False
            continue
        
        # Check if attached to this node
        if pv.node_name != hostname:
            logger.info(f"Attaching volume {pv.name} to node {hostname}")
            success = volume_manager.attach_volume_to_node(pv.name, hostname)
            if not success:
                logger.error(f"Failed to attach volume {pv.name} to node {hostname}")
                all_attached = False
        else:
            logger.debug(f"Volume {pv.name} already attached to node {hostname}")
    
    return all_attached


@log_to_file(logger)
def validate_volume_mounts(
    volume_mounts: Optional[List[Dict[str, Any]]],
    volumes: Optional[List[Dict[str, Any]]]
) -> tuple[bool, Optional[str]]:
    """Validate that all volume mounts reference valid volumes.
    
    Args:
        volume_mounts: List of volumeMount definitions
        volumes: List of volume definitions
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not volume_mounts:
        return True, None
    
    if not volumes:
        return False, "Volume mounts specified but no volumes defined"
    
    # Build volume name set
    volume_names = {v.get('name') for v in volumes if v.get('name')}
    
    # Check each mount
    for mount in volume_mounts:
        mount_name = mount.get('name') or mount.get('volumeName')
        if mount_name and mount_name not in volume_names:
            # Check if it's a direct mount (has hostPath/source)
            if not (mount.get('hostPath') or mount.get('source')):
                return False, f"Volume mount references unknown volume: {mount_name}"
    
    return True, None


@log_to_file(logger)
def prepare_volumes_for_pod(
    volumes: Optional[List[Dict[str, Any]]],
    namespace: str,
    hostname: str
) -> Dict[str, Any]:
    """Prepare volumes for pod creation.
    
    This function:
    1. Creates PVCs if they don't exist (from volume definitions)
    2. Ensures volumes are attached to the node
    3. Returns volume preparation status
    
    Args:
        volumes: List of volume definitions from pod spec
        namespace: Pod namespace
        hostname: Node hostname
        
    Returns:
        Dictionary with preparation status:
        {
            'success': bool,
            'volumes_prepared': int,
            'volumes_failed': int,
            'errors': List[str]
        }
    """
    if not volumes:
        return {
            'success': True,
            'volumes_prepared': 0,
            'volumes_failed': 0,
            'errors': []
        }
    
    from utils.celery.tasks.scheduler_tasks import _create_pvcs_from_volumes
    
    result = {
        'success': True,
        'volumes_prepared': 0,
        'volumes_failed': 0,
        'errors': []
    }
    
    # Create PVCs if needed
    try:
        _create_pvcs_from_volumes(volumes, namespace, "pod-creation")
        result['volumes_prepared'] = len([v for v in volumes if 'persistentVolumeClaim' in v])
    except Exception as e:
        error_msg = f"Failed to create PVCs: {str(e)}"
        logger.error(error_msg, exc_info=True)
        result['errors'].append(error_msg)
        result['success'] = False
    
    # Ensure volumes are attached
    try:
        attached = ensure_volumes_attached(volumes, namespace, hostname)
        if not attached:
            result['volumes_failed'] = 1
            result['errors'].append("Some volumes failed to attach")
            result['success'] = False
    except Exception as e:
        error_msg = f"Failed to attach volumes: {str(e)}"
        logger.error(error_msg, exc_info=True)
        result['errors'].append(error_msg)
        result['success'] = False
    
    return result

