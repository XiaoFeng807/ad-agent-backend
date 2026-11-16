"""StorePilot FastAPI 应用。启动：python -m uvicorn fast_server:app --host 127.0.0.1 --port 5010。"""

import os, json, datetime, time
from dotenv import load_dotenv
from contextlib import asynccontextmanager, closing, contextmanager
from sqlite3 import Error as SQLiteError
from threading import Lock
from typing import Annotated

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from anyio import CancelScope, create_task_group
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError
import uvicorn

# ===== 导入项目模块 =====
from backend.config.config import settings
from backend.agent.diagnosis import DIAGNOSIS_REQUEST, NaturalDiagnosis, NaturalDiagnosisRequest
from backend.agent.group_date_change import Clarification, InvalidDateChange, InvalidPlanChange, understand_dates
from backend.agent.request_budget import ContextBudgetExceeded, ExecutionStopped, RequestBudget
from backend.agent.team_tools import TeamBudget
from backend.agent.multi_window_review import parse_review_goal
from backend.agent.product_evidence_reply import requested as product_evidence_requested
from backend.database.database import get_db, init_db, seed_data
from backend.database.diagnosis_pauses import PauseRejected, confirm_pause, group_pause_request, pause_enabled, read_pause
from backend.database.diagnosis_reviews import confirm_review, read_review
from backend.database import chat_groups, chat_storage, listing_operations, mysql_diagnosis
from backend.database.ctr_alerts import CheckRejected, check_ctr_alert
from backend.database.diagnosis_tasks import (
    CandidateRevisionChanged, InvalidNaturalSource, accept_diagnosis, list_diagnosis_tasks, read_diagnosis_task, save_diagnosis_reply,
)
from backend.auth.auth import Auth, JWTConfigurationError, set_auth_instance
from backend.agent.multi_agent import OrchestratorAgent, PipelineCoordinator, AgentPool
from backend.di import ConfigProvider, DatabaseProvider, create_tool_registry
from backend.routes.shared import init as init_shared
from backend.routes.order_routes import order_router
from backend.routes.product_evidence import product_evidence_router
from backend.observability import add_trace_step, end_trace, new_trace_id, record_request, get_system_status, start_trace

# ===== 初始化 =====
config_provider = ConfigProvider()
db_provider = DatabaseProvider()
tool_registry = create_tool_registry(db_provider)
auth_instance = Auth(db_provider)
mysql_auth_instance = Auth(DatabaseProvider(mysql_diagnosis.connect))
set_auth_instance(auth_instance)
agent_pool = AgentPool(min_size=2, max_size=10)
captcha_store = {}
app_start_time = datetime.datetime.now()
agent = agent_pool.acquire()  # 给旧接口留一个默认 agent

init_shared(db_provider, auth_instance, agent, captcha_store, app_start_time)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库"""
    if settings.DIAGNOSIS_MYSQL_ENABLED:
        await run_in_threadpool(mysql_diagnosis.validate_schema)
        if settings.MYSQL_CHAT_MEMORY_ROOT:
            def validate_memory_root():
                with closing(chat_storage.connect(read_only=True)) as conn:
                    mysql_diagnosis.chat_memory_root(conn)

            await run_in_threadpool(validate_memory_root)
        yield
        return
    init_db()
    seed_data()

    from backend.mock_ad_data.mock_ad_data import generate_mock_daily_reports as gdr
    _conn=db_provider.get_connection()
    gdr(_conn.cursor())
    _conn.commit()
    db_provider.close(_conn)
    print(" [data] daily reports updated")
    print("FastAPI server started with WebSocket support")
    yield


MYSQL_DIAGNOSIS_ROUTES = {
    "/api/captcha", "/api/login", "/api/health", "/api/chat/stream",
    "/api/diagnosis/tasks", "/api/diagnosis/tasks/{task_id}",
    "/api/diagnosis/review-proposals/{action_id}/confirm", "/api/diagnosis/reviews/{review_id}",
    "/api/knowledge/chunks/{chunk_id:path}",
}
MYSQL_CHAT_ROUTES = {
    "/", "/api/chat/history", "/api/chat/conversations",
    "/api/chat/conversations/{conversation_id}/history",
    "/api/chat/conversations/{conversation_id}/archive",
}
MYSQL_PAUSE_ROUTES = {"/", "/api/diagnosis/pause-actions/{action_id}/confirm", "/api/diagnosis/pause-actions/{action_id}"}


async def require_rehearsal_route(connection: HTTPConnection):
    path = getattr(connection.scope.get("route"), "path", None)
    if (settings.DIAGNOSIS_MYSQL_ENABLED and path not in MYSQL_DIAGNOSIS_ROUTES
            and not (mysql_diagnosis.chat_enabled() and path in MYSQL_CHAT_ROUTES)
            and not (mysql_diagnosis.pause_enabled() and path in MYSQL_PAUSE_ROUTES)):
        raise HTTPException(status_code=404, detail="此接口未纳入 MySQL 诊断演练")


# ===== 创建 FastAPI 应用 =====
app = FastAPI(
    title="StorePilot Agent API",
    description="广告投放管理 + AI 智能分析系统",
    version="3.0.0",
    lifespan=lifespan,
    dependencies=[Depends(require_rehearsal_route)],
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


class PlanCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    plan_name: str = ""
    platform: str = "Google Ads"
    daily_budget: FiniteFloat = 300.0


PlanPathId = Annotated[int, Path(ge=-(2**63), le=2**63 - 1)]


# ===== 认证辅助 =====
def get_token_from_request(request):
    """从请求头中提取 Bearer Token"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""

def verify_token(token: str = ""):
    """验证 JWT Token"""
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = request_auth().validate_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期，请重新登录")
    return payload


def authenticated_user_id(request: Request):
    return verify_token(get_token_from_request(request))["user_id"]


def request_auth():
    return mysql_auth_instance if settings.DIAGNOSIS_MYSQL_ENABLED else auth_instance


def session_user(request, conn):
    payload = request_auth().validate_token(get_token_from_request(request), connection=conn)
    if payload is None or conn.execute(
        "SELECT 1 FROM users WHERE id=?", (payload["user_id"],),
    ).fetchone() is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期，请重新登录")
    return payload["user_id"]


app.include_router(order_router(session_user))
app.include_router(product_evidence_router(session_user))


@app.exception_handler(mysql_diagnosis.MySQLStorageError)
async def diagnosis_storage_unavailable(request: Request, error: mysql_diagnosis.MySQLStorageError):
    return JSONResponse({"detail": "MySQL 诊断存储暂不可用"}, status_code=503)


@contextmanager
def diagnosis_history_connection(request):
    connect = mysql_diagnosis.connect if settings.DIAGNOSIS_MYSQL_ENABLED else get_db
    with closing(connect(read_only=True)) as conn, conn:
        conn.execute("BEGIN")
        yield conn, session_user(request, conn)


@app.get("/api/diagnosis/tasks")
def get_diagnosis_tasks(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(pattern=r"^[0-9]{1,19}:.{1,128}$")] = None,
):
    with diagnosis_history_connection(request) as (conn, user_id):
        before = (None, None)
        if cursor is not None:
            turn_id, task_id = cursor.split(":", 1)
            if int(turn_id) > 2**63 - 1:
                raise HTTPException(status_code=422, detail="诊断任务游标无效")
            before = (int(turn_id), task_id)
        data = list_diagnosis_tasks(conn, user_id, limit, before)
    return JSONResponse({"code": 200, "data": data}, headers={"Cache-Control": "no-store"})


@app.get("/api/diagnosis/tasks/{task_id}")
def get_diagnosis_task(task_id: str, request: Request):
    with diagnosis_history_connection(request) as (conn, user_id):
        data = read_diagnosis_task(conn, user_id, task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    return JSONResponse({"code": 200, "data": data}, headers={"Cache-Control": "no-store"})


@app.post("/api/diagnosis/review-proposals/{action_id}/confirm")
def confirm_diagnosis_review(action_id: str, user_id: int = Depends(authenticated_user_id)):
    try:
        receipt = confirm_review(user_id, action_id)
    except CandidateRevisionChanged as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except (SQLiteError, mysql_diagnosis.MySQLStorageError):
        raise HTTPException(status_code=503, detail="复核单保存失败，请重试确认") from None
    if receipt is None:
        raise HTTPException(status_code=404, detail="复核提案不存在")
    return {"code": 200, "data": receipt}


@app.get("/api/diagnosis/reviews/{review_id}")
def get_diagnosis_review(review_id: str, user_id: int = Depends(authenticated_user_id)):
    receipt = read_review(user_id, review_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="复核单不存在")
    return {"code": 200, "data": receipt}


PAUSE_ERRORS = {
    "pause_not_found": (404, "沙箱暂停动作或回执不存在"),
    "pause_session_invalid": (401, "Token 无效或已过期，请重新登录"),
    "pause_status_changed": (409, "目标状态已变化，请重新诊断"),
    "pause_storage_error": (503, "沙箱暂停暂不可用，请重试"),
    "pause_sandbox_disabled": (404, "工作群 SQLite 暂停沙箱未启用"),
}


def _diagnosis_pause_request(action_id, request, *, confirm, group_scope=None):
    if group_scope is None and not pause_enabled():
        raise HTTPException(status_code=404, detail="沙箱暂停动作或回执不存在")
    token = get_token_from_request(request)
    trace_id = start_trace(source="chat_group_pause" if group_scope else "diagnosis_pause")
    operation = "confirm_pause" if confirm else "read_pause_status"
    status = "error"
    try:
        validate_token = request_auth().validate_token
        if group_scope is not None:
            result = group_pause_request(*group_scope, action_id, token, validate_token, trace_id, confirm=confirm)
        else:
            verify_token(token)
            result = (confirm_pause(action_id, token, validate_token, trace_id) if confirm
                      else read_pause(action_id, token, validate_token))
        if confirm:
            receipt = result
            operation = "pause_committed" if receipt["execution_trace_id"] == trace_id else "pause_replayed"
            observed = receipt["committed_status"]
        else:
            receipt, observed = result["receipt"], result["actual_status"]
        add_trace_step(trace_id, "sandbox_action", operation,
                       detail=f"action={receipt['action_id']} source={receipt['source_trace_id']} "
                              f"receipt={receipt['receipt_id']} status={observed}")
        status = "completed"
        body = {"code": 200, "data": result}
        if group_scope is not None:
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        return body
    except (PauseRejected, SQLiteError, mysql_diagnosis.MySQLStorageError) as error:
        code = error.args[0] if isinstance(error, PauseRejected) else "pause_storage_error"
        add_trace_step(trace_id, "sandbox_action", operation, status="error", error_code=code)
        http_status, detail = PAUSE_ERRORS[code]
        raise HTTPException(status_code=http_status, detail=detail) from None
    finally:
        end_trace(trace_id, status)


@app.post("/api/diagnosis/pause-actions/{action_id}/confirm")
def confirm_diagnosis_pause(action_id: str, request: Request):
    return _diagnosis_pause_request(action_id, request, confirm=True)


@app.get("/api/diagnosis/pause-actions/{action_id}")
def read_diagnosis_pause(action_id: str, request: Request):
    return _diagnosis_pause_request(action_id, request, confirm=False)


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
    # 验证验证码
    if req.captcha_id in captcha_store:
        expected = captcha_store.pop(req.captcha_id)
        if req.captcha_text.lower().strip() != expected:
            return {"code": 400, "msg": "验证码错误"}
    elif req.captcha_id:
        return {"code": 400, "msg": "验证码已过期"}
    
    # 验证用户名密码（复用 Auth 的 login 方法）
    try:
        result = await run_in_threadpool(request_auth().login, req.username, req.password)
    except JWTConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
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
# ===== 知识库 API =====

@app.get("/api/knowledge/stats")
async def knowledge_stats():
    """知识库统计"""
    from backend.rag.rag_knowledge import RAGKnowledge
    try:
        rag = RAGKnowledge()
        data = rag.collection.get()
        docs = data["documents"] if data and data["documents"] else []
        metas = data["metadatas"] if data and data["metadatas"] else []
        sources = set()
        categories = set()
        for m in metas:
            if m:
                if m.get("source"): sources.add(m["source"])
                if m.get("category"): categories.add(m["category"])
        return {"total": len(docs), "sources": len(sources), "categories": len(categories)}
    except:
        return {"total": 0, "sources": 0, "categories": 0}

@app.get("/api/knowledge/all")
async def knowledge_all():
    """获取所有知识条目"""
    from backend.rag.rag_knowledge import RAGKnowledge
    try:
        rag = RAGKnowledge()
        data = rag.collection.get()
        items = []
        if data and data["documents"]:
            for i, doc in enumerate(data["documents"]):
                meta = data["metadatas"][i] if data["metadatas"] and i < len(data["metadatas"]) else {}
                items.append({
                    "text": doc[:300] + ("..." if len(doc) > 300 else ""),
                    "source": meta.get("source", "未知来源"),
                    "category": meta.get("category", "未分类")
                })
        return {"items": items}
    except:
        return {"items": []}

@app.get("/api/knowledge/search")
def knowledge_search(q: str = ""):
    """搜索知识库"""
    if not q:
        return {"results": [], "sources": [], "evidence": []}
    from backend.rag.rag_knowledge import get_rag_knowledge
    try:
        result = get_rag_knowledge().generate(q, top_k=5)
        if result and result.get("context"):
            blocks = result["context"].split("\n\n---\n\n")
            return {"results": blocks, "sources": result["sources"], "evidence": result["evidence"]}
        return {"results": [], "sources": [], "evidence": []}
    except Exception as e:
        return {"results": [], "sources": [], "evidence": [], "error": str(e)}

@app.get("/api/knowledge/chunks/{chunk_id:path}")
def knowledge_chunk(chunk_id: str):
    """按分块 ID 读取本知识库保存的原文。"""
    from chromadb.errors import NotFoundError
    from backend.rag.rag_knowledge import RAGKnowledge

    try:
        with closing(RAGKnowledge(create=False)) as rag:
            chunk = rag.get_chunk(chunk_id) if chunk_id else None
    except NotFoundError:
        chunk = None
    if chunk is None:
        raise HTTPException(status_code=404, detail="知识分块不存在")
    return chunk


@app.get("/knowledge")
async def serve_knowledge_page():
    """知识库前端页面"""
    static_dir = os.path.join(os.path.dirname(__file__), "backend", "static")
    kb_path = os.path.join(static_dir, "knowledge_base.html")
    if os.path.exists(kb_path):
        return FileResponse(kb_path)
    return {"msg": "页面未找到"}


@app.get("/api/daily_reports")
def daily_reports(
    days: Annotated[int, Query(ge=-(2**63), le=2**63 - 1)] = 7,
    user_id: int = Depends(authenticated_user_id),
):
    """每日趋势"""
    from backend.tools.tools import get_daily_trend
    data = get_daily_trend(days=days, user_id=user_id)
    return {"code": 200, "data": data}


@app.get("/api/alerts")
async def alerts(request: Request):
    """告警列表"""
    token = get_token_from_request(request)
    payload = verify_token(token)
    from backend.tools.tools import get_alerts
    data = get_alerts(user_id=payload.get("user_id", 0))
    return {"code": 200, "data": data}


@app.post("/api/plans/{plan_id}/toggle")
def toggle_plan(plan_id: PlanPathId, user_id: int = Depends(authenticated_user_id)):
    """切换计划状态（暂停/启用）"""
    from backend.tools.tools import toggle_plan_status
    result = toggle_plan_status(plan_id, user_id=user_id)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["message"])
    return {"code": 200, "data": result}


@app.post("/api/plans/{plan_id}/ctr-alert-check")
def plan_ctr_alert_check(plan_id: PlanPathId, request: Request):
    try:
        data = check_ctr_alert(plan_id, get_token_from_request(request), request_auth().validate_token)
    except CheckRejected as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from None
    return JSONResponse({"code": 200, "data": data}, headers={"Cache-Control": "no-store"})


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: PlanPathId, user_id: int = Depends(authenticated_user_id)):
    """删除计划"""
    from backend.database.database import get_db
    with closing(get_db()) as conn, conn:
        deleted = conn.execute("DELETE FROM ad_plans WHERE id=? AND user_id=?", (plan_id, user_id)).rowcount
        if not deleted:
            raise HTTPException(status_code=404, detail="计划不存在")
    return {"code": 200, "msg": "已删除"}


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
def accounts(user_id: int = Depends(authenticated_user_id)):
    """获取当前用户的广告账户"""
    from backend.database.database import get_db
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM ad_accounts WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    data = [dict(r) for r in rows]
    return {"code": 200, "data": data}

@app.post("/api/plans")
async def create_plan(request: Request, user_id: int = Depends(authenticated_user_id)):
    """创建新投放计划"""
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="计划参数必须是有效 JSON 对象") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="计划参数必须是有效 JSON 对象")
    if "user_id" in body:
        raise HTTPException(status_code=400, detail="计划属主由当前会话确定")
    if isinstance(body.get("daily_budget"), bool):
        raise HTTPException(status_code=400, detail="日预算必须是有限数值")
    try:
        plan = PlanCreateRequest.model_validate(body)
    except ValidationError:
        raise HTTPException(status_code=400, detail="计划名称、平台或日预算格式无效") from None
    if not plan.plan_name:
        return {"code": 400, "msg": "请输入计划名称"}
    from backend.database.database import get_db
    with closing(get_db()) as conn, conn:
        conn.execute(
            "INSERT INTO ad_plans (plan_name, platform, daily_budget, status, user_id, created_at) VALUES (?,?,?,?,?,?)",
            (plan.plan_name, plan.platform, plan.daily_budget, "active", user_id,
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return {"code": 200, "msg": "创建成功"}


@app.get("/api/plans")
def plans(user_id: int = Depends(authenticated_user_id)):
    """获取当前用户的广告计划"""
    from backend.tools.tools import get_plans_summary
    data = get_plans_summary(user_id=user_id)
    return {"code": 200, "data": data}

@app.get("/api/materials")
def materials(user_id: int = Depends(authenticated_user_id)):
    """获取当前用户的素材及同属主的关联计划。"""
    with closing(db_provider.get_connection()) as conn:
        rows = conn.execute(
            "SELECT m.id, m.name, m.type, m.url, p.id AS plan_id, p.plan_name, m.user_id, m.created_at "
            "FROM materials m LEFT JOIN ad_plans p "
            "ON p.id=m.plan_id AND m.plan_id<>0 AND p.user_id=m.user_id "
            "WHERE m.user_id=? ORDER BY m.id DESC",
            (user_id,),
        ).fetchall()
    return {"code": 200, "data": [dict(r) for r in rows]}

def current_user_role(conn, user_id):
    requester = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if requester is None:
        raise HTTPException(status_code=401, detail="用户不存在，请重新登录")
    return requester["role"]


@app.get("/api/users")
def users(user_id: int = Depends(authenticated_user_id)):
    """管理员和老板按数据库中的当前角色读取用户目录。"""
    with closing(db_provider.get_connection()) as conn, conn:
        conn.execute("BEGIN")
        if current_user_role(conn, user_id) not in ("admin", "boss"):
            raise HTTPException(status_code=403, detail="无权查看用户目录")
        rows = conn.execute("SELECT id, username, role FROM users ORDER BY id").fetchall()
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/logs")
def logs(user_id: int = Depends(authenticated_user_id)):
    """获取操作日志"""
    with closing(db_provider.get_connection()) as conn, conn:
        conn.execute("BEGIN")
        role = current_user_role(conn, user_id)
        if role == "boss":
            rows = conn.execute("SELECT * FROM operation_logs ORDER BY id DESC LIMIT 50").fetchall()
        elif role == "admin":
            rows = conn.execute(
                "SELECT l.* FROM operation_logs l JOIN users u ON l.user_id=u.id "
                "WHERE u.role IN ('admin','user') ORDER BY l.id DESC LIMIT 50"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM operation_logs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,),
            ).fetchall()
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/decisions")
def decisions(user_id: int = Depends(authenticated_user_id)):
    """获取当前用户最近的决策记录。"""
    with closing(db_provider.get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM decision_logs WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,),
        ).fetchall()
    return {"code": 200, "data": [dict(r) for r in rows]}

@app.get("/api/timeline")
def timeline(user_id: int = Depends(authenticated_user_id)):
    """获取活动时间线"""
    from backend.tools.tools import get_activity_timeline
    data = get_activity_timeline(user_id=user_id)
    return {"code": 200, "data": data}

@app.get("/api/chat/history")
def chat_history(request: Request, exclude_chat_view_id: str | None = None):
    """获取聊天历史记录"""
    token = get_token_from_request(request)
    payload = verify_token(token)
    history, _ = agent.get_optimized_context(
        payload.get("user_id", 0), exclude_chat_view_id=exclude_chat_view_id,
    )
    return {"code": 200, "data": history}


class CreateChatGroup(BaseModel):
    title: str = "新工作群"


class ArchiveChatGroup(BaseModel):
    archived: bool


class GroupChangePreview(BaseModel, extra="forbid"):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=2000)


class GroupChangeConfirmation(BaseModel, extra="forbid"):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class StopChatGroupRun(BaseModel, extra="forbid"):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")


_group_executions = {}
_group_execution_lock = Lock()


def _register_group_execution(group_run, execution):
    with _group_execution_lock:
        status = group_run.status()
        if status in {"stopped", "superseded"}:
            execution.cancel(f"run_{status}")
        _group_executions[group_run.run_id] = execution


def _unregister_group_execution(run_id):
    with _group_execution_lock:
        _group_executions.pop(run_id, None)


def _confirm_group_change(user_id, conversation_id, confirmation, *, dates_only=False):
    def confirm():
        return chat_groups.confirm_change(user_id, conversation_id, confirmation.run_id,
                                          confirmation.proposal_id, new_trace_id(), dates_only=dates_only)

    with _group_execution_lock:
        execution = _group_executions.get(confirmation.run_id)
        if execution is not None:
            return execution.cancel_after(confirm, "run_superseded", committed=lambda result: result[0] is not None)
        return confirm()


def _stop_group_run(user_id, conversation_id, run_id):
    def commit():
        return chat_groups.stop(user_id, conversation_id, run_id)

    with _group_execution_lock:
        execution = _group_executions.get(run_id)
        return execution.cancel_after(commit, "run_stopped") if execution is not None else commit()


@contextmanager
def group_request():
    try:
        yield
    except (chat_groups.GroupNotFound, chat_groups.GroupStorageUnavailable):
        raise HTTPException(status_code=404, detail="工作群不存在") from None
    except chat_groups.GroupConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except (InvalidDateChange, InvalidPlanChange) as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@app.get("/api/chat/conversations")
def list_chat_groups(user_id: int = Depends(authenticated_user_id)):
    with group_request():
        return {"code": 200, "data": chat_groups.list_groups(user_id)}


@app.post("/api/chat/conversations", status_code=201)
def create_chat_group(req: CreateChatGroup, user_id: int = Depends(authenticated_user_id)):
    title = req.title.strip()
    if not title or len(title) > 80:
        raise HTTPException(status_code=422, detail="工作群名称须为 1–80 字")
    with group_request():
        return {"code": 201, "data": chat_groups.create(user_id, title)}


@app.get("/api/chat/conversations/{conversation_id}/history")
def chat_group_history(conversation_id: str, user_id: int = Depends(authenticated_user_id)):
    with group_request():
        return {"code": 200, "data": chat_groups.history(user_id, conversation_id)}


@app.post("/api/chat/conversations/{conversation_id}/stop")
def stop_chat_group_run(conversation_id: str, req: StopChatGroupRun,
                        user_id: int = Depends(authenticated_user_id)):
    with group_request():
        return {"code": 200, "data": _stop_group_run(user_id, conversation_id, req.run_id)}


@app.get("/api/chat/conversations/{conversation_id}/runs/{run_id}/evidence")
def chat_group_reply_evidence(conversation_id: str, run_id: str, user_id: int = Depends(authenticated_user_id)):
    with group_request():
        return {"code": 200, "data": chat_groups.reply_evidence(user_id, conversation_id, run_id)}


@app.post("/api/chat/conversations/{conversation_id}/runs/{run_id}/pause-actions/{action_id}/confirm")
def confirm_chat_group_pause(conversation_id: str, run_id: str, action_id: str, request: Request):
    return _diagnosis_pause_request(action_id, request, confirm=True, group_scope=(conversation_id, run_id))


@app.get("/api/chat/conversations/{conversation_id}/runs/{run_id}/pause-actions/{action_id}")
def read_chat_group_pause(conversation_id: str, run_id: str, action_id: str, request: Request):
    return _diagnosis_pause_request(action_id, request, confirm=False, group_scope=(conversation_id, run_id))


@app.get("/api/chat/conversations/{conversation_id}/runs/{run_id}/listing-actions/{action_id}")
def read_chat_group_listing(conversation_id: str, run_id: str, action_id: str, request: Request, review: bool = False):
    try:
        with closing(get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            result = listing_operations.read_action(
                conn, session_user(request, conn), conversation_id, run_id, action_id, review=review,
            )
    except listing_operations.ListingNotFound:
        raise HTTPException(404, "商品提案不存在") from None
    except listing_operations.ListingConflict:
        raise HTTPException(409, "商品提案与原轮依据不一致") from None
    return JSONResponse({"code": 200, "data": result}, headers={"Cache-Control": "no-store"})


@app.post("/api/chat/conversations/{conversation_id}/runs/{run_id}/listing-actions/{action_id}/confirm")
def confirm_chat_group_listing(conversation_id: str, run_id: str, action_id: str, request: Request):
    trace_id = start_trace(source="listing_approval")
    status = "error"
    try:
        with closing(get_db()) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            result = listing_operations.approve(conn, session_user(request, conn), conversation_id, run_id, action_id, trace_id)
        receipt = result["receipt"]
        add_trace_step(trace_id, "listing_action", "applied" if receipt["execution_trace_id"] == trace_id else "replayed",
                       evidence={"action_id": action_id, "run_id": run_id, "receipt_id": receipt["receipt_id"]})
        status = "completed"
    except listing_operations.ListingNotFound:
        add_trace_step(trace_id, "listing_action", "blocked", error_code="listing_not_found",
                       evidence={"run_id": run_id, "action_id": action_id})
        raise HTTPException(404, "商品提案不存在") from None
    except listing_operations.ListingConflict as error:
        add_trace_step(trace_id, "listing_action", "blocked", error_code=str(error),
                       evidence={"run_id": run_id, "action_id": action_id})
        raise HTTPException(409, "商品、证据或工作群状态已变化，请重新生成提案") from None
    except SQLiteError:
        raise HTTPException(503, "商品修改未提交，请重试") from None
    finally:
        end_trace(trace_id, status)
    return JSONResponse({"code": 200, "data": result}, headers={"Cache-Control": "no-store"})


@app.patch("/api/chat/conversations/{conversation_id}/archive")
def archive_chat_group(conversation_id: str, req: ArchiveChatGroup, user_id: int = Depends(authenticated_user_id)):
    with group_request():
        return {"code": 200, "data": chat_groups.set_archived(user_id, conversation_id, req.archived)}


@app.post("/api/chat/conversations/{conversation_id}/date-changes")
def preview_chat_group_dates(conversation_id: str, req: GroupChangePreview,
                             user_id: int = Depends(authenticated_user_id)):
    return _preview_group_change(conversation_id, req, user_id, allow_plans=False)


@app.post("/api/chat/conversations/{conversation_id}/changes")
def preview_chat_group_change(conversation_id: str, req: GroupChangePreview,
                              user_id: int = Depends(authenticated_user_id)):
    return _preview_group_change(conversation_id, req, user_id, allow_plans=True)


def _preview_group_change(conversation_id, req, user_id, *, allow_plans):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="请输入修改要求")
    with group_request():
        goal, old_trace_id = chat_groups.change_scope(user_id, conversation_id, req.run_id)
        trace_id = start_trace(source="group_change" if allow_plans else "group_date_change")
        status = "error"
        add_trace_step(trace_id, "chat_group", "preview", runtime_call_id=req.run_id,
                       evidence={"conversation_id": conversation_id, "old_trace_id": old_trace_id})
        try:
            try:
                change = understand_dates(goal, message, trace_id, allow_plans=allow_plans)
            except (InvalidDateChange, InvalidPlanChange):
                raise
            except Exception:
                raise HTTPException(status_code=503, detail="暂时无法理解修改要求，请稍后重试") from None
            if isinstance(change, Clarification):
                chat_groups.change_scope(user_id, conversation_id, req.run_id)
                status = "clarification"
                return {"code": 200, "data": {"run_id": req.run_id, **change.model_dump(), "trace_id": trace_id}}
            preview = chat_groups.propose_change(user_id, conversation_id, req.run_id, message, change, trace_id)
            result = {**preview, "run_id": req.run_id, "trace_id": trace_id}
            add_trace_step(trace_id, "chat_group", "proposed", evidence=result)
            status = "completed"
            return {"code": 200, "data": result}
        finally:
            end_trace(trace_id, status)

@app.get("/api/health")
async def health():
    """健康检查接口"""
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
        await websocket.send_json({"type": "thinking", "content": "🤔 思考中..."})
        
        # 4. 保存用户消息
        data_keywords = ["数据", "今天", "报告", "ROAS", "花费", "点击", "转化", "趋势", "对比", "分析", "计划", "广告", "预算", "效果", "查看", "多少", "怎么样"]
        is_data_query = any(kw in message for kw in data_keywords)
        msg_priority = 2 if is_data_query else 1
        ws_agent = agent_pool.acquire()
        ws_agent.save_conversation(user_id, "user", message, priority=msg_priority)
        history = ws_agent.get_history(user_id)
        
        # 5. 获取 AI 回复（流式）
        full_reply = ""
        async for chunk in _stream_chat(history, user_id):
            full_reply += chunk
            await websocket.send_json({"type": "text", "content": chunk})
        
        # 6. 保存 AI 回复
        ws_agent.save_conversation(user_id, "assistant", full_reply, priority=1)
        
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
        if ws_agent:
            agent_pool.release(ws_agent)
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
class ChatStreamingResponse(StreamingResponse):
    def __init__(self, *args, execution=None, group_run=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution = execution
        self.group_run = group_run

    async def __call__(self, scope, receive, send):
        try:
            await self._stream_and_listen(receive, send)
        finally:
            if self.group_run is not None:
                with CancelScope(shield=True):
                    await run_in_threadpool(self.group_run.finish, "interrupted")

    async def _stream_and_listen(self, receive, send):
        async with create_task_group() as tasks:
            async def stream():
                try:
                    await self.stream_response(send)
                except OSError:
                    if self.execution is not None:
                        self.execution.cancel()
                finally:
                    await self.body_iterator.aclose()
                    tasks.cancel_scope.cancel()

            # 让同步工具退出后再归还 Agent，避免另一请求复用运行中的实例。
            tasks.start_soon(stream)
            try:
                await self.listen_for_disconnect(receive)
            finally:
                if self.execution is not None:
                    self.execution.cancel()
                tasks.cancel_scope.cancel()


@app.post("/api/chat/stream")
async def chat_stream_sse(request: Request):
    """AI 聊天流式接口（SSE），支持 ReAct 思考可视化和流式输出"""
    token = get_token_from_request(request)
    payload = await run_in_threadpool(verify_token, token)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象")
    chat_view_id = body.get("chat_view_id")
    conversation_id = body.get("conversation_id")
    user_id = payload["user_id"]
    diagnosis = None
    diagnosis_trace_id = None
    confirmation = None
    change = None
    listing_mode = body.get("listing_operations", False)
    if "listing_operations" in body and (type(listing_mode) is not bool or "conversation_id" not in body
                                         or {"diagnosis", "change", "date_change"} & body.keys()):
        raise HTTPException(422, "商品运营须在独立的工作群消息中启用")
    if listing_mode and settings.DIAGNOSIS_MYSQL_ENABLED:
        raise HTTPException(404, "商品运营使用本地 SQLite 测试数据")
    change_fields = {"date_change", "change"} & body.keys()
    if change_fields:
        if len(change_fields) != 1 or "diagnosis" in body or "conversation_id" not in body:
            raise HTTPException(status_code=422, detail="请在工作群确认一项修改要求")
        if settings.DIAGNOSIS_MYSQL_ENABLED:
            raise HTTPException(status_code=404, detail="此入口仅支持 SQLite 工作群修改要求")
        change_field, = change_fields
        try:
            confirmation = GroupChangeConfirmation.model_validate(body[change_field])
        except ValidationError:
            raise HTTPException(status_code=422, detail="确认须提供本轮和提议 ID") from None
    if "diagnosis" in body:
        if "conversation_id" in body:
            raise HTTPException(status_code=422, detail="诊断任务使用独立会话")
        message = body.get("message", "请按本轮日期窗口复盘计划并应用指定规则。")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=422, detail="消息必须是非空字符串")
        message = message.strip()
        try:
            params = DIAGNOSIS_REQUEST.validate_python(body["diagnosis"])
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_context=False, include_input=False, include_url=False),
            ) from None
        if isinstance(params, NaturalDiagnosisRequest) and ("message" not in body or len(message) > 2000):
            raise HTTPException(status_code=422, detail="请输入不超过 2000 字的诊断需求")
        diagnosis_trace_id = new_trace_id()
        try:
            diagnosis = await run_in_threadpool(accept_diagnosis, user_id, params, message, trace_id=diagnosis_trace_id)
        except InvalidNaturalSource:
            raise HTTPException(status_code=400, detail="来源轮次不存在、未提交或没有可用比较结果") from None
        if diagnosis is None:
            raise HTTPException(status_code=404, detail="诊断任务不存在")
    elif confirmation is None:
        if settings.DIAGNOSIS_MYSQL_ENABLED and not mysql_diagnosis.chat_enabled():
            raise HTTPException(status_code=404, detail="此接口仅接受 MySQL 诊断演练请求")
        message = body.get("message", "")
        if not isinstance(message, str):
            raise HTTPException(status_code=422, detail="消息必须是字符串")
        message = message.strip()
        if not message:
            return JSONResponse({"code": 400, "msg": "消息不能为空"})
        # 智能优先级：数据查询类 = 重要，闲聊 = 普通
        data_keywords = ["数据", "今天", "报告", "ROAS", "花费", "点击", "转化", "趋势", "对比", "分析", "计划", "广告", "预算", "效果", "查看", "多少", "怎么样"]
        msg_priority = 2 if any(kw in message for kw in data_keywords) else 1
    from backend.memory.memory_manager import short_term_from_history, store_conversation_summary
    from backend.memory.conversation_memory import store_conversation
    group_run = None
    if "conversation_id" in body:
        if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 32:
            raise HTTPException(status_code=404, detail="工作群不存在")
        with group_request():
            if confirmation is None:
                group_run, group_context = await run_in_threadpool(
                    chat_groups.accept, user_id, conversation_id, new_trace_id(), message, msg_priority,
                )
            else:
                group_run, group_context, change = await run_in_threadpool(
                    _confirm_group_change, user_id, conversation_id, confirmation, dates_only=change_field == "date_change",
                )
                if group_run is None:
                    async def replay():
                        yield "data: " + json.dumps({
                            "type": "done", "conversation_id": conversation_id, "already_confirmed": True, **change,
                        }) + "\n\n"

                    return StreamingResponse(replay(), media_type="text/event-stream")
                message = group_context[-1]["content"]

    execution = None
    if diagnosis is None:
        if group_run and not change and not settings.DIAGNOSIS_MYSQL_ENABLED and "listing_operations" not in body:
            from backend.agent import listing_run, olist_sales

            listing_mode = (not product_evidence_requested(message)
                            and not olist_sales.requested(message, group_run.product_context)
                            and await run_in_threadpool(listing_run.requested, message, user_id, group_run.product_context))
        review_goal = parse_review_goal(message)[1] if group_run else None
        # Five evidence workers can each need a tool-call and a summary turn.
        # Keep room for the alert, dispatch, append and independent reviewer.
        execution = TeamBudget(model_limit=16) if review_goal and review_goal.independent else TeamBudget()

    product_evidence_mode = (diagnosis is None and not listing_mode and not change
                             and not settings.DIAGNOSIS_MYSQL_ENABLED and product_evidence_requested(message))
    if product_evidence_mode and execution is None:
        execution = TeamBudget()

    def product_evidence_session(conn):
        current = auth_instance.validate_token(token, connection=conn)
        return current is not None and current["user_id"] == user_id

    async def gen():
        trace_id = start_trace(source="chat_sse", trace_id=group_run.trace_id if group_run else diagnosis_trace_id)
        budget = RequestBudget(trace_id)
        budget.execution = execution
        ids = {"trace_id": trace_id}
        if group_run:
            ids.update(conversation_id=group_run.conversation_id, run_id=group_run.run_id)
            add_trace_step(trace_id, "chat_group", "accepted", detail=group_run.conversation_id,
                           runtime_call_id=group_run.run_id)
            if change:
                add_trace_step(trace_id, "chat_group", "replacement_started", evidence=change)
        if diagnosis:
            ids["task_id"] = diagnosis.snapshot.task_id
        natural = diagnosis is not None and isinstance(diagnosis.snapshot, NaturalDiagnosis)
        if natural:
            ids["turn_id"] = diagnosis.turn_id
        terminal_status = "interrupted"
        full_reply = ""
        evidence = {}
        group_evidence = None
        deferred_reply = None
        product_evidence_receipts = []
        proposals = {}
        sess_agent = None
        conversation_snapshot = None
        operation = "acquire_agent"
        save_started = None
        pool = agent_pool
        try:
            if group_run and not settings.DIAGNOSIS_MYSQL_ENABLED:
                await run_in_threadpool(_register_group_execution, group_run, execution)
                if code := execution.stop_code():
                    raise ExecutionStopped(code)
            sess_agent = await pool.acquire_async()
            if diagnosis:
                operation = "load_context"
                history = diagnosis.history
            elif group_run:
                message_id, history = group_run.message_id, group_context
            else:
                operation = "save_user_message"
                if mysql_diagnosis.chat_enabled():
                    conversation_snapshot = await run_in_threadpool(chat_storage.MySQLChatSnapshot)
                    message_id, history = await run_in_threadpool(
                        conversation_snapshot.accept, user_id, message, msg_priority, chat_view_id,
                    )
                else:
                    message_id = await run_in_threadpool(
                        sess_agent.save_conversation, user_id, "user", message,
                        priority=msg_priority, chat_view_id=chat_view_id,
                    )
                    operation = "load_context"
                    history, _ = await run_in_threadpool(
                        sess_agent.get_optimized_context, user_id, through_message_id=message_id,
                    )
            # 末条是当前用户消息，短期上下文只取此前的消息。
            short_term_context = short_term_from_history(history[:-1])
            yield "data: " + json.dumps({"type": "thinking", "content": "🤔 思考中...", **ids}) + "\n\n"
            operation = "generate_reply"
            with closing(sess_agent.chat_stream(
                [{"role": "user", "content": message}] if product_evidence_mode else history,
                user_id, trace_id=trace_id, raise_on_error=True,
                short_term_context=short_term_context, diagnosis=diagnosis.snapshot if diagnosis else None,
                through_message_id=message_id if diagnosis is None else None, budget=budget,
                **({"conversation_snapshot": conversation_snapshot} if conversation_snapshot is not None else {}),
                **({"group_run": group_run} if group_run is not None else {}),
                **({"listing_operations": True} if listing_mode else {}),
                **({"product_evidence_sink": product_evidence_receipts.append,
                    "product_evidence_session": product_evidence_session} if product_evidence_mode else {}),
            )) as stream:
                async for chunk in iterate_in_threadpool(stream):
                    try:
                        event = json.loads(chunk)
                    except json.JSONDecodeError:
                        event = None
                    if not isinstance(event, dict) or "type" not in event:
                        event = {"type": "text", "content": chunk}
                    if event["type"] == "done":
                        break
                    if event["type"] == "error":
                        raise RuntimeError("agent_stream_error")
                    if event["type"] == "text":
                        full_reply += event.get("content", "")
                        if group_run and "group_evidence" in event:
                            group_evidence = event.pop("group_evidence")
                        if diagnosis:
                            evidence.update({key: event[key] for key in (
                                "citations", "evidence_gaps", "metrics", "calculation", "rule_evaluation",
                                "plan_id", "rule_id", "baseline", "current", "comparison", "candidate_scope",
                            ) if key in event})
                        if natural or settings.DIAGNOSIS_MYSQL_ENABLED or group_run or product_evidence_mode:
                            if "candidate_revision" in event:
                                evidence["candidate_revision"] = event.pop("candidate_revision")
                            deferred_reply = event
                            continue
                    yield "data: " + json.dumps({**event, **(ids if group_run else {})}, ensure_ascii=False) + "\n\n"
            operation = "save_reply"
            save_started = time.perf_counter()
            if diagnosis:
                proposals = await run_in_threadpool(
                    save_diagnosis_reply, user_id, diagnosis, full_reply, trace_id, evidence,
                )
                add_trace_step(trace_id, "persistence", operation, duration=time.perf_counter() - save_started)
                if pause := proposals.get("pause_proposal"):
                    add_trace_step(trace_id, "sandbox_action", "pause_proposed",
                                   detail=f"action={pause['action_id']} plan={pause['target']['plan_id']} status=active")
                if deferred_reply is not None:
                    yield "data: " + json.dumps(deferred_reply, ensure_ascii=False) + "\n\n"
            else:
                reply_priority = 2 if any(kw in full_reply for kw in ["元", "ROAS", "点击", "转化", "%", "数据"]) else 1

                def save_reply_and_index():
                    if product_evidence_mode and (code := execution.stop_code()):
                        raise ExecutionStopped(code)
                    reply = full_reply
                    validation = {}
                    citation_ids = []
                    if product_evidence_receipts:
                        reply, validate, citation_ids = product_evidence_receipts[0]
                        validation = {"validate": validate}
                    if group_run:
                        assistant_message_id, reply = group_run.save_reply(
                            reply, reply_priority, evidence=group_evidence, **validation,
                        )
                    elif validation:
                        assistant_message_id = chat_storage.save_message(
                            user_id, "assistant", reply, reply_priority, chat_view_id,
                            user_message_id=message_id, **validation,
                        )
                    else:
                        assistant_message_id = sess_agent.save_conversation(
                            user_id, "assistant", reply, priority=reply_priority, chat_view_id=chat_view_id,
                            user_message_id=message_id,
                        )
                    add_trace_step(trace_id, "persistence", operation, duration=time.perf_counter() - save_started,
                                   evidence={"final_citation_ids": citation_ids} if product_evidence_mode else None)
                    if product_evidence_mode:
                        return full_reply
                    try:
                        store_conversation(
                            user_id, message, reply,
                            user_message_id=message_id, assistant_message_id=assistant_message_id,
                            **({"conversation_id": group_run.conversation_id} if group_run else {}),
                        )
                    except Exception:
                        add_trace_step(trace_id, "memory", "store_conversation", status="error",
                                       error_code="conversation_memory_write_failed")
                    return reply

                # 提交前仍可取消等待。同一次线程调用完成已开始的提交及索引。
                full_reply = await run_in_threadpool(save_reply_and_index)
                if group_run or product_evidence_mode:
                    deferred_reply = {"type": "text", "content": full_reply}
                if deferred_reply is not None:
                    yield "data: " + json.dumps({**deferred_reply, **(ids if group_run else {})}, ensure_ascii=False) + "\n\n"
            terminal_status = "completed"
            terminal_event = {"type": "done", **ids, **proposals}
            if diagnosis is None and group_run is None and not product_evidence_mode:
                try:
                    await run_in_threadpool(store_conversation_summary, user_id, message, full_reply)
                except Exception:
                    add_trace_step(trace_id, "memory", "store_conversation_summary", status="error",
                                   error_code="conversation_summary_write_failed")
        except CandidateRevisionChanged as error:
            terminal_status = "error"
            add_trace_step(trace_id, "persistence", operation, duration=time.perf_counter() - save_started,
                           status="error", error_code=error.code)
            terminal_event = {"type": "error", "error_code": error.code, "content": str(error), **ids}
        except ContextBudgetExceeded as error:
            terminal_status = "error"
            terminal_event = {"type": "error", "error_code": error.code,
                              "content": "当前请求超出模型上下文预算，请缩短问题后重试", **ids}
        except ExecutionStopped as error:
            if error.code == "run_cancelled":
                return
            if error.code in {"run_superseded", "run_stopped"}:
                terminal_status = error.code.removeprefix("run_")
                terminal_event = {"type": terminal_status, **ids}
            elif error.code == "product_evidence_scope_changed":
                terminal_status = "error"
                add_trace_step(trace_id, "persistence", operation, status="error", error_code=error.code,
                               evidence={"final_citation_ids": []})
                terminal_event = {"type": "error", "error_code": error.code,
                                  "content": "商品资料版本或访问权限已变化，请重新查询。", **ids}
            else:
                terminal_status = "error"
                add_trace_step(trace_id, "execution", "budget_stopped", status="error", error_code=error.code)
                terminal_event = {"type": "error", "error_code": error.code,
                                  "content": "本轮执行已停止，未能完成回复", **ids}
        except chat_groups.GroupConflict:
            terminal_status = await run_in_threadpool(group_run.status)
            add_trace_step(trace_id, "persistence", operation, status="error", error_code=f"run_{terminal_status}")
            terminal_event = {"type": terminal_status, **ids}
        except Exception:
            terminal_status = "error"
            stage = "persistence" if operation in {"save_reply", "save_user_message"} else "execution"
            duration = time.perf_counter() - save_started if save_started is not None else 0
            add_trace_step(trace_id, stage, operation, duration=duration, status="error",
                           error_code=f"{operation}_failed")
            error_message = "回复保存失败，请稍后再试" if operation == "save_reply" else "系统繁忙，请稍后再试"
            terminal_event = {"type": "error", "content": error_message, **ids}
        finally:
            if sess_agent is not None:
                pool.release(sess_agent)
            with CancelScope(shield=True):
                if group_run and not settings.DIAGNOSIS_MYSQL_ENABLED:
                    await run_in_threadpool(_unregister_group_execution, group_run.run_id)
                if conversation_snapshot is not None:
                    await run_in_threadpool(conversation_snapshot.close)
                if group_run is not None and terminal_status != "completed":
                    await run_in_threadpool(group_run.finish, terminal_status)
                    # A stop can win the write transaction even while the stream is handling an error.
                    persisted = await run_in_threadpool(group_run.status)
                    if persisted in {"stopped", "superseded"}:
                        terminal_status = persisted
                        terminal_event = {"type": persisted, **ids}
                await run_in_threadpool(end_trace, trace_id, terminal_status)
        yield "data: " + json.dumps(terminal_event) + "\n\n"

    return ChatStreamingResponse(gen(), execution=execution, group_run=group_run, media_type="text/event-stream",
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
    raise HTTPException(status_code=503, detail="缺少工作台静态页面")


@app.get("/orders", include_in_schema=False)
async def order_page():
    return FileResponse(os.path.join(static_dir, "orders.html"))


# ===== 启动入口 =====
if __name__ == "__main__":
    port = int(settings.PORT)
    host = "127.0.0.1" if settings.DIAGNOSIS_MYSQL_ENABLED else "0.0.0.0"
    print(f"FastAPI server starting on http://{host}:{port}")
    if not settings.DIAGNOSIS_MYSQL_ENABLED:
        print(f"WebSocket chat endpoint: ws://127.0.0.1:{port}/ws/chat")
        print("Login accounts: boss/admin123 | admin/admin123 | zhangsan/user123 | lisi/user123")
    print(f"API docs (Swagger): http://127.0.0.1:{port}/docs")
    uvicorn.run(app, host=host, port=port)



