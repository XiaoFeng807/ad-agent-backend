"""Auto-extracted from multi_agent.py"""

import time
from threading import Lock

from anyio import sleep
from anyio.to_thread import run_sync

from backend.agent.pipeline import PipelineCoordinator
from backend.agent.orchestrator import OrchestratorAgent

class AgentPool:
    """Agent 连接池：支持并发请求，避免单实例阻塞"""
    
    def __init__(self, min_size=2, max_size=10):
        self._min_size = min_size
        self._max_size = max_size
        self._pool = []
        self._in_use = set()
        self._building = 0
        self._lock = Lock()
        self._init_pool()
    
    def _init_pool(self):
        for _ in range(self._min_size):
            agent = PipelineCoordinator(OrchestratorAgent())
            self._pool.append(agent)
    
    def _try_acquire(self):
        with self._lock:
            for agent in self._pool:
                if id(agent) not in self._in_use:
                    self._in_use.add(id(agent))
                    return agent, False
            return None, len(self._pool) + self._building < self._max_size

    def _expand(self):
        with self._lock:
            if len(self._pool) > len(self._in_use) or len(self._pool) + self._building >= self._max_size:
                return
            self._building += 1

        agent = None
        try:
            agent = PipelineCoordinator(OrchestratorAgent())
        finally:
            with self._lock:
                self._building -= 1
                # 发布为空闲实例，即使等待构造的请求已断开也可复用。
                if agent is not None:
                    self._pool.append(agent)

    def acquire(self):
        """从池中获取一个可用 agent"""
        deadline = time.monotonic() + 10
        while True:
            agent, can_expand = self._try_acquire()
            if agent is not None:
                return agent
            if time.monotonic() >= deadline:
                break
            if can_expand:
                self._expand()
            else:
                time.sleep(0.1)
        raise TimeoutError("Agent 池已满，请稍后再试")

    async def acquire_async(self):
        """池满时在事件循环中等待，给持有 Agent 的请求留出工作线程。"""
        deadline = time.monotonic() + 10
        while True:
            agent, can_expand = self._try_acquire()
            if agent is not None:
                return agent
            if time.monotonic() >= deadline:
                break
            if can_expand:
                await run_sync(self._expand)
            else:
                await sleep(0.1)
        raise TimeoutError("Agent 池已满，请稍后再试")
    
    def release(self, agent):
        """释放 agent 回池中"""
        with self._lock:
            self._in_use.discard(id(agent))
    
    @property
    def stats(self):
        """池状态"""
        with self._lock:
            return {
                "total": len(self._pool),
                "in_use": len(self._in_use),
                "available": len(self._pool) - len(self._in_use),
                "max_size": self._max_size
            }
