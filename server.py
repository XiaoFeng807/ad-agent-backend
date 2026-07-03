"""Ad Agent Backend Server - 服务入口"""
import os, datetime
from flask import Flask

# 导入各个模块
from backend.config.config import Config            # 配置
from backend.database.database import init_db, seed_data  # 数据库初始化
from backend.auth.auth import Auth, set_auth_instance      # 认证
from backend.agent.multi_agent import Coordinator                      # AI助手
from backend.di import ConfigProvider, DatabaseProvider, create_tool_registry  # 依赖注入
from backend.routes import register_blueprints      # 路由注册
from backend.routes.shared import init as init_shared  # 共享状态初始化


def create_app():
    """创建并配置Flask应用"""
    # === 初始化各个组件 ===
    config_provider = ConfigProvider()          # 配置提供者
    db_provider = DatabaseProvider()            # 数据库提供者
    tool_registry = create_tool_registry(db_provider)  # 函数注册表
    auth_instance = Auth(db_provider)           # 认证实例
    set_auth_instance(auth_instance)            # 保存到全局（给装饰器用）
    agent = Coordinator()                # AI助手实例
    captcha_store = {}                          # 验证码存储（临时存内存里）
    app_start_time = datetime.datetime.now()    # 记录启动时间

    # 把共享状态传给路由模块（蓝图里要用）
    init_shared(db_provider, auth_instance, agent, captcha_store, app_start_time)

    # === 创建Flask应用 ===
    app = Flask(__name__,
                static_folder=os.path.join(os.path.dirname(__file__), "backend", "static"),
                static_url_path="/static",
                template_folder=os.path.join(os.path.dirname(__file__), "backend", "templates"))

    # === 跨域设置（允许前端从其他地址访问） ===
    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    # === 首页路由 ===
    @app.route("/")
    def index():
        return app.send_static_file("index.html")

    # === 注册所有模块化路由蓝图 ===
    register_blueprints(app)

    return app, agent, auth_instance, captcha_store, app_start_time


if __name__ == "__main__":
    # 启动时自动初始化数据库和样例数据
    init_db()
    seed_data()
    app, _, _, _, _ = create_app()
    print("Starting server on 0.0.0.0:5000")
    print("Login with: boss/admin123 | admin/admin123 | zhangsan/user123 | lisi/user123")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
