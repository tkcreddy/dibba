#!/usr/bin/env python3
"""
Script to check health check history stored in Redis.

This script queries Redis to see how many health check entries are actually
stored for a given pod and container, showing the actual data in Redis.
"""
import sys
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# Add parent directory to path to import utils
sys.path.insert(0, '/opt/dibba')

try:
    from utils.redis.redis_interface import RedisInterface
    from utils.healthcheck.health_check_helpers import get_health_check_history_key
    from utils.ReadConfig import ReadConfig as rc
except ImportError:
    # Try relative import if running from project root
    sys.path.insert(0, '.')
    from utils.redis.redis_interface import RedisInterface
    from utils.healthcheck.health_check_helpers import get_health_check_history_key
    from utils.ReadConfig import ReadConfig as rc


def format_timestamp(ts: float) -> str:
    """Format Unix timestamp to readable string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def check_health_history(pod_id: str, container_name: str, probe_type: str = 'liveness', seconds: int = 180):
    """Check health check history for a specific pod/container/probe.
    
    Args:
        pod_id: Pod ID to check
        container_name: Container name
        probe_type: 'liveness' or 'readiness'
        seconds: Number of seconds to look back (default: 180)
    """
    print(f"\n{'='*80}")
    print(f"Health Check History Query")
    print(f"{'='*80}")
    print(f"Pod ID: {pod_id}")
    print(f"Container: {container_name}")
    print(f"Probe Type: {probe_type}")
    print(f"Window: {seconds} seconds")
    print(f"{'='*80}\n")
    
    try:
        # Initialize Redis interface
        redis_interface = RedisInterface()
        redis_client = redis_interface.redis_client
        
        # Get the Redis key
        key = get_health_check_history_key(pod_id, probe_type, container_name)
        print(f"Redis Key: {key}")
        
        # Check if key exists
        exists = redis_client.exists(key)
        print(f"Key exists: {exists}")
        
        if not exists:
            print("\n⚠️  No health check history found in Redis for this key.")
            print("This could mean:")
            print("  - Health checks haven't been performed yet")
            print("  - The pod/container/probe combination is incorrect")
            print("  - The TTL has expired (key was deleted)")
            return
        
        # Get TTL
        ttl = redis_client.ttl(key)
        print(f"TTL: {ttl} seconds ({'expires soon' if ttl > 0 and ttl < 60 else 'active' if ttl > 0 else 'no expiration'})")
        
        # Get all entries in the sorted set (without time filter first)
        all_members = redis_client.zrange(key, 0, -1, withscores=True)
        print(f"\nTotal entries in sorted set: {len(all_members)}")
        
        if not all_members:
            print("\n⚠️  Sorted set exists but is empty.")
            return
        
        # Calculate cutoff time
        now = datetime.now(timezone.utc)
        now_timestamp = now.timestamp()
        cutoff_time = now_timestamp - seconds
        
        print(f"\nCurrent time: {format_timestamp(now_timestamp)}")
        print(f"Cutoff time (now - {seconds}s): {format_timestamp(cutoff_time)}")
        
        # Filter entries within the window
        entries_in_window = []
        entries_outside_window = []
        
        for member, score in all_members:
            try:
                data = json.loads(member)
                entry = {
                    'success': data.get('success', False),
                    'timestamp_str': data.get('timestamp', format_timestamp(score)),
                    'score': score,
                    'age': now_timestamp - score
                }
                if score >= cutoff_time:
                    entries_in_window.append(entry)
                else:
                    entries_outside_window.append(entry)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"⚠️  Failed to parse entry: {e}")
                continue
        
        print(f"\n📊 Summary:")
        print(f"  Entries in {seconds}s window: {len(entries_in_window)}")
        print(f"  Entries outside window: {len(entries_outside_window)}")
        
        if entries_in_window:
            successful = sum(1 for e in entries_in_window if e['success'])
            failed = len(entries_in_window) - successful
            
            oldest = min(e['score'] for e in entries_in_window)
            newest = max(e['score'] for e in entries_in_window)
            time_span = newest - oldest
            
            print(f"\n✅ Checks in window:")
            print(f"  Successful: {successful}")
            print(f"  Failed: {failed}")
            print(f"  Total: {len(entries_in_window)}")
            print(f"  Success rate: {(successful/len(entries_in_window)*100):.1f}%")
            # Sort entries by score (time) for analysis - do this FIRST before using sorted_entries
            sorted_entries = sorted(entries_in_window, key=lambda x: x['score'])
            
            print(f"\n⏱️  Time span of entries in window:")
            oldest_age = max(e['age'] for e in entries_in_window)  # Oldest = highest age (most time ago)
            newest_age = min(e['age'] for e in entries_in_window)  # Newest = lowest age (least time ago)
            print(f"  Oldest: {format_timestamp(oldest)} ({oldest_age:.1f}s ago)")
            print(f"  Newest: {format_timestamp(newest)} ({newest_age:.1f}s ago)")
            print(f"  Span: {time_span:.1f} seconds")
            print(f"  Window coverage: {time_span/seconds*100:.1f}% of {seconds}s window")
            print(f"  Expected checks (at 10s period): {int(seconds/10)}")
            print(f"  Actual checks: {len(entries_in_window)}")
            print(f"  Missing checks: {int(seconds/10) - len(entries_in_window)}")
            
            # Calculate gaps
            gaps = []
            for i in range(len(sorted_entries) - 1):
                gap = sorted_entries[i+1]['score'] - sorted_entries[i]['score']
                if gap > 12:  # More than 20% over expected 10s
                    gaps.append(gap)
            if gaps:
                print(f"  Gaps detected: {len(gaps)} intervals > 12s (avg gap: {sum(gaps)/len(gaps):.1f}s)")
            
            # Show all entries in detail with gap analysis
            print(f"\n📋 Detailed entries (in window, sorted by time):")
            previous_score = None
            for i, entry in enumerate(sorted_entries, 1):
                status = "✓" if entry['success'] else "✗"
                gap_info = ""
                if previous_score is not None:
                    gap = entry['score'] - previous_score
                    expected_period = 10  # Default periodSeconds
                    if gap > expected_period * 1.5:  # More than 50% over expected
                        missing_checks = int(gap / expected_period) - 1
                        gap_info = f" ⚠️  GAP: {gap:.1f}s (expected ~{expected_period}s, ~{missing_checks} missing check{'s' if missing_checks != 1 else ''})"
                print(f"  {i:2d}. {status} {entry['timestamp_str']} (age: {entry['age']:.1f}s, score: {entry['score']:.3f}){gap_info}")
                previous_score = entry['score']
        else:
            print("\n⚠️  No entries found within the {seconds}-second window.")
            if entries_outside_window:
                oldest_outside = min(e['age'] for e in entries_outside_window)
                print(f"  Oldest entry outside window: {oldest_outside:.1f} seconds ago (too old)")
        
        # Check for readiness history too (if checking liveness)
        if probe_type == 'liveness':
            readiness_key = get_health_check_history_key(pod_id, 'readiness', container_name)
            readiness_exists = redis_client.exists(readiness_key)
            if readiness_exists:
                readiness_members = redis_client.zrange(readiness_key, 0, -1, withscores=True)
                readiness_in_window = []
                for m, s in readiness_members:
                    if s >= cutoff_time:
                        try:
                            data = json.loads(m)
                            readiness_in_window.append(data)
                        except (json.JSONDecodeError, ValueError):
                            continue
                if readiness_in_window:
                    print(f"\n📋 Readiness checks in window: {len(readiness_in_window)}")
                    readiness_successful = sum(1 for d in readiness_in_window if d.get('success', False))
                    print(f"  Successful: {readiness_successful}, Failed: {len(readiness_in_window) - readiness_successful}")
        
    except Exception as e:
        print(f"\n❌ Error querying Redis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def list_all_health_check_keys():
    """List all health check history keys in Redis."""
    try:
        redis_interface = RedisInterface()
        redis_client = redis_interface.redis_client
        
        # Health check keys follow pattern: health:history:{pod_id}:{container_name}:{probe_type}
        pattern = "health:history:*"
        keys = redis_client.keys(pattern)
        
        print(f"\n{'='*80}")
        print(f"All Health Check History Keys in Redis")
        print(f"{'='*80}")
        print(f"Found {len(keys)} keys matching pattern: {pattern}\n")
        
        if not keys:
            print("No health check history keys found.")
            return
        
        # Group by pod_id for better readability
        # Key format: health:history:{pod_id}:{container_name}:{probe_type}
        pods = {}
        for key in sorted(keys):
            parts = key.split(':')
            if len(parts) >= 5:
                pod_id = parts[2]
                container_name = parts[3]
                probe_type = parts[4]
                
                if pod_id not in pods:
                    pods[pod_id] = []
                pods[pod_id].append((key, probe_type, container_name))
        
        for pod_id, entries in pods.items():
            print(f"Pod: {pod_id}")
            for key, probe_type, container_name in entries:
                count = redis_client.zcard(key)
                ttl = redis_client.ttl(key)
                print(f"  {probe_type:10s} / {container_name:20s} -> {count:3d} entries (TTL: {ttl}s)")
            print()
            
    except Exception as e:
        print(f"\n❌ Error listing keys: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 check_health_history.py <pod_id> <container_name> [probe_type] [seconds]")
        print("  python3 check_health_history.py --list-all")
        print("\nExamples:")
        print("  python3 check_health_history.py b2a43ea1dc194faa simple-api liveness 180")
        print("  python3 check_health_history.py b2a43ea1dc194faa simple-api readiness 180")
        print("  python3 check_health_history.py --list-all")
        sys.exit(1)
    
    if sys.argv[1] == '--list-all':
        list_all_health_check_keys()
    else:
        pod_id = sys.argv[1]
        container_name = sys.argv[2] if len(sys.argv) > 2 else 'simple-api'
        probe_type = sys.argv[3] if len(sys.argv) > 3 else 'liveness'
        seconds = int(sys.argv[4]) if len(sys.argv) > 4 else 180
        
        check_health_history(pod_id, container_name, probe_type, seconds)

