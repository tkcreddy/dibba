#!/usr/bin/env python3
"""
update_vm_targets.py

Dynamically updates VictoriaMetrics service discovery targets file
by querying Redis for active Dibba worker hosts.

This script should be run periodically (via cron or systemd timer) to
keep VictoriaMetrics scrape targets up-to-date.
"""

import os
import json
import sys
from typing import List, Dict

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from logpkg.log_kcld import LogKCld

logger = LogKCld()

# Configuration
VM_TARGETS_FILE = os.getenv("VM_TARGETS_FILE", "/etc/victoriametrics/dibba-targets.json")
METRICS_PORT = int(os.getenv("DIBBA_METRICS_PORT", "9333"))
CLUSTER_NAME = os.getenv("DIBBA_CLUSTER_NAME", "dibba")


def get_active_hosts(store: HostPodStore) -> List[str]:
    """Get list of active hostnames from Redis."""
    try:
        hosts = store.get_all_hosts()
        active_hosts = []
        
        for host in hosts:
            hostname = host.get("hostname")
            status = host.get("status", "unknown")
            
            # Only include online hosts
            if hostname and status in ("online", "ONLINE"):
                active_hosts.append(hostname)
        
        logger.info(f"Found {len(active_hosts)} active hosts: {active_hosts}")
        return active_hosts
    except Exception as e:
        logger.error(f"Error getting active hosts: {e}", exc_info=True)
        return []


def generate_targets_config(hosts: List[str]) -> List[Dict]:
    """Generate VictoriaMetrics targets configuration."""
    targets = [f"{host}:{METRICS_PORT}" for host in hosts]
    
    return [
        {
            "targets": targets,
            "labels": {
                "cluster": CLUSTER_NAME,
                "exporter_type": "dibba-metrics",
                "job": "dibba-metrics"
            }
        }
    ]


def write_targets_file(config: List[Dict], filepath: str) -> bool:
    """Write targets configuration to file."""
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        # Write configuration
        with open(filepath, 'w') as f:
            json.dump(config, f, indent=2)
        
        logger.info(f"Successfully wrote {len(config[0]['targets'])} targets to {filepath}")
        return True
    except Exception as e:
        logger.error(f"Error writing targets file: {e}", exc_info=True)
        return False


def main():
    """Main function."""
    try:
        # Initialize Redis connection using RedisInterface
        # This uses the same connection pool and SSL settings as the rest of the system
        redis_interface = RedisInterface()
        store = HostPodStore(redis_interface)
        
        # Get active hosts
        hosts = get_active_hosts(store)
        
        if not hosts:
            logger.warning("No active hosts found, keeping existing targets file")
            return
        
        # Generate configuration
        config = generate_targets_config(hosts)
        
        # Write to file
        success = write_targets_file(config, VM_TARGETS_FILE)
        
        if success:
            print(f"Updated VictoriaMetrics targets: {len(hosts)} hosts", flush=True)
            sys.exit(0)
        else:
            print("Failed to update VictoriaMetrics targets", flush=True)
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"Fatal error: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
