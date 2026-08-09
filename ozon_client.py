#!/usr/bin/env python3
"""Ozon API client: Seller API (products/content) + Performance API (ads).

Two separate services with separate auth, confirmed live 2026-08-09:
- Seller API (api-seller.ozon.ru): static `Client-Id`/`Api-Key` headers.
- Performance API (api-performance.ozon.ru): OAuth2 client_credentials
  (different client_id/client_secret pair, obtained separately in
  seller.ozon.ru -> Настройки -> API-ключи -> Performance API), Bearer
  token expires in 1800s, refreshed transparently here.

No offer/ToS check has been done for Ozon yet (unlike WB, see
/root/wb-agent/CLAUDE.md) -- the two PDFs reviewed so far didn't contain an
equivalent clause, but that's "not found", not "confirmed absent". Don't
assume browser automation is fine here without checking further first.
"""
import csv
import io
import sqlite3
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "ozon.db"

SELLER_BASE = "https://api-seller.ozon.ru"
PERF_BASE = "https://api-performance.ozon.ru"

CONFIRM_PHRASE = "подтверждаю"


def load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class ConfirmationRequired(Exception):
    pass


def _check_confirm(confirm, action):
    if (confirm or "").strip().lower() != CONFIRM_PHRASE:
        raise ConfirmationRequired(
            f'Refusing to {action}: requires --confirm "{CONFIRM_PHRASE}" '
            "(the owner's explicit confirmation for this specific action, "
            "not a general task go-ahead)."
        )


class OzonClient:
    def __init__(self, client_id: str, api_key: str, perf_client_id: str, perf_client_secret: str):
        self.seller_session = requests.Session()
        self.seller_session.headers.update({"Client-Id": client_id, "Api-Key": api_key})
        self.perf_client_id = perf_client_id
        self.perf_client_secret = perf_client_secret
        self._perf_token = None
        self._perf_token_expires = 0

    def _perf_headers(self) -> dict:
        if not self._perf_token or time.time() > self._perf_token_expires - 60:
            resp = requests.post(
                f"{PERF_BASE}/api/client/token",
                json={
                    "client_id": self.perf_client_id,
                    "client_secret": self.perf_client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._perf_token = data["access_token"]
            self._perf_token_expires = time.time() + data.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._perf_token}"}

    def _request(self, session_or_headers, method, base, path, **kwargs):
        url = f"{base}{path}"
        for attempt in range(5):
            if isinstance(session_or_headers, dict):
                resp = requests.request(method, url, headers=session_or_headers, timeout=30, **kwargs)
            else:
                resp = session_or_headers.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 30))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()

    # ---- Seller API: read-only ----

    def fetch_products(self, limit: int = 100, last_id: str = "") -> dict:
        resp = self._request(
            self.seller_session, "POST", SELLER_BASE, "/v3/product/list",
            json={"filter": {}, "last_id": last_id, "limit": limit},
        )
        return resp.json()["result"]

    def fetch_product_description(self, product_id: int) -> dict:
        resp = self._request(
            self.seller_session, "POST", SELLER_BASE, "/v1/product/info/description",
            json={"product_id": product_id},
        )
        return resp.json()["result"]

    # ---- Seller API: mutating (confirm-gated) ----

    DESCRIPTION_ATTRIBUTE_ID = 4191  # confirmed live 2026-08-09 -- description is
    # stored as a regular attribute, not a top-level field

    def _fetch_full_product_snapshot(self, product_id: int) -> dict:
        """Everything /v3/product/import needs to safely round-trip a
        product without wiping fields (confirmed live 2026-08-09 -- the
        endpoint is a full replace, no partial-patch semantics: sending
        only name/description would blank out category/attributes/images/
        price/etc). Merges /v4/product/info/attributes (content) with
        /v5/product/info/prices (price/vat, not included in the first
        call)."""
        attrs_resp = self._request(
            self.seller_session, "POST", SELLER_BASE, "/v4/product/info/attributes",
            json={"filter": {"product_id": [str(product_id)]}, "limit": 1},
        ).json()["result"][0]
        price_resp = self._request(
            self.seller_session, "POST", SELLER_BASE, "/v5/product/info/prices",
            json={"filter": {"product_id": [str(product_id)]}, "limit": 1},
        ).json()["items"][0]["price"]
        return {
            "offer_id": attrs_resp["offer_id"],
            "name": attrs_resp["name"],
            "description_category_id": attrs_resp["description_category_id"],
            "type_id": attrs_resp["type_id"],
            "barcode": attrs_resp["barcode"],
            "images": attrs_resp["images"],
            "primary_image": attrs_resp["primary_image"],
            "height": attrs_resp["height"],
            "depth": attrs_resp["depth"],
            "width": attrs_resp["width"],
            "dimension_unit": attrs_resp["dimension_unit"],
            "weight": attrs_resp["weight"],
            "weight_unit": attrs_resp["weight_unit"],
            "attributes": attrs_resp["attributes"],
            "price": str(price_resp["price"]),
            "old_price": str(price_resp["old_price"]),
            "currency_code": price_resp["currency_code"],
            "vat": str(price_resp["vat"]),
        }

    def update_product_seo(self, product_id: int, name: str | None = None,
                            description: str | None = None, confirm: str | None = None):
        """Confirmed live 2026-08-09 (product_id 51483692, tested and
        reverted) -- fetches the current full product state first, patches
        only name/description on top of it, then does the full-payload
        import /v3/product/import requires. Async: returns the import
        task_id: {"task_id": ...} -- poll /v1/product/import/info with
        {"task_id": ...} for status/errors."""
        _check_confirm(confirm, f"update product SEO for product_id {product_id}")
        item = self._fetch_full_product_snapshot(product_id)
        if name is not None:
            item["name"] = name
        if description is not None:
            for attr in item["attributes"]:
                if attr["id"] == self.DESCRIPTION_ATTRIBUTE_ID:
                    attr["values"][0]["value"] = description
                    break
        resp = self._request(
            self.seller_session, "POST", SELLER_BASE, "/v3/product/import",
            json={"items": [item]},
        )
        return resp.json()["result"]

    # ---- Performance API: read-only ----

    def fetch_campaigns(self) -> list:
        resp = self._request(self._perf_headers(), "GET", PERF_BASE, "/api/client/campaign")
        return resp.json().get("list", [])

    MAX_CAMPAIGNS_PER_STATS_REQUEST = 10  # confirmed live 2026-08-09: "Превышен лимит по количеству кампаний (максимум 10)"

    def fetch_campaign_stats(self, campaign_ids: list, date_from: str, date_to: str,
                              poll_timeout: int = 60) -> dict:
        """Submit a stats report, poll until ready, download and parse the
        per-campaign CSVs inside the returned zip. Confirmed live 2026-08-09:
        submit -> {"UUID": ...}, poll /statistics/{UUID} -> state OK, then
        GET /statistics/report?UUID=... returns a zip of
        "<campaignId>_<from>-<to>.csv" files. Max 10 campaign_ids per call --
        batch and merge for more (see fetch_campaign_stats_batched)."""
        if len(campaign_ids) > self.MAX_CAMPAIGNS_PER_STATS_REQUEST:
            raise ValueError(
                f"max {self.MAX_CAMPAIGNS_PER_STATS_REQUEST} campaign_ids per call, "
                f"got {len(campaign_ids)} -- use fetch_campaign_stats_batched"
            )
        headers = self._perf_headers()
        submit = self._request(
            headers, "POST", PERF_BASE, "/api/client/statistics",
            json={"campaigns": campaign_ids, "dateFrom": date_from, "dateTo": date_to},
        )
        uuid = submit.json()["UUID"]

        deadline = time.time() + poll_timeout
        state = None
        while time.time() < deadline:
            status = self._request(headers, "GET", PERF_BASE, f"/api/client/statistics/{uuid}")
            state = status.json().get("state")
            if state == "OK":
                break
            if state in ("ERROR", "CANCELLED"):
                raise RuntimeError(f"Ozon stats report {uuid} failed: state={state}")
            time.sleep(3)
        else:
            raise TimeoutError(f"Ozon stats report {uuid} not ready after {poll_timeout}s (last state={state})")

        report = self._request(
            headers, "GET", PERF_BASE, "/api/client/statistics/report", params={"UUID": uuid}
        )
        return self._parse_stats_report(report.content, campaign_ids)

    @staticmethod
    def _parse_csv_skip_title(raw: str) -> list:
        # First line is "Рекламная кампания № <id>, период <from>-<to>" (a
        # title, not headers) -- confirmed live 2026-08-09. Real column
        # headers are line 2.
        lines = raw.splitlines()
        return list(csv.DictReader(lines[1:], delimiter=";"))

    def _parse_stats_report(self, content: bytes, campaign_ids: list) -> dict:
        # Confirmed live 2026-08-09: a single-campaign request returns a
        # plain CSV (Content-Type text/csv), multi-campaign returns a zip
        # of one CSV per campaign -- same title-line-then-headers shape
        # either way.
        result = {}
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    campaign_id = name.split("_")[0]
                    raw = zf.read(name).decode("utf-8-sig")
                    result[campaign_id] = self._parse_csv_skip_title(raw)
        except zipfile.BadZipFile:
            campaign_id = campaign_ids[0] if len(campaign_ids) == 1 else "unknown"
            result[campaign_id] = self._parse_csv_skip_title(content.decode("utf-8-sig"))
        return result

    def fetch_campaign_stats_batched(self, campaign_ids: list, date_from: str, date_to: str) -> dict:
        result = {}
        for i in range(0, len(campaign_ids), self.MAX_CAMPAIGNS_PER_STATS_REQUEST):
            batch = campaign_ids[i:i + self.MAX_CAMPAIGNS_PER_STATS_REQUEST]
            result.update(self.fetch_campaign_stats(batch, date_from, date_to))
        return result

    def fetch_campaign_products(self, campaign_id: int) -> list:
        """GET /api/client/campaign/{id}/v2/products -- confirmed live
        2026-08-09 (campaign 24098195). Returns our SKUs with current CPC
        bid in rubles (API unit is millionths of a ruble, same convention
        as budget -- converted here)."""
        resp = self._request(
            self._perf_headers(), "GET", PERF_BASE, f"/api/client/campaign/{campaign_id}/v2/products"
        )
        return [
            {"sku": p["sku"], "title": p["title"], "bid_rub": int(p["bid"]) / 1_000_000}
            for p in resp.json().get("products", [])
        ]

    def fetch_competitive_bids(self, campaign_id: int, skus: list) -> dict:
        """GET /api/client/campaign/{id}/products/bids/competitive --
        confirmed live 2026-08-09. Ozon's own signal of the CPC bid needed
        to compete for placement on each SKU right now. Max 200 SKUs/call
        per the spec (github.com/PCDCK/ozon-mcp perf_swagger.json) --
        untested above that. bid 0 means the SKU isn't in the campaign."""
        resp = self._request(
            self._perf_headers(), "GET", PERF_BASE,
            f"/api/client/campaign/{campaign_id}/products/bids/competitive",
            params=[("skus", s) for s in skus],
        )
        return {b["sku"]: int(b["bid"]) / 1_000_000 for b in resp.json().get("bids", [])}

    # ---- Performance API: mutating (confirm-gated, endpoints UNVERIFIED) ----

    def start_campaign(self, campaign_id: int, confirm: str | None = None):
        """Ozon API: unverified against live response -- path inferred from
        common Ozon Performance API convention, not confirmed by an actual
        call (would require a real campaign state change to test)."""
        _check_confirm(confirm, f"start campaign {campaign_id}")
        return self._request(
            self._perf_headers(), "POST", PERF_BASE, f"/api/client/campaign/{campaign_id}/activate"
        ).json()

    def pause_campaign(self, campaign_id: int, confirm: str | None = None):
        _check_confirm(confirm, f"pause campaign {campaign_id}")
        return self._request(
            self._perf_headers(), "POST", PERF_BASE, f"/api/client/campaign/{campaign_id}/deactivate"
        ).json()

    def update_campaign_budget(self, campaign_id: int, rubles: float, confirm: str | None = None):
        """PATCH /api/client/campaign/{campaignId} -- confirmed live
        2026-08-09 (campaign 27767900: 10000 -> 10001 -> reverted to 10000
        RUB, verified via fetch_campaigns() after each step). Amount unit
        is millionths of a ruble (1_000_000 = 1 RUB). Budget type (daily vs
        weekly) is fixed at campaign creation and can't be switched, so
        this reads the campaign's current budgetType first and sends
        whichever field applies."""
        _check_confirm(confirm, f"update budget for campaign {campaign_id}")
        campaign = next(
            (c for c in self.fetch_campaigns() if str(c["id"]) == str(campaign_id)), None
        )
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")
        budget_type = campaign.get("budgetType")
        micro = str(int(round(rubles * 1_000_000)))
        if budget_type == "PRODUCT_CAMPAIGN_BUDGET_TYPE_DAILY":
            body = {"dailyBudget": micro}
        elif budget_type == "PRODUCT_CAMPAIGN_BUDGET_TYPE_WEEKLY":
            body = {"weeklyBudget": micro}
        else:
            raise ValueError(
                f"campaign {campaign_id} has no budget type set (budgetType={budget_type}) "
                "-- can't tell whether to send dailyBudget or weeklyBudget"
            )
        return self._request(
            self._perf_headers(), "PATCH", PERF_BASE, f"/api/client/campaign/{campaign_id}", json=body
        ).json()

    def update_product_bids(self, campaign_id: int, bids: dict, confirm: str | None = None):
        """PUT /api/client/campaign/{id}/products -- path/schema taken from
        github.com/PCDCK/ozon-mcp perf_swagger.json (OpenAPI spec extracted
        from Ozon Performance API, same source already used for
        update_campaign_budget), cross-checked against the live GET
        .../v2/products response on 2026-08-09 (same "bid" field, same
        millionths-of-a-ruble unit, x-code-sample in the spec matches).
        NOT yet confirmed by an actual PUT call. `bids`: {sku: rubles}.
        WARNING: per the spec this PUT is a full replace of the campaign's
        bid list (also wipes any phrase/stop-word data if present) -- pass
        every SKU you want to keep an active bid for, not just the ones
        you're changing."""
        _check_confirm(confirm, f"update product bids for campaign {campaign_id} ({len(bids)} SKUs)")
        body = {"bids": [
            {"sku": str(sku), "bid": str(int(round(rub * 1_000_000)))}
            for sku, rub in bids.items()
        ]}
        return self._request(
            self._perf_headers(), "PUT", PERF_BASE, f"/api/client/campaign/{campaign_id}/products", json=body
        ).json()


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campaign_stats (
            snapshot_date TEXT,
            campaign_id TEXT,
            row_json TEXT,
            PRIMARY KEY (snapshot_date, campaign_id)
        )
    """)
    conn.commit()


def store_campaign_stats(conn: sqlite3.Connection, stats: dict):
    import json as _json
    today = date.today().isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO campaign_stats (snapshot_date, campaign_id, row_json) VALUES (?, ?, ?)",
        [(today, cid, _json.dumps(rows, ensure_ascii=False)) for cid, rows in stats.items()],
    )
    conn.commit()


def build_report(client: OzonClient, conn: sqlite3.Connection, days: int = 7) -> str:
    lines = []
    date_from = (date.today() - timedelta(days=days)).isoformat()
    date_to = date.today().isoformat()

    campaigns = client.fetch_campaigns()
    running = [c for c in campaigns if c.get("state") == "CAMPAIGN_STATE_RUNNING"]
    lines.append(f"РК: {len(campaigns)} всего, {len(running)} активных (RUNNING).")

    sample_ids = [c["id"] for c in running[:20]]
    if sample_ids:
        try:
            stats = client.fetch_campaign_stats_batched(sample_ids, date_from, date_to)
            store_campaign_stats(conn, stats)
            lines.append(f"Статистика получена по {len(stats)} кампаниям за {days} дн. (первые 20 активных).")
        except (TimeoutError, RuntimeError) as e:
            lines.append(f"Статистика: не удалось получить ({e}).")

    products = client.fetch_products(limit=1)
    lines.append(f"Товары в кабинете: {products['total']}.")

    return "\n".join(lines)


def demo():
    """Self-check: confirm gate blocks mutations without --confirm, no network call happens."""
    c = OzonClient("dummy", "dummy", "dummy", "dummy")
    for fn, args in [
        (c.start_campaign, (123,)),
        (c.pause_campaign, (123,)),
        (c.update_product_seo, (123,)),
        (c.update_campaign_budget, (123, 500)),
        (c.update_product_bids, (123, {456: 5.0})),
    ]:
        try:
            fn(*args)
            raise AssertionError(f"{fn.__name__} did not raise ConfirmationRequired")
        except ConfirmationRequired:
            pass
    print("demo: confirm-gate self-check passed")


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif len(sys.argv) > 1 and sys.argv[1] == "verify":
        env = load_env()
        client = OzonClient(env["OZON_CLIENT_ID"], env["OZON_API_KEY"],
                             env["OZON_PERF_CLIENT_ID"], env["OZON_PERF_CLIENT_SECRET"])
        products = client.fetch_products(limit=5)
        print("products:", json.dumps(products, ensure_ascii=False, indent=2)[:1500])
        campaigns = client.fetch_campaigns()
        print(f"campaigns: {len(campaigns)} total")
        for camp in campaigns[:10]:
            print(f"  id={camp['id']} state={camp['state']} type={camp.get('advObjectType')}")
    elif len(sys.argv) > 1 and sys.argv[1] == "report":
        env = load_env()
        client = OzonClient(env["OZON_CLIENT_ID"], env["OZON_API_KEY"],
                             env["OZON_PERF_CLIENT_ID"], env["OZON_PERF_CLIENT_SECRET"])
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        print(build_report(client, conn))
        conn.close()
    else:
        print("Usage: ozon_client.py demo|verify|report")
