"""
AutoScout24 + 2ememain Toyota Corolla — Multi-source Scraper + Dashboard
=========================================================================
Run:  python autoscout24_2ememain_dashboard.py

Requires:
    pip install playwright pandas plotly
    python -m playwright install chromium

CHANGELOG (v4):
- AutoScout24: [Certain, confirmed by real captured HTML] every listing
  page embeds a schema.org JSON-LD block (<script type="application/
  ld+json">) with a clean ItemList: price, mileage, fuelType,
  vehicleTransmission (gearbox!), seller.@type (AutoDealer vs Person —
  real seller type, no more code-guessing), and the canonical URL. This
  is read FIRST and matched to each <article data-guid=...> by the guid
  embedded in the JSON-LD url's trailing UUID. DOM scraping is now only
  used as a fallback for fields the JSON-LD doesn't carry (year/zip/
  price-label/leads-range/raw fuel+seller codes) or for the rare listing
  the JSON-LD block doesn't include. This should fix the link problem
  completely rather than patch around it — the URL bug, the missing
  gearbox, and the seller-type guessing all shared the same root cause
  (extracting from rendered text instead of this structured block).
- 2ememain: replaced regex-on-flattened-text with real selectors, based
  on actual captured markup: attribute spans keyed by icon class
  (hz-SvgIconCarConstructionYear/Mileage/Fuel/Transmission/Body), title
  via ListingListViewContentCars_title__ span, price via the h4, seller
  type via the listing image's title attribute (contains a literal
  "Particulier"/"Entreprise" tag), location/posted-date via a
  closest('li') ancestor since they sit outside the anchor.
- 2ememain listings missing year OR mileage are now dropped entirely,
  per explicit instruction, rather than kept with gaps.
- 2ememain pagination switched from scroll-triggering (which was only
  surfacing ~23 of 350+ listings) to explicit ?page=N navigation.
  [Guessing on param name] — verify the printed count; if still short,
  check what URL your browser uses when you click to page 2 manually.
"""

import asyncio
import io
import json
import re
import random
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from playwright.async_api import async_playwright


# ─────────────────────────────────────────────
#  SHARED CONSTANTS / HELPERS
# ─────────────────────────────────────────────

AUTOSCOUT_BASE_URL = "https://www.autoscout24.be"
TWEEDEHANDS_BASE_URL = "https://www.2ememain.be"

SELLER_TYPE_MAP = {"d": "Professionnel", "p": "Particulier"}   # [Likely] "p" now inferred from the
# customerType taxonomy (P=Particulier, D=Professionnel) in the NEXT_DATA payload; DOM-code
# fallback only — NEXT_DATA/JSON-LD's own seller.type field is preferred and unambiguous.

FUEL_CODE_MAP = {   # [Certain] — confirmed via NEXT_DATA's own taxonomy.fuelType list
    "2": "Electrique/Essence",
    "3": "Electrique/Diesel",
    "B": "Essence",
    "C": "CNG",
    "D": "Diesel",
    "E": "Electrique",
    "H": "Hydrogène",
    "L": "GPL",
    "M": "Ethanol",
    "O": "Autres",
}

GEONAMES_BE_ZIP_URL = "https://download.geonames.org/export/zip/BE.zip"
POSTAL_CACHE_PATH = Path("be_postal_codes.csv")
HISTORY_PATH = Path("listing_history.json")
RECENT_WINDOW_DAYS = 2

GUID_RE = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def parse_number(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = re.sub(r"[^\d,.]", "", str(raw).strip())
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 1 and len(parts[-1]) == 3:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_power_kw(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"(\d+)\s*kW", raw)
    return float(m.group(1)) if m else None


def parse_power_hp(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"(\d+)\s*Ch", raw)
    return float(m.group(1)) if m else None


def has_sport_trim(text: str | None) -> bool:
    if not text:
        return False
    return bool(re.search(r"\bsport\b", text, re.IGNORECASE))


def map_seller_type(code: str | None) -> str | None:
    if not code:
        return None
    return SELLER_TYPE_MAP.get(code.lower(), f"Inconnu ({code})")


def map_fuel_code(code: str | None) -> str | None:
    if not code:
        return None
    return FUEL_CODE_MAP.get(code.upper(), f"Inconnu ({code})")


def load_belgian_postal_codes() -> dict:
    if not POSTAL_CACHE_PATH.exists():
        print("📮 Downloading Belgian postal code coordinates (GeoNames, one-time)...")
        try:
            with urllib.request.urlopen(GEONAMES_BE_ZIP_URL, timeout=20) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                with zf.open("BE.txt") as f:
                    raw = f.read().decode("utf-8")
            POSTAL_CACHE_PATH.write_text(raw, encoding="utf-8")
        except Exception as exc:
            print(f"  ⚠ Could not download postal code data ({exc}); map will be empty.")
            return {}
    lookup = {}
    with open(POSTAL_CACHE_PATH, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 11:
                continue
            postal_code, place_name = parts[1], parts[2]
            try:
                lat, lon = float(parts[9]), float(parts[10])
            except ValueError:
                continue
            if postal_code not in lookup:
                lookup[postal_code] = {"lat": lat, "lon": lon, "place": place_name}
    return lookup


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_history(history: dict) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps(history), encoding="utf-8")
    except Exception as exc:
        print(f"  ⚠ Could not save listing history ({exc}) — recency tracking won't persist.")


# ─────────────────────────────────────────────
#  AUTOSCOUT24 SCRAPER
# ─────────────────────────────────────────────

def build_autoscout_url(filters: dict, page: int) -> str:
    make  = filters.get("make",  "toyota").lower()
    model = filters.get("model", "corolla").lower()
    params = {}
    if filters.get("price_min"):  params["pricefrom"] = filters["price_min"]
    if filters.get("price_max"):  params["priceto"]   = filters["price_max"]
    if filters.get("km_min"):     params["kmfrom"]    = filters["km_min"]
    if filters.get("km_max"):     params["kmto"]      = filters["km_max"]
    if filters.get("year_min"):   params["fregfrom"]  = filters["year_min"]
    if filters.get("year_max"):   params["fregto"]    = filters["year_max"]
    if filters.get("adage"):      params["adage"]     = filters["adage"]
    # [Likely, not independently verified] "adage" = days since listed,
    # inferred from the taxonomy.onlineSince block (1-6, 7, 14) matching
    # your captured URL exactly. Per your own testing, it's inclusive
    # (adage=3 returns 1+2+3 day-old listings together).
    params["sort"] = "standard"
    params["desc"] = "0"
    params["ustate"] = "N,U"
    params["cy"] = "B"
    params["page"] = page
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{AUTOSCOUT_BASE_URL}/fr/lst/{make}/{model}?{qs}"


def detect_body_type_heuristic(title: str | None) -> str:
    if not title:
        return "Non précisé"
    t = title.lower()
    if re.search(r"\b(touring sports|touring|break|sw)\b", t):
        return "Break / Touring (détecté)"
    if re.search(r"\bts\b", t):
        return "Break / Touring (probable, 'TS')"
    return "Berline / 5 portes (non détecté comme break)"


async def extract_nextdata_listings(page) -> dict:
    """
    {guid: {...}} parsed from Next.js's <script id="__NEXT_DATA__"> payload.
    [Certain, confirmed by real captured HTML] — richer than the JSON-LD
    block: also gives real body type (vehicle.variant, not a title guess),
    leads range, zip/city, first registration, and new/used car status
    (vehicle.offerType), so DOM scraping below becomes a genuine fallback
    rather than the norm. Does NOT include the "Nouveau" recency badge —
    that's rendered client-side only and stays DOM-sourced, see below.
    """
    result = {}
    script = await page.query_selector("script#__NEXT_DATA__")
    if not script:
        return result
    try:
        raw = await script.text_content()
        data = json.loads(raw)
    except Exception:
        return result

    listings = (((data.get("props") or {}).get("pageProps") or {}).get("listings")) or []
    for item in listings:
        guid = item.get("id")
        if not guid:
            continue
        vehicle = item.get("vehicle") or {}
        price = item.get("price") or {}
        location = item.get("location") or {}
        seller = item.get("seller") or {}
        tracking = item.get("tracking") or {}
        statistics = item.get("statistics") or {}
        url = item.get("url")

        seller_type_raw2 = seller.get("type")   # "Dealer" / "PrivateSeller"
        seller_type = ("Professionnel" if seller_type_raw2 == "Dealer"
                        else "Particulier" if seller_type_raw2 == "PrivateSeller" else None)

        offer_type = vehicle.get("offerType")    # "N" new car / "U" used car
        condition = "Neuf" if offer_type == "N" else ("Occasion" if offer_type == "U" else None)

        title_bits = [vehicle.get("make"), vehicle.get("model"), vehicle.get("modelVersionInput")]
        title = " ".join(b for b in title_bits if b).strip() or None

        result[guid] = {
            "url":               (AUTOSCOUT_BASE_URL + url) if url and url.startswith("/") else url,
            "title":             title,
            "km":                parse_number(tracking.get("mileage")),
            "fuel":              vehicle.get("fuel"),
            "fuel_code":         tracking.get("fuelType"),
            "gear":              vehicle.get("transmission"),
            "body_type":         vehicle.get("variant"),   # e.g. "Touring Sports", "Hatchback 5-Door"
            "price":             price.get("priceRaw"),
            "price_label":       tracking.get("priceLabel"),
            "seller_type":       seller_type,
            "seller_name":       seller.get("companyName"),
            "zip_code":          location.get("zip"),
            "city":              location.get("city"),
            "first_registration": tracking.get("firstRegistration"),
            "condition":         condition,
            "leads_range":       statistics.get("leadsRange"),
        }
    return result


async def extract_jsonld_listings(page) -> dict:
    """
    {guid: {...}} parsed from the page's embedded schema.org JSON-LD
    ItemList. [Certain] this block exists — confirmed from real captured
    HTML. Falls back to an empty dict (triggering full DOM extraction
    for every listing) if the block is absent or malformed, so this
    degrades safely rather than crashing the scrape.
    """
    result = {}
    scripts = await page.query_selector_all("script[type='application/ld+json']")
    for script in scripts:
        try:
            raw = await script.text_content()
            data = json.loads(raw)
        except Exception:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            main_entity = node.get("mainEntity") or {}
            items = main_entity.get("itemListElement", [])
            for it in items:
                url = it.get("url")
                if not url:
                    continue
                m = GUID_RE.search(url)
                if not m:
                    continue
                guid = m.group(1)
                item = it.get("item", {}) or {}
                offers = item.get("offers", {}) or {}
                seller = offers.get("seller", {}) or {}
                seller_type = None
                if seller.get("@type") == "AutoDealer":
                    seller_type = "Professionnel"
                elif seller.get("@type") == "Person":
                    seller_type = "Particulier"
                result[guid] = {
                    "url": (AUTOSCOUT_BASE_URL + url) if url.startswith("/") else url,
                    "title": item.get("name"),
                    "km": (item.get("mileageFromOdometer") or {}).get("value"),
                    "fuel": item.get("fuelType"),
                    "gear": item.get("vehicleTransmission"),
                    "condition_raw": item.get("itemCondition"),
                    "price": offers.get("price"),
                    "seller_type": seller_type,
                    "seller_name": seller.get("name"),
                    "seller_location": (seller.get("address") or {}).get("addressLocality"),
                }
    return result


async def scrape_autoscout_page(page, url: str) -> list[dict]:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(random.randint(3000, 5500))

    # Source priority: __NEXT_DATA__ (richest, confirmed real) > JSON-LD
    # (confirmed real, narrower) > DOM attributes/pills (always-available
    # fallback). All three are merged per-field, not all-or-nothing per
    # listing, so a listing missing from one source can still pick up
    # individual fields from another.
    nextdata_map = await extract_nextdata_listings(page)
    jsonld_map = await extract_jsonld_listings(page)

    articles = await page.query_selector_all("article[data-guid]")
    if not articles:
        articles = await page.query_selector_all("article")

    rows = []
    n_from_nextdata = 0
    for art in articles:
        try:
            guid = await art.get_attribute("data-guid") or ""
            nd = nextdata_map.get(guid) or {}
            jl = jsonld_map.get(guid) or {}

            first_reg     = await art.get_attribute("data-first-registration")
            make_attr     = await art.get_attribute("data-make")
            model_attr    = await art.get_attribute("data-model")
            zip_code      = await art.get_attribute("data-listing-zip-code")
            country       = await art.get_attribute("data-listing-country")
            fuel_code_dom = await art.get_attribute("data-fuel-type")
            price_label_dom = await art.get_attribute("data-price-label")
            seller_type_r = await art.get_attribute("data-seller-type")
            leads_range_dom = await art.get_attribute("data-leads-range")

            # DOM fallback fields (used only when neither structured source has them)
            title_short_el = await art.query_selector("[class*='ListItemTitle_title__']")
            title_sub_el   = await art.query_selector("[class*='ListItemTitle_subtitle__']")
            title_short = (await title_short_el.text_content() or "").strip() if title_short_el else ""
            title_sub   = (await title_sub_el.text_content() or "").strip() if title_sub_el else ""
            title_dom = (f"{title_short} {title_sub}".strip()) or None

            pills = {}
            pill_els = await art.query_selector_all("[class*='ListItemPill_pill__'][data-testid]")
            for p in pill_els:
                testid = await p.get_attribute("data-testid")
                text_el = await p.query_selector("[class*='ListItemPill_text__']")
                text = ((await text_el.text_content() or "").strip() if text_el
                        else (await p.text_content() or "").strip())
                if testid:
                    pills[testid] = text

            def pill(*keywords):
                for k, v in pills.items():
                    if any(kw in k for kw in keywords):
                        return v
                return None

            date_text = pill("calendar")

            # ── "Nouveau" badge — AD RECENCY, kept fully separate from
            # car condition (new-car vs used-car). Per your info this
            # means the ad itself was added within the last 24h. This is
            # a highlight pill with NO data-testid, so it's not part of
            # the `pills` dict above — queried directly here, always,
            # regardless of which source matched everything else.
            badge_el = await art.query_selector("[class*='ListItemPill_pill--highlight__']")
            badge_text = (await badge_el.text_content() or "").strip() if badge_el else None
            is_newly_listed = bool(badge_text and "nouveau" in badge_text.lower())

            price_el_dom = await art.query_selector("[data-testid='regular-price']")
            price_dom = parse_number((await price_el_dom.text_content()) if price_el_dom else None)
            km_dom = parse_number(pill("mileage"))
            link_el_dom = await art.query_selector("[class*='ListItemTitle_anchor__'][href]")
            href_dom = await link_el_dom.get_attribute("href") if link_el_dom else None
            url_dom = (AUTOSCOUT_BASE_URL + href_dom) if href_dom and href_dom.startswith("/") else href_dom

            # ── Merge: NEXT_DATA > JSON-LD > DOM, per field ─────────────
            title = nd.get("title") or jl.get("title") or title_dom
            price = nd.get("price") or jl.get("price") or price_dom
            km    = nd.get("km") or jl.get("km") or km_dom
            fuel  = nd.get("fuel") or jl.get("fuel") or pill("gas_pump", "fuel")
            gear  = nd.get("gear") or jl.get("gear") or pill("gearbox", "transmission")
            listing_url = nd.get("url") or jl.get("url") or url_dom
            seller_type = nd.get("seller_type") or jl.get("seller_type") or map_seller_type(seller_type_r)
            seller_name = nd.get("seller_name") or jl.get("seller_name")
            price_label = nd.get("price_label") or price_label_dom
            fuel_code   = nd.get("fuel_code") or fuel_code_dom
            leads_range = nd.get("leads_range") or leads_range_dom
            zip_code    = nd.get("zip_code") or zip_code
            city        = nd.get("city")
            first_reg   = nd.get("first_registration") or first_reg

            condition = nd.get("condition")   # new/used CAR, from vehicle.offerType — [Certain]
            if condition is None:
                condition_raw = jl.get("condition_raw") or ""
                if "NewCondition" in condition_raw:
                    condition = "Neuf"
                elif "UsedCondition" in condition_raw:
                    condition = "Occasion"

            body_type = nd.get("body_type")   # real, e.g. "Touring Sports" — [Certain] when present
            if body_type:
                body_type_confidence = "direct"
            else:
                body_type = detect_body_type_heuristic(title)
                body_type_confidence = "heuristic"

            if nd:
                n_from_nextdata += 1
            if price is None and not title:
                continue

            rows.append({
                "source":               "autoscout24",
                "guid":                 guid,
                "title":                title,
                "price":                price,
                "price_label":          price_label,
                "km":                   km,
                "first_registration":   first_reg,
                "year_raw":             date_text or first_reg,
                "fuel_code":            fuel_code,
                "fuel_code_label":      map_fuel_code(fuel_code),
                "fuel":                 fuel,
                "gear":                 gear,
                "condition":            condition,
                "seller_type_raw":      seller_type_r,
                "seller_type":          seller_type,
                "leads_range":          leads_range,
                "zip_code":             zip_code,
                "is_sport":             has_sport_trim(title),
                "body_type":            body_type,
                "body_type_confidence": body_type_confidence,
                "make":                 make_attr,
                "model":                model_attr,
                "seller_name":          seller_name,
                "location":             city or (f"{country.upper()}-{zip_code}" if country and zip_code else None),
                "url":                  listing_url,
                # ── Recency: real site data now, not scraper-tracked ──
                "is_recent":            is_newly_listed,
                "recency_label":        "Nouveau (<1 jour)" if is_newly_listed else "Non signalé comme récent",
                "recency_bucket":       "Nouveau (<1 jour)" if is_newly_listed else "Non signalé comme récent",
                "recency_reliable":     True,
                "data_source_detail":   "nextdata" if nd else ("jsonld" if jl else "dom_fallback"),
                "scraped_at":           datetime.now().isoformat(),
            })
        except Exception as exc:
            print(f"  ⚠ Listing parse error: {exc}")
            continue

    print(f"    ↳ {n_from_nextdata}/{len(articles)} listings matched via __NEXT_DATA__ "
          f"(richest source; rest used JSON-LD or DOM fallback)")
    return rows


async def scrape_autoscout(filters: dict, max_pages: int = 8) -> pd.DataFrame:
    print(f"\n🚗 Scraping AutoScout24.be — {filters.get('make','Toyota')} "
          f"{filters.get('model','Corolla')} (up to {max_pages} pages)\n")

    all_rows: list[dict] = []
    seen_guids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900}, locale="fr-BE",
        )
        page = await ctx.new_page()
        await page.goto(AUTOSCOUT_BASE_URL + "/fr", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)
        for selector in [
            "button#didomi-notice-agree-button", "button[data-testid='didomi-accept-all']",
            "#onetrust-accept-btn-handler", "button:has-text('Tout accepter')",
            "button:has-text('Accept all')", "button:has-text('Alles accepteren')",
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    print("✓ Cookie banner dismissed")
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        for page_num in range(1, max_pages + 1):
            url = build_autoscout_url(filters, page_num)
            print(f"  Page {page_num}: {url}")
            try:
                rows = await scrape_autoscout_page(page, url)
            except Exception as exc:
                print(f"  ✗ Page {page_num} failed: {exc}")
                await page.screenshot(path=f"debug_autoscout_p{page_num}.png")
                break

            new_rows = [r for r in rows if r["guid"] not in seen_guids]
            for r in new_rows:
                seen_guids.add(r["guid"])
            all_rows.extend(new_rows)
            print(f"    → {len(new_rows)} new listings")

            if len(rows) == 0:
                print("  ✓ No more listings — stopping.")
                break
            await asyncio.sleep(random.uniform(1.5, 3.0))

        await browser.close()

    if not all_rows:
        print("\n⚠  No AutoScout24 listings collected.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["km"]    = pd.to_numeric(df["km"], errors="coerce")
    df["year"] = df["year_raw"].astype("string").str.extract(r"(\d{4})").astype("Int64")

    postal_lookup = load_belgian_postal_codes()
    df["zip_code"] = df["zip_code"].astype("string")
    df["lat"]        = df["zip_code"].map(lambda z: postal_lookup.get(z, {}).get("lat"))
    df["lon"]        = df["zip_code"].map(lambda z: postal_lookup.get(z, {}).get("lon"))
    df["place_name"] = df["zip_code"].map(lambda z: postal_lookup.get(z, {}).get("place"))

    df = df.dropna(subset=["price"])
    df = df[df["price"] > 500]

    # Recency is now set per-row in scrape_autoscout_page() from the real
    # "Nouveau" badge (is_recent / recency_label / recency_bucket /
    # recency_reliable are already populated — no history-file guessing
    # needed anymore). If you also want the "En ligne depuis" server-side
    # filter (adage=N), pass adage in `filters` before calling this.

    n_nextdata = int((df["data_source_detail"] == "nextdata").sum())
    n_jsonld   = int((df["data_source_detail"] == "jsonld").sum())
    n_dom      = len(df) - n_nextdata - n_jsonld
    n_new_badge = int(df["is_recent"].sum())
    print(f"\n✅ {len(df)} clean AutoScout24 listings. "
          f"{n_nextdata} from __NEXT_DATA__, {n_jsonld} from JSON-LD, {n_dom} DOM-only fallback.")
    print(f"ℹ {n_new_badge} listing(s) carry the 'Nouveau' badge (<1 day old, per your info).\n")

    return df


# ─────────────────────────────────────────────
#  2EMEMAIN.BE SCRAPER  — real selectors from captured markup
# ─────────────────────────────────────────────

ICON_FIELD_MAP_2M = {
    "hz-SvgIconCarConstructionYear": "year",
    "hz-SvgIconCarMileage": "km",
    "hz-SvgIconCarFuel": "fuel",
    "hz-SvgIconCarTransmission": "gear",
    "hz-SvgIconCarBody": "body_type",
}


def build_2ememain_url(page: int = 1, construction_year_from: int | None = None) -> str:
    """
    [Testing a hypothesis, not yet confirmed] Switched from a ?page=N query
    param (confirmed NOT to work — caused the hydration-reset stall seen in
    the last run) to a /p/N/ path segment, which matches this site's own
    convention of using path segments for filters (e.g. /f/corolla/1230/).
    The stall-detector below will confirm or refute this on the next run.
    """
    base = f"{TWEEDEHANDS_BASE_URL}/l/autos/toyota/f/corolla/1230/"
    path_suffix = f"p/{page}/" if page > 1 else ""
    frag = "#q:toyota+corolla|Language:all-languages"
    if construction_year_from:
        frag += f"|constructionYearFrom:{construction_year_from}"
    return f"{base}{path_suffix}{frag}"


async def parse_2ememain_card(a_handle) -> dict | None:
    href = await a_handle.get_attribute("href")
    if not href:
        return None
    id_m = re.search(r"/m(\d+)-", href)
    listing_id = id_m.group(1) if id_m else href
    url = href if href.startswith("http") else TWEEDEHANDS_BASE_URL + href

    title_el = await a_handle.query_selector("[class*='ListingListViewContentCars_title__']")
    title = (await title_el.text_content() or "").strip() if title_el else None

    price_el = await a_handle.query_selector("h4[class*='hz-Title']")
    price_raw = (await price_el.text_content() or "").strip() if price_el else None
    price = parse_number(price_raw)

    fields = {"year": None, "km": None, "fuel": None, "gear": None, "body_type": None}
    attr_spans = await a_handle.query_selector_all(
        "[class*='ListingListViewContentCars_attributes__'] .hz-Attribute"
    )
    for span in attr_spans:
        icon_el = await span.query_selector("i")
        icon_class = (await icon_el.get_attribute("class") or "") if icon_el else ""
        field = next((f for cls, f in ICON_FIELD_MAP_2M.items() if cls in icon_class), None)
        if not field:
            continue
        fields[field] = (await span.text_content() or "").strip()

    seller_type = None
    img_el = await a_handle.query_selector("img[title]")
    if img_el:
        img_title = await img_el.get_attribute("title") or ""
        if "Particulier" in img_title:
            seller_type = "Particulier"
        elif "Entreprise" in img_title:
            seller_type = "Professionnel"

    location, posted_text = None, None
    try:
        container = await a_handle.evaluate_handle("el => el.closest('li') || el.parentElement")
        loc_el = await container.query_selector("[class*='ListingListViewContentCars_sellerLocation__']")
        location = (await loc_el.text_content() or "").strip() if loc_el else None
        date_el = await container.query_selector("[class*='ListingListViewContentCars_listingDate__']")
        posted_text = (await date_el.text_content() or "").strip() if date_el else None
    except Exception:
        pass

    year = None
    if fields["year"]:
        ym = re.search(r"\d{4}", fields["year"])
        year = int(ym.group()) if ym else None
    km = parse_number(fields["km"]) if fields["km"] else None

    # Explicit rule: don't consider listings missing year or mileage.
    if year is None or km is None:
        return None
    if price is None:
        return None

    is_recent = posted_text in ("Aujourd'hui", "Aujourd\u2019hui", "Hier")

    return {
        "source":               "2ememain",
        "listing_id":           listing_id,
        "title":                title,
        "price":                price,
        "km":                   km,
        "year":                 year,
        "fuel":                 fields["fuel"],
        "gear":                 fields["gear"],
        "body_type":            fields["body_type"],
        "body_type_confidence": "direct" if fields["body_type"] else "unknown",
        "seller_type":          seller_type,
        "is_sport":             has_sport_trim(title),
        "location":             location,
        "url":                  url,
        "posted_text":          posted_text,
        "is_recent":            is_recent,
        "recency_label":        posted_text or "Plus ancien / non précisé",
        "recency_bucket":       posted_text or "Plus ancien / non précisé",
        "recency_reliable":     True,   # 2ememain's date comes from the site itself, not inferred
        "scraped_at":           datetime.now().isoformat(),
    }


async def extract_total_results(page) -> int | None:
    """Parses the site's own 'X résultats' breadcrumb text so we can report
    collected-vs-total honestly instead of leaving it to guesswork."""
    try:
        text = await page.inner_text("body")
        m = re.search(r"([\d.,]+)\s*résultats?", text)
        if m:
            n = parse_number(m.group(1))
            return int(n) if n else None
    except Exception:
        pass
    return None


async def scrape_2ememain(construction_year_from: int = 2019, max_pages: int = 25) -> pd.DataFrame:
    print(f"\n🚗 Scraping 2ememain.be — Toyota Corolla, from {construction_year_from}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900}, locale="fr-BE",
        )
        page = await ctx.new_page()

        rows = []
        seen_ids = set()
        n_dropped_incomplete = 0
        total_results = None
        prev_first_id = None
        stalled_pages = 0

        for page_num in range(1, max_pages + 1):
            url = build_2ememain_url(page_num, construction_year_from)
            print(f"  Page {page_num}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if page_num == 1:
                for selector in ["button#didomi-notice-agree-button", "#onetrust-accept-btn-handler",
                                 "button:has-text('Tout accepter')", "button:has-text('Accepteren')"]:
                    try:
                        el = await page.query_selector(selector)
                        if el:
                            await el.click()
                            print("✓ Cookie banner dismissed")
                            await page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass
                total_results = await extract_total_results(page)
                if total_results:
                    print(f"  ℹ Site reports {total_results} total results for this search.")

            # Poll for the listing grid to actually reflect this page rather
            # than trusting a fixed timeout — guards against the hydration-
            # reset scenario (client-side JS silently reverting to page 1's
            # data after navigation).
            first_id_this_page = None
            for _ in range(6):
                await page.wait_for_timeout(700)
                anchors_probe = await page.query_selector_all("a[href*='/v/autos/']")
                if anchors_probe:
                    href0 = await anchors_probe[0].get_attribute("href")
                    id0_m = re.search(r"/m(\d+)-", href0 or "")
                    first_id_this_page = id0_m.group(1) if id0_m else href0
                    break

            if page_num > 1 and first_id_this_page == prev_first_id:
                stalled_pages += 1
                print(f"    ⚠ First listing on this page is IDENTICAL to the previous page "
                      f"({first_id_this_page}). This looks like the hydration-reset issue, "
                      f"not end-of-results — page param isn't sticking client-side.")
                if stalled_pages >= 2:
                    print("  ✗ Stalled twice in a row — stopping. This confirms the pagination "
                          "mechanism needs a different approach (e.g. clicking an in-page "
                          "'next' control instead of a fresh URL per page). Tell me and I'll "
                          "rework it rather than guess a third time.")
                    break
            else:
                stalled_pages = 0
            prev_first_id = first_id_this_page

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

            anchors = await page.query_selector_all("a[href*='/v/autos/']")
            new_ids_this_page = 0
            for a in anchors:
                try:
                    href = await a.get_attribute("href")
                    id_m = re.search(r"/m(\d+)-", href or "")
                    listing_id = id_m.group(1) if id_m else href
                    if not listing_id or listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)

                    row = await parse_2ememain_card(a)
                    if row is None:
                        n_dropped_incomplete += 1
                        continue
                    rows.append(row)
                    new_ids_this_page += 1
                except Exception as exc:
                    print(f"  ⚠ 2ememain listing parse error: {exc}")
                    continue

            print(f"    → {new_ids_this_page} new usable listings ({len(anchors)} links seen on page, "
                  f"{len(seen_ids)} unique so far)")

            if new_ids_this_page == 0 and stalled_pages == 0:
                print("  ✓ No new listings and no stall detected — genuine end of results.")
                break
            await asyncio.sleep(random.uniform(1.0, 2.0))

        await browser.close()

    if not rows:
        print("\n⚠  No usable 2ememain listings collected.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df[df["price"] > 500]
    coverage = f" out of ~{total_results} the site reports" if total_results else ""
    print(f"\n✅ {len(df)} usable 2ememain listings collected{coverage} "
          f"({n_dropped_incomplete} dropped for missing year/mileage/price, as instructed).\n")
    return df


# ─────────────────────────────────────────────
#  MERGE
# ─────────────────────────────────────────────

def merge_sources(df_autoscout: pd.DataFrame, df_2ememain: pd.DataFrame) -> pd.DataFrame:
    frames = [d for d in (df_autoscout, df_2ememain) if d is not None and not d.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    if "is_recent" not in merged.columns:
        merged["is_recent"] = False
    merged["is_recent"] = merged["is_recent"].fillna(False)
    if "recency_reliable" not in merged.columns:
        merged["recency_reliable"] = False
    merged["recency_reliable"] = merged["recency_reliable"].fillna(False)
    return merged

# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────

PALETTE = {
    "bg":       "#0f1117",
    "card":     "#1a1d27",
    "accent":   "#e8272e",
    "accent2":  "#ff6b6b",
    "muted":    "#8b8fa8",
    "text":     "#e8eaf0",
    "gridline": "#2a2d3a",
}


def summary_stats(series: pd.Series) -> dict:
    s = series.dropna()
    return {
        "count":  int(s.count()),
        "mean":   s.mean() if s.count() else 0,
        "median": s.median() if s.count() else 0,
        "std":    s.std() if s.count() else 0,
        "min":    s.min() if s.count() else 0,
        "max":    s.max() if s.count() else 0,
        "q25":    s.quantile(0.25) if s.count() else 0,
        "q75":    s.quantile(0.75) if s.count() else 0,
    }


def build_dashboard(df: pd.DataFrame) -> str:
    if df.empty:
        return "<h1>No data to display.</h1>"

    p_stats  = summary_stats(df["price"])
    km_stats = summary_stats(df["km"])
    n_recent = int(df["is_recent"].sum()) if "is_recent" in df.columns else 0
    n_autoscout = int((df["source"] == "autoscout24").sum())
    n_2ememain  = int((df["source"] == "2ememain").sum())

    cols = [
        "price", "km", "year", "fuel", "gear", "title", "url", "location",
        "seller_type", "price_label", "fuel_code", "fuel_code_label", "leads_range",
        "is_sport", "body_type", "body_type_confidence", "zip_code", "lat", "lon",
        "place_name", "source", "is_recent", "recency_label", "recency_bucket",
        "recency_reliable", "condition",
    ]
    export = df[[c for c in cols if c in df.columns]].copy()
    export = export.rename(columns={"zip_code": "zip", "place_name": "place"})
    export = export.where(pd.notnull(export), None)
    records_json = export.to_json(orient="records")

    def hist_fig(series, color, unit=""):
        stats = summary_stats(series)
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=series.dropna(), nbinsx=40, marker_color=color, opacity=0.85,
            hovertemplate=f"%{{x:,.0f}}{unit}<br>Count: %{{y}}<extra></extra>",
        ))
        for val, name, dash in [(stats["mean"], "Mean", "dash"), (stats["median"], "Median", "dot")]:
            fig.add_vline(x=val, line_dash=dash, line_color=PALETTE["text"], line_width=1.5,
                          annotation_text=f"{name}: {val:,.0f}{unit}", annotation_position="top right",
                          annotation_font_color=PALETTE["text"], annotation_font_size=11)
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color=PALETTE["text"], margin=dict(l=40, r=20, t=10, b=40),
                          showlegend=False, xaxis=dict(gridcolor=PALETTE["gridline"], tickformat=",.0f"),
                          yaxis=dict(gridcolor=PALETTE["gridline"]))
        return fig.to_json()

    def scatter_fig(df_):
        fig = go.Figure()
        year_vals = df_["year"].dropna().astype(int)
        if year_vals.empty:
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              font_color=PALETTE["text"],
                              annotations=[dict(text="No year data available", showarrow=False,
                                                font=dict(color=PALETTE["muted"], size=13))])
            return fig.to_json()
        for yr in range(int(year_vals.min()), int(year_vals.max()) + 1):
            sub = df_[df_["year"] == yr]
            if sub.empty:
                continue
            fig.add_trace(go.Scatter(
                x=sub["km"], y=sub["price"], mode="markers", name=str(yr),
                marker=dict(size=7, opacity=0.7),
                hovertemplate="<b>%{text}</b><br>Price: €%{y:,.0f}<br>KM: %{x:,.0f}<extra></extra>",
                text=sub["title"].fillna(""),
            ))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color=PALETTE["text"], margin=dict(l=50, r=20, t=10, b=40),
                          legend=dict(title="Year", bgcolor="rgba(0,0,0,0)", font_color=PALETTE["text"]),
                          xaxis=dict(title="Mileage (km)", gridcolor=PALETTE["gridline"], tickformat=",.0f"),
                          yaxis=dict(title="Price (€)", gridcolor=PALETTE["gridline"], tickformat=",.0f"))
        return fig.to_json()

    def box_fig(df_):
        fig = go.Figure()
        for yr in sorted(df_["year"].dropna().unique()):
            sub = df_[df_["year"] == yr]["price"].dropna()
            if len(sub) < 3:
                continue
            fig.add_trace(go.Box(y=sub, name=str(int(yr)), marker_color=PALETTE["accent"],
                                 line_color=PALETTE["accent2"], fillcolor="rgba(232,39,46,0.15)", boxmean=True))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color=PALETTE["text"], margin=dict(l=50, r=20, t=10, b=40), showlegend=False,
                          yaxis=dict(title="Price (€)", gridcolor=PALETTE["gridline"], tickformat=",.0f"),
                          xaxis=dict(title="Year", gridcolor=PALETTE["gridline"]))
        return fig.to_json()

    def bar_fig(series, color):
        counts = series.value_counts().head(8)
        fig = go.Figure(go.Bar(x=counts.index.tolist(), y=counts.values.tolist(),
                               marker_color=color, opacity=0.85,
                               hovertemplate="%{x}<br>Count: %{y}<extra></extra>"))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color=PALETTE["text"], margin=dict(l=40, r=20, t=10, b=60),
                          showlegend=False, xaxis=dict(gridcolor=PALETTE["gridline"]),
                          yaxis=dict(gridcolor=PALETTE["gridline"]))
        return fig.to_json()

    def map_fig(df_all):
        # Explicit, unconditional restriction to AutoScout24 — 2ememain
        # only gives commune names, not postal codes, so it has no lat/lon
        # to plot; excluding it here (not just relying on NaN elsewhere)
        # is the fix you asked for.
        df_ = df_all[df_all["source"] == "autoscout24"]
        fig = go.Figure()
        if "lat" not in df_.columns:
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              font_color=PALETTE["text"])
            return fig.to_json()
        agg = (df_.dropna(subset=["lat", "lon"]).groupby("zip_code")
               .agg(count=("price", "size"), avg_price=("price", "mean"),
                    lat=("lat", "first"), lon=("lon", "first"), place=("place_name", "first"))
               .reset_index())
        if agg.empty:
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              font_color=PALETTE["text"],
                              annotations=[dict(text="No postal-code data available (AutoScout24 only)",
                                                showarrow=False, font=dict(color=PALETTE["muted"], size=13))])
            return fig.to_json()
        max_count = max(agg["count"].max(), 1)
        fig.add_trace(go.Scattermapbox(
            lat=agg["lat"], lon=agg["lon"], mode="markers",
            marker=dict(size=(agg["count"] / max_count * 30 + 7), color=agg["avg_price"],
                       colorscale=[[0, "#4a90d9"], [0.5, PALETTE["accent2"]], [1, PALETTE["accent"]]],
                       showscale=True,
                       colorbar=dict(title="Avg €", tickfont=dict(color=PALETTE["text"]),
                                    title_font=dict(color=PALETTE["text"])), opacity=0.85),
            text=[f"{p} ({z})<br>{c} listing(s)<br>Avg €{ap:,.0f}"
                  for p, z, c, ap in zip(agg["place"], agg["zip_code"], agg["count"], agg["avg_price"])],
            hoverinfo="text",
        ))
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          mapbox=dict(style="open-street-map", center=dict(lat=50.6, lon=4.6), zoom=6.6),
                          margin=dict(l=0, r=0, t=0, b=0), showlegend=False)
        return fig.to_json()

    price_hist_json = hist_fig(df["price"], PALETTE["accent"], "€")
    km_hist_json    = hist_fig(df["km"], "#4a90d9", " km")
    scatter_json    = scatter_fig(df)
    box_json        = box_fig(df)
    fuel_bar_json   = bar_fig(df["fuel"].fillna("Unknown"), "#f0a500")
    gear_bar_json   = bar_fig(df["gear"].fillna("Unknown"), "#50c878")
    map_json        = map_fig(df)

    def opts(col):
        return sorted(df[col].dropna().unique().tolist()) if col in df.columns else []

    fuel_options        = opts("fuel")
    gear_options        = opts("gear")
    seller_options      = opts("seller_type")
    price_label_options = opts("price_label")
    fuel_code_options   = opts("fuel_code")
    leads_options       = opts("leads_range")
    body_type_options   = opts("body_type")
    source_options      = opts("source")
    recency_options     = opts("recency_bucket")
    year_min_val = int(df["year"].dropna().min()) if not df["year"].dropna().empty else 2000
    year_max_val = int(df["year"].dropna().max()) if not df["year"].dropna().empty else 2025

    def multiselect_options(values):
        return "".join(f'<option value="{v}">{v}</option>' for v in values)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Toyota Corolla — Multi-source Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Rajdhani:wght@600;700&display=swap');
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{ --bg: {PALETTE["bg"]}; --card: {PALETTE["card"]}; --accent: {PALETTE["accent"]};
          --accent2: {PALETTE["accent2"]}; --muted: {PALETTE["muted"]}; --text: {PALETTE["text"]}; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; min-height: 100vh; padding: 0 0 60px; }}
  .header {{ background: linear-gradient(135deg, #0f1117 0%, #1a0507 60%, #0f1117 100%); border-bottom: 2px solid var(--accent); padding: 28px 40px 22px; display: flex; align-items: center; gap: 20px; }}
  .header-logo {{ width: 48px; height: 48px; background: var(--accent); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-family: 'Rajdhani', sans-serif; font-size: 22px; font-weight: 700; color: white; flex-shrink: 0; }}
  .header-text h1 {{ font-family: 'Rajdhani', sans-serif; font-size: 32px; font-weight: 700; letter-spacing: 0.04em; line-height: 1; }}
  .header-text h1 span {{ color: var(--accent); }}
  .header-text p {{ font-size: 13px; color: var(--muted); margin-top: 4px; letter-spacing: 0.02em; }}
  .header-badge {{ margin-left: auto; background: rgba(232,39,46,0.12); border: 1px solid rgba(232,39,46,0.35); border-radius: 20px; padding: 6px 16px; font-size: 12px; color: var(--accent2); letter-spacing: 0.05em; }}
  .main {{ padding: 28px 40px; }}
  .filter-bar {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 20px 24px; margin-bottom: 28px; display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 20px; align-items: end; }}
  .filter-group label {{ display: block; font-size: 11px; font-weight: 600; letter-spacing: 0.1em; color: var(--muted); text-transform: uppercase; margin-bottom: 8px; }}
  .filter-group .hint {{ font-size: 10px; color: var(--muted); text-transform: none; letter-spacing: normal; display: block; margin-top: 4px; }}
  .range-row {{ display: flex; gap: 8px; align-items: center; }}
  .range-row input {{ width: 100%; padding: 7px 10px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; color: var(--text); font-size: 13px; font-family: 'Inter', sans-serif; }}
  .range-row input:focus {{ outline: none; border-color: var(--accent); }}
  .range-row span {{ color: var(--muted); font-size: 12px; flex-shrink: 0; }}
  .filter-group select {{ width: 100%; padding: 7px 10px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; color: var(--text); font-size: 13px; font-family: 'Inter', sans-serif; }}
  .filter-group select[multiple] {{ appearance: auto; }}
  .filter-group select option {{ background: #1a1d27; padding: 3px 4px; }}
  .checkbox-row {{ display: flex; align-items: center; gap: 8px; padding: 7px 0; }}
  .checkbox-row input {{ width: 16px; height: 16px; accent-color: var(--accent); }}
  .checkbox-row label {{ margin-bottom: 0; font-size: 13px; color: var(--text); text-transform: none; letter-spacing: normal; font-weight: 400; }}
  .btn-reset {{ padding: 9px 20px; background: rgba(232,39,46,0.15); border: 1px solid var(--accent); border-radius: 6px; color: var(--accent); font-size: 13px; font-weight: 600; cursor: pointer; letter-spacing: 0.04em; transition: background 0.2s; }}
  .btn-reset:hover {{ background: rgba(232,39,46,0.28); }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 16px; margin-bottom: 28px; }}
  .kpi {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; padding: 18px 20px; position: relative; overflow: hidden; }}
  .kpi::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: var(--accent); }}
  .kpi .kpi-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 6px; }}
  .kpi .kpi-value {{ font-family: 'Rajdhani', sans-serif; font-size: 26px; font-weight: 700; line-height: 1; color: var(--text); }}
  .kpi .kpi-sub {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .span-2 {{ grid-column: span 2; }}
  .card {{ background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 20px; overflow: hidden; }}
  .card-title {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 14px; }}
  .stats-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .stats-table th {{ text-align: left; color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 0 8px 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }}
  .stats-table td {{ padding: 7px 8px 7px 0; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }}
  .stats-table tr:last-child td {{ border-bottom: none; }}
  .stat-val {{ font-family: 'Rajdhani', sans-serif; font-size: 15px; font-weight: 600; color: var(--text); }}
  #listings-section {{ margin-top: 28px; }}
  .section-title {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); margin-bottom: 14px; }}
  .table-wrap {{ overflow-x: auto; }}
  table.listings {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table.listings thead th {{ text-align: left; padding: 10px 14px; background: rgba(255,255,255,0.04); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; cursor: pointer; user-select: none; border-bottom: 1px solid rgba(255,255,255,0.08); white-space: nowrap; }}
  table.listings thead th:hover {{ color: var(--text); }}
  table.listings tbody tr {{ border-bottom: 1px solid rgba(255,255,255,0.04); transition: background 0.15s; }}
  table.listings tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
  table.listings td {{ padding: 9px 14px; vertical-align: middle; }}
  table.listings td a {{ color: var(--accent2); text-decoration: none; }}
  table.listings td a:hover {{ text-decoration: underline; }}
  .sort-arrow {{ margin-left: 4px; opacity: 0.5; }}
  .src-badge {{ font-size: 10px; padding: 2px 6px; border-radius: 4px; }}
  .src-autoscout24 {{ background: rgba(232,39,46,0.15); color: var(--accent2); }}
  .src-2ememain {{ background: rgba(80,200,120,0.15); color: #50c878; }}
  .footer {{ text-align: center; padding: 24px 40px; color: var(--muted); font-size: 11px; letter-spacing: 0.04em; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 40px; }}
  @media (max-width: 900px) {{
    .main {{ padding: 20px; }}
    .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
    .span-2 {{ grid-column: span 1; }}
    .header {{ padding: 18px 20px; }}
  }}
</style>
</head>
<body>

<header class="header">
  <div class="header-logo">T</div>
  <div class="header-text">
    <h1><span>Toyota</span> Corolla</h1>
    <p>AutoScout24.be + 2ememain.be · Multi-source Market Analysis · Scraped {datetime.now().strftime("%d %b %Y, %H:%M")}</p>
  </div>
  <div class="header-badge" id="listing-badge">{p_stats["count"]} listings ({n_autoscout} AS24 + {n_2ememain} 2M)</div>
</header>

<main class="main">

<div class="filter-bar">
  <div class="filter-group">
    <label>Price (€)</label>
    <div class="range-row">
      <input type="number" id="f-price-min" placeholder="Min" step="500">
      <span>–</span>
      <input type="number" id="f-price-max" placeholder="Max" step="500">
    </div>
  </div>
  <div class="filter-group">
    <label>Mileage (km)</label>
    <div class="range-row">
      <input type="number" id="f-km-min" placeholder="Min" step="5000">
      <span>–</span>
      <input type="number" id="f-km-max" placeholder="Max" step="5000">
    </div>
  </div>
  <div class="filter-group">
    <label>Year</label>
    <div class="range-row">
      <input type="number" id="f-year-min" placeholder="From" value="{year_min_val}" min="1990" max="2030">
      <span>–</span>
      <input type="number" id="f-year-max" placeholder="To"   value="{year_max_val}" min="1990" max="2030">
    </div>
  </div>
  <div class="filter-group">
    <label>Source</label>
    <select id="f-source">
      <option value="">All sources</option>
      {"".join(f'<option value="{s}">{s}</option>' for s in source_options)}
    </select>
  </div>
  <div class="filter-group">
    <label>Fuel <span class="hint">ctrl/cmd-click for multiple</span></label>
    <select id="f-fuel" multiple size="4">{multiselect_options(fuel_options)}</select>
  </div>
  <div class="filter-group">
    <label>Gearbox</label>
    <select id="f-gear">
      <option value="">All gearboxes</option>
      {"".join(f'<option value="{g}">{g}</option>' for g in gear_options)}
    </select>
  </div>
  <div class="filter-group">
    <label>Seller type <span class="hint">multi-select</span></label>
    <select id="f-seller" multiple size="3">{multiselect_options(seller_options)}</select>
  </div>
  <div class="filter-group">
    <label>Price rating (AS24)</label>
    <select id="f-price-label">
      <option value="">All ratings</option>
      {"".join(f'<option value="{p}">{p}</option>' for p in price_label_options)}
    </select>
  </div>
  <div class="filter-group">
    <label>Fuel code raw (AS24)</label>
    <select id="f-fuel-code">
      <option value="">All codes</option>
      {"".join(f'<option value="{fc}">{fc}</option>' for fc in fuel_code_options)}
    </select>
  </div>
  <div class="filter-group">
    <label>Leads range (AS24) <span class="hint">multi-select</span></label>
    <select id="f-leads" multiple size="3">{multiselect_options(leads_options)}</select>
  </div>
  <div class="filter-group">
    <label>Body type <span class="hint">multi-select, not harmonized — 2M is direct, AS24 is a text guess</span></label>
    <select id="f-body" multiple size="4">{multiselect_options(body_type_options)}</select>
  </div>
  <div class="filter-group">
    <label>Recency <span class="hint">AS24 "Nouveau" = &lt;1 day, per site; 2M dates are exact</span></label>
    <select id="f-recency" multiple size="4">{multiselect_options(recency_options)}</select>
  </div>
  <div class="filter-group">
    <label>&nbsp;</label>
    <div class="checkbox-row">
      <input type="checkbox" id="f-sport">
      <label for="f-sport">Sport / GR Sport only</label>
    </div>
  </div>
  <div class="filter-group" style="display:flex;align-items:flex-end;">
    <button class="btn-reset" onclick="resetFilters()">↺ Reset</button>
  </div>
</div>

<div class="kpis" id="kpi-strip">
  <div class="kpi">
    <div class="kpi-label">Listings</div>
    <div class="kpi-value" id="kpi-count">{p_stats["count"]}</div>
    <div class="kpi-sub">after filters</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">New / Recent</div>
    <div class="kpi-value" id="kpi-recent">{n_recent}</div>
    <div class="kpi-sub">"Nouveau" badge or 2M same-day</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Avg Price</div>
    <div class="kpi-value" id="kpi-avg-price">€{p_stats["mean"]:,.0f}</div>
    <div class="kpi-sub">median €<span id="kpi-med-price">{p_stats["median"]:,.0f}</span></div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Avg Mileage</div>
    <div class="kpi-value" id="kpi-avg-km">{km_stats["mean"]:,.0f}</div>
    <div class="kpi-sub">km — median <span id="kpi-med-km">{km_stats["median"]:,.0f}</span></div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Price Range</div>
    <div class="kpi-value" id="kpi-price-range">€{p_stats["min"]:,.0f}</div>
    <div class="kpi-sub">to €<span id="kpi-price-max">{p_stats["max"]:,.0f}</span></div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Std Dev Price</div>
    <div class="kpi-value" id="kpi-std">€{p_stats["std"]:,.0f}</div>
    <div class="kpi-sub">IQR €<span id="kpi-iqr">{p_stats["q25"]:,.0f}–{p_stats["q75"]:,.0f}</span></div>
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title">Price Distribution (€)</div>
    <div id="chart-price-hist" style="height:280px"></div>
  </div>
  <div class="card">
    <div class="card-title">Mileage Distribution (km)</div>
    <div id="chart-km-hist" style="height:280px"></div>
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title">Price vs Mileage (coloured by year)</div>
    <div id="chart-scatter" style="height:320px"></div>
  </div>
  <div class="card" style="display:flex;flex-direction:column;gap:24px;">
    <div>
      <div class="card-title">Price Summary</div>
      <table class="stats-table">
        <thead><tr><th>Metric</th><th>Price (€)</th><th>Mileage (km)</th></tr></thead>
        <tbody id="stats-tbody">
          <tr><td>Count</td><td class="stat-val" id="st-count-p">{p_stats["count"]}</td><td class="stat-val" id="st-count-k">{km_stats["count"]}</td></tr>
          <tr><td>Mean</td><td class="stat-val" id="st-mean-p">{p_stats["mean"]:,.0f}</td><td class="stat-val" id="st-mean-k">{km_stats["mean"]:,.0f}</td></tr>
          <tr><td>Median</td><td class="stat-val" id="st-med-p">{p_stats["median"]:,.0f}</td><td class="stat-val" id="st-med-k">{km_stats["median"]:,.0f}</td></tr>
          <tr><td>Std Dev</td><td class="stat-val" id="st-std-p">{p_stats["std"]:,.0f}</td><td class="stat-val" id="st-std-k">{km_stats["std"]:,.0f}</td></tr>
          <tr><td>Min</td><td class="stat-val" id="st-min-p">{p_stats["min"]:,.0f}</td><td class="stat-val" id="st-min-k">{km_stats["min"]:,.0f}</td></tr>
          <tr><td>Max</td><td class="stat-val" id="st-max-p">{p_stats["max"]:,.0f}</td><td class="stat-val" id="st-max-k">{km_stats["max"]:,.0f}</td></tr>
          <tr><td>Q25</td><td class="stat-val" id="st-q25-p">{p_stats["q25"]:,.0f}</td><td class="stat-val" id="st-q25-k">{km_stats["q25"]:,.0f}</td></tr>
          <tr><td>Q75</td><td class="stat-val" id="st-q75-p">{p_stats["q75"]:,.0f}</td><td class="stat-val" id="st-q75-k">{km_stats["q75"]:,.0f}</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="grid-3">
  <div class="card span-2">
    <div class="card-title">Price Distribution by Year</div>
    <div id="chart-box" style="height:280px"></div>
  </div>
  <div class="card">
    <div class="card-title" style="margin-bottom:8px">Fuel Types</div>
    <div id="chart-fuel" style="height:125px;margin-bottom:12px"></div>
    <div class="card-title" style="margin-bottom:8px">Gearbox</div>
    <div id="chart-gear" style="height:125px"></div>
  </div>
</div>

<div class="card" style="margin-bottom:20px;">
  <div class="card-title">Listings by Area (Belgium, AutoScout24 only — 2ememain excluded, see notes) — bubble size = count, colour = avg price</div>
  <div id="chart-map" style="height:480px"></div>
</div>

<section id="listings-section">
  <div class="section-title">Individual Listings <span style="color:var(--accent);font-size:10px" id="listing-count-label"></span></div>
  <div class="card">
    <div class="table-wrap">
      <table class="listings" id="listings-table">
        <thead>
          <tr>
            <th onclick="sortTable(0)">Title <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable(1)">Price (€) <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable(2)">Mileage (km) <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable(3)">Year <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable(4)">Fuel <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable(5)">Gear <span class="sort-arrow">↕</span></th>
            <th>Body</th>
            <th>Source</th>
            <th>Recency</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody id="listings-tbody"></tbody>
      </table>
    </div>
  </div>
</section>

</main>

<footer class="footer">
  AutoScout24.be + 2ememain.be data · Postal-code coordinates © GeoNames (CC BY 4.0) · Personal/research use only · Generated {datetime.now().strftime("%Y-%m-%d")}
</footer>

<script>
const ALL_DATA = {records_json};
const PRICE_HIST_BASE  = {price_hist_json};
const KM_HIST_BASE     = {km_hist_json};
const SCATTER_BASE     = {scatter_json};
const BOX_BASE         = {box_json};
const FUEL_BASE        = {fuel_bar_json};
const GEAR_BASE        = {gear_bar_json};
const MAP_BASE         = {map_json};

const plotCfg = {{ displayModeBar: true, modeBarButtonsToRemove: ['select2d','lasso2d','autoScale2d'], displaylogo: false, responsive: true }};

Plotly.newPlot('chart-price-hist', PRICE_HIST_BASE.data, PRICE_HIST_BASE.layout, plotCfg);
Plotly.newPlot('chart-km-hist',    KM_HIST_BASE.data,    KM_HIST_BASE.layout,    plotCfg);
Plotly.newPlot('chart-scatter',    SCATTER_BASE.data,    SCATTER_BASE.layout,    plotCfg);
Plotly.newPlot('chart-box',        BOX_BASE.data,        BOX_BASE.layout,        plotCfg);
Plotly.newPlot('chart-fuel',       FUEL_BASE.data,       FUEL_BASE.layout,       plotCfg);
Plotly.newPlot('chart-gear',       GEAR_BASE.data,       GEAR_BASE.layout,       plotCfg);
Plotly.newPlot('chart-map',        MAP_BASE.data,        MAP_BASE.layout,       plotCfg);

renderTable(ALL_DATA);

function selectedValues(id) {{
  return Array.from(document.getElementById(id).selectedOptions).map(o => o.value);
}}

function getFilters() {{
  return {{
    priceMin: parseFloat(document.getElementById('f-price-min').value) || null,
    priceMax: parseFloat(document.getElementById('f-price-max').value) || null,
    kmMin:    parseFloat(document.getElementById('f-km-min').value)    || null,
    kmMax:    parseFloat(document.getElementById('f-km-max').value)    || null,
    yearMin:  parseInt(document.getElementById('f-year-min').value)    || null,
    yearMax:  parseInt(document.getElementById('f-year-max').value)    || null,
    source:   document.getElementById('f-source').value                || null,
    fuel:     selectedValues('f-fuel'),
    gear:     document.getElementById('f-gear').value                  || null,
    seller:   selectedValues('f-seller'),
    priceLabel: document.getElementById('f-price-label').value         || null,
    fuelCode: document.getElementById('f-fuel-code').value             || null,
    leads:    selectedValues('f-leads'),
    body:     selectedValues('f-body'),
    recency:  selectedValues('f-recency'),
    sportOnly: document.getElementById('f-sport').checked,
  }};
}}

function applyFilters(data, f) {{
  return data.filter(d => {{
    if (f.priceMin  !== null && (d.price === null || d.price  < f.priceMin))  return false;
    if (f.priceMax  !== null && (d.price === null || d.price  > f.priceMax))  return false;
    if (f.kmMin     !== null && (d.km    === null || d.km     < f.kmMin))     return false;
    if (f.kmMax     !== null && (d.km    === null || d.km     > f.kmMax))     return false;
    if (f.yearMin   !== null && (d.year  === null || d.year   < f.yearMin))   return false;
    if (f.yearMax   !== null && (d.year  === null || d.year   > f.yearMax))   return false;
    if (f.source    !== null && d.source !== f.source)                       return false;
    if (f.fuel.length && !f.fuel.includes(d.fuel))                           return false;
    if (f.gear      !== null && d.gear !== f.gear)                            return false;
    if (f.seller.length && !f.seller.includes(d.seller_type))                return false;
    if (f.priceLabel!== null && d.price_label !== f.priceLabel)               return false;
    if (f.fuelCode  !== null && d.fuel_code !== f.fuelCode)                   return false;
    if (f.leads.length && !f.leads.includes(d.leads_range))                  return false;
    if (f.body.length && !f.body.includes(d.body_type))                     return false;
    if (f.recency.length && !f.recency.includes(d.recency_bucket))          return false;
    if (f.sportOnly && !d.is_sport)                                          return false;
    return true;
  }});
}}

function stats(arr) {{
  const s = arr.filter(v => v !== null && !isNaN(v)).sort((a,b)=>a-b);
  if (!s.length) return {{count:0, mean:0, median:0, std:0, min:0, max:0, q25:0, q75:0}};
  const mean = s.reduce((a,b)=>a+b,0)/s.length;
  const med  = s[Math.floor(s.length/2)];
  const std  = Math.sqrt(s.reduce((a,b)=>a+(b-mean)**2,0)/s.length);
  return {{ count: s.length, mean, median: med, std, min: s[0], max: s[s.length-1],
           q25: s[Math.floor(s.length*0.25)], q75: s[Math.floor(s.length*0.75)] }};
}}

function fmt(v, dec=0) {{
  if (v === null || isNaN(v)) return '—';
  return v.toLocaleString('en-BE', {{minimumFractionDigits:dec, maximumFractionDigits:dec}});
}}

function updateMap(filtered) {{
  // Explicit source restriction here too, mirroring the Python-side fix,
  // so filtering never re-introduces 2ememain points into the map.
  const mapData = filtered.filter(d => d.source === 'autoscout24');
  const byZip = {{}};
  mapData.forEach(d => {{
    if (d.lat == null || d.lon == null || !d.zip) return;
    if (!byZip[d.zip]) byZip[d.zip] = {{ count:0, priceSum:0, lat:d.lat, lon:d.lon, place:d.place }};
    byZip[d.zip].count++;
    byZip[d.zip].priceSum += (d.price || 0);
  }});
  const zips = Object.keys(byZip);
  if (!zips.length) {{ Plotly.react('chart-map', [], MAP_BASE.layout); return; }}
  const maxCount = Math.max(...zips.map(z => byZip[z].count), 1);
  Plotly.react('chart-map', [{{
    type: 'scattermapbox', mode: 'markers',
    lat: zips.map(z => byZip[z].lat), lon: zips.map(z => byZip[z].lon),
    marker: {{
      size: zips.map(z => (byZip[z].count / maxCount) * 30 + 7),
      color: zips.map(z => byZip[z].priceSum / byZip[z].count),
      colorscale: [[0,'#4a90d9'],[0.5,'{PALETTE["accent2"]}'],[1,'{PALETTE["accent"]}']],
      showscale: true, opacity: 0.85
    }},
    text: zips.map(z => `${{byZip[z].place}} (${{z}})<br>${{byZip[z].count}} listing(s)<br>Avg €${{fmt(byZip[z].priceSum/byZip[z].count)}}`),
    hoverinfo: 'text'
  }}], MAP_BASE.layout);
}}

function updateCharts(filtered) {{
  const prices = filtered.map(d=>d.price).filter(v=>v!==null);
  const kms    = filtered.map(d=>d.km).filter(v=>v!==null);
  const ps = stats(prices), ks = stats(kms);

  document.getElementById('kpi-count').textContent       = ps.count;
  document.getElementById('kpi-recent').textContent      = filtered.filter(d=>d.is_recent).length;
  document.getElementById('kpi-avg-price').textContent   = '€'+fmt(ps.mean);
  document.getElementById('kpi-med-price').textContent   = fmt(ps.median);
  document.getElementById('kpi-avg-km').textContent      = fmt(ks.mean);
  document.getElementById('kpi-med-km').textContent      = fmt(ks.median);
  document.getElementById('kpi-price-range').textContent = '€'+fmt(ps.min);
  document.getElementById('kpi-price-max').textContent   = fmt(ps.max);
  document.getElementById('kpi-std').textContent         = '€'+fmt(ps.std);
  document.getElementById('kpi-iqr').textContent         = fmt(ps.q25)+'–'+fmt(ps.q75);
  document.getElementById('listing-badge').textContent   = ps.count + ' listings';

  [['count','count'],['mean','mean'],['med','median'],['std','std'],['min','min'],['max','max'],['q25','q25'],['q75','q75']].forEach(([id,key])=>{{
    const ep = document.getElementById('st-'+id+'-p');
    const ek = document.getElementById('st-'+id+'-k');
    if(ep) ep.textContent = fmt(ps[key]);
    if(ek) ek.textContent = fmt(ks[key]);
  }});

  const mean = ps.mean, med = ps.median;
  Plotly.react('chart-price-hist', [{{ type:'histogram', x:prices, nbinsx:40,
    marker:{{color:'{PALETTE["accent"]}', opacity:0.85}},
    hovertemplate:'€%{{x:,.0f}}<br>Count: %{{y}}<extra></extra>' }}], {{
    ...PRICE_HIST_BASE.layout,
    shapes:[
      {{type:'line',x0:mean,x1:mean,y0:0,y1:1,yref:'paper', line:{{color:'{PALETTE["text"]}',width:1.5,dash:'dash'}}}},
      {{type:'line',x0:med,x1:med,y0:0,y1:1,yref:'paper', line:{{color:'{PALETTE["text"]}',width:1.5,dash:'dot'}}}},
    ],
    annotations:[
      {{x:mean, y:1, yref:'paper', text:'Mean: '+fmt(mean)+'€', showarrow:false, xanchor:'left', font:{{color:'{PALETTE["text"]}',size:11}}}},
      {{x:med,  y:0.9, yref:'paper', text:'Med: '+fmt(med)+'€', showarrow:false, xanchor:'left', font:{{color:'{PALETTE["text"]}',size:11}}}},
    ]
  }});

  const kmMean = ks.mean, kmMed = ks.median;
  Plotly.react('chart-km-hist', [{{ type:'histogram', x:kms, nbinsx:40,
    marker:{{color:'#4a90d9', opacity:0.85}},
    hovertemplate:'%{{x:,.0f}} km<br>Count: %{{y}}<extra></extra>' }}], {{
    ...KM_HIST_BASE.layout,
    shapes:[
      {{type:'line',x0:kmMean,x1:kmMean,y0:0,y1:1,yref:'paper', line:{{color:'{PALETTE["text"]}',width:1.5,dash:'dash'}}}},
      {{type:'line',x0:kmMed,x1:kmMed,y0:0,y1:1,yref:'paper', line:{{color:'{PALETTE["text"]}',width:1.5,dash:'dot'}}}},
    ],
  }});

  const years = [...new Set(filtered.map(d=>d.year).filter(v=>v!==null))].sort();
  const scatterTraces = years.map(yr => {{
    const sub = filtered.filter(d=>d.year===yr);
    return {{ type:'scatter', mode:'markers', name:String(yr),
      x: sub.map(d=>d.km), y: sub.map(d=>d.price), text: sub.map(d=>d.title||''),
      marker:{{size:7, opacity:0.7}},
      hovertemplate:'<b>%{{text}}</b><br>Price: €%{{y:,.0f}}<br>KM: %{{x:,.0f}}<extra></extra>' }};
  }});
  Plotly.react('chart-scatter', scatterTraces, SCATTER_BASE.layout);

  const boxTraces = years.map(yr => {{
    const sub = filtered.filter(d=>d.year===yr && d.price!==null).map(d=>d.price);
    if(sub.length<3) return null;
    return {{ type:'box', y:sub, name:String(yr), marker:{{color:'{PALETTE["accent"]}'}},
      line:{{color:'{PALETTE["accent2"]}'}}, fillcolor:'rgba(232,39,46,0.15)', boxmean:true }};
  }}).filter(Boolean);
  Plotly.react('chart-box', boxTraces, BOX_BASE.layout);

  const fuelCounts = {{}};
  filtered.forEach(d=>{{ const k=d.fuel||'Unknown'; fuelCounts[k]=(fuelCounts[k]||0)+1; }});
  const fuelSorted = Object.entries(fuelCounts).sort((a,b)=>b[1]-a[1]).slice(0,8);
  Plotly.react('chart-fuel', [{{ type:'bar', x:fuelSorted.map(e=>e[0]), y:fuelSorted.map(e=>e[1]),
    marker:{{color:'#f0a500', opacity:0.85}}, hovertemplate:'%{{x}}<br>%{{y}}<extra></extra>' }}], FUEL_BASE.layout);

  const gearCounts = {{}};
  filtered.forEach(d=>{{ const k=d.gear||'Unknown'; gearCounts[k]=(gearCounts[k]||0)+1; }});
  const gearSorted = Object.entries(gearCounts).sort((a,b)=>b[1]-a[1]);
  Plotly.react('chart-gear', [{{ type:'bar', x:gearSorted.map(e=>e[0]), y:gearSorted.map(e=>e[1]),
    marker:{{color:'#50c878', opacity:0.85}}, hovertemplate:'%{{x}}<br>%{{y}}<extra></extra>' }}], GEAR_BASE.layout);

  updateMap(filtered);
}}

let currentSortCol = 1, currentSortAsc = true;

function renderTable(data) {{
  const tbody = document.getElementById('listings-tbody');
  document.getElementById('listing-count-label').textContent = '(' + data.length + ')';
  tbody.innerHTML = data.slice(0,200).map(d => `
    <tr>
      <td>${{d.title || '—'}}${{d.is_sport ? ' <span style="color:var(--accent2)">★</span>' : ''}}</td>
      <td><strong style="color:#e8eaf0">€${{fmt(d.price)}}</strong></td>
      <td>${{fmt(d.km)}} km</td>
      <td>${{d.year || '—'}}</td>
      <td>${{d.fuel || '—'}}</td>
      <td>${{d.gear || '—'}}</td>
      <td style="color:var(--muted);font-size:12px">${{d.body_type || '—'}}</td>
      <td><span class="src-badge src-${{d.source}}">${{d.source||'—'}}</span></td>
      <td style="color:${{d.recency_reliable ? 'var(--accent2)' : 'var(--muted)'}};font-size:11px" title="${{d.recency_reliable ? 'From the site itself' : 'Scraper-tracked, not a real posting date'}}">${{d.recency_label || '—'}}</td>
      <td>${{d.url ? '<a href="'+d.url+'" target="_blank">View ↗</a>' : '—'}}</td>
    </tr>
  `).join('');
}}

function sortTable(col) {{
  if (currentSortCol === col) currentSortAsc = !currentSortAsc;
  else {{ currentSortCol = col; currentSortAsc = true; }}
  const keys = ['title','price','km','year','fuel','gear','body_type','source','recency_label'];
  const key = keys[col];
  const filtered = applyFilters(ALL_DATA, getFilters());
  filtered.sort((a,b)=>{{
    const va = a[key], vb = b[key];
    if(va===null) return 1; if(vb===null) return -1;
    return currentSortAsc ? (va>vb?1:-1) : (va<vb?1:-1);
  }});
  renderTable(filtered);
}}

let filterTimer;
function onFilterChange() {{
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => {{
    const f = getFilters();
    const filtered = applyFilters(ALL_DATA, f);
    updateCharts(filtered);
    renderTable(filtered);
  }}, 300);
}}

function resetFilters() {{
  ['f-price-min','f-price-max','f-km-min','f-km-max'].forEach(id=>{{ document.getElementById(id).value = ''; }});
  document.getElementById('f-year-min').value = '{year_min_val}';
  document.getElementById('f-year-max').value = '{year_max_val}';
  ['f-source','f-gear','f-price-label','f-fuel-code'].forEach(id=>{{ document.getElementById(id).value = ''; }});
  ['f-fuel','f-seller','f-leads','f-body','f-recency'].forEach(id=>{{
    Array.from(document.getElementById(id).options).forEach(o => o.selected = false);
  }});
  document.getElementById('f-sport').checked = false;
  onFilterChange();
}}

['f-price-min','f-price-max','f-km-min','f-km-max','f-year-min','f-year-max',
 'f-source','f-fuel','f-gear','f-seller','f-price-label','f-fuel-code','f-leads','f-body','f-recency'
].forEach(id => {{
  const el = document.getElementById(id);
  if(el) el.addEventListener('input', onFilterChange);
  if(el) el.addEventListener('change', onFilterChange);
}});
document.getElementById('f-sport').addEventListener('change', onFilterChange);
</script>
</body>
</html>"""

    return html


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

async def main():
    autoscout_filters = {
        "make":      "toyota",
        "model":     "corolla",
        "km_max":    100000,
        "year_min":  2019,
        "cy":        "B",
        # "adage":   3,   # uncomment to restrict AutoScout24 to listings
                          # posted in the last N days server-side (1-6, 7, 14)
                          # [Likely, not independently verified] — cheaper
                          # than scraping everything and filtering after.
                          # The per-listing "Nouveau" badge (<1 day) is
                          # captured either way, unaffected by this.
    }

    df_autoscout = await scrape_autoscout(autoscout_filters, max_pages=30)
    df_2ememain = await scrape_2ememain(construction_year_from=2019, max_pages=20)

    df = merge_sources(df_autoscout, df_2ememain)

    if df.empty:
        print("No data collected from either source.")
        return

    csv_path = Path("toyota_corolla_combined.csv")
    df.to_csv(csv_path, index=False)
    print(f"✅ Combined raw data saved to {csv_path}")

    html = build_dashboard(df)
    out_path = Path("index.html")   # matches GitHub Pages' default entry file
    out_path.write_text(html, encoding="utf-8")
    print(f"✅ Dashboard saved to {out_path.resolve()}")

    webbrowser.open(out_path.resolve().as_uri())
    print("🌐 Dashboard opened in browser.")


if __name__ == "__main__":
    asyncio.run(main())
