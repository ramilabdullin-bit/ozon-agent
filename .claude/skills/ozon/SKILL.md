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
