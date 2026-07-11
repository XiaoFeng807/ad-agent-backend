"""向后兼容入口 — 所有类从拆分后的模块重新导出"""
import json, os, re
from datetime import datetime

# 重新导出所有类，保证 from backend.agent.multi_agent import X 仍然可用
from backend.agent.tool_registry import ToolRegistry
from backend.agent.sub_agent import SubAgent
from backend.agent.agent_factory import AgentFactory
from backend.agent.competition import Competition
from backend.agent.orchestrator import (
    OrchestratorAgent, DATA_TOOLS, ANALYSIS_TOOLS, KNOWLEDGE_TOOLS
)
from backend.agent.pipeline import Pipeline, PipelineCoordinator
from backend.agent.agent_pool import AgentPool

__all__ = [
    "ToolRegistry", "SubAgent", "AgentFactory", "Competition",
    "OrchestratorAgent", "Pipeline", "PipelineCoordinator", "AgentPool",
    "DATA_TOOLS", "ANALYSIS_TOOLS", "KNOWLEDGE_TOOLS",
]