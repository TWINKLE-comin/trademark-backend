"""
Trademark Search Backend API - Full Detail Scraper (fixed)
==========================================================
Scrapes QuickCompany.in search results AND visits each detail page.

Fixes vs. original:
  - Scrape failures (403, captcha, timeout) now return a 503 error instead of
    an empty list, so the widget never falsely says "may be available".
  - Empty results are NEVER cached.
  - /debug route (protected by ADMIN_SECRET) to see what the source returns.
  - Mobile number is normalised.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time
import re
import hashlib
import json
import os
from urllib.parse import urlencode
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

BASE_URL      = "https://www.quickcompany.in"
SEARCH_URL    = f"{BASE_URL}/trademarks"
CACHE_TTL_HRS = 24
REQUEST_DELAY = 1.0
MAX_DETAIL_WORKERS = 5   # parallel detail page fetches

# Each page fetched through the proxy uses credits, so these are env-configurable.
MAX_PAGES   = int(os.environ.get("MAX_PAGES", "1"))      # search result pages
MAX_DETAILS = int(os.environ.get("MAX_DETAILS", "10"))   # detail pages per search

# Optional scraping proxy (ScraperAPI). Set SCRAPER_API_KEY in Render to enable.
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()
SCRAPER_API_URL = "https://api.scraperapi.com/"
SCRAPER_COUNTRY = os.environ.get("SCRAPER_COUNTRY", "in")
# Set SCRAPER_PREMIUM=true if normal proxies still get blocked (uses more credits)
SCRAPER_PREMIUM = os.environ.get("SCRAPER_PREMIUM", "").lower() in ("1", "true", "yes")
PROXY_TIMEOUT   = 70

SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
}

# Text that usually appears on block / captcha pages
BLOCK_MARKERS = [
    "just a moment", "cf-browser-verification", "attention required",
    "access denied", "captcha", "unusual traffic", "are you a robot",
]

_cache = {}


class SourceError(Exception):
    """Raised when the trademark source can't be reached or is blocking us."""
    pass


# ─────────────────────────────────────────────
#  APP
# ─────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ─────────────────────────────────────────────
#  CACHE
# ─────────────────────────────────────────────

def cache_key(q):
    return hashlib.md5(q.strip().lower().encode()).hexdigest()

def cache_get(key):
    e = _cache.get(key)
    if e and datetime.utcnow() < e["expires"]:
        return e["data"]
    return None

def cache_set(key, data):
    _cache[key] = {"data": data, "expires": datetime.utcnow() + timedelta(hours=CACHE_TTL_HRS)}

# ─────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────

def make_session():
    s = requests.Session()
    s.headers.update(SCRAPER_HEADERS)
    if not SCRAPER_API_KEY:          # warm-up only needed for direct requests
        try:
            s.get(BASE_URL, timeout=10)
        except Exception:
            pass
    return s

# ─────────────────────────────────────────────
#  HTTP GET (direct, or through ScraperAPI if key is set)
# ─────────────────────────────────────────────

def http_get(session, url, params=None, timeout=15):
    """GET a URL. Uses ScraperAPI when SCRAPER_API_KEY is set, else direct."""
    if not SCRAPER_API_KEY:
        return session.get(url, params=params, timeout=timeout)

    target = url + ("?" + urlencode(params) if params else "")
    proxy_params = {
        "api_key": SCRAPER_API_KEY,
        "url": target,
        "country_code": SCRAPER_COUNTRY,
    }
    if SCRAPER_PREMIUM:
        proxy_params["premium"] = "true"

    resp = requests.get(SCRAPER_API_URL, params=proxy_params, timeout=PROXY_TIMEOUT)

    if resp.status_code == 401:
        raise SourceError("Proxy: invalid SCRAPER_API_KEY")
    if resp.status_code == 403:
        raise SourceError("Proxy: out of credits or plan doesn't allow this site")
    if resp.status_code == 429:
        raise SourceError("Proxy: too many concurrent requests")
    return resp


# ─────────────────────────────────────────────
#  SEARCH PAGE SCRAPER
# ─────────────────────────────────────────────

def looks_blocked(html):
    low = html[:5000].lower()
    return any(m in low for m in BLOCK_MARKERS)


def fetch_search_page(session, query, page):
    """Return soup. Raises SourceError if the source fails or blocks us."""
    try:
        resp = http_get(session, SEARCH_URL, params={"q": query, "page": page}, timeout=15)
    except requests.RequestException as e:
        raise SourceError(f"Request failed: {e}")

    if resp.status_code != 200:
        raise SourceError(f"Source returned HTTP {resp.status_code}")

    if looks_blocked(resp.text):
        raise SourceError("Source returned a block/captcha page")

    return BeautifulSoup(resp.text, "html.parser")


def parse_search_results(soup):
    """Extract trademark links and basic info from search results page."""
    results = []

    links = soup.find_all("a", href=re.compile(r"/trademarks/[a-zA-Z0-9\-]+"))
    seen = set()

    for link in links:
        href = link.get("href", "")
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in ("trademarks", "search") or slug in seen:
            continue
        if not re.search(r'[a-zA-Z]', slug) and not re.search(r'\d{5,}', slug):
            continue
        seen.add(slug)

        full_url = BASE_URL + href if href.startswith("/") else href

        title = link.get_text(strip=True)
        if not title or len(title) < 2:
            parent = link.parent
            if parent:
                title = parent.get_text(strip=True)[:60]

        results.append({
            "title": title or slug.replace("-", " ").title(),
            "url":   full_url,
            "slug":  slug,
        })

    return results

# ─────────────────────────────────────────────
#  DETAIL PAGE SCRAPER
# ─────────────────────────────────────────────

def fetch_detail(session, tm):
    """Visit the trademark detail page and extract full info."""
    url = tm.get("url", "")
    if not url:
        return tm

    try:
        if not SCRAPER_API_KEY:
            time.sleep(0.3)
        resp = http_get(session, url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        def find_value(labels):
            """Find a value near a label text on the page."""
            for label_text in labels:
                for dt in soup.find_all(["dt", "th", "label", "strong", "b"]):
                    if label_text.lower() in dt.get_text(strip=True).lower():
                        sibling = dt.find_next_sibling(["dd", "td", "span", "p"])
                        if sibling:
                            val = sibling.get_text(strip=True)
                            if val and len(val) > 0:
                                return val
                for el in soup.find_all(["div", "span", "p", "td"]):
                    text = el.get_text(strip=True)
                    if label_text.lower() in text.lower() and len(text) < 80:
                        next_el = el.find_next_sibling()
                        if next_el:
                            val = next_el.get_text(strip=True)
                            if val and 1 < len(val) < 200:
                                return val
            return "—"

        title_el = (
            soup.select_one("h1")
            or soup.select_one("h2")
            or soup.select_one(".trademark-name")
            or soup.select_one(".brand-name")
        )
        title = title_el.get_text(strip=True) if title_el else tm.get("title", "—")

        app_slug = tm.get("slug", "")
        app_num_match = re.search(r'\d{5,}', app_slug)
        app_num = app_num_match.group(0) if app_num_match else app_slug

        status      = find_value(["status", "trademark status", "current status"])
        applicant   = find_value(["applicant", "owner", "proprietor", "filed by"])
        tm_class    = find_value(["class", "nice class", "trademark class", "goods & services"])
        filing_date = find_value(["filing date", "application date", "date of filing", "filed on"])
        description = find_value(["goods & services", "description", "specification"])

        for meta in soup.find_all("meta"):
            content = meta.get("content", "")
            name    = (meta.get("name") or "") + (meta.get("property") or "")
            if "applicant" in name.lower() and applicant == "—":
                applicant = content
            if "status" in name.lower() and status == "—":
                status = content
            if "class" in name.lower() and tm_class == "—":
                tm_class = content

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    if applicant == "—" and data.get("applicant"):
                        applicant = str(data["applicant"])
                    if status == "—" and data.get("status"):
                        status = str(data["status"])
                    if filing_date == "—" and data.get("filingDate"):
                        filing_date = str(data["filingDate"])
            except Exception:
                pass

        tm.update({
            "title":              title,
            "application_number": app_num or app_slug,
            "status":             status,
            "applicant":          applicant,
            "class":              tm_class,
            "filing_date":        filing_date,
            "description":        description if description != "—" else "",
            "url":                url,
        })

    except Exception as e:
        print(f"  [DETAIL ERROR] {url}: {e}")
        tm.setdefault("application_number", tm.get("slug", "—"))
        tm.setdefault("status", "—")
        tm.setdefault("applicant", "—")
        tm.setdefault("class", "—")
        tm.setdefault("filing_date", "—")

    return tm


# ─────────────────────────────────────────────
#  MAIN SCRAPER
# ─────────────────────────────────────────────

def scrape_trademarks(query):
    key    = cache_key(query)
    cached = cache_get(key)
    if cached is not None:
        print(f"  [CACHE HIT] '{query}'")
        return cached

    print(f"  [SCRAPING] '{query}'")
    session  = make_session()
    all_tms  = []
    seen_url = set()

    # Step 1: collect trademark URLs from search pages
    for page in range(1, MAX_PAGES + 1):
        print(f"    Search page {page}...")
        try:
            soup = fetch_search_page(session, query, page)
        except SourceError:
            if page == 1:
                raise            # first page failing = real failure, tell the user
            print(f"    Page {page} failed, using results so far.")
            break

        results = parse_search_results(soup)
        if not results:
            print(f"    No results on page {page}, stopping.")
            break

        for tm in results:
            if tm["url"] not in seen_url:
                seen_url.add(tm["url"])
                all_tms.append(tm)

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY)

    total_found = len(all_tms)
    all_tms = all_tms[:MAX_DETAILS]
    print(f"    Found {total_found} trademarks. Fetching details for {len(all_tms)}...")

    # Step 2: fetch detail pages in parallel
    enriched = []
    with ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as executor:
        futures = {executor.submit(fetch_detail, session, tm): tm for tm in all_tms}
        for future in as_completed(futures):
            try:
                enriched.append(future.result())
            except Exception as e:
                print(f"  [THREAD ERROR] {e}")
                enriched.append(futures[future])

    # Deduplicate by application number
    seen_ids = set()
    final = []
    for tm in enriched:
        uid = tm.get("application_number", "") or tm.get("url", "")
        if uid and uid not in seen_ids:
            seen_ids.add(uid)
            final.append(tm)

    # IMPORTANT: never cache empty results
    if final:
        cache_set(key, final)

    print(f"    Done. {len(final)} unique trademarks.")
    return final


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/search")
def api_search():
    query  = request.args.get("q", "").strip()
    mobile = request.args.get("mobile", "").strip()

    if not query:
        return jsonify({"error": "Brand name is required."}), 400
    if not mobile or not re.match(r'^[0-9+\-\s]{7,15}$', mobile):
        return jsonify({"error": "A valid mobile number is required."}), 400

    log_lead(query, mobile)

    try:
        results = scrape_trademarks(query)
        return jsonify({
            "query":   query,
            "count":   len(results),
            "results": results,
        })
    except SourceError as e:
        print(f"  [SOURCE ERROR] {e}")
        return jsonify({
            "error": "The trademark database is temporarily unavailable. Please try again later."
        }), 503
    except Exception as e:
        print(f"  [ERROR] {e}")
        return jsonify({"error": "Search failed. Please try again later."}), 500


# ─────────────────────────────────────────────
#  DEBUG (protected) - remove when finished
#  Usage: /debug?q=tata&secret=YOUR_ADMIN_SECRET
# ─────────────────────────────────────────────

@app.route("/debug")
def debug():
    secret       = request.args.get("secret", "")
    admin_secret = os.environ.get("ADMIN_SECRET", "changeme123")
    if secret != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401

    q = request.args.get("q", "tata")
    s = make_session()
    try:
        r = http_get(s, SEARCH_URL, params={"q": q}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        return jsonify({
            "using_proxy":  bool(SCRAPER_API_KEY),
            "status_code":  r.status_code,
            "final_url":    r.url,
            "html_length":  len(r.text),
            "looks_blocked": looks_blocked(r.text),
            "links_parsed": len(parse_search_results(soup)),
            "snippet":      r.text[:600],
        })
    except Exception as e:
        return jsonify({"using_proxy": bool(SCRAPER_API_KEY), "error": str(e)})


# ─────────────────────────────────────────────
#  LEAD LOGGING
# ─────────────────────────────────────────────

LEADS_FILE = "leads.json"

def log_lead(query, mobile):
    try:
        leads = []
        if os.path.exists(LEADS_FILE):
            with open(LEADS_FILE, "r") as f:
                leads = json.load(f)
        leads.append({"timestamp": datetime.utcnow().isoformat(), "brand": query, "mobile": mobile})
        with open(LEADS_FILE, "w") as f:
            json.dump(leads, f, indent=2)
    except Exception as e:
        print(f"  [LEAD LOG ERROR] {e}")


@app.route("/api/leads")
def get_leads():
    secret       = request.args.get("secret", "")
    admin_secret = os.environ.get("ADMIN_SECRET", "changeme123")
    if secret != admin_secret:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        if not os.path.exists(LEADS_FILE):
            return jsonify({"leads": [], "count": 0})
        with open(LEADS_FILE, "r") as f:
            leads = json.load(f)
        return jsonify({"leads": leads, "count": len(leads)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  Trademark API running on http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
