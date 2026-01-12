"""
Health check helper functions for Dibba pods.

This module provides helper functions for:
- Performing liveness and readiness probes on pods
- Managing probe state in Redis
- Evaluating probe results with threshold support
- Honor all Kubernetes probe parameters: initialDelaySeconds, periodSeconds, 
  timeoutSeconds, successThreshold, failureThreshold
"""
import requests
import socket
import subprocess
import aiohttp
import asyncio
import json
from typing import Dict, Any, Optional, Tuple, List
from logpkg.log_kcld import LogKCld
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from datetime import datetime, timezone

logger = LogKCld()


def check_http_probe(http_get: Dict[str, Any], pod_ip: str, container_port: int, timeout_seconds: int = 1) -> bool:
    """Check HTTP health probe.
    
    Args:
        http_get: HTTP probe configuration with 'path' and optional 'port'
        pod_ip: Pod IP address (may contain CIDR notation like /32 - will be stripped)
        container_port: Default container port
        timeout_seconds: Timeout in seconds (from probe config, not httpGet)
        
    Returns:
        True if probe succeeds, False otherwise
    """
    try:
        # Strip CIDR notation from IP address if present (e.g., "192.168.1.1/32" -> "192.168.1.1")
        # This can happen when IPs come from etcd/Calico which stores them with CIDR notation
        if '/' in pod_ip:
            pod_ip = pod_ip.split('/')[0]
            logger.debug(f"Stripped CIDR notation from pod IP, using: {pod_ip}")
        
        path = http_get.get('path', '/')
        port = http_get.get('port', container_port)
        scheme = http_get.get('scheme', 'HTTP').upper()
        
        if scheme == 'HTTPS':
            url = f"https://{pod_ip}:{port}{path}"
        else:
            url = f"http://{pod_ip}:{port}{path}"
        
        # Use timeout from probe config (passed as parameter), fallback to httpGet if not provided
        timeout = timeout_seconds if timeout_seconds > 0 else http_get.get('timeoutSeconds', 1)
        response = requests.get(url, timeout=timeout, verify=False)
        
        # Check status code (default: 200-399 is success)
        http_headers = http_get.get('httpHeaders', [])
        expected_status = None
        for header in http_headers:
            if header.get('name', '').lower() == 'expected-status':
                expected_status = int(header.get('value', '200'))
        
        if expected_status:
            return response.status_code == expected_status
        else:
            # Default: 200-399 is success
            return 200 <= response.status_code < 400
            
    except Exception as e:
        logger.warning(f"HTTP probe failed for {pod_ip}:{port}{path}: {e}")
        return False


def check_tcp_probe(tcp_socket: Dict[str, Any], pod_ip: str, container_port: int, timeout_seconds: int = 1) -> bool:
    """Check TCP health probe.
    
    Args:
        tcp_socket: TCP probe configuration with optional 'port'
        pod_ip: Pod IP address (may contain CIDR notation like /32 - will be stripped)
        container_port: Default container port
        timeout_seconds: Timeout in seconds (from probe config, not tcpSocket)
        
    Returns:
        True if TCP connection succeeds, False otherwise
    """
    try:
        # Strip CIDR notation from IP address if present (e.g., "192.168.1.1/32" -> "192.168.1.1")
        # This can happen when IPs come from etcd/Calico which stores them with CIDR notation
        if '/' in pod_ip:
            pod_ip = pod_ip.split('/')[0]
            logger.debug(f"Stripped CIDR notation from pod IP for TCP probe, using: {pod_ip}")
        
        port = tcp_socket.get('port', container_port)
        # Use timeout from probe config (passed as parameter), fallback to tcpSocket if not provided
        timeout = timeout_seconds if timeout_seconds > 0 else tcp_socket.get('timeoutSeconds', 1)
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((pod_ip, port))
        sock.close()
        
        return result == 0
        
    except Exception as e:
        logger.warning(f"TCP probe failed for {pod_ip}:{port}: {e}")
        return False


# ============================================================================
# ASYNC VERSIONS FOR NON-BLOCKING PARALLEL EXECUTION
# ============================================================================

async def check_http_probe_async(http_get: Dict[str, Any], pod_ip: str, container_port: int, 
                                 timeout_seconds: int = 1, session: Optional[aiohttp.ClientSession] = None) -> bool:
    """Async version of HTTP health probe using aiohttp for non-blocking I/O.
    
    Args:
        http_get: HTTP probe configuration with 'path' and optional 'port'
        pod_ip: Pod IP address (may contain CIDR notation like /32 - will be stripped)
        container_port: Default container port
        timeout_seconds: Timeout in seconds (from probe config)
        session: Optional aiohttp session (creates new one if not provided)
        
    Returns:
        True if probe succeeds, False otherwise
    """
    try:
        # Strip CIDR notation from IP address if present (e.g., "192.168.1.1/32" -> "192.168.1.1")
        # This can happen when IPs come from etcd/Calico which stores them with CIDR notation
        if '/' in pod_ip:
            pod_ip = pod_ip.split('/')[0]
            logger.debug(f"Stripped CIDR notation from pod IP, using: {pod_ip}")
        
        path = http_get.get('path', '/')
        port = http_get.get('port', container_port)
        scheme = http_get.get('scheme', 'HTTP').upper()
        
        if scheme == 'HTTPS':
            url = f"https://{pod_ip}:{port}{path}"
        else:
            url = f"http://{pod_ip}:{port}{path}"
        
        # Use timeout from probe config (passed as parameter), fallback to httpGet if not provided
        timeout = timeout_seconds if timeout_seconds > 0 else http_get.get('timeoutSeconds', 1)
        timeout_obj = aiohttp.ClientTimeout(total=timeout, connect=timeout)
        
        # Use provided session or create a new one for this request
        if session is None:
            # Create a temporary session for this single request
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as temp_session:
                async with temp_session.get(url, allow_redirects=False, ssl=False) as response:
                    status_code = response.status
                    
                    # Check status code (default: 200-399 is success)
                    http_headers = http_get.get('httpHeaders', [])
                    expected_status = None
                    for header in http_headers:
                        if header.get('name', '').lower() == 'expected-status':
                            expected_status = int(header.get('value', '200'))
                    
                    if expected_status:
                        return status_code == expected_status
                    else:
                        # Default: 200-399 is success
                        return 200 <= status_code < 400
        else:
            # Use provided shared session
            async with session.get(url, allow_redirects=False, ssl=False) as response:
                status_code = response.status
                
                # Check status code (default: 200-399 is success)
                http_headers = http_get.get('httpHeaders', [])
                expected_status = None
                for header in http_headers:
                    if header.get('name', '').lower() == 'expected-status':
                        expected_status = int(header.get('value', '200'))
                
                if expected_status:
                    return status_code == expected_status
                else:
                    # Default: 200-399 is success
                    return 200 <= status_code < 400
                
    except asyncio.TimeoutError:
        logger.warning(f"HTTP probe timeout for {pod_ip}:{port}{path} after {timeout}s")
        return False
    except Exception as e:
        logger.warning(f"HTTP probe failed for {pod_ip}:{port}{path}: {e}")
        return False


async def check_tcp_probe_async(tcp_socket: Dict[str, Any], pod_ip: str, container_port: int, 
                                timeout_seconds: int = 1) -> bool:
    """Async version of TCP health probe using asyncio for non-blocking I/O.
    
    Args:
        tcp_socket: TCP probe configuration with optional 'port'
        pod_ip: Pod IP address (may contain CIDR notation like /32 - will be stripped)
        container_port: Default container port
        timeout_seconds: Timeout in seconds (from probe config)
        
    Returns:
        True if TCP connection succeeds, False otherwise
    """
    try:
        # Strip CIDR notation from IP address if present (e.g., "192.168.1.1/32" -> "192.168.1.1")
        # This can happen when IPs come from etcd/Calico which stores them with CIDR notation
        if '/' in pod_ip:
            pod_ip = pod_ip.split('/')[0]
            logger.debug(f"Stripped CIDR notation from pod IP for TCP probe, using: {pod_ip}")
        
        port = tcp_socket.get('port', container_port)
        # Use timeout from probe config (passed as parameter), fallback to tcpSocket if not provided
        timeout = timeout_seconds if timeout_seconds > 0 else tcp_socket.get('timeoutSeconds', 1)
        
        # Use asyncio to create connection asynchronously
        try:
            # Try to connect with timeout
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(pod_ip, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except asyncio.TimeoutError:
            logger.warning(f"TCP probe timeout for {pod_ip}:{port} after {timeout}s")
            return False
        except Exception as conn_error:
            logger.debug(f"TCP probe connection failed for {pod_ip}:{port}: {conn_error}")
            return False
            
    except Exception as e:
        logger.warning(f"TCP probe failed for {pod_ip}:{port}: {e}")
        return False


async def check_exec_probe_async(exec_cmd: Dict[str, Any], hostname: str, namespace: str, 
                                 pod_id: str, container_name: str, timeout_seconds: int = 1) -> bool:
    """Async version of exec health probe.
    
    This still submits a Celery task to the worker node (since we can't execute
    commands remotely), but does so asynchronously.
    
    Args:
        exec_cmd: Exec probe configuration with 'command' list
        hostname: Host where pod is running (used for queue routing)
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        timeout_seconds: Timeout in seconds (from probe config)
        
    Returns:
        True if command exits with code 0, False otherwise
    """
    try:
        command = exec_cmd.get('command', [])
        if not command:
            logger.warning(f"Exec probe has no command for pod {pod_id}")
            return False
        
        # Import here to avoid circular dependencies
        from utils.celery.queue_utils import create_host_queue_info
        from utils.celery.celery_config import celery_app
        from utils.extensions.utilities_extention import UtilitiesExtension
        from utils.ReadConfig import ReadConfig as rc
        
        # Execute command on the target worker node asynchronously
        read_config = rc()
        key = read_config.encryption_config['key']
        encode_util = UtilitiesExtension(key)
        queue_info = create_host_queue_info(hostname, encode_util)
        
        # Submit task asynchronously using asyncio
        loop = asyncio.get_event_loop()
        task_result = await loop.run_in_executor(
            None,
            lambda: celery_app.send_task(
                'containerd.exec_container_command',
                args=[namespace, pod_id, container_name, command, timeout_seconds],
                **queue_info
            ).get(timeout=timeout_seconds + 5)  # Add 5s buffer for task overhead
        )
        
        if task_result and isinstance(task_result, dict):
            exit_code = task_result.get('exit_code', -1)
            return exit_code == 0
        else:
            logger.warning(f"Exec probe task returned invalid result for pod {pod_id}: {task_result}")
            return False
            
    except asyncio.TimeoutError:
        logger.warning(f"Exec probe timeout for pod {pod_id} container {container_name} after {timeout_seconds}s")
        return False
    except Exception as e:
        logger.warning(f"Exec probe failed for pod {pod_id} container {container_name}: {e}")
        return False


async def perform_probe_async(probe_config: Dict[str, Any], pod_ip: str, container_port: int,
                             hostname: str, namespace: str, pod_id: str, container_name: str,
                             session: Optional[aiohttp.ClientSession] = None) -> bool:
    """Async version of perform_probe for non-blocking execution.
    
    Args:
        probe_config: Probe configuration (httpGet, tcpSocket, or exec) with timeoutSeconds at probe level
        pod_ip: Pod IP address
        container_port: Default container port
        hostname: Host where pod is running
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        session: Optional aiohttp session for HTTP probes (shared across calls)
        
    Returns:
        True if probe succeeds, False otherwise
    """
    # Extract timeoutSeconds from probe config (at probe level, not in httpGet/tcpSocket/exec)
    timeout_seconds = probe_config.get('timeoutSeconds', 1)
    
    if 'httpGet' in probe_config:
        http_get = probe_config['httpGet']
        return await check_http_probe_async(http_get, pod_ip, container_port, timeout_seconds, session)
    elif 'tcpSocket' in probe_config:
        tcp_socket = probe_config['tcpSocket']
        return await check_tcp_probe_async(tcp_socket, pod_ip, container_port, timeout_seconds)
    elif 'exec' in probe_config:
        exec_cmd = probe_config['exec']
        return await check_exec_probe_async(exec_cmd, hostname, namespace, pod_id, container_name, timeout_seconds)
    else:
        logger.warning(f"Unknown probe type for pod {pod_id}: {probe_config}")
        return False


# ============================================================================
# SYNC VERSIONS (kept for backward compatibility)
# ============================================================================

def check_exec_probe(exec_cmd: Dict[str, Any], hostname: str, namespace: str, pod_id: str, container_name: str, timeout_seconds: int = 1) -> bool:
    """Check exec health probe by running command in container (SYNC VERSION).
    
    This function submits a task to the worker node where the pod is running,
    since the health check worker runs on a separate node and cannot directly
    execute commands in containers on other nodes.
    
    Args:
        exec_cmd: Exec probe configuration with 'command' list
        hostname: Host where pod is running (used for queue routing)
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        timeout_seconds: Timeout in seconds (from probe config, not exec)
        
    Returns:
        True if command exits with code 0, False otherwise
    """
    try:
        command = exec_cmd.get('command', [])
        if not command:
            logger.warning(f"Exec probe has no command for pod {pod_id}")
            return False
        
        # Import here to avoid circular dependencies
        from utils.celery.tasks.containerd_tasks import exec_container_command_task
        from utils.celery.queue_utils import create_host_queue_info, submit_celery_task
        from utils.ReadConfig import ReadConfig as rc
        from utils.extensions.utilities_extention import UtilitiesExtension
        
        # Get queue configuration for the worker node
        read_config = rc()
        key = read_config.encryption_config['key']
        encode_util = UtilitiesExtension(key)
        queue_info = create_host_queue_info(hostname, encode_util)
        
        # Use timeout from probe config (passed as parameter), fallback to exec if not provided
        timeout = timeout_seconds if timeout_seconds > 0 else exec_cmd.get('timeoutSeconds', 1)
        
        # Submit task to worker node and wait for result
        # Note: This is synchronous - we wait for the result with a timeout
        try:
            async_result = exec_container_command_task.apply_async(
                args=(pod_id, container_name, command, namespace, timeout),
                **queue_info
            )
            
            # Wait for result with timeout (add buffer for network overhead)
            result = async_result.get(timeout=timeout + 5)
            
            if result and result.get('success'):
                return True
            else:
                logger.debug(f"Exec probe failed for pod {pod_id}: exit_code={result.get('exit_code')}, error={result.get('error')}")
                return False
                
        except Exception as e:
            logger.warning(f"Failed to execute exec probe task for pod {pod_id} on host {hostname}: {e}")
            return False
        
    except Exception as e:
        logger.warning(f"Exec probe failed for pod {pod_id} container {container_name}: {e}")
        return False


def get_probe_state_key(pod_id: str, probe_type: str, container_name: str) -> str:
    """Get Redis key for storing probe state.
    
    Args:
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        
    Returns:
        Redis key string
    """
    return f"health:probe:{pod_id}:{container_name}:{probe_type}"


def get_health_check_history_key(pod_id: str, probe_type: str, container_name: str) -> str:
    """Get Redis key for storing health check history (sorted set).
    
    Args:
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        
    Returns:
        Redis key string for sorted set
    """
    return f"health:history:{pod_id}:{container_name}:{probe_type}"


def record_health_check_result(redis_interface: RedisInterface, pod_id: str, probe_type: str, 
                               container_name: str, success: bool, timestamp: Optional[datetime] = None) -> None:
    """Record a health check result with timestamp in Redis sorted set.
    
    Stores health check results in a sorted set with timestamp as score.
    Automatically removes entries older than 180 seconds.
    
    Args:
        redis_interface: Redis interface
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        success: True if check succeeded, False otherwise
        timestamp: Timestamp for the check (default: current time)
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    
    # Use Unix timestamp as score for sorted set
    timestamp_score = timestamp.timestamp()
    
    # Create member value: "success" or "failure" with timestamp
    member = json.dumps({
        'success': success,
        'timestamp': timestamp.isoformat()
    })
    
    key = get_health_check_history_key(pod_id, probe_type, container_name)
    redis_client = redis_interface.redis_client
    
    try:
        # Add to sorted set with timestamp as score
        # Try modern format first: zadd(key, {member: score}) - works for redis-py >= 3.0
        # Fallback to legacy format: zadd(key, score, member) - for older redis-py versions
        try:
            added_count = redis_client.zadd(key, {member: timestamp_score})
        except TypeError:
            # Fallback to legacy format if dictionary format fails
            logger.debug(f"Dictionary format failed for zadd, trying legacy format: zadd({key}, {timestamp_score}, {member[:50]}...)")
            added_count = redis_client.zadd(key, timestamp_score, member)
        
        logger.debug(f"zadd result for key {key}: added_count={added_count}, member_length={len(member)}, score={timestamp_score}")
        
        # Verify the entry was actually added by checking if key exists and has entries
        key_exists_before = redis_client.exists(key)
        
        # Remove entries older than 180 seconds
        cutoff_time = timestamp.timestamp() - 180
        removed_count = redis_client.zremrangebyscore(key, '-inf', cutoff_time)
        logger.debug(f"Removed {removed_count} old entries (older than {cutoff_time}) from key {key}")
        
        # Set TTL on the key (190 seconds to allow some buffer)
        # Note: TTL is reset on each write, so key persists as long as health checks run
        ttl_set = redis_client.expire(key, 190)
        logger.debug(f"Set TTL on key {key}: {ttl_set} (TTL=190s)")
        
        # Get current count of entries for debugging
        total_entries = redis_client.zcard(key)
        
        # Verify the key exists and has entries AFTER the operation
        key_exists_after = redis_client.exists(key)
        
        # Double-check: Try to retrieve the entry we just added to verify it's there
        # Get entries with score around our timestamp (within 1 second tolerance)
        test_entries = redis_client.zrangebyscore(key, timestamp_score - 1, timestamp_score + 1, withscores=True)
        logger.debug(f"Verification: Found {len(test_entries)} entries with score near {timestamp_score} for key {key}")
        
        logger.info(
            f"✓ Recorded health check for {pod_id}/{container_name}/{probe_type}: "
            f"success={success}, timestamp={timestamp.isoformat()}, "
            f"total_entries={total_entries}, removed_old={removed_count}, "
            f"key={key}, key_exists_before={key_exists_before}, key_exists_after={key_exists_after}, "
            f"added_count={added_count}, verification_entries={len(test_entries)}"
        )
    except Exception as redis_error:
        logger.error(
            f"✗ Failed to record health check in Redis for {pod_id}/{container_name}/{probe_type}: {redis_error}",
            exc_info=True
        )
        # Don't raise - log the error but don't fail the health check
        # This allows health checks to continue even if history recording fails
        logger.warning(f"Continuing health check despite Redis recording failure for {pod_id}/{container_name}/{probe_type}")


def get_health_check_history(redis_interface: RedisInterface, pod_id: str, probe_type: str, 
                             container_name: str, seconds: int = 180) -> List[Dict[str, Any]]:
    """Get health check history for the last N seconds.
    
    Args:
        redis_interface: Redis interface
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        seconds: Number of seconds to look back (default: 180)
        
    Returns:
        List of health check results, each with 'success' (bool) and 'timestamp' (str)
        Results are ordered by timestamp (oldest first)
    """
    key = get_health_check_history_key(pod_id, probe_type, container_name)
    redis_client = redis_interface.redis_client
    
    # Calculate cutoff time - use 'now' at the start to be consistent
    now = datetime.now(timezone.utc)
    now_timestamp = now.timestamp()
    cutoff_time = now_timestamp - seconds
    
    logger.debug(f"Getting health check history for key: {key}, now={now_timestamp}, cutoff_time={cutoff_time} (looking back {seconds}s)")
    
    # Get all entries from cutoff_time to now (inclusive of both boundaries)
    # ZRANGEBYSCORE with withscores=True returns (member, score) tuples
    # Use 'ge' (greater than or equal) and 'le' (less than or equal) to include boundaries
    try:
        # zrangebyscore(key, min, max) - includes entries with score >= min and score <= max
        members = redis_client.zrangebyscore(key, cutoff_time, '+inf', withscores=True)
        logger.debug(f"Found {len(members)} history entries for key {key} (cutoff: {cutoff_time}, now: {now_timestamp}, window: {now_timestamp - cutoff_time:.1f}s)")
    except Exception as e:
        logger.warning(f"Failed to get health check history from Redis for key {key}: {e}")
        return []
    
    results = []
    for member, score in members:
        try:
            data = json.loads(member)
            results.append({
                'success': data.get('success', False),
                'timestamp': data.get('timestamp', datetime.fromtimestamp(score, tz=timezone.utc).isoformat())
            })
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse health check history entry: {e}")
            continue
    
    logger.debug(f"Returning {len(results)} parsed history entries for {pod_id}/{container_name}/{probe_type}")
    return results


def get_health_check_success_rate(redis_interface: RedisInterface, pod_id: str, probe_type: str, 
                                  container_name: str, seconds: int = 180) -> Dict[str, Any]:
    """Get health check success rate for the last N seconds.
    
    Args:
        redis_interface: Redis interface
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        seconds: Number of seconds to analyze (default: 180)
        
    Returns:
        Dictionary with:
        - total_checks: Total number of checks
        - successful_checks: Number of successful checks
        - failed_checks: Number of failed checks
        - success_rate: Success rate as percentage (0-100)
        - history: List of recent check results
    """
    history = get_health_check_history(redis_interface, pod_id, probe_type, container_name, seconds)
    
    total = len(history)
    successful = sum(1 for h in history if h.get('success', False))
    failed = total - successful
    
    # Calculate time span of history entries for debugging
    if history:
        timestamps = []
        for h in history:
            timestamp_str = h.get('timestamp')
            if timestamp_str:
                try:
                    ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).timestamp()
                    timestamps.append(ts)
                except Exception:
                    continue
        if timestamps:
            oldest_timestamp = min(timestamps)
            newest_timestamp = max(timestamps)
            time_span = newest_timestamp - oldest_timestamp
            now = datetime.now(timezone.utc).timestamp()
            age_of_oldest = now - oldest_timestamp
            logger.info(f"Health check history for {pod_id}/{container_name}/{probe_type}: {total} checks over {time_span:.1f}s span (oldest entry: {age_of_oldest:.1f}s ago, requested window: {seconds}s, TTL: 190s, expected for 10s period: {int(seconds/10)} checks)")
        else:
            logger.warning(f"No valid timestamps found in history for {pod_id}/{container_name}/{probe_type}")
    else:
        logger.debug(f"No health check history found for {pod_id}/{container_name}/{probe_type} (requested window: {seconds}s)")
    
    success_rate = (successful / total * 100) if total > 0 else 0.0
    
    return {
        'total_checks': total,
        'successful_checks': successful,
        'failed_checks': failed,
        'success_rate': round(success_rate, 2),
        'history': history
    }


def get_probe_state(redis_interface: RedisInterface, pod_id: str, probe_type: str, container_name: str) -> Dict[str, Any]:
    """Get current probe state from Redis.
    
    Args:
        redis_interface: Redis interface
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        
    Returns:
        Dictionary with probe state
    """
    key = get_probe_state_key(pod_id, probe_type, container_name)
    data = redis_interface.redis_client.get(key)
    if data:
        try:
            return json.loads(data)
        except:
            pass
    return {
        'consecutive_failures': 0,
        'consecutive_successes': 0,
        'last_check_time': None,
        'first_check_time': None,
        'current_status': 'unknown'
    }


def save_probe_state(redis_interface: RedisInterface, pod_id: str, probe_type: str, container_name: str, state: Dict[str, Any]):
    """Save probe state to Redis.
    
    Args:
        redis_interface: Redis interface
        pod_id: Pod ID
        probe_type: 'liveness' or 'readiness'
        container_name: Container name
        state: State dictionary to save
    """
    key = get_probe_state_key(pod_id, probe_type, container_name)
    redis_interface.redis_client.setex(key, 3600, json.dumps(state))  # 1 hour TTL


async def should_check_probe(probe_config: Dict[str, Any], probe_state: Dict[str, Any], pod_creation_time: Optional[str],
                              redis_interface: Optional['RedisInterface'] = None, pod_id: Optional[str] = None,
                              probe_type: Optional[str] = None, container_name: Optional[str] = None) -> Tuple[bool, str]:
    """Determine if probe should be checked based on initialDelaySeconds and periodSeconds (ASYNC VERSION).
    
    Uses Redis health check history as the source of truth for last_check_time to persist timing
    across worker list refreshes. Falls back to probe_state if history is unavailable.
    Redis operations are run in executor to avoid blocking the async event loop.
    
    Args:
        probe_config: Probe configuration
        probe_state: Current probe state
        pod_creation_time: Pod creation timestamp
        redis_interface: Optional Redis interface for checking history (recommended)
        pod_id: Optional pod ID for checking history
        probe_type: Optional probe type ('liveness' or 'readiness') for checking history
        container_name: Optional container name for checking history
        
    Returns:
        Tuple of (should_check: bool, reason: str)
    """
    now = datetime.now(timezone.utc)
    
    # Check initialDelaySeconds (synchronous - no blocking operations)
    initial_delay = probe_config.get('initialDelaySeconds', 0)
    if pod_creation_time:
        try:
            creation_time = datetime.fromisoformat(pod_creation_time.replace('Z', '+00:00'))
            time_since_creation = (now - creation_time).total_seconds()
            if time_since_creation < initial_delay:
                remaining = initial_delay - time_since_creation
                return False, f"Waiting for initial delay ({remaining:.1f}s remaining)"
        except Exception as e:
            logger.warning(f"Failed to parse pod creation time: {e}")
    
    # Check periodSeconds - use Redis history as source of truth for last_check_time
    # This ensures timing persists across worker list refreshes
    period_seconds = probe_config.get('periodSeconds', 10)
    last_check_time_from_history = None
    last_check_time_from_state = None
    time_since_last_check = None
    
    # PRIORITY 1: Use Redis health check history as source of truth (most reliable)
    # Run in executor to avoid blocking async event loop
    # OPTIMIZATION: Only get the most recent entry (not full history) for faster lookups
    if redis_interface and pod_id and probe_type and container_name:
        try:
            loop = asyncio.get_running_loop()
            key = get_health_check_history_key(pod_id, probe_type, container_name)
            redis_client = redis_interface.redis_client
            
            # Wrap blocking Redis call in executor (non-blocking)
            # OPTIMIZATION: Use ZREVRANGE to get only the most recent entry (score is timestamp, highest = newest)
            def _get_most_recent_sync():
                """Get only the most recent health check entry (non-blocking, optimized)."""
                try:
                    # ZREVRANGE returns entries sorted by score (highest first), limit to 1 entry
                    # This is much faster than getting full 180s history when we only need the latest timestamp
                    members = redis_client.zrevrange(key, 0, 0, withscores=True)  # Get 1 most recent entry
                    if members:
                        member_data, score = members[0]  # (member, score) tuple
                        try:
                            data = json.loads(member_data)
                            return {
                                'timestamp': data.get('timestamp', datetime.fromtimestamp(score, tz=timezone.utc).isoformat()),
                                'success': data.get('success', False)
                            }
                        except (json.JSONDecodeError, ValueError):
                            # Fallback: use score as timestamp if JSON parsing fails
                            return {
                                'timestamp': datetime.fromtimestamp(score, tz=timezone.utc).isoformat(),
                                'success': False
                            }
                    return None
                except Exception as e:
                    logger.debug(f"Failed to get most recent entry from Redis for {key}: {e}")
                    return None
            
            most_recent = await loop.run_in_executor(None, _get_most_recent_sync)
            
            if most_recent:
                last_check_timestamp_str = most_recent.get('timestamp')
                if last_check_timestamp_str:
                    try:
                        last_check_time_from_history = datetime.fromisoformat(last_check_timestamp_str.replace('Z', '+00:00'))
                        time_since_last_check = (now - last_check_time_from_history).total_seconds()
                        logger.debug(f"Using Redis history (most recent) for {pod_id}/{container_name}/{probe_type}: last_check={last_check_timestamp_str}, time_since={time_since_last_check:.2f}s")
                    except Exception as e:
                        logger.debug(f"Failed to parse timestamp from history for {pod_id}/{container_name}/{probe_type}: {e}")
        except Exception as e:
            logger.debug(f"Failed to get health check history for {pod_id}/{container_name}/{probe_type}: {e}")
    
    # PRIORITY 2: Fall back to probe_state if history unavailable (synchronous - no blocking operations)
    if time_since_last_check is None:
        last_check = probe_state.get('last_check_time')
        if last_check:
            try:
                last_check_time_from_state = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                time_since_last_check = (now - last_check_time_from_state).total_seconds()
                logger.debug(f"Using probe_state for {pod_id or 'unknown'}/{container_name or 'unknown'}/{probe_type or 'unknown'}: last_check={last_check}, time_since={time_since_last_check:.2f}s")
            except Exception as e:
                logger.warning(f"Failed to parse last check time from probe_state: {e}")
    
    if time_since_last_check is not None:
        # FOOL-PROOF LOGIC to achieve accurate periodSeconds timing:
        # Since beat runs every 5s and tasks complete in ~0.5s, we need to ensure checks happen every ~periodSeconds
        # 
        # Key insight: With a 5s beat and 10s period, we need to check when:
        # - time_since_last_check >= (periodSeconds - beat_interval) to catch the next beat
        # - This ensures: if last check at T=0, next check at T=5 or T=10 (whichever is >= threshold)
        #
        # Strategy: Use (periodSeconds - beat_interval) as threshold, with minimum of 50% of period
        # This ensures with 5s beat and 10s period:
        # - Check at T=0, last_check_time = T=0.5
        # - Beat at T=5: 4.5s < 5s threshold, skip
        # - Beat at T=10: 9.5s >= 5s threshold, check, last_check_time = T=10.5
        # - Beat at T=15: 4.5s < 5s threshold, skip
        # - Beat at T=20: 9.5s >= 5s threshold, check, last_check_time = T=20.5
        # Result: Checks at ~10s intervals (every other beat, which is correct for 10s period with 5s beat)
        
        # Use (periodSeconds - 5s) as threshold to align with 5s beat schedule
        # This ensures we check on every other beat for a 10s period (which is correct)
        # Minimum threshold is 50% of period to avoid checking too frequently
        beat_interval = 5.0  # Beat runs every 5 seconds
        threshold = max(period_seconds - beat_interval, period_seconds * 0.5)  # e.g., 5s for 10s period (50% minimum)
        
        if time_since_last_check < threshold:
            # Still waiting for the threshold to elapse - normal skip
            remaining = period_seconds - time_since_last_check
            source = "Redis history" if last_check_time_from_history else "probe_state"
            logger.debug(f"Probe check skipped: only {time_since_last_check:.2f}s since last check (from {source}), need {threshold:.1f}s (remaining: {remaining:.1f}s)")
            return False, f"Waiting for period ({remaining:.1f}s remaining)"
        elif time_since_last_check >= 1.5 * period_seconds:
            # Way overdue - catch-up mechanism: always check even if we're late
            overdue_by = time_since_last_check - period_seconds
            source = "Redis history" if last_check_time_from_history else "probe_state"
            logger.warning(f"Probe check OVERDUE: {time_since_last_check:.2f}s since last check (from {source}, expected {period_seconds}s, overdue by {overdue_by:.2f}s) - performing catch-up check")
            return True, f"Catch-up check (overdue by {overdue_by:.1f}s)"
        # else: time_since_last_check >= threshold and < 1.5 * period_seconds - normal check (fall through)
        # With 5s threshold: checks at T=10, T=20, T=30... (every ~10s with 5s beat)
    
    # Log when check is allowed
    if time_since_last_check is not None:
        source = "Redis history" if last_check_time_from_history else "probe_state" if last_check_time_from_state else "none"
        logger.debug(f"Probe check allowed: periodSeconds={period_seconds}, time_since_last_check={time_since_last_check:.2f}s (from {source})")
    else:
        logger.debug(f"Probe check allowed: periodSeconds={period_seconds}, no previous check (first check)")
    return True, "Ready to check"


def evaluate_probe_result(probe_result: bool, probe_config: Dict[str, Any], probe_state: Dict[str, Any],
                         redis_interface: Optional[RedisInterface] = None, pod_id: Optional[str] = None,
                         probe_type: Optional[str] = None, container_name: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Evaluate probe result considering successThreshold and failureThreshold.
    
    Also records the result in health check history if redis_interface is provided.
    
    Args:
        probe_result: Current probe result (True/False)
        probe_config: Probe configuration
        probe_state: Current probe state
        redis_interface: Optional Redis interface for recording history
        pod_id: Optional pod ID for recording history
        probe_type: Optional probe type ('liveness' or 'readiness') for recording history
        container_name: Optional container name for recording history
        
    Returns:
        Tuple of (final_status: str, updated_state: Dict)
    """
    success_threshold = probe_config.get('successThreshold', 1)
    failure_threshold = probe_config.get('failureThreshold', 3)
    
    now = datetime.now(timezone.utc)
    updated_state = probe_state.copy()
    updated_state['last_check_time'] = now.isoformat()
    
    logger.info(f"🔵 [EVALUATE] evaluate_probe_result called: pod_id={pod_id}, probe_type={probe_type}, container_name={container_name}, probe_result={probe_result}")
    logger.info(f"🔵 [EVALUATE] Parameters check: redis_interface={redis_interface is not None}, pod_id={pod_id}, probe_type={probe_type}, container_name={container_name}")
    
    # Record health check result in history if Redis interface is provided
    if redis_interface and pod_id and probe_type and container_name:
        logger.info(f"🔵 [EVALUATE] All parameters present - calling record_health_check_result for {pod_id}/{container_name}/{probe_type}")
        try:
            record_health_check_result(redis_interface, pod_id, probe_type, container_name, probe_result, now)
            logger.info(f"✅ [EVALUATE] Successfully recorded health check history for {pod_id}/{container_name}/{probe_type}: success={probe_result}")
        except Exception as e:
            logger.error(f"❌ [EVALUATE] Failed to record health check history for {pod_id}/{container_name}/{probe_type}: {e}", exc_info=True)
    else:
        missing = []
        if not redis_interface:
            missing.append("redis_interface")
        if not pod_id:
            missing.append("pod_id")
        if not probe_type:
            missing.append("probe_type")
        if not container_name:
            missing.append("container_name")
        logger.warning(f"⚠️ [EVALUATE] Skipping health check history recording for {pod_id}/{container_name}/{probe_type} - missing: {', '.join(missing)}")
    
    if probe_result:
        # Probe succeeded
        updated_state['consecutive_successes'] = updated_state.get('consecutive_successes', 0) + 1
        updated_state['consecutive_failures'] = 0  # Reset failure count
        
        # Check if we've reached success threshold
        if updated_state['consecutive_successes'] >= success_threshold:
            updated_state['current_status'] = 'success'
            return 'success', updated_state
        else:
            # Not enough successes yet
            updated_state['current_status'] = 'pending_success'
            return 'pending_success', updated_state
    else:
        # Probe failed
        updated_state['consecutive_failures'] = updated_state.get('consecutive_failures', 0) + 1
        updated_state['consecutive_successes'] = 0  # Reset success count
        
        # Check if we've reached failure threshold
        if updated_state['consecutive_failures'] >= failure_threshold:
            updated_state['current_status'] = 'failed'
            return 'failed', updated_state
        else:
            # Not enough failures yet
            updated_state['current_status'] = 'pending_failure'
            return 'pending_failure', updated_state


def perform_probe(probe_config: Dict[str, Any], pod_ip: str, container_port: int,
                 hostname: str, namespace: str, pod_id: str, container_name: str) -> bool:
    """Perform a health probe based on configuration.
    
    Args:
        probe_config: Probe configuration (httpGet, tcpSocket, or exec) with timeoutSeconds at probe level
        pod_ip: Pod IP address
        container_port: Default container port
        hostname: Host where pod is running
        namespace: Pod namespace
        pod_id: Pod ID
        container_name: Container name
        
    Returns:
        True if probe succeeds, False otherwise
    """
    # Get timeoutSeconds from probe config level (not from httpGet/tcpSocket/exec)
    timeout_seconds = probe_config.get('timeoutSeconds', 1)
    
    if 'httpGet' in probe_config:
        return check_http_probe(probe_config['httpGet'], pod_ip, container_port, timeout_seconds)
    elif 'tcpSocket' in probe_config:
        return check_tcp_probe(probe_config['tcpSocket'], pod_ip, container_port, timeout_seconds)
    elif 'exec' in probe_config:
        return check_exec_probe(probe_config['exec'], hostname, namespace, pod_id, container_name, timeout_seconds)
    else:
        logger.warning(f"No valid probe type found in probe config: {probe_config}")
        return False


def update_pod_health_status(host_pod_store: HostPodStore, pod_id: str, health_results: Dict[str, Any]):
    """Update pod health status in Redis.
    
    Args:
        host_pod_store: HostPodStore instance
        pod_id: Pod ID
        health_results: Health check results
    """
    try:
        pod = host_pod_store.get_pod(pod_id)
        if not pod:
            return
        
        # Update pod with health check results
        pod['health_checks'] = {
            'liveness': health_results.get('liveness', {}),
            'readiness': health_results.get('readiness', {}),
            'last_check': health_results.get('timestamp')
        }
        
        # Determine overall pod health status
        # Note: 'pending_success' and 'pending_failure' mean we haven't reached thresholds yet
        liveness_status = health_results.get('liveness', {}).get('status', 'unknown')
        readiness_status = health_results.get('readiness', {}).get('status', 'unknown')
        
        # Update health_status based on health checks (do NOT modify pod status)
        # Pod status should reflect actual container state from containerd, not health check results
        # Health checks only update health_status to indicate health probe results
        if liveness_status == 'failed':
            # Liveness has failed - pod is unhealthy but status remains as-is (from containerd)
            pod['health_status'] = 'unhealthy'
        elif liveness_status == 'pending_failure':
            # Liveness is failing but hasn't reached threshold yet
            pod['health_status'] = 'degraded'
        elif readiness_status == 'success':
            # Readiness has succeeded
            pod['health_status'] = 'ready'
        elif readiness_status == 'failed':
            # Readiness has failed - pod is not ready but may still be running
            pod['health_status'] = 'not_ready'
        elif readiness_status == 'pending_success':
            # Readiness is succeeding but hasn't reached threshold yet
            pod['health_status'] = 'starting'
        else:
            pod['health_status'] = 'unknown'
        
        # Save updated pod with health check results
        # Note: save_pod reads existing data and merges, so we preserve all existing pod data
        # This ensures we don't overwrite data being updated by host_pod_sync operations
        # IMPORTANT: Do NOT overwrite pod status - preserve the actual status from containerd
        # Only update health-related metadata (health_status, health_checks)
        original_status = pod.get('status')  # Preserve original status from containerd/host_pod_sync
        host_pod_store.save_pod(
            pod_id=pod_id,
            hostname=pod.get('hostname'),
            namespace=pod.get('namespace'),
            containers=pod.get('containers', []),  # Preserve existing containers
            pause_container=pod.get('pause_container', {}),  # Preserve existing pause container
            labels=pod.get('labels', {}),  # Preserve existing labels
            creation_time=pod.get('creation_time'),  # Preserve creation time
            startup_time=pod.get('startup_time'),  # Preserve startup time
            ip_address=pod.get('ip_address'),  # Preserve IP address
            cni_network=pod.get('cni_network'),  # Preserve CNI network
            status=original_status or 'running'  # Preserve original status, don't let health checks overwrite it
        )
        
        # Update health_checks and health_status in the pod data
        # save_pod merges data, but health_checks is not a standard field in save_pod signature
        # So we need to update it separately. This is safe because:
        # 1. We read existing pod data first
        # 2. We only update health-related fields, preserving all other data
        # 3. This doesn't interfere with host_pod_sync which updates different fields
        try:
            from utils.redis.host_pod_store import RedisKeyPatterns
            pod_key = RedisKeyPatterns.POD_DATA.format(pod_id=pod_id)
            # Access Redis through the host_pod_store's redis interface
            redis_interface = host_pod_store.redis
            existing_data = redis_interface.hget(pod_key, "data")
            if existing_data:
                pod_data = json.loads(existing_data)
                # Update health check fields without overwriting other data
                pod_data['health_checks'] = {
                    'liveness': health_results.get('liveness', {}),
                    'readiness': health_results.get('readiness', {}),
                    'last_check': health_results.get('timestamp')
                }
                pod_data['health_status'] = pod.get('health_status', 'unknown')
                # Save back (this is safe - we're only adding/updating health fields)
                # This doesn't interfere with host_pod_sync because it uses save_pod which also merges
                redis_interface.hset(pod_key, "data", json.dumps(pod_data))
        except Exception as e:
            logger.warning(f"Failed to update health_checks for pod {pod_id}: {e}")
        
        logger.info(f"Updated health status for pod {pod_id}: liveness={liveness_status}, readiness={readiness_status}")
        
    except Exception as e:
        logger.error(f"Failed to update pod health status for {pod_id}: {e}", exc_info=True)

