from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from celery import Celery
from kombu.utils.json import dumps as json_dumps  # optional, if you want JSON


@dataclass
class WorkerInfo:
    name: str                      # full worker name, e.g. "celery@ip-172-31-19-101"
    host: str                      # stripped host, e.g. "ip-172-31-19-101"
    online: bool                   # True if responding to inspect calls
    pid: Optional[int] = None
    concurrency: Optional[int] = None
    platform: Optional[str] = None
    broker: Optional[str] = None

    queues: List[str] = None       # queues this worker listens on
    active_tasks: int = 0          # number of active tasks
    reserved_tasks: int = 0        # number of reserved tasks
    registered_tasks: int = 0      # number of registered task names


def _extract_host(worker_name: str) -> str:
    # "celery@ip-172-31-19-101" -> "ip-172-31-19-101"
    return worker_name.split("@", 1)[1] if "@" in worker_name else worker_name


def discover_workers(app: Celery, timeout: int = 5) -> Dict[str, WorkerInfo]:
    """
    Discover Celery workers and aggregate their stats.

    Returns:
        Dict keyed by worker-name with WorkerInfo objects.
    """
    insp = app.control.inspect(timeout=timeout)

    # Each of these returns a dict: { "celery@host": ... } or None
    stats = insp.stats() or {}
    active = insp.active() or {}
    reserved = insp.reserved() or {}
    registered = insp.registered() or {}
    queues = insp.active_queues() or {}
    pings = insp.ping() or {}

    workers: Dict[str, WorkerInfo] = {}

    # First pass: initialize from stats
    for worker_name, s in stats.items():
        workers[worker_name] = WorkerInfo(
            name=worker_name,
            host=_extract_host(worker_name),
            online=worker_name in pings,  # if it replied to ping
            pid=s.get("pid"),
            concurrency=s.get("pool", {}).get("max-concurrency")
            or s.get("pool", {}).get("processes"),
            platform=s.get("platform"),
            broker=s.get("broker", {}).get("transport") if s.get("broker") else None,
            queues=[],
            active_tasks=0,
            reserved_tasks=0,
            registered_tasks=0,
        )

    # Ensure workers seen only in pings/queues but not stats are also included
    for worker_name in set(list(pings.keys()) + list(queues.keys())):
        if worker_name not in workers:
            workers[worker_name] = WorkerInfo(
                name=worker_name,
                host=_extract_host(worker_name),
                online=worker_name in pings,
                pid=None,
                concurrency=None,
                platform=None,
                broker=None,
                queues=[],
                active_tasks=0,
                reserved_tasks=0,
                registered_tasks=0,
            )

    # Attach queue names
    for worker_name, qlist in queues.items():
        if worker_name not in workers:
            continue
        workers[worker_name].queues = [q["name"] for q in qlist]

    # Count active tasks
    for worker_name, task_list in active.items():
        if worker_name not in workers:
            continue
        workers[worker_name].active_tasks = len(task_list or [])

    # Count reserved tasks
    for worker_name, task_list in reserved.items():
        if worker_name not in workers:
            continue
        workers[worker_name].reserved_tasks = len(task_list or [])

    # Count registered tasks
    for worker_name, task_list in registered.items():
        if worker_name not in workers:
            continue
        workers[worker_name].registered_tasks = len(task_list or [])

    return workers


# Optional: helper to get JSON-friendly dict
def discover_workers_as_dict(app: Celery, timeout: int = 5) -> Dict[str, dict]:
    return {name: asdict(info) for name, info in discover_workers(app, timeout).items()}


# Optional: helper to get JSON string (for Redis, API, etc.)
def discover_workers_as_json(app: Celery, timeout: int = 5) -> str:
    return json_dumps(discover_workers_as_dict(app, timeout))
