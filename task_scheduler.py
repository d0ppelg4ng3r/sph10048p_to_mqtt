import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Any, Optional


@dataclass
class _Task:
    name: str
    func: Callable[..., Any]
    interval: float
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    run_immediately: bool = False
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)


class TaskScheduler:
    """Simple thread-based task scheduler.

    Usage:
      sched = TaskScheduler()
      sched.add_task(myfunc, interval=5, name='every5s')
      sched.start()
      # later: sched.stop()

    Features:
    - Add and remove named tasks
    - Each task runs in its own thread and is rescheduled after completion
    - Stops cleanly via `stop()`
    """

    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()

    def add_task(self, func: Callable[..., Any], interval: float, *,
                 name: Optional[str] = None, args=(), kwargs=None,
                 run_immediately: bool = False) -> str:
        """Add a periodic task.

        Returns the task name (auto-generated if not provided).
        """
        if kwargs is None:
            kwargs = {}
        if name is None:
            name = f"task-{len(self._tasks)+1}"

        task = _Task(name=name, func=func, interval=float(interval),
                     args=tuple(args), kwargs=dict(kwargs),
                     run_immediately=run_immediately)

        with self._lock:
            if name in self._tasks:
                raise ValueError(f"Task with name '{name}' already exists")
            self._tasks[name] = task

        return name

    def remove_task(self, name: str) -> bool:
        """Remove a task and stop it if running. Returns True if removed."""
        with self._lock:
            task = self._tasks.pop(name, None)

        if task:
            self._stop_task(task)
            return True
        return False

    def _stop_task(self, task: _Task):
        task._stop_event.set()
        th = task._thread
        if th and th.is_alive():
            th.join(timeout=2)

    def start(self):
        """Start all tasks. Tasks previously added will start running."""
        with self._lock:
            for task in list(self._tasks.values()):
                if task._thread and task._thread.is_alive():
                    continue
                task._stop_event.clear()
                task._thread = threading.Thread(target=self._run_task_loop, args=(task,), daemon=True)
                task._thread.start()

    def stop(self):
        """Stop all tasks and wait briefly for threads to finish."""
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            self._stop_task(task)

    def _run_task_loop(self, task: _Task):
        next_wait = 0.0
        if not task.run_immediately:
            next_wait = task.interval

        while not task._stop_event.wait(next_wait):
            start_time = time.time()
            try:
                task.func(*task.args, **task.kwargs)
            except Exception:
                traceback.print_exc()
            elapsed = time.time() - start_time
            next_wait = max(0.0, task.interval - elapsed)

    def list_tasks(self):
        """Return a list of (name, interval) for current tasks."""
        with self._lock:
            return [(t.name, t.interval) for t in self._tasks.values()]


__all__ = ["TaskScheduler"]
