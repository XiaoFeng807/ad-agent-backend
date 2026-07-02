"""健康检查模块：检查服务器和数据库状态"""
from backend.database.database import get_db


def check_health():
    """检查服务器和数据库是否正常运行"""
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        return {
            "server": {"status": "ok"},
            "database": {"status": "ok", "user_count": count}
        }
    except Exception as e:
        return {
            "server": {"status": "ok"},
            "database": {"status": "error", "detail": str(e)}
        }
