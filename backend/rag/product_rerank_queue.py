"""Bounded process-local execution for the pinned product reranker."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Condition, Lock, Thread
from time import perf_counter

from backend.rag.product_rerank_model import Reranker


@dataclass
class Job:
    run: Callable
    on_late: Callable
    queued_at: float
    queue_deadline: float
    timing: dict = field(default_factory=dict)
    started: float | None = None
    finished: float | None = None
    abandoned: str | None = None
    result: tuple | None = None


class RerankQueue:
    def __init__(self, concurrency, queue_size, queue_timeout_ms, inference_timeout_ms, *, factory=Reranker):
        self.concurrency = concurrency
        self.queue_size = queue_size
        self.queue_timeout = queue_timeout_ms / 1000
        self.inference_timeout = inference_timeout_ms / 1000
        self.factory = factory
        self.model = None
        self.model_lock = Lock()
        self.changed = Condition()
        self.pending = deque()
        self.running = 0
        for _ in range(concurrency):
            Thread(target=self._worker, name="product-rerank", daemon=True).start()

    def _model(self):
        with self.model_lock:
            if self.model is None:
                self.model = self.factory()
            return self.model

    def submit(self, run, cancelled, on_late):
        with self.changed:
            depth = max(0, self.running + len(self.pending) - self.concurrency)
            if cancelled():
                return None, "cancelled", {"queue_depth": depth}
            if self.running + len(self.pending) >= self.concurrency + self.queue_size:
                return None, "queue_full", {"queue_depth": depth}
            now = perf_counter()
            job = Job(run, on_late, now, now + self.queue_timeout)
            job.timing["queue_depth"] = max(0, self.running + len(self.pending) + 1 - self.concurrency)
            self.pending.append(job)
            self.changed.notify_all()
            while True:
                now = perf_counter()
                deadline = job.queue_deadline if job.started is None else job.started + self.inference_timeout
                if cancelled():
                    reason = "cancelled"
                elif job.finished is not None and job.finished <= deadline:
                    result, reason = job.result
                    return result, reason, dict(job.timing)
                elif now >= deadline:
                    reason = "queue_timeout" if job.started is None else "inference_timeout"
                else:
                    self.changed.wait(min(deadline - now, .02))
                    continue
                if job.started is None:
                    if job in self.pending:
                        self.pending.remove(job)
                    job.timing["queue_wait_ms"] = (now - job.queued_at) * 1000
                job.abandoned = reason
                if job.finished is not None and job.started is not None:
                    job.on_late(reason, dict(job.timing))
                return None, reason, dict(job.timing)

    def _worker(self):
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.pending)
                job = self.pending.popleft()
                now = perf_counter()
                job.timing["queue_wait_ms"] = (now - job.queued_at) * 1000
                if now >= job.queue_deadline:
                    job.result = (None, "queue_timeout")
                    job.finished = now
                    self.changed.notify_all()
                    continue
                job.started = now
                self.running += 1
                self.changed.notify_all()

            def active():
                with self.changed:
                    return job.abandoned is None and perf_counter() < job.started + self.inference_timeout

            timing = {}
            try:
                result = job.run(self._model, active, timing)
            except Exception as error:
                timing["error_type"] = type(error).__name__
                result = (None, "gpu_unavailable")
            finished = perf_counter()
            timing["execution_ms"] = (finished - job.started) * 1000
            with self.changed:
                job.timing.update(timing)
                job.finished = finished
                job.result = result
                self.running -= 1
                late = job.abandoned
                self.changed.notify_all()
            if late:
                job.on_late(late, dict(job.timing))
