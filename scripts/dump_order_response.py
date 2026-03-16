"""Dump raw order API responses from Coles and Woolworths to inspect available fields."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shopping_agent.database import init_db
from shopping_agent.scrapers.coles import coles_scraper
from shopping_agent.scrapers.woolworths import woolworths_scraper


async def dump_coles_instore():
    print("\n" + "=" * 60)
    print("COLES IN-STORE - /api/bff/orders?status=in-store (1 order)")
    print("=" * 60)
    if not await coles_scraper.is_authenticated():
        print("Not authenticated")
        return
    resp = await coles_scraper._request(
        "GET", "/api/bff/orders", params={"status": "in-store", "pageNumber": 1, "pageSize": 1}
    )
    if resp is None:
        print("No response (auth failed?)")
        return
    print(f"HTTP {resp.status_code}")
    data = resp.json()
    print(json.dumps(data, indent=2, default=str))

    orders = data.get("orders") or data.get("data", {}).get("orders") or (data if isinstance(data, list) else [])
    if orders:
        order_data = orders[0]
        order_id = str(order_data.get("orderId") or order_data.get("id") or "")
        txn_id = str(order_data.get("transactionId") or order_data.get("transactionBarcode") or "")
        if order_id and txn_id:
            print(f"\n--- In-store order detail: {order_id} (txn: {txn_id}) ---")
            detail = await coles_scraper._request(
                "GET", f"/api/bff/orders/{order_id}",
                headers={"x-api-version": "2", "x-transaction-id": txn_id},
            )
            if detail:
                print(json.dumps(detail.json(), indent=2, default=str))


async def dump_woolworths_cnc():
    print("\n" + "=" * 60)
    print("WOOLWORTHS - scanning for Click & Collect orders (up to 20)")
    print("=" * 60)
    if not await woolworths_scraper.is_authenticated():
        print("Not authenticated")
        return
    shopper_id = await woolworths_scraper._get_shopper_id()
    if not shopper_id:
        print("Could not get shopper ID")
        return
    resp = await woolworths_scraper._mobile_request(
        "GET",
        "/wow/v1/orders/api/orders",
        params={"shopperId": shopper_id, "pageNumber": 1, "pageSize": 20},
    )
    if resp is None or resp.status_code != 200:
        print(f"Failed: {resp.status_code if resp else 'no response'}")
        return
    items = resp.json().get("items") or []
    print(f"Found {len(items)} orders. DeliveryMethod values: {list({i.get('DeliveryMethod') for i in items})}")

    # Find a click-and-collect order
    cnc = next((i for i in items if i.get("DeliveryMethod") in ("PickUp", "ClickCollect", "Click&Collect", "Pickup")), None)
    if not cnc:
        print("No Click & Collect order found in first 20 — showing all DeliveryMethod values above")
        return

    order_id = cnc.get("OrderId")
    print(f"\n--- C&C order summary ---")
    print(json.dumps(cnc, indent=2, default=str))

    print(f"\n--- C&C order detail: {order_id} ---")
    detail = await woolworths_scraper._mobile_request(
        "GET", f"/wow/v1/orders/api/orders/{order_id}"
    )
    if detail and detail.status_code == 200:
        print(json.dumps(detail.json(), indent=2, default=str))


async def main():
    await init_db()
    await dump_coles_instore()
    await dump_woolworths_cnc()


asyncio.run(main())
