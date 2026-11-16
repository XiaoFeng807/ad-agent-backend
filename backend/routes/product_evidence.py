"""Authenticated retrieval and source lookup for product documents."""

from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.database import database, listing_operations
from backend.rag.product_evidence import RECALL_K, ProductEvidence, ReleaseConflict, read_chunk


class ProductSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    shop_id: str = Field(min_length=1, max_length=128)
    marketplace_id: str = Field(min_length=1, max_length=128)
    sku: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=500)
    strategy: Literal["fixed", "structured"] = "structured"
    mode: Literal["vector", "hybrid"] = "hybrid"
    expansion: Literal["none", "aliases", "parts", "both"] = "none"
    top_k: int = Field(default=RECALL_K, ge=1, le=RECALL_K)


def product_evidence_router(session_user):
    router = APIRouter(prefix="/api/product-evidence")

    @router.post("/search")
    def search(request: Request, response: Response, body: ProductSearch):
        response.headers["Cache-Control"] = "no-store"
        options = body.model_dump(exclude={"shop_id", "marketplace_id", "sku"})
        target = body.model_dump(include={"shop_id", "marketplace_id", "sku"})
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            user_id = session_user(request, conn)
            try:
                return ProductEvidence().search(conn, user_id, target, **options)
            except listing_operations.ListingNotFound:
                raise HTTPException(404, "商品不存在") from None
            except (OSError, ReleaseConflict):
                raise HTTPException(503, "商品资料索引暂不可用") from None
            except ValueError:
                raise HTTPException(422, "请检查检索选项") from None

    @router.get("/chunks/{chunk_id}")
    def original(request: Request, chunk_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with closing(database.get_db(read_only=True)) as conn:
            conn.execute("BEGIN")
            user_id = session_user(request, conn)
            chunk = read_chunk(conn, user_id, chunk_id)
        if chunk is None:
            raise HTTPException(404, "当前商品资料片段不存在", headers={"Cache-Control": "no-store"})
        return chunk

    return router
