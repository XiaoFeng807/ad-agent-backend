"""依赖注入模块：管理各个组件的创建和共享"""
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


class ConfigProvider:
    """配置提供者"""
    def get(self, key, default=None):
        return os.getenv(key, default)


class DatabaseProvider:
    """数据库提供者：管理数据库连接的创建和关闭"""
    def __init__(self):
        from backend.database.database import get_db
        self._get_db = get_db  # 保存get_db函数的引用

    def get_connection(self):
        """获取一个新的数据库连接"""
        return self._get_db()

    def close(self, conn):
        """关闭数据库连接"""
        if conn:
            try:
                conn.close()
            except:
                pass


class ToolRegistry:
    """函数注册表：管理所有AI可调用的工具函数"""

    def __init__(self):
        self._tools = {}       # 函数名 → 实际函数
        self._definitions = []  # 函数描述列表（发给AI用）

    def register(self, name, func, definition):
        """注册一个函数：给AI提供名字、实际函数、描述"""
        self._tools[name] = func
        self._definitions.append(definition)

    def get_tool(self, name):
        """根据函数名获取实际函数"""
        return self._tools.get(name)

    def get_definitions(self):
        """获取所有函数描述（发给AI用）"""
        return self._definitions


def create_tool_registry(db_provider=None):
    """创建并初始化函数注册表，注册所有工具函数"""
    from backend.tools import tools
    registry = ToolRegistry()
    tools.register_all_tools(registry, db_provider)
    return registry
