---
name: ozon
description: Entry point for the Ozon seller-cabinet agent (/root/ozon-agent). Use when asked about Ozon campaigns, card SEO, or "как там озон" for this cabinet. Currently covers one block (ozon-search: ads + SEO analytics/recommendations) — other blocks (sales/margin, reviews, price/RRP monitoring, stock/fulfillment, media content) are planned but not built yet, same roadmap as wb-agent (currently paused).
---

# ozon — Ozon cabinet agent entry point

Single cabinet, two credential pairs in `/root/ozon-agent/.env` (Seller API
+ Performance API — see CLAUDE.md, they are NOT the same key). Read
`/root/ozon-agent/CLAUDE.md` before doing anything — async stats report
flow, confirmed CSV quirks, and which endpoints are unverified.

## Routing

- Ads/SEO analytics, campaign recommendations, card text → delegate to the
  `ozon-search` subagent (`.claude/agents/ozon-search.md`).
- Bid-gap / competitor analysis for a specific campaign → `competitor_analysis.py
  <campaign_id> [--no-mpstats]` (uses `fetch_competitive_bids` — Ozon's own
  signal — plus real named competing brands in our own niche via MPSTATS,
  auto-detected from our brand "INKI", not a manually-typed category path).
  See CLAUDE.md for two important caveats: (1) a low bid relative to Ozon's
  "competitive" signal does NOT mean the campaign is underperforming —
  cross-check actual ДРР from `daily_report.py` before recommending a bid
  change; (2) MPSTATS' `avg_price` field is catalog-wide (includes unsold
  SKUs), the script uses `revenue/sales` instead for price comparisons.
- Other planned blocks (продажи/маржа, отзывы, РРЦ, остатки/поставки,
  медиа-контент) → not implemented yet, same as the WB side of this
  roadmap. Say so rather than improvising.

## Ground rules

- Every mutating call goes through `_check_confirm()` in `ozon_client.py`
  — never call a mutating method without the owner's literal "подтверждаю"
  for that specific action in the current message.
  `start_campaign`/`pause_campaign`/`update_product_seo` are all verified
  live (2026-08-09) — still require explicit per-call confirmation, just
  not "unverified" anymore.
- No browser automation — not confirmed safe under Ozon's terms (unlike
  WB, where it's confirmed forbidden; for Ozon it's just unconfirmed, see
  CLAUDE.md). Stick to the two APIs.
