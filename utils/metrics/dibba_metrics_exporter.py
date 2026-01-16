#!/usr/bin/env python3
"""
dibba_metrics_exporter.py

Node-local Dibba container metrics exporter for VictoriaMetrics.

Supports two modes:
  1. Prometheus scrape mode (default): Exposes HTTP endpoint for VictoriaMetrics to scrape
  2. Push mode: Pushes metrics directly to VictoriaMetrics via remote_write API

Uses HostPodStore (Redis-backed) to discover:
  - Hosts
  - Pods on this host
  - Containers inside each pod (with PID and container_id)

Exports Prometheus-compatible metrics:
  - dibba_container_cpu_seconds_total
  - dibba_container_memory_usage_bytes
  - dibba_container_info
  - dibba_pod_info
  - dibba_host_info
  - dibba_pod_status

Run one instance on *each* Dibba worker host.
"""

import os
import sys
import time
import socket
import json
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple, Optional, List
from datetime import datetime, timezone

# Add project root to Python path for imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psutil
import requests
from prometheus_client import start_http_server, Gauge, Counter, Histogram, generate_latest, REGISTRY
from prometheus_client.core import CollectorRegistry, GaugeMetricFamily, CounterMetricFamily

# Adjust these imports to your project layout if needed
from utils.redis.redis_interface import RedisInterface
from utils.redis.host_pod_store import HostPodStore
from logpkg.log_kcld import LogKCld, log_to_file

logger = LogKCld()


# -----------------------------
# Config
# -----------------------------

DIBBA_CLUSTER = os.getenv("DIBBA_CLUSTER_NAME", "dibba")
EXPORTER_PORT = int(os.getenv("DIBBA_METRICS_PORT", "9333"))
SCRAPE_INTERVAL_SECONDS = int(os.getenv("DIBBA_SCRAPE_INTERVAL", "5"))

# VictoriaMetrics configuration
VMODE_PUSH = os.getenv("DIBBA_METRICS_MODE", "scrape").lower() == "push"
VM_REMOTE_WRITE_URL = os.getenv("VICTORIAMETRICS_REMOTE_WRITE_URL", "http://localhost:8428/api/v1/write")
VM_BATCH_SIZE = int(os.getenv("VICTORIAMETRICS_BATCH_SIZE", "1000"))
VM_PUSH_INTERVAL = int(os.getenv("VICTORIAMETRICS_PUSH_INTERVAL", "15"))
VM_TIMEOUT = int(os.getenv("VICTORIAMETRICS_TIMEOUT", "10"))

HOSTNAME = socket.gethostname()


# -----------------------------
# Data model we export
# -----------------------------

@dataclass
class ContainerRecord:
    cluster: str
    namespace: str
    pod: str          # pod_name (or pod_id fallback)
    container: str    # logical container name
    container_id: str # containerd/task id
    pid: int
    node: str         # host node name
    app_name: Optional[str] = None
    owner_team: Optional[str] = None

    @property
    def labels(self) -> Dict[str, str]:
        labels = {
            "cluster": self.cluster,
            "namespace": self.namespace,
            "pod": self.pod,
            "container": self.container,
            "container_id": self.container_id,
            "node": self.node,
        }
        if self.app_name:
            labels["app"] = self.app_name
        if self.owner_team:
            labels["owner"] = self.owner_team
        return labels


@dataclass
class PodRecord:
    cluster: str
    namespace: str
    pod: str
    pod_id: str
    node: str
    status: str
    ip_address: Optional[str] = None
    app_name: Optional[str] = None
    owner_team: Optional[str] = None

    @property
    def labels(self) -> Dict[str, str]:
        labels = {
            "cluster": self.cluster,
            "namespace": self.namespace,
            "pod": self.pod,
            "pod_id": self.pod_id,
            "node": self.node,
            "status": self.status,
        }
        if self.ip_address:
            labels["ip"] = self.ip_address
        if self.app_name:
            labels["app"] = self.app_name
        if self.owner_team:
            labels["owner"] = self.owner_team
        return labels


# -----------------------------
# Prometheus metrics
# -----------------------------

CONTAINER_CPU = Gauge(
    "dibba_container_cpu_seconds_total",
    "Cumulative CPU time consumed by Dibba container (user+system seconds)",
    ["cluster", "namespace", "pod", "container", "container_id", "node", "app", "owner"],
)

CONTAINER_MEM = Gauge(
    "dibba_container_memory_usage_bytes",
    "Current RSS memory usage of Dibba container in bytes",
    ["cluster", "namespace", "pod", "container", "container_id", "node", "app", "owner"],
)

CONTAINER_INFO = Gauge(
    "dibba_container_info",
    "Static metadata about Dibba container (value is always 1)",
    ["cluster", "namespace", "pod", "container", "container_id", "node", "app", "owner"],
)

POD_INFO = Gauge(
    "dibba_pod_info",
    "Static metadata about Dibba pod (value is always 1)",
    ["cluster", "namespace", "pod", "pod_id", "node", "status", "ip", "app", "owner"],
)

POD_STATUS = Gauge(
    "dibba_pod_status",
    "Pod status (1=running, 0=stopped/failed/unknown)",
    ["cluster", "namespace", "pod", "pod_id", "node", "status", "app", "owner"],
)

HOST_INFO = Gauge(
    "dibba_host_info",
    "Static metadata about Dibba host (value is always 1)",
    ["cluster", "node", "ip_address", "status"],
)

SCRAPE_ERRORS = Counter(
    "dibba_metrics_scrape_errors_total",
    "Total number of scrape errors",
    ["node"],
)

PUSH_ERRORS = Counter(
    "dibba_metrics_push_errors_total",
    "Total number of push errors to VictoriaMetrics",
    ["node"],
)

SCRAPE_DURATION = Histogram(
    "dibba_metrics_scrape_duration_seconds",
    "Time spent scraping metrics",
    ["node"],
)


# -----------------------------
# HostPodStore client wrapper
# -----------------------------

class HostPodStoreClient:
    """
    Thin wrapper over your HostPodStore so the exporter code stays clean.

    We:
      - Use get_pods_by_host(hostname)
      - Skip pause_container
      - Iterate each entry in pod["containers"]
        expecting at minimum:
          - name / container name
          - pid   (int or string)
          - container_id / id  (containerd id)
    """

    def __init__(self):
        try:
            # Use RedisInterface which reads config from ReadConfig
            # This ensures we use the same connection pool and SSL settings as the rest of the system
            self.redis_interface = RedisInterface()
            self.store = HostPodStore(self.redis_interface)
        except Exception as e:
            logger.error(f"Failed to initialize HostPodStoreClient: {e}", exc_info=True)
            raise

    def iter_pods_for_host(self, host: str):
        """Yield pod dicts for a given host."""
        try:
            pods = self.store.get_pods_by_host(host)
            for pod in pods or []:
                # Defensive: only yield if hostname matches
                if pod.get("hostname") == host:
                    yield pod
        except Exception as e:
            logger.error(f"Error iterating pods for host {host}: {e}", exc_info=True)

    def iter_containers_for_host(self, host: str) -> Iterable[ContainerRecord]:
        """Yield ContainerRecord objects for all *application* containers on this host."""
        for pod in self.iter_pods_for_host(host):
            namespace = pod.get("namespace", "default")
            pod_name = pod.get("pod_name") or pod.get("pod_id") or "unknown-pod"
            pod_id = pod.get("pod_id") or pod_name
            node = pod.get("hostname") or host

            # Extract labels for app_name and owner_team
            labels = pod.get("labels", {})
            app_name = labels.get("app") or labels.get("application") or labels.get("dibba_app")
            owner_team = labels.get("owner") or labels.get("dibba_owner")

            # Application containers list
            containers = pod.get("containers") or []

            for c in containers:
                if not isinstance(c, dict):
                    continue

                # Try to extract PID
                pid_val = (
                    c.get("pid")
                    or c.get("task_pid")
                    or c.get("process_pid")
                )
                try:
                    pid = int(pid_val)
                except (TypeError, ValueError):
                    continue  # skip containers without valid PID

                # Container logical name
                container_name = (
                    c.get("name")
                    or c.get("container_name")
                    or c.get("id")
                    or c.get("container_id")
                    or f"{pod_name}-unknown"
                )

                # Container ID (prefer explicit container_id)
                container_id = (
                    c.get("container_id")
                    or c.get("id")
                    or container_name
                )

                yield ContainerRecord(
                    cluster=DIBBA_CLUSTER,
                    namespace=namespace,
                    pod=pod_name,
                    container=container_name,
                    container_id=container_id,
                    pid=pid,
                    node=node,
                    app_name=app_name,
                    owner_team=owner_team,
                )

    def iter_pods_for_host_records(self, host: str) -> Iterable[PodRecord]:
        """Yield PodRecord objects for all pods on this host."""
        for pod in self.iter_pods_for_host(host):
            namespace = pod.get("namespace", "default")
            pod_name = pod.get("pod_name") or pod.get("pod_id") or "unknown-pod"
            pod_id = pod.get("pod_id") or pod_name
            node = pod.get("hostname") or host
            status = pod.get("status", "UNKNOWN")
            ip_address = pod.get("ip_address")

            # Extract labels for app_name and owner_team
            labels = pod.get("labels", {})
            app_name = labels.get("app") or labels.get("application") or labels.get("dibba_app")
            owner_team = labels.get("owner") or labels.get("dibba_owner")

            yield PodRecord(
                cluster=DIBBA_CLUSTER,
                namespace=namespace,
                pod=pod_name,
                pod_id=pod_id,
                node=node,
                status=status,
                ip_address=ip_address,
                app_name=app_name,
                owner_team=owner_team,
            )

    def get_host_info(self, host: str) -> Optional[Dict]:
        """Get host information."""
        try:
            return self.store.get_host(host)
        except Exception as e:
            logger.error(f"Error getting host info for {host}: {e}", exc_info=True)
            return None


# -----------------------------
# Sampling helpers
# -----------------------------

def collect_proc_stats(pid: int) -> Tuple[float, int]:
    """
    Return (cpu_seconds, rss_bytes) for a process PID.

    cpu_seconds is user+system cumulative CPU time.
    rss_bytes is resident set size in bytes.
    """
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            cpu_times = proc.cpu_times()
            mem_info = proc.memory_info()
        cpu_seconds = float(cpu_times.user + cpu_times.system)
        rss_bytes = int(mem_info.rss)
        return cpu_seconds, rss_bytes
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return 0.0, 0
    except Exception as e:
        logger.debug(f"Error collecting stats for PID {pid}: {e}")
        return 0.0, 0


# -----------------------------
# VictoriaMetrics Push Client
# -----------------------------

class VictoriaMetricsPushClient:
    """Client for pushing metrics to VictoriaMetrics via remote_write API."""

    def __init__(self, remote_write_url: str = VM_REMOTE_WRITE_URL, timeout: int = VM_TIMEOUT):
        self.remote_write_url = remote_write_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
        })

    def push_metrics(self, metrics_data: List[Dict]) -> bool:
        """
        Push metrics to VictoriaMetrics in Prometheus remote_write format.
        
        Args:
            metrics_data: List of metric dictionaries with 'name', 'value', 'labels', 'timestamp'
            
        Returns:
            True if successful, False otherwise
        """
        if not metrics_data:
            return True

        try:
            # Convert to Prometheus remote_write format (simplified text format for now)
            # In production, you'd use protobuf encoding
            lines = []
            timestamp_ms = int(time.time() * 1000)
            
            for metric in metrics_data:
                name = metric.get("name")
                value = metric.get("value")
                labels = metric.get("labels", {})
                ts = metric.get("timestamp", timestamp_ms)

                # Build label string
                label_parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
                label_str = "{" + ",".join(label_parts) + "}"

                # Prometheus text format
                lines.append(f"{name}{label_str} {value} {ts}")

            payload = "\n".join(lines) + "\n"

            # Use text format endpoint (VictoriaMetrics supports this)
            response = self.session.post(
                self.remote_write_url.replace("/write", "/import/prometheus"),
                data=payload,
                timeout=self.timeout,
                headers={"Content-Type": "text/plain"},
            )

            if response.status_code in (200, 204):
                logger.debug(f"Successfully pushed {len(metrics_data)} metrics to VictoriaMetrics")
                return True
            else:
                logger.warning(
                    f"VictoriaMetrics push failed: status={response.status_code}, "
                    f"response={response.text[:200]}"
                )
                return False

        except Exception as e:
            logger.error(f"Error pushing metrics to VictoriaMetrics: {e}", exc_info=True)
            return False


# -----------------------------
# Metrics collection
# -----------------------------

def collect_metrics(client: HostPodStoreClient) -> Tuple[List[ContainerRecord], List[PodRecord], Optional[Dict]]:
    """Collect all metrics from HostPodStore."""
    containers = list(client.iter_containers_for_host(HOSTNAME))
    pods = list(client.iter_pods_for_host_records(HOSTNAME))
    host_info = client.get_host_info(HOSTNAME)
    return containers, pods, host_info


def update_prometheus_metrics(containers: List[ContainerRecord], pods: List[PodRecord], host_info: Optional[Dict]):
    """Update Prometheus metrics registry."""
    # Clear metrics each loop
    CONTAINER_CPU.clear()
    CONTAINER_MEM.clear()
    CONTAINER_INFO.clear()
    POD_INFO.clear()
    POD_STATUS.clear()
    HOST_INFO.clear()

    # Update container metrics
    for rec in containers:
        labels = rec.labels
        # Ensure all label keys are present
        full_labels = {
            "cluster": labels.get("cluster", DIBBA_CLUSTER),
            "namespace": labels.get("namespace", "default"),
            "pod": labels.get("pod", "unknown"),
            "container": labels.get("container", "unknown"),
            "container_id": labels.get("container_id", "unknown"),
            "node": labels.get("node", HOSTNAME),
            "app": labels.get("app", ""),
            "owner": labels.get("owner", ""),
        }

        cpu_seconds, rss_bytes = collect_proc_stats(rec.pid)

        CONTAINER_CPU.labels(**full_labels).set(cpu_seconds)
        CONTAINER_MEM.labels(**full_labels).set(rss_bytes)
        CONTAINER_INFO.labels(**full_labels).set(1)

    # Update pod metrics
    seen_pods = set()
    for rec in pods:
        labels = rec.labels
        pod_key = (rec.cluster, rec.namespace, rec.pod, rec.node)

        full_labels = {
            "cluster": labels.get("cluster", DIBBA_CLUSTER),
            "namespace": labels.get("namespace", "default"),
            "pod": labels.get("pod", "unknown"),
            "pod_id": labels.get("pod_id", "unknown"),
            "node": labels.get("node", HOSTNAME),
            "status": labels.get("status", "UNKNOWN"),
            "ip": labels.get("ip", ""),
            "app": labels.get("app", ""),
            "owner": labels.get("owner", ""),
        }

        POD_INFO.labels(**full_labels).set(1)

        # Status metric (1 for RUNNING, 0 for others)
        status_value = 1.0 if rec.status == "RUNNING" else 0.0
        POD_STATUS.labels(
            cluster=full_labels["cluster"],
            namespace=full_labels["namespace"],
            pod=full_labels["pod"],
            pod_id=full_labels["pod_id"],
            node=full_labels["node"],
            status=full_labels["status"],
            app=full_labels["app"],
            owner=full_labels["owner"],
        ).set(status_value)

        seen_pods.add(pod_key)

    # Update host info
    if host_info:
        ip_address = host_info.get("ip_address", "")
        status = host_info.get("status", "unknown")
        HOST_INFO.labels(
            cluster=DIBBA_CLUSTER,
            node=HOSTNAME,
            ip_address=ip_address,
            status=status,
        ).set(1)


def convert_to_vm_format(containers: List[ContainerRecord], pods: List[PodRecord], host_info: Optional[Dict]) -> List[Dict]:
    """Convert metrics to VictoriaMetrics format."""
    metrics = []
    timestamp_ms = int(time.time() * 1000)

    # Container metrics
    for rec in containers:
        labels = rec.labels
        cpu_seconds, rss_bytes = collect_proc_stats(rec.pid)

        # CPU metric
        metrics.append({
            "name": "dibba_container_cpu_seconds_total",
            "value": cpu_seconds,
            "labels": labels,
            "timestamp": timestamp_ms,
        })

        # Memory metric
        metrics.append({
            "name": "dibba_container_memory_usage_bytes",
            "value": rss_bytes,
            "labels": labels,
            "timestamp": timestamp_ms,
        })

        # Info metric
        metrics.append({
            "name": "dibba_container_info",
            "value": 1,
            "labels": labels,
            "timestamp": timestamp_ms,
        })

    # Pod metrics
    for rec in pods:
        labels = rec.labels

        # Pod info
        metrics.append({
            "name": "dibba_pod_info",
            "value": 1,
            "labels": labels,
            "timestamp": timestamp_ms,
        })

        # Pod status (1 for RUNNING, 0 for others)
        status_value = 1.0 if rec.status == "RUNNING" else 0.0
        metrics.append({
            "name": "dibba_pod_status",
            "value": status_value,
            "labels": labels,
            "timestamp": timestamp_ms,
        })

    # Host info
    if host_info:
        ip_address = host_info.get("ip_address", "")
        status = host_info.get("status", "unknown")
        metrics.append({
            "name": "dibba_host_info",
            "value": 1,
            "labels": {
                "cluster": DIBBA_CLUSTER,
                "node": HOSTNAME,
                "ip_address": ip_address,
                "status": status,
            },
            "timestamp": timestamp_ms,
        })

    return metrics


# -----------------------------
# Main scrape/push loop
# -----------------------------

def scrape_loop():
    """Main loop for scrape mode (Prometheus endpoint)."""
    client = HostPodStoreClient()

    while True:
        try:
            with SCRAPE_DURATION.labels(node=HOSTNAME).time():
                containers, pods, host_info = collect_metrics(client)
                update_prometheus_metrics(containers, pods, host_info)
        except Exception as e:
            SCRAPE_ERRORS.labels(node=HOSTNAME).inc()
            logger.error(f"[dibba_metrics_exporter] scrape error: {e}", exc_info=True)

        time.sleep(SCRAPE_INTERVAL_SECONDS)


def push_loop():
    """Main loop for push mode (VictoriaMetrics remote_write)."""
    client = HostPodStoreClient()
    vm_client = VictoriaMetricsPushClient()

    while True:
        try:
            containers, pods, host_info = collect_metrics(client)
            metrics_data = convert_to_vm_format(containers, pods, host_info)

            if metrics_data:
                success = vm_client.push_metrics(metrics_data)
                if not success:
                    PUSH_ERRORS.labels(node=HOSTNAME).inc()
        except Exception as e:
            PUSH_ERRORS.labels(node=HOSTNAME).inc()
            logger.error(f"[dibba_metrics_exporter] push error: {e}", exc_info=True)

        time.sleep(VM_PUSH_INTERVAL)


def main():
    mode = "push" if VMODE_PUSH else "scrape"
    print(
        f"Starting Dibba metrics exporter on {HOSTNAME} "
        f"(cluster={DIBBA_CLUSTER}, mode={mode}) port={EXPORTER_PORT}",
        flush=True,
    )

    if VMODE_PUSH:
        # Push mode: run push loop
        push_loop()
    else:
        # Scrape mode: start HTTP server and run scrape loop
        start_http_server(EXPORTER_PORT)
        scrape_loop()


if __name__ == "__main__":
    main()
