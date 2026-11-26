"""Order read API and manual Amazon source ingestion."""

from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from sqlite3 import Error as SQLiteError
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict

from backend.config.config import settings
from backend.database import order_sales, orders
from backend.database.database import get_db
from backend.routes.amazon_orders import SyncError, fetch_orders


ShopId = Annotated[str, Path(min_length=1, max_length=128)]
OrderId = Annotated[str, Path(min_length=1, max_length=128)]
After = Annotated[int, Query(ge=0, le=2**63 - 1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class SyncWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_after: AwareDatetime
    updated_before: AwareDatetime


def _response(data, *, status=200, retry_after=None):
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return JSONResponse({"code": status, "data": data}, status_code=status, headers=headers)


def _check_shop(conn, user_id, source, shop_id):
    if not orders.has_access(conn, user_id, source, shop_id):
        raise HTTPException(404, "店铺不存在")


def order_router(session_user):
    router = APIRouter(prefix="/api/orders")

    @contextmanager
    def read_session(request):
        with closing(get_db(read_only=True)) as conn, conn:
            conn.execute("BEGIN")
            yield conn, session_user(request, conn)

    @router.get("/shops")
    def list_shops(request: Request):
        with read_session(request) as (conn, user_id):
            return _response(orders.shops(conn, user_id))

    @router.get("/shops/{source}/{shop_id}")
    def list_shop_orders(
        request: Request, source: str, shop_id: ShopId,
        status: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        search: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ):
        with read_session(request) as (conn, user_id):
            _check_shop(conn, user_id, source, shop_id)
            return _response(orders.list_orders(
                conn, source, shop_id, status=status, search=search, limit=limit, offset=offset,
            ))

    @router.get("/shops/{source}/{shop_id}/orders/{order_id}")
    def order_detail(request: Request, source: str, shop_id: ShopId, order_id: OrderId):
        with read_session(request) as (conn, user_id):
            _check_shop(conn, user_id, source, shop_id)
            data = orders.read_order(conn, source, shop_id, order_id)
            if data is None:
                raise HTTPException(404, "订单不存在")
            return _response(data)

    @router.get("/sales/publications")
    def publications(request: Request, after: After = 0, limit: PageSize = 100):
        with read_session(request) as (conn, user_id):
            return _response(order_sales.list_publications(conn, user_id, after=after, limit=limit))

    @router.get("/sales/publications/{publication_id}")
    def publication(request: Request, publication_id: Annotated[str, Path(pattern=r"^osp_[0-9a-f]{32}$")]):
        with read_session(request) as (conn, user_id):
            data = order_sales.read_publication(conn, user_id, publication_id)
            if data is None:
                raise HTTPException(404, "销量发布不存在")
            return _response(data)

    @router.get("/shops/{source}/{shop_id}/sales-imports/{batch_id}")
    def sales_import(request: Request, source: str, shop_id: ShopId,
                     batch_id: Annotated[int, Path(ge=1, le=2**63 - 1)], after: After = 0, limit: PageSize = 100):
        with read_session(request) as (conn, user_id):
            data = order_sales.read_import(conn, user_id, source, shop_id, batch_id, after=after, limit=limit)
            if data is None:
                raise HTTPException(404, "订单导入不存在")
            return _response(data)

    @router.get("/shops/olist-v2/{shop_id}/sales/products")
    def sales_products(request: Request, shop_id: ShopId):
        with read_session(request) as (conn, user_id):
            _check_shop(conn, user_id, orders.OLIST, shop_id)
            return _response(orders.olist_sales_products(conn, shop_id))

    @router.get("/shops/olist-v2/{shop_id}/sales")
    def sales_chart(
        request: Request, shop_id: ShopId,
        product_id: Annotated[str, Query(min_length=1, max_length=128)],
        baseline_start: date, baseline_end: date, current_start: date, current_end: date,
        status: Literal["delivered", "shipped"] | None = None,
    ):
        if not baseline_start <= baseline_end < current_start <= current_end:
            raise HTTPException(422, "日期窗须有效且基准窗早于当前窗")
        baseline_days = (baseline_end - baseline_start).days
        current_days = (current_end - current_start).days
        if baseline_days != current_days:
            raise HTTPException(422, "两个日期窗须等长")
        if baseline_days >= 90:
            raise HTTPException(422, "每个日期窗最多 90 天")
        with read_session(request) as (conn, user_id):
            _check_shop(conn, user_id, orders.OLIST, shop_id)
            if not orders.has_olist_product(conn, shop_id, product_id):
                raise HTTPException(404, "商品不存在")
            return _response(orders.olist_sales(
                conn, shop_id, product_id, (baseline_start, baseline_end), (current_start, current_end), status,
            ))

    @router.post("/shops/amazon-us-fbm/{shop_id}/preflight")
    def amazon_preflight(request: Request, shop_id: ShopId):
        end = datetime.now(UTC) - timedelta(minutes=3)
        return amazon_ingestion(request, shop_id, "preflight", (orders.utc_text(end - timedelta(hours=1)), orders.utc_text(end)))

    @router.post("/shops/amazon-us-fbm/{shop_id}/sync")
    def amazon_sync(request: Request, shop_id: ShopId, body: SyncWindow):
        try:
            start, end = body.updated_after.astimezone(UTC), body.updated_before.astimezone(UTC)
        except (OverflowError, ValueError):
            raise HTTPException(422, "同步时间窗口无效") from None
        if end < start or end > datetime.now(UTC) - timedelta(minutes=2):
            raise HTTPException(422, "同步结束时间须大于等于开始时间，且至少早于当前时间两分钟")
        return amazon_ingestion(request, shop_id, "sync", (orders.utc_text(start), orders.utc_text(end)))

    def amazon_ingestion(request, shop_id, operation, window):
        source = orders.AMAZON
        with read_session(request) as (conn, user_id):
            _check_shop(conn, user_id, source, shop_id)
        if shop_id != settings.AMAZON_ORDERS_SHOP_ID.strip():
            raise HTTPException(404, "店铺连接不存在")
        preflight = operation == "preflight"
        error = None
        try:
            records, pages = fetch_orders(settings, window, preflight=preflight)
        except SyncError as failure:
            error = failure
            pages = failure.pages
        try:
            with closing(get_db()) as conn, conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                user_id = session_user(request, conn)
                _check_shop(conn, user_id, source, shop_id)
                if not preflight and error is None:
                    receipt = order_sales.import_sync(conn, user_id, source, shop_id, records, window=window, pages=pages)
                else:
                    receipt = orders.record_run(
                        conn, source, shop_id, operation, window=window, pages=pages, written=(0, 0),
                        error_code=error.code if error else None, upstream_status=error.upstream_status if error else None,
                    )
        except (SQLiteError, orders.IngestionConflict):
            raise HTTPException(503, "订单同步记录保存失败") from None
        if error is None:
            return _response(receipt)
        status = {"amazon_credentials_missing": 503, "amazon_rate_limited": 429}.get(error.code, 502)
        return _response(receipt, status=status, retry_after=error.retry_after)

    return router
