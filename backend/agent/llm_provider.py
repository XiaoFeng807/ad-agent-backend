"""LLM 适配器层：统一接口，支持多 Provider 切换"""

from abc import ABC, abstractmethod
import os, json, time
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


# ==================== 抽象基类 ====================
class LLMProvider(ABC):
    """所有 LLM Provider 的抽象基类"""
    
    @abstractmethod
    def chat(self, messages, tools=None, stream=False, temperature=0):
        """发送聊天请求"""
        pass
    
    @abstractmethod
    def chat_stream(self, messages, tools=None):
        """流式聊天"""
        pass
    
    @property
    @abstractmethod
    def name(self):
        """Provider 名称"""
        pass


# ==================== DeepSeek 适配器 ====================
class DeepSeekProvider(LLMProvider):
    """DeepSeek API 适配器"""
    
    def __init__(self):
        from openai import OpenAI
        self._client = OpenAI(
            api_key=os.getenv("API_KEY") or os.getenv("DEEPSEEK_API_KEY") or "",
            base_url=os.getenv("BASE_URL", "https://api.deepseek.com")
        )
        self._model = os.getenv("MODEL", "deepseek-chat")
    
    @property
    def name(self):
        return f"DeepSeek({self._model})"
    
    @property
    def chat(self):
        """兼容 OpenAI SDK 的 chat.completions.create 调用方式"""
        return self._client.chat
    
    def _chat(self, messages, tools=None, stream=False, temperature=0):
        return self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=stream,
            temperature=temperature
        )
    
    def chat_stream(self, messages, tools=None):
        return self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=True
        )


# ==================== OpenAI 适配器 ====================
class OpenAIProvider(LLMProvider):
    """OpenAI API 适配器"""
    
    def __init__(self):
        from openai import OpenAI
        self._client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self._model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    @property
    def name(self):
        return f"OpenAI({self._model})"
    
    @property
    def chat(self):
        return self._client.chat
    
    def _chat(self, messages, tools=None, stream=False, temperature=0):
        return self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=stream,
            temperature=temperature
        )
    
    def chat_stream(self, messages, tools=None):
        return self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=True
        )


# ==================== Mock 适配器（测试用） ====================
class _MockCompletions:
    """Mock 的 chat.completions 对象，模拟 OpenAI SDK 接口"""
    class _Completions:
        def create(self, **kwargs):
            import time
            from backend.agent.llm_provider import MockProvider
            # 直接用 MockProvider 的 mock 方法
            mp = MockProvider()
            return mp._chat(kwargs.get('messages', []), 
                                kwargs.get('tools'),
                                kwargs.get('stream', False))
    def __init__(self):
        self.completions = self._Completions()


class MockProvider(LLMProvider):
    """Mock 适配器：不调 API，返回固定回复（用于测试）"""
    
    def __init__(self):
        self._call_count = 0
        self._mock_completions = _MockCompletions()
    
    @property
    def chat(self):
        """兼容 Agent.chat() 的 self.client.chat.completions.create 调用方式"""
        return self._mock_completions
    
    @property
    def name(self):
        return "Mock(测试用)"
    
    def _chat(self, messages, tools=None, stream=False, temperature=0):
        self._call_count += 1
        
        class MockChoice:
            def __init__(self, text):
                class Msg:
                    def __init__(self, t):
                        self.content = t
                        self.tool_calls = None
                self.message = Msg(text)
                self.delta = Msg(text)
                self.finish_reason = "stop"
                self.choices = [self]
        
        class MockResp:
            def __init__(self, text):
                self.choices = [MockChoice(text)]
            def __iter__(self):
                return iter(self.choices)
        
        # 提取用户消息
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        
        # 模拟LLM决策：如果有关键词且工具有效，模拟工具调用
        query_keywords = ["数据", "今天", "昨天", "趋势", "计划", "账户", "花费", "roas", "dashboard"]
        need_data = any(kw in last_user.lower() for kw in query_keywords)
        
        if need_data and tools:
            import json
            tool_name = tools[0]["function"]["name"]
            
            # 模拟 tool_calls
            class FuncStub:
                def __init__(self, n):
                    self.name = n
                    self.arguments = json.dumps({})
            class ToolCallItem:
                def __init__(self):
                    self.id = "mock_call_001"
                    self.function = FuncStub(tool_name)
            
            choice = MockChoice("")
            choice.message.tool_calls = [ToolCallItem()]
            resp = MockResp("")
            resp.choices = [choice]
            return resp
        
        # 没有工具调用：检查是否有工具执行结果
        tool_results = []
        for m in messages:
            if m.get("role") == "tool":
                data = m.get("content", "")
                tool_results.append(data[:200])
        
        if tool_results:
            # 有工具执行结果，格式化为摘要
            summary = "[Mock数据摘要] 查询结果如下：\n"
            for r in tool_results:
                summary += r + "\n"
            resp = MockResp(summary)
        else:
            resp = MockResp("[Mock提示] 请问你想查询什么数据？可以试试问：'今天数据怎么样'")
        if stream:
            def gen():
                for c in resp.choices:
                    yield c
            return gen()
        return resp
    
    def chat_stream(self, messages, tools=None):
        yield self._chat(messages, tools).choices[0]


# ==================== Provider 工厂 ====================
class ProviderFactory:
    """根据配置创建对应的 Provider"""
    
    _instances = {}
    
    @classmethod
    def create(cls, provider_name=None):
        """创建 Provider 实例"""
        name = provider_name or os.getenv("LLM_PROVIDER", "deepseek").lower()
        
        # 缓存实例（避免重复创建）
        if name in cls._instances:
            return cls._instances[name]
        
        providers = {
            "deepseek": DeepSeekProvider,
            "openai": OpenAIProvider,
            "mock": MockProvider,
        }
        
        provider_class = providers.get(name)
        if not provider_class:
            raise ValueError(f"不支持的 Provider: {name}，可选: {list(providers.keys())}")
        
        instance = provider_class()
        cls._instances[name] = instance
        return instance
    
    @classmethod
    def list_providers(cls):
        """列出所有可用的 Provider"""
        return {
            "deepseek": "DeepSeek API（默认，需配置 API_KEY）",
            "openai": "OpenAI API（需配置 OPENAI_API_KEY）",
            "mock": "Mock 模式（不调 API，测试用）",
        }


print(f"  可用 Provider: {list(ProviderFactory.list_providers().keys())}")
