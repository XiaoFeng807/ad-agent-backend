"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime
from backend.agent.sub_agent import SubAgent
from backend.prompts.data_agent import DATA_AGENT_PROMPT
from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT

class AgentFactory:
    """Agent工厂：根据任务类型动态创建不同风格的Agent"""

    STYLES = {
        "conservative": {
            "suffix": "谨慎分析，只基于确定的数据说话。没有充分依据的判断不要下结论。"
        },
        "aggressive": {
            "suffix": "大胆分析，即使数据有限也要给出推测方向。敢于下结论，标注置信度。"
        },
        "detail": {
            "suffix": "极度详细，逐条数据展开分析。每个指标都单独说明，不遗漏细节。"
        },
    }

    @staticmethod
    def create_agent(agent_type, style="conservative", peer_agents=None):
        from backend.prompts.data_agent import DATA_AGENT_PROMPT
        from backend.prompts.analysis_agent import ANALYSIS_AGENT_PROMPT
        from backend.prompts.knowledge_agent import KNOWLEDGE_AGENT_PROMPT

        suffix = AgentFactory.STYLES.get(style, AgentFactory.STYLES["conservative"])["suffix"]

        if agent_type == "data":
            prompt = DATA_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["get_dashboard_data", "get_daily_trend", "get_plans_summary",
                     "get_daily_report_by_date", "get_week_over_week", "get_activity_timeline"]
        elif agent_type == "analysis":
            prompt = ANALYSIS_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["compare_plans", "get_week_over_week", "get_decision_summary"]
        elif agent_type == "knowledge":
            prompt = KNOWLEDGE_AGENT_PROMPT + "\n\n[风格指令]" + suffix
            tools = ["search_knowledge", "get_hot_products"]
        else:
            raise ValueError(f"Unknown agent type: {agent_type}")

        agent = SubAgent(f"{agent_type}_{style}", prompt, tools)
        if peer_agents:
            agent.peers = peer_agents
        return agent

    @staticmethod
    def create_competing_agents(agent_type, peer_agents=None):
        agents = []
        for style in ["conservative", "aggressive", "detail"]:
            agent = AgentFactory.create_agent(agent_type, style, peer_agents)
            agents.append(agent)
        return agents


