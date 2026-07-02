"""健康检查路由"""
from flask import jsonify
from .blueprints import health_bp
from backend.health.health import check_health


@health_bp.route("/api/health")
def api_health():
    """检查服务器和数据库状态"""
    return jsonify({"code": 200, "data": check_health()})
