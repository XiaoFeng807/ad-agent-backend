"""Read-only Amazon Orders v2026-01-01 adapter."""

from datetime import UTC, datetime
from hashlib import sha256
import json

import requests

from backend.database.orders import FetchedOrder, Item, Order, utc_text


LWA_URL = "https://api.amazon.com/auth/o2/token"
ORDERS_URL = "https://sellingpartnerapi-na.amazon.com/orders/2026-01-01/orders"
US_MARKETPLACE = "ATVPDKIKX0DER"
MAX_PAGES = 500


class SyncError(Exception):
    def __init__(self, code, upstream_status=None, *, pages=0, retry_after=None):
        super().__init__(code)
        self.code = code
        self.upstream_status = upstream_status
        self.pages = pages
        self.retry_after = retry_after


def _request(session, method, url, **kwargs):
    try:
        response = session.request(
            method, url, timeout=(5, 20), allow_redirects=False, **kwargs,
        )
    except requests.RequestException:
        raise SyncError("amazon_unavailable") from None
    status = response.status_code
    if status == 429:
        delay = response.headers.get("Retry-After", "")
        raise SyncError("amazon_rate_limited", status, retry_after=delay if delay.isascii() and delay.isdigit() else None)
    if status in (401, 403) or (url == LWA_URL and status == 400):
        raise SyncError("amazon_authorization_failed", status)
    if status != 200:
        raise SyncError("amazon_upstream_error", status)
    try:
        data = response.json()
    except ValueError:
        raise SyncError("amazon_invalid_response", status) from None
    if not isinstance(data, dict):
        raise SyncError("amazon_invalid_response", status)
    return data


def _text(value, *, required=False):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid string")
    return value


def _timestamp(value, *, required=False):
    if _text(value, required=required) is None:
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("missing offset")
    return utc_text(stamp)


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid object")
    return value


def _order(raw):
    raw = _object(raw)
    channel = raw.get("salesChannel")
    fulfillment = raw.get("fulfillment")
    marketplace = channel.get("marketplaceId") if isinstance(channel, dict) else None
    fulfilled_by = fulfillment.get("fulfilledBy") if isinstance(fulfillment, dict) else None
    if isinstance(marketplace, str) and marketplace and not any(char.isspace() for char in marketplace) and marketplace != US_MARKETPLACE:
        return None
    if fulfilled_by == "AMAZON":
        return None
    if marketplace != US_MARKETPLACE:
        raise SyncError("amazon_invalid_marketplace", 200)
    if fulfilled_by != "MERCHANT":
        raise SyncError("amazon_invalid_fulfillment", 200)
    try:
        created = _timestamp(raw.get("createdTime"), required=True)
    except (ValueError, OverflowError):
        raise SyncError("amazon_invalid_created_time", 200) from None
    try:
        updated = _timestamp(raw.get("lastUpdatedTime"), required=True)
    except (ValueError, OverflowError):
        raise SyncError("amazon_invalid_last_updated_time", 200) from None
    raw_items = raw.get("orderItems")
    if not isinstance(raw_items, list):
        raise ValueError("missing orderItems")
    items = {}
    for raw_item in raw_items:
        raw_item = _object(raw_item)
        item_id = _text(raw_item.get("orderItemId"), required=True)
        product = _object(raw_item.get("product"))
        quantity = raw_item.get("quantityOrdered")
        if quantity is not None and (type(quantity) is not int or not 0 <= quantity <= 2**63 - 1):
            raise ValueError("invalid quantity")
        item = Item(item_id, _text(product.get("sellerSku")), None, quantity, None)
        if item_id in items and items[item_id] != item:
            raise ValueError("conflicting order item")
        items[item_id] = item
    return Order(
        _text(raw.get("orderId"), required=True), _text(fulfillment.get("fulfillmentStatus")),
        created, updated,
        _timestamp(_object(fulfillment.get("shipByWindow")).get("latestDateTime")),
        None, None, tuple(sorted(items.values())),
    )


def fetch_orders(settings, window, *, preflight=False):
    credentials = {
        "client_id": settings.AMAZON_LWA_CLIENT_ID.get_secret_value(),
        "client_secret": settings.AMAZON_LWA_CLIENT_SECRET.get_secret_value(),
        "refresh_token": settings.AMAZON_LWA_REFRESH_TOKEN.get_secret_value(),
    }
    if any(not value.strip() for value in credentials.values()):
        raise SyncError("amazon_credentials_missing")
    records = []
    tokens = set()
    pages = 0
    with requests.Session() as session:
        auth = _request(session, "POST", LWA_URL, data={"grant_type": "refresh_token", **credentials})
        access_token = auth.get("access_token")
        if not isinstance(access_token, str) or not access_token or any(char.isspace() for char in access_token):
            raise SyncError("amazon_invalid_response", 200)
        params = {
            "lastUpdatedAfter": window[0], "lastUpdatedBefore": window[1],
            "marketplaceIds": US_MARKETPLACE, "fulfilledBy": "MERCHANT",
            "includedData": "FULFILLMENT", "maxResultsPerPage": 1 if preflight else 100,
        }
        try:
            for _ in range(1 if preflight else MAX_PAGES):
                result = _request(
                    session, "GET", ORDERS_URL, params=params,
                    headers={
                        "x-amz-access-token": access_token,
                        "x-amz-date": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
                        "User-Agent": "AdAgent/3.0 (Language=Python)",
                        "Accept": "application/json",
                    },
                )
                pages += 1
                if not isinstance(result.get("orders"), list):
                    raise ValueError("missing orders")
                for position, raw in enumerate(result["orders"], start=1):
                    order = _order(raw)
                    if order is None:
                        continue
                    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    records.append(FetchedOrder(order, {"kind": "amazon_order", "page": pages,
                                                       "position": position, "sha256": sha256(encoded).hexdigest()}))
                if "pagination" not in result:
                    return records, pages
                pagination = result["pagination"]
                token = pagination.get("nextToken") if isinstance(pagination, dict) else None
                if not isinstance(token, str) or not token.strip():
                    raise SyncError("amazon_invalid_pagination", 200)
                if preflight:
                    return records, pages
                if token in tokens:
                    raise SyncError("amazon_pagination_loop")
                tokens.add(token)
                params["paginationToken"] = token
        except SyncError as error:
            error.pages = pages
            raise
        except (ValueError, OverflowError):
            raise SyncError("amazon_invalid_response", 200, pages=pages) from None
    raise SyncError("amazon_page_limit", pages=pages)
