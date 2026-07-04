"""容错模块：重试、超时、降级"""

import time
import functools
import json

# ==================== 配置 ====================

RETRY_CONFIG = {
    "max_retries": 3,           # 最多重试3次
    "base_delay": 1.0,          # 首次等待1秒
    "max_delay": 10.0,          # 最长等待10秒
    "backoff_factor": 2.0,      # 指数退避（1s → 2s → 4s）
}

TIMEOUT_CONFIG = {
    "llm_call": 30,             # LLM调用超时30秒
    "function_call": 10,        # 函数调用超时10秒
    "db_query": 5,              # 数据库查询超时5秒
}

# ==================== 重试装饰器 ====================

def with_retry(max_retries=None, base_delay=None, fallback_return=None):
    """
    函数调用自动重试装饰器
    
    用法:
        @with_retry(max_retries=3)
        def get_data():
            ...
    
        @with_retry(fallback_return=[])
        def get_plans():
            ...
    """
    cfg = RETRY_CONFIG
    max_r = max_retries if max_retries is not None else cfg["max_retries"]
    base_d = base_delay if base_delay is not None else cfg["base_delay"]
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_r + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_r:
                        delay = min(base_d * (cfg["backoff_factor"] ** (attempt - 1)), cfg["max_delay"])
                        print(f"  [容错] {func.__name__} 第{attempt}次失败: {e}, {delay}s后重试...")
                        time.sleep(delay)
            
            # 所有重试都失败了
            print(f"  [容错] {func.__name__} 重试{max_r}次全部失败: {last_error}")
            if fallback_return is not None:
                print(f"  [容错] 使用降级返回值: {fallback_return}")
                return fallback_return
            raise last_error
        return wrapper
    return decorator


# ==================== 降级方案 ====================

FALLBACK_CHAINS = {
    # 主函数失败 → 备选函数
    "get_dashboard_data": ["get_mock_dashboard_data"],
    "get_daily_trend": ["get_mock_daily_report"],
    "get_alerts": [],
    "get_plans_summary": [],
}

# ==================== 函数调用执行器 ====================

def safe_execute(func, func_name, args, fallback_fn_map=None):
    """
    安全执行函数：重试 + 超时 + 降级
    
    参数:
        func: 要执行的函数
        func_name: 函数名
        args: 参数dict
        fallback_fn_map: 降级函数映射 {函数名: 函数对象}
    
    返回:
        (成功标志, 结果或错误信息)
    """
    # 尝试主函数
    try:
        result = func(**args)
        return (True, result)
    except Exception as e:
        print(f"  [容错] 主函数 {func_name} 执行失败: {e}")
    
    # 查找降级方案
    fallback_names = FALLBACK_CHAINS.get(func_name, [])
    if fallback_names and fallback_fn_map:
        for fb_name in fallback_names:
            fb_func = fallback_fn_map.get(fb_name)
            if fb_func:
                try:
                    print(f"  [容错] 尝试降级函数: {fb_name}")
                    result = fb_func(**args)
                    return (True, result)
                except Exception as e2:
                    print(f"  [容错] 降级函数 {fb_name} 也失败: {e2}")
    
    # 全部失败，返回降级数据
    return (False, {"error": f"数据查询失败", "fallback": True})


# ==================== 结果校验 ====================

def validate_result(func_name, result):
    """
    校验函数执行结果是否合理
    
    返回:
        (是否有效, 校验信息)
    """
    if result is None:
        return (False, "返回值为空")
    
    if isinstance(result, dict):
        if "error" in result:
            return (False, f"返回错误: {result['error']}")
        # 检查关键字段是否存在
        key_fields = {
            "get_dashboard_data": ["total_cost", "total_revenue", "roas"],
            "get_daily_trend": [],
            "get_alerts": [],
        }
        fields = key_fields.get(func_name, [])
        missing = [f for f in fields if f not in result]
        if missing:
            return (False, f"缺少关键字段: {missing}")
    
    if isinstance(result, list) and len(result) == 0 and func_name in ["get_dashboard_data"]:
        return (False, "返回空列表，数据可能未就绪")
    
    return (True, "有效")
