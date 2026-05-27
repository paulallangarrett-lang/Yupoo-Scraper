"""
Yupoo + Weidian Browser — Flask PWA
Search both platforms simultaneously.
"""

import re, os, time, json
from urllib.parse import urlparse, urljoin

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

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

# ─── Yahoo search helper ──────────────────────────────────────────────────────

def yahoo_search(query_variants, result_filter_fn, max_results=20):
    """Run Yahoo searches and collect results using a filter function."""
    results = []
    seen = set()
    for variant in query_variants:
        if len(results) >= max_results:
            break
        for page_start in [1, 11]:
            if len(results) >= max_results:
                break
            try:
                r = SESSION.get(
                    "https://search.yahoo.com/search",
                    params={"n": "10", "p": variant, "nojs": "1", "b": str(page_start)},
                    headers=HEADERS_DESKTOP, timeout=15
                )
                if r.status_code != 200:
                    break
                soup = BeautifulSoup(r.content, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if "r.search.yahoo.com" not in href:
                        continue
                    m = re.search(r"/RU=([^/;]+)", href)
                    if not m:
                        continue
                    url = requests.utils.unquote(m.group(1))
                    item = result_filter_fn(url, a, soup)
                    if item and item["_key"] not in seen:
                        seen.add(item["_key"])
                        results.append(item)
                time.sleep(0.8)
            except Exception:
                break
    return results[:max_results]


# ─── Translation ──────────────────────────────────────────────────────────────

_TRANS_CACHE = {}   # simple in-process cache

def _needs_translation(text):
    """Returns True if text has >10% CJK characters."""
    if not text or len(text.strip()) < 2:
        return False
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f')
    return cjk / max(len(text), 1) > 0.10

def _translate_one(text):
    """Translate a single string via Google Translate (unofficial, free)."""
    if text in _TRANS_CACHE:
        return _TRANS_CACHE[text]
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
            data = r.json()
            result = "".join(part[0] for part in data[0] if part[0]).strip()
            _TRANS_CACHE[text] = result
            return result
    except Exception:
        pass
    _TRANS_CACHE[text] = text
    return text

def translate_batch(texts):
    """Translate a list of strings concurrently. Returns same-length list."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    to_do = [(i, t) for i, t in enumerate(texts) if _needs_translation(t) and t not in _TRANS_CACHE]
    result = list(texts)

    if not to_do:
        return [_TRANS_CACHE.get(t, t) for t in texts]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_translate_one, text): i for i, text in to_do}
        for fut in as_completed(futures):
            idx = futures[fut]
            result[idx] = fut.result()

    # Fill cached ones
    for i, t in enumerate(texts):
        if t in _TRANS_CACHE:
            result[i] = _TRANS_CACHE[t]

    return result

def translate_yupoo_result(r):
    """Translate all text fields in a Yupoo seller result dict in-place."""
    texts = []
    # seller name, snippet
    texts.append(r.get("seller_name", ""))
    texts.append(r.get("snippet", ""))
    # album titles
    for a in r.get("albums", []):
        texts.append(a.get("title", ""))
    translated = translate_batch(texts)
    r["seller_name"] = translated[0]
    r["snippet"] = translated[1]
    for i, a in enumerate(r.get("albums", [])):
        a["title"] = translated[2 + i]
    return r

def translate_weidian_result(item):
    """Translate all text fields in a Weidian item result dict in-place."""
    texts = [
        item.get("title", ""),
        item.get("description", ""),
        item.get("snippet", ""),
    ] + item.get("sizes", []) + item.get("colors", [])

    translated = translate_batch(texts)

    item["title"] = translated[0]
    item["description"] = translated[1]
    item["snippet"] = translated[2]
    n_sizes = len(item.get("sizes", []))
    n_colors = len(item.get("colors", []))
    item["sizes"] = translated[3: 3 + n_sizes]
    item["colors"] = translated[3 + n_sizes: 3 + n_sizes + n_colors]
    return item


# ─── YUPOO helpers ────────────────────────────────────────────────────────────

def upgrade_to_big(url):
    return re.sub(r"/(small|medium|square|thumb)\.", "/big.", url)

def extract_best_thumb(tag):
    for attr in ("data-src", "data-origin-src", "src"):
        v = tag.get(attr, "")
        if v and "photo.yupoo.com" in v and "base64" not in v:
            return v
    return ""

def parse_albums_from_soup(soup, base):
    albums = []
    seen = set()
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
        albums.append({"id": aid, "title": title or f"Album {aid}", "url": uid_url, "thumb": thumb})
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

# ─── WEIDIAN helpers ──────────────────────────────────────────────────────────

def fetch_weidian_item(item_id):
    """Fetch Weidian item data via their API."""
    s = requests.Session()
    item_url = f"https://weidian.com/item.html?itemID={item_id}"
    try:
        s.get(item_url, headers=HEADERS_MOBILE, timeout=10)
    except Exception:
        pass
    ref_h = {**HEADERS_MOBILE, "Referer": item_url}

    result = {"item_id": item_id, "url": item_url, "title": "", "price": "",
              "cover": "", "images": [], "description": "", "sizes": [], "colors": []}

    # Description + images
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

    # SKU (variants, cover images)
    try:
        r2 = s.get(f"https://thor.weidian.com/detail/getItemSkuInfo/1.0?param={json.dumps({'itemId': item_id})}",
                   headers=ref_h, timeout=10)
        if r2.status_code == 200:
            for attr in r2.json().get("result", {}).get("attrList", []):
                vals = [v.get("attrValue", "") for v in attr.get("attrValues", [])]
                imgs = [v["img"] for v in attr.get("attrValues", []) if v.get("img")]
                attr_title = attr.get("attrTitle", "")
                if any(c in attr_title for c in ("码", "尺", "size", "Size")):
                    result["sizes"] = vals
                elif any(c in attr_title for c in ("色", "颜", "color", "Color")):
                    result["colors"] = [v.get("attrValue","") for v in attr.get("attrValues",[])]
                    if imgs and not result["cover"]:
                        result["cover"] = imgs[0]
    except Exception:
        pass

    if not result["cover"] and result["images"]:
        result["cover"] = result["images"][0]

    return result

def weidian_filter(url, a_tag, soup):
    if "weidian.com" not in url: return None
    if "item" not in url: return None
    m = re.search(r"itemI[dD]=(\d+)", url)
    if not m: return None
    item_id = m.group(1)
    # Normalise URL
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
    """Search both Yupoo and Weidian simultaneously."""
    query = request.args.get("q", "").strip()
    max_each = min(int(request.args.get("max", 15)), 20)
    if not query:
        return jsonify({"error": "No query"}), 400

    # ── Yupoo ──
    yupoo_hits = yahoo_search(
        [f"site:x.yupoo.com {query}", f'site:x.yupoo.com "{query}"'],
        yupoo_filter, max_results=max_each
    )
    yupoo_results = []
    for hit in yupoo_hits:
        soup = get_soup(hit["url"], referer=hit["base_url"] + "/")
        if not soup:
            yupoo_results.append({**hit, "seller_name": hit["seller"], "albums": [], "total_albums": 0})
            continue
        albums = parse_albums_from_soup(soup, hit["base_url"])
        yupoo_results.append({
            "seller": hit["seller"],
            "seller_name": get_seller_name(soup, hit["base_url"]),
            "base_url": hit["base_url"],
            "matched_url": hit["url"],
            "page_type": hit["page_type"],
            "snippet": hit["snippet"],
            "albums": albums[:12],
            "total_albums": len(albums),
            "platform": "yupoo",
        })

    # ── Weidian ──
    weidian_hits = yahoo_search(
        [f"site:weidian.com {query}", f"weidian.com {query} item"],
        weidian_filter, max_results=max_each
    )
    weidian_results = []
    for hit in weidian_hits:
        item = fetch_weidian_item(hit["item_id"])
        weidian_results.append({
            "item_id": hit["item_id"],
            "url": hit["url"],
            "snippet": hit["snippet"],
            "title": item["title"],
            "cover": item["cover"],
            "images": item["images"][:20],
            "description": item["description"],
            "sizes": item["sizes"],
            "colors": item["colors"],
            "platform": "weidian",
        })

    # ── Translate non-English text (skip if client disabled it) ──
    should_translate = request.args.get('translate', '1') != '0'
    if not should_translate:
        return jsonify({"query": query, "yupoo": yupoo_results, "weidian": weidian_results})
    # ── Translate non-English text ──
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    def _translate_seller(r): return translate_yupoo_result(r)
    def _translate_item(item): return translate_weidian_result(item)

    with ThreadPoolExecutor(max_workers=4) as pool:
        yupoo_futures  = [pool.submit(_translate_seller, r) for r in yupoo_results]
        weidian_futures = [pool.submit(_translate_item, i) for i in weidian_results]
        yupoo_results  = [f.result() for f in yupoo_futures]
        weidian_results = [f.result() for f in weidian_futures]

    return jsonify({
        "query": query,
        "yupoo": yupoo_results,
        "weidian": weidian_results,
    })

# ── Yupoo: seller home ──
@app.route("/api/seller")
def api_seller():
    raw = request.args.get("url", "").strip()
    base = normalise_yupoo_url(raw)
    if not base: return jsonify({"error": "Invalid URL"}), 400
    soup = get_soup(base)
    if not soup: return jsonify({"error": "Could not load seller"}), 404
    name = get_seller_name(soup, base)
    cats = []
    seen_cats = set()
    for a in soup.find_all("a", href=lambda h: h and "/categories/" in str(h)):
        href = a.get("href", "")
        m = re.search(r"/categories/(\d+)", href)
        if not m or m.group(1) in seen_cats: continue
        seen_cats.add(m.group(1))
        cats.append({"id": m.group(1), "name": a.get_text().strip() or f"Cat {m.group(1)}", "url": urljoin(base, href)})
    albums = parse_albums_from_soup(soup, base)[:48]
    # Translate category names and album titles
    cat_names = translate_batch([c["name"] for c in cats])
    for c, n in zip(cats, cat_names):
        c["name"] = n
    alb_titles = translate_batch([a["title"] for a in albums])
    for a, t in zip(albums, alb_titles):
        a["title"] = t
    name_translated = _translate_one(name)
    return jsonify({"name": name_translated, "base_url": base, "categories": cats, "albums": albums})

# ── Yupoo: browse category/album listing ──
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
    total_pages = get_total_pages(soup)
    title_tag = soup.find("title")
    title = (title_tag.get_text() if title_tag else "").split("|")[0].strip()
    # Translate album titles
    titles = translate_batch([a["title"] for a in albums])
    for a, t in zip(albums, titles):
        a["title"] = t
    title_translated = _translate_one(title) if title else title
    return jsonify({"albums": albums, "page": page, "total_pages": total_pages, "title": title_translated})

# ── Yupoo: album images ──
@app.route("/api/album")
def api_album():
    url = add_uid(request.args.get("url", "").strip())
    if not url: return jsonify({"error": "No URL"}), 400
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    soup = get_soup(url, referer=base + "/")
    if not soup: return jsonify({"error": "Could not load"}), 404
    title_tag = soup.find("title")
    title = (title_tag.get_text() if title_tag else "").split("|")[0].strip()
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
    title = _translate_one(title) if title else title
    return jsonify({"title": title, "url": url, "base_url": base, "images": images})

# ── Weidian: item detail ──
@app.route("/api/weidian/item")
def api_weidian_item():
    item_id = request.args.get("id", "").strip()
    if not re.match(r"^\d+$", item_id): return jsonify({"error": "Invalid item ID"}), 400
    item = fetch_weidian_item(item_id)
    item = translate_weidian_result(item)
    return jsonify(item)


# ── Translation endpoint ──
@app.route("/api/translate")
def api_translate():
    """Translate a single text string to English."""
    text = request.args.get("q", "").strip()
    if not text:
        return jsonify({"original": text, "translated": text})
    result = _translate_one(text)
    return jsonify({"original": text, "translated": result, "changed": result != text})

# ── Image proxy ──
@app.route("/img")
def img_proxy():
    url = request.args.get("url", "")
    ref = request.args.get("ref", "https://x.yupoo.com/")
    allowed = ("photo.yupoo.com", "si.geilicdn.com")
    if not any(h in url for h in allowed): return "Bad URL", 400
    try:
        r = SESSION.get(url, headers={**HEADERS_DESKTOP, "Referer": ref}, timeout=20, stream=True)
        if r.status_code != 200: return f"Error {r.status_code}", r.status_code
        return Response(r.iter_content(8192), content_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return str(e), 500

# ── SPA routes ──
@app.route("/static/<path:filename>")
def static_files(filename): return send_from_directory("static", filename)

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path): return HTML_APP

# ─── Frontend ─────────────────────────────────────────────────────────────────

HTML_APP = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#0a0a0a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="RepSearch">
<title>Rep Search</title>
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Inter:wght@400;500&display=swap">
<style>
:root{
  --bg:#0a0a0a; --bg2:#111; --bg3:#1a1a1a; --bg4:#222; --border:#2a2a2a;
  --text:#f0f0f0; --muted:#666; --accent:#ff6b00; --weidian:#1989fa;
  --ok:#30d158; --safe-top:env(safe-area-inset-top,0px); --safe-bot:env(safe-area-inset-bottom,0px);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased;overscroll-behavior:none}
#app{display:flex;flex-direction:column;height:100dvh;overflow:hidden}

/* Header */
.hdr{flex-shrink:0;display:flex;align-items:center;gap:10px;padding:calc(var(--safe-top)+10px) 14px 10px;background:var(--bg);border-bottom:1px solid var(--border);z-index:10}
.hdr-back{display:none;width:34px;height:34px;border:none;background:var(--bg3);border-radius:50%;color:var(--text);font-size:17px;cursor:pointer;flex-shrink:0;align-items:center;justify-content:center}
.hdr-back.on{display:flex}
.hdr-title{flex:1;font-family:'Syne',sans-serif;font-size:17px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:.3px}
.hdr-title em{color:var(--accent);font-style:normal}
.hdr-seller-btn{flex-shrink:0;padding:6px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;font-size:12px;font-weight:600;color:var(--text);cursor:pointer;white-space:nowrap;display:none}
.hdr-seller-btn.on{display:block}

/* Search bar */
.search-bar{flex-shrink:0;display:flex;gap:8px;padding:10px 14px;background:var(--bg);border-bottom:1px solid var(--border)}
.search-input{flex:1;background:var(--bg3);border:1.5px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-family:inherit;font-size:15px;outline:none;transition:border-color .2s}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:#444}
.btn-search{padding:10px 16px;background:var(--accent);color:#000;border:none;border-radius:10px;font-family:'Syne',sans-serif;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.5px;transition:opacity .15s;white-space:nowrap}
.btn-search:active{opacity:.75}
.btn-search:disabled{opacity:.4;cursor:not-allowed}

/* Filter */
.filter-bar{flex-shrink:0;padding:8px 14px;background:var(--bg);border-bottom:1px solid var(--border);display:none}
.filter-bar.on{display:block}
.filter-input{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:14px;outline:none}

/* Content */
#content{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch}
.view{display:none}
.view.on{display:block}

/* Home */
.home-wrap{padding:36px 20px 16px;display:flex;flex-direction:column;align-items:center}
.home-logo{font-family:'Syne',sans-serif;font-size:46px;font-weight:800;line-height:1;letter-spacing:-2px;margin-bottom:4px}
.home-logo em{color:var(--accent);font-style:normal}
.home-tagline{color:var(--muted);font-size:12px;margin-bottom:6px;text-align:center}
.platform-badges{display:flex;gap:6px;margin-bottom:24px}
.pbadge{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;letter-spacing:.5px}
.pbadge.yupoo{background:rgba(255,107,0,.15);color:var(--accent);border:1px solid rgba(255,107,0,.3)}
.pbadge.weidian{background:rgba(25,137,250,.15);color:var(--weidian);border:1px solid rgba(25,137,250,.3)}
.home-form{width:100%;max-width:420px;display:flex;flex-direction:column;gap:10px}
.home-input{width:100%;background:var(--bg3);border:1.5px solid var(--border);border-radius:12px;padding:14px 16px;color:var(--text);font-size:16px;outline:none;transition:border-color .2s}
.home-input:focus{border-color:var(--accent)}
.home-input::placeholder{color:#444}
.btn-go{width:100%;padding:14px;background:var(--accent);color:#000;border:none;border-radius:12px;font-family:'Syne',sans-serif;font-size:16px;font-weight:800;letter-spacing:1px;cursor:pointer;transition:opacity .15s}
.btn-go:active{opacity:.8}
.home-tips{margin-top:24px;width:100%;max-width:420px}
.tips-label{font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.tips-list{display:flex;flex-wrap:wrap;gap:7px}
.tip-chip{padding:6px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:20px;font-size:12px;cursor:pointer;color:var(--muted);transition:all .15s}
.tip-chip:active{color:var(--text);border-color:var(--muted)}

/* Recent */
.recent-wrap{padding:0 16px 24px;width:100%;max-width:500px}
.recent-list{display:flex;flex-direction:column;gap:6px}
.recent-row{display:flex;align-items:center;justify-content:space-between;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;cursor:pointer}
.recent-row:active{background:var(--bg3)}
.recent-q{font-weight:500;font-size:14px}
.recent-del{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:2px 6px}

/* Search results */
.sr-header{padding:10px 14px 4px;border-bottom:1px solid var(--border)}
.sr-stats{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.sr-tabs{display:flex;gap:2px}
.sr-tab{padding:7px 16px;border:none;border-radius:8px;font-family:'Syne',sans-serif;font-size:13px;font-weight:700;cursor:pointer;color:var(--muted);background:transparent;transition:all .15s;letter-spacing:.3px}
.sr-tab.active{color:#000}
.sr-tab.yupoo.active{background:var(--accent)}
.sr-tab.weidian.active{background:var(--weidian)}

/* Yupoo seller card */
.seller-card{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px}
.sc-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:8px}
.sc-name{font-family:'Syne',sans-serif;font-size:15px;font-weight:700}
.sc-sub{color:var(--muted);font-size:11px;margin-top:2px;display:flex;align-items:center;gap:6px}
.sc-badge{padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
.sc-badge.cat{background:rgba(255,107,0,.15);color:var(--accent)}
.sc-badge.alb{background:rgba(48,209,88,.12);color:var(--ok)}
.btn-browse{padding:6px 12px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;color:var(--text);white-space:nowrap;flex-shrink:0;margin-left:10px}
.sc-snippet{font-size:12px;color:var(--muted);margin-bottom:8px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.album-strip{display:flex;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px;scrollbar-width:none}
.album-strip::-webkit-scrollbar{display:none}
.strip-card{flex-shrink:0;width:90px;cursor:pointer}
.strip-thumb{width:90px;height:90px;object-fit:cover;border-radius:6px;background:var(--bg3);display:block}
.strip-ph{width:90px;height:90px;border-radius:6px;background:var(--bg3);display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--bg4)}
.strip-title{font-size:10px;color:var(--muted);margin-top:3px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sc-more{margin-top:6px;font-size:11px;color:var(--muted)}

/* Weidian item card */
.wd-card{display:flex;gap:12px;background:var(--bg2);border-bottom:1px solid var(--border);padding:12px 14px;cursor:pointer;transition:background .15s}
.wd-card:active{background:var(--bg3)}
.wd-thumb-wrap{flex-shrink:0;width:90px;height:90px;border-radius:8px;overflow:hidden;background:var(--bg3)}
.wd-thumb{width:100%;height:100%;object-fit:cover;display:block}
.wd-thumb-ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:24px;color:var(--bg4)}
.wd-info{flex:1;min-width:0}
.wd-platform{display:inline-block;padding:2px 7px;background:rgba(25,137,250,.15);color:var(--weidian);border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:4px}
.wd-title{font-size:13px;font-weight:600;line-height:1.4;margin-bottom:4px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.wd-sizes{font-size:11px;color:var(--muted);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

/* Weidian item detail view */
.wd-detail-hdr{padding:12px 14px;border-bottom:1px solid var(--border)}
.wd-detail-title{font-family:'Syne',sans-serif;font-size:16px;font-weight:700;line-height:1.4}
.wd-detail-sub{color:var(--muted);font-size:12px;margin-top:4px}
.wd-detail-link{display:inline-block;margin-top:8px;padding:8px 16px;background:var(--weidian);color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600}
.wd-img-list{display:flex;flex-direction:column;gap:2px;padding:2px}
.wd-img-item{background:var(--bg2)}
.wd-img-item img{width:100%;display:block;background:var(--bg3)}

/* Seller/listing views */
.seller-info{padding:14px 14px 10px;border-bottom:1px solid var(--border)}
.seller-name{font-family:'Syne',sans-serif;font-size:22px;font-weight:800}
.seller-url{color:var(--muted);font-size:11px;margin-top:2px}
.cats-scroll{display:flex;gap:8px;padding:10px 14px;overflow-x:auto;border-bottom:1px solid var(--border);-webkit-overflow-scrolling:touch;scrollbar-width:none}
.cats-scroll::-webkit-scrollbar{display:none}
.cat-chip{flex-shrink:0;padding:6px 13px;background:var(--bg3);border:1px solid var(--border);border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis;transition:all .15s}
.cat-chip.active{background:var(--accent);border-color:var(--accent);color:#000}
.grid-wrap{padding:0 2px 2px}
.grid-hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 12px 6px}
.grid-count{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)}
.album-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:2px}
.album-card{position:relative;background:var(--bg2);cursor:pointer;overflow:hidden;aspect-ratio:1;transition:opacity .15s}
.album-card:active{opacity:.7}
.album-thumb{width:100%;height:100%;object-fit:cover;display:block;background:var(--bg3)}
.album-ph{width:100%;height:100%;background:var(--bg3);display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--bg4)}
.album-label{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.88));padding:16px 7px 7px;font-size:10px;font-weight:500;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.load-more{display:flex;justify-content:center;padding:18px 14px}
.btn-more{width:100%;max-width:300px;padding:12px;background:var(--bg3);border:1px solid var(--border);border-radius:10px;font-family:'Syne',sans-serif;font-size:14px;font-weight:700;cursor:pointer;color:var(--text)}
.album-hdr{padding:12px 14px;border-bottom:1px solid var(--border)}
.album-title{font-family:'Syne',sans-serif;font-size:15px;font-weight:700;line-height:1.3}
.album-cnt{color:var(--muted);font-size:12px;margin-top:2px}
.img-list{display:flex;flex-direction:column;gap:2px;padding:2px}
.img-item{cursor:pointer;background:var(--bg2);overflow:hidden}
.img-item img{width:100%;display:block;background:var(--bg3);transition:opacity .2s}
.img-item img.loading{opacity:0}
.img-item img.loaded{opacity:1}

/* Lightbox */
#lb{display:none;position:fixed;inset:0;z-index:100;background:#000;flex-direction:column}
#lb.on{display:flex}
.lb-top{display:flex;align-items:center;justify-content:space-between;padding:calc(var(--safe-top)+10px) 14px 10px;flex-shrink:0}
.lb-close{width:34px;height:34px;background:rgba(255,255,255,.1);border:none;border-radius:50%;color:#fff;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.lb-ctr{color:rgba(255,255,255,.55);font-size:13px}
.lb-dl{width:34px;height:34px;background:rgba(255,255,255,.1);border:none;border-radius:50%;color:#fff;font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;text-decoration:none}
.lb-body{flex:1;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}
.lb-img{max-width:100%;max-height:100%;object-fit:contain;user-select:none}
.lb-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.12);border:none;width:36px;height:56px;border-radius:6px;color:#fff;font-size:20px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.lb-nav.prev{left:6px}
.lb-nav.next{right:6px}
.lb-thumbs{flex-shrink:0;display:flex;gap:4px;overflow-x:auto;padding:6px 6px calc(var(--safe-bot)+6px);scrollbar-width:none}
.lb-thumbs::-webkit-scrollbar{display:none}
.lb-t{flex-shrink:0;width:48px;height:48px;object-fit:cover;border-radius:4px;opacity:.45;cursor:pointer;transition:opacity .15s;border:2px solid transparent}
.lb-t.active{opacity:1;border-color:var(--accent)}

/* Translation indicator */
.trans-toggle{flex-shrink:0;display:flex;align-items:center;gap:5px;padding:5px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:20px;cursor:pointer;font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.3px;transition:all .15s;white-space:nowrap}
.trans-toggle.on{background:rgba(25,137,250,.12);border-color:rgba(25,137,250,.3);color:var(--weidian)}
.trans-dot{width:6px;height:6px;border-radius:50%;background:var(--muted)}
.trans-toggle.on .trans-dot{background:var(--weidian)}
.trans-banner{padding:4px 14px;background:rgba(25,137,250,.08);border-bottom:1px solid rgba(25,137,250,.15);font-size:11px;color:rgba(25,137,250,.7);display:none;align-items:center;gap:6px}
.trans-banner.on{display:flex}

/* States */
.spinner-wrap{display:flex;align-items:center;justify-content:center;padding:48px 20px;flex-direction:column;gap:10px}
.spinner{width:28px;height:28px;border:3px solid var(--bg3);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
.spinner-lbl{color:var(--muted);font-size:13px}
@keyframes spin{to{transform:rotate(360deg)}}
.err-box{margin:16px 14px;background:rgba(255,69,58,.1);border:1px solid rgba(255,69,58,.25);border-radius:10px;padding:14px;font-size:13px;color:#ff453a}
.empty-box{padding:40px 20px;text-align:center;color:var(--muted);font-size:14px;line-height:1.7}
.skel{background:linear-gradient(90deg,var(--bg2) 25%,var(--bg3) 50%,var(--bg2) 75%);background-size:200% 100%;animation:shimmer 1.4s infinite;border-radius:4px}
@keyframes shimmer{to{background-position:-200% 0}}
.section-label{font-family:'Syne',sans-serif;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:12px 14px 6px;color:var(--muted)}
</style>
</head>
<body>
<div id="app">

<header class="hdr">
  <button class="hdr-back" id="backBtn" onclick="goBack()">&#8592;</button>
  <div class="hdr-title" id="hdrTitle"><em>REP</em> SEARCH</div>
  <button class="hdr-seller-btn" id="hdrSellerBtn" onclick="openCurrentSeller()">Browse Seller</button>
  <div class="trans-toggle on" id="transToggle" onclick="toggleTranslation()" title="Auto-translate">
    <div class="trans-dot"></div>
    <span id="transLabel">EN</span>
  </div>
</header>
<div class="trans-banner" id="transBanner">
  🌐 Content auto-translated to English
</div>

<div class="search-bar">
  <input class="search-input" id="searchInput" type="search"
    placeholder="nike dunk, jordan 4 bred, yeezy 350…"
    autocorrect="off" autocapitalize="none" spellcheck="false"
    onkeydown="if(event.key==='Enter'){event.preventDefault();doSearch()}">
  <button class="btn-search" id="searchBtn" onclick="doSearch()">SEARCH</button>
</div>

<div class="filter-bar" id="filterBar">
  <input class="filter-input" id="filterInput" type="search"
    placeholder="Filter albums…" oninput="applyFilter(this.value)">
</div>

<div id="content">

  <!-- HOME -->
  <div class="view on" id="viewHome">
    <div class="home-wrap">
      <div class="home-logo"><em>REP</em>S</div>
      <div class="home-tagline">Search Yupoo &amp; Weidian simultaneously</div>
      <div class="platform-badges">
        <span class="pbadge yupoo">YUPOO</span>
        <span class="pbadge weidian">WEIDIAN</span>
      </div>
      <div class="home-form">
        <input class="home-input" id="homeInput" type="search"
          placeholder="e.g. nike air force 1, jordan 4"
          autocorrect="off" autocapitalize="none" spellcheck="false"
          onkeydown="if(event.key==='Enter'){event.preventDefault();doSearchFromHome()}">
        <button class="btn-go" onclick="doSearchFromHome()">SEARCH BOTH &#8594;</button>
      </div>
      <div class="home-tips">
        <div class="tips-label">Quick search</div>
        <div class="tips-list">
          <div class="tip-chip" onclick="quickSearch('nike dunk low panda')">Dunk Low Panda</div>
          <div class="tip-chip" onclick="quickSearch('jordan 4 bred')">Jordan 4 Bred</div>
          <div class="tip-chip" onclick="quickSearch('yeezy 350 zebra')">Yeezy 350 Zebra</div>
          <div class="tip-chip" onclick="quickSearch('air force 1 white')">AF1 White</div>
          <div class="tip-chip" onclick="quickSearch('new balance 550')">NB 550</div>
          <div class="tip-chip" onclick="quickSearch('jordan 1 chicago')">Jordan 1 Chicago</div>
          <div class="tip-chip" onclick="quickSearch('travis scott dunk')">Travis Scott Dunk</div>
          <div class="tip-chip" onclick="quickSearch('samba adidas')">Adidas Samba</div>
        </div>
      </div>
    </div>
    <div class="recent-wrap" id="recentWrap" style="display:none">
      <div class="tips-label" style="margin-bottom:8px">Recent</div>
      <div class="recent-list" id="recentList"></div>
    </div>
  </div>

  <!-- RESULTS -->
  <div class="view" id="viewResults">
    <div class="sr-header" id="srHeader" style="display:none">
      <div class="sr-stats" id="srStats"></div>
      <div class="sr-tabs">
        <button class="sr-tab yupoo active" id="tabYupoo" onclick="switchTab('yupoo')">YUPOO</button>
        <button class="sr-tab weidian" id="tabWeidian" onclick="switchTab('weidian')">WEIDIAN</button>
      </div>
    </div>
    <div id="yupooResults" class="tab-panel"></div>
    <div id="weidianResults" class="tab-panel" style="display:none"></div>
  </div>

  <!-- SELLER -->
  <div class="view" id="viewSeller">
    <div class="seller-info">
      <div class="seller-name" id="sellerName">—</div>
      <div class="seller-url" id="sellerUrl">—</div>
    </div>
    <div class="cats-scroll" id="catsScroll"></div>
    <div id="sellerAlbums"></div>
  </div>

  <!-- LISTING -->
  <div class="view" id="viewListing">
    <div id="listingContent"></div>
    <div class="load-more" id="loadMoreWrap" style="display:none">
      <button class="btn-more" onclick="loadMorePage()">Load more albums</button>
    </div>
  </div>

  <!-- ALBUM (Yupoo) -->
  <div class="view" id="viewAlbum">
    <div class="album-hdr">
      <div class="album-title" id="albumTitle">—</div>
      <div class="album-cnt" id="albumCnt">—</div>
    </div>
    <div class="img-list" id="imgList"></div>
  </div>

  <!-- WEIDIAN ITEM -->
  <div class="view" id="viewWdItem">
    <div class="wd-detail-hdr">
      <div class="wd-detail-title" id="wdTitle">—</div>
      <div class="wd-detail-sub" id="wdSizes">—</div>
      <a class="wd-detail-link" id="wdLink" target="_blank">Open in Weidian &#8599;</a>
    </div>
    <div class="wd-img-list" id="wdImgList"></div>
  </div>

</div>
</div>

<!-- Lightbox -->
<div id="lb">
  <div class="lb-top">
    <button class="lb-close" onclick="closeLb()">&#10005;</button>
    <span class="lb-ctr" id="lbCtr">1/1</span>
    <a class="lb-dl" id="lbDl" target="_blank">&#8681;</a>
  </div>
  <div class="lb-body">
    <button class="lb-nav prev" onclick="lbNav(-1)">&#8249;</button>
    <img class="lb-img" id="lbImg" src="" alt="">
    <button class="lb-nav next" onclick="lbNav(1)">&#8250;</button>
  </div>
  <div class="lb-thumbs" id="lbThumbs"></div>
</div>

<script>
const S = {
  view:'home', query:'', activeTab:'yupoo',
  sellerBase:'', sellerName:'',
  listingUrl:'', listingPage:1, listingTotal:1, listingAlbums:[],
  albumImages:[], lbIdx:0,
  _fromSearch:false,
  lastResults:null,
};

// ── Views ────────────────────────────────────────────────────────────────
function show(name){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('on'));
  document.getElementById('view'+cap(name)).classList.add('on');
  S.view=name;
  document.getElementById('backBtn').classList.toggle('on',!['home','results'].includes(name));
  document.getElementById('filterBar').classList.toggle('on',['seller','listing'].includes(name));
  document.getElementById('hdrSellerBtn').classList.toggle('on',
    ['listing','album'].includes(name) && !!S.sellerBase);
  if(!['listing','seller'].includes(name))
    document.getElementById('filterInput').value='';
}
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1)}
function setTitle(html){document.getElementById('hdrTitle').innerHTML=html}

function goBack(){
  if(S.view==='album'||S.view==='wdItem'){
    if(S._fromSearch){show('results'); renderTabPanels(S.lastResults);} 
    else{show('listing'); renderListingGrid(S.listingAlbums);}
    return;
  }
  if(S.view==='listing'){
    if(S._fromSearch){show('results'); renderTabPanels(S.lastResults);}
    else show('seller');
    return;
  }
  if(S.view==='seller'){show('results'); setTitle(`<em>${esc(S.query)}</em>`); return;}
  if(S.view==='results'){show('home'); setTitle('<em>REP</em> SEARCH'); return;}
}
function openCurrentSeller(){if(S.sellerBase) loadSeller(S.sellerBase);}

// ── Proxy URL ────────────────────────────────────────────────────────────
function px(url, ref){
  ref=ref||(S.sellerBase?S.sellerBase+'/':'https://x.yupoo.com/');
  return '/img?url='+encodeURIComponent(url)+'&ref='+encodeURIComponent(ref);
}

// ── Recent ───────────────────────────────────────────────────────────────
const RK='reps_searches';
function getRecent(){return JSON.parse(localStorage.getItem(RK)||'[]')}
function saveRecent(q){let r=getRecent().filter(x=>x!==q);r.unshift(q);r=r.slice(0,8);localStorage.setItem(RK,JSON.stringify(r));renderRecent()}
function renderRecent(){
  const r=getRecent();
  document.getElementById('recentWrap').style.display=r.length?'':'none';
  document.getElementById('recentList').innerHTML=r.map((q,i)=>`
    <div class="recent-row" onclick="quickSearch('${esc(q)}')">
      <span class="recent-q">${esc(q)}</span>
      <button class="recent-del" onclick="event.stopPropagation();delRecent(${i})">&#215;</button>
    </div>`).join('');
}
function delRecent(i){const r=getRecent();r.splice(i,1);localStorage.setItem(RK,JSON.stringify(r));renderRecent()}

// ── Search ───────────────────────────────────────────────────────────────
function doSearchFromHome(){
  const q=document.getElementById('homeInput').value.trim();
  if(q){document.getElementById('searchInput').value=q; doSearch();}
}
function quickSearch(q){
  document.getElementById('homeInput').value=q;
  document.getElementById('searchInput').value=q;
  doSearch();
}
function doSearch(){
  const q=document.getElementById('searchInput').value.trim();
  if(!q) return;
  S.query=q; S.sellerBase=''; S._fromSearch=true;
  document.getElementById('homeInput').value=q;
  saveRecent(q);
  show('results');
  setTitle(`<em>${esc(q)}</em>`);
  document.getElementById('srHeader').style.display='none';
  document.getElementById('yupooResults').innerHTML=spinHtml('Searching Yupoo &amp; Weidian…');
  document.getElementById('weidianResults').innerHTML='';
  const btn=document.getElementById('searchBtn');
  btn.disabled=true; btn.textContent='…';

  fetch(searchWithTranslation('/api/search?q='+encodeURIComponent(q)+'&max=15'))
    .then(r=>r.json())
    .then(data=>{
      btn.disabled=false; btn.textContent='SEARCH';
      if(data.error){document.getElementById('yupooResults').innerHTML=errHtml(data.error); return;}
      S.lastResults=data;
      renderTabPanels(data);
    })
    .catch(()=>{
      btn.disabled=false; btn.textContent='SEARCH';
      document.getElementById('yupooResults').innerHTML=errHtml('Search failed. Check your connection.');
    });
}

function renderTabPanels(data){
  const yupoo=data.yupoo||[]; const weidian=data.weidian||[];
  const hdr=document.getElementById('srHeader');
  hdr.style.display='';
  document.getElementById('srStats').textContent=
    `${yupoo.length} YUPOO SELLERS  ·  ${weidian.length} WEIDIAN ITEMS`;
  document.getElementById('tabYupoo').textContent=`YUPOO (${yupoo.length})`;
  document.getElementById('tabWeidian').textContent=`WEIDIAN (${weidian.length})`;

  // Yupoo panel
  const yp=document.getElementById('yupooResults');
  if(!yupoo.length){
    yp.innerHTML='<div class="empty-box">No Yupoo sellers found for this search.</div>';
  } else {
    yp.innerHTML=yupoo.map(r=>sellerCardHtml(r)).join('');
  }

  // Weidian panel
  const wp=document.getElementById('weidianResults');
  if(!weidian.length){
    wp.innerHTML='<div class="empty-box">No Weidian listings found for this search.</div>';
  } else {
    wp.innerHTML='<div class="section-label">WEIDIAN LISTINGS</div>'+weidian.map(item=>wdCardHtml(item)).join('');
  }

  switchTab(S.activeTab);
  showTransBanner(yupoo.some(r=>r.snippet||r.seller_name)||weidian.length>0);
}

function switchTab(tab){
  S.activeTab=tab;
  document.getElementById('tabYupoo').classList.toggle('active', tab==='yupoo');
  document.getElementById('tabWeidian').classList.toggle('active', tab==='weidian');
  document.getElementById('yupooResults').style.display=tab==='yupoo'?'':'none';
  document.getElementById('weidianResults').style.display=tab==='weidian'?'':'none';
}

// ── Yupoo seller card ────────────────────────────────────────────────────
function sellerCardHtml(r){
  const badgeCls=r.page_type==='album'?'alb':'cat';
  const badgeTxt=r.page_type==='album'?'ALBUM':'CATEGORY';
  const albums=r.albums||[];
  const strp=albums.length
    ?albums.slice(0,8).map(a=>`
      <div class="strip-card" onclick="event.stopPropagation();openAlbum('${esc(a.url)}','${esc(a.title)}','${esc(r.base_url)}')">
        ${a.thumb
          ?`<img class="strip-thumb" loading="lazy" src="${px(a.thumb,r.base_url+'/')}" alt="${esc(a.title)}"
               onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
             <div class="strip-ph" style="display:none">📦</div>`
          :`<div class="strip-ph">📦</div>`}
        <div class="strip-title">${esc(a.title)}</div>
      </div>`).join('')
    :`<div style="color:var(--muted);font-size:13px;padding:4px">No matching albums found</div>`;
  const more=r.total_albums>8?`<div class="sc-more">+${r.total_albums-8} more albums in this category</div>`:'';
  return `<div class="seller-card">
    <div class="sc-top">
      <div>
        <div class="sc-name">${esc(r.seller_name||r.seller)}</div>
        <div class="sc-sub">
          <span>${esc(r.seller)}.x.yupoo.com</span>
          <span class="sc-badge ${badgeCls}">${badgeTxt}</span>
        </div>
      </div>
      <button class="btn-browse" onclick="loadSeller('${esc(r.base_url)}')">Browse</button>
    </div>
    ${r.snippet?`<div class="sc-snippet">${esc(r.snippet)}</div>`:''}
    <div class="album-strip">${strp}</div>
    ${more}
  </div>`;
}

// ── Weidian item card ────────────────────────────────────────────────────
function wdCardHtml(item){
  const ref='https://weidian.com/';
  const thumb=item.cover
    ?`<img class="wd-thumb" loading="lazy" src="${px(item.cover,ref)}" alt="${esc(item.title)}"
         onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="wd-thumb-ph" style="display:none">📦</div>`
    :`<div class="wd-thumb-ph">📦</div>`;
  const sizes=item.sizes&&item.sizes.length
    ?`Sizes: ${item.sizes.join(' · ')}` : (item.description||'').split('\n').slice(1,3).join(' ');
  return `<div class="wd-card" onclick="openWdItem('${esc(item.item_id)}','${esc(item.url)}')">
    <div class="wd-thumb-wrap">${thumb}</div>
    <div class="wd-info">
      <div class="wd-platform">WEIDIAN</div>
      <div class="wd-title">${esc(item.title||'Weidian Item')}</div>
      <div class="wd-sizes">${esc(sizes.substring(0,120))}</div>
    </div>
  </div>`;
}

// ── Open Weidian item ────────────────────────────────────────────────────
function openWdItem(itemId, url){
  show('wdItem');
  setTitle('<em>WEIDIAN</em> ITEM');
  document.getElementById('wdTitle').textContent='Loading…';
  document.getElementById('wdSizes').textContent='';
  document.getElementById('wdLink').href=url;
  document.getElementById('wdImgList').innerHTML=spinHtml('Loading…');

  // Use cached data from search results if available
  const cached=S.lastResults&&S.lastResults.weidian&&S.lastResults.weidian.find(i=>i.item_id===itemId);
  if(cached && cached.images && cached.images.length){
    renderWdItem(cached, url);
    return;
  }

  fetch('/api/weidian/item?id='+encodeURIComponent(itemId))
    .then(r=>r.json())
    .then(data=>{
      if(data.error){document.getElementById('wdImgList').innerHTML=errHtml(data.error); return;}
      renderWdItem(data, url);
    })
    .catch(()=>document.getElementById('wdImgList').innerHTML=errHtml('Failed to load item.'));
}

function renderWdItem(data, url){
  document.getElementById('wdTitle').textContent=data.title||'Weidian Item';
  const sizeTxt=data.sizes&&data.sizes.length?'Sizes: '+data.sizes.join(' · '):'';
  const colorTxt=data.colors&&data.colors.length?'  Colors: '+data.colors.join(', '):'';
  document.getElementById('wdSizes').textContent=(sizeTxt+colorTxt).substring(0,200);
  document.getElementById('wdLink').href=url||data.url;
  setTitle(`<em>${esc((data.title||'Item').substring(0,35))}</em>`);
  const imgs=data.images||[];
  if(!imgs.length){
    document.getElementById('wdImgList').innerHTML='<div class="empty-box">No images found.</div>';
    return;
  }
  const ref='https://weidian.com/';
  S.albumImages=imgs.map(u=>({url:u, orig:u, thumb:u}));
  document.getElementById('wdImgList').innerHTML=imgs.map((img,i)=>
    `<div class="wd-img-item" onclick="openLb(${i})">
      <img loading="lazy" src="${px(img,ref)}" alt=""
           onload="this.style.opacity=1" style="opacity:0;transition:opacity .2s">
    </div>`).join('');
}

// ── Load seller (Yupoo) ──────────────────────────────────────────────────
function loadSeller(url){
  S.sellerBase=url; S._fromSearch=false;
  show('seller');
  document.getElementById('sellerAlbums').innerHTML=spinHtml('Loading seller…');
  document.getElementById('catsScroll').innerHTML='';
  setTitle('Loading…');
  fetch('/api/seller?url='+encodeURIComponent(url))
    .then(r=>r.json())
    .then(d=>{
      if(d.error){document.getElementById('sellerAlbums').innerHTML=errHtml(d.error); return;}
      S.sellerName=d.name; S.sellerBase=d.base_url;
      document.getElementById('sellerName').textContent=d.name;
      document.getElementById('sellerUrl').textContent=d.base_url;
      setTitle(`<em>${esc(d.name)}</em>`);
      // Cats
      const el=document.getElementById('catsScroll');
      if(d.categories.length){
        el.style.display='flex';
        el.innerHTML=`<div class="cat-chip active" onclick="activateCat(this,'${esc(d.base_url)}/albums')">All</div>`+
          d.categories.slice(0,40).map(c=>`<div class="cat-chip" onclick="activateCat(this,'${esc(c.url)}')">${esc(c.name)}</div>`).join('');
      } else el.style.display='none';
      // Albums
      const ag=document.getElementById('sellerAlbums');
      if(!d.albums.length){ag.innerHTML='<div class="empty-box">No albums found.</div>'; return;}
      ag.innerHTML=`<div class="grid-hdr"><span class="grid-count">${d.albums.length} ALBUMS</span></div>`+
        `<div class="album-grid" id="sellerGrid">${d.albums.map(a=>albumCardHtml(a)).join('')}</div>`;
    })
    .catch(()=>document.getElementById('sellerAlbums').innerHTML=errHtml('Failed to load seller.'));
}
function activateCat(el, url){
  document.querySelectorAll('.cat-chip').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  loadListing(url, false);
}

// ── Listing ──────────────────────────────────────────────────────────────
function loadListing(url, fromSearch){
  S.listingUrl=url; S.listingPage=1; S.listingAlbums=[]; S._fromSearch=!!fromSearch;
  show('listing');
  document.getElementById('listingContent').innerHTML=skelGrid();
  document.getElementById('loadMoreWrap').style.display='none';
  fetchListPage(1, true);
}
function fetchListPage(page, replace){
  fetch('/api/browse?url='+encodeURIComponent(S.listingUrl)+'&page='+page)
    .then(r=>r.json())
    .then(d=>{
      if(d.error){document.getElementById('listingContent').innerHTML=errHtml(d.error); return;}
      S.listingPage=d.page; S.listingTotal=d.total_pages;
      S.listingAlbums=replace?d.albums:S.listingAlbums.concat(d.albums);
      setTitle(`<em>${esc((d.title||'Albums').substring(0,40))}</em>`);
      renderListingGrid(S.listingAlbums);
      document.getElementById('loadMoreWrap').style.display=(d.page<d.total_pages)?'flex':'none';
    })
    .catch(()=>document.getElementById('listingContent').innerHTML=errHtml('Failed to load.'));
}
function loadMorePage(){fetchListPage(S.listingPage+1, false)}
function renderListingGrid(albums){
  const el=document.getElementById('listingContent');
  if(!albums.length){el.innerHTML='<div class="empty-box">No albums found.</div>'; return;}
  el.innerHTML=`<div class="grid-hdr"><span class="grid-count">${albums.length} ALBUMS</span></div>`+
    `<div class="album-grid" id="listingGrid">${albums.map(a=>albumCardHtml(a)).join('')}</div>`;
}
function applyFilter(q){
  const lower=q.toLowerCase();
  ['listingGrid','sellerGrid'].forEach(id=>{
    const grid=document.getElementById(id);
    if(grid) grid.querySelectorAll('.album-card').forEach(c=>{
      c.style.display=(!lower||(c.dataset.title||'').toLowerCase().includes(lower))?'':'none';
    });
  });
}
function albumCardHtml(a){
  const ref=S.sellerBase?S.sellerBase+'/':'https://x.yupoo.com/';
  const thumb=a.thumb
    ?`<img class="album-thumb" loading="lazy" src="${px(a.thumb,ref)}" alt="${esc(a.title)}"
         onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
       <div class="album-ph" style="display:none">📦</div>`
    :`<div class="album-ph">📦</div>`;
  return `<div class="album-card" data-title="${esc(a.title)}" onclick="loadAlbum('${esc(a.url)}','${esc(a.title)}')">
    ${thumb}<div class="album-label">${esc(a.title)}</div>
  </div>`;
}

// ── Album / lightbox ─────────────────────────────────────────────────────
function openAlbum(url, title, base){
  S.sellerBase=base||S.sellerBase; S._fromSearch=true;
  loadAlbum(url, title);
}
function loadAlbum(url, title){
  show('album');
  document.getElementById('albumTitle').textContent=title||'…';
  document.getElementById('albumCnt').textContent='';
  document.getElementById('imgList').innerHTML=spinHtml('Loading images…');
  setTitle(`<em>${esc((title||'').substring(0,40))}</em>`);
  fetch('/api/album?url='+encodeURIComponent(url))
    .then(r=>r.json())
    .then(d=>{
      if(d.error){document.getElementById('imgList').innerHTML=errHtml(d.error); return;}
      S.albumImages=d.images; S.sellerBase=d.base_url||S.sellerBase;
      document.getElementById('albumTitle').textContent=d.title;
      document.getElementById('albumCnt').textContent=d.images.length+' images';
      setTitle(`<em>${esc(d.title.substring(0,40))}</em>`);
      const ref=d.base_url+'/';
      document.getElementById('imgList').innerHTML=d.images.map((img,i)=>
        `<div class="img-item" onclick="openLb(${i})">
          <img loading="lazy" class="loading" src="${px(img.url,ref)}" alt="${esc(img.alt)}"
            onload="this.classList.replace('loading','loaded')"
            onerror="this.classList.replace('loading','loaded')">
        </div>`).join('');
    })
    .catch(()=>document.getElementById('imgList').innerHTML=errHtml('Failed to load album.'));
}
function openLb(i){S.lbIdx=i; document.getElementById('lb').classList.add('on'); renderLb()}
function closeLb(){document.getElementById('lb').classList.remove('on')}
function lbNav(d){S.lbIdx=(S.lbIdx+d+S.albumImages.length)%S.albumImages.length; renderLb()}
function renderLb(){
  const imgs=S.albumImages; const i=S.lbIdx; const img=imgs[i]; if(!img) return;
  const isWd=!img.orig||img.orig===img.url;
  const ref=isWd?'https://weidian.com/':(S.sellerBase?S.sellerBase+'/':'https://x.yupoo.com/');
  document.getElementById('lbImg').src=px(img.url,ref);
  document.getElementById('lbCtr').textContent=`${i+1}/${imgs.length}`;
  document.getElementById('lbDl').href=px(img.orig||img.url,ref);
  const tb=document.getElementById('lbThumbs');
  if(!tb.children.length){
    tb.innerHTML=imgs.map((im,idx)=>
      `<img class="lb-t${idx===i?' active':''}" loading="lazy"
        src="${px(im.thumb||im.url,ref)}"
        onclick="S.lbIdx=${idx};renderLb()" alt="">`).join('');
  } else {
    tb.querySelectorAll('.lb-t').forEach((t,idx)=>t.classList.toggle('active',idx===i));
    const act=tb.querySelector('.lb-t.active');
    if(act) act.scrollIntoView({inline:'center',behavior:'smooth'});
  }
}
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lb').classList.contains('on')) return;
  if(e.key==='ArrowRight') lbNav(1);
  if(e.key==='ArrowLeft') lbNav(-1);
  if(e.key==='Escape') closeLb();
});
let _tx=0;
document.getElementById('lb').addEventListener('touchstart',e=>{_tx=e.touches[0].clientX},{passive:true});
document.getElementById('lb').addEventListener('touchend',e=>{
  const dx=e.changedTouches[0].clientX-_tx;
  if(Math.abs(dx)>50) lbNav(dx<0?1:-1);
});

// ── Helpers ──────────────────────────────────────────────────────────────
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function spinHtml(msg){return`<div class="spinner-wrap"><div class="spinner"></div><div class="spinner-lbl">${msg}</div></div>`}
function errHtml(msg){return`<div class="err-box">&#9888; ${esc(msg)}</div>`}
function skelGrid(){return`<div class="album-grid">${Array(8).fill('<div class="album-card skel" style="aspect-ratio:1"></div>').join('')}</div>`}

// ── Translation toggle ───────────────────────────────────────────────────
let _translateOn = localStorage.getItem('trans_on') !== 'false';
updateTransUI();

function toggleTranslation(){
  _translateOn = !_translateOn;
  localStorage.setItem('trans_on', _translateOn);
  updateTransUI();
}
function updateTransUI(){
  const tog = document.getElementById('transToggle');
  const lbl = document.getElementById('transLabel');
  tog.classList.toggle('on', _translateOn);
  lbl.textContent = _translateOn ? 'EN' : 'ZH';
}
function showTransBanner(show){
  document.getElementById('transBanner').classList.toggle('on', show && _translateOn);
}
function searchWithTranslation(url){
  // Append &translate=0 if translation is off
  return _translateOn ? url : url + (url.includes('?') ? '&' : '?') + 'translate=0';
}

// ── Init ─────────────────────────────────────────────────────────────────
renderRecent();
if('serviceWorker' in navigator) navigator.serviceWorker.register('/static/sw.js');
const _qs=new URLSearchParams(location.search);
const _q=_qs.get('q');
if(_q){document.getElementById('searchInput').value=_q;document.getElementById('homeInput').value=_q;doSearch();}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
