"""Self-check for daily_report.py's schema normalization + flags.
Run: python3 test_daily_report.py
"""
from daily_report import normalize_totals, flag_row, MIN_SPEND_TO_FLAG, HIGH_DRR_PCT

# Real "Всего" rows captured live 2026-08-09 from two different Ozon
# campaign types (banner-style REF_VK/REF_BLOGGER/SEARCH_PROMO vs
# SKU-promo ALL_SKU_PROMO/SKU) -- see daily_report.py docstring.
SCHEMA_A_IDLE = {"sku": "Всего", "Показы": "0", "Клики": "0", "CTR (%)": "0,00",
                  "Расход, ₽, с НДС": "0,00", "Заказы": "0", "Заказы модели": "0",
                  "Выручка, ₽": "0,00"}

SCHEMA_B_ACTIVE = {"sku": "Всего", "Показы": "230612", "Клики": "5623", "CTR, %": "2,44",
                     "Расход, ₽, с НДС": "34760,22", "Продано товаров": "403",
                     "Продано товаров модели": "73", "Заказано на сумму, ₽": "1259092,00",
                     "ДРР (общий), %": "2,8"}

SCHEMA_A_NO_ORDERS = {"sku": "Всего", "Показы": "1000", "Клики": "20", "CTR (%)": "2,00",
                        "Расход, ₽, с НДС": "500,00", "Заказы": "0", "Заказы модели": "0",
                        "Выручка, ₽": "0,00"}


def test_idle_flagged():
    totals = normalize_totals([SCHEMA_A_IDLE])
    assert totals["impressions"] == 0
    assert "простаивает" in flag_row(totals)


def test_active_sku_campaign_not_flagged_no_orders():
    totals = normalize_totals([SCHEMA_B_ACTIVE])
    assert totals["orders"] == 403
    assert totals["drr"] == 2.8
    flags = flag_row(totals)
    assert "расход без заказов" not in flags
    assert not any(f.startswith("высокий ДРР") for f in flags)


def test_spend_without_orders_flagged():
    totals = normalize_totals([SCHEMA_A_NO_ORDERS])
    assert totals["spend"] >= MIN_SPEND_TO_FLAG
    assert "расход без заказов" in flag_row(totals)


def test_missing_totals_row_returns_none():
    assert normalize_totals([{"sku": "12345", "Показы": "10"}]) is None


def test_daily_budget_near_cap_flagged():
    totals = normalize_totals([SCHEMA_B_ACTIVE])
    totals["daily_budget"] = 34760.22 / 0.95  # spend is ~95% of this budget
    totals["weekly_budget"] = 0
    assert "упёрлась в дневной бюджет" in flag_row(totals)


def test_weekly_budget_fast_burn_flagged():
    totals = normalize_totals([SCHEMA_A_NO_ORDERS])  # spend 500
    totals["daily_budget"] = 0
    totals["weekly_budget"] = 800  # 500 / 800 = 62.5% burned in a single day
    flags = flag_row(totals)
    assert any(f.startswith("за день потрачено") for f in flags)


def test_no_budget_flag_when_budget_unset():
    totals = normalize_totals([SCHEMA_B_ACTIVE])  # spend 34760.22, no budget fields set
    flags = flag_row(totals)
    assert not any("бюджет" in f for f in flags)


if __name__ == "__main__":
    test_idle_flagged()
    test_active_sku_campaign_not_flagged_no_orders()
    test_spend_without_orders_flagged()
    test_missing_totals_row_returns_none()
    test_daily_budget_near_cap_flagged()
    test_weekly_budget_fast_burn_flagged()
    test_no_budget_flag_when_budget_unset()
    print("test_daily_report: all passed")
