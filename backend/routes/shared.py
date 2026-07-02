"""共享状态：路由蓝图之间共享的实例（数据库、认证、AI等）
   注意：路由文件要通过 from . import shared as _shared; _shared.xxx 来访问，
   不要用 from .shared import xxx，否则拿不到更新后的值"""
import datetime

# 这些变量会在create_app()时通过init()函数设置
db_provider = None       # 数据库提供者
auth_instance = None     # 认证实例
agent = None             # AI助手实例
captcha_store = {}       # 验证码存储（临时字典）
app_start_time = None    # 服务启动时间


def init(db_prov, auth_inst, agent_inst, captcha, start_time):
    """初始化所有共享状态（由server.py的create_app调用）"""
    global db_provider, auth_instance, agent, captcha_store, app_start_time
    db_provider = db_prov
    auth_instance = auth_inst
    agent = agent_inst
    captcha_store = captcha
    app_start_time = start_time
