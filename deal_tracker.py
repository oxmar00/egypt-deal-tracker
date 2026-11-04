#!/usr/bin/env python3
"""
Amazon Egypt (amazon.eg) & Noon Egypt (noon.com/egypt-en) Automated Deal Tracker
High-Frequency Edition (4-Minute Continuous Loop + Anti-Bot Stealth & Proxy Rotation)
Designed for scheduled execution via GitHub Actions with Telegram Bot alerts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple, Any

from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, Response, BrowserContext, Page
except ImportError:
    async_playwright = None
    Response = BrowserContext = Page = Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("deal_tracker")


# ==============================================================================
# Configuration (via Environment Variables / GitHub Secrets)
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Filtering thresholds
MIN_DISCOUNT_PCT = float(os.getenv("MIN_DISCOUNT_PCT", "20.0"))
MAX_PRICE_EGP = float(os.getenv("MAX_PRICE_EGP", "0.0"))  # 0 = no upper limit
MIN_RATING = float(os.getenv("MIN_RATING", "3.8"))
RE_ALERT_DROP_PCT = float(os.getenv("RE_ALERT_DROP_PCT", "5.0"))  # Re-notify if price drops another 5%
MAX_ALERTS_PER_RUN = int(os.getenv("MAX_ALERTS_PER_RUN", "15"))

# High-Frequency 1-Minute Loop Settings
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))   # 1 minute between cycle starts
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "10"))                 # Random +/- 10s human jitter
MIN_CYCLE_GAP_SECONDS = float(os.getenv("MIN_CYCLE_GAP_SECONDS", "5"))  # Always pause at least this long
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "50"))       # Run for 50m per GitHub job
URLS_PER_PLATFORM_CYCLE = int(os.getenv("URLS_PER_PLATFORM_CYCLE", "2"))  # Stagger URLs per cycle

# Anti-Block Proxy Pool (comma-separated list of http/socks5 proxies)
# Example: http://user:pass@gate.smartproxy.com:7000,http://user:pass@proxy2.com:8080
RAW_PROXIES = os.getenv("PROXY_URLS", "").strip()
PROXY_POOL: List[str] = [p.strip() for p in RAW_PROXIES.split(",") if p.strip()]

# Optional Affiliate Tags
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG", "").strip()
NOON_AFFILIATE_CODE = os.getenv("NOON_AFFILIATE_CODE", "").strip()

# Database path (cached & committed across GitHub Actions runs)
DB_PATH = os.getenv("DEALS_DB_PATH", "deals_history.db")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Only alert on Amazon offers shipped & sold by Amazon.eg (not third-party sellers)
AMAZON_SOLD_BY_AMAZON_ONLY = os.getenv("AMAZON_SOLD_BY_AMAZON_ONLY", "true").lower() in ("1", "true", "yes")
AMAZON_EG_SELLER_ID = "A1ZVRGNO5AYLOV"  # "Amazon.eg" in the search page's Seller (p_6) filter


AMAZON_ALL_DISCOUNTS = "26901036031"  # "Deals & Discounts > All Discounts" (p_n_deal_type)


def build_amazon_deal_url(department: str, node: str = "") -> str:
    """Discounted-items search URL for a department (and optional sub-category node), optionally Amazon.eg-sold only."""
    rh = (f"n:{node}," if node else "") + f"p_n_deal_type:{AMAZON_ALL_DISCOUNTS}"
    if AMAZON_SOLD_BY_AMAZON_ONLY:
        rh += f",p_6:{AMAZON_EG_SELLER_ID}"
    params = {"i": department, "rh": rh, "s": "featured-rank"}
    return "https://www.amazon.eg/-/en/s?" + urllib.parse.urlencode(params)


# Target URLs for Egypt Deals: Electronics, Women's & Men's Fashion, Supermarket, Automotive, Health
# (department aliases from amazon.eg's search dropdown; fashion nodes from its Department refinement)
AMAZON_EG_CATEGORY_URLS: List[Tuple[str, str]] = [
    ("Electronics", build_amazon_deal_url("electronics")),
    ("Women's Fashion", build_amazon_deal_url("fashion", "21845145031")),
    ("Men's Fashion", build_amazon_deal_url("fashion", "21845147031")),
    ("Supermarket", build_amazon_deal_url("grocery")),
    ("Automotive", build_amazon_deal_url("automotive")),
    ("Health & Nutrition", build_amazon_deal_url("hpc")),  # Health, Household & Baby Care
]

# Same categories on Noon; each uses whichever deal tag (mega-deal / deals-generic) lists the most 20%+ offers
NOON_EG_CATEGORY_URLS: List[Tuple[str, str]] = [
    ("Electronics", "https://www.noon.com/egypt-en/electronics-and-mobiles/?f[deal_tag][]=mega-deal&sort[by]=popularity&sort[dir]=desc"),
    ("Women's Fashion", "https://www.noon.com/egypt-en/fashion/eg-fashion-women-cat/?f[deal_tag][]=deals-generic&sort[by]=popularity&sort[dir]=desc"),
    ("Men's Fashion", "https://www.noon.com/egypt-en/fashion/eg-fashion-men-cat/?f[deal_tag][]=deals-generic&sort[by]=popularity&sort[dir]=desc"),
    ("Supermarket", "https://www.noon.com/egypt-en/grocery-store/?f[deal_tag][]=mega-deal&sort[by]=popularity&sort[dir]=desc"),
    ("Automotive", "https://www.noon.com/egypt-en/automotive/?f[deal_tag][]=deals-generic&sort[by]=popularity&sort[dir]=desc"),
    ("Health & Nutrition", "https://www.noon.com/egypt-en/health/?f[deal_tag][]=mega-deal&sort[by]=popularity&sort[dir]=desc"),
]

AMAZON_EG_DEAL_URLS = [url for _, url in AMAZON_EG_CATEGORY_URLS]
NOON_EG_DEAL_URLS = [url for _, url in NOON_EG_CATEGORY_URLS]
CATEGORY_BY_URL: Dict[str, str] = {url: cat for cat, url in AMAZON_EG_CATEGORY_URLS + NOON_EG_CATEGORY_URLS}


def tag_new_deals_with_category(deals: Dict[str, "DealItem"], url: str) -> None:
    """Labels deals found on the page just scanned with that page's category."""
    category = CATEGORY_BY_URL.get(url)
    for deal in deals.values():
        if deal.category is None:
            deal.category = category


# ==============================================================================
# Anti-Bot Fingerprint & Stealth Profiles
# ==============================================================================
BROWSER_PROFILES: List[Dict[str, object]] = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        "platform": '"Windows"',
        "viewport": {"width": 1920, "height": 1080},
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "platform": '"macOS"',
        "viewport": {"width": 1440, "height": 900},
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        "platform": '"Windows"',
        "viewport": {"width": 1536, "height": 864},
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not)A;Brand";v="99", "Google Chrome";v="127", "Chromium";v="127"',
        "platform": '"Windows"',
        "viewport": {"width": 1366, "height": 768},
    },
]

STEALTH_INIT_SCRIPT = """
() => {
    // Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Mock Chrome runtime object
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {}
    };

    // Mock realistic plugins & languages for Egypt locale
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-EG', 'en-US', 'en', 'ar-EG']
    });

    // Pass permissions check
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );

    // Mask WebGL vendor/renderer against headless detection
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.apply(this, arguments);
    };
}
"""

BLOCKED_RESOURCE_TYPES = {"media", "font", "beacon", "csp_report"}
BLOCKED_TRACKER_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "tiktok.com",
    "hotjar.com",
    "clarity.ms",
    "criteo.com",
)


def parse_proxy_config(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    """Converts a standard proxy URI into Playwright's proxy configuration dict."""
    if not proxy_url:
        return None
    parsed = urllib.parse.urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    cfg: Dict[str, str] = {"server": server}
    if parsed.username:
        cfg["username"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        cfg["password"] = urllib.parse.unquote(parsed.password)
    return cfg


async def create_stealth_context(browser) -> BrowserContext:
    """Creates an isolated, fingerprint-rotated browser context with optional proxy."""
    profile = random.choice(BROWSER_PROFILES)
    chosen_proxy = random.choice(PROXY_POOL) if PROXY_POOL else None
    proxy_cfg = parse_proxy_config(chosen_proxy)

    if chosen_proxy:
        masked = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", chosen_proxy)
        logger.info(f"[Stealth] Using rotated proxy: {masked}")

    context = await browser.new_context(
        user_agent=str(profile["ua"]),
        viewport=profile["viewport"],  # type: ignore[arg-type]
        locale="en-EG",
        timezone_id="Africa/Cairo",
        proxy=proxy_cfg,
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-EG,en-US;q=0.9,en;q=0.8,ar-EG;q=0.7",
            "Sec-CH-UA": str(profile["sec_ch_ua"]),
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": str(profile["platform"]),
            "Upgrade-Insecure-Requests": "1",
        },
    )
    await context.add_init_script(STEALTH_INIT_SCRIPT)

    # Block telemetry & heavy media to speed up loads and reduce tracker fingerprinting
    async def route_interceptor(route):
        req = route.request
        if req.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        if any(domain in req.url for domain in BLOCKED_TRACKER_DOMAINS):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_interceptor)
    return context


async def simulate_human_interaction(page: Page) -> None:
    """Simulates natural human cursor movement and randomized scroll steps."""
    try:
        for _ in range(random.randint(2, 4)):
            x = random.randint(150, 950)
            y = random.randint(150, 650)
            await page.mouse.move(x, y, steps=random.randint(5, 12))
            await page.mouse.wheel(0, random.randint(650, 1350))
            await page.wait_for_timeout(random.randint(850, 1850))
    except Exception:
        pass


def is_bot_challenged(html: str, platform: str) -> bool:
    """Detects Amazon CAPTCHA pages or Noon WAF / Cloudflare challenge screens."""
    lower = html.lower()
    if platform == "Amazon Egypt":
        return (
            "validatecaptcha" in lower
            or "enter the characters you see below" in lower
            or "api-services-support@amazon.com" in lower
        )
    if platform == "Noon Egypt":
        return (
            "access denied" in lower
            or "cf-chl-bypass" in lower
            or "just a moment..." in lower
            or ("<title>403" in lower)
        )
    return False


# ==============================================================================
# Data Model
# ==============================================================================
@dataclass
class DealItem:
    product_id: str          # ASIN (Amazon) or SKU (Noon)
    platform: str            # "Amazon Egypt" or "Noon Egypt"
    title: str
    current_price: float     # EGP
    original_price: float    # EGP
    discount_pct: float      # 0 - 100
    url: str
    image_url: Optional[str] = None
    coupon_text: Optional[str] = None
    is_express_or_fba: bool = False
    rating: Optional[float] = None
    review_count: Optional[int] = None
    is_historical_low: bool = False
    category: Optional[str] = None  # e.g. "Electronics", from the page it was found on


# ==============================================================================
# SQLite Price History & Deduplication Store
# ==============================================================================
class DealDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    lowest_price REAL NOT NULL,
                    highest_price REAL NOT NULL,
                    last_seen_price REAL NOT NULL,
                    last_notified_price REAL,
                    last_notified_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )

    def evaluate_and_record(self, deal: DealItem) -> bool:
        """
        Records the current price in SQLite and returns True if the deal
        qualifies for a new Telegram alert.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT lowest_price, highest_price, last_notified_price
            FROM products WHERE product_id = ?
            """,
            (deal.product_id,),
        )
        row = cur.fetchone()

        # Log snapshot
        cur.execute(
            "INSERT INTO price_snapshots (product_id, price, recorded_at) VALUES (?, ?, ?)",
            (deal.product_id, deal.current_price, now_iso),
        )

        if row is None:
            # First time seeing this product
            deal.is_historical_low = True
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO products (
                        product_id, platform, title, lowest_price, highest_price,
                        last_seen_price, last_notified_price, last_notified_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        deal.product_id,
                        deal.platform,
                        deal.title,
                        deal.current_price,
                        max(deal.original_price, deal.current_price),
                        deal.current_price,
                        now_iso,
                    ),
                )
            return True

        lowest_price, highest_price, last_notified_price = row
        if deal.current_price <= lowest_price:
            deal.is_historical_low = True
            new_lowest = deal.current_price
        else:
            new_lowest = lowest_price

        new_highest = max(highest_price, deal.original_price, deal.current_price)

        # Update product record
        with self.conn:
            self.conn.execute(
                """
                UPDATE products
                SET title = ?, lowest_price = ?, highest_price = ?,
                    last_seen_price = ?, updated_at = ?
                WHERE product_id = ?
                """,
                (
                    deal.title,
                    new_lowest,
                    new_highest,
                    deal.current_price,
                    now_iso,
                    deal.product_id,
                ),
            )

        # Decide whether to notify
        if last_notified_price is None:
            return True

        # Only re-notify if current price dropped by at least RE_ALERT_DROP_PCT below last notified price
        drop_from_last_alert = ((last_notified_price - deal.current_price) / last_notified_price) * 100.0
        return drop_from_last_alert >= RE_ALERT_DROP_PCT

    def mark_notified(self, product_id: str, notified_price: float) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                UPDATE products
                SET last_notified_price = ?, last_notified_at = ?
                WHERE product_id = ?
                """,
                (notified_price, now_iso, product_id),
            )

    def close(self) -> None:
        self.conn.close()


# ==============================================================================
# Helper Utilities
# ==============================================================================
def parse_egp_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


def build_affiliate_url(url: str, platform: str) -> str:
    if platform == "Amazon Egypt" and AMAZON_PARTNER_TAG:
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        q["tag"] = [AMAZON_PARTNER_TAG]
        new_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    if platform == "Noon Egypt" and NOON_AFFILIATE_CODE:
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        q["utm_source"] = ["affiliate"]
        q["utm_code"] = [NOON_AFFILIATE_CODE]
        new_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    return url


def passes_filters(deal: DealItem) -> bool:
    if deal.discount_pct < MIN_DISCOUNT_PCT:
        return False
    if MAX_PRICE_EGP > 0 and deal.current_price > MAX_PRICE_EGP:
        return False
    if deal.rating is not None and deal.rating < MIN_RATING:
        return False
    if deal.current_price >= deal.original_price:
        return False
    return True


def select_cycle_urls(url_list: List[str], cycle_index: int, count: int) -> List[str]:
    """Rotates through target URLs across 4-minute cycles to minimize request bursts."""
    if len(url_list) <= count:
        return list(url_list)
    start = (cycle_index * count) % len(url_list)
    selected = [url_list[(start + i) % len(url_list)] for i in range(count)]
    return selected


# ==============================================================================
# Scrapers (Amazon Egypt & Noon Egypt via Playwright + Anti-Bot)
# ==============================================================================
async def scrape_amazon_egypt(context: BrowserContext, urls_to_scan: List[str]) -> Tuple[List[DealItem], bool]:
    deals: Dict[str, DealItem] = {}
    was_blocked = False
    page = await context.new_page()

    # Warm-up referer header
    await page.set_extra_http_headers({"Referer": "https://www.amazon.eg/"})

    for url in urls_to_scan:
        logger.info(f"[Amazon.eg] Scanning: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(2400, 4200))

            html = await page.content()
            if is_bot_challenged(html, "Amazon Egypt"):
                logger.warning("[Amazon.eg] Bot/CAPTCHA challenge detected! Aborting cycle & rotating session.")
                was_blocked = True
                break

            await simulate_human_interaction(page)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            # 1. Standard search/category deal cards (data-asin)
            for item in soup.select("div[data-asin]"):
                asin = (item.get("data-asin") or "").strip()
                if not asin or len(asin) != 10:
                    continue

                title_el = item.select_one("h2 span") or item.select_one("img.s-image")
                title = ""
                if title_el:
                    title = title_el.get_text(strip=True) if title_el.name != "img" else (title_el.get("alt") or "")
                if not title:
                    continue

                price_whole = item.select_one("span.a-price:not([data-a-strike]) span.a-offscreen")
                if not price_whole:
                    price_whole = item.select_one("span.a-price-whole")
                orig_el = item.select_one("span.a-price[data-a-strike='true'] span.a-offscreen")

                current_price = parse_egp_price(price_whole.get_text() if price_whole else None)
                original_price = parse_egp_price(orig_el.get_text() if orig_el else None)

                if not current_price or not original_price or original_price <= current_price:
                    continue

                discount_pct = round(((original_price - current_price) / original_price) * 100.0, 1)

                # Check coupon badge
                coupon_el = item.select_one("span.s-coupon-unclipped") or item.select_one("span[data-component-type='s-coupon-component']")
                coupon_text = coupon_el.get_text(" ", strip=True) if coupon_el else None

                # Rating
                rating_el = item.select_one("span.a-icon-alt")
                rating = None
                if rating_el:
                    m = re.search(r"(\d+(\.\d+)?)", rating_el.get_text())
                    if m:
                        rating = float(m.group(1))

                # Image
                img_el = item.select_one("img.s-image")
                img_url = img_el.get("src") if img_el else None

                # Fulfilled by Amazon / Prime check
                is_fba = bool(item.select_one("i.a-icon-prime") or "Fulfilled by Amazon" in item.get_text())

                product_url = build_affiliate_url(f"https://www.amazon.eg/-/en/dp/{asin}", "Amazon Egypt")

                deal = DealItem(
                    product_id=f"AMZ-{asin}",
                    platform="Amazon Egypt",
                    title=title[:180],
                    current_price=current_price,
                    original_price=original_price,
                    discount_pct=discount_pct,
                    url=product_url,
                    image_url=img_url,
                    coupon_text=coupon_text,
                    is_express_or_fba=is_fba,
                    rating=rating,
                )
                if passes_filters(deal):
                    deals[deal.product_id] = deal

            # 2. Goldbox / Today's Deals cards (data-testid="deal-card")
            for card in soup.select("[data-testid='deal-card'], [data-testid='product-card']"):
                link_el = card.select_one("a[href*='/dp/']")
                if not link_el:
                    continue
                href = link_el.get("href", "")
                m_asin = re.search(r"/dp/([A-Z0-9]{10})", href)
                if not m_asin:
                    continue
                asin = m_asin.group(1)

                title = card.get_text(" ", strip=True)
                img_el = card.select_one("img")
                if img_el and img_el.get("alt"):
                    title = img_el.get("alt").strip()

                prices = re.findall(r"EGP\s*([\d,]+(?:\.\d+)?)", card.get_text(" ", strip=True))
                if len(prices) >= 2:
                    p1 = parse_egp_price(prices[0])
                    p2 = parse_egp_price(prices[1])
                    if p1 and p2 and p2 > p1:
                        discount_pct = round(((p2 - p1) / p2) * 100.0, 1)
                        product_url = build_affiliate_url(f"https://www.amazon.eg/-/en/dp/{asin}", "Amazon Egypt")
                        deal = DealItem(
                            product_id=f"AMZ-{asin}",
                            platform="Amazon Egypt",
                            title=title[:180],
                            current_price=p1,
                            original_price=p2,
                            discount_pct=discount_pct,
                            url=product_url,
                            image_url=img_el.get("src") if img_el else None,
                            is_express_or_fba=True,
                        )
                        if passes_filters(deal):
                            deals[deal.product_id] = deal

            # Human delay between pages
            await page.wait_for_timeout(random.randint(2500, 5200))

        except Exception as exc:
            logger.warning(f"[Amazon.eg] Error scraping {url}: {exc}")

        tag_new_deals_with_category(deals, url)

    await page.close()
    logger.info(f"[Amazon.eg] Found {len(deals)} qualifying deals.")
    return list(deals.values()), was_blocked


def amazon_buybox_sold_by_amazon(html: str) -> Optional[bool]:
    """
    Reads the product page buy box. Amazon-sold offers show a single "Shipper / Seller: Amazon.eg"
    row; third-party offers show "Sold by: <shop>" (often with "Fulfilled by: Amazon").
    Returns True/False, or None if the seller row is missing (e.g. out of stock or layout change).
    """
    soup = BeautifulSoup(html, "lxml")

    def feature_text(name: str) -> Optional[str]:
        el = soup.select_one(f"[offer-display-feature-name='{name}'] .offer-display-feature-text-message")
        return el.get_text(strip=True) if el else None

    seller = feature_text("desktop-merchant-info")
    if seller is None:
        return None
    shipper = feature_text("desktop-fulfiller-info")
    return seller.lower() == "amazon.eg" and (shipper is None or shipper.lower().startswith("amazon"))


# ASIN -> sold-by-Amazon.eg verdict, cached for the lifetime of one run
AMAZON_SELLER_CACHE: Dict[str, bool] = {}


async def verify_amazon_seller(context: BrowserContext, deal: DealItem) -> bool:
    """Opens the product page and confirms the buy box offer is shipped & sold by Amazon.eg."""
    asin = deal.product_id.removeprefix("AMZ-")
    if asin in AMAZON_SELLER_CACHE:
        return AMAZON_SELLER_CACHE[asin]

    page = await context.new_page()
    try:
        await page.goto(f"https://www.amazon.eg/-/en/dp/{asin}", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(1200, 2600))
        html = await page.content()
    except Exception as exc:
        logger.warning(f"[Amazon.eg] Could not open {asin} to check seller: {exc}")
        return False
    finally:
        await page.close()

    if is_bot_challenged(html, "Amazon Egypt"):
        logger.warning(f"[Amazon.eg] CAPTCHA while checking seller for {asin}; skipping this cycle.")
        return False

    verdict = amazon_buybox_sold_by_amazon(html)
    if verdict is None:
        logger.info(f"[Amazon.eg] No seller info on {asin} page; skipping.")
        return False
    AMAZON_SELLER_CACHE[asin] = verdict
    if not verdict:
        logger.info(f"[Amazon.eg] Skipping {asin}: not shipped & sold by Amazon.eg.")
    return verdict


def parse_noon_product_boxes(soup: BeautifulSoup) -> List[DealItem]:
    """
    Parses Noon PLP product cards. Each card is an <a href=".../<SKU>/p/"> holding
    data-qa="plp-product-box-name" and data-qa="plp-product-box-price"; the price box
    shows "EGP <sale>" in <strong> and the old price in .strikeThrough (no "EGP" prefix).
    """
    results: List[DealItem] = []
    for a_tag in soup.select("a[href*='/p/']"):
        price_box = a_tag.select_one("[data-qa='plp-product-box-price']")
        if not price_box:
            continue
        href = a_tag.get("href", "")
        m_sku = re.search(r"/([A-Z0-9]+(?:\-[A-Z0-9]+)?)/p/", href, re.I)
        if not m_sku:
            continue
        sku = m_sku.group(1).upper()

        curr_el = price_box.select_one("strong")
        orig_el = price_box.select_one(".strikeThrough")
        curr = parse_egp_price(curr_el.get_text() if curr_el else None)
        orig = parse_egp_price(orig_el.get_text() if orig_el else None)
        if not curr or not orig or orig <= curr:
            continue

        name_el = a_tag.select_one("[data-qa='plp-product-box-name']")
        title = ((name_el.get("title") or name_el.get_text(strip=True)) if name_el else "").strip()
        if not title:
            continue

        rating = None
        rating_el = a_tag.select_one("[class*='_textCtr_']")
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True))
            except ValueError:
                rating = None

        # The first <img> in a card is a placeholder SVG; the real one is the product image
        img_el = a_tag.select_one("img[class*='productImage']")
        disc = round(((orig - curr) / orig) * 100.0, 1)
        full_url = urllib.parse.urljoin("https://www.noon.com", href.split("?")[0])

        results.append(DealItem(
            product_id=f"NOON-{sku}",
            platform="Noon Egypt",
            title=title[:180],
            current_price=curr,
            original_price=orig,
            discount_pct=disc,
            url=build_affiliate_url(full_url, "Noon Egypt"),
            image_url=img_el.get("src") if img_el else None,
            is_express_or_fba=bool(a_tag.select_one("[data-qa='product-noon-express']")),
            rating=rating,
        ))
    return results


async def scrape_noon_egypt(context: BrowserContext, urls_to_scan: List[str]) -> Tuple[List[DealItem], bool]:
    deals: Dict[str, DealItem] = {}
    was_blocked = False
    page = await context.new_page()

    await page.set_extra_http_headers({"Referer": "https://www.noon.com/egypt-en/"})

    # Intercept Noon's internal JSON catalog responses if fired
    async def handle_response(response: Response):
        if "/_svc/catalog/api/" in response.url and response.status == 200:
            try:
                data = await response.json()
                hits = data.get("hits") or []
                for hit in hits:
                    sku = hit.get("sku")
                    name = hit.get("name")
                    price = hit.get("price")
                    sale_price = hit.get("sale_price")
                    if not sku or not name or not price or not sale_price:
                        continue
                    curr = float(sale_price)
                    orig = float(price)
                    if orig <= curr:
                        continue
                    disc = round(((orig - curr) / orig) * 100.0, 1)
                    uri = hit.get("url") or sku
                    full_url = f"https://www.noon.com/egypt-en/{uri.lstrip('/')}/{sku}/p/"
                    full_url = build_affiliate_url(full_url, "Noon Egypt")
                    img_key = hit.get("image_key")
                    img_url = f"https://f.nooncdn.com/p/{img_key}.jpg" if img_key else None
                    rating_val = hit.get("product_rating", {}).get("value") if isinstance(hit.get("product_rating"), dict) else None

                    deal = DealItem(
                        product_id=f"NOON-{sku}",
                        platform="Noon Egypt",
                        title=name[:180],
                        current_price=curr,
                        original_price=orig,
                        discount_pct=disc,
                        url=full_url,
                        image_url=img_url,
                        is_express_or_fba=bool(hit.get("is_fbn")),
                        rating=float(rating_val) if rating_val else None,
                    )
                    if passes_filters(deal):
                        deals[deal.product_id] = deal
            except Exception:
                pass

    page.on("response", handle_response)

    for url in urls_to_scan:
        logger.info(f"[Noon Egypt] Scanning: {url}")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(random.randint(2800, 4800))

            html = await page.content()
            if (resp and resp.status in (403, 429)) or is_bot_challenged(html, "Noon Egypt"):
                logger.warning("[Noon Egypt] WAF/Rate-limit challenge detected! Aborting cycle & rotating session.")
                was_blocked = True
                break

            await simulate_human_interaction(page)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            # Fallback DOM parsing & Next.js __NEXT_DATA__ extraction
            next_data_script = soup.select_one("script#__NEXT_DATA__")
            if next_data_script and next_data_script.string:
                try:
                    nd = json.loads(next_data_script.string)
                    props = nd.get("props", {}).get("pageProps", {})
                    catalog = props.get("catalog", {}) or props.get("initialState", {}).get("catalog", {})
                    for hit in catalog.get("hits", []):
                        sku = hit.get("sku")
                        name = hit.get("name")
                        orig = hit.get("price")
                        curr = hit.get("sale_price")
                        if sku and name and orig and curr and float(orig) > float(curr):
                            disc = round(((float(orig) - float(curr)) / float(orig)) * 100.0, 1)
                            uri = hit.get("url") or sku
                            full_url = build_affiliate_url(
                                f"https://www.noon.com/egypt-en/{uri.lstrip('/')}/{sku}/p/",
                                "Noon Egypt",
                            )
                            img_key = hit.get("image_key")
                            img_url = f"https://f.nooncdn.com/p/{img_key}.jpg" if img_key else None
                            deal = DealItem(
                                product_id=f"NOON-{sku}",
                                platform="Noon Egypt",
                                title=name[:180],
                                current_price=float(curr),
                                original_price=float(orig),
                                discount_pct=disc,
                                url=full_url,
                                image_url=img_url,
                                is_express_or_fba=bool(hit.get("is_fbn")),
                            )
                            if passes_filters(deal):
                                deals[deal.product_id] = deal
                except Exception:
                    pass

            # Product box cards (PLP layout)
            for deal in parse_noon_product_boxes(soup):
                if passes_filters(deal):
                    deals[deal.product_id] = deal

            await page.wait_for_timeout(random.randint(2500, 5200))

        except Exception as exc:
            logger.warning(f"[Noon Egypt] Error scraping {url}: {exc}")

        tag_new_deals_with_category(deals, url)

    await page.close()
    logger.info(f"[Noon Egypt] Found {len(deals)} qualifying deals.")
    return list(deals.values()), was_blocked


# ==============================================================================
# Telegram Notification Dispatcher
# ==============================================================================
def format_telegram_caption(deal: DealItem) -> str:
    savings = deal.original_price - deal.current_price
    platform_icon = "🟠" if deal.platform == "Amazon Egypt" else "🟡"
    badges = [f"🔥 <b>{deal.discount_pct:.0f}% OFF</b>"]
    if deal.is_historical_low:
        badges.append("📉 <b>Historical Low</b>")
    if deal.is_express_or_fba:
        badges.append("🚀 <b>Fast Shipping</b>")

    badge_line = " | ".join(badges)
    safe_title = (
        deal.title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    lines = [
        f"{platform_icon} <b>{deal.platform} Hot Offer</b>",
        f"{badge_line}",
    ]
    if deal.category:
        # Hashtag lets you tap it in Telegram to see every alert from that category
        hashtag = "#" + re.sub(r"[^A-Za-z0-9]", "", deal.category)
        lines.append(f"🏷 <b>Category:</b> {deal.category.replace('&', '&amp;')} {hashtag}")
    lines += [
        "",
        f"📦 <b>{safe_title}</b>",
        "",
        f"💰 <b>New Price:</b> <code>{deal.current_price:,.2f} EGP</code>",
        f"❌ <b>Was:</b> <s>{deal.original_price:,.2f} EGP</s> (Save {savings:,.2f} EGP)",
    ]
    if deal.coupon_text:
        safe_coupon = deal.coupon_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"🎟 <b>Extra Coupon:</b> {safe_coupon}")
    if deal.rating:
        lines.append(f"⭐ <b>Rating:</b> {deal.rating:.1f} / 5.0")

    return "\n".join(lines)


def send_telegram_alert(deal: DealItem) -> bool:
    caption = format_telegram_caption(deal)
    reply_markup = {
        "inline_keyboard": [
            [{"text": f"🛒 Buy on {deal.platform}", "url": deal.url}]
        ]
    }

    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[DRY-RUN / NO-TOKEN] Telegram Alert Prepared:\n{caption}\nURL: {deal.url}\n")
        return True

    base_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    # Try sendPhoto first if image_url is a valid HTTP URL
    if deal.image_url and deal.image_url.startswith("http"):
        photo_payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": deal.image_url,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_api}/sendPhoto",
            data=photo_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:
            logger.warning(f"sendPhoto failed ({exc}), falling back to sendMessage...")

    # Fallback to sendMessage
    msg_payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": reply_markup,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_api}/sendMessage",
        data=msg_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.error(f"Failed to send Telegram alert for {deal.product_id}: {exc}")
        return False


# ==============================================================================
# Single Scan Cycle & Continuous 4-Minute Loop Engine
# ==============================================================================
async def execute_single_cycle(browser, db: DealDatabase, cycle_idx: int) -> Tuple[int, int, bool]:
    """Runs one 4-minute scan cycle with isolated browser contexts per site."""
    amz_urls = select_cycle_urls(AMAZON_EG_DEAL_URLS, cycle_idx, URLS_PER_PLATFORM_CYCLE)
    noon_urls = select_cycle_urls(NOON_EG_DEAL_URLS, cycle_idx, URLS_PER_PLATFORM_CYCLE)

    amz_context = await create_stealth_context(browser)
    noon_context = await create_stealth_context(browser)

    sent_count = 0
    try:
        (amz_deals, amz_blocked), (noon_deals, noon_blocked) = await asyncio.gather(
            scrape_amazon_egypt(amz_context, amz_urls),
            scrape_noon_egypt(noon_context, noon_urls),
        )

        all_deals = amz_deals + noon_deals
        all_deals.sort(key=lambda d: d.discount_pct, reverse=True)

        for deal in all_deals:
            if sent_count >= MAX_ALERTS_PER_RUN:
                break
            if not db.evaluate_and_record(deal):
                continue
            # Seller check runs only for deals about to be alerted, keeping extra page loads low
            if deal.platform == "Amazon Egypt" and AMAZON_SOLD_BY_AMAZON_ONLY:
                if not await verify_amazon_seller(amz_context, deal):
                    continue
            if send_telegram_alert(deal):
                db.mark_notified(deal.product_id, deal.current_price)
                sent_count += 1
                await asyncio.sleep(1.3)
    finally:
        await amz_context.close()
        await noon_context.close()

    any_blocked = amz_blocked or noon_blocked
    return len(all_deals), sent_count, any_blocked


async def run_tracker(loop_mode: bool = False) -> None:
    if async_playwright is None:
        logger.error("Playwright is not installed. Run: pip install playwright beautifulsoup4 lxml && playwright install chromium")
        sys.exit(1)

    db = DealDatabase(DB_PATH)
    start_ts = time.monotonic()
    max_seconds = MAX_RUNTIME_MINUTES * 60
    cycle_idx = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        while True:
            cycle_start = time.monotonic()
            logger.info(f"=== Starting Scan Cycle #{cycle_idx + 1} ===")
            scanned, sent, was_blocked = await execute_single_cycle(browser, db, cycle_idx)
            logger.info(
                f"=== Cycle #{cycle_idx + 1} Complete | Deals Scanned: {scanned} | Alerts Sent: {sent} ==="
            )
            cycle_idx += 1

            if not loop_mode:
                break

            elapsed_total = time.monotonic() - start_ts
            if elapsed_total + LOOP_INTERVAL_SECONDS >= max_seconds:
                logger.info(
                    f"Reached {MAX_RUNTIME_MINUTES}-minute window ({elapsed_total / 60:.1f}m elapsed). "
                    "Exiting cleanly to persist SQLite DB before next hourly GitHub Actions job."
                )
                break

            # Calculate sleep time to hit the loop interval target + anti-bot jitter
            cycle_duration = time.monotonic() - cycle_start
            jitter = random.randint(-JITTER_SECONDS, JITTER_SECONDS)
            target_wait = max(MIN_CYCLE_GAP_SECONDS, (LOOP_INTERVAL_SECONDS + jitter) - cycle_duration)

            # If a CAPTCHA or WAF challenge occurred without a proxy pool, apply extra cooldown
            if was_blocked and not PROXY_POOL:
                backoff = random.randint(90, 180)
                logger.warning(f"Applying {backoff}s anti-block cooldown before next cycle...")
                target_wait += backoff

            logger.info(f"Cycle took {cycle_duration:.1f}s; sleeping {target_wait:.1f}s until next scan cycle...")
            await asyncio.sleep(target_wait)

        await browser.close()

    db.close()


def run_self_test() -> None:
    """Offline self-test verifying SQLite history, URL rotation, proxy parsing, and Telegram formatting."""
    logger.info("Running offline self-test of anti-block helpers, SQLite deduplication & Telegram formatter...")
    test_db_path = ":memory:"
    db = DealDatabase(test_db_path)

    # Verify proxy parser
    proxy_cfg = parse_proxy_config("http://egyptuser:secret123@gate.proxy.com:8080")
    assert proxy_cfg == {
        "server": "http://gate.proxy.com:8080",
        "username": "egyptuser",
        "password": "secret123",
    }, "Proxy parser failed"

    # Verify staggered URL rotation
    c0 = select_cycle_urls(AMAZON_EG_DEAL_URLS, 0, 2)
    c1 = select_cycle_urls(AMAZON_EG_DEAL_URLS, 1, 2)
    assert len(c0) == 2 and len(c1) == 2 and c0 != c1, "URL sharding rotation failed"

    # Verify Amazon.eg seller filter URL and buy box seller detection (markup from amazon.eg, Nov 2026)
    assert all(f"p_6:{AMAZON_EG_SELLER_ID}" in urllib.parse.unquote(u) for u in AMAZON_EG_DEAL_URLS), "Seller filter missing"
    sold_by_amazon = """
    <div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-label"><span>Shipper / Seller</span></div>
    <div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-text">
      <span class="a-size-small offer-display-feature-text-message">Amazon.eg</span></div>
    """
    third_party_fba = """
    <div offer-display-feature-name="desktop-fulfiller-info" class="offer-display-feature-text">
      <span class="a-size-small offer-display-feature-text-message">Amazon</span></div>
    <div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-label"><span>Sold by</span></div>
    <div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-text">
      <a id="sellerProfileTriggerId" class="a-link-normal offer-display-feature-text-message">فرندز</a></div>
    """
    assert amazon_buybox_sold_by_amazon(sold_by_amazon) is True, "Amazon.eg-sold offer not recognised"
    assert amazon_buybox_sold_by_amazon(third_party_fba) is False, "Third-party FBA offer must be rejected"
    assert amazon_buybox_sold_by_amazon("<html><body>Currently unavailable.</body></html>") is None

    # Verify Noon PLP product box parsing (markup captured from noon.com/egypt-en, Nov 2026)
    noon_html = """
    <a href="/egypt-en/galaxy-a36-5g-dual-sim-awesome-white-8gb-ram-256gb/N70158921V/p/?o=e8faa407200c3bec">
      <img class="_placeholderContainer_a0aok_7" src="https://f.nooncdn.com/s/app/com/noon/images/media-placeholder.svg" alt="placeholder">
      <img class="_productImage_1yztz_82" src="https://f.nooncdn.com/p/pzsku/Z1/45/_/1/a.jpg?width=800" alt="Galaxy - Image 1">
      <h2 data-qa="plp-product-box-name" title="Samsung Galaxy A36 5G Dual SIM Awesome White 8GB RAM 256GB">Samsung Galaxy A36 5G</h2>
      <div class="_textCtr_6uyte_16">4.4</div>
      <div data-qa="plp-product-box-price">
        <div><span>EGP</span><strong class="_amount_a2gq6_61">23,999</strong></div>
        <div><span class="_oldPrice_a2gq6_87 strikeThrough">27,999</span><span>14% Off</span></div>
      </div>
      <div data-qa="product-noon-express"></div>
    </a>
    <a href="/egypt-en/a200-plus-dual-sim-nightly-blue/N70377906V/p/">
      <h2 data-qa="plp-product-box-name" title="ITEL A200 Plus">ITEL A200 Plus</h2>
      <div data-qa="plp-product-box-price"><div><span>EGP</span><strong>7,888</strong></div></div>
    </a>
    """
    # Verify every scanned page has a category and captions show it
    assert all(u in CATEGORY_BY_URL for u in AMAZON_EG_DEAL_URLS + NOON_EG_DEAL_URLS), "URL without category"
    tagged = {"x": DealItem("NOON-X", "Noon Egypt", "Vitamin C", 80.0, 100.0, 20.0, "https://www.noon.com/")}
    tag_new_deals_with_category(tagged, NOON_EG_DEAL_URLS[-1])
    assert "Health &amp; Nutrition #HealthNutrition" in format_telegram_caption(tagged["x"]), "Category missing from caption"

    noon_deals = parse_noon_product_boxes(BeautifulSoup(noon_html, "lxml"))
    assert len(noon_deals) == 1, f"Noon parser expected 1 discounted card, got {len(noon_deals)}"
    nd = noon_deals[0]
    assert nd.product_id == "NOON-N70158921V", nd.product_id
    assert (nd.current_price, nd.original_price, nd.rating) == (23999.0, 27999.0, 4.4), nd
    assert nd.title.startswith("Samsung Galaxy A36") and nd.is_express_or_fba, nd
    assert nd.image_url and "placeholder" not in nd.image_url, nd.image_url
    assert nd.url == "https://www.noon.com/egypt-en/galaxy-a36-5g-dual-sim-awesome-white-8gb-ram-256gb/N70158921V/p/", nd.url

    sample_deals = [
        DealItem(
            product_id="AMZ-B09V3KXJPB",
            platform="Amazon Egypt",
            title="Samsung Galaxy A55 5G Dual SIM Smartphone, 8GB RAM, 256GB Storage - Awesome Navy",
            current_price=16499.00,
            original_price=21999.00,
            discount_pct=25.0,
            url="https://www.amazon.eg/-/en/dp/B09V3KXJPB",
            image_url="https://m.media-amazon.com/images/I/71ee+5V4ZRL._AC_SL1500_.jpg",
            coupon_text="Save extra EGP 500 with Vodafone Cash",
            is_express_or_fba=True,
            rating=4.6,
        ),
        DealItem(
            product_id="NOON-N53432547A",
            platform="Noon Egypt",
            title="Tornado Digital Air Fryer 4.2 Liter, 1400 Watt, Black - THF-1450D-BK",
            current_price=3299.00,
            original_price=4999.00,
            discount_pct=34.0,
            url="https://www.noon.com/egypt-en/tornado-air-fryer/N53432547A/p/",
            is_express_or_fba=True,
            rating=4.5,
        ),
    ]

    for d in sample_deals:
        assert passes_filters(d), f"Sample deal {d.product_id} should pass filters"
        qualifies_first = db.evaluate_and_record(d)
        assert qualifies_first is True, "First sighting must qualify for notification"
        send_telegram_alert(d)
        db.mark_notified(d.product_id, d.current_price)

        # Immediate duplicate check must be suppressed
        qualifies_duplicate = db.evaluate_and_record(d)
        assert qualifies_duplicate is False, "Duplicate price must be suppressed"

    db.close()
    logger.info("Self-test passed! Anti-block stealth config, URL rotation, and SQLite deduplication verified.")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        DRY_RUN = True
        run_self_test()
    elif "--loop" in sys.argv or os.getenv("CONTINUOUS_LOOP", "false").lower() in ("1", "true", "yes"):
        asyncio.run(run_tracker(loop_mode=True))
    else:
        asyncio.run(run_tracker(loop_mode=False))