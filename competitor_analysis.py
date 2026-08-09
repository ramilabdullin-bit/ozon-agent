#!/usr/bin/env python3
"""Ad campaign competitor analysis: our CPC bids vs Ozon's own competitive-bid
signal, plus optional MPSTATS category context (top sellers by sales) for the
same niche.

Usage:
    python3 competitor_analysis.py <campaign_id> [mpstats_category_path]

Example:
    python3 competitor_analysis.py 24098195 "Красота и здоровье/Уход за ногтями"

If the category path is omitted, only the Ozon bid-gap section runs (no
MPSTATS calls). Look up the exact path with mpstats' own
scripts/ozon/ozon-categories-list.sh if unsure -- category strings must
match mpstats' tree exactly, there's no fuzzy matching here.
"""
import json
import subprocess
import sys
from datetime import date, timedelta

from ozon_client import load_env, OzonClient

BASE_DIR_MPSTATS = "/root/.claude/skills/mpstats/scripts/ozon/ozon-category.sh"

UNDERBID_RATIO = 0.5  # our bid below this fraction of Ozon's competitive bid -> flag


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


def mpstats_category_context(category_path: str, limit: int = 10) -> list:
    # MPSTATS rejects d2 == today ("d2 должно быть датой до <today>"), so the
    # script's own "today" default doesn't work here -- pin to yesterday.
    d2 = date.today() - timedelta(days=1)
    d1 = d2 - timedelta(days=30)
    proc = subprocess.run(
        [BASE_DIR_MPSTATS, category_path, "products", d1.isoformat(), d2.isoformat(), str(limit)],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(proc.stdout)
    if isinstance(data, dict) and "message" in data:
        raise RuntimeError(f"mpstats error: {data['message']}")
    return data.get("data", [])


def print_report(campaign_id: int, category_path: str | None):
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

    if category_path:
        print(f"\n=== MPSTATS: топ ниши «{category_path}» по продажам ===")
        try:
            top = mpstats_category_context(category_path)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"Не удалось получить данные MPSTATS: {e}")
            return
        if not top:
            print("Пусто -- проверь точное название категории через ozon-categories-list.sh.")
        for item in top[:10]:
            price = item.get("final_price") or item.get("price")
            print(f'  {item.get("name", "?")[:50]:<50}  цена={price}₽  '
                  f'продано={item.get("sales")}  бренд={item.get("brand")}')


def demo():
    """Self-check: bid-gap math and underbid flag logic, no network."""
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
    print("demo: bid-gap self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif len(sys.argv) > 1:
        print_report(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print("Usage: competitor_analysis.py <campaign_id> [mpstats_category_path] | demo")
