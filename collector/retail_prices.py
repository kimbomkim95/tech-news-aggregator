#!/usr/bin/env python3
"""Quét giá bán lẻ linh kiện 1 lần/ngày từ GearVN (VND) + Canada Computers (CAD).

Chạy bằng GitHub Actions (retail-daily.yml, 02:00 giờ VN), POST về
retail_ingest.php. Mỗi (linh kiện, shop) lưu khoảng giá thấp nhất - cao
nhất trong ngày, hiện ở trang prices.php.

Lưu ý đã kiểm chứng 14/11/2026:
- GearVN: trang tìm kiếm trả HTML đầy đủ, tên trong `product-card-name`,
  giá bán trong `product-card-price-sale`, ghép theo thứ tự.
- Canada Computers: tên trong slug của link sản phẩm, giá trong
  thuộc tính `data-price="$X"` của khối product-description.
- KHÔNG quét được bằng requests thường: HACOM (Next.js, dữ liệu tải bằng
  JS, không có trong HTML), Alternate.de (listing tải bằng JS),
  Phong Vũ / Newegg / PCPartPicker / B&H / Geizhals / Micro Center
  (chặn Cloudflare/bot). Nếu sau này các shop này mở lại thì thêm vào STORES.
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # noqa: BLE001
    VN_TZ = timezone.utc

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import HEADERS, post_ingest  # noqa: E402

RETAIL_INGEST_URL = os.environ.get("RETAIL_INGEST_URL", "")
TIMEOUT = 20

# (nhãn hiển thị, từ khóa tìm kiếm, regex bắt buộc, regex loại trừ, nhóm)
COMPONENTS = [
    ("RTX 5060", "RTX 5060", [r"RTX\s*5060"], [r"5060\s*TI\b"], "VGA"),
    ("RTX 5060 Ti", "RTX 5060 Ti", [r"RTX\s*5060\s*TI\b"], [], "VGA"),
    ("RTX 5070", "RTX 5070", [r"RTX\s*5070"], [r"5070\s*TI\b"], "VGA"),
    ("RTX 5070 Ti", "RTX 5070 Ti", [r"RTX\s*5070\s*TI\b"], [], "VGA"),
    ("RTX 5080", "RTX 5080", [r"RTX\s*5080"], [], "VGA"),
    ("RX 9070 XT", "RX 9070 XT", [r"RX\s*9070\s*XT\b"], [], "VGA"),
    ("Ryzen 7 9800X3D", "9800X3D", [r"9800X3D"], [], "CPU"),
    ("Ryzen 7 7800X3D", "7800X3D", [r"7800X3D"], [], "CPU"),
    ("Core i9-14900K", "14900K", [r"14900K"], [r"14900KF\b"], "CPU"),
    ("RAM DDR5 32GB", "DDR5 32GB", [r"DDR5", r"32\s*GB"], [r"SODIMM", r"LAPTOP"], "RAM"),
    ("SSD 1TB NVMe", "SSD 1TB NVMe", [r"\b1\s*TB\b", r"(SSD|NVME|M\.2)"], [r"\bHDD\b"], "SSD"),
]

# Máy tính nguyên bộ / laptop hay lẫn vào kết quả tìm VGA -> loại ra.
PREBUILT_RE = re.compile(r"PC GVN|PC GAMING|MÁY TÍNH|BỘ PC|GAMING PC|DESKTOP PC|LAPTOP|NOTEBOOK", re.I)


def match_component(name: str, comp) -> bool:
    label, _q, includes, excludes, _cat = comp
    upper = " " + re.sub(r"\s+", " ", name.upper()) + " "
    if PREBUILT_RE.search(name):
        return False
    for pat in includes:
        if not re.search(pat, upper, re.I):
            return False
    for pat in excludes:
        if re.search(pat, upper, re.I):
            return False
    return True


def fetch(url: str) -> str:
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and len(r.text) > 1000:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as ex:  # noqa: BLE001
            last = str(ex)
    print(f"[WARN] Không tải được {url}: {last}", flush=True)
    return ""


def parse_gearvn(html_text: str):
    """Trả về [(tên, giá VND)] — tên và giá bán ghép theo thứ tự xuất hiện."""
    names = re.findall(r"product-card-name[^>]*>([^<]+)</p>", html_text)
    prices = re.findall(r"product-card-price-sale[^>]*>([^<]+)<", html_text)
    out = []
    for name, price_raw in zip(names, prices):
        m = re.search(r"([\d\.]+)\s*đ", price_raw)
        if not m:
            continue
        try:
            price = float(m.group(1).replace(".", ""))
        except ValueError:
            continue
        if price > 0:
            out.append((html.unescape(name).strip(), price))
    return out


def parse_canadacomputers(html_text: str):
    """Trả về [(tên, giá CAD)] — tên từ slug link sản phẩm, giá từ data-price."""
    slugs = re.findall(
        r"href=\"https://www\.canadacomputers\.com/en/[^\"]+?/([^\"]+?)\.html[^\"]*\"\s+class=\"thumbnail product-thumbnail",
        html_text,
    )
    prices = re.findall(r"data-price=\"\$([0-9,]+\.[0-9]{2})\"", html_text)
    out = []
    for slug, price_raw in zip(slugs, prices):
        name = slug.replace("-", " ").strip()
        try:
            price = float(price_raw.replace(",", ""))
        except ValueError:
            continue
        if price > 0:
            out.append((name, price))
    return out


STORES = [
    {
        "name": "GearVN",
        "currency": "VND",
        "search_url": "https://gearvn.com/search?q={q}",
        "parse": parse_gearvn,
    },
    {
        "name": "Canada Computers",
        "currency": "CAD",
        "search_url": "https://www.canadacomputers.com/en/search?s={q}",
        "parse": parse_canadacomputers,
    },
]


def main() -> int:
    today = datetime.now(VN_TZ).date().isoformat()
    rows = []
    for label, query, _inc, _exc, _cat in COMPONENTS:
        comp = (label, query, _inc, _exc, _cat)
        for store in STORES:
            url = store["search_url"].format(q=requests.utils.quote(query))
            html_text = fetch(url)
            if not html_text:
                continue
            matched = [p for name, p in store["parse"](html_text)
                       if match_component(name, comp)]
            if not matched:
                print(f"[INFO] {label} @ {store['name']}: không khớp sản phẩm nào.", flush=True)
                continue
            lo, hi = min(matched), max(matched)
            rows.append({
                "component": label, "store": store["name"],
                "price_min": lo, "price_max": hi,
                "currency": store["currency"], "price_date": today, "url": url,
            })
            print(f"[INFO] {label} @ {store['name']}: {len(matched)} mẫu, "
                  f"{lo:,.0f} - {hi:,.0f} {store['currency']}.", flush=True)
    if not rows:
        print("[ERROR] Không thu được giá nào từ cả 2 shop.", flush=True)
        return 1
    res = post_ingest({"retail_prices": rows}, url=RETAIL_INGEST_URL or None)
    print(f"[INFO] Retail ingest trả về: {res}", flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
