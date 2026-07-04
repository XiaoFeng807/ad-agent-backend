"""健康检查模块：服务器 + 数据库 + 可观测性状态"""

import time
from backend.database.database import get_db
from backend.observability import get_system_status

# 服务器启动时间
_start_time = time.time()


def check_health():
    """详细健康检查"""
    result = {"status": "ok", "timestamp": time.time()}
    
    # 服务器状态
    result["server"] = {"status": "ok", "version": "v17.0"}
    
    # 数据库状态
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        result["database"] = {"status": "ok", "user_count": count}
    except Exception as e:
        result["database"] = {"status": "error", "detail": str(e)}
        result["status"] = "degraded"
    
    # 可观测性概览
    result["observability"] = get_system_status(_start_time)
    
    return result


def get_start_time():
    return _start_time
