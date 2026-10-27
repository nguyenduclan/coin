"""Thu thập tin từ RSS: lưu URL gốc, thời gian xuất bản/thu thập, loại trùng, gắn coin."""
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select

from ..core.config import settings
from ..core.db import SessionLocal
from ..models import NewsItem, Symbol

log = logging.getLogger("coin.news")

# Từ khóa -> tài sản cơ sở. Chỉ gắn thẻ khi khớp nguyên từ, tránh khớp nhầm.
KEYWORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin"], "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink", "link"],
}


def parse_feed(xml_text: str, source: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = (it.findtext("title") or "").strip()
        if not link or not title:
            continue
        pub = None
        raw = it.findtext("pubDate")
        if raw:
            try:
                pub = parsedate_to_datetime(raw)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub = None
        items.append({"url": link, "title": title, "published_at": pub, "source": source,
                      "summary": re.sub(r"<[^>]+>", "", it.findtext("description") or "")[:500]})
    return items


def tag_assets(text: str, assets: list[str]) -> list[str]:
    low = text.lower()
    found = []
    for a in assets:
        words = KEYWORDS.get(a, [a.lower()])
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            found.append(a)
    return found


def url_hash(url: str) -> str:
    return hashlib.sha256(url.split("?")[0].encode()).hexdigest()


async def fetch_news(client: httpx.AsyncClient | None = None) -> dict:
    """Trả về {"new": n, "errors": [...]}. Lỗi một nguồn không ảnh hưởng nguồn khác."""
    feeds = [f.strip() for f in settings.news_feeds.split(",") if f.strip()]
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                         headers={"User-Agent": "coin-assistant/0.1"})
    new, errors = 0, []
    try:
        async with SessionLocal() as s:
            # Gắn thẻ theo cả danh mục từ khóa lẫn watchlist, để tin thu thập trước khi thêm mã vẫn dùng được
            assets = sorted(set(await s.scalars(select(Symbol.base_asset))) | set(KEYWORDS))
            for feed in feeds:
                try:
                    r = await client.get(feed)
                    r.raise_for_status()
                    parsed = parse_feed(r.text, source=httpx.URL(feed).host)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{feed}: {e!r}")
                    log.warning("news feed %s failed: %r", feed, e)
                    continue
                for p in parsed:
                    h = url_hash(p["url"])
                    if await s.scalar(select(NewsItem.id).where(NewsItem.url_hash == h)):
                        continue
                    s.add(NewsItem(url_hash=h, url=p["url"], title=p["title"], summary=p["summary"],
                                   source=p["source"], published_at=p["published_at"],
                                   assets=",".join(tag_assets(p["title"] + " " + p["summary"], assets)),
                                   verified_level="UNVERIFIED_MEDIA",
                                   fetched_at=datetime.now(timezone.utc)))
                    new += 1
            await s.commit()
    finally:
        if own:
            await client.aclose()
    return {"new": new, "errors": errors}
