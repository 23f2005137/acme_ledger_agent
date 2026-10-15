import json
import os
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP_TITLE = "Acme Appliances Ledger Q&A"
QUESTION_DATA_URL = os.getenv(
    "QUESTION_DATA_URL",
    "https://exam.sanand.workers.dev/questionData?email=23f2005137%40ds.study.iitm.ac.in&quizSign=D95NJNYho6djNgotOhZUm5HgUyl0HVgCqSRmILKaUU5MR6l3IE6ZB8jo7cp2%2BTD55IFZKxKigJe6OyrGVr1Wo7L8tVxydJmv59DSQjfag23XiwSPjrGsB8yRqk1ZnBuuXxZNjKRUYmkQwRlYxq7B%2BXLB6biK8sJ4L4lE%2FMTtxtg0V6uLBMOK98MN1V4RaiLFHobyRq%2BlvfrwxXopsARtoACvJDiWeY3u3vicawxzAeRKun3%2BnUsmN4BWEt3iB0Df2NVVDcyJt1mdpSEyULArVIeWLlTziLoD9HyZblzeo30hfI%2FMdBw4BOHtRDrtTLjLcwb%2BBm0sLx80foJDffsMwQ%3D%3D&questionId=q-ledger-agent-server&path=/",
)
EMAIL = "23f2005137@ds.study.iitm.ac.in"
TIMEOUT = 8
MAX_PAGES = 40

app = FastAPI(title=APP_TITLE)
session = requests.Session()
session.headers.update({"User-Agent": "AcmeLedgerQuestionAgent/1.0", "Accept": "application/json"})


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def get_json(url: str) -> Any:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    ctype = response.headers.get("content-type", "")
    if "json" in ctype:
        return response.json()
    try:
        return response.json()
    except Exception:
        return response.text


def extract_urls(obj: Any, base_url: str) -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and (
                k.lower() in {"url", "href", "endpoint", "api", "openapi", "swagger", "docs", "baseurl", "base_url", "next"}
                or v.startswith("/") or v.startswith("http")
            ):
                if v.startswith(("http://", "https://", "/")):
                    found.append(urljoin(base_url, v))
            else:
                found.extend(extract_urls(v, base_url))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(extract_urls(v, base_url))
    return found


def load_source() -> tuple[Any, dict[str, Any]]:
    try:
        entry = get_json(QUESTION_DATA_URL)
    except Exception as e:
        return {"load_error": str(e), "entry_url": QUESTION_DATA_URL}, {}
    root = urlparse(QUESTION_DATA_URL)
    candidates = [QUESTION_DATA_URL]
    candidates.extend(extract_urls(entry, QUESTION_DATA_URL))
    candidates.extend([
        f"{root.scheme}://{root.netloc}/",
        f"{root.scheme}://{root.netloc}/openapi.json",
        f"{root.scheme}://{root.netloc}/swagger.json",
        f"{root.scheme}://{root.netloc}/docs",
    ])
    visited: dict[str, Any] = {}
    queue = list(dict.fromkeys(candidates))
    while queue and len(visited) < MAX_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        try:
            data = get_json(url)
        except Exception:
            continue
        visited[url] = data
        for child in extract_urls(data, url):
            if child not in visited and child not in queue and len(queue) < MAX_PAGES:
                queue.append(child)
    return entry, visited


ENTRY_DATA: Any = {}
DISCOVERED: dict[str, Any] = {}


@app.on_event("startup")
def startup():
    global ENTRY_DATA, DISCOVERED
    ENTRY_DATA, DISCOVERED = load_source()


@app.get("/")
def health():
    return {"status": "ok", "service": APP_TITLE, "endpoint": "/answer"}


@app.get("/debug/source")
def debug_source():
    # Useful during setup; remove or protect this route for production if desired.
    return {
        "entry": ENTRY_DATA,
        "discovered_urls": list(DISCOVERED.keys()),
        "discovered_summaries": {
            u: (list(v.keys())[:40] if isinstance(v, dict) else type(v).__name__)
            for u, v in DISCOVERED.items()
        },
    }


def flatten_records(value: Any, path: str = "") -> list[dict[str, Any]]:
    records = []
    if isinstance(value, list):
        for item in value:
            records.extend(flatten_records(item, path))
    elif isinstance(value, dict):
        # Treat dictionaries with common ledger fields as records.
        keys = {str(k).lower() for k in value}
        if keys & {"order_id", "orderid", "customer", "customer_id", "product", "product_name", "quantity", "unit_price", "price", "revenue", "amount", "refund", "refund_amount", "region", "date", "order_date"}:
            records.append(value)
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                records.extend(flatten_records(v, f"{path}/{k}"))
    return records


def all_records() -> list[dict[str, Any]]:
    records = []
    seen = set()
    for url, data in DISCOVERED.items():
        for record in flatten_records(data):
            marker = json.dumps(record, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                records.append(record)
    for record in flatten_records(ENTRY_DATA):
        marker = json.dumps(record, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            records.append(record)
    return records


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def field(record: dict[str, Any], *names: str) -> Any:
    by_norm = {norm(k).replace(" ", "_"): v for k, v in record.items()}
    for name in names:
        n = norm(name).replace(" ", "_")
        if n in by_norm:
            return by_norm[n]
    return None


def money(v: Any) -> Decimal:
    try:
        if isinstance(v, str):
            v = v.replace("$", "").replace(",", "").strip()
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def date_parts(v: Any) -> tuple[int | None, int | None]:
    s = str(v or "")
    m = re.search(r"(20\d{2})[-/](\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})[-/](20\d{2})", s)
    if m:
        return int(m.group(2)), int(m.group(1))
    return None, None


MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def parse_period(q: str):
    ql = q.lower()
    year_match = re.search(r"\b(20\d{2})\b", ql)
    year = int(year_match.group(1)) if year_match else None
    month = next((n for name, n in MONTHS.items() if re.search(r"\b" + re.escape(name) + r"\b", ql)), None)
    return year, month


def choose_amount(record: dict[str, Any], want_refunds: bool) -> Decimal:
    if want_refunds:
        for n in ("refund_amount", "refund", "refunded_amount", "total_refund"):
            v = field(record, n)
            if v is not None:
                return money(v)
    for n in ("revenue", "total_amount", "order_total", "amount", "sales_amount", "total", "line_total"):
        v = field(record, n)
        if v is not None:
            return money(v)
    qty = field(record, "quantity", "qty", "units")
    price = field(record, "unit_price", "price", "selling_price")
    if qty is not None and price is not None:
        return money(qty) * money(price)
    return Decimal("0")


def answer_question(question: str) -> Any:
    q = question.strip()
    ql = q.lower()
    records = all_records()
    if not records:
        return "The ledger data could not be loaded from the source API."

    # Match region/product/customer/month/year and common metric intent.
    year, month = parse_period(q)
    region_terms = ["north", "south", "east", "west", "central", "northeast", "northwest", "southeast", "southwest"]
    region = next((r for r in region_terms if re.search(r"\b" + r + r"\b", ql)), None)
    products = sorted({str(field(r, "product_name", "product", "item", "sku_name")) for r in records if field(r, "product_name", "product", "item", "sku_name") is not None}, key=len, reverse=True)
    customers = sorted({str(field(r, "customer_name", "customer", "client_name")) for r in records if field(r, "customer_name", "customer", "client_name") is not None}, key=len, reverse=True)
    product = next((x for x in products if x.lower() in ql), None)
    customer = next((x for x in customers if x.lower() in ql), None)

    want_refunds = any(x in ql for x in ("refund", "refunded", "returned"))
    want_count = any(x in ql for x in ("how many", "count", "number of orders", "number of customers"))
    want_customers = "customer" in ql and any(x in ql for x in ("how many", "count", "number", "unique", "distinct"))
    want_product_list = any(x in ql for x in ("which products", "list products", "what products", "product names"))
    filtered = []
    for r in records:
        d = field(r, "date", "order_date", "created_at", "transaction_date", "timestamp")
        ry, rm = date_parts(d)
        if year is not None and ry is not None and year != ry:
            continue
        if month is not None and rm is not None and month != rm:
            continue
        if region:
            rr = field(r, "region", "sales_region", "territory")
            if rr is None or region not in str(rr).lower():
                continue
        if product and str(field(r, "product_name", "product", "item", "sku_name")) != product:
            continue
        if customer and str(field(r, "customer_name", "customer", "client_name")) != customer:
            continue
        filtered.append(r)

    if want_product_list:
        names = sorted({str(field(r, "product_name", "product", "item", "sku_name")) for r in filtered if field(r, "product_name", "product", "item", "sku_name") is not None})
        return names

    if want_customers:
        names = {str(field(r, "customer_id", "customer_name", "customer", "client_name")) for r in filtered if field(r, "customer_id", "customer_name", "customer", "client_name") is not None}
        return len(names)

    if want_count:
        ids = [field(r, "order_id", "orderid", "id") for r in filtered]
        return len(set(map(str, ids))) if any(x is not None for x in ids) else len(filtered)

    amounts = [choose_amount(r, want_refunds) for r in filtered]
    if any(x in ql for x in ("average", "avg", "mean")):
        return round(float(sum(amounts, Decimal(0)) / max(len(amounts), 1)), 2)
    if any(x in ql for x in ("highest", "largest", "most")) and ("order" in ql or "customer" in ql or "product" in ql):
        # Return the label for the highest aggregate group when possible.
        group_key = "customer_name" if "customer" in ql else ("product_name" if "product" in ql else "order_id")
        totals = {}
        for r in filtered:
            label = field(r, group_key, group_key.replace("_", " "), "product", "customer", "order_id")
            if label is not None:
                totals[str(label)] = totals.get(str(label), Decimal(0)) + choose_amount(r, want_refunds)
        if totals:
            return max(totals.items(), key=lambda x: (x[1], x[0]))[0]
    return round(float(sum(amounts, Decimal(0))), 2)


@app.post("/answer")
@app.post("/")
def answer(payload: Question):
    try:
        return {"answer": answer_question(payload.question)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to answer question: {exc}")
