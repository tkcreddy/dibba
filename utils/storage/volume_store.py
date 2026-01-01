"""
Redis-based storage for PersistentVolumes and PersistentVolumeClaims.
"""
from typing import Optional, List, Dict, Any
from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.redis_interface import RedisInterface
from utils.storage.volume import PersistentVolume, PersistentVolumeClaim, VolumeStatus
import json

logger = LogKCld()


class VolumeStore:
    """Store for PersistentVolumes and PersistentVolumeClaims in Redis."""
    
    # Redis key patterns
    PV_KEY_PREFIX = "pv:"
    PVC_KEY_PREFIX = "pvc:"
    PV_INDEX = "pv:index:all"
    PVC_INDEX = "pvc:index:all"
    PVC_INDEX_NAMESPACE = "pvc:index:namespace:"
    PV_INDEX_CLASS = "pv:index:storageclass:"
    SNAPSHOT_KEY_PREFIX = "snapshot:"
    SNAPSHOT_INDEX = "snapshot:index:all"
    SNAPSHOT_INDEX_NAMESPACE = "snapshot:index:namespace:"
    SNAPSHOT_CONTENT_KEY_PREFIX = "snapshotcontent:"
    SNAPSHOT_CONTENT_INDEX = "snapshotcontent:index:all"
    
    def __init__(self, redis_interface: Optional[RedisInterface] = None):
        """Initialize VolumeStore.
        
        Args:
            redis_interface: Optional RedisInterface instance. If None, creates a new one.
        """
        if redis_interface is None:
            redis_interface = RedisInterface()
        self.redis = redis_interface
    
    @log_to_file(logger)
    def save_pv(self, pv: PersistentVolume) -> None:
        """Save PersistentVolume to Redis."""
        key = f"{self.PV_KEY_PREFIX}{pv.name}"
        data = pv.to_dict()
        self.redis.redis_client.set(key, json.dumps(data))
        
        # Add to indexes
        self.redis.redis_client.sadd(self.PV_INDEX, pv.name)
        self.redis.redis_client.sadd(f"{self.PV_INDEX_CLASS}{pv.storage_class}", pv.name)
        
        logger.info(f"Saved PV {pv.name} to Redis")
    
    @log_to_file(logger)
    def get_pv(self, name: str) -> Optional[PersistentVolume]:
        """Get PersistentVolume by name."""
        key = f"{self.PV_KEY_PREFIX}{name}"
        data = self.redis.redis_client.get(key)
        if not data:
            return None
        
        try:
            pv_dict = json.loads(data)
            return PersistentVolume.from_dict(pv_dict)
        except Exception as e:
            logger.error(f"Failed to deserialize PV {name}: {e}")
            return None
    
    @log_to_file(logger)
    def delete_pv(self, name: str) -> None:
        """Delete PersistentVolume from Redis."""
        pv = self.get_pv(name)
        if not pv:
            logger.warning(f"PV {name} not found for deletion")
            return
        
        key = f"{self.PV_KEY_PREFIX}{name}"
        self.redis.redis_client.delete(key)
        
        # Remove from indexes
        self.redis.redis_client.srem(self.PV_INDEX, name)
        self.redis.redis_client.srem(f"{self.PV_INDEX_CLASS}{pv.storage_class}", name)
        
        logger.info(f"Deleted PV {name} from Redis")
    
    @log_to_file(logger)
    def list_pvs(self, storage_class: Optional[str] = None, status: Optional[VolumeStatus] = None) -> List[PersistentVolume]:
        """List all PersistentVolumes, optionally filtered by storage class or status."""
        if storage_class:
            pv_names = self.redis.redis_client.smembers(f"{self.PV_INDEX_CLASS}{storage_class}")
        else:
            pv_names = self.redis.redis_client.smembers(self.PV_INDEX)
        
        pvs = []
        for name in pv_names:
            pv = self.get_pv(name.decode() if isinstance(name, bytes) else name)
            if pv:
                if status is None or pv.status == status:
                    pvs.append(pv)
        
        return pvs
    
    @log_to_file(logger)
    def find_available_pv(self, storage_class: str, capacity: str, access_modes: List) -> Optional[PersistentVolume]:
        """Find an available PV matching the requirements."""
        pvs = self.list_pvs(storage_class=storage_class, status=VolumeStatus.AVAILABLE)
        
        for pv in pvs:
            # Check capacity (simple string comparison for now)
            if self._capacity_sufficient(pv.capacity, capacity):
                # Check access modes
                if self._access_modes_compatible(pv.access_modes, access_modes):
                    return pv
        
        return None
    
    @log_to_file(logger)
    def _capacity_sufficient(self, pv_capacity: str, requested_capacity: str) -> bool:
        """Check if PV capacity is sufficient for requested capacity."""
        # Simple implementation: parse and compare
        # In production, use proper unit conversion
        try:
            pv_size = self._parse_size(pv_capacity)
            req_size = self._parse_size(requested_capacity)
            return pv_size >= req_size
        except Exception:
            logger.warning(f"Failed to compare capacities: {pv_capacity} vs {requested_capacity}")
            return False
    
    @log_to_file(logger)
    def _parse_size(self, size_str: str) -> int:
        """Parse size string to bytes."""
        size_str = size_str.strip().upper()
        if size_str.endswith('KI'):
            return int(size_str[:-2]) * 1024
        elif size_str.endswith('MI'):
            return int(size_str[:-2]) * 1024 * 1024
        elif size_str.endswith('GI'):
            return int(size_str[:-2]) * 1024 * 1024 * 1024
        elif size_str.endswith('TI'):
            return int(size_str[:-2]) * 1024 * 1024 * 1024 * 1024
        else:
            # Assume bytes
            return int(size_str)
    
    @log_to_file(logger)
    def _access_modes_compatible(self, pv_modes: List, requested_modes: List) -> bool:
        """Check if PV access modes are compatible with requested modes."""
        pv_mode_values = {mode.value if hasattr(mode, 'value') else mode for mode in pv_modes}
        req_mode_values = {mode.value if hasattr(mode, 'value') else mode for mode in requested_modes}
        
        # At least one requested mode must be supported
        return bool(pv_mode_values & req_mode_values)
    
    @log_to_file(logger)
    def save_pvc(self, pvc: PersistentVolumeClaim) -> None:
        """Save PersistentVolumeClaim to Redis."""
        key = f"{self.PVC_KEY_PREFIX}{pvc.namespace}:{pvc.name}"
        data = pvc.to_dict()
        self.redis.redis_client.set(key, json.dumps(data))
        
        # Add to indexes
        self.redis.redis_client.sadd(self.PVC_INDEX, f"{pvc.namespace}:{pvc.name}")
        self.redis.redis_client.sadd(f"{self.PVC_INDEX_NAMESPACE}{pvc.namespace}", f"{pvc.namespace}:{pvc.name}")
        
        logger.info(f"Saved PVC {pvc.namespace}/{pvc.name} to Redis")
    
    @log_to_file(logger)
    def get_pvc(self, namespace: str, name: str) -> Optional[PersistentVolumeClaim]:
        """Get PersistentVolumeClaim by namespace and name."""
        key = f"{self.PVC_KEY_PREFIX}{namespace}:{name}"
        data = self.redis.redis_client.get(key)
        if not data:
            return None
        
        try:
            pvc_dict = json.loads(data)
            return PersistentVolumeClaim.from_dict(pvc_dict)
        except Exception as e:
            logger.error(f"Failed to deserialize PVC {namespace}/{name}: {e}")
            return None
    
    @log_to_file(logger)
    def delete_pvc(self, namespace: str, name: str) -> None:
        """Delete PersistentVolumeClaim from Redis."""
        key = f"{self.PVC_KEY_PREFIX}{namespace}:{name}"
        self.redis.redis_client.delete(key)
        
        # Remove from indexes
        self.redis.redis_client.srem(self.PVC_INDEX, f"{namespace}:{name}")
        self.redis.redis_client.srem(f"{self.PVC_INDEX_NAMESPACE}{namespace}", f"{namespace}:{name}")
        
        logger.info(f"Deleted PVC {namespace}/{name} from Redis")
    
    @log_to_file(logger)
    def list_pvcs(self, namespace: Optional[str] = None) -> List[PersistentVolumeClaim]:
        """List all PersistentVolumeClaims, optionally filtered by namespace."""
        if namespace:
            pvc_keys = self.redis.redis_client.smembers(f"{self.PVC_INDEX_NAMESPACE}{namespace}")
        else:
            pvc_keys = self.redis.redis_client.smembers(self.PVC_INDEX)
        
        pvcs = []
        for key in pvc_keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            parts = key_str.split(':', 1)
            if len(parts) == 2:
                ns, name = parts
                pvc = self.get_pvc(ns, name)
                if pvc:
                    pvcs.append(pvc)
        
        return pvcs
    
    @log_to_file(logger)
    def save_snapshot(self, snapshot) -> None:
        """Save VolumeSnapshot to Redis."""
        from utils.storage.snapshot import VolumeSnapshot
        key = f"{self.SNAPSHOT_KEY_PREFIX}{snapshot.namespace}:{snapshot.name}"
        data = snapshot.to_dict() if isinstance(snapshot, VolumeSnapshot) else snapshot
        self.redis.redis_client.set(key, json.dumps(data))
        
        # Add to indexes
        self.redis.redis_client.sadd(self.SNAPSHOT_INDEX, f"{snapshot.namespace}:{snapshot.name}")
        self.redis.redis_client.sadd(f"{self.SNAPSHOT_INDEX_NAMESPACE}{snapshot.namespace}", f"{snapshot.namespace}:{snapshot.name}")
        
        logger.info(f"Saved snapshot {snapshot.namespace}/{snapshot.name} to Redis")
    
    @log_to_file(logger)
    def get_snapshot(self, namespace: str, name: str):
        """Get VolumeSnapshot by namespace and name."""
        from utils.storage.snapshot import VolumeSnapshot
        key = f"{self.SNAPSHOT_KEY_PREFIX}{namespace}:{name}"
        data = self.redis.redis_client.get(key)
        if not data:
            return None
        
        try:
            snapshot_dict = json.loads(data)
            return VolumeSnapshot.from_dict(snapshot_dict)
        except Exception as e:
            logger.error(f"Failed to deserialize snapshot {namespace}/{name}: {e}")
            return None
    
    @log_to_file(logger)
    def list_snapshots(self, namespace: Optional[str] = None) -> List:
        """List all snapshots, optionally filtered by namespace."""
        from utils.storage.snapshot import VolumeSnapshot
        if namespace:
            snapshot_keys = self.redis.redis_client.smembers(f"{self.SNAPSHOT_INDEX_NAMESPACE}{namespace}")
        else:
            snapshot_keys = self.redis.redis_client.smembers(self.SNAPSHOT_INDEX)
        
        snapshots = []
        for key in snapshot_keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            parts = key_str.split(':', 1)
            if len(parts) == 2:
                ns, name = parts
                snapshot = self.get_snapshot(ns, name)
                if snapshot:
                    snapshots.append(snapshot)
        
        return snapshots
    
    @log_to_file(logger)
    def delete_snapshot(self, namespace: str, name: str) -> None:
        """Delete VolumeSnapshot from Redis."""
        key = f"{self.SNAPSHOT_KEY_PREFIX}{namespace}:{name}"
        self.redis.redis_client.delete(key)
        
        # Remove from indexes
        self.redis.redis_client.srem(self.SNAPSHOT_INDEX, f"{namespace}:{name}")
        self.redis.redis_client.srem(f"{self.SNAPSHOT_INDEX_NAMESPACE}{namespace}", f"{namespace}:{name}")
        
        logger.info(f"Deleted snapshot {namespace}/{name} from Redis")

