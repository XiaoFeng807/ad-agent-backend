"""配置模块：从.env文件读取配置"""
import os
from dotenv import load_dotenv

# 加载.env文件（项目根目录）
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


class Config:
    """所有配置项"""
    API_KEY = os.getenv("API_KEY", "")              # DeepSeek API密钥
    BASE_URL = os.getenv("BASE_URL", "https://api.deepseek.com")  # API地址
    MODEL = os.getenv("MODEL", "deepseek-chat")     # 模型名
    PORT = int(os.getenv("PORT", 5000))              # 服务端口
    DEBUG = os.getenv("DEBUG", "True") == "True"    # 调试模式
    HOST = os.getenv("HOST", "0.0.0.0")             # 监听地址
