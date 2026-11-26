"""LLM 适配器层：统一接口，支持多 Provider 切换"""

from abc import ABC, abstractmethod
from uuid import uuid4
from backend.config.config import settings
from backend.agent.fault_tolerance import TIMEOUT_CONFIG
from backend.observability import add_trace_step


# ==================== 抽象基类 ====================
class LLMProvider(ABC):
    """所有 LLM Provider 的抽象基类"""

    @property
    def model(self):
        return self._model
    
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


# ==================== OpenAI 兼容适配器 ====================
class _NonThinkingCompletions:
    """Keep tool continuations in non-thinking mode until reasoning history is supported."""

    def __init__(self, completions, *, supports_tool_choice_none):
        self._completions = completions
        self._supports_tool_choice_none = supports_tool_choice_none

    def with_defaults(self, kwargs):
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_body"] = extra_body
        if not self._supports_tool_choice_none and kwargs.get("tool_choice") == "none":
            # GLM expects auto when tools are offered. Omit them for a text-only turn.
            kwargs.pop("tool_choice")
            kwargs.pop("tools", None)
        return kwargs

    def create(self, **kwargs):
        return self._completions.create(**self.with_defaults(kwargs))

    def __getattr__(self, name):
        return getattr(self._completions, name)


class _NonThinkingChat:
    def __init__(self, chat, *, supports_tool_choice_none):
        self._chat = chat
        self.completions = _NonThinkingCompletions(
            chat.completions, supports_tool_choice_none=supports_tool_choice_none,
        )

    def __getattr__(self, name):
        return getattr(self._chat, name)


def create_completion(provider, budget, *, role, step, without_memory=None,
                      optional=False, preflight=False, sdk_max_retries=None, authorize_dispatch=None, **payload):
    """Prepare the request, then let the caller authorize its handoff to the SDK."""
    from backend.agent.request_budget import ChatSessionInvalid, ExecutionStopped

    execution = budget.execution if budget is not None else None
    if execution is not None:
        sdk_max_retries = 0
    completions = provider.chat.completions
    if isinstance(completions, _NonThinkingCompletions):
        payload = completions.with_defaults(payload)
        completions = completions._completions
    if (sdk_max_retries is not None and isinstance(provider, _OpenAICompatibleProvider)
            and provider._client.max_retries != sdk_max_retries):
        completions = provider._client.with_options(max_retries=sdk_max_retries).chat.completions
    if budget is not None:
        payload = budget.prepare(payload, role=role, step=step, without_memory=without_memory,
                                 optional=optional, preflight=preflight)
    if preflight:
        return None
    sdk_create = completions.create
    if execution is not None:
        if not execution.reserve("model", keep=int(role != "orchestrator")):
            raise ExecutionStopped(execution.stop_code() or "model_budget_exhausted")
        payload["timeout"] = max(0.001, min(payload.get("timeout", TIMEOUT_CONFIG["llm_call"]),
                                            execution.seconds_left()))
        call_id = uuid4().hex
        link = {"role": role, "runtime_call_id": call_id}
        add_trace_step(budget.trace_id, "model_call", step, **link)
    if authorize_dispatch is not None:
        try:
            authorize_dispatch()
        except Exception:
            if execution is not None:
                add_trace_step(budget.trace_id, "model_result", step, status="error",
                               error_code="dispatch_rejected", **link)
            raise
    try:
        if execution is not None:
            execution.require_session(budget.trace_id, "sdk_dispatch", **link)
        response = sdk_create(**payload)
    except ChatSessionInvalid as error:
        if execution is not None:
            add_trace_step(budget.trace_id, "model_result", step, status="error", error_code=error.code, **link)
        raise
    except Exception:
        if execution is not None:
            code = execution.stop_code()
            add_trace_step(budget.trace_id, "model_result", step, status="error",
                           error_code=code or "model_failure", **link)
            if code:
                raise ExecutionStopped(code) from None
        raise
    if execution is not None:
        if code := execution.stop_code():
            if payload.get("stream") and hasattr(response, "close"):
                response.close()
            add_trace_step(budget.trace_id, "model_result", step, status="error",
                           error_code="expired_receipt", **link)
            raise ExecutionStopped(code)
        add_trace_step(budget.trace_id, "model_result", step, **link)
    return response


class _OpenAICompatibleProvider(LLMProvider):
    _disable_thinking = False
    _supports_tool_choice_none = True

    def __init__(self, config=None):
        from openai import OpenAI
        config = config or settings.llm_config(self._provider_name)
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=TIMEOUT_CONFIG["llm_call"],
        )
        self._model = config.model
        self._chat_api = self._client.chat
        if self._disable_thinking:
            self._chat_api = _NonThinkingChat(
                self._chat_api, supports_tool_choice_none=self._supports_tool_choice_none,
            )
    
    @property
    def name(self):
        return f"{self._label}({self._model})"
    
    @property
    def chat(self):
        """兼容 OpenAI SDK 的 chat.completions.create 调用方式"""
        return self._chat_api
    
    def _chat(self, messages, tools=None, stream=False, temperature=0):
        return self.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=stream,
            temperature=temperature
        )
    
    def chat_stream(self, messages, tools=None):
        return self.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            stream=True
        )


class OpenAIProvider(_OpenAICompatibleProvider):
    _provider_name = "openai"
    _label = "OpenAI"


class DeepSeekProvider(_OpenAICompatibleProvider):
    _provider_name = "deepseek"
    _label = "DeepSeek"
    _disable_thinking = True


class GLMProvider(_OpenAICompatibleProvider):
    _provider_name = "glm"
    _label = "GLM"
    _disable_thinking = True
    _supports_tool_choice_none = False

    def __init__(self, config=None):
        config = config or settings.llm_config("glm")
        if not config.api_key:
            raise ValueError("GLM 缺少 API Key，请在仓库根目录的 .env 填写 GLM_API_KEY")
        super().__init__(config)


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
    
    def __init__(self, config=None):
        self._model = "mock"
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
        
        # 先检查是否已有工具执行结果（避免无限循环）
        has_tool_results = any(m.get("role") == "tool" for m in messages)
        if has_tool_results:
            tool_results_list = []
            for m in messages:
                if m.get("role") == "tool":
                    data = m.get("content", "")
                    tool_results_list.append(data[:200])
            summary = "[Mock数据摘要] 查询结果如下：\n"
            for r in tool_results_list:
                summary += r + "\n"
            resp = MockResp(summary)
            if stream:
                def gen_summary():
                    for c in resp.choices:
                        yield c
                return gen_summary()
            return resp
        
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
        config = settings.llm_config(provider_name)
        name = config.provider
        
        # 缓存实例（避免重复创建）
        if config in cls._instances:
            return cls._instances[config]
        
        providers = {
            "deepseek": DeepSeekProvider,
            "openai": OpenAIProvider,
            "glm": GLMProvider,
            "mock": MockProvider,
        }
        
        provider_class = providers.get(name)
        if not provider_class:
            raise ValueError(f"不支持的 Provider: {name}，可选: {list(providers.keys())}")
        
        instance = provider_class(config)
        cls._instances[config] = instance
        return instance

    @staticmethod
    def model_for(provider):
        # Keep SDK-compatible test doubles and downstream custom providers working.
        return getattr(provider, "model", None) or settings.llm_config().model
    
    @classmethod
    def list_providers(cls):
        """列出所有可用的 Provider"""
        return {
            "deepseek": "DeepSeek API（默认，需配置 API_KEY）",
            "openai": "OpenAI API（需配置 OPENAI_API_KEY）",
            "glm": "智谱 GLM API（需配置 GLM_API_KEY）",
            "mock": "Mock 模式（不调 API，测试用）",
        }


print(f"  可用 Provider: {list(ProviderFactory.list_providers().keys())}")
