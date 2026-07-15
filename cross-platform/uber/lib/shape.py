"""Map Uber Eats JSON → AgentOS / Shopping order shapes."""
from __future__ import annotations

import re as _re

from lib.session import _UBER_EATS, _parse_fare, _ueats

def _active_order_uuid(order: dict) -> str:
    """Pull the workflow/order UUID from a getActiveOrdersV1 order card."""
    info = order.get("orderInfo") or {}
    overview = order.get("activeOrderOverview") or {}
    return (
        info.get("orderUuid")
        or order.get("orderUUID")
        or order.get("uuid")
        or overview.get("orderUuid")
        or ""
    )

def _money_str(amount: float | int | None) -> str | None:
    if amount is None:
        return None
    try:
        return f"${float(amount):.2f}"
    except (TypeError, ValueError):
        return None

def _summary_from_checkout_info(checkout_info: list | None, *, total: str | None = None) -> dict:
    """Amazon-shaped summary keys from getPastOrdersV1 fareInfo.checkoutInfo."""
    summary: dict[str, str] = {}
    best_discount: tuple[float, str] | None = None
    for item in checkout_info or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        label = str(item.get("label") or "").lower()
        raw = item.get("rawValue")
        if not isinstance(raw, (int, float)):
            continue
        display = _money_str(raw)
        if not display:
            continue
        if key == "eats_fare.subtotal" or label == "subtotal":
            summary["subtotal"] = display
        elif (
            "booking_fee" in key
            or label in ("delivery fee", "delivery")
            or (key.endswith("booking_fee") and raw >= 0)
        ) and raw >= 0:
            # Prefer real delivery fee over tiny service-fee line items.
            if "booking_fee" in key or label == "delivery fee":
                summary["shipping"] = display
            else:
                summary.setdefault("shipping", display)
        elif "tax" in key or label == "tax":
            summary["tax"] = display
        elif raw < 0 or "discount" in key or "discount" in label or "benefit" in label or "credit" in key:
            if best_discount is None or abs(raw) >= abs(best_discount[0]):
                best_discount = (float(raw), display)
    if best_discount:
        summary["discount"] = best_discount[1]
    if total:
        summary["grand_total"] = total
    return {k: v for k, v in summary.items() if v}

def _shop_items(items: list) -> list:
    """Ensure each line item has Shopping's ``title`` (Amazon parity)."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        row = dict(it)
        name = row.get("title") or row.get("name") or ""
        if name:
            row.setdefault("title", name)
            row.setdefault("name", name)
        out.append(row)
    return out

async def _enrich_items_from_store_catalog(store_uuid: str | None, items: list) -> list:
    """Fill missing item images/prices via ``__ueats.enrichItems`` (catalog title match)."""
    if not store_uuid or not items:
        return items
    need = any(not (it.get("image") or it.get("imageUrl")) or not it.get("price") for it in items)
    if not need:
        return items
    try:
        en = await _ueats("enrichItems", store_uuid, items)
    except Exception:
        return items
    if isinstance(en, dict) and en.get("ok") and isinstance(en.get("items"), list):
        return en["items"]
    return items

async def _past_order_meta(order_uuid: str) -> dict:
    """Lookup one order in getPastOrdersV1 (first page). Empty dict if missing."""
    try:
        past = await _ueats("pastOrders", "")
    except Exception:
        return {}
    if not isinstance(past, dict) or not past.get("ok"):
        return {}
    return (past.get("ordersMap") or {}).get(order_uuid) or {}

def _detail_from_past_meta(order_uuid: str, order_meta: dict, *, items: list | None = None) -> dict:
    """Shopping detail shell from past-order metadata (no receipt required)."""
    base = order_meta.get("baseEaterOrder") or {}
    store_info = order_meta.get("storeInfo") or {}
    fare = order_meta.get("fareInfo") or {}
    location = store_info.get("location") or {}
    raw_addr = location.get("address") or {}
    if isinstance(raw_addr, str):
        raw_addr = {"eaterFormattedAddress": raw_addr}

    total_cents = fare.get("totalPrice", 0) or 0
    total_amount = total_cents / 100 if total_cents else None
    total_str = _money_str(total_amount)
    when = base.get("completedAt") or base.get("lastStateChangeAt") or base.get("createdAt")
    dining_mode = (
        order_meta.get("diningMode")
        or base.get("diningMode")
        or order_meta.get("fulfillmentType")
        or ""
    )
    if isinstance(dining_mode, str):
        dining_mode = dining_mode.upper()
    else:
        dining_mode = ""

    product_items = _shop_items(items or [])
    checkout = fare.get("checkoutInfo") or []
    summary = _summary_from_checkout_info(checkout, total=total_str)
    fare_breakdown = [
        {"label": item.get("label"), "amount": item.get("rawValue"), "key": item.get("key")}
        for item in checkout
        if isinstance(item, dict)
    ]

    store_title = store_info.get("title") or "Uber Eats order"
    result = {
        "id": order_uuid,
        "orderId": order_uuid,
        "name": store_title,
        "image": store_info.get("heroImageUrl"),
        "published": when,
        "orderDate": when,
        "deliveryDate": when if base.get("isCompleted") else None,
        "total": total_str,
        "totalAmount": total_amount,
        "currency": fare.get("currencyCode") or "USD",
        "status": "cancelled" if base.get("isCancelled") else ("completed" if base.get("isCompleted") else "in_progress"),
        "itemCount": len(product_items) or None,
        "items": product_items,
        "contains": product_items,
        "summary": summary or None,
        "fareBreakdown": fare_breakdown or None,
        "diningMode": dining_mode or None,
        "isPickup": dining_mode == "PICKUP",
        "interactionType": order_meta.get("interactionType"),
        "at": _UBER_EATS,
        "purchased_at": {
            "shape": "place",
            "id": store_info.get("uuid"),
            "name": store_title,
            "image": store_info.get("heroImageUrl"),
            "featureType": "poi",
            "fullAddress": raw_addr.get("eaterFormattedAddress"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
        },
        "url": f"https://www.ubereats.com/orders/{order_uuid}",
    }

    delivery_addr = base.get("deliveryAddress") or {}
    delivery_address_obj = delivery_addr.get("address") or {}
    if isinstance(delivery_address_obj, str):
        ship_str = delivery_address_obj
    else:
        ship_str = (
            delivery_address_obj.get("eaterFormattedAddress")
            or delivery_address_obj.get("title")
            or ""
        )
    if not ship_str and dining_mode == "PICKUP":
        ship_str = raw_addr.get("eaterFormattedAddress") or ""
    if ship_str:
        result["shipped_to"] = ship_str

    timeline = []
    for sc in (base.get("orderStateChanges") or []):
        timeline.append({"at": sc.get("stateChangeTime"), "category": "order", "type": sc.get("type")})
    for dc in (base.get("deliveryStateChanges") or []):
        timeline.append({"at": dc.get("stateChangeTime"), "category": "delivery", "type": dc.get("type")})
    timeline.sort(key=lambda e: e.get("at") or "")
    if timeline:
        result["timeline"] = timeline
    return {k: v for k, v in result.items() if v is not None}

def _active_order_to_list_row(order: dict) -> dict | None:
    """Shape one live getActiveOrdersV1 card as an order list row for Shopping."""
    order_uuid = _active_order_uuid(order)
    if not order_uuid:
        return None
    info = order.get("orderInfo") or {}
    overview = order.get("activeOrderOverview") or {}
    status_obj = order.get("activeOrderStatus") or {}
    store = info.get("storeInfo") or {}
    store_loc = store.get("location") or {}
    store_addr = store_loc.get("address") or {}
    if isinstance(store_addr, str):
        store_addr = {"eaterFormattedAddress": store_addr}

    phase = ((status_obj.get("titleSummary") or {}).get("summary") or {}).get("text") or ""
    eta = ((status_obj.get("subtitleSummary") or {}).get("summary") or {}).get("text") or ""
    status = phase.lower().replace("...", "").replace("…", "").strip() or "in_progress"

    dining_mode = (
        info.get("diningMode")
        or overview.get("diningMode")
        or info.get("fulfillmentType")
        or overview.get("fulfillmentType")
        or ""
    )
    if isinstance(dining_mode, str):
        dining_mode = dining_mode.upper()
    else:
        dining_mode = ""

    items = []
    for oi in (overview.get("items") or []):
        name = oi.get("title") or oi.get("text") or ""
        if not name:
            continue
        item = {"name": name, "title": name, "quantity": oi.get("quantity", 1)}
        customization = oi.get("subtitle") or oi.get("description") or ""
        if customization:
            item["customizations"] = customization
        if oi.get("imageUrl") or oi.get("image"):
            item["image"] = oi.get("imageUrl") or oi.get("image")
        items.append(item)

    store_title = overview.get("title") or store.get("name") or "Uber Eats order"
    total_subtitle = overview.get("subtitle")  # e.g. "1 item for $21.42"
    total_amount = None
    if isinstance(total_subtitle, str):
        m = _re.search(r"\$([\d,.]+)", total_subtitle)
        if m:
            try:
                total_amount = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    row = {
        "id": order_uuid,
        "orderId": order_uuid,
        "name": store_title,
        "image": store.get("heroImageUrl") or store.get("imageUrl"),
        "orderDate": info.get("createdAt") or overview.get("createdAt"),
        "published": info.get("createdAt") or overview.get("createdAt"),
        "itemCount": len(items) or None,
        "items": items,
        "total": total_subtitle,
        "totalAmount": total_amount,
        "currency": info.get("currencyCode") or "USD",
        "status": status,
        "eta": eta or None,
        "diningMode": dining_mode or None,
        "isPickup": dining_mode == "PICKUP",
        "at": _UBER_EATS,
        "purchased_at": {
            "shape": "place",
            "id": store.get("uuid") or store_addr.get("eaterFormattedAddress") or store_title,
            "name": store.get("name") or store_title,
            "fullAddress": store_addr.get("eaterFormattedAddress"),
            "latitude": store_loc.get("latitude"),
            "longitude": store_loc.get("longitude"),
            "featureType": "poi",
        } if (store.get("name") or store_title) else None,
        "url": f"https://www.ubereats.com/orders/{order_uuid}",
    }
    return row

async def _fetch_active_orders() -> list:
    """getActiveOrdersV1 with orderUuid=null — via ``__ueats.activeOrders``."""
    try:
        act = await _ueats("activeOrders")
    except Exception:
        return []
    if not isinstance(act, dict) or not act.get("ok"):
        return []
    return act.get("orders") or []

async def _shape_ueats_order(order_uuid: str, raw: dict) -> dict:
    """Map ``__ueats.getOrder`` JSON → Shopping/AgentOS order shape."""
    past = raw.get("past") or {}
    receipt = raw.get("receipt") or {}
    active = raw.get("active")
    entity = raw.get("entity")

    # Prefer receipt items (JS-parsed); else active overview; else entity cart.
    items_raw = []
    if isinstance(receipt, dict) and receipt.get("items"):
        items_raw = receipt["items"]
    elif active:
        overview = (active.get("activeOrderOverview") or {})
        for oi in overview.get("items") or []:
            name = oi.get("title") or oi.get("text") or ""
            if not name:
                continue
            row = {"name": name, "title": name, "quantity": oi.get("quantity", 1)}
            cust = oi.get("subtitle") or oi.get("description") or ""
            if cust:
                row["customizations"] = cust
            if oi.get("imageUrl") or oi.get("image"):
                row["image"] = oi.get("imageUrl") or oi.get("image")
            items_raw.append(row)
    elif entity:
        cart = ((entity.get("orderEntity") or {}).get("cart") or {}).get("shoppingCart") or {}
        for it in cart.get("items") or []:
            title = it.get("title") or ""
            items_raw.append({
                "id": it.get("skuUUID") or (it.get("itemID") or {}).get("catalogItemUUID"),
                "name": title,
                "title": title,
                "image": it.get("imageURL"),
                "quantity": it.get("quantity", 1),
            })

    fare = {}
    if isinstance(receipt, dict) and receipt.get("fare"):
        fare = dict(receipt["fare"])
    total_str = fare.get("total") or ""
    total_amount, currency = _parse_fare(total_str) if total_str else (None, None)

    store_ref = None
    order_meta = past if past else {}
    if order_meta:
        si = order_meta.get("storeInfo") or {}
        loc = si.get("location") or {}
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            addr = {"eaterFormattedAddress": addr}
        if si.get("title"):
            store_ref = {
                "shape": "place",
                "id": si.get("uuid"),
                "name": si["title"],
                "image": si.get("heroImageUrl"),
                "fullAddress": addr.get("eaterFormattedAddress"),
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
                "featureType": "poi",
            }
        fare_info = order_meta.get("fareInfo") or {}
        if total_amount is None and fare_info.get("totalPrice"):
            total_amount = (fare_info.get("totalPrice") or 0) / 100
            total_str = _money_str(total_amount) or total_str
            currency = currency or fare_info.get("currencyCode") or "USD"

    product_items = _shop_items([
        {
            "shape": "product",
            "id": it.get("id") or it.get("itemUuid"),
            "name": it.get("name") or it.get("title"),
            "title": it.get("title") or it.get("name"),
            "quantity": it.get("quantity", 1),
            **({"customizations": it["customizations"]} if it.get("customizations") else {}),
            **({"image": it["image"], "imageUrl": it["image"]} if it.get("image") else {}),
            **({"price": it["price"]} if it.get("price") else {}),
            **({"priceAmount": it["priceAmount"]} if it.get("priceAmount") is not None else {}),
        }
        for it in items_raw
        if isinstance(it, dict)
    ])
    store_uuid = (store_ref or {}).get("id") or (order_meta.get("storeInfo") or {}).get("uuid")
    product_items = await _enrich_items_from_store_catalog(store_uuid, product_items)

    summary = {
        "subtotal": fare.get("item_subtotal"),
        "shipping": fare.get("delivery_fee"),
        "tax": fare.get("tax"),
        "discount": fare.get("delivery_discount"),
        "grand_total": fare.get("total") or total_str or None,
    }
    if fare.get("tip") and not summary.get("shipping"):
        summary["shipping"] = fare.get("tip")

    base = order_meta.get("baseEaterOrder") or {}
    when = (
        base.get("completedAt")
        or base.get("lastStateChangeAt")
        or base.get("createdAt")
        or (receipt.get("timestamp") if isinstance(receipt, dict) else None)
    )
    if base.get("isCancelled"):
        status = "cancelled"
    elif base.get("isCompleted") or (raw.get("source") or "").startswith("receipt"):
        status = "completed"
    elif active:
        phase = ((active.get("activeOrderStatus") or {}).get("titleSummary") or {}).get("summary") or {}
        status = (phase.get("text") or "in_progress").lower().replace("...", "").replace("…", "").strip()
    else:
        status = "in_progress"

    dining_mode = (
        order_meta.get("diningMode")
        or base.get("diningMode")
        or order_meta.get("fulfillmentType")
        or ""
    )
    if isinstance(dining_mode, str):
        dining_mode = dining_mode.upper()
    else:
        dining_mode = ""

    store_name = (store_ref or {}).get("name") or ""
    result = {
        "id": order_uuid,
        "name": store_name or f"Order {order_uuid[:8]}",
        "published": when,
        "orderDate": when,
        "orderId": order_uuid,
        "total": total_str or _money_str(total_amount),
        "totalAmount": total_amount,
        "currency": currency or "USD",
        "status": status,
        "itemCount": len(product_items),
        "at": _UBER_EATS,
        "fareBreakdown": fare or None,
        "summary": {k: v for k, v in summary.items() if v},
        "items": product_items,
        "contains": product_items,
        "url": f"https://www.ubereats.com/orders/{order_uuid}",
        "source": raw.get("source"),
    }
    if store_ref:
        result["purchased_at"] = store_ref
        result["image"] = store_ref.get("image")
    if when and base.get("isCompleted"):
        result["deliveryDate"] = when
    if dining_mode:
        result["diningMode"] = dining_mode
        result["isPickup"] = dining_mode == "PICKUP"

    delivery_addr = base.get("deliveryAddress") or {}
    delivery_address_obj = delivery_addr.get("address") or {}
    if isinstance(delivery_address_obj, str):
        ship_str = delivery_address_obj
    else:
        ship_str = (
            delivery_address_obj.get("eaterFormattedAddress")
            or delivery_address_obj.get("title")
            or ""
        )
    if not ship_str and result.get("isPickup") and store_ref:
        ship_str = store_ref.get("fullAddress") or ""
    if ship_str:
        result["shipped_to"] = ship_str

    timeline = []
    for sc in (base.get("orderStateChanges") or []):
        timeline.append({"at": sc.get("stateChangeTime"), "category": "order", "type": sc.get("type")})
    for dc in (base.get("deliveryStateChanges") or []):
        timeline.append({"at": dc.get("stateChangeTime"), "category": "delivery", "type": dc.get("type")})
    timeline.sort(key=lambda e: e.get("at") or "")
    if timeline:
        result["timeline"] = timeline

    past_checkout = (order_meta.get("fareInfo") or {}).get("checkoutInfo") or []
    past_summary = _summary_from_checkout_info(
        past_checkout,
        total=result.get("total") or _money_str(result.get("totalAmount")),
    )
    merged = dict(past_summary)
    merged.update(result.get("summary") or {})
    if merged:
        result["summary"] = merged

    return {k: v for k, v in result.items() if v is not None}

