"""Website post fetcher using Playwright for dynamic sites."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright, Browser, Page

from web_crawler.config import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_BATCH_DELAY,
    HEADLESS,
    TIMEOUT,
    USER_AGENT,
)
from app.autonomous.self_healing_scraper import SelfHealingScraper, ScraperFailure
from app.autonomous.state_manager import AutonomousStateManager
from services.ollama_service import AsyncOllamaAnalyzer
from app.autonomous.budget_guard import OllamaBudgetGuard

logger = logging.getLogger(__name__)

# sameSite values accepted by Playwright
_SAME_SITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",   # Chrome extension export
    "unspecified": "Lax",       # Chrome extension export
    "": "Lax",
}


def _build_linkedin_navigation_urls(raw_url: str) -> list[str]:
    """Build stable LinkedIn jobs URLs to reduce redirect loops.

    Removes volatile query params (e.g. currentJobId/origin/spellCorrectionEnabled)
    and returns multiple safe fallbacks.
    """
    base_search = "https://www.linkedin.com/jobs/search/"
    parsed = urlparse(raw_url or "")
    query = parse_qs(parsed.query)

    allowed_keys = {
        "keywords",
        "location",
        "f_WT",
        "f_TPR",
        "f_E",
        "f_JT",
        "f_AL",
        "f_LF",
        "f_WRA",
        "distance",
        "geoId",
        "start",
    }

    cleaned_query: dict[str, str] = {}
    for k, vals in query.items():
        if k in allowed_keys and vals:
            cleaned_query[k] = vals[-1]

    # Ensure stable defaults for job scraping when URL is too sparse
    if "keywords" not in cleaned_query:
        cleaned_query["keywords"] = "software engineer"
    if "location" not in cleaned_query:
        cleaned_query["location"] = "Worldwide"
    if "f_WT" not in cleaned_query:
        cleaned_query["f_WT"] = "2"

    urls: list[str] = []
    urls.append(f"{base_search}?{urlencode(cleaned_query)}")
    urls.append("https://www.linkedin.com/jobs/search/?keywords=software%20engineer&location=Worldwide&f_WT=2")
    urls.append("https://www.linkedin.com/jobs/")

    # Include the raw URL last as final fallback.
    if raw_url:
        urls.append(raw_url)

    # Dedupe while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


async def _goto_with_linkedin_fallback(
    page: Page,
    url: str,
    site_type: str,
    timeout: int,
    wait_until: str,
) -> str:
    """Navigate with LinkedIn-specific fallback URLs on redirect loops."""
    if site_type != "linkedin":
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        return url

    candidates = _build_linkedin_navigation_urls(url)
    last_error: Optional[Exception] = None

    for idx, candidate in enumerate(candidates, start=1):
        try:
            await page.goto(candidate, wait_until=wait_until, timeout=timeout)

            # LinkedIn may silently redirect to login; try next candidate.
            final_url = (page.url or "").lower()
            if "/login" in final_url or "session_redirect" in final_url:
                logger.warning(
                    "[FETCH LINKEDIN] Candidate %d redirected to login: %s",
                    idx,
                    page.url,
                )
                continue

            if idx > 1:
                logger.info("[FETCH LINKEDIN] Navigation fallback succeeded with: %s", candidate)
            return candidate
        except Exception as e:
            last_error = e
            logger.warning("[FETCH LINKEDIN] Navigation candidate %d failed: %s", idx, e)
            continue

    if last_error:
        raise last_error
    await page.goto(url, wait_until=wait_until, timeout=timeout)
    return url


def _parse_relative_datetime(text: str) -> Optional[datetime]:
    """Parse relative/absolute datetime text from job sites.

    Supports common LinkedIn strings like "3 days ago", "Reposted 2 weeks ago",
    and Chinese forms like "3天前".
    """
    if not text:
        return None

    raw = text.strip()
    t = raw.lower()
    now = datetime.utcnow()

    if "just now" in t or "刚刚" in raw:
        return now
    if "yesterday" in t or "昨天" in raw:
        return now - timedelta(days=1)
    if "today" in t or "今天" in raw:
        return now

    m = re.search(r"(\d+)\s*(minute|min|hour|day|week|month|year)s?\s+ago", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("minute", "min"):
            return now - timedelta(minutes=n)
        if unit == "hour":
            return now - timedelta(hours=n)
        if unit == "day":
            return now - timedelta(days=n)
        if unit == "week":
            return now - timedelta(weeks=n)
        if unit == "month":
            return now - timedelta(days=30 * n)
        if unit == "year":
            return now - timedelta(days=365 * n)

    m = re.search(r"(\d+)\s*(分钟|小时|天|周|个月|月|年)前", raw)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "分钟":
            return now - timedelta(minutes=n)
        if unit == "小时":
            return now - timedelta(hours=n)
        if unit == "天":
            return now - timedelta(days=n)
        if unit == "周":
            return now - timedelta(weeks=n)
        if unit in ("个月", "月"):
            return now - timedelta(days=30 * n)
        if unit == "年":
            return now - timedelta(days=365 * n)

    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass

    return None


def _sanitize_cookies(cookies: list[dict]) -> list[dict]:
    """Normalise cookie fields so Playwright accepts them.

    Browser extensions (e.g. Chrome's cookie export) use different field names
    and value formats than Playwright expects:
      - expirationDate (float)  →  expires (int)
      - sameSite "unspecified" / "no_restriction" / lowercase  →  Strict|Lax|None
      - hostOnly / session / storeId are stripped (Playwright doesn't accept them)
    """
    allowed_keys = {
        "name", "value", "domain", "path", "expires",
        "httpOnly", "secure", "sameSite",
    }
    result = []
    for raw in cookies:
        cookie: dict = {}

        for k, v in raw.items():
            # Rename expirationDate → expires and cast to int
            if k == "expirationDate":
                cookie["expires"] = int(v)
            elif k in allowed_keys:
                cookie[k] = v
            # All other keys (hostOnly, session, storeId, …) are dropped

        # Normalise sameSite
        same_site = str(cookie.get("sameSite", "")).strip()
        cookie["sameSite"] = _SAME_SITE_MAP.get(same_site.lower(), "Lax")

        result.append(cookie)
    return result


async def fetch_posts(
    url: str,
    site_type: str,
    days_back: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_delay: float = DEFAULT_BATCH_DELAY,
    cookies: list[dict] | None = None,
    analyzer: Optional[AsyncOllamaAnalyzer] = None,
    budget_guard: Optional[OllamaBudgetGuard] = None,
    state_manager: Optional[AutonomousStateManager] = None,
    proxy_pool: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch posts from a website with batch processing.

    Args:
        url: The website URL to fetch from.
        site_type: The site type (e.g., 'v2ex', 'eleduck') for parser selection.
        days_back: Extra days before today to include (0 = today only).
        batch_size: Posts per page/batch (default: 20).
        batch_delay: Seconds to wait between batches (default: 2.0).
        cookies: Optional list of cookies for authenticated requests.
        analyzer: Optional AsyncOllamaAnalyzer for self-healing selector recovery.
        budget_guard: Optional OllamaBudgetGuard for LLM cost management.
        state_manager: Optional AutonomousStateManager for persisting state.
        proxy_pool: Optional list of proxy URLs for rotation.

    Returns:
        List of post dictionaries.
    """
    posts: list[dict[str, Any]] = []
    nav_url = url
    if site_type == "linkedin":
        nav_url = _build_linkedin_navigation_urls(url)[0]
        if nav_url != url:
            logger.info("[FETCH LINKEDIN] Normalized URL for navigation: %s", nav_url)

    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_date = today_midnight - timedelta(days=days_back)

    # Use self-healing scraper if analyzer and budget_guard are provided
    if analyzer and budget_guard:
        try:
            scraper = SelfHealingScraper(
                analyzer=analyzer,
                budget_guard=budget_guard,
                state_manager=state_manager,
                proxy_pool=proxy_pool,
                max_attempts=3,
            )
            await scraper.initialize()

            # Define extraction function based on site type
            # NOTE: cookies are injected into the context by the scraper before
            # the first navigation — no need to call add_cookies here.
            async def extract_fn(page):

                # Set longer timeout for heavy SPA sites
                page_timeout = 120000 if site_type in ("bossjob", "linkedin") else TIMEOUT
                page.set_default_timeout(page_timeout)

                # Navigate to the URL
                logger.info(f"[FETCH] Navigating to {nav_url}")
                wait_until = "commit" if site_type in ("bossjob", "linkedin") else "networkidle"
                await _goto_with_linkedin_fallback(
                    page=page,
                    url=nav_url,
                    site_type=site_type,
                    timeout=page_timeout,
                    wait_until=wait_until,
                )

                # Site-specific parsing
                if site_type == "v2ex":
                    return await _fetch_v2ex_posts(page, cutoff_date, batch_size, batch_delay)
                elif site_type == "eleduck":
                    return await _fetch_eleduck_posts(page, cutoff_date, batch_size, batch_delay)
                elif site_type == "bossjob":
                    return await _fetch_bossjob_posts(page, cutoff_date, batch_size, batch_delay)
                elif site_type == "linkedin":
                    return await _fetch_linkedin_posts(page, cutoff_date, batch_size, batch_delay)
                else:
                    logger.error(f"[FETCH] Unknown site type: {site_type}")
                    return []

            sanitized_cookies = _sanitize_cookies(cookies) if cookies else None
            posts = await scraper.scrape(nav_url, site_type, extract_fn, cookies=sanitized_cookies)
            return posts

        except ScraperFailure as e:
            logger.error(f"[FETCH] Self-healing scraper failed for {nav_url}: {e}")
            # Fall through to legacy fetcher as backup
        except Exception as e:
            logger.error(f"[FETCH] Self-healing scraper error for {nav_url}: {e}", exc_info=True)
            # Fall through to legacy fetcher as backup

    # Legacy fetcher (fallback)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )

            # Add cookies if provided (for authenticated sites)
            if cookies:
                await context.add_cookies(_sanitize_cookies(cookies))
                logger.info(f"[FETCH] Added {len(cookies)} cookies for authentication")

            page = await context.new_page()
            # Set longer timeout for heavy SPA sites
            page_timeout = 120000 if site_type in ("bossjob", "linkedin") else TIMEOUT
            page.set_default_timeout(page_timeout)

            # Navigate to the URL
            logger.info(f"[FETCH] Navigating to {nav_url}")
            # Use less strict wait for SPA sites with lots of ads/analytics
            wait_until = "commit" if site_type in ("bossjob", "linkedin") else "networkidle"
            await _goto_with_linkedin_fallback(
                page=page,
                url=nav_url,
                site_type=site_type,
                timeout=page_timeout,
                wait_until=wait_until,
            )

            # Site-specific parsing
            if site_type == "v2ex":
                posts = await _fetch_v2ex_posts(page, cutoff_date, batch_size, batch_delay)
            elif site_type == "eleduck":
                posts = await _fetch_eleduck_posts(page, cutoff_date, batch_size, batch_delay)
            elif site_type == "bossjob":
                posts = await _fetch_bossjob_posts(page, cutoff_date, batch_size, batch_delay)
            elif site_type == "linkedin":
                posts = await _fetch_linkedin_posts(page, cutoff_date, batch_size, batch_delay)
            else:
                logger.error(f"[FETCH] Unknown site type: {site_type}")

            await context.close()
            await browser.close()

    except Exception as e:
        logger.error(f"[FETCH] Error fetching from {nav_url}: {e}", exc_info=True)

    return posts


async def _fetch_v2ex_posts(
    page: Page,
    cutoff_date: datetime,
    batch_size: int,
    batch_delay: float,
) -> list[dict[str, Any]]:
    """Fetch posts from V2EX.

    Args:
        page: Playwright page instance.
        cutoff_date: Date cutoff for posts.
        batch_size: Posts per page.
        batch_delay: Delay between pages.

    Returns:
        List of post dictionaries.
    """
    posts: list[dict[str, Any]] = []
    page_num = 1
    reached_cutoff = False

    try:
        while not reached_cutoff:
            logger.info(f"[FETCH V2EX] Page {page_num}")

            # Wait for posts to load
            await page.wait_for_selector(".item", timeout=10000)

            # Extract posts
            post_elements = await page.query_selector_all(".item")
            batch_count = 0

            for element in post_elements:
                if batch_count >= batch_size:
                    break

                try:
                    # Extract post data
                    title_elem = await element.query_selector(".topic-link")
                    post_id = await element.get_attribute("data-id")
                    title = await title_elem.inner_text() if title_elem else None

                    # Extract date
                    date_elem = await element.query_selector(".ago")
                    date_text = await date_elem.inner_text() if date_elem else None
                    post_date = _parse_v2ex_date(date_text) if date_text else None

                    # Extract author
                    author_elem = await element.query_selector(".user-name")
                    author = await author_elem.inner_text() if author_elem else None

                    # Extract URL
                    post_url = await title_elem.get_attribute("href") if title_elem else None
                    if post_url and not post_url.startswith("http"):
                        post_url = f"https://v2ex.com{post_url}"

                    # Check cutoff
                    if post_date and post_date < cutoff_date:
                        reached_cutoff = True
                        break

                    if title and post_id:
                        posts.append({
                            "id": post_id,
                            "title": title,
                            "url": post_url,
                            "author": author,
                            "date": post_date,
                            "text": title,  # V2EX posts need detail page fetch for full text
                        })
                        batch_count += 1

                except Exception as e:
                    logger.warning(f"[FETCH V2EX] Error parsing post: {e}")
                    continue

            # Check if we need to go to next page
            if batch_count < batch_size or reached_cutoff:
                break

            # Look for next page button
            next_button = await page.query_selector(".page_normal:last-child")
            if next_button:
                await next_button.click()
                await asyncio.sleep(batch_delay)
                page_num += 1
            else:
                break

    except Exception as e:
        logger.error(f"[FETCH V2EX] Error: {e}", exc_info=True)

    return posts


async def _fetch_eleduck_posts(
    page: Page,
    cutoff_date: datetime,
    batch_size: int,
    batch_delay: float,
) -> list[dict[str, Any]]:
    """Fetch posts from 电鸭社区.

    Args:
        page: Playwright page instance.
        cutoff_date: Date cutoff for posts.
        batch_size: Posts per page.
        batch_delay: Delay between pages.

    Returns:
        List of post dictionaries.
    """
    posts: list[dict[str, Any]] = []
    page_num = 1
    reached_cutoff = False

    try:
        while not reached_cutoff:
            logger.info(f"[FETCH ELEDUCK] Page {page_num}")

            # Wait for posts to load
            await page.wait_for_selector(".post-item", timeout=10000)

            # Extract posts
            post_elements = await page.query_selector_all(".post-item")
            batch_count = 0

            for element in post_elements:
                if batch_count >= batch_size:
                    break

                try:
                    # Extract post data
                    title_elem = await element.query_selector(".post-title a")
                    title = await title_elem.inner_text() if title_elem else None

                    # Extract post ID from URL
                    post_url = await title_elem.get_attribute("href") if title_elem else None
                    post_id = post_url.split("/")[-1] if post_url else None

                    # Extract date
                    date_elem = await element.query_selector(".post-meta .time")
                    date_text = await date_elem.inner_text() if date_elem else None
                    post_date = _parse_eleduck_date(date_text) if date_text else None

                    # Extract author
                    author_elem = await element.query_selector(".post-meta .author")
                    author = await author_elem.inner_text() if author_elem else None

                    # Check cutoff
                    if post_date and post_date < cutoff_date:
                        reached_cutoff = True
                        break

                    if title and post_id:
                        posts.append({
                            "id": post_id,
                            "title": title,
                            "url": post_url,
                            "author": author,
                            "date": post_date,
                            "text": title,  # 电鸭 posts need detail page fetch for full text
                        })
                        batch_count += 1

                except Exception as e:
                    logger.warning(f"[FETCH ELEDUCK] Error parsing post: {e}")
                    continue

            # Check if we need to go to next page
            if batch_count < batch_size or reached_cutoff:
                break

            # Look for next page button
            next_button = await page.query_selector(".pagination .next")
            if next_button:
                await next_button.click()
                await asyncio.sleep(batch_delay)
                page_num += 1
            else:
                break

    except Exception as e:
        logger.error(f"[FETCH ELEDUCK] Error: {e}", exc_info=True)

    return posts


def _parse_v2ex_date(date_text: str) -> datetime:
    """Parse V2EX date string (e.g., "2小时前", "3天前").

    Args:
        date_text: Date string from V2EX.

    Returns:
        Datetime object.
    """
    now = datetime.now(timezone.utc)

    if "分钟前" in date_text:
        minutes = int(date_text.replace("分钟前", ""))
        return now - timedelta(minutes=minutes)
    elif "小时前" in date_text:
        hours = int(date_text.replace("小时前", ""))
        return now - timedelta(hours=hours)
    elif "天前" in date_text:
        days = int(date_text.replace("天前", ""))
        return now - timedelta(days=days)
    else:
        # Try to parse as regular date
        try:
            return datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except:
            return now


def _parse_eleduck_date(date_text: str) -> datetime:
    """Parse 电鸭 date string (e.g., "2小时前", "3天前").

    Args:
        date_text: Date string from 电鸭.

    Returns:
        Datetime object.
    """
    now = datetime.now(timezone.utc)

    if "分钟前" in date_text:
        minutes = int(date_text.replace("分钟前", ""))
        return now - timedelta(minutes=minutes)
    elif "小时前" in date_text:
        hours = int(date_text.replace("小时前", ""))
        return now - timedelta(hours=hours)
    elif "天前" in date_text:
        days = int(date_text.replace("天前", ""))
        return now - timedelta(days=days)
    else:
        # Try to parse as regular date
        try:
            return datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except:
            return now


async def _fetch_bossjob_posts(
    page: Page,
    cutoff_date: datetime,
    batch_size: int,
    batch_delay: float,
) -> list[dict[str, Any]]:
    """Fetch job posts from bossjob.us with detail page scraping.

    Uses JavaScript extraction via page.evaluate() to avoid stale element handles.
    First extracts all job data (title, company, item_id) from listing page,
    then navigates to each job's detail page individually.

    Args:
        page: Playwright page instance.
        cutoff_date: Date cutoff for posts (not used for bossjob, always fetch).
        batch_size: Posts per page (used as max jobs per page).
        batch_delay: Delay between job detail fetches.

    Returns:
        List of post dictionaries with full job details.
    """
    posts: list[dict[str, Any]] = []
    max_pages = 3  # Fetch only 3 pages for recent jobs
    jobs_per_page = min(batch_size, 10)  # Bossjob shows ~10 jobs per page

    try:
        for page_num in range(1, max_pages + 1):
            logger.info(f"[FETCH BOSSJOB] Page {page_num}/{max_pages}")

            # Wait for job cards to load
            await asyncio.sleep(4)

            # Extract job data using JavaScript (returns plain dicts with card index)
            jobs_data = await page.evaluate("""
                () => {
                    const jobs = [];
                    const selectors = [
                        '.yolo-technology-jobCard',
                        '[class*="index_pc_listItem"]',
                        '[data-sentry-component="JobCardPc"]',
                        '[class*="JobCard"]',
                        '.job-card'
                    ];
                    
                    for (const selector of selectors) {
                        const cards = document.querySelectorAll(selector);
                        if (cards.length > 0) {
                            cards.forEach((card, index) => {
                                // Skip cards from 'Top Web3 Companies' section
                                let parent = card.parentElement;
                                let isCompaniesSection = false;
                                while (parent) {
                                    if (parent.className && (parent.className.includes('companies') || parent.className.includes('style_companies'))) {
                                        isCompaniesSection = true;
                                        break;
                                    }
                                    parent = parent.parentElement;
                                }
                                if (isCompaniesSection) {
                                    return; // Skip this card (return from forEach iteration)
                                }

                                const itemId = card.getAttribute('data-item-id');

                                // Try multiple title selectors
                                const titleSelectors = [
                                    'h3[class*="jobHireTopTitle"]',
                                    'h3 span',
                                    'h3'
                                ];
                                let title = '';
                                for (const ts of titleSelectors) {
                                    const titleEl = card.querySelector(ts);
                                    if (titleEl) {
                                        title = titleEl.innerText?.trim() || '';
                                        if (title) break;
                                    }
                                }
                                
                                // Try multiple company selectors
                                const companySelectors = [
                                    '[class*="jobHireRecruiterName"]',
                                    '[class*="company-name"]',
                                    '[class*="Company"]'
                                ];
                                let company = '';
                                for (const cs of companySelectors) {
                                    const companyEl = card.querySelector(cs);
                                    if (companyEl) {
                                        company = companyEl.innerText?.trim() || '';
                                        if (company) break;
                                    }
                                }
                                
                                if (itemId && title) {
                                    jobs.push({
                                        item_id: itemId,
                                        title: title,
                                        company: company,
                                        card_index: index,
                                        card_selector: selector
                                    });
                                }
                            });
                            break;
                        }
                    }
                    return jobs;
                }
            """)

            if not jobs_data:
                logger.warning(f"[FETCH BOSSJOB] No job cards found on page {page_num}, stopping")
                break

            logger.info(f"[FETCH BOSSJOB] Found {len(jobs_data)} jobs on page {page_num}")

            # Iterate through extracted job data (plain Python dicts, no stale handles)
            for idx, job_data in enumerate(jobs_data[:jobs_per_page]):
                try:
                    title = job_data.get('title', '').strip()
                    company = job_data.get('company', '').strip()
                    item_id = job_data.get('item_id', '')
                    card_index = job_data.get('card_index', 0)
                    card_selector = job_data.get('card_selector', '')

                    if not title:
                        continue

                    logger.info(f"[FETCH BOSSJOB] -------------------------------")
                    logger.info(f"[FETCH BOSSJOB] Job {idx+1}/{len(jobs_data[:jobs_per_page])}: {title[:50]}... (item_id: {item_id})")

                    # Construct job URL for reference
                    job_url = f"https://bossjob.us/en-us/job/{item_id}"

                    # Click the job card to open modal/sidebar
                    card_elements = await page.query_selector_all(card_selector)
                    if card_index < len(card_elements):
                        await card_elements[card_index].click()
                        await asyncio.sleep(2)  # Wait for modal to load
                    else:
                        logger.warning(f"[FETCH BOSSJOB] Card index {card_index} out of range, skipping")
                        continue

                    # Wait for detail page content - wait for MainSection specifically
                    try:
                        await page.wait_for_selector("[class*='MainSection_pc_mainSection']", timeout=10000)
                    except Exception:
                        # If MainSection not found, try fallback selectors
                        try:
                            await page.wait_for_selector("[class*='detail'], [class*='Detail'], [data-testid='job-detail'], .job-description, [class*='description'], [class*='Description']", timeout=5000)
                        except Exception:
                            pass  # Continue even if detail selector not found

                    # Extract full details from detail page using JavaScript
                    detail_data = await page.evaluate("""
                        () => {
                            const result = {
                                description: '',
                                requirements: '',
                                location: '',
                                salary: ''
                            };

                            // Target the main job detail section
                            const mainSection = document.querySelector('[class*="MainSection_pc_mainSection"]');
                            const useMainSection = !!mainSection;

                            // Filter out warning messages
                            const isWarningText = (text) => {
                                const warningPhrases = ['mobile device', 'desktop browser', 'Download App', 'features may not work'];
                                return warningPhrases.some(phrase => text.toLowerCase().includes(phrase.toLowerCase()));
                            };

                            // Check if element is inside Similar Jobs section
                            const isSimilarJobsSection = (el) => {
                                let parent = el.parentElement;
                                while (parent) {
                                    if (parent.className && (parent.className.includes('similarJobs') || parent.className.includes('SimilarJobs'))) {
                                        return true;
                                    }
                                    parent = parent.parentElement;
                                }
                                return false;
                            };

                            // Check if element is inside Job List section
                            const isJobListSection = (el) => {
                                let parent = el.parentElement;
                                while (parent) {
                                    if (parent.className && (parent.className.includes('jobList') || parent.className.includes('JobList'))) {
                                        return true;
                                    }
                                    parent = parent.parentElement;
                                }
                                return false;
                            };

                            // Check if element is inside Top Web3 Companies section
                            const isCompaniesSection = (el) => {
                                let parent = el.parentElement;
                                while (parent) {
                                    if (parent.className && parent.className.includes('companies')) {
                                        return true;
                                    }
                                    parent = parent.parentElement;
                                }
                                return false;
                            };

                            // Combined filter for all unwanted sections
                            const isUnwantedSection = (el) => {
                                return isSimilarJobsSection(el) || isJobListSection(el) || isCompaniesSection(el);
                            };

                            // Description - use more specific selectors
                            const descSelectors = [
                                '[class*="job-description"]',
                                '[class*="JobDescription"]',
                                '[data-testid="job-description"]',
                                '[class*="jobDetail"]',
                                '[class*="job-detail"]',
                                '[class*="detailContent"]',
                                '[class*="detail-content"]',
                                '[class*="Desc_pc_descContent"]'
                            ];
                            for (const sel of descSelectors) {
                                const el = useMainSection ? mainSection.querySelector(sel) : document.querySelector(sel);
                                if (el && !isUnwantedSection(el)) {
                                    const text = el.innerText?.trim() || '';
                                    if (text.length > 50 && !isWarningText(text)) {
                                        result.description = text;
                                        break;
                                    }
                                }
                            }
                            
                            // If no description found, try broader selectors but filter warnings
                            if (!result.description) {
                                const broadDescSelectors = [
                                    '.description',
                                    '[class*="description"]',
                                    '[class*="content"]',
                                    'article',
                                    'main'
                                ];
                                for (const sel of broadDescSelectors) {
                                    const el = useMainSection ? mainSection.querySelector(sel) : document.querySelector(sel);
                                    if (el && !isUnwantedSection(el)) {
                                        const text = el.innerText?.trim() || '';
                                        if (text.length > 100 && !isWarningText(text)) {
                                            result.description = text;
                                            break;
                                        }
                                    }
                                }
                            }
                            
                            // Requirements
                            const reqSelectors = [
                                '[class*="requirement"]',
                                '[class*="Requirement"]',
                                '[class*="qualification"]',
                                '[class*="Qualification"]',
                                '[class*="skill"]',
                                '[class*="Skill"]'
                            ];
                            for (const sel of reqSelectors) {
                                const el = useMainSection ? mainSection.querySelector(sel) : document.querySelector(sel);
                                if (el && !isUnwantedSection(el)) {
                                    const text = el.innerText?.trim() || '';
                                    if (text.length > 20 && !isWarningText(text)) {
                                        result.requirements = text;
                                        break;
                                    }
                                }
                            }
                            
                            // Location
                            const locSelectors = [
                                '[class*="location"]',
                                '[class*="Location"]',
                                '[data-testid="location"]',
                                '[class*="city"]',
                                '[class*="City"]'
                            ];
                            for (const sel of locSelectors) {
                                const el = useMainSection ? mainSection.querySelector(sel) : document.querySelector(sel);
                                if (el && !isUnwantedSection(el)) {
                                    const text = el.innerText?.trim() || '';
                                    if (text) {
                                        result.location = text;
                                        break;
                                    }
                                }
                            }
                            
                            // Salary
                            const salSelectors = [
                                '[class*="salary"]',
                                '[class*="Salary"]',
                                '[data-testid="salary"]'
                            ];
                            for (const sel of salSelectors) {
                                const el = useMainSection ? mainSection.querySelector(sel) : document.querySelector(sel);
                                if (el && !isUnwantedSection(el)) {
                                    const text = el.innerText?.trim() || '';
                                    if (text) {
                                        result.salary = text;
                                        break;
                                    }
                                }
                            }
                            
                            return result;
                        }
                    """)

                    description = detail_data.get('description', '')
                    requirements = detail_data.get('requirements', '')
                    location = detail_data.get('location', '')
                    salary = detail_data.get('salary', '')

                    # Construct full text
                    full_text_parts = [title]
                    if company:
                        full_text_parts.append(f"Company: {company}")
                    if salary:
                        full_text_parts.append(f"Salary: {salary}")
                    if location:
                        full_text_parts.append(f"Location: {location}")
                    if requirements:
                        full_text_parts.append(f"Requirements: {requirements}")
                    if description:
                        full_text_parts.append(f"Description: {description}")

                    full_text = "\n\n".join(full_text_parts)

                    # Create analysis text with only description + requirements for Ollama
                    analysis_parts = []
                    if description:
                        analysis_parts.append(description)
                    if requirements:
                        analysis_parts.append(requirements)
                    analysis_text = "\n\n".join(analysis_parts)

                    logger.info(f"[FETCH BOSSJOB] Full text:\n{full_text}")
                    logger.info(f"[FETCH BOSSJOB] Analysis text length: {len(analysis_text)} chars")

                    # Create post entry
                    post_id = item_id if item_id else f"bossjob_{page_num}_{idx}"
                    posts.append({
                        "id": post_id,
                        "title": title,
                        "url": job_url,
                        "company": company,
                        "date": datetime.now(),
                        "text": full_text,
                        "analysis_text": analysis_text,  # For Ollama analysis
                        "salary": salary,
                        "location": location,
                        "requirements": requirements,
                        "description": description,
                    })

                    logger.info(f"[FETCH BOSSJOB] ✓ Extracted: {title[:50]}... ({len(full_text)} chars)")

                    # Close modal by pressing Escape
                    await page.keyboard.press('Escape')
                    await asyncio.sleep(batch_delay)

                except Exception as e:
                    logger.warning(f"[FETCH BOSSJOB] Error processing job {idx+1}: {e}")
                    # Try to close modal in case we're stuck
                    try:
                        await page.keyboard.press('Escape')
                    except Exception:
                        pass
                    continue

            # Check if there's a next page button
            try:
                # Bossjob pagination: next button is a span with Pagination_actionBtn class
                # Use partial class matching for Next.js hash classnames
                next_selectors = [
                    "[class*='Pagination_actionBtn'][style*='rotate(0deg)']",
                    "[class*='Pagination_actionBtn']:not([data-disabled='true'])",
                    "span[class*='Pagination_actionBtn']:last-child",
                    "[class*='Pagination_actionBtn']",
                    "button[class*='next']",
                    "a[class*='next']"
                ]
                next_button = None
                for selector in next_selectors:
                    next_button = await page.query_selector(selector)
                    if next_button:
                        break

                if next_button:
                    # Check data-disabled attribute (Bossjob uses this instead of disabled)
                    is_disabled = await next_button.get_attribute("data-disabled")
                    if is_disabled == "true":
                        logger.info("[FETCH BOSSJOB] Next button disabled, reached last page")
                        break

                    # Click next page
                    await next_button.click()
                    await asyncio.sleep(batch_delay + 1)
                else:
                    # Alternative: click the next page number
                    next_page_num = await page.evaluate("""
                        () => {
                            const currentPage = document.querySelector('[data-checked="true"]');
                            if (currentPage) {
                                const nextSibling = currentPage.nextElementSibling;
                                if (nextSibling && nextSibling.getAttribute('data-checked') === 'false') {
                                    return nextSibling.innerText.trim();
                                }
                            }
                            return null;
                        }
                    """)
                    if next_page_num:
                        logger.info(f"[FETCH BOSSJOB] Clicking page number: {next_page_num}")
                        await page.click(f"[data-checked='false']:text('{next_page_num}')")
                        await asyncio.sleep(batch_delay + 1)
                    else:
                        # Fallback: try to increment page number in URL
                        current_url = page.url
                        if "page=" in current_url:
                            current_page = int(current_url.split("page=")[1].split("&")[0])
                            next_url = current_url.replace(f"page={current_page}", f"page={current_page + 1}")
                            await page.goto(next_url, wait_until="domcontentloaded")
                            await asyncio.sleep(batch_delay)
                        else:
                            logger.info("[FETCH BOSSJOB] No next button found, stopping")
                            break
            except Exception as e:
                logger.warning(f"[FETCH BOSSJOB] Error navigating to next page: {e}")
                break

    except Exception as e:
        logger.error(f"[FETCH BOSSJOB] Error: {e}", exc_info=True)

    logger.info(f"[FETCH BOSSJOB] Total jobs fetched: {len(posts)}")
    return posts


async def _fetch_linkedin_posts(
    page: Page,
    cutoff_date: datetime,
    batch_size: int,
    batch_delay: float,
) -> list[dict[str, Any]]:
    """Fetch job posts from linkedin.com/jobs with detail page scraping.

    Supports both public (unauthenticated) and authenticated (cookie-injected) sessions.
    Extracts job cards from the search listing, then navigates to each job detail page.

    Args:
        page: Playwright page instance (cookies already injected by caller if needed).
        cutoff_date: Date cutoff for posts (not strictly enforced; fetches recent pages).
        batch_size: Max jobs to process per page.
        batch_delay: Seconds to wait between job fetches.

    Returns:
        List of post dictionaries with full job details.
    """
    posts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    max_pages = 3
    jobs_per_page = min(batch_size, 25)

    card_wait_selectors = [
        "[data-job-id]",
        ".job-card-container",
        ".jobs-job-board-list__item",
        "li.scaffold-finite-scroll__content-item",
        "a[href*='/jobs/view/']",
    ]

    async def _wait_for_linkedin_cards() -> None:
        for selector in card_wait_selectors:
            try:
                await page.wait_for_selector(selector, timeout=9000)
                return
            except Exception:
                continue

        # collections 页面常见懒加载，滚动一次再等
        await page.evaluate("window.scrollTo(0, Math.max(500, document.body.scrollHeight * 0.25))")
        await asyncio.sleep(2)

    async def _is_login_page() -> bool:
        try:
            state = await page.evaluate(
                """
                () => {
                    const href = location.href || '';
                    const title = (document.title || '').toLowerCase();
                    return {
                        href,
                        title,
                        hasLoginForm: !!document.querySelector('form.login__form, #username, input[name="session_key"]'),
                    };
                }
                """
            )
            href = (state.get("href") or "").lower()
            title = (state.get("title") or "").lower()
            return (
                "/login" in href
                or "session_redirect" in href
                or "登录" in title
                or "sign in" in title
                or bool(state.get("hasLoginForm"))
            )
        except Exception:
            return False

    async def _try_public_jobs_fallback() -> None:
        fallback_urls = [
            "https://www.linkedin.com/jobs/search/?keywords=software%20engineer&location=Worldwide&f_WT=2",
            "https://www.linkedin.com/jobs/search/?keywords=software%20engineer",
            "https://www.linkedin.com/jobs/",
        ]
        for fallback_url in fallback_urls:
            try:
                logger.info(f"[FETCH LINKEDIN] Login redirect detected, trying fallback: {fallback_url}")
                await page.goto(fallback_url, wait_until="domcontentloaded", timeout=60000)
                await _wait_for_linkedin_cards()
                if not await _is_login_page():
                    logger.info(f"[FETCH LINKEDIN] Fallback page ready: {page.url}")
                    return
            except Exception:
                continue

    try:
        if await _is_login_page():
            await _try_public_jobs_fallback()

        for page_num in range(max_pages):
            logger.info(f"[FETCH LINKEDIN] Page {page_num + 1}/{max_pages}")
            await _wait_for_linkedin_cards()

            jobs_data = await page.evaluate("""
                () => {
                    const jobs = [];
                    const seen = new Set();

                    // Strategy A: Auth/SPA cards with data-job-id
                    const authCards = document.querySelectorAll('[data-job-id]');
                    authCards.forEach((card, index) => {
                        const jobId = card.getAttribute('data-job-id') || '';

                        const titleEl = card.querySelector(
                            '.job-card-list__title--link, .job-card-list__title, '
                            + 'strong.job-card-search__title, h3 a, h3, a[href*="/jobs/view/"]'
                        );
                        const title = (titleEl?.innerText || titleEl?.getAttribute('aria-label') || '').trim();

                        let jobUrl = titleEl?.href || '';
                        if (!jobUrl && jobId) {
                            jobUrl = `https://www.linkedin.com/jobs/view/${jobId}/`;
                        }

                        const companyEl = card.querySelector(
                            '.job-card-container__primary-description, '
                            + '.artdeco-entity-lockup__subtitle, '
                            + '[class*="company"]'
                        );
                        const company = (companyEl?.innerText || '').trim();

                        const locEl = card.querySelector(
                            '.job-card-container__metadata-item, '
                            + '.job-card-container__metadata-wrapper li, '
                            + '[class*="location"]'
                        );
                        const location = (locEl?.innerText || '').trim();

                        const postedEl = card.querySelector(
                            'time, .job-card-container__footer-item, .job-card-container__listed-time, '
                            + '[class*="listed"], [class*="time"]'
                        );
                        const postedText = (postedEl?.innerText || '').trim();

                        const dedupKey = jobId || jobUrl;
                        if (!dedupKey || seen.has(dedupKey)) return;
                        seen.add(dedupKey);

                        if (title || jobId) {
                            jobs.push({
                                job_id: jobId,
                                title,
                                company,
                                location,
                                job_url: jobUrl,
                                posted_text: postedText,
                                card_index: index,
                                is_auth: true,
                            });
                        }
                    });

                    // Strategy B: Fallback via links (/jobs/view/)
                    if (jobs.length === 0) {
                        const links = document.querySelectorAll('a[href*="/jobs/view/"]');
                        links.forEach((link, index) => {
                            const href = link.href || '';
                            const m = href.match(/\/jobs\/view\/(\d+)/);
                            const jobId = m ? m[1] : '';
                            const dedupKey = jobId || href;
                            if (!dedupKey || seen.has(dedupKey)) return;
                            seen.add(dedupKey);

                            const container = link.closest('li, article, div');
                            const title = (link.innerText || link.getAttribute('aria-label') || '').trim();

                            const companyEl = container?.querySelector(
                                '.base-search-card__subtitle, [class*="company"], [class*="subtitle"]'
                            );
                            const company = (companyEl?.innerText || '').trim();

                            const locEl = container?.querySelector(
                                '.job-search-card__location, [class*="location"], [class*="metadata"]'
                            );
                            const location = (locEl?.innerText || '').trim();

                            const postedEl = container?.querySelector(
                                'time, .job-search-card__listdate, [class*="time"], [class*="listed"]'
                            );
                            const postedText = (postedEl?.innerText || '').trim();

                            jobs.push({
                                job_id: jobId,
                                title,
                                company,
                                location,
                                job_url: href,
                                posted_text: postedText,
                                card_index: index,
                                is_auth: true,
                            });
                        });
                    }

                    return jobs;
                }
            """)

            if not jobs_data:
                debug = await page.evaluate("""
                    () => ({
                        url: location.href,
                        title: document.title,
                        dataJobIdCount: document.querySelectorAll('[data-job-id]').length,
                        jobViewLinkCount: document.querySelectorAll('a[href*="/jobs/view/"]').length,
                        cardCount: document.querySelectorAll('.job-card-container, .jobs-job-board-list__item, li.scaffold-finite-scroll__content-item').length,
                    })
                """)
                logger.warning(f"[FETCH LINKEDIN] No job cards found on page {page_num + 1}, debug={debug}")
                break

            logger.info(f"[FETCH LINKEDIN] Found {len(jobs_data)} jobs on page {page_num + 1}")

            for idx, job_data in enumerate(jobs_data[:jobs_per_page]):
                try:
                    title = job_data.get("title", "").strip()
                    company = job_data.get("company", "").strip()
                    location = job_data.get("location", "").strip()
                    job_id = job_data.get("job_id", "")
                    job_url = job_data.get("job_url", "").strip()

                    if not job_url and job_id:
                        job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
                    if job_url and not job_url.startswith("http"):
                        job_url = f"https://www.linkedin.com{job_url}"

                    dedup_key = job_id or job_url
                    if not dedup_key or dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)

                    if not title and not job_id:
                        continue

                    description = ""
                    salary = ""
                    requirements = ""

                    # 尝试点击列表项读取右侧详情（auth collections/search 均可）
                    card_elements = await page.query_selector_all("[data-job-id], .job-card-container, .jobs-job-board-list__item")
                    card_index = job_data.get("card_index", 0)
                    if card_index < len(card_elements):
                        try:
                            await card_elements[card_index].click()
                            await asyncio.sleep(1.5)
                            await page.wait_for_selector(
                                ".jobs-description__content, .jobs-box__html-content, .job-details-jobs-unified-top-card__job-insight",
                                timeout=7000,
                            )
                        except Exception:
                            pass

                    detail_data = await page.evaluate("""
                        () => {
                            const result = { description: '', salary: '' };

                                                        const extractTimeTexts = () => {
                                                                const nodes = Array.from(document.querySelectorAll(
                                                                    '.job-details-jobs-unified-top-card__job-insight, '
                                                                    + '.job-details-jobs-unified-top-card__job-insight-view-model-secondary, '
                                                                    + 'time, [class*="insight"], [class*="posted"], [class*="repost"]'
                                                                ));
                                                                const texts = nodes
                                                                    .map((n) => (n.innerText || '').trim())
                                                                    .filter(Boolean)
                                                                    .filter((v) => v.length < 120);
                                                                const joined = texts.join(' | ');
                                                                let published = '';
                                                                let updated = '';
                                                                const agoRegex = /(\d+\s*(minute|min|hour|day|week|month|year)s?\s+ago|\d+\s*(分钟|小时|天|周|个月|月|年)前)/i;

                                                                for (const v of texts) {
                                                                    const lower = v.toLowerCase();
                                                                    if (!updated && (lower.includes('repost') || lower.includes('updated') || lower.includes('更新') || lower.includes('重新发布'))) {
                                                                        updated = v;
                                                                    }
                                                                    if (!published && agoRegex.test(v)) {
                                                                        published = v;
                                                                    }
                                                                }

                                                                if (!published) {
                                                                    const m = joined.match(agoRegex);
                                                                    if (m) published = m[0];
                                                                }
                                                                if (!updated && /(repost|updated|更新|重新发布)/i.test(joined)) {
                                                                    updated = joined;
                                                                }

                                                                return { published_text: published, updated_text: updated };
                                                        };

                            const descEl = document.querySelector(
                                '.jobs-description__content .jobs-description-content__text, '
                                + '.jobs-description-content__text--stretch, '
                                + '.jobs-box__html-content, '
                                + '.show-more-less-html__markup'
                            );
                            if (descEl) {
                                result.description = (descEl.innerText || '').trim();
                            }

                            const salaryEl = document.querySelector(
                                '.compensation__salary, '
                                + '[class*="salary"], '
                                + '[class*="compensation"], '
                                + '.job-details-jobs-unified-top-card__job-insight-view-model-secondary'
                            );
                            if (salaryEl) {
                                result.salary = (salaryEl.innerText || '').trim();
                            }

                            const times = extractTimeTexts();
                            result.published_text = times.published_text;
                            result.updated_text = times.updated_text;

                            return result;
                        }
                    """)
                    description = detail_data.get("description", "")
                    salary = detail_data.get("salary", "")
                    published_text = (detail_data.get("published_text") or job_data.get("posted_text") or "").strip()
                    updated_text = (detail_data.get("updated_text") or "").strip()
                    published_at = _parse_relative_datetime(published_text)
                    updated_at = _parse_relative_datetime(updated_text)

                    full_text_parts = [title or f"LinkedIn Job {job_id}"]
                    if company:
                        full_text_parts.append(f"Company: {company}")
                    if salary:
                        full_text_parts.append(f"Salary: {salary}")
                    if location:
                        full_text_parts.append(f"Location: {location}")
                    if description:
                        full_text_parts.append(f"Description: {description}")
                    full_text = "\n\n".join(full_text_parts)

                    posts.append({
                        "id": job_id if job_id else f"linkedin_{page_num}_{idx}",
                        "title": title or f"LinkedIn Job {job_id}",
                        "url": job_url,
                        "company": company,
                        "date": datetime.now(),
                        "text": full_text,
                        "analysis_text": description,
                        "salary": salary,
                        "location": location,
                        "requirements": requirements,
                        "description": description,
                        "source_published_text": published_text,
                        "source_updated_text": updated_text,
                        "source_published_at": published_at,
                        "source_updated_at": updated_at,
                    })

                    logger.info(f"[FETCH LINKEDIN] ✓ {posts[-1]['title'][:50]} ({len(full_text)} chars)")
                    await asyncio.sleep(batch_delay)

                except Exception as e:
                    logger.warning(f"[FETCH LINKEDIN] Error processing job {idx + 1}: {e}")
                    continue

            # Pagination: collections 页面优先滚动，search 页面优先 start= 参数
            try:
                current_url = page.url
                if "/jobs/collections/" in current_url:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(batch_delay + 2)
                else:
                    if "start=" in current_url:
                        import re as _re
                        m = _re.search(r"start=(\d+)", current_url)
                        current_start = int(m.group(1)) if m else page_num * 25
                        next_url = _re.sub(r"start=\d+", f"start={current_start + 25}", current_url)
                    else:
                        separator = "&" if "?" in current_url else "?"
                        next_url = f"{current_url}{separator}start={(page_num + 1) * 25}"

                    logger.info(f"[FETCH LINKEDIN] Navigating to page {page_num + 2}: {next_url}")
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(batch_delay + 1)

            except Exception as e:
                logger.warning(f"[FETCH LINKEDIN] Pagination error: {e}")
                break

    except Exception as e:
        logger.error(f"[FETCH LINKEDIN] Error: {e}", exc_info=True)

    logger.info(f"[FETCH LINKEDIN] Total jobs fetched: {len(posts)}")
    return posts
