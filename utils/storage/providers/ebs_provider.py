"""
AWS EBS volume provider.
"""
from typing import Dict, Any, Optional
from logpkg.log_kcld import LogKCld, log_to_file
from utils.storage.providers.base_provider import VolumeProvider
from utils.ReadConfig import ReadConfig
import boto3
import os
import subprocess

logger = LogKCld()


class EBSVolumeProvider(VolumeProvider):
    """Provider for AWS EBS volumes."""
    
    DEFAULT_MOUNT_BASE = "/var/lib/dibba/ebs"
    DEFAULT_FS_TYPE = "ext4"
    
    @log_to_file(logger)
    def __init__(self):
        """Initialize EBS provider."""
        try:
            config = ReadConfig()
            aws_config = config.aws_config
            self.aws_access_key_id = aws_config.get('aws_access_key_id')
            self.aws_secret_access_key = aws_config.get('aws_secret_access_key')
            self.region = aws_config.get('region', 'us-east-1')
            
            if not self.aws_access_key_id or not self.aws_secret_access_key:
                raise ValueError("AWS credentials not configured")
            
            self.ec2_client = boto3.client(
                'ec2',
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.region
            )
            
            # Ensure mount base exists
            os.makedirs(self.DEFAULT_MOUNT_BASE, mode=0o755, exist_ok=True)
            logger.info(f"EBS provider initialized for region: {self.region}")
        except Exception as e:
            logger.error(f"Failed to initialize EBS provider: {e}", exc_info=True)
            raise
    
    @log_to_file(logger)
    def _parse_size_gb(self, capacity: str) -> int:
        """Parse capacity string to GB."""
        capacity = capacity.strip().upper()
        if capacity.endswith('GI'):
            return int(capacity[:-2])
        elif capacity.endswith('MI'):
            return max(1, int(capacity[:-2]) // 1024)  # Round up to at least 1GB
        elif capacity.endswith('TI'):
            return int(capacity[:-2]) * 1024
        else:
            # Assume GB
            return int(capacity) if capacity.isdigit() else 1
    
    @log_to_file(logger)
    def create_volume(
        self,
        name: str,
        capacity: str,
        parameters: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Create an EBS volume."""
        try:
            volume_type = parameters.get('type', 'gp3')
            encrypted = parameters.get('encrypted', False)
            iops = parameters.get('iops')  # For io1/io2
            throughput = parameters.get('throughput')  # For gp3
            
            size_gb = self._parse_size_gb(capacity)
            
            # Create volume request
            create_params = {
                'Size': size_gb,
                'VolumeType': volume_type,
                'Encrypted': encrypted,
                'TagSpecifications': [{
                    'ResourceType': 'volume',
                    'Tags': [
                        {'Key': 'Name', 'Value': name},
                        {'Key': 'ManagedBy', 'Value': 'dibba'},
                    ]
                }]
            }
            
            if iops:
                create_params['Iops'] = iops
            if throughput:
                create_params['Throughput'] = throughput
            
            # Create volume
            response = self.ec2_client.create_volume(**create_params)
            volume_id = response['VolumeId']
            
            logger.info(f"Created EBS volume {volume_id} ({size_gb}GB, {volume_type})")
            
            return {
                'aws_volume_id': volume_id,
                'mount_path': os.path.join(self.DEFAULT_MOUNT_BASE, name),
                'metadata': {
                    'capacity': capacity,
                    'size_gb': size_gb,
                    'volume_type': volume_type,
                    'encrypted': encrypted,
                    'region': self.region,
                    'created_by': 'dibba-ebs-provider'
                }
            }
        except Exception as e:
            logger.error(f"Failed to create EBS volume {name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def _get_instance_id_from_node_name(self, node_name: str) -> Optional[str]:
        """Get EC2 instance ID from node name (hostname).
        
        This is a simplified implementation. In production, you might want to:
        - Query EC2 instances by tag
        - Use instance metadata service
        - Store node-to-instance mapping in Redis
        """
        try:
            # Try to get instance ID from node name
            # This assumes node_name is the instance ID or private DNS name
            response = self.ec2_client.describe_instances(
                Filters=[
                    {'Name': 'private-dns-name', 'Values': [node_name]},
                    {'Name': 'instance-state-name', 'Values': ['running']}
                ]
            )
            
            for reservation in response.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    return instance['InstanceId']
            
            # If not found, try treating node_name as instance ID
            try:
                response = self.ec2_client.describe_instances(InstanceIds=[node_name])
                if response.get('Reservations'):
                    return node_name
            except:
                pass
            
            logger.warning(f"Could not find EC2 instance for node {node_name}")
            return None
        except Exception as e:
            logger.error(f"Failed to get instance ID for node {node_name}: {e}")
            return None
    
    @log_to_file(logger)
    def attach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Attach EBS volume to a node."""
        try:
            # Get instance ID
            instance_id = self._get_instance_id_from_node_name(node_name)
            if not instance_id:
                logger.error(f"Cannot attach volume {volume_id}: instance not found for node {node_name}")
                return None
            
            # Find available device name
            device_name = self._find_available_device(instance_id)
            if not device_name:
                logger.error(f"No available device on instance {instance_id}")
                return None
            
            # Attach volume
            self.ec2_client.attach_volume(
                VolumeId=volume_id,
                InstanceId=instance_id,
                Device=device_name
            )
            
            # Wait for attachment (simplified - in production use waiter)
            import time
            time.sleep(5)  # Give it time to attach
            
            # Format and mount (this would typically be done on the node)
            # For now, we'll return the device path
            mount_path = mount_path or os.path.join(self.DEFAULT_MOUNT_BASE, volume_id)
            
            logger.info(f"Attached EBS volume {volume_id} to {instance_id} as {device_name}")
            
            return {
                'mount_path': mount_path,
                'device': device_name,
                'instance_id': instance_id
            }
        except Exception as e:
            logger.error(f"Failed to attach EBS volume {volume_id} to node {node_name}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def _find_available_device(self, instance_id: str) -> Optional[str]:
        """Find an available device name on the instance."""
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            if not response.get('Reservations'):
                return None
            
            instance = response['Reservations'][0]['Instances'][0]
            block_devices = instance.get('BlockDeviceMappings', [])
            used_devices = {bd['DeviceName'] for bd in block_devices}
            
            # Try common device names
            for device in ['/dev/xvdf', '/dev/xvdg', '/dev/xvdh', '/dev/xvdi', '/dev/xvdj']:
                if device not in used_devices:
                    return device
            
            logger.warning(f"No available device found on instance {instance_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to find available device: {e}")
            return None
    
    @log_to_file(logger)
    def detach_volume(
        self,
        volume_id: str,
        node_name: str,
        mount_path: Optional[str] = None
    ) -> bool:
        """Detach EBS volume from a node."""
        try:
            instance_id = self._get_instance_id_from_node_name(node_name)
            if not instance_id:
                logger.warning(f"Instance not found for node {node_name}, attempting force detach")
                instance_id = None
            
            if instance_id:
                self.ec2_client.detach_volume(
                    VolumeId=volume_id,
                    InstanceId=instance_id,
                    Force=False
                )
            else:
                # Force detach if instance not found
                self.ec2_client.detach_volume(
                    VolumeId=volume_id,
                    Force=True
                )
            
            logger.info(f"Detached EBS volume {volume_id} from node {node_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to detach EBS volume {volume_id}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    def delete_volume(
        self,
        volume_id: str,
        node_name: Optional[str] = None
    ) -> bool:
        """Delete an EBS volume."""
        try:
            # First detach if attached
            if node_name:
                self.detach_volume(volume_id, node_name)
            
            # Delete volume
            self.ec2_client.delete_volume(VolumeId=volume_id)
            logger.info(f"Deleted EBS volume {volume_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete EBS volume {volume_id}: {e}", exc_info=True)
            return False
    
    @log_to_file(logger)
    def create_snapshot(
        self,
        volume_id: str,
        snapshot_name: str,
        node_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Create an EBS snapshot."""
        try:
            response = self.ec2_client.create_snapshot(
                VolumeId=volume_id,
                Description=f"Snapshot created by Dibba: {snapshot_name}",
                TagSpecifications=[{
                    'ResourceType': 'snapshot',
                    'Tags': [
                        {'Key': 'Name', 'Value': snapshot_name},
                        {'Key': 'ManagedBy', 'Value': 'dibba'},
                    ]
                }]
            )
            
            snapshot_id = response['SnapshotId']
            logger.info(f"Created EBS snapshot {snapshot_id} for volume {volume_id}")
            
            # Wait for snapshot to complete (simplified - in production use waiter)
            import time
            time.sleep(10)  # Give it time to start
            
            return {
                'aws_snapshot_id': snapshot_id,
                'ready': True,
                'metadata': {
                    'volume_id': volume_id,
                    'state': response.get('State', 'pending')
                }
            }
        except Exception as e:
            logger.error(f"Failed to create EBS snapshot for volume {volume_id}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def restore_from_snapshot(
        self,
        snapshot_id: str,
        volume_name: str,
        capacity: str,
        parameters: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Restore an EBS volume from a snapshot."""
        try:
            # Get snapshot info
            response = self.ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
            if not response.get('Snapshots'):
                logger.error(f"Snapshot {snapshot_id} not found")
                return None
            
            snapshot = response['Snapshots'][0]
            volume_type = parameters.get('type', 'gp3')
            
            # Create volume from snapshot
            create_params = {
                'SnapshotId': snapshot_id,
                'VolumeType': volume_type,
                'TagSpecifications': [{
                    'ResourceType': 'volume',
                    'Tags': [
                        {'Key': 'Name', 'Value': volume_name},
                        {'Key': 'ManagedBy', 'Value': 'dibba'},
                        {'Key': 'RestoredFrom', 'Value': snapshot_id},
                    ]
                }]
            }
            
            if parameters.get('iops'):
                create_params['Iops'] = parameters['iops']
            if parameters.get('throughput'):
                create_params['Throughput'] = parameters['throughput']
            
            response = self.ec2_client.create_volume(**create_params)
            volume_id = response['VolumeId']
            
            logger.info(f"Restored EBS volume {volume_id} from snapshot {snapshot_id}")
            
            return {
                'aws_volume_id': volume_id,
                'mount_path': os.path.join(self.DEFAULT_MOUNT_BASE, volume_name),
                'metadata': {
                    'restored_from_snapshot': snapshot_id,
                    'volume_type': volume_type
                }
            }
        except Exception as e:
            logger.error(f"Failed to restore volume from snapshot {snapshot_id}: {e}", exc_info=True)
            return None
    
    @log_to_file(logger)
    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete an EBS snapshot."""
        try:
            self.ec2_client.delete_snapshot(SnapshotId=snapshot_id)
            logger.info(f"Deleted EBS snapshot {snapshot_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete EBS snapshot {snapshot_id}: {e}", exc_info=True)
            return False

