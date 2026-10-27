"""
In-memory background task registry.

Every long running job (clip generation, summarising a 2h podcast, ...) is
registered as a Task, executed on a worker thread and polled by the frontend
through ``/api/<feature>/status/{task_id}``.

The store is deliberately simple: a dict + a lock. Swap it for Redis if you
ever run more than one uvicorn worker.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Task:
    id: str
    feature: str
    status: str = "queued"            # queued | running | done | error
    progress: float = 0.0             # 0..100
    stage: str = "Queued"
    steps: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        return {
            "task_id": self.id,
            "feature": self.feature,
            "status": self.status,
            "progress": round(self.progress, 1),
            "stage": self.stage,
            "steps": self.steps,
            "result": self.result,
            "error": self.error,
        }


class TaskStore:
    """Thread-safe registry of background jobs."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()

    # -- creation ---------------------------------------------------------
    def create(self, feature: str, steps: Optional[List[str]] = None) -> Task:
        task = Task(id=uuid.uuid4().hex, feature=feature)
        task.steps = [{"label": s, "state": "pending"} for s in (steps or [])]
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    # -- mutation (used from worker threads) ------------------------------
    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for key, value in fields.items():
                setattr(task, key, value)
            task.updated_at = time.time()

    def stage(self, task_id: str, index: int, progress: float, stage: str) -> None:
        """Mark step *index* as running, mark previous ones complete."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for i, step in enumerate(task.steps):
                if i < index:
                    step["state"] = "done"
                elif i == index:
                    step["state"] = "running"
            task.progress = progress
            task.stage = stage
            task.status = "running"
            task.updated_at = time.time()

    def finish(self, task_id: str, result: Dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for step in task.steps:
                step["state"] = "done"
            task.status = "done"
            task.progress = 100.0
            task.stage = "Complete"
            task.result = result
            task.updated_at = time.time()

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.status = "error"
            task.error = error
            task.updated_at = time.time()

    # -- execution --------------------------------------------------------
    @staticmethod
    def run(task: Task, fn: Callable[[], Dict[str, Any]], store: "TaskStore") -> None:
        """Run *fn* on a daemon thread and record success/failure."""

        def _worker() -> None:
            store.update(task.id, status="running", stage="Starting")
            try:
                result = fn()
                store.finish(task.id, result)
            except Exception as exc:            # noqa: BLE001 - surface to UI
                traceback.print_exc()
                store.fail(task.id, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=_worker, daemon=True, name=f"task-{task.id[:8]}").start()


STORE = TaskStore()
