#!/usr/bin/env python3
"""Media-content audit for every product card in the cabinet: read-only,
flags what's missing (photos, video, rich-content) so the owner can decide
what to shoot/build. No mutation -- see roadmap block 5 (медиа-контент) in
/root/ozon-agent/.claude/skills/ozon/SKILL.md.

Usage:
    python3 content_audit.py
    python3 content_audit.py demo
"""
import sys

from ozon_client import load_env, OzonClient

MIN_PHOTOS = 4  # Ozon's own guidance is 4-5+ photos per card for good conversion


def audit_rows(items: list) -> list:
    for item in items:
        item["low_photos"] = item["photos_count"] < MIN_PHOTOS
    return items


def print_report():
    env = load_env()
    client = OzonClient(env["OZON_CLIENT_ID"], env["OZON_API_KEY"],
                         env["OZON_PERF_CLIENT_ID"], env["OZON_PERF_CLIENT_SECRET"])
    items = audit_rows(client.fetch_all_products_content())

    print(f"=== Аудит медиа-контента карточек: {len(items)} товаров ===\n")

    no_video = [i for i in items if not i["has_video"]]
    no_rich = [i for i in items if not i["has_rich_content"]]
    low_photos = [i for i in items if i["low_photos"]]
    no_primary = [i for i in items if not i["has_primary_image"]]

    print(f"Без видео: {len(no_video)}/{len(items)}")
    print(f"Без rich-контента (JSON-конструктор): {len(no_rich)}/{len(items)}")
    print(f"Меньше {MIN_PHOTOS} фото: {len(low_photos)}/{len(items)}")
    if no_primary:
        print(f"Без главного фото (!): {len(no_primary)}/{len(items)}")

    if low_photos:
        print(f"\n--- Мало фото (<{MIN_PHOTOS}) ---")
        for i in sorted(low_photos, key=lambda x: x["photos_count"]):
            print(f'  {i["offer_id"]:<12} фото={i["photos_count"]}  {i["name"][:60]}')

    if no_primary:
        print("\n--- Нет главного фото ---")
        for i in no_primary:
            print(f'  {i["offer_id"]:<12} {i["name"][:60]}')

    if no_video:
        print(f"\n--- Без видео ({len(no_video)} шт., первые 20) ---")
        for i in no_video[:20]:
            print(f'  {i["offer_id"]:<12} {i["name"][:60]}')

    if no_rich:
        print(f"\n--- Без rich-контента ({len(no_rich)} шт., первые 20) ---")
        for i in no_rich[:20]:
            print(f'  {i["offer_id"]:<12} {i["name"][:60]}')


def demo():
    """Self-check: threshold flag and empty-attribute detection, no network."""
    fake_items = [
        {"product_id": 1, "offer_id": "A", "sku": 1, "name": "мало фото", "photos_count": 2,
         "has_primary_image": True, "has_video": False, "has_rich_content": False},
        {"product_id": 2, "offer_id": "B", "sku": 2, "name": "всё есть", "photos_count": 7,
         "has_primary_image": True, "has_video": True, "has_rich_content": True},
    ]
    rows = audit_rows(fake_items)
    assert rows[0]["low_photos"] is True, "2 photos should be below MIN_PHOTOS threshold"
    assert rows[1]["low_photos"] is False, "7 photos should be above MIN_PHOTOS threshold"
    print("demo: content audit threshold self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        print_report()
