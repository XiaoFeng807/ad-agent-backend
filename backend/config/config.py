"""集中配置管理 — 所有环境变量统一从这里读取"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # ── LLM ──
    API_KEY: str = ""
    BASE_URL: str = "https://api.deepseek.com"
    MODEL: str = "deepseek-chat"
    LLM_PROVIDER: str = "deepseek"

    # ── OpenAI 备用 ──
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # ── DeepSeek 独立密钥 ──
    DEEPSEEK_API_KEY: str = ""

    # ── 服务 ──
    HOST: str = "0.0.0.0"
    PORT: int = 5010
    DEBUG: bool = True

    # ── JWT ──
    JWT_SECRET: str = "ad_agent_secret_key_2026"


# 全局单例，其他地方 from backend.config.config import settings
settings = Settings()