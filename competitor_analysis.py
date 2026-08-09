#!/usr/bin/env python3
"""Ad campaign competitor analysis: our CPC bids vs Ozon's own competitive-bid
signal, plus real named competing brands (not just generic top products) in
our own niche via MPSTATS.

Usage:
    python3 competitor_analysis.py <campaign_id> [--no-mpstats]

The MPSTATS section auto-detects our niche: reads our brand from the Seller
API (OzonClient.fetch_own_brand), asks MPSTATS which niche that brand earns
the most revenue in (brand/niches), then lists the top brands by revenue in
that exact niche (category/brands) -- these are the real competitors, not a
generic "top products in a loosely-matched category" list. No manual
category-path guessing needed.

Note: MPSTATS indexes by sales volume, so this only works for brands/SKUs
with real sales history -- confirmed live 2026-08-09 that a near-zero-sales
SKU looked up individually returns "SKU не найден", but the same cabinet's
brand (aggregated across all its SKUs) was indexed fine.
"""
import json
import subprocess
import sys
from datetime import date, timedelta

from ozon_client import load_env, OzonClient

MPSTATS_BRAND_SH = "/root/.claude/skills/mpstats/scripts/ozon/ozon-brand.sh"
MPSTATS_CATEGORY_SH = "/root/.claude/skills/mpstats/scripts/ozon/ozon-category.sh"

UNDERBID_RATIO = 0.5  # our bid below this fraction of Ozon's competitive bid -> flag
TOP_COMPETITORS = 8

# MPSTATS seller ids (Ozon public storefront id, NOT the Seller API Client-Id
# used for auth -- different id space) confirmed by the owner 2026-08-09 as
# our own cabinets selling brand INKI: BEST GOODS (76973), "Официальный
# представитель" (2017608), STORE for HOME (2728148). Anyone else selling
# INKI with real sales is an outside reseller, not us -- update this set if
# the owner opens/closes a cabinet.
OWN_SELLER_IDS = {76973, 2017608, 2728148}


def _mpstats_dates():
    # MPSTATS rejects d2 == today ("d2 должно быть датой до <today>").
    d2 = date.today() - timedelta(days=1)
    return (d2 - timedelta(days=30)).isoformat(), d2.isoformat()


def _run_mpstats(script: str, *args) -> object:
    proc = subprocess.run([script, *args], capture_output=True, text=True, timeout=60)
    data = json.loads(proc.stdout)
    if isinstance(data, dict) and "message" in data and "data" not in data:
        raise RuntimeError(f"mpstats error: {data['message']}")
    return data


def bid_gap_report(client: OzonClient, campaign_id: int) -> list:
    products = client.fetch_campaign_products(campaign_id)
    if not products:
        return []
    skus = [p["sku"] for p in products]
    competitive = client.fetch_competitive_bids(campaign_id, skus)

    rows = []
    for p in products:
        comp_bid = competitive.get(p["sku"], 0)
        ratio = (p["bid_rub"] / comp_bid) if comp_bid else None
        rows.append({
            **p,
            "competitive_bid_rub": comp_bid,
            "ratio": ratio,
            "underbid": ratio is not None and ratio < UNDERBID_RATIO,
        })
    rows.sort(key=lambda r: (r["ratio"] is None, r["ratio"] or 0))
    return rows


def our_primary_niche(brand: str) -> dict:
    """The niche where `brand` earns the most revenue on Ozon, per MPSTATS."""
    d1, d2 = _mpstats_dates()
    niches = _run_mpstats(MPSTATS_BRAND_SH, brand, "niches", d1, d2)
    if not niches:
        raise RuntimeError(f'MPSTATS has no niche data for brand "{brand}" (no sales history indexed?)')
    return max(niches, key=lambda n: n.get("revenue", 0))


def niche_competitor_brands(niche_path: str, our_brand: str, limit: int = TOP_COMPETITORS) -> tuple:
    """Real named brands ranked by revenue within the exact niche, split into
    our own row and the top competitors (brand-less "Бренд не указан" rows
    excluded -- that's an aggregate of untracked/generic listings, not a
    named competitor)."""
    d1, d2 = _mpstats_dates()
    brands = _run_mpstats(MPSTATS_CATEGORY_SH, niche_path, "brands", d1, d2, str(limit + 5))
    rows = brands.get("data", brands) if isinstance(brands, dict) else brands
    rows = [r for r in rows if r.get("name") and r["name"] != "Бренд не указан"]
    rows.sort(key=lambda r: r.get("revenue", 0), reverse=True)
    ours = next((r for r in rows if r["name"].lower() == our_brand.lower()), None)
    competitors = [r for r in rows if r is not ours][:limit]
    return ours, competitors


def brand_reseller_competitors(brand: str) -> list:
    """Other Ozon sellers with real sales of OUR OWN branded goods --
    excludes OWN_SELLER_IDS (our cabinets) and zero-sales noise rows (MPSTATS
    lists many sellers per brand with 0 sales -- listings/duplicates, not
    real market presence). What's left is a genuine competitive threat: a
    third party riding on our brand's demand, potentially undercutting our
    price. Sorted by revenue desc so the dominant one surfaces first."""
    d1, d2 = _mpstats_dates()
    sellers = _run_mpstats(MPSTATS_BRAND_SH, brand, "sellers", d1, d2)
    rows = sellers.get("data", sellers) if isinstance(sellers, dict) else sellers
    rows = [r for r in rows if r.get("sales") and r.get("id") not in OWN_SELLER_IDS]
    rows.sort(key=lambda r: r.get("revenue", 0), reverse=True)
    return rows


def print_report(campaign_id: int, run_mpstats: bool = True):
    env = load_env()
    client = OzonClient(env["OZON_CLIENT_ID"], env["OZON_API_KEY"],
                         env["OZON_PERF_CLIENT_ID"], env["OZON_PERF_CLIENT_SECRET"])

    print(f"=== Ставки vs конкурентный ориентир Ozon, кампания {campaign_id} ===")
    rows = bid_gap_report(client, campaign_id)
    if not rows:
        print("В кампании нет товаров.")
    for r in rows:
        flag = " <-- ЗАНИЖЕНА" if r["underbid"] else ""
        ratio_str = f'{r["ratio"]:.0%}' if r["ratio"] is not None else "н/д"
        print(f'  sku={r["sku"]:<12} наша={r["bid_rub"]:>7.2f}₽  '
              f'ozon-ориентир={r["competitive_bid_rub"]:>7.2f}₽  '
              f'({ratio_str}){flag}  {r["title"][:50]}')
    underbid_count = sum(1 for r in rows if r["underbid"])
    if underbid_count:
        print(f"\n{underbid_count} SKU с бидом ниже {UNDERBID_RATIO:.0%} от конкурентного ориентира Ozon.")
        print("Внимание: это сигнал для анализа, не команда поднять ставку -- кампания может")
        print("хорошо работать и на заниженном биде (см. фактический ДРР в daily_report.py")
        print("перед решением поднимать бид).")

    if not run_mpstats:
        return

    try:
        brand = client.fetch_own_brand()
        if not brand:
            print("\nMPSTATS: не удалось определить наш бренд (пустой каталог?), пропуск.")
            return
        niche = our_primary_niche(brand)
        ours, competitors = niche_competitor_brands(niche["name"], brand)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"\nMPSTATS: не удалось получить данные конкурентов ({e}).")
        return

    # NOTE: MPSTATS "avg_price" is a flat average across every listed SKU,
    # including ones with zero sales -- for INKI that's 4345 rubles, vastly
    # above the realized selling price (confirmed live 2026-08-09, see
    # CLAUDE.md). revenue/sales is the sales-weighted realized price and is
    # what's used below for the cross-brand comparison; "avg_price" is not
    # used for anything decision-relevant here.
    def realized_price(row):
        sales = row.get("sales") or 0
        return row["revenue"] / sales if sales else None

    print(f'\n=== MPSTATS: наш бренд "{brand}" в нише «{niche["name"]}» ===')
    our_price = realized_price(ours) if ours else None
    if ours:
        print(f'  Наши продажи={ours.get("sales")}  выручка={ours.get("revenue")}₽  '
              f'цена факт. продаж={our_price:.0f}₽  рейтинг={ours.get("rating")}')
    else:
        print(f'  "{brand}" не входит в топ брендов этой ниши по выручке за период (проверь другие ниши бренда).')

    print(f"\n  Реальные конкуренты (топ {len(competitors)} брендов по выручке в этой нише,")
    print("  цена — не каталожная avg_price MPSTATS, а revenue/sales, чтобы не искажалась")
    print("  непроданными дорогими позициями в каталоге):")
    for c in competitors:
        c_price = realized_price(c)
        price_delta = ""
        if our_price and c_price:
            diff_pct = (our_price / c_price - 1) * 100
            price_delta = f'  (наша цена {diff_pct:+.0f}% к их факт. цене)'
        c_price_str = f"{c_price:.0f}₽" if c_price else "н/д"
        print(f'  {c["name"]:<20} продажи={c.get("sales"):<7} выручка={c.get("revenue"):<10}₽  '
              f'цена факт. продаж={c_price_str:>8}  рейтинг={c.get("rating")}{price_delta}')

    try:
        resellers = brand_reseller_competitors(brand)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"\nMPSTATS: не удалось получить продавцов бренда ({e}).")
        return
    print(f'\n=== MPSTATS: посторонние продавцы, торгующие нашим брендом "{brand}" ===')
    if not resellers:
        print("  Не найдено -- бренд продают только наши кабинеты.")
    for r in resellers:
        r_price = realized_price(r)
        r_price_str = f"{r_price:.0f}₽" if r_price else "н/д"
        print(f'  id={r["id"]:<10} {r["name"]:<20} продажи={r.get("sales"):<7} '
              f'выручка={r.get("revenue"):<10}₽  цена факт. продаж={r_price_str}  '
              "<-- РЕАЛЬНЫЙ КОНКУРЕНТ (перепродаёт наш бренд)")


def demo():
    """Self-check: bid-gap math/underbid flag and competitor-brand filtering, no network."""
    class FakeClient:
        def fetch_campaign_products(self, campaign_id):
            return [
                {"sku": "1", "title": "низкий бид", "bid_rub": 5.0},
                {"sku": "2", "title": "нормальный бид", "bid_rub": 60.0},
            ]

        def fetch_competitive_bids(self, campaign_id, skus):
            return {"1": 70.0, "2": 65.0}

    rows = bid_gap_report(FakeClient(), 1)
    assert rows[0]["sku"] == "1" and rows[0]["underbid"], "low bid should be flagged and sorted first"
    assert not rows[1]["underbid"], "bid above threshold should not be flagged"

    fake_brands = {"data": [
        {"name": "Бренд не указан", "revenue": 999999, "sales": 1000, "avg_price": 999},
        {"name": "US", "revenue": 1000, "avg_price": 5000, "sales": 10, "rating": 4.5},  # avg_price way off realized price (1000/10=100) -- must not be used for the comparison
        {"name": "Rival", "revenue": 500, "avg_price": 400, "sales": 50, "rating": 4.8},
    ]}
    import unittest.mock as mock
    # patch by module object, not string name -- this file may run as
    # "__main__" rather than "competitor_analysis", where the string form
    # would silently patch a separate re-imported copy and do nothing
    with mock.patch.object(sys.modules[__name__], "_run_mpstats", return_value=fake_brands):
        ours, competitors = niche_competitor_brands("Fake/Niche", "US")
    assert ours["name"] == "US", "should find our own brand case-insensitively"
    assert all(c["name"] != "Бренд не указан" for c in competitors), "unbranded aggregate row must be excluded"
    assert competitors[0]["name"] == "Rival", "competitors should be sorted by revenue desc"

    fake_sellers = {"data": [
        {"id": 76973, "name": "наш кабинет 1", "sales": 100, "revenue": 1000},
        {"id": 999, "name": "чужой ноль продаж", "sales": 0, "revenue": 0},
        {"id": 307611, "name": "BLScosmetics", "sales": 50, "revenue": 5000},
    ]}
    with mock.patch.object(sys.modules[__name__], "_run_mpstats", return_value=fake_sellers), \
         mock.patch.object(sys.modules[__name__], "OWN_SELLER_IDS", {76973}):
        resellers = brand_reseller_competitors("US")
    assert [r["id"] for r in resellers] == [307611], "own cabinet and zero-sales noise must be excluded"
    print("demo: bid-gap + competitor-brand + brand-reseller self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif len(sys.argv) > 1:
        print_report(int(sys.argv[1]), run_mpstats="--no-mpstats" not in sys.argv)
    else:
        print("Usage: competitor_analysis.py <campaign_id> [--no-mpstats] | demo")
