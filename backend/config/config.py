"""集中配置管理 — 所有环境变量统一从这里读取"""
from dataclasses import dataclass, field
import os
from typing import Literal

from pydantic_settings import BaseSettings
from pydantic import Field, SecretStr


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None


class Settings(BaseSettings):
    model_config = {"env_file": None if os.environ.get("AD_OFFLINE_ACCEPTANCE") == "1" else ".env",
                    "env_file_encoding": "utf-8"}

    # ── LLM ──
    API_KEY: str = ""
    BASE_URL: str = "https://api.deepseek.com"
    MODEL: str = "deepseek-flash"
    LLM_PROVIDER: str = "deepseek"

    # ── OpenAI 备用 ──
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # ── DeepSeek 独立密钥 ──
    DEEPSEEK_API_KEY: str = ""

    # ── 智谱 GLM ──
    GLM_API_KEY: str = ""
    GLM_MODEL: str = "glm-4.5-air"

    # ── 服务 ──
    HOST: str = "0.0.0.0"
    PORT: int = 5010
    DEBUG: bool = True
    APP_STARTUP_MODE: Literal["business", "demo"] = "business"
    RAG_DB_PATH: str = ""
    PLAN_METRICS_DB_PATH: str = ""
    DIAGNOSIS_SANDBOX_DB_PATH: str = ""
    LISTING_SIGNIFICANT_DECLINE_PCT: float | None = Field(default=None, gt=0, le=100)
    DIAGNOSIS_MYSQL_ENABLED: bool = False
    DIAGNOSIS_MYSQL_SCHEMA_VERSION: Literal["1", "2"] = "1"
    DIAGNOSIS_MYSQL_PORT: int = 0
    DIAGNOSIS_MYSQL_PASSWORD: SecretStr = SecretStr("")
    DIAGNOSIS_MYSQL_PAUSE_ENABLED: bool = False
    MYSQL_CHAT_MEMORY_ROOT: str = ""
    AMAZON_ORDERS_SHOP_ID: str = ""
    AMAZON_LWA_CLIENT_ID: SecretStr = SecretStr("")
    AMAZON_LWA_CLIENT_SECRET: SecretStr = SecretStr("")
    AMAZON_LWA_REFRESH_TOKEN: SecretStr = SecretStr("")

    PRODUCT_R4_CPU_ENABLED: bool = True
    PRODUCT_EVIDENCE_MODEL_ADMISSION_ENABLED: bool = True
    PRODUCT_RERANK_ENABLED: bool = False
    PRODUCT_RERANK_USER_IDS: frozenset[int] = frozenset()
    PRODUCT_RERANK_MAX_CONCURRENCY: int = Field(default=1, ge=1)
    PRODUCT_RERANK_QUEUE_SIZE: int = Field(default=3, ge=0)
    PRODUCT_RERANK_QUEUE_TIMEOUT_MS: int = Field(default=400, gt=0)
    PRODUCT_RERANK_INFERENCE_TIMEOUT_MS: int = Field(default=700, gt=0)

    # ── JWT ──
    JWT_SECRET: str = ""

    def llm_config(self, provider_name=None):
        """Resolve once for both SDK construction and every Agent request."""
        name = (provider_name or self.LLM_PROVIDER).strip().lower()
        if name == "deepseek":
            return LLMConfig(name, self.MODEL, self.API_KEY or self.DEEPSEEK_API_KEY, self.BASE_URL)
        if name == "openai":
            return LLMConfig(name, self.OPENAI_MODEL, self.OPENAI_API_KEY)
        if name == "glm":
            return LLMConfig(name, self.GLM_MODEL, self.GLM_API_KEY.strip(), "https://open.bigmodel.cn/api/paas/v4")
        if name == "mock":
            return LLMConfig(name, "mock", "")
        raise ValueError("不支持的 Provider，可选: deepseek, openai, glm, mock")


# 全局单例，其他地方 from backend.config.config import settings
settings = Settings()
