"""
Rep Search — Flask PWA
Search Yupoo + Weidian simultaneously.
Features: auto-translate, price parsing, GBP conversion, agent buy links, favourites.
"""

import re, os, time, json
from urllib.parse import urlparse, urljoin, quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response, send_from_directory

app = Flask(__name__, static_folder="static")

# ─── HTTP ─────────────────────────────────────────────────────────────────────

HEADERS_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
}

# ─── Search cache (30-min TTL, prevents hammering Yahoo on repeat searches) ──────

from collections import OrderedDict
import random

class _TTLCache:
    def __init__(self, max_size=200, ttl=1800):
        self.cache = OrderedDict()
        self.times = {}
        self.max_size = max_size
        self.ttl = ttl
    def get(self, key):
        if key in self.cache:
            if time.time() - self.times[key] < self.ttl:
                self.cache.move_to_end(key)
                return self.cache[key]
            del self.cache[key]; del self.times[key]
        return None
    def set(self, key, value):
        if key in self.cache: self.cache.move_to_end(key)
        self.cache[key] = value
        self.times[key] = time.time()
        if len(self.cache) > self.max_size:
            k = next(iter(self.cache)); del self.cache[k]; del self.times[k]

SEARCH_CACHE = _TTLCache()
_LAST_YAHOO_CALL = 0.0   # timestamp of last Yahoo request
_YAHOO_MIN_GAP   = 3.0   # minimum seconds between searches for different queries

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
def _yahoo_headers():
    return {"User-Agent": random.choice(_UA_POOL),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5", "Accept-Encoding": "gzip, deflate, br", "DNT": "1"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS_DESKTOP)

def get_soup(url, referer="", timeout=15):
    try:
        h = dict(HEADERS_DESKTOP)
        if referer: h["Referer"] = referer
        r = SESSION.get(url, headers=h, timeout=timeout)
        if r.status_code == 200:
            return BeautifulSoup(r.content, "lxml")
    except Exception:
        pass
    return None

# ─── Exchange rates ───────────────────────────────────────────────────────────

_RATES_CACHE = {"rates": {}, "ts": 0}

def get_exchange_rates():
    """Fetch CNY rates, cached for 1 hour."""
    if time.time() - _RATES_CACHE["ts"] < 3600 and _RATES_CACHE["rates"]:
        return _RATES_CACHE["rates"]
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=CNY&to=GBP,USD,EUR",
                         timeout=8)
        if r.status_code == 200:
            _RATES_CACHE["rates"] = r.json().get("rates", {})
            _RATES_CACHE["ts"] = time.time()
    except Exception:
        pass
    return _RATES_CACHE["rates"]

# ─── Price parsing ────────────────────────────────────────────────────────────

def parse_price_rmb(title):
    """Extract RMB price from Yupoo album title like 【250】[...]."""
    m = re.match(r'^[【\[]\s*(\d{2,4})\s*[】\]]', title.strip())
    if m:
        p = int(m.group(1))
        if 30 <= p <= 5000:
            return p
    m2 = re.search(r'[¥￥]\s*(\d{2,4})', title)
    if m2:
        p = int(m2.group(1))
        if 30 <= p <= 5000:
            return p
    return None

def price_to_currencies(rmb):
    """Convert RMB amount to GBP/USD dict."""
    if not rmb:
        return {}
    rates = get_exchange_rates()
    result = {"rmb": rmb}
    if "GBP" in rates:
        result["gbp"] = round(rmb * rates["GBP"], 2)
    if "USD" in rates:
        result["usd"] = round(rmb * rates["USD"], 2)
    return result

# ─── Agent buy links ──────────────────────────────────────────────────────────

def get_agent_urls(source_url, item_id=None, is_weidian=False):
    enc = quote(source_url, safe="")
    urls = {
        "Pandabuy": f"https://www.pandabuy.com/product?ra=1&url={enc}",
        "Sugargoo": f"https://www.sugargoo.com/#/home/productDetails?productLink={enc}",
        "Kakobuy":  f"https://www.kakobuy.com/item/details?url={enc}",
        "CSSBUY":   f"https://www.cssbuy.com/item-weidian-{item_id}.html"
                    if is_weidian and item_id else
                    f"https://www.cssbuy.com/item.html?url={enc}",
    }
    return urls

# ─── Translation ──────────────────────────────────────────────────────────────

_TRANS_CACHE = {}

def _needs_translation(text):
    if not text or len(text.strip()) < 2: return False
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f')
    return cjk / max(len(text), 1) > 0.10

def _translate_one(text):
    if text in _TRANS_CACHE: return _TRANS_CACHE[text]
    if not _needs_translation(text):
        _TRANS_CACHE[text] = text
        return text
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200:
            result = "".join(p[0] for p in r.json()[0] if p[0]).strip()
            _TRANS_CACHE[text] = result
            return result
    except Exception:
        pass
    _TRANS_CACHE[text] = text
    return text

def translate_batch(texts):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    to_do = [(i, t) for i, t in enumerate(texts) if _needs_translation(t) and t not in _TRANS_CACHE]
    result = list(texts)
    if to_do:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_translate_one, text): i for i, text in to_do}
            for fut in as_completed(futures):
                result[futures[fut]] = fut.result()
    for i, t in enumerate(texts):
        if t in _TRANS_CACHE:
            result[i] = _TRANS_CACHE[t]
    return result

def translate_yupoo_result(r):
    texts = [r.get("seller_name", ""), r.get("snippet", "")] + \
            [a.get("title", "") for a in r.get("albums", [])]
    translated = translate_batch(texts)
    r["seller_name"] = translated[0]
    r["snippet"] = translated[1]
    for i, a in enumerate(r.get("albums", [])):
        a["title"] = translated[2 + i]
    return r

def translate_weidian_result(item):
    texts = [item.get("title", ""), item.get("description", ""), item.get("snippet", "")] + \
            item.get("sizes", []) + item.get("colors", [])
    translated = translate_batch(texts)
    item["title"] = translated[0]
    item["description"] = translated[1]
    item["snippet"] = translated[2]
    ns = len(item.get("sizes", []))
    nc = len(item.get("colors", []))
    item["sizes"] = translated[3:3 + ns]
    item["colors"] = translated[3 + ns:3 + ns + nc]
    return item

# ─── Yahoo search ─────────────────────────────────────────────────────────────

def yahoo_search(query_variants, result_filter_fn, max_results=20):
    """Fresh session + UA rotation + retry on empty results + throttle."""
    global _LAST_YAHOO_CALL
    # Enforce minimum gap between Yahoo searches (avoids 500 rate-limits)
    gap = time.time() - _LAST_YAHOO_CALL
    if gap < _YAHOO_MIN_GAP:
        time.sleep(_YAHOO_MIN_GAP - gap)
    _LAST_YAHOO_CALL = time.time()
    results, seen = [], set()
    for variant in query_variants:
        if len(results) >= max_results:
            break
        for attempt in range(2):
            try:
                sess = requests.Session()
                r = sess.get("https://search.yahoo.com/search",
                             params={"n": "10", "p": variant, "nojs": "1"},
                             headers=_yahoo_headers(), timeout=15)
                if r.status_code == 500:
                    time.sleep(5 + random.uniform(0, 2)); continue  # hard rate-limit, longer wait
                if r.status_code not in (200,):
                    time.sleep(2); continue
                soup = BeautifulSoup(r.content, "lxml")
                found = 0
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if "r.search.yahoo.com" not in href: continue
                    m = re.search(r"/RU=([^/;]+)", href)
                    if not m: continue
                    url = requests.utils.unquote(m.group(1))
                    item = result_filter_fn(url, a, soup)
                    if item and item["_key"] not in seen:
                        seen.add(item["_key"]); results.append(item); found += 1
                if found > 0: break
                time.sleep(2 + random.uniform(0, 1))
            except Exception:
                time.sleep(2)
        time.sleep(1.2 + random.uniform(0, 0.5))
    return results[:max_results]

# ─── Yupoo helpers ────────────────────────────────────────────────────────────

def upgrade_to_big(url):
    return re.sub(r"/(small|medium|square|thumb)\.", "/big.", url)

def extract_best_thumb(tag):
    for attr in ("data-src", "data-origin-src", "src"):
        v = tag.get(attr, "")
        if v and "photo.yupoo.com" in v and "base64" not in v:
            return v
    return ""

def parse_albums_from_soup(soup, base):
    albums, seen = [], set()
    for a in soup.find_all("a", href=lambda h: h and re.search(r"/albums/\d+", str(h))):
        href = a.get("href", "")
        m = re.search(r"/albums/(\d+)", href)
        if not m: continue
        aid = m.group(1)
        if aid in seen: continue
        seen.add(aid)
        title = (a.get("title", "") or a.get_text()).strip().split(" | ")[0]
        full_url = urljoin(base, href)
        thumb = ""
        for img in (list(a.find_all("img")) + (list(a.parent.find_all("img")) if a.parent else [])):
            t = extract_best_thumb(img)
            if t: thumb = t; break
        uid_url = full_url if "uid=" in full_url else full_url + ("&uid=1" if "?" in full_url else "?uid=1")
        price_rmb = parse_price_rmb(title)
        albums.append({
            "id": aid, "title": title or f"Album {aid}", "url": uid_url, "thumb": thumb,
            "price": price_to_currencies(price_rmb),
        })
    return albums

def get_seller_name(soup, base):
    title_tag = soup.find("title")
    raw = title_tag.get_text() if title_tag else ""
    parts = [p.strip() for p in raw.split("|")]
    candidate = next((p for p in parts if p and p not in ("首页", "Home") and "Supplier" not in p and len(p) > 2), "")
    return candidate.split(" - ")[0].strip() or base.split("//")[1].split(".")[0]

def get_total_pages(soup):
    span = soup.find("span", class_=re.compile("pagination.span"))
    if span:
        m = re.search(r"\d+\s*/\s*(\d+)", span.get_text())
        if m: return int(m.group(1))
    m = re.search(r"共(\d+)页", soup.get_text())
    if m: return int(m.group(1))
    nums = [int(a.get_text().strip()) for a in soup.select("a.pagination__number") if a.get_text().strip().isdigit()]
    return max(nums) if nums else 1

def add_uid(url):
    return url if "uid=" in url else url + ("&uid=1" if "?" in url else "?uid=1")

def normalise_yupoo_url(raw):
    raw = raw.strip().rstrip("/")
    if raw.startswith("http"):
        parsed = urlparse(raw)
        if "x.yupoo.com" in (parsed.hostname or ""):
            return f"https://{parsed.hostname}"
    if re.match(r"^[\w\-]+$", raw):
        return f"https://{raw}.x.yupoo.com"
    if "yupoo.com" in raw:
        return f"https://{raw.lstrip('/')}"
    return ""

def yupoo_filter(url, a_tag, soup):
    if "x.yupoo.com" not in url: return None
    sm = re.match(r"https?://([^.]+)\.x\.yupoo\.com", url)
    if not sm: return None
    seller = sm.group(1)
    parent = a_tag.find_parent(class_=re.compile("algo|dd"))
    snippet = ""
    if parent:
        p = parent.find("p")
        snippet = p.get_text().strip()[:200] if p else ""
    pt = "album" if "/albums/" in url else ("category" if ("/categories/" in url or "/collections/" in url) else "home")
    return {"_key": seller, "seller": seller, "url": url, "snippet": snippet,
            "page_type": pt, "base_url": f"https://{seller}.x.yupoo.com", "platform": "yupoo"}

# ─── Weidian helpers ──────────────────────────────────────────────────────────

def fetch_weidian_item(item_id):
    s = requests.Session()
    item_url = f"https://weidian.com/item.html?itemID={item_id}"
    try:
        s.get(item_url, headers=HEADERS_MOBILE, timeout=10)
    except Exception:
        pass
    ref_h = {**HEADERS_MOBILE, "Referer": item_url}
    result = {"item_id": item_id, "url": item_url, "title": "", "price": "",
              "cover": "", "images": [], "description": "", "sizes": [], "colors": [],
              "agents": get_agent_urls(item_url, item_id=item_id, is_weidian=True)}
    try:
        r1 = s.get(f"https://thor.weidian.com/detail/getDetailDesc/1.0?param={json.dumps({'vItemId': item_id})}",
                   headers=ref_h, timeout=10)
        if r1.status_code == 200:
            content = r1.json().get("result", {}).get("item_detail", {}).get("desc_content", [])
            text_parts = [c["text"] for c in content if c.get("type") == 1 and c.get("text")]
            result["description"] = " ".join(text_parts)[:300]
            result["images"] = [c["url"] for c in content if c.get("type") == 2 and c.get("url")]
            if text_parts:
                result["title"] = text_parts[0].split("\n")[0].strip()[:100]
    except Exception:
        pass
    try:
        r2 = s.get(f"https://thor.weidian.com/detail/getItemSkuInfo/1.0?param={json.dumps({'itemId': item_id})}",
                   headers=ref_h, timeout=10)
        if r2.status_code == 200:
            for attr in r2.json().get("result", {}).get("attrList", []):
                vals = [v.get("attrValue", "") for v in attr.get("attrValues", [])]
                imgs = [v["img"] for v in attr.get("attrValues", []) if v.get("img")]
                title_a = attr.get("attrTitle", "")
                if any(c in title_a for c in ("码", "尺", "size", "Size")):
                    result["sizes"] = vals
                elif any(c in title_a for c in ("色", "颜", "color", "Color")):
                    result["colors"] = [v.get("attrValue", "") for v in attr.get("attrValues", [])]
                    if imgs and not result["cover"]:
                        result["cover"] = imgs[0]
    except Exception:
        pass
    if not result["cover"] and result["images"]:
        result["cover"] = result["images"][0]
    return result

def weidian_filter(url, a_tag, soup):
    if "weidian.com" not in url or "item" not in url: return None
    m = re.search(r"itemI[dD]=(\d+)", url)
    if not m: return None
    item_id = m.group(1)
    clean_url = f"https://weidian.com/item.html?itemID={item_id}"
    parent = a_tag.find_parent(class_=re.compile("algo|dd"))
    snippet = ""
    if parent:
        p = parent.find("p")
        snippet = p.get_text().strip()[:200] if p else ""
    return {"_key": item_id, "item_id": item_id, "url": clean_url, "snippet": snippet, "platform": "weidian"}

# ─── API routes ───────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    max_each = min(int(request.args.get("max", 15)), 20)
    should_translate = request.args.get("translate", "1") != "0"
    if not query: return jsonify({"error": "No query"}), 400

    # ── Cache check — same query within 30 min returns instantly ──
    cache_key = f"{query.lower().strip()}|{max_each}"
    cached = SEARCH_CACHE.get(cache_key)
    if cached:
        return jsonify(cached)

    # Yupoo
    yupoo_hits = yahoo_search(
        [f"site:x.yupoo.com {query}", f'site:x.yupoo.com "{query}"'],
        yupoo_filter, max_results=max_each)
    yupoo_results = []
    for hit in yupoo_hits:
        soup = get_soup(hit["url"], referer=hit["base_url"] + "/")
        albums = parse_albums_from_soup(soup, hit["base_url"]) if soup else []
        seller_name = get_seller_name(soup, hit["base_url"]) if soup else hit["seller"]
        yupoo_results.append({
            "seller": hit["seller"], "seller_name": seller_name,
            "base_url": hit["base_url"], "matched_url": hit["url"],
            "page_type": hit["page_type"], "snippet": hit["snippet"],
            "albums": albums[:12], "total_albums": len(albums), "platform": "yupoo",
        })

    # Weidian
    weidian_hits = yahoo_search(
        [f"site:weidian.com {query}", f"weidian.com {query} item"],
        weidian_filter, max_results=max_each)
    weidian_results = []
    for hit in weidian_hits:
        item = fetch_weidian_item(hit["item_id"])
        weidian_results.append({**item, "snippet": hit["snippet"]})

    # Translate
    if should_translate:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4) as pool:
            yf = [pool.submit(translate_yupoo_result, r) for r in yupoo_results]
            wf = [pool.submit(translate_weidian_result, i) for i in weidian_results]
            yupoo_results  = [f.result() for f in yf]
            weidian_results = [f.result() for f in wf]

    # If both empty, it may be a rate-limit - add a hint
    message = ""
    if not yupoo_results and not weidian_results:
        message = "No results found. If this keeps happening, wait a moment and try again."
    payload = {"query": query, "yupoo": yupoo_results, "weidian": weidian_results, "message": message}
    SEARCH_CACHE.set(cache_key, payload)
    return jsonify(payload)


@app.route("/api/seller")
def api_seller():
    raw = request.args.get("url", "").strip()
    base = normalise_yupoo_url(raw)
    if not base: return jsonify({"error": "Invalid URL"}), 400
    soup = get_soup(base)
    if not soup: return jsonify({"error": "Could not load seller"}), 404
    name = get_seller_name(soup, base)
    cats, seen_cats = [], set()
    for a in soup.find_all("a", href=lambda h: h and "/categories/" in str(h)):
        m = re.search(r"/categories/(\d+)", a.get("href", ""))
        if not m or m.group(1) in seen_cats: continue
        seen_cats.add(m.group(1))
        cats.append({"id": m.group(1), "name": a.get_text().strip(), "url": urljoin(base, a["href"])})
    albums = parse_albums_from_soup(soup, base)[:48]
    # Translate
    cat_names = translate_batch([c["name"] for c in cats])
    for c, n in zip(cats, cat_names): c["name"] = n
    alb_titles = translate_batch([a["title"] for a in albums])
    for a, t in zip(albums, alb_titles): a["title"] = t
    return jsonify({"name": _translate_one(name), "base_url": base, "categories": cats, "albums": albums})


@app.route("/api/browse")
def api_browse():
    url = request.args.get("url", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    if not url: return jsonify({"error": "No URL"}), 400
    page_url = re.sub(r"page=\d+", f"page={page}", url) if "page=" in url else url + (f"&page={page}" if "?" in url else f"?page={page}")
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    soup = get_soup(page_url, referer=base + "/")
    if not soup: return jsonify({"error": "Could not load"}), 404
    albums = parse_albums_from_soup(soup, base)
    titles = translate_batch([a["title"] for a in albums])
    for a, t in zip(albums, titles): a["title"] = t
    title_tag = soup.find("title")
    title = _translate_one((title_tag.get_text() if title_tag else "").split("|")[0].strip())
    return jsonify({"albums": albums, "page": page, "total_pages": get_total_pages(soup), "title": title})


@app.route("/api/album")
def api_album():
    url = add_uid(request.args.get("url", "").strip())
    if not url: return jsonify({"error": "No URL"}), 400
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    soup = get_soup(url, referer=base + "/")
    if not soup: return jsonify({"error": "Could not load"}), 404
    title_tag = soup.find("title")
    title = _translate_one((title_tag.get_text() if title_tag else "").split("|")[0].strip())
    images = []
    for tag in soup.find_all("img", attrs={"data-type": "photo"}):
        big = tag.get("data-src", "").strip()
        orig = tag.get("data-origin-src", "").strip()
        src = tag.get("src", "").strip()
        alt = tag.get("alt", "").strip()
        if not big and src and "photo.yupoo.com" in src and "base64" not in src:
            big = upgrade_to_big(src)
        if not big or "photo.yupoo.com" not in big: continue
        images.append({"url": big, "orig": orig or big, "thumb": re.sub(r"/(big|medium|small)\.", "/small.", big), "alt": alt})
    agents = get_agent_urls(url)
    return jsonify({"title": title, "url": url, "base_url": base, "images": images, "agents": agents})


@app.route("/api/weidian/item")
def api_weidian_item():
    item_id = request.args.get("id", "").strip()
    if not re.match(r"^\d+$", item_id): return jsonify({"error": "Invalid item ID"}), 400
    item = fetch_weidian_item(item_id)
    return jsonify(translate_weidian_result(item))


@app.route("/api/translate")
def api_translate():
    text = request.args.get("q", "").strip()
    result = _translate_one(text) if text else text
    return jsonify({"original": text, "translated": result, "changed": result != text})


@app.route("/api/rates")
def api_rates():
    return jsonify(get_exchange_rates())


@app.route("/img")
def img_proxy():
    url = request.args.get("url", "")
    ref = request.args.get("ref", "https://x.yupoo.com/")
    if not any(h in url for h in ("photo.yupoo.com", "si.geilicdn.com")):
        return "Bad URL", 400
    try:
        r = SESSION.get(url, headers={**HEADERS_DESKTOP, "Referer": ref}, timeout=20, stream=True)
        if r.status_code != 200: return f"Error {r.status_code}", r.status_code
        return Response(r.iter_content(8192), content_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return str(e), 500


@app.route("/static/<path:filename>")
def static_files(filename): return send_from_directory("static", filename)

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path): return HTML_APP

# ─── Frontend ─────────────────────────────────────────────────────────────────

HTML_APP = open(os.path.join(os.path.dirname(__file__), "static", "index.html")).read() \
    if os.path.exists(os.path.join(os.path.dirname(__file__), "static", "index.html")) \
    else "<h1>Loading...</h1>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
