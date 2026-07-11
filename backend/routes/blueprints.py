"""定义所有Flask蓝图（每个蓝图负责一类功能的路由）"""
from flask import Blueprint

auth_bp = Blueprint("auth", __name__)           # 认证：验证码、登录
user_bp = Blueprint("user", __name__)           # 用户管理
dashboard_bp = Blueprint("dashboard", __name__) # 仪表盘
plan_bp = Blueprint("plan", __name__)           # 投放计划
account_bp = Blueprint("account", __name__)     # 预算管理
alert_bp = Blueprint("alert", __name__)         # 告警中心
chat_bp = Blueprint("chat", __name__)           # AI聊天
log_bp = Blueprint("log", __name__)             # 操作日志
material_bp = Blueprint("material", __name__)   # 素材管理
health_bp = Blueprint("health", __name__)       # 健康检查
decision_bp = Blueprint("decision", __name__)   # AI决策
