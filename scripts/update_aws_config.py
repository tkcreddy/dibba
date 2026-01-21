#!/usr/bin/env python3
"""
Utility script to update AWS node configuration in Redis.

This script allows manual updates of AWS node configuration outside the frontend.
The configuration is stored in Redis and takes precedence over config.json.

Usage:
    python scripts/update_aws_config.py --ami-id ami-123 --key-name KEY --security-groups sg-123 --subnet-id subnet-123
    python scripts/update_aws_config.py --all ami-123 KEY sg-123,sg-456 subnet-123 us-east-1 t3.medium
    python scripts/update_aws_config.py --view  # View current configuration
"""
import argparse
import sys
import os

# Add parent directory to path to import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utils.aws.config_helper import update_aws_node_config, get_aws_node_config
from logpkg.log_kcld import LogKCld

logger = LogKCld()


def main():
    parser = argparse.ArgumentParser(
        description='Update AWS node configuration in Redis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Update individual fields
  %(prog)s --ami-id ami-0671ebe391035cb7b --key-name NEW_KCR
  
  # Update all fields at once
  %(prog)s --all ami-0671ebe391035cb7b NEW_KCR sg-0856f84bf6929948a subnet-1dba9678 us-east-1 t3.medium
  
  # View current configuration
  %(prog)s --view
  
  # Update security groups (comma-separated)
  %(prog)s --security-groups sg-123,sg-456
        """
    )
    
    parser.add_argument('--ami-id', help='AMI ID (e.g., ami-0671ebe391035cb7b)')
    parser.add_argument('--key-name', help='Key pair name (e.g., NEW_KCR)')
    parser.add_argument('--security-groups', help='Security group IDs (comma-separated, e.g., sg-123,sg-456)')
    parser.add_argument('--subnet-id', help='Subnet ID (e.g., subnet-1dba9678)')
    parser.add_argument('--region', help='AWS region (e.g., us-east-1)')
    parser.add_argument('--instance-type', help='Instance type (e.g., t3.medium)')
    
    parser.add_argument(
        '--all',
        nargs=6,
        metavar=('AMI_ID', 'KEY_NAME', 'SECURITY_GROUPS', 'SUBNET_ID', 'REGION', 'INSTANCE_TYPE'),
        help='Update all fields at once: AMI_ID KEY_NAME SECURITY_GROUPS SUBNET_ID REGION INSTANCE_TYPE'
    )
    
    parser.add_argument('--view', action='store_true', help='View current configuration from Redis')
    
    args = parser.parse_args()
    
    # View mode
    if args.view:
        config = get_aws_node_config()
        if config:
            print("\nCurrent AWS Node Configuration (from Redis with fallback to config):")
            print("=" * 70)
            for key, value in sorted(config.items()):
                if key == 'security_group_ids' and isinstance(value, list):
                    print(f"  {key}: {', '.join(value)}")
                else:
                    print(f"  {key}: {value}")
            print("=" * 70)
        else:
            print("No AWS node configuration found in Redis or config file.")
        return
    
    # Prepare config dictionary
    config = {}
    
    if args.all:
        # Update all fields at once
        ami_id, key_name, security_groups, subnet_id, region, instance_type = args.all
        config = {
            'ami_id': ami_id,
            'key_name': key_name,
            'security_group_ids': security_groups.split(',') if security_groups else [],
            'subnet_id': subnet_id,
            'region': region,
            'instance_type': instance_type,
        }
    else:
        # Update individual fields
        if args.ami_id:
            config['ami_id'] = args.ami_id
        if args.key_name:
            config['key_name'] = args.key_name
        if args.security_groups:
            config['security_group_ids'] = [sg.strip() for sg in args.security_groups.split(',')]
        if args.subnet_id:
            config['subnet_id'] = args.subnet_id
        if args.region:
            config['region'] = args.region
        if args.instance_type:
            config['instance_type'] = args.instance_type
    
    if not config:
        parser.print_help()
        sys.exit(1)
    
    # Update configuration
    try:
        update_aws_node_config(config)
        print("\n✓ Successfully updated AWS node configuration in Redis:")
        print("=" * 70)
        for key, value in sorted(config.items()):
            if key == 'security_group_ids' and isinstance(value, list):
                print(f"  {key}: {', '.join(value)}")
            else:
                print(f"  {key}: {value}")
        print("=" * 70)
        print("\nNote: This configuration will take precedence over config.json")
    except Exception as e:
        print(f"\n✗ Error updating configuration: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

