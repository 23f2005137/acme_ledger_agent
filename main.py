import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_TITLE = "Acme Appliances Ledger Agent"
ROOT_URL = os.getenv(
    "QUESTION_DATA_URL",
    "https://exam.sanand.workers.dev/questionData?email=23f2005137%40ds.study.iitm.ac.in&quizSign=D95NJNYho6djNgotOhZUm5HgUyl0HVgCqSRmILKaUU5MR6l3IE6ZB8jo7cp2%2BTD55IFZKxKigJe6OyrGVr1Wo7L8tVxydJmv59DSQjfag23XiwSPjrGsB8yRqk1ZnBuuXxZNjKRUYmkQwRlYxq7B%2BXLB6biK8sJ4L4lE%2FMTtxtg0V6uLBMOK98MN1V4RaiLFHobyRq%2BlvfrwxXopsARtoACvJDiWeY3u3vicawxzAeRKun3%2BnUsmN4BWEt3iB0Df2NVVDcyJt1mdpSEyULArVIeWLlTziLoD9HyZblzeo30hfI%2FMdBw4BOHtRDrtTLjLcwb%2BBm0sLx80foJDffsMwQ%3D%3D&questionId=q-ledger-agent-server&path=%2F",
)
TIMEOUT = 20
MAX_PAGES = 200

app = FastAPI(title=APP_TITLE)
http = requests.Session()
http.headers.update({"Accept": "application/json", "User-Agent": "AcmeLedgerAgent/2.0"})


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def get_json(url: str) -> Any:
    r = http.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_paginated(url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    current = url
    for _ in range(MAX_PAGES):
        if not current or current in seen_urls:
            break
        seen_urls.add(current)
        payload = get_json(current)
        if isinstance(payload, list):
            rows.extend(x for x in payload if isinstance(x, dict))
            break
        if not isinstance(payload, dict):
            break
        batch = payload.get("orders", payload.get("data", payload.get("results", [])))
        if isinstance(batch, list):
            rows.extend(x for x in batch if isinstance(x, dict))
        current = payload.get("next")
    return rows


def load_ledger():
    entry = get_json(ROOT_URL)
    links = entry.get("links", {}) if isinstance(entry, dict) else {}
    orders_url = links.get("orders")
    rates_url = links.get("rates")
    if not orders_url or not rates_url:
        raise RuntimeError("Root API response did not contain links.orders and links.rates")
    raw_rows = fetch_paginated(orders_url)
    rate_payload = get_json(rates_url)
    rates = rate_payload.get("usd_per_unit", {})
    # The source may repeat order IDs; keep the row with latest updated_at.
    current: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        oid = str(row.get("id", "")).strip()
        if not oid:
            continue
        previous = current.get(oid)
        if previous is None or parse_dt(row.get("updated_at")) >= parse_dt(previous.get("updated_at")):
            current[oid] = row
    return list(current.values()), rates, len(raw_rows), len(current)


def parse_dt(value: Any) -> datetime:
    if not value:
        return datetime.min
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return dt.timestamp() and datetime.fromtimestamp(dt.timestamp(), tz=dt.tzinfo)
    except Exception:
        return datetime.min


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def amount_usd(row: dict[str, Any], rates: dict[str, Any]) -> Decimal:
    currency = str(row.get("currency", "USD")).upper()
    rate = dec(rates.get(currency, 1 if currency == "USD" else 0))
    return dec(row.get("amount", dec(row.get("qty", 0)) * dec(row.get("unit_price", 0)))) * rate


def parse_query_date(question: str):
    q = question.lower()
    year_match = re.search(r"\b(20\d{2})\b", q)
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
        "november": 11, "nov": 11, "december": 12, "dec": 12,
    }
    month = next((n for name, n in months.items() if re.search(r"\b" + re.escape(name) + r"\b", q)), None)
    return (int(year_match.group(1)) if year_match else None), month


def order_date(row):
    return parse_dt(row.get("created_at"))


def answer_question(question: str, rows: list[dict[str, Any]], rates: dict[str, Any]):
    q = question.strip()
    ql = q.lower()
    year, month = parse_query_date(q)
    filtered = rows[:]

    # Date filters apply to created_at; timestamps are parsed with timezone offsets.
    if year or month:
        filtered = [r for r in filtered if (
            (not year or (order_date(r) != datetime.min and order_date(r).year == year))
            and (not month or (order_date(r) != datetime.min and order_date(r).month == month))
        )]

    regions = ["north", "south", "east", "west", "central"]
    region = next((x for x in regions if re.search(r"\b" + x + r"\b", ql)), None)
    if region:
        filtered = [r for r in filtered if str(r.get("region", "")).lower() == region]

    products = sorted({str(r.get("product", "")) for r in rows if r.get("product")}, key=len, reverse=True)
    product = next((p for p in products if p.lower() in ql), None)
    if product:
        filtered = [r for r in filtered if str(r.get("product", "")).lower() == product.lower()]

    customer_match = re.search(r"\bC\d{4}\b", q, re.I)
    if customer_match:
        cid = customer_match.group(0).upper()
        filtered = [r for r in filtered if str(r.get("customer", "")).upper() == cid]

    # Revenue is explicitly limited to current paid orders.
    if any(k in ql for k in ["revenue", "sales", "turnover", "total amount", "total value"]):
        paid = [r for r in filtered if str(r.get("status", "")).lower() == "paid"]
        total = sum((amount_usd(r, rates) for r in paid), Decimal("0"))
        return {
            "answer": f"USD {total.quantize(Decimal('0.01'))}",
            "currency": "USD",
            "orders_counted": len(paid),
            "rule": "Only current orders with status 'paid' count as revenue.",
        }

    if "how many" in ql or "count" in ql or "number of" in ql:
        if "customer" in ql and any(k in ql for k in ["unique", "distinct", "different"]):
            return {"answer": len({r.get("customer") for r in filtered if r.get("customer")})}
        return {"answer": len(filtered)}

    if any(k in ql for k in ["list products", "which products", "what products", "product names"]):
        return {"answer": sorted({r.get("product") for r in filtered if r.get("product")})}

    if "refund" in ql or "refunded" in ql:
        refunded = [r for r in filtered if str(r.get("status", "")).lower() == "refunded"]
        total = sum((amount_usd(r, rates) for r in refunded), Decimal("0"))
        return {
            "refunded_orders": len(refunded),
            "refunded_amount_usd": str(total.quantize(Decimal("0.01"))),
            "note": "This uses the amount on rows whose current status is refunded; it is not a separate refund transaction ledger.",
        }

    if any(k in ql for k in ["average", "avg", "mean"]):
        paid = [r for r in filtered if str(r.get("status", "")).lower() == "paid"]
        if not paid:
            return {"answer": "No paid orders matched the filters."}
        total = sum((amount_usd(r, rates) for r in paid), Decimal("0"))
        return {"average_paid_order_usd": str((total / len(paid)).quantize(Decimal("0.01"))), "orders_counted": len(paid)}

    return {
        "matched_orders": len(filtered),
        "orders": filtered[:50],
        "hint": "Ask for revenue/sales, order count, products, refunds, average order value, a region/product, customer ID (e.g. C1213), and/or month/year.",
    }


DATA_ROWS: list[dict[str, Any]] = []
RATES: dict[str, Any] = {}
LOAD_ERROR: str | None = None
RAW_COUNT = 0


@app.on_event("startup")
def startup():
    global DATA_ROWS, RATES, LOAD_ERROR, RAW_COUNT
    try:
        DATA_ROWS, RATES, RAW_COUNT, _ = load_ledger()
        LOAD_ERROR = None
    except Exception as e:
        LOAD_ERROR = f"{type(e).__name__}: {e}"


@app.get("/")
def health():
    return {"status": "ok" if LOAD_ERROR is None else "degraded", "service": APP_TITLE, "endpoint": "/answer"}


@app.get("/health")
def health_detail():
    return {
        "status": "ok" if LOAD_ERROR is None else "degraded",
        "load_error": LOAD_ERROR,
        "raw_rows_loaded": RAW_COUNT,
        "unique_current_orders": len(DATA_ROWS),
        "rates": RATES,
    }


@app.post("/answer")
def answer(body: Question):
    if LOAD_ERROR:
        raise HTTPException(status_code=503, detail=f"Could not load ledger source: {LOAD_ERROR}")
    return answer_question(body.question, DATA_ROWS, RATES)


@app.get("/debug/source")
def debug_source():
    # Do not expose the signed source URL.
    return {
        "load_error": LOAD_ERROR,
        "raw_rows_loaded": RAW_COUNT,
        "unique_current_orders": len(DATA_ROWS),
        "rates": RATES,
        "sample_orders": DATA_ROWS[:3],
    }
