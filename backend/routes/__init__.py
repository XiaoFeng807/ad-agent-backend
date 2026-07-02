"""路由注册入口：所有蓝图在这里统一注册到Flask应用"""
from .blueprints import auth_bp, user_bp, dashboard_bp, plan_bp, account_bp
from .blueprints import alert_bp, chat_bp, log_bp, material_bp, health_bp, decision_bp

# 导入各个路由模块（这样它们的 @xxx_bp.route 装饰器才会执行）
from . import auth_routes
from . import user_routes
from . import dashboard_routes
from . import plan_routes
from . import account_routes
from . import alert_routes
from . import chat_routes
from . import log_routes
from . import material_routes
from . import health_routes
from . import decision_routes

# 所有蓝图列表
ALL_BLUEPRINTS = [
    auth_bp, user_bp, dashboard_bp, plan_bp, account_bp,
    alert_bp, chat_bp, log_bp, material_bp, health_bp, decision_bp
]


def register_blueprints(app, prefix=""):
    """注册所有蓝图到Flask应用"""
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp, url_prefix=prefix)
