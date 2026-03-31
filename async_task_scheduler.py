import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Dict, Tuple


@dataclass
class _AsyncTask:
    name: str
    func: Callable[..., Any]
    interval: float
    args: Tuple[Any, ...] = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    run_immediately: bool = False
    _handle: Optional[asyncio.Task] = None


class AsyncTaskScheduler:
    """Schedule coroutines or sync functions on the current asyncio event loop.

    Usage:
      sched = AsyncTaskScheduler()
      sched.add_task(my_coroutine, interval=5, name='read', run_immediately=True)
      await sched.start()
      # scheduler runs until you call await sched.stop()

    Notes:
    - If you pass a regular (sync) function, it will be executed in the default
      thread executor via `run_in_executor`.
    - Tasks are cancelled and awaited when `stop()` is called.
    """

    def __init__(self):
        self._tasks: Dict[str, _AsyncTask] = {}
        self._lock = asyncio.Lock()

    def add_task(self, func: Callable[..., Any], interval: float, *,
                 name: Optional[str] = None, args=(), kwargs=None,
                 run_immediately: bool = False) -> str:
        if kwargs is None:
            kwargs = {}
        if name is None:
            name = f"task-{len(self._tasks)+1}"
        if name in self._tasks:
            raise ValueError(f"Task with name '{name}' already exists")
        self._tasks[name] = _AsyncTask(name=name, func=func, interval=float(interval),
                                       args=tuple(args), kwargs=dict(kwargs),
                                       run_immediately=run_immediately)
        return name

    async def remove_task(self, name: str) -> bool:
        async with self._lock:
            task = self._tasks.pop(name, None)
        if not task:
            return False
        if task._handle:
            task._handle.cancel()
            try:
                await task._handle
            except asyncio.CancelledError:
                pass
        return True

    async def start(self):
        """Start scheduling all added tasks on the running event loop."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            for t in self._tasks.values():
                if t._handle and not t._handle.done():
                    continue
                t._handle = loop.create_task(self._task_loop(t))

    async def stop(self):
        """Cancel all running tasks and wait for them to finish."""
        async with self._lock:
            tasks = [t for t in self._tasks.values() if t._handle]
        for t in tasks:
            t._handle.cancel()
        for t in tasks:
            try:
                await t._handle
            except asyncio.CancelledError:
                pass

    async def _task_loop(self, task: _AsyncTask):
        # First-run handling
        first_run = task.run_immediately
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(task.interval)
                else:
                    first_run = False

                # Execute function: await if coroutine, else run in executor
                if inspect.iscoroutinefunction(task.func) or inspect.iscoroutine(task.func):
                    await task.func(*task.args, **task.kwargs)
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, lambda: task.func(*task.args, **task.kwargs))

            except asyncio.CancelledError:
                # Task was cancelled - exit cleanly
                break
            except Exception:
                # Avoid crashing the loop; log to stderr
                import traceback as _tb
                _tb.print_exc()
                # continue and schedule next run

    def list_tasks(self):
        return [(t.name, t.interval) for t in self._tasks.values()]


__all__ = ["AsyncTaskScheduler"]
