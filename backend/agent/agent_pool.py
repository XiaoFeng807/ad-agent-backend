"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.pipeline import PipelineCoordinator
from backend.agent.orchestrator import OrchestratorAgent
from backend.agent.sub_agent import SubAgent
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT

class AgentPool:
    """Agent 连接池：支持并发请求，避免单实例阻塞"""
    
    def __init__(self, min_size=2, max_size=10):
        self._min_size = min_size
        self._max_size = max_size
        self._pool = []
        self._in_use = set()
        self._lock = __import__("threading").Lock()
        self._init_pool()
    
    def _init_pool(self):
        for _ in range(self._min_size):
            agent = PipelineCoordinator(OrchestratorAgent())
            self._pool.append(agent)
    
    def acquire(self):
        """从池中获取一个可用 agent"""
        import time
        start = time.time()
        while time.time() - start < 10:  # 最多等 10 秒
            with self._lock:
                # 找空闲的
                for agent in self._pool:
                    if id(agent) not in self._in_use:
                        self._in_use.add(id(agent))
                        return agent
                # 没空闲但没超上限，创建新的
                if len(self._pool) < self._max_size:
                    agent = PipelineCoordinator(OrchestratorAgent())
                    self._pool.append(agent)
                    self._in_use.add(id(agent))
                    return agent
            # 池满了，等一会再试
            time.sleep(0.1)
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
