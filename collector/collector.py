#!/usr/bin/env python3
"""
collector.py — Thu thập tin công nghệ EN/ZH/RU, dịch tóm tắt sang tiếng Việt,
đẩy về ingest.php trên InfinityFree.

Chạy:  python3 collector.py [--prices] [--limit N]
Env:   INGEST_URL, INGEST_TOKEN, LLM_API_KEY (DeepSeek, OpenAI-compatible),
       GITHUB_TOKEN (tăng giới hạn GitHub Search API), PRODUCTHUNT_TOKEN.
Không hardcode key/token thật — tất cả qua env / GitHub Secrets.

Quy tắc VIP (xem README):
  - Mặc định mọi bài is_vip=0.
  - Chỉ đánh VIP khi bài từ nguồn "pro" (OpenAI, DeepMind, Hugging Face Blog)
    VÀ tiêu đề chứa từ khóa AI/model, với vip_xu_cost mặc định 10 xu.
  - Admin vẫn có thể chuyển bất kỳ bài nào thành VIP trong trang quản trị.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests

TIMEOUT = 10          # giây / feed, retry 1 lần
MAX_PER_FEED = 20
INGEST_URL = os.environ.get("INGEST_URL", "")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
PRODUCTHUNT_TOKEN = os.environ.get("PRODUCTHUNT_TOKEN", "")

HEADERS = {"User-Agent": "TechNewsAggregator/1.0 (+https://example.invalid; contact: admin@example.invalid)"}

# 12 nguồn RSS đợt 1 — đúng schema.sql
RSS_SOURCES = [
    {"name": "Hacker News", "lang": "en", "feed": "https://news.ycombinator.com/rss", "category": "general"},
    {"name": "TechCrunch", "lang": "en", "feed": "https://techcrunch.com/feed/", "category": "general"},
    {"name": "The Verge", "lang": "en", "feed": "https://www.theverge.com/rss/index.xml", "category": "general"},
    {"name": "Ars Technica", "lang": "en", "feed": "https://feeds.arstechnica.com/arstechnica/index", "category": "general"},
    {"name": "Habr", "lang": "ru", "feed": "https://habr.com/ru/rss/all/all/?fl=ru", "category": "general"},
    {"name": "36Kr", "lang": "zh", "feed": "https://36kr.com/feed", "category": "general"},
    {"name": "Huxiu", "lang": "zh", "feed": "https://www.huxiu.com/rss/0.xml", "category": "general"},
    {"name": "V2EX", "lang": "zh", "feed": "https://v2ex.com/index.xml", "category": "tools"},
    {"name": "SSPAI", "lang": "zh", "feed": "https://sspai.com/feed", "category": "tools"},
    {"name": "OpenAI News", "lang": "en", "feed": "https://openai.com/news/rss.xml", "category": "ai"},
    {"name": "DeepMind Blog", "lang": "en", "feed": "https://deepmind.google/blog/rss.xml", "category": "ai"},
    {"name": "Hugging Face Blog", "lang": "en", "feed": "https://huggingface.co/blog/feed.xml", "category": "ai"},
]

# Nguồn "pro" được phép đánh VIP theo quy tắc ở trên
VIP_SOURCES = {"OpenAI News", "DeepMind Blog", "Hugging Face Blog"}
VIP_KEYWORDS = re.compile(
    r"\b(gpt|gemini|claude|llama|mistral|model|agent|reasoning|multimodal|"
    r"open weights|fine-?tune|benchmark|sota|frontier)\b", re.I)

AI_KEYWORDS = re.compile(r"\b(ai|artificial intelligence|llm|gpt|gemini|claude|model|agent|machine learning|deep learning|neural|chatbot)\b", re.I)
TOOLS_KEYWORDS = re.compile(r"\b(tool|app|software|ide|cli|framework|library|sdk|api|open source|github|launch|release|update|version)\b", re.I)
HARDWARE_KEYWORDS = re.compile(r"\b(gpu|cpu|chip|semiconductor|nvidia|amd|intel|ram|ssd|memory|dram|nand|hardware|benchmark|price)\b", re.I)

GOOGLE_NEWS_QUERIES = ["Anthropic", "Meta AI", "xAI"]  # hãng không có RSS

PRICE_TICKERS = [  # đúng config.php PRICE_TICKERS
    ("NVDA", "NVIDIA", "GPU/AI"), ("AMD", "AMD", "CPU/GPU"),
    ("INTC", "Intel", "CPU"), ("TSM", "TSMC", "Chip"),
    ("MU", "Micron", "RAM/DRAM"), ("AVGO", "Broadcom", "Chip/AI"),
    ("QCOM", "Qualcomm", "Chip"), ("ASML", "ASML", "Thiết bị chip"),
    ("SMCI", "Super Micro", "Server/AI"), ("ARM", "Arm", "Chip"),
    ("005930.KS", "Samsung", "RAM/Điện thoại"), ("000660.KS", "SK Hynix", "RAM/HBM"),
]


def clean_url(url: str) -> str:
    """Bỏ fragment / utm để dedup ổn định."""
    u = urlparse(url.strip())
    q = "&".join(p for p in u.query.split("&") if not p.lower().startswith(("utm_", "fbclid=")))
    return f"{u.scheme}://{u.netloc}{u.path}" + (f"?{q}" if q else "")


def item_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title.strip()}|{clean_url(url)}".encode("utf-8")).hexdigest()


def classify(title: str, summary: str, default: str) -> str:
    text = f"{title} {summary}"
    if AI_KEYWORDS.search(text):
        return "ai"
    if HARDWARE_KEYWORDS.search(text):
        return "hardware"
    if TOOLS_KEYWORDS.search(text):
        return "tools"
    return default


def fetch_with_retry(url: str, **kw):
    last = None
    for _ in range(2):  # 1 lần thử + 1 retry
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HEADERS, **kw)
            r.raise_for_status()
            return r
        except Exception as ex:  # noqa: BLE001 — ghi log và retry
            last = ex
            time.sleep(2)
    raise last


def fetch_rss(source: dict, limit: int):
    items = []
    try:
        parsed = feedparser.parse(source["feed"], request_headers=HEADERS)
    except Exception as ex:  # noqa: BLE001
        print(f"[WARN] {source['name']}: feedparser lỗi: {ex}", flush=True)
        return items, "error"
    if getattr(parsed, "bozo", False) and not parsed.entries:
        print(f"[WARN] {source['name']}: feed lỗi/bozo", flush=True)
        return items, "error"
    for e in parsed.entries[:limit]:
        url = clean_url(getattr(e, "link", "") or "")
        title = (getattr(e, "title", "") or "").strip()
        if not title or not url:
            continue
        summary = re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or "")
        summary = re.sub(r"\s+", " ", summary).strip()[:500]
        pub = None
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                pub = datetime(*getattr(e, key)[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                break
        cat = classify(title, summary, source["category"])
        is_vip = 1 if (source["name"] in VIP_SOURCES and VIP_KEYWORDS.search(title)) else 0
        items.append({
            "source": source["name"], "lang": source["lang"], "title": title,
            "title_vi": title, "summary_vi": summary, "url": url,
            "published_at": pub, "score": 0, "category": cat,
            "is_vip": is_vip, "vip_xu_cost": 10 if is_vip else 0,
            "hash": item_hash(title, url),
        })
    return items, "ok"


def fetch_github(limit: int):
    """GitHub Search API: repo tạo 7 ngày qua, sort theo stars."""
    items = []
    try:
        h = dict(HEADERS)
        if GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = fetch_with_retry(
            "https://api.github.com/search/repositories",
            params={"q": "created:>2026-11-07 stars:>50", "sort": "stars",
                    "order": "desc", "per_page": str(limit)}, headers=h)
        for repo in r.json().get("items", []):
            title = f"{repo['full_name']}: {repo.get('description') or ''}".strip()
            url = clean_url(repo["html_url"])
            items.append({
                "source": "GitHub", "lang": "en", "title": title[:500],
                "title_vi": title[:500], "summary_vi": (repo.get("description") or "")[:500],
                "url": url, "published_at": repo.get("created_at", "")[:19].replace("T", " "),
                "score": int(repo.get("stargazers_count", 0)), "category": "tools",
                "is_vip": 0, "vip_xu_cost": 0, "hash": item_hash(title, url),
            })
    except Exception as ex:  # noqa: BLE001
        print(f"[WARN] GitHub API lỗi: {ex}", flush=True)
    return items


def fetch_producthunt(limit: int):
    """Product Hunt GraphQL v2 — cần PRODUCTHUNT_TOKEN (developer token miễn phí)."""
    items = []
    if not PRODUCTHUNT_TOKEN:
        print("[INFO] Bỏ qua Product Hunt (chưa có PRODUCTHUNT_TOKEN).", flush=True)
        return items
    query = """query { posts(first: %d) { edges { node {
        name tagline url createdAt votesCount } } } }""" % limit
    try:
        r = requests.post("https://api.producthunt.com/v2/api/graphql",
                          json={"query": query}, timeout=TIMEOUT,
                          headers={**HEADERS, "Authorization": f"Bearer {PRODUCTHUNT_TOKEN}"})
        r.raise_for_status()
        for edge in r.json().get("data", {}).get("posts", {}).get("edges", []):
            n = edge["node"]
            title = f"{n['name']}: {n.get('tagline') or ''}".strip()
            url = clean_url(n["url"])
            items.append({
                "source": "Product Hunt", "lang": "en", "title": title[:500],
                "title_vi": title[:500], "summary_vi": (n.get("tagline") or "")[:500],
                "url": url, "published_at": n.get("createdAt", "")[:19].replace("T", " "),
                "score": int(n.get("votesCount", 0)), "category": "tools",
                "is_vip": 0, "vip_xu_cost": 0, "hash": item_hash(title, url),
            })
    except Exception as ex:  # noqa: BLE001
        print(f"[WARN] Product Hunt lỗi: {ex}", flush=True)
    return items


def fetch_google_news(limit: int):
    """Google News RSS — dự phòng cho Anthropic / Meta AI / xAI (không có RSS)."""
    items = []
    for q in GOOGLE_NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(q)
               + "&hl=en-US&gl=US&ceid=US:en")
        got, _ = fetch_rss({"name": f"Google News ({q})", "lang": "en",
                            "feed": url, "category": "ai"}, limit)
        items.extend(got)
    return items


def translate_batch(items: list) -> None:
    """Dịch tiêu đề + tóm tắt sang tiếng Việt bằng DeepSeek (OpenAI-compatible).
    Không có LLM_API_KEY -> giữ nguyên tiêu đề gốc, summary_vi = summary gốc.
    Dịch theo batch 10 bài/lần, lỗi thì fallback từng bài."""
    if not items:
        return
    if not LLM_API_KEY:
        print("[INFO] Chưa có LLM_API_KEY — giữ tiêu đề gốc, chưa dịch tiếng Việt.", flush=True)
        return
    for i in range(0, len(items), 10):
        batch = items[i:i + 10]
        prompt = ("Dịch sang tiếng Việt, trả về JSON array, mỗi phần tử "
                  '{"title_vi": "...", "summary_vi": "..."} (tóm tắt 2-3 câu, giữ tên riêng, link, số liệu):\n'
                  + json.dumps([{"t": b["title"], "s": b["summary_vi"]} for b in batch],
                               ensure_ascii=False))
        try:
            r = requests.post("https://api.deepseek.com/chat/completions",
                              json={"model": "deepseek-chat",
                                    "messages": [{"role": "user", "content": prompt}],
                                    "temperature": 0.3},
                              timeout=30,
                              headers={"Authorization": f"Bearer {LLM_API_KEY}",
                                       "Content-Type": "application/json"})
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            content = content[content.find("["):content.rfind("]") + 1]
            out = json.loads(content)
            for b, t in zip(batch, out):
                if t.get("title_vi"):
                    b["title_vi"] = str(t["title_vi"])[:500]
                if t.get("summary_vi"):
                    b["summary_vi"] = str(t["summary_vi"])[:1000]
        except Exception as ex:  # noqa: BLE001
            print(f"[WARN] Dịch batch lỗi, giữ nguyên gốc: {ex}", flush=True)


def fetch_prices():
    """Snapshot giá cổ phiếu hằng ngày qua Stooq CSV (miễn phí, không key).
    Chỉ chạy khi có cờ --prices. Lỗi 1 mã không làm hỏng các mã khác."""
    out, today = [], datetime.now(timezone.utc).date().isoformat()
    symbols = ",".join(s.lower() for s, _, _ in PRICE_TICKERS)
    try:
        r = fetch_with_retry(f"https://stooq.com/q/l/?s={symbols}&f=sd2t2ohlcv&h&e=csv")
        rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:] if ln.strip()]
        by_sym = {row[0].strip().lower(): row for row in rows if len(row) >= 7}
        for sku, name, cat in PRICE_TICKERS:
            row = by_sym.get(sku.lower())
            if not row:
                continue
            try:
                price = float(row[6])
            except (ValueError, IndexError):
                continue
            if price > 0:
                out.append({"sku": sku, "name": name, "category": cat,
                            "source": "stooq", "price": price,
                            "currency": "USD", "price_date": today})
    except Exception as ex:  # noqa: BLE001
        print(f"[WARN] Lấy giá lỗi: {ex}", flush=True)
    return out


def post_ingest(payload: dict) -> dict:
    if not INGEST_URL or not INGEST_TOKEN:
        print("[INFO] Chưa có INGEST_URL/INGEST_TOKEN — in payload ra stdout thay vì POST.", flush=True)
        print(json.dumps({"items": len(payload.get("items", [])),
                          "prices": len(payload.get("prices", []))}, ensure_ascii=False))
        return {"ok": False, "reason": "no_ingest_config"}
    r = requests.post(INGEST_URL, json=payload, timeout=30,
                      headers={**HEADERS, "Authorization": f"Bearer {INGEST_TOKEN}",
                               "Content-Type": "application/json"})
    print(f"[INFO] Ingest HTTP {r.status_code}, body {len(r.content)} bytes.", flush=True)
    if r.status_code >= 400:
        print(f"[ERROR] Ingest HTTP {r.status_code} "
              f"Body[:500]={r.text[:500]!r}", flush=True)
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        print(f"[ERROR] Ingest trả về không phải JSON. Status={r.status_code} "
              f"Content-Type={r.headers.get('Content-Type')} "
              f"Body[:500]={r.text[:500]!r}", flush=True)
        raise


def main() -> int:
    limit = 20
    do_prices = "--prices" in sys.argv
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (ValueError, IndexError):
            pass
    limit = max(1, min(limit, MAX_PER_FEED))

    all_items, seen, status = [], set(), {}
    for src in RSS_SOURCES:
        got, st = fetch_rss(src, limit)
        status[src["name"]] = {"count": len(got), "status": st}
        for it in got:
            if it["hash"] not in seen:
                seen.add(it["hash"])
                all_items.append(it)
    for extra in (fetch_github(10), fetch_producthunt(10), fetch_google_news(5)):
        for it in extra:
            if it["hash"] not in seen:
                seen.add(it["hash"])
                all_items.append(it)

    translate_batch(all_items)
    prices = fetch_prices() if do_prices else []
    print(f"[INFO] Thu được {len(all_items)} tin, {len(prices)} mã giá. "
          f"VIP: {sum(1 for i in all_items if i['is_vip'])} bài.", flush=True)
    for name, s in status.items():
        print(f"  - {name}: {s['count']} tin ({s['status']})", flush=True)
    try:
        res = post_ingest({"items": all_items, "prices": prices})
        print(f"[INFO] Ingest trả về: {res}", flush=True)
    except Exception as ex:  # noqa: BLE001
        print(f"[ERROR] POST ingest thất bại: {ex}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
