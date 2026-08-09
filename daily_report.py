#!/usr/bin/env python3
"""Daily Ozon ads report for the previous calendar day, across ALL running
campaigns (not a sample). Pure aggregation + rule-based flags, no LLM call
-- cheap enough to run every morning via cron.

Sends a short summary to Telegram (admin bot) + attaches a detailed PDF.
See ozon_daily_report.cron.txt for the cron entry.

Ozon's stats CSV has two different column schemas depending on campaign
type (confirmed live 2026-08-09 across REF_VK/REF_BLOGGER/SEARCH_PROMO
vs SKU/ALL_SKU_PROMO) -- normalize_totals() detects which one a row uses
by column presence, not by advObjectType, so any future campaign type
that reuses one of these two shapes is handled without a code change.
"""
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from fpdf import FPDF

sys.path.insert(0, str(Path(__file__).parent))
from ozon_client import OzonClient, load_env  # noqa: E402

BOT_ENV = Path("/root/telegram-claude-admin-bot/.env")
OWNER_CHAT_ID = "863886461"
REPORT_DIR = Path(__file__).parent / "reports"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ponytail: fixed thresholds, tune here if the report gets too noisy/quiet
MIN_SPEND_TO_FLAG = 300.0        # ignore "no orders" flag below this daily spend
HIGH_DRR_PCT = 15.0              # ad spend / revenue ratio considered high
DAILY_BUDGET_WARN_PCT = 90.0     # day's spend this close to the campaign's daily budget cap
WEEKLY_DAY_SPEND_WARN_PCT = 50.0  # one day already burned this much of the weekly budget


def _num(v, cast=float):
    if v in (None, ""):
        return 0
    try:
        return cast(float(str(v).replace(",", ".")))
    except ValueError:
        return 0


def normalize_totals(rows: list) -> dict | None:
    total = next((r for r in rows if r.get("sku") == "Всего"), None)
    if total is None:
        return None
    impressions = _num(total.get("Показы"), int)
    clicks = _num(total.get("Клики"), int)
    spend = _num(total.get("Расход, ₽, с НДС"))
    ctr = _num(total.get("CTR (%)") or total.get("CTR, %"))
    if "Заказы" in total:  # REF_VK / REF_BLOGGER / SEARCH_PROMO schema
        orders = _num(total.get("Заказы"), int)
        model_orders = _num(total.get("Заказы модели"), int)
        revenue = _num(total.get("Выручка, ₽"))
        drr = round(spend / revenue * 100, 1) if revenue else None
    else:  # SKU / ALL_SKU_PROMO schema
        orders = _num(total.get("Продано товаров"), int)
        model_orders = _num(total.get("Продано товаров модели"), int)
        revenue = _num(total.get("Заказано на сумму, ₽"))
        drr = _num(total.get("ДРР (общий), %"))
    return dict(impressions=impressions, clicks=clicks, spend=spend, ctr=ctr,
                orders=orders, model_orders=model_orders, revenue=revenue, drr=drr)


def flag_row(totals: dict) -> list:
    flags = []
    if totals["impressions"] == 0:
        flags.append("простаивает")
    if totals["spend"] >= MIN_SPEND_TO_FLAG and totals["orders"] == 0 and totals["model_orders"] == 0:
        flags.append("расход без заказов")
    if totals["drr"] is not None and totals["drr"] > HIGH_DRR_PCT:
        flags.append(f"высокий ДРР {totals['drr']}%")
    daily_budget = totals.get("daily_budget") or 0
    weekly_budget = totals.get("weekly_budget") or 0
    if daily_budget and totals["spend"] >= daily_budget * DAILY_BUDGET_WARN_PCT / 100:
        flags.append("упёрлась в дневной бюджет")
    if weekly_budget and totals["spend"] >= weekly_budget * WEEKLY_DAY_SPEND_WARN_PCT / 100:
        pct = round(totals["spend"] / weekly_budget * 100)
        flags.append(f"за день потрачено {pct}% недельного бюджета")
    return flags


def build_data(client: OzonClient, day: date):
    campaigns = client.fetch_campaigns()
    running = [c for c in campaigns if c.get("state") == "CAMPAIGN_STATE_RUNNING"]
    ids = [c["id"] for c in running]
    stats = client.fetch_campaign_stats_batched(ids, day.isoformat(), day.isoformat()) if ids else {}
    rows = []
    for c in running:
        totals = normalize_totals(stats.get(c["id"], []))
        if totals is None:
            continue
        totals["id"] = c["id"]
        totals["type"] = c.get("advObjectType", "")
        totals["daily_budget"] = _num(c.get("dailyBudget")) / 1_000_000
        totals["weekly_budget"] = _num(c.get("weeklyBudget")) / 1_000_000
        totals["flags"] = flag_row(totals)
        rows.append(totals)
    rows.sort(key=lambda r: -r["spend"])
    return rows, len(campaigns), len(running)


def summarize(rows: list, total_campaigns: int, running_count: int) -> dict:
    return dict(
        total_spend=sum(r["spend"] for r in rows),
        total_impr=sum(r["impressions"] for r in rows),
        total_clicks=sum(r["clicks"] for r in rows),
        total_orders=sum(r["orders"] + r["model_orders"] for r in rows),
        idle=[r for r in rows if "простаивает" in r["flags"]],
        no_conv=[r for r in rows if "расход без заказов" in r["flags"]],
        high_drr=[r for r in rows if any(f.startswith("высокий ДРР") for f in r["flags"])],
        budget_warn=[r for r in rows if any("бюджет" in f for f in r["flags"])],
        total_campaigns=total_campaigns,
        running_count=running_count,
    )


def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", " ")


def build_telegram_text(day: date, s: dict) -> str:
    lines = [
        f"📊 Ozon реклама — {day.strftime('%d.%m.%Y')}",
        f"Всего РК: {s['total_campaigns']}, активных: {s['running_count']}",
        f"Расход: {_fmt(s['total_spend'])} ₽ | показы: {_fmt(s['total_impr'])} | "
        f"клики: {_fmt(s['total_clicks'])} | заказы: {_fmt(s['total_orders'])}",
        "",
    ]
    if s["no_conv"]:
        lines.append(f"⚠️ {len(s['no_conv'])} РК потратили бюджет без заказов")
    if s["high_drr"]:
        lines.append(f"⚠️ {len(s['high_drr'])} РК с высоким ДРР (>{HIGH_DRR_PCT:.0f}%)")
    if s["idle"]:
        lines.append(f"💤 {len(s['idle'])} активных РК без показов за день")
    if s["budget_warn"]:
        lines.append(f"💰 {len(s['budget_warn'])} РК близко к пределу бюджета")
    if not (s["no_conv"] or s["high_drr"] or s["idle"] or s["budget_warn"]):
        lines.append("Аномалий не найдено.")
    lines.append("\nПодробности — в приложенном PDF.")
    return "\n".join(lines)


def build_pdf(day: date, rows: list, s: dict, out_path: Path):
    pdf = FPDF()
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()

    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 10, f"Ozon — отчёт по рекламе за {day.strftime('%d.%m.%Y')}", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("DejaVu", "", 11)
    pdf.ln(2)
    pdf.multi_cell(0, 6,
        f"Всего кампаний в кабинете: {s['total_campaigns']}, активных: {s['running_count']}\n"
        f"Суммарный расход: {_fmt(s['total_spend'])} ₽\n"
        f"Показы: {_fmt(s['total_impr'])} | Клики: {_fmt(s['total_clicks'])} | "
        f"Заказы (прямые + модельные): {_fmt(s['total_orders'])}",
        new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(0, 8, "Рекомендации", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    recs = []
    for r in s["no_conv"]:
        recs.append(f"- РК {r['id']} ({r['type']}): потрачено {r['spend']:.0f} ₽ без единого заказа — "
                     f"проверить или приостановить.")
    for r in s["high_drr"]:
        recs.append(f"- РК {r['id']} ({r['type']}): ДРР {r['drr']}% — реклама съедает большую долю "
                     f"выручки, снизить ставку.")
    for r in s["idle"][:10]:
        recs.append(f"- РК {r['id']} ({r['type']}): активна, но 0 показов за день — поднять ставку "
                     f"или приостановить.")
    for r in s["budget_warn"]:
        budget_flag = next(f for f in r["flags"] if "бюджет" in f)
        recs.append(f"- РК {r['id']} ({r['type']}): {budget_flag} — проверить темп расхода.")
    if len(s["idle"]) > 10:
        recs.append(f"...и ещё {len(s['idle']) - 10} простаивающих РК (полный список — в таблице ниже).")
    if not recs:
        recs.append("Существенных проблем не найдено.")
    for line in recs:
        pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(0, 8, "Все активные кампании (по убыванию расхода)", new_x="LMARGIN", new_y="NEXT")

    headers = ["ID", "Тип", "Показы", "Клики", "CTR%", "Расход ₽", "Заказы", "ДРР%", "Флаги"]
    widths = [20, 24, 18, 16, 12, 20, 16, 14, 50]

    def header_row():
        pdf.set_font("DejaVu", "B", 8)
        for h, w in zip(headers, widths):
            pdf.cell(w, 7, h, border=1)
        pdf.ln()
        pdf.set_font("DejaVu", "", 8)

    header_row()
    for r in rows:
        if pdf.get_y() > 270:  # ponytail: no repeated header on page break, just a fresh one
            pdf.add_page()
            header_row()
        vals = [r["id"], r["type"], _fmt(r["impressions"]), _fmt(r["clicks"]),
                f"{r['ctr']:.1f}", f"{r['spend']:.0f}", str(r["orders"] + r["model_orders"]),
                f"{r['drr']:.1f}" if r["drr"] is not None else "-", ", ".join(r["flags"])]
        for v, w in zip(vals, widths):
            pdf.cell(w, 6, str(v), border=1)
        pdf.ln()

    pdf.output(str(out_path))


def send_telegram(text: str, pdf_path: Path):
    token = None
    for line in BOT_ENV.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
    if not token:
        raise RuntimeError(f"no TELEGRAM_BOT_TOKEN in {BOT_ENV}")
    base = f"https://api.telegram.org/bot{token}"
    subprocess.run(["curl", "-s", "-X", "POST", f"{base}/sendMessage",
                     "-d", f"chat_id={OWNER_CHAT_ID}", "--data-urlencode", f"text={text}"],
                    check=True)
    subprocess.run(["curl", "-s", "-X", "POST", f"{base}/sendDocument",
                     "-F", f"chat_id={OWNER_CHAT_ID}", "-F", f"document=@{pdf_path}"],
                    check=True)


def main():
    day = date.today() - timedelta(days=1)
    env = load_env()
    client = OzonClient(env["OZON_CLIENT_ID"], env["OZON_API_KEY"],
                         env["OZON_PERF_CLIENT_ID"], env["OZON_PERF_CLIENT_SECRET"])
    rows, total_campaigns, running_count = build_data(client, day)
    s = summarize(rows, total_campaigns, running_count)
    text = build_telegram_text(day, s)

    REPORT_DIR.mkdir(exist_ok=True)
    pdf_path = REPORT_DIR / f"ozon_report_{day.isoformat()}.pdf"
    build_pdf(day, rows, s, pdf_path)

    send_telegram(text, pdf_path)
    print(text)
    print(f"PDF: {pdf_path}")


if __name__ == "__main__":
    main()
