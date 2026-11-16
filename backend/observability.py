"""可观测性模块：链路追踪 + 函数调用日志 + 系统监控"""

import time
import json
import threading
import os
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ==================== 配置 ====================

LOG_DIR = Path(__file__).parent.parent / "logs"
TRACE_LOG_FILE = LOG_DIR / "traces.jsonl"
FUNC_LOG_FILE = LOG_DIR / "functions.jsonl"
ERROR_LOG_FILE = LOG_DIR / "errors.jsonl"
PRODUCT_RERANK_LATE_FILE = LOG_DIR / "product-rerank-late.jsonl"

# 确保日志目录存在
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ==================== 链路追踪 ====================

_traces = {}       # trace_id -> trace_data
_trace_lock = threading.Lock()
_trace_write_lock = threading.Lock()
MAX_TRACES = 500   # 内存保留上限

def new_trace_id():
    return uuid.uuid4().hex[:12]

def start_trace(user_id=None, message=None, *, source=None, trace_id=None, request_id=None):
    trace_id = trace_id or new_trace_id()
    trace = {
        "trace_id": trace_id,
        "start_time": time.time(),
        "start_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "steps": [],       # ReAct 各步骤
        "status": "running",
        "total_duration": 0,
    }
    if user_id is not None:
        trace["user_id"] = user_id
    if message is not None:
        trace["message"] = message[:100]
    if source is not None:
        trace["source"] = source
    if request_id is not None:
        trace["request_id"] = request_id
    with _trace_lock:
        _traces[trace_id] = trace
        # 控制内存上限
        if len(_traces) > MAX_TRACES:
            # SSE 有明确的收尾路径，执行期间保留其工具记录。
            oldest = [key for key, value in _traces.items()
                      if value["status"] != "running" or "source" not in value][:50]
            for k in oldest:
                del _traces[k]
    return trace_id

def add_trace_step(trace_id, stage, action, detail="", duration=0, status="ok", *,
                   role=None, tool_call_id=None, runtime_call_id=None, error_code=None,
                   parent_call_id=None, retry_of=None, budget=None, evidence=None):
    """记录一个ReAct步骤"""
    if not trace_id:
        return
    with _trace_lock:
        trace = _traces.get(trace_id)
        if not trace or trace["status"] != "running":
            return
        step = {
            "stage": stage,        # think / act / observe
            "action": action,      # 调用了什么函数 / LLM回复
            "detail": str(detail)[:200],
            "duration": round(duration, 3),
            "time": datetime.now().strftime("%H:%M:%S"),
            "epoch_ns": time.time_ns(),
            "status": status,
        }
        if role is not None:
            step["role"] = role
        if tool_call_id is not None:
            step["tool_call_id"] = tool_call_id
        if runtime_call_id is not None:
            step["runtime_call_id"] = runtime_call_id
        if parent_call_id is not None:
            step["parent_call_id"] = parent_call_id
        if retry_of is not None:
            step["retry_of"] = retry_of
        if error_code is not None:
            step["error_code"] = error_code
        if budget is not None:
            step["budget"] = budget
        if evidence is not None:
            step["evidence"] = evidence
        trace["steps"].append(step)


@contextmanager
def trace_tool_call(trace_id, role, tool_name, tool_call_id=None, *, runtime_call_id=None,
                    parent_call_id=None, retry_of=None):
    """记录工具调用的元数据。调用方可设置固定错误码表示工具返回失败。"""
    started = time.perf_counter()
    result = {"error_code": None}
    link = {"role": role, "tool_call_id": tool_call_id, "runtime_call_id": runtime_call_id,
            "parent_call_id": parent_call_id, "retry_of": retry_of}
    add_trace_step(trace_id, "tool_call", tool_name, **link)
    try:
        yield result
    except BaseException:
        result["error_code"] = "tool_exception"
        raise
    finally:
        error_code = result["error_code"]
        add_trace_step(trace_id, "tool_result", tool_name, duration=time.perf_counter() - started,
                       status="error" if error_code else "ok", error_code=error_code,
                       evidence=result.get("evidence"), **link)

def end_trace(trace_id, status="completed"):
    if not trace_id:
        return
    with _trace_lock:
        trace = _traces.get(trace_id)
        if not trace or trace["status"] != "running":
            return
        trace["total_duration"] = round(time.time() - trace["start_time"], 3)
        trace["status"] = status
    # 写盘单独加锁，避免并发追加同一文件时影响内存追踪。
    with _trace_write_lock:
        _append_jsonl(TRACE_LOG_FILE, trace)

def get_trace(trace_id):
    with _trace_lock:
        return _traces.get(trace_id)


def record_late_product_rerank(trace_id, reason, timing):
    if trace_id:
        with _trace_write_lock:
            _append_jsonl(PRODUCT_RERANK_LATE_FILE, {"trace_id": trace_id, "reason": reason, **timing})

def list_recent_traces(limit=20):
    with _trace_lock:
        sorted_traces = sorted(_traces.values(), key=lambda t: t["start_time"], reverse=True)
        return sorted_traces[:limit]

# ==================== 函数调用日志 ====================

_func_stats = {
    "total": 0,
    "by_name": defaultdict(lambda: {"calls": 0, "errors": 0, "total_time": 0.0}),
    "slowest": {"name": "", "time": 0, "args": ""},
}
_func_lock = threading.Lock()

_request_stats = {
    "total": 0,
    "by_path": {},
    "by_method": {},
    "by_status": {},
    "errors": [],
    "total_time": 0.0,
    "slowest": {"path": "", "time": 0, "method": ""},
}
_request_lock_2 = __import__('threading').Lock()


def record_request(method, path, status_code, duration):
    """记录一次API请求"""
    with _request_lock_2:
        _request_stats["total"] = _request_stats.get("total", 0) + 1
        by_path = _request_stats["by_path"]
        by_path[path] = by_path.get(path, 0) + 1
        by_method = _request_stats["by_method"]
        by_method[method] = by_method.get(method, 0) + 1
        by_status = _request_stats["by_status"]
        by_status[status_code] = by_status.get(status_code, 0) + 1
        _request_stats["total_time"] = _request_stats.get("total_time", 0) + duration
        if duration > _request_stats["slowest"]["time"]:
            _request_stats["slowest"] = {"path": path, "time": round(duration, 3), "method": method}
        if status_code >= 400:
            errors = _request_stats.get("errors", [])
            errors.append({"time": str(__import__('datetime').datetime.now().strftime('%H:%M:%S')), "method": method, "path": path, "status": status_code})
            if len(errors) > 20:
                _request_stats["errors"] = errors[-20:]


def record_ai_call(duration, tokens=0):
    """兼容旧接口：记录AI调用"""
    with _func_lock:
        _func_stats["total"] += 1


def record_function_call(func_name, args, result, duration, error=None, trace_id=None):
    """记录一次函数调用"""
    with _func_lock:
        _func_stats["total"] += 1
        stat = _func_stats["by_name"][func_name]
        stat["calls"] += 1
        stat["total_time"] += duration
        if error:
            stat["errors"] += 1
        
        if duration > _func_stats["slowest"]["time"]:
            _func_stats["slowest"] = {
                "name": func_name,
                "time": round(duration, 3),
                "args": str(args)[:100],
            }
    
    # 持久化
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "func_name": func_name,
        "args": str(args)[:200],
        "duration": round(duration, 3),
        "has_error": bool(error),
        "error": str(error)[:200] if error else "",
        "result_len": len(str(result)),
        "trace_id": trace_id or "",
    }
    _append_jsonl(FUNC_LOG_FILE, record)

def get_function_stats():
    with _func_lock:
        stats = {}
        for name, s in _func_stats["by_name"].items():
            avg = round(s["total_time"] / s["calls"], 3) if s["calls"] > 0 else 0
            stats[name] = {
                "calls": s["calls"],
                "errors": s["errors"],
                "avg_duration": avg,
                "error_rate": round(s["errors"] / s["calls"] * 100, 1) if s["calls"] > 0 else 0,
            }
        return {
            "total": _func_stats["total"],
            "by_name": stats,
            "slowest": _func_stats["slowest"],
        }

# ==================== 错误日志 ====================

def record_error(category, message, detail=""):
    """记录错误（按类别分类）"""
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "category": category,  # llm_error / func_error / auth_error / db_error
        "message": str(message)[:200],
        "detail": str(detail)[:500],
    }
    _append_jsonl(ERROR_LOG_FILE, record)

# ==================== 持久化工具 ====================

def _append_jsonl(filepath, record):
    """追加一行JSONL日志"""
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入失败不能影响主流程

def read_logs(filepath, limit=50):
    """读取最近的日志"""
    if not filepath.exists():
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-limit:]]
    except Exception:
        return []

# ==================== 系统状态查询（增强版） ====================

def get_system_status(start_time):
    now = time.time()
    uptime = now - start_time
    h, m = int(uptime // 3600), int((uptime % 3600) // 60)

    with _trace_lock:
        traces = list(_traces.values())
        running = sum(1 for t in traces if t["status"] == "running")
        recent = traces[-20:] if traces else []

    func_stats = get_function_stats()

    # 最近错误
    recent_errors = read_logs(ERROR_LOG_FILE, 10)

    return {
        "uptime": f"{h}h {m}m",
        "uptime_seconds": int(uptime),
        "traces": {
            "total": len(traces),
            "running": running,
            "recent": [{
                "trace_id": t["trace_id"],
                "message": t.get("message", ""),
                "steps": len(t["steps"]),
                "duration": t["total_duration"],
                "status": t["status"],
            } for t in recent],
        },
        "functions": func_stats,
        "recent_errors": recent_errors,
        "log_files": {
            "traces": TRACE_LOG_FILE.name,
            "functions": FUNC_LOG_FILE.name,
            "errors": ERROR_LOG_FILE.name,
        },
    }
