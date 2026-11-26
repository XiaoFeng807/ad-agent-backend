"""Authenticated search term workflow within the existing workbench conversation."""

from contextlib import closing, contextmanager

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.agent import search_term_diagnosis as diagnosis
from backend.database import chat_groups, listing_operations, search_term_reports as reports
from backend.database.database import get_db
from backend.observability import add_trace_step, end_trace, new_trace_id, start_trace
from backend.routes.shared import optional_idempotency_key


class DiagnoseRequest(BaseModel):
    import_id: str = Field(min_length=32, max_length=32)
    sku: str | None = Field(default=None, min_length=1, max_length=160)
    batch: int = Field(default=1, ge=1)
    message: str = Field(min_length=1, max_length=2000)


class TitleEdit(BaseModel):
    title: str = Field(max_length=2000)
    revision: int = Field(ge=1)


def source_rows(report, item, *, bucket=False):
    locations = item.get("bucket_locations") if bucket else None
    if locations is None:
        locations = item.get("row_locations")
    if locations is None:
        locations = [{"sheet": None, "line": line} for line in item["row_lines"]]
    return frozenset((report["sha256"], row["sheet"], row["line"]) for row in locations)


def report_page(item):
    return (item["id"] - 1) // reports.MAX_TERMS + 1


def rows_by_location(report, rows):
    return {(report["sha256"], row.get("sheet"), row["line"]): row for row in rows}


def recheck_ad_group(item, fresh, bucket, report, resolved):
    if fresh is None or source_rows(report, fresh, bucket=True) != bucket:
        raise reports.ReportInvalid("当前广告组聚合与原报表行范围不一致。")
    scope = item["scope"]
    if (fresh["scope"], fresh["term"]) != (scope, item["term"]):
        raise reports.ReportInvalid("活动、广告组或搜索词身份无法唯一核验。")
    return reports.verify_amazon_group_rows(report["shop_id"], scope, item["term"], bucket, resolved)


def search_term_router(session_user):
    router = APIRouter(prefix="/api/chat/conversations/{conversation_id}/search-terms")

    @contextmanager
    def connection(request, conversation_id, *, write=False, allow_archived=False):
        with closing(get_db(read_only=not write)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            user_id = session_user(request, conn)
            try:
                group = chat_groups._group(conn, user_id, conversation_id)
                if write and not allow_archived and group["archived"]:
                    raise HTTPException(409, "请先恢复归档会话。")
                yield conn, user_id
            except LookupError:
                raise HTTPException(404, "此会话中没有该报表或诊断结果。") from None
            except reports.ReportInvalid as error:
                raise HTTPException(422, str(error)) from None

    @router.get("/reports")
    def list_reports(request: Request, conversation_id: str):
        with connection(request, conversation_id) as (conn, user_id):
            shops = {shop["shop_id"] for shop in reports.amazon_shops(conn, user_id)}
            return [report for report in reports.report_summaries(conn, conversation_id)
                    if not report.get("format") or report["shop_id"] in shops]

    @router.get("/shops")
    def list_shops(request: Request, conversation_id: str):
        with connection(request, conversation_id) as (conn, user_id):
            return reports.amazon_shops(conn, user_id)

    def preview_report(conn, user_id, manifest, rows):
        products = reports.verify_scope(conn, user_id, manifest)
        if manifest.get("format"):
            groups, resolved = reports.resolve_amazon(conn, manifest, rows, products)
            terms = len({(*row["group_key"], row["normalized"]["term"].casefold()) for row in resolved})
            manifest = {**manifest, "groups": groups, "term_count": terms,
                        "verified_skus": sorted({row["mapped_sku"] for row in resolved if row["mapped_sku"]}),
                        "batch_count": (terms + reports.MAX_TERMS - 1) // reports.MAX_TERMS}
        return {"report": manifest, "products": products}

    def import_file(request, conversation_id, filename, shop_id, raw):
        with connection(request, conversation_id):
            manifest, rows = reports.parse_report(raw, filename)
        with connection(request, conversation_id, write=True) as (conn, user_id):
            if manifest.get("format"):
                manifest["shop_id"] = reports.bind_shop(conn, user_id, shop_id)
            reports.verify_scope(conn, user_id, manifest)
            manifest = reports.save_import(conn, conversation_id, manifest, rows, raw)
            return preview_report(conn, user_id, manifest, rows)

    @router.post("/reports", status_code=201)
    async def upload(request: Request, conversation_id: str, filename: str = Query(min_length=1, max_length=255),
                     shop_id: str | None = Query(default=None, max_length=128)):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > reports.MAX_BYTES:
                raise HTTPException(413, "报表不得超过 16 MB。")
            raw.extend(chunk)
        try:
            return await run_in_threadpool(import_file, request, conversation_id, filename, shop_id, bytes(raw))
        except reports.ReportInvalid as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/reports/{import_id}")
    def preview(request: Request, conversation_id: str, import_id: str):
        with connection(request, conversation_id) as (conn, user_id):
            manifest, rows = reports.read_import(conn, conversation_id, import_id)
            return preview_report(conn, user_id, manifest, rows)

    def resolve_items(conn, user_id, report, rows, products, sku, batch):
        if report.get("format"):
            groups, rows = reports.resolve_amazon(conn, report, rows, products)
            report = {**report, "groups": groups, "batch": batch}
        return report, diagnosis.aggregate(report, rows, sku), rows

    def execute_diagnosis(request, conversation_id, body=None, *, rerun_of=None):
        key = optional_idempotency_key(request)
        identity = None
        if key is not None:
            action, payload = ("search_term_rerun", {"run_id": rerun_of}) if rerun_of is not None else (
                "search_term_diagnosis", body.model_dump(),
            )
            identity = chat_groups.RunRequest.from_payload(action, key, payload)

        def snapshot(conn, user_id, manifest, rows, *, allow_missing_product=False):
            products = reports.verify_scope(conn, user_id, manifest)
            sku = body.sku
            product = None
            if sku is not None or not manifest.get("format"):
                try:
                    product = diagnosis.load_product(conn, user_id, manifest, sku)
                except (reports.ReportInvalid, listing_operations.ListingNotFound):
                    if not allow_missing_product:
                        raise
                    sku = None
            manifest, all_items, resolved = resolve_items(conn, user_id, manifest, rows, products, sku, body.batch)
            start = (body.batch - 1) * reports.MAX_TERMS
            page = all_items[start:start + reports.MAX_TERMS]
            if diagnosis.uses_claims(manifest, product, page):
                diagnosis.claims.retrieve(conn, user_id, product, page)
            return manifest, product, all_items, page, resolved

        trace_id = new_trace_id()
        try:
            with connection(request, conversation_id, write=True, allow_archived=True) as (conn, user_id):
                if identity is not None and (original := identity.replay(conn, user_id, conversation_id)):
                    if not reports.has_shop_access(conn, user_id, original):
                        raise LookupError
                    response = {name: original[name] for name in ("run_id", "trace_id", "status")}
                    response.update(replayed=True, history_url=f"/api/chat/conversations/{conversation_id}/history")
                    if original["status"] == "completed":
                        evidence = read_run(conn, user_id, conversation_id, original["run_id"])
                        response["entry_id"] = evidence["entry_id"]
                    return JSONResponse(response, status_code=202 if original["status"] == "running" else 200,
                                        headers={"Cache-Control": "no-store"})
                if rerun_of is not None:
                    body = rerun_request(conn, user_id, conversation_id, rerun_of)
                message = body.message
                if not message.strip():
                    raise HTTPException(422, "请输入诊断问题。")
                imported, rows = reports.read_import(conn, conversation_id, body.import_id)
                report, product, _, items, _ = snapshot(conn, user_id, imported, rows)
                if not items:
                    raise HTTPException(422, "此报表批次没有可分析的搜索词。")
                group = chat_groups._group(conn, user_id, conversation_id)
                run, _ = chat_groups._accept_new(conn, group, trace_id, message, 1)
                if identity is not None:
                    identity.record(conn, run, shop_id=report["shop_id"], marketplace=report["marketplace"])
        except (chat_groups.GroupConflict, chat_groups.GroupCapacityExceeded) as error:
            raise HTTPException(409, str(error)) from None

        def stage(name, text):
            run.progress("search_term_stage", text, stage=name)
            add_trace_step(trace_id, "search_term_stage", name)

        partial = bool(report.get("format")) and diagnosis.uses_claims(report, product, items)
        invalid = {}
        original_rows = {item["id"]: source_rows(report, item) for item in items}
        original_buckets = {item["id"]: source_rows(report, item, bucket=True) for item in items}
        latest_report, latest_product = report, product
        latest_rows, latest_resolved, latest_matches = [], [], {}

        def recheck(conn, original, phase, attempt):
            nonlocal latest_report, latest_product, latest_rows, latest_resolved, latest_matches
            bindings = [{
                "id": item["id"], "source_rows": sorted(original_rows[item["id"]], key=lambda row: (row[1] or "", row[2])),
            } for item in original]
            try:
                if session_user(request, conn) != user_id or chat_groups._group(conn, user_id, conversation_id)["archived"]:
                    raise LookupError
                manifest, current_rows = reports.read_import(conn, conversation_id, body.import_id)
                if manifest != imported:
                    raise LookupError
                latest_report, latest_product, current_items, page, latest_resolved = snapshot(
                    conn, user_id, manifest, current_rows, allow_missing_product=partial)
            except (LookupError, HTTPException, reports.ReportInvalid, listing_operations.ListingNotFound) as error:
                add_trace_step(trace_id, "scope_recheck", phase, status="error", error_code="scope_unavailable",
                               evidence={"attempt": attempt, "items": bindings})
                reason = str(error) if isinstance(error, reports.ReportInvalid) else "会话权限或报表范围已变化。"
                raise diagnosis.ScopeChanged(reason + "本轮结果未保存，请重新确认权限和报表。") from None
            if not partial and (report, product, items) != (latest_report, latest_product, page):
                add_trace_step(trace_id, "scope_recheck", phase, status="error", error_code="scope_changed",
                               evidence={"attempt": attempt, "items": bindings})
                raise diagnosis.ScopeChanged("商品资料、标题或广告组范围已变化，请重新确认后诊断。")

            by_row = {}
            for fresh in current_items:
                for row in source_rows(latest_report, fresh, bucket=True):
                    by_row.setdefault(row, []).append(fresh)
            latest_rows = current_rows
            available = rows_by_location(latest_report, current_rows)
            latest_matches = {}
            for item, binding in zip(original, bindings, strict=True):
                item_id = item["id"]
                bucket = original_buckets[item_id]
                candidates = {fresh["id"]: fresh for row in bucket for fresh in by_row.get(row, ())}
                binding["current_ids"] = sorted(candidates)
                cause = None
                if bucket - available.keys():
                    cause = "source_rows_missing"
                elif len(candidates) != 1:
                    cause = "regrouped_split" if candidates else "regrouped_scope"
                else:
                    fresh = next(iter(candidates.values()))
                    latest_matches[item_id] = fresh
                    if source_rows(latest_report, fresh, bucket=True) != bucket:
                        cause = "regrouped_merge"
                    elif report_page(fresh) != body.batch:
                        cause = "regrouped_page"
                    elif (fresh["scope"], fresh["term"]) != (item["scope"], item["term"]):
                        cause = "regrouped_scope"
                    elif (source_rows(latest_report, fresh) != original_rows[item_id] or any(
                        fresh.get(key) != item.get(key) for key in ("attribution", "ownership", "metrics")
                    )):
                        cause = "ownership_changed"
                    elif item["attribution"] == "product" and (
                        latest_product != product or fresh.get("source_chunks") != item.get("source_chunks")
                    ):
                        cause = "product_version_changed"
                if cause and invalid.get(item_id) in (None, "product_version_changed"):
                    invalid[item_id] = cause
                binding["result"] = invalid.get(item_id, "verified")
            add_trace_step(trace_id, "scope_recheck", phase,
                           evidence={"attempt": attempt, "items": bindings,
                                     "shop_id": report["shop_id"], "marketplace": report["marketplace"],
                                     "sku": body.sku, "batch": body.batch,
                                     "document_version": (latest_product or {}).get("document_version"),
                                     "title_version": (latest_product or {}).get("title_version"),
                                     "facts_version": (latest_product or {}).get("facts_version")})
            return {item["id"]: invalid[item["id"]] for item in original if item["id"] in invalid}

        def recheck_sdk(original, phase, attempt):
            with closing(get_db(read_only=True)) as conn, conn:
                conn.execute("BEGIN")
                return recheck(conn, original, phase, attempt)

        def recheck_commit(conn):
            recheck(conn, items, "before_commit", None)
            evidence.update(report=latest_report, product=latest_product)
            scope_changed = {item_id for item_id, cause in invalid.items() if cause != "product_version_changed"}
            file_error = None
            resolved = {}
            if scope_changed:
                resolved = rows_by_location(latest_report, latest_resolved)
                try:
                    if latest_rows != reports.read_original_rows(conn, conversation_id, report):
                        file_error = "当前报表行与原始文件不一致。"
                except reports.ReportInvalid as error:
                    file_error = str(error)
            originals = {item["id"]: item for item in items}
            for item in evidence["items"]:
                item_id = item["id"]
                if item_id not in invalid:
                    continue
                diagnosis.mark_incomplete(item, invalid[item_id])
                if item_id not in scope_changed:
                    continue
                original = originals[item_id]
                bucket = original_buckets[item_id]
                try:
                    if missing := bucket - resolved.keys():
                        locations = "、".join(f"{sheet + ' · ' if sheet else ''}第 {line} 行"
                                             for _, sheet, line in sorted(missing, key=lambda row: (row[1] or "", row[2])))
                        raise reports.ReportInvalid(f"缺少报表原行：{locations}。")
                    if file_error:
                        raise reports.ReportInvalid(file_error)
                    group_rows = recheck_ad_group(original, latest_matches.get(item_id), bucket, latest_report, resolved)
                except reports.ReportInvalid as error:
                    reports.set_unavailable(item, str(error))
                    item["historical"] = reports.historical_metrics(original, body.sku)
                    item["reason"] += " " + str(error)
                else:
                    item.update(attribution="ad_group", ownership=[], metrics=diagnosis.sum_metrics(group_rows),
                                row_locations=[{"sheet": row["sheet"], "line": row["line"]} for row in group_rows],
                                metric_recheck="verified")
                    item.pop("bucket_locations", None)

        start_trace(source="search_term_diagnosis", trace_id=trace_id)
        try:
            if rerun_of is not None:
                add_trace_step(trace_id, "rerun", "search_term_diagnosis", evidence={"original_run_id": rerun_of})
            stage("scope_verified", "已核对报表范围与商品归属")
            stage("metrics_calculated", f"已从原始报表复算 {len(items)} 项搜索词指标")
            stage("agent_started", "Agent 正在核对指标、当前标题和商品原文")
            evidence = diagnosis.infer(message, report, product, items, trace_id, recheck=recheck_sdk)
            stage("evidence_verified", "已核对 Agent 的建议、报表范围和商品原文")
            run.save_reply("搜索词诊断已保存到本会话。", 1, evidence=evidence, validate=recheck_commit)
            return {"run_id": run.run_id, "trace_id": trace_id}
        except (diagnosis.DiagnosisRejected, chat_groups.GroupConflict) as error:
            add_trace_step(trace_id, "verification", "not_published", status="error",
                           error_code=type(error).__name__)
            if run.status() == "running":
                run.progress("search_term_stage", str(error), stage="failed")
            raise HTTPException(409, str(error)) from None
        finally:
            status, _ = run.drain("failed")
            end_trace(trace_id, status)

    @router.post("/runs")
    def run_diagnosis(request: Request, conversation_id: str, body: DiagnoseRequest):
        return execute_diagnosis(request, conversation_id, body)

    def read_run(conn, user_id, conversation_id, run_id):
        evidence = chat_groups.read_reply_evidence(conn, user_id, conversation_id, run_id)
        if evidence.get("kind") != reports.KIND:
            raise LookupError
        return evidence

    def rerun_request(conn, user_id, conversation_id, run_id):
        evidence = read_run(conn, user_id, conversation_id, run_id)
        report = evidence["report"]
        product = evidence["product"]
        original = conn.execute(
            "SELECT content FROM chat_group_entries WHERE run_id=? AND kind='user'", (run_id,),
        ).fetchone()
        if original is None:
            raise LookupError
        return DiagnoseRequest(import_id=report["import_id"], batch=report.get("batch", 1),
                               sku=product["target"]["sku"] if product else None, message=original["content"])

    @router.post("/runs/{run_id}/rerun")
    def rerun_diagnosis(request: Request, conversation_id: str, run_id: str):
        return execute_diagnosis(request, conversation_id, rerun_of=run_id)

    def read_item(conn, user_id, conversation_id, run_id, item_id):
        evidence = read_run(conn, user_id, conversation_id, run_id)
        item = next((item for item in evidence["items"] if item["id"] == item_id), None)
        if item is None:
            raise LookupError
        draft = None
        if "initial_draft" in item:
            draft = reports.read_drafts(conn, run_id).get(item_id, {"title": item["initial_draft"], "revision": 1})
        return evidence, item, draft

    @router.get("/runs/{run_id}/items/{item_id}")
    def read_result(request: Request, conversation_id: str, run_id: str, item_id: int):
        with connection(request, conversation_id) as (conn, user_id):
            evidence, item, draft = read_item(conn, user_id, conversation_id, run_id, item_id)
            report = evidence["report"]
            if "metric_recheck" in item:
                try:
                    rows = reports.read_original_rows(conn, conversation_id, report)
                except reports.ReportInvalid as error:
                    item = item.copy()
                    reports.set_unavailable(item, str(error))
                    rows = []
            else:
                _, rows = reports.read_import(conn, conversation_id, report["import_id"])
            by_location = rows_by_location(report, rows)
            lines = source_rows(report, item)
            historical_lines = source_rows(report, item["historical"]) if "historical" in item else frozenset()
            return {"report": report, "item": item, "product": evidence["product"], "rules": evidence["rules"],
                    "rows": [row for key, row in by_location.items() if key in lines],
                    "historical_rows": [row for key, row in by_location.items() if key in historical_lines],
                    "draft": draft,
                    "edit_rules": diagnosis.RULES,
                    "entry_id": evidence["entry_id"]}

    def current_draft_product(conn, user_id, conversation_id, evidence, item):
        original = evidence["product"]
        try:
            current = diagnosis.read_product(conn, user_id, **original["target"])
        except listing_operations.ListingNotFound:
            current = None
        if current is None:
            reasons = ["product_unavailable"]
        else:
            reasons = []
            if any(current[key] != original[key] for key in ("facts_version", "facts")):
                reasons.append("facts_changed")
            if any(current[key] != original[key] for key in ("title_version", "title")):
                reasons.append("title_changed")
            if "demand" in item and not reasons:
                report = evidence["report"]
                imported, rows = reports.read_import(conn, conversation_id, report["import_id"])
                products = reports.verify_scope(conn, user_id, imported)
                _, current_items, _ = resolve_items(conn, user_id, imported, rows, products,
                                                    current["target"]["sku"], report["batch"])
                original_rows = source_rows(report, item)
                fresh = next((entry for entry in current_items if report_page(entry) == report["batch"]
                              and source_rows(imported, entry) == original_rows), {})
                if any(fresh.get(key) != item[key] for key in ("attribution", "ownership", "row_locations", "metrics")):
                    reasons.append("ownership_changed")
                if fresh and ((fresh["scope"], fresh["term"]) != (item["scope"], item["term"])
                              or source_rows(imported, fresh, bucket=True) != source_rows(report, item, bucket=True)):
                    reasons.append("ownership_changed")
                diagnosis.claims.retrieve(conn, user_id, current, [fresh] if fresh else [])
                if (current["document_version"] != original.get("document_version")
                        or fresh.get("source_chunks") != item["source_chunks"]):
                    reasons.append("facts_changed")
        if reasons:
            raise HTTPException(409, {"code": "stale_title_draft", "reasons": reasons,
                                      "message": "商品资料或标题已更新，请重新诊断"},
                                headers={"Cache-Control": "no-store"})
        return current

    def ensure_valid(title, product, item):
        if errors := diagnosis.validate_title(title, product, diagnosis.RULES, item):
            raise HTTPException(422, errors)

    @router.get("/runs/{run_id}/items/{item_id}/draft/copy")
    def copy_draft(request: Request, response: Response, conversation_id: str, run_id: str, item_id: int,
                   revision: int = Query(ge=1)):
        with connection(request, conversation_id) as (conn, user_id):
            evidence, item, draft = read_item(conn, user_id, conversation_id, run_id, item_id)
            if draft is None:
                raise LookupError
            product = current_draft_product(conn, user_id, conversation_id, evidence, item)
            if draft["revision"] != revision:
                raise HTTPException(409, "已保存版本有变化，请重新打开卡片。")
            ensure_valid(draft["title"], product, item)
            response.headers["Cache-Control"] = "no-store"
            return draft

    @router.put("/runs/{run_id}/items/{item_id}/draft")
    def update_draft(request: Request, conversation_id: str, run_id: str, item_id: int, body: TitleEdit):
        with connection(request, conversation_id, write=True) as (conn, user_id):
            evidence, item, draft = read_item(conn, user_id, conversation_id, run_id, item_id)
            if draft is None:
                raise LookupError
            product = current_draft_product(conn, user_id, conversation_id, evidence, item)
            if draft["revision"] != body.revision:
                raise HTTPException(409, "已保存版本有变化，请重新打开卡片。")
            ensure_valid(body.title, product, item)
            if draft["title"] == body.title:
                return draft
            draft = {"revision": draft["revision"] + 1, "title": body.title}
            conn.execute("""INSERT INTO search_term_title_drafts VALUES (?, ?, ?, ?)
                            ON CONFLICT(run_id, item_id) DO UPDATE SET revision=excluded.revision, title=excluded.title""",
                         (run_id, item_id, draft["revision"], draft["title"]))
            return draft

    return router
