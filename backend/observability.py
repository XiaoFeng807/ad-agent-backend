"""可观测性模块：API请求监控 + AI调用统计"""

import time
import threading
from collections import defaultdict
from datetime import datetime

# ==================== 请求统计 ====================
_request_stats = {
    "total": 0,
    "by_path": defaultdict(int),
    "by_method": defaultdict(int),
    "by_status": defaultdict(int),
    "errors": [],
    "total_time": 0.0,
    "slowest": {"path": "", "time": 0, "method": ""},
}

_request_lock = threading.Lock()


def record_request(method, path, status_code, duration):
    """记录一次API请求"""
    with _request_lock:
        _request_stats["total"] += 1
        _request_stats["by_path"][path] += 1
        _request_stats["by_method"][method] += 1
        _request_stats["by_status"][status_code] += 1
        _request_stats["total_time"] += duration

        if duration > _request_stats["slowest"]["time"]:
            _request_stats["slowest"] = {
                "path": path, "time": round(duration, 3), "method": method
            }

        # 只保留最近20条错误
        if status_code >= 400:
            _request_stats["errors"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "method": method,
                "path": path,
                "status": status_code,
                "duration": round(duration, 3),
            })
            if len(_request_stats["errors"]) > 20:
                _request_stats["errors"] = _request_stats["errors"][-20:]


# ==================== AI 调用统计 ====================
_ai_stats = {
    "total_calls": 0,
    "total_tokens": 0,
    "total_time": 0.0,
    "call_history": [],
}

_ai_lock = threading.Lock()


def record_ai_call(duration, tokens=0):
    """记录一次AI调用"""
    with _ai_lock:
        _ai_stats["total_calls"] += 1
        _ai_stats["total_time"] += duration
        _ai_stats["total_tokens"] += tokens
        _ai_stats["call_history"].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "duration": round(duration, 2),
            "tokens": tokens,
        })
        if len(_ai_stats["call_history"]) > 100:
            _ai_stats["call_history"] = _ai_stats["call_history"][-100:]


# ==================== 系统状态查询 ====================

def get_system_status(start_time):
    """获取系统运行状态"""
    now = time.time()
    uptime_seconds = now - start_time
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)

    with _request_lock:
        req = dict(_request_stats)

    with _ai_lock:
        ai = dict(_ai_stats)

    # 计算平均耗时
    avg_time = round(req["total_time"] / req["total"], 3) if req["total"] > 0 else 0

    # 统计状态码分布
    status_dist = dict(req["by_status"])
    ok_count = sum(v for k, v in status_dist.items() if k < 400)
    error_count = sum(v for k, v in status_dist.items() if k >= 400)

    # 最常访问的路径
    top_paths = sorted(req["by_path"].items(), key=lambda x: -x[1])[:5]

    return {
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": int(uptime_seconds),
        "requests": {
            "total": req["total"],
            "ok": ok_count,
            "errors": error_count,
            "avg_duration": avg_time,
            "slowest": req["slowest"],
            "top_paths": [{"path": p, "count": c} for p, c in top_paths],
            "recent_errors": req["errors"][-5:],
        },
        "ai": {
            "total_calls": ai["total_calls"],
            "total_tokens": ai["total_tokens"],
            "avg_duration": round(ai["total_time"] / ai["total_calls"], 2) if ai["total_calls"] > 0 else 0,
            "recent_calls": ai["call_history"][-5:],
        },
    }
