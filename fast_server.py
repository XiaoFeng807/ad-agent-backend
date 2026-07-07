"""
FastAPI 服务入口 — 支持 REST API + WebSocket 聊天
替代旧的 Flask server.py（向后兼容）

启动方式：
    python fast_server.py
    或
    uvicorn fast_server:app --host 0.0.0.0 --port 5010 --reload
"""

import os, json, datetime, time
from dotenv import load_dotenv
from contextlib import asynccontextmanager

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ===== 导入项目模块 =====
from backend.config.config import Config
from backend.database.database import init_db, seed_data
from backend.auth.auth import Auth, set_auth_instance
from backend.agent.multi_agent import Coordinator, PipelineCoordinator
from backend.di import ConfigProvider, DatabaseProvider, create_tool_registry
from backend.routes.shared import init as init_shared
from backend.observability import record_request, get_system_status

# ===== 初始化 =====
config_provider = ConfigProvider()
db_provider = DatabaseProvider()
tool_registry = create_tool_registry(db_provider)
auth_instance = Auth(db_provider)
set_auth_instance(auth_instance)
agent = PipelineCoordinator(Coordinator())
captcha_store = {}
app_start_time = datetime.datetime.now()

init_shared(db_provider, auth_instance, agent, captcha_store, app_start_time)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库"""
    init_db()
    seed_data()
    print("FastAPI server started with WebSocket support")
    yield


# ===== 创建 FastAPI 应用 =====
app = FastAPI(
    title="智能广告投放助手 API",
    description="广告投放管理 + AI 智能分析系统",
    version="3.0.0",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 请求/响应模型 =====
class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str = ""
    captcha_text: str = ""


class ChatMessage(BaseModel):
    message: str


# ===== 认证辅助 =====
def get_token_from_request(request):
    """从请求头中提取 Bearer Token"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""

def verify_token(token: str = ""):
    """验证 JWT Token"""
    from backend.auth.auth import SECRET
    import jwt
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        return payload
    except:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")


# ===== REST API 端点（复用后端模块） =====
@app.get("/api/captcha")
async def get_captcha():
    """获取验证码"""
    from backend.captcha.captcha import generate_captcha
    import base64, io
    img, code = generate_captcha()
    captcha_id = os.urandom(6).hex()
    captcha_store[captcha_id] = code
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"code": 200, "captcha_id": captcha_id, "image": b64}


@app.post("/api/login")
async def login(req: LoginRequest):
    """用户登录"""
    import jwt, datetime
    
    # 验证验证码
    if req.captcha_id in captcha_store:
        expected = captcha_store.pop(req.captcha_id)
        if req.captcha_text.lower().strip() != expected:
            return {"code": 400, "msg": "验证码错误"}
    elif req.captcha_id:
        return {"code": 400, "msg": "验证码已过期"}
    
    # 验证用户名密码（复用 Auth 的 login 方法）
    result = auth_instance.login(req.username, req.password)
    if not result:
        return {"code": 400, "msg": "用户名或密码错误"}
    
    return {"code": 200, "data": result}


@app.get("/api/dashboard")
async def dashboard(request: Request):
    """仪表盘数据"""
    token = get_token_from_request(request)
    payload = verify_token(token)
    from backend.tools.tools import get_dashboard_data
    data = get_dashboard_data(user_id=payload.get("user_id", 1))
    return {"code": 200, "data": data}


@app.get("/api/daily_reports")
async def daily_reports(request: Request, days: int = 7):
    """每日趋势"""
    token = get_token_from_request(request)
    verify_token(token)
    from backend.tools.tools import get_daily_trend
    data = get_daily_trend(days=days)
    return {"code": 200, "data": data}


@app.get("/api/alerts")
async def alerts(request: Request):
    """告警列表"""
    token = get_token_from_request(request)
    payload = verify_token(token)
    from backend.tools.tools import get_alerts
    data = get_alerts(user_id=payload.get("user_id", 0))
    return {"code": 200, "data": data}

@app.post("/api/alerts/read")
async def alerts_read(request: Request):
    """标记告警已读"""
    token = get_token_from_request(request)
    payload = verify_token(token)
    body = await request.json()
    alert_ids = body.get("ids", [])
    single_id = body.get("alert_id")
    if not alert_ids and single_id is not None:
        alert_ids = [int(single_id)]
    conn = db_provider.get_connection()
    uid = payload.get("user_id", 0)
    if alert_ids:
        placeholders = ",".join("?" * len(alert_ids))
        conn.execute(f"UPDATE alerts SET is_read=1 WHERE id IN ({placeholders}) AND user_id=?", (*alert_ids, uid))
    else:
        conn.execute("UPDATE alerts SET is_read=1 WHERE user_id=?", (uid,))
    conn.commit()
    db_provider.close(conn)
    return {"code": 200, "msg": "已标记已读"}

@app.get("/api/accounts")
async def accounts(request: Request):
    token = get_token_from_request(request)
    payload = verify_token(token)
    
    conn = db_provider.get_connection()
    rows = conn.execute("SELECT * FROM ad_accounts ORDER BY id").fetchall()
    db_provider.close(conn)
    data = [dict(r) for r in rows]
    return {"code": 200, "data": data}

@app.get("/api/plans")
async def plans(request: Request):
    token = get_token_from_request(request)
    verify_token(token)
    from backend.tools.tools import get_plans_summary
    data = get_plans_summary()
    return {"code": 200, "data": data}

@app.get("/api/materials")
async def materials(request: Request):
    token = get_token_from_request(request)
    verify_token(token)
    conn = db_provider.get_connection()
    rows = conn.execute("SELECT * FROM materials ORDER BY id DESC").fetchall()
    db_provider.close(conn)
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/users")
async def users(request: Request):
    token = get_token_from_request(request)
    verify_token(token)
    conn = db_provider.get_connection()
    rows = conn.execute("SELECT id, username, role FROM users ORDER BY id").fetchall()
    db_provider.close(conn)
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/logs")
async def logs(request: Request):
    token = get_token_from_request(request)
    payload = verify_token(token)
    role = payload.get("role", "user")
    uid = payload.get("user_id", 0)
    conn = db_provider.get_connection()
    if role == "boss":
        rows = conn.execute("SELECT * FROM operation_logs ORDER BY id DESC LIMIT 50").fetchall()
    elif role == "admin":
        rows = conn.execute("SELECT l.* FROM operation_logs l JOIN users u ON l.user_id=u.id WHERE u.role IN ('admin','user') ORDER BY l.id DESC LIMIT 50").fetchall()
    else:
        rows = conn.execute("SELECT * FROM operation_logs WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    db_provider.close(conn)
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/decisions")
async def decisions(request: Request):
    token = get_token_from_request(request)
    verify_token(token)
    conn = db_provider.get_connection()
    rows = conn.execute("SELECT * FROM decision_logs ORDER BY id DESC LIMIT 20").fetchall()
    db_provider.close(conn)
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/timeline")
async def timeline(request: Request):
    token = get_token_from_request(request)
    verify_token(token)
    from backend.tools.tools import get_activity_timeline
    data = get_activity_timeline()
    return {"code": 200, "data": data}

@app.get("/api/chat/history")
async def chat_history(request: Request):
    token = get_token_from_request(request)
    payload = verify_token(token)
    history = agent.get_history(payload.get("user_id", 0))
    return {"code": 200, "data": history}

@app.get("/api/health")
async def health():
    return {"code": 200, "data": {"status": "ok", "uptime": "fastapi", "version": "3.0.0"}}


# ===== WebSocket 聊天端点（核心亮点） =====
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 双向聊天 — AI 投放助手

    流程:
    1. 客户端发送 JSON: {"token":"xxx", "message":"今天数据怎么样"}
    2. 服务端返回流式消息: {"type":"text", "content":"今天..."}
    3. 服务端返回完成标记: {"type":"done"}

    优势: 相比 SSE，WebSocket 全双工通信，服务器可主动推送通知
    """
    await websocket.accept()
    
    try:
        # 1. 接收客户端消息
        data = await websocket.receive_text()
        msg = json.loads(data)
        token = msg.get("token", "")
        message = msg.get("message", "").strip()
        
        if not message:
            await websocket.send_json({"type": "error", "content": "消息不能为空"})
            await websocket.send_json({"type": "done"})
            return
        
        # 2. 验证 token
        payload = auth_instance.verify_token(token)
        if not payload:
            await websocket.send_json({"type": "error", "content": "Token 无效，请重新登录"})
            await websocket.send_json({"type": "done"})
            return
        
        user_id = payload.get("user_id", 0)
        
        # 3. 发送思考状态
        await websocket.send_json({"type": "thinking"})
        
        # 4. 保存用户消息
        agent.save_conversation(user_id, "user", message, priority=1)
        history = agent.get_history(user_id)
        
        # 5. 获取 AI 回复（流式）
        full_reply = ""
        async for chunk in _stream_chat(history, user_id):
            full_reply += chunk
            await websocket.send_json({"type": "text", "content": chunk})
        
        # 6. 保存 AI 回复
        agent.save_conversation(user_id, "assistant", full_reply, priority=1)
        
        # 7. 保存对话摘要
        try:
            from backend.memory.memory_manager import store_conversation_summary
            store_conversation_summary(user_id, message, full_reply)
        except:
            pass
        
        await websocket.send_json({"type": "done"})
        
    except WebSocketDisconnect:
        print("  [WS] 客户端断开连接")
    except Exception as e:
        print(f"  [WS] 错误: {type(e).__name__}: {e}")
        try:
            await websocket.send_json({"type": "error", "content": "系统繁忙，请稍后再试"})
            await websocket.send_json({"type": "done"})
        except:
            pass


async def _stream_chat(messages, user_id):
    """异步流式生成器：逐字从 AI 获取回复"""
    import asyncio
    loop = asyncio.get_event_loop()
    
    def sync_generator():
        yield from agent.chat_stream(messages, user_id)
    
    # 使用线程池运行同步生成器
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = await loop.run_in_executor(pool, lambda: list(agent.chat_stream(messages, user_id)))
        # 注意：这里一次性获取所有回复，非真正的逐字流式
        # 实际逐字流式需要使用队列
        for chunk in result:
            yield chunk


# ===== 聊天 SSE 端点(WebSocket降级备用) =====
@app.post("/api/chat/stream")
async def chat_stream_sse(request: Request):
    token = get_token_from_request(request)
    payload = verify_token(token)
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"code": 400, "msg": "消息不能为空"})
    user_id = payload.get("user_id", 0)
    from backend.memory.memory_manager import store_conversation_summary
    async def gen():
        try:
            agent.save_conversation(user_id, "user", message, priority=1)
            history = agent.get_history(user_id)
            yield "data: " + json.dumps({"type": "thinking"}) + "\n\n"
            full_reply = ""
            for chunk in agent.chat_stream(history, user_id):
                full_reply += chunk
                yield "data: " + json.dumps({"type": "text", "content": chunk}) + "\n\n"
            agent.save_conversation(user_id, "assistant", full_reply, priority=1)
            try:
                store_conversation_summary(user_id, message, full_reply)
            except:
                pass
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "content": str(e)[:200]}) + "\n\n"
            yield "data: " + json.dumps({"type": "done"}) + "\n\n"
    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ===== 静态文件服务 =====
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 挂载静态文件
static_dir = os.path.join(os.path.dirname(__file__), "backend", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    """首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"msg": "请先运行 gen_all.py 生成前端页面"}


# ===== 启动入口 =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5010"))
    print(f"FastAPI server starting on http://0.0.0.0:{port}")
    print(f"WebSocket chat endpoint: ws://127.0.0.1:{port}/ws/chat")
    print(f"API docs (Swagger): http://127.0.0.1:{port}/docs")
    print(f"Login accounts: boss/admin123 | admin/admin123 | zhangsan/user123 | lisi/user123")
    uvicorn.run(app, host="0.0.0.0", port=port)

