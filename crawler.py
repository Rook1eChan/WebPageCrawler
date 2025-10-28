import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import yaml
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import aiofiles
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
import urllib.robotparser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

COOKIE_BUTTON_TEXTS = [
    "close", "Only", "accept", "agree", "i agree", "accept all", "accept cookies", "agree and continue", "ok",
    "allow", "got it", "continue", "yes",
    "接受", "同意", "同意并继续", "关闭", "知道了", "允许",
]

LOAD_MORE_TEXTS = [
    "load more", "show more", "load more articles", "加载更多", "更多", "view more", "show more",
]

NEXT_PAGE_TEXTS = [
    "next", "next >", "后页", ">", "›", "下一页", "下一頁", "后页", "下一章", "下一",
]


def sanitize_filename(s: str, max_len=40) -> str:
    s = re.sub(r"[:\/\\\?\%\*\|\"<>\n\r]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return (s[:max_len]).strip("_") or "page"


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """
    Remove fragment from url.
    xxx.com#main_content -> xxx.com
    """
    u, _ = urldefrag(url)
    return u


async def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
            return json.loads(content)
    except Exception as e:
        logging.warning("Failed to read json %s: %s", path, e)
        return {}


async def write_json_atomic(path: Path, data):
    # Write to temp file then atomic replace
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        # Ensure parent exists (should already)
        os.replace(str(tmp), str(path))
    except Exception as e:
        logging.warning("Failed to write json %s: %s", path, e)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


async def handle_cookie_security_dot_com(page: Page):
    """
    Handle Cookie pop-up for security.com website:
    When a pop-up with aria-label="Cookies" is detected, click the specified close button
    """
    try:
        # 1. Locate the pop-up container with aria-label="Cookies"
        cookie_dialog = page.locator('div[role="dialog"][aria-label="Cookies"]')
        # Check if the pop-up exists (timeout after 3 seconds to avoid blocking)
        if await cookie_dialog.count() > 0 and await cookie_dialog.is_visible(timeout=3000):
            logging.debug("Detected Cookie pop-up")

            # 2. Locate the close button (based on class and aria-label)
            close_btn = cookie_dialog.locator(
                'button.onetrust-close-btn-handler.ot-close-icon.banner-close-button[aria-label="Close"]'
            )

            # 3. Click the close button and wait
            if await close_btn.count() > 0 and await close_btn.is_enabled(timeout=2000):
                await close_btn.click(timeout=2000)
                logging.debug("Close button clicked")
                await asyncio.sleep(0.6)  # Wait 0.6 seconds as required
                return True  # Successfully closed the pop-up
            else:
                logging.debug("No valid close button found")
        else:
            logging.debug("No Cookie pop-up detected on security.com")
    except Exception as e:
        logging.debug(f"Error handling Cookie pop-up on security.com: {e}")
    return False  # Pop-up not closed


class Crawler:
    def __init__(
            self,
            start_url: str,
            output_dir: Path,
            history_path: str,
            concurrency: int,
            max_depth: int,
            timeout_ms: int,
            delay_s: float,
            refresh_mode: str,
            no_new_limit: int,
            prefixes: Optional[List[str]],
            obey_robots: bool,
            deal_cookie: bool,
    ):
        self.start_url = normalize_url(start_url)
        self.output_dir = Path(os.path.join(os.getcwd(), output_dir))
        self.concurrency = max(1, concurrency)
        self.max_depth = max(1, max_depth)
        self.timeout_ms = max(1000, int(timeout_ms))
        self.delay_s = max(0.0, float(delay_s))
        if len(prefixes) and isinstance(prefixes, str):
            prefixes = [prefixes]
        self.prefixes = prefixes
        self.refresh_mode = refresh_mode.lower() if refresh_mode else "none"
        if self.refresh_mode not in ("pull", "pagination", "none"):
            logging.warning("unknown refresh_mode %s -> treating as 'none'", self.refresh_mode)
            self.refresh_mode = "none"
        self.obey_robots = obey_robots
        self.no_new_limit = max(1, int(no_new_limit))

        self.history_file = Path(history_path)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history = {}
        self.processed_urls: Set[str] = set()  # loaded from history keys
        self.seen_urls: Set[str] = set()  # seen in this run to avoid duplicates
        self.per_domain_last = defaultdict(lambda: 0.0)
        self.sem = asyncio.Semaphore(self.concurrency)
        self.browser = None
        self.playwright = None
        self.robots_cache = {}

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.deal_cookie = deal_cookie

    async def load_history(self):
        """Load the information of the collected PDFs for subsequent deduplication."""

        self.history = await read_json(self.history_file)
        # Ensure keys are normalized (some URLs might have fragments)
        # new_hist = {}
        # for k, v in self.history.items():
        #     new_hist[normalize_url(k)] = v
        # self.history = new_hist
        self.processed_urls = set(self.history.keys())
        logging.info("Loaded %d history entries from %s", len(self.history), self.history_file)

    async def save_history_entry(self, url: str, filename: str, h: str):
        self.history[url] = {
            "filename": filename,
            "sha1": h,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        # persist
        await write_json_atomic(self.history_file, self.history)
        # update processed_urls set
        self.processed_urls.add(url)

    def prefix_allowed(self, url: str) -> bool:
        if not self.prefixes:
            return True
        for p in self.prefixes:
            if url.startswith(p):
                return True
        return False

    async def can_fetch_robots(self, url: str) -> bool:
        """Follow the rules specified in the website's robots.txt file"""
        if not self.obey_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = self.robots_cache.get(origin)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception as e:
                logging.warning("Failed to read robots.txt at %s: %s — assuming allowed", robots_url, e)
                self.robots_cache[origin] = rp
                return True
            self.robots_cache[origin] = rp
        return rp.can_fetch("*", url)

    async def domain_delay_wait(self, url: str):
        parsed = urlparse(url)
        domain = parsed.netloc
        now = time.time()
        last = self.per_domain_last[domain]
        elapsed = now - last
        if elapsed < self.delay_s:
            await asyncio.sleep(self.delay_s - elapsed)
        self.per_domain_last[domain] = time.time()

    async def _handle_cookie_popup(self, page: Page):
        """Close the cookie pop-up to prevent it from obscuring the page."""
        try:
            # Special rules for security.com
            if self.start_url.startswith("https://www.security.com/"):
                closed = await handle_cookie_security_dot_com(page)
                if closed:
                    return  # If handled, return directly to reduce unnecessary general checks

            # General detection for cookie pop-ups
            for txt in COOKIE_BUTTON_TEXTS:
                btns = page.locator(
                    f'xpath=//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                    f' | //input[@type="button" and contains(translate(normalize-space(@value),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                )
                if await btns.count() > 0:
                    try:
                        await btns.first.click(timeout=3000)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                anchors = page.locator(
                    f'xpath=//a[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]')
                if await anchors.count() > 0:
                    try:
                        await anchors.first.click(timeout=3000)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
            # try frames
            for f in page.frames:
                if f == page.main_frame:
                    continue
                for txt in COOKIE_BUTTON_TEXTS:
                    try:
                        locator = f.locator(
                            f'xpath=//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]')
                        if await locator.count() > 0:
                            try:
                                await locator.first.click(timeout=2000)
                                await asyncio.sleep(0.3)
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception as e:
            logging.debug("Cookie dismissal helper failed: %s", e)

    async def _extract_links_from_page(self, page: Page) -> List[str]:
        """
        Extract all URLs from a specific page that meet the following criteria:
        *   Cannot be empty
        *   Match the specified prefix
        *   Not present in seen_urls (URLs processed during this program run)
        *   Not present in processed_urls (URLs already saved as PDFs)
        """
        try:
            # Extract all links
            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            out = []

            # Perform filtering
            for h in hrefs:
                if not h:
                    continue
                normalized_h = normalize_url(h)
                if self.prefixes and not self.prefix_allowed(normalized_h):
                    continue
                if normalized_h in self.seen_urls:
                    continue
                if normalized_h in self.processed_urls:
                    continue

                # Mark as seen
                self.seen_urls.add(normalized_h)
                out.append(normalized_h)

            return out
        except Exception as e:
            logging.debug("Failed to extract links: %s", e)
            return []

    async def _save_pdf_from_page(self, page: Page, url: str) -> Path:
        title = ""
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        prefix = sanitize_filename(title, max_len=50)
        h = url_hash(url)
        filename = f"{prefix}_{h}.pdf"
        path = self.output_dir / filename

        try:
            try:
                await page.emulate_media(media="screen")
            except Exception:
                pass
            await asyncio.sleep(0.3)
            pdf_coro = page.pdf(path=str(path), format="A4", print_background=True)
            try:
                await asyncio.wait_for(pdf_coro, timeout=(self.timeout_ms / 1000.0))
            except asyncio.TimeoutError:
                logging.warning("PDF generation timed out for %s", url)
                raise PlaywrightTimeoutError(f"PDF generation timed out for {url}")
            except Exception as e:
                logging.warning("Failed to save PDF for %s: %s", url, e)
                raise
            logging.info("Saved PDF for %s -> %s", url, path)
        except PlaywrightTimeoutError:
            raise
        except Exception:
            raise

        await self.save_history_entry(url, filename, h)
        return path

    async def _process_page_task(self, url: str, depth: int) -> List[str]:
        """
        Process a single page:
        1. Save the current page as PDF
        2. If current depth < maximum depth, extract links from the page for next level
        Return value: List of compliant next-level links (only when further exploration is needed)
        """
        if not await self.can_fetch_robots(url):
            logging.info("Robots.txt prohibits access to %s", url)
            return []

        async with self.sem:
            await self.domain_delay_wait(url)
            page = None
            try:
                page = await self.browser.new_page()
            except Exception as e:
                logging.error("Failed to create new page: %s", e)
                raise

            try:
                logging.info("Opening page %s (timeout=%d ms)", url, self.timeout_ms)
                try:
                    await page.goto(url, timeout=self.timeout_ms, wait_until="networkidle")
                except PlaywrightTimeoutError:
                    logging.warning("Page loading timed out: %s", url)
                except Exception as e:
                    logging.warning("Failed to open page %s: %s", url, e)

                if self.deal_cookie:
                    await self._handle_cookie_popup(page)

                # 1. Save current page as PDF
                try:
                    await self._save_pdf_from_page(page, url)
                except Exception:
                    logging.warning("Failed to save PDF for %s, skipping record", url)

                # 2. Extract next-level links only when further exploration is needed
                discovered_links = []
                if depth < self.max_depth:
                    discovered_links = await self._extract_links_from_page(page)
                logging.debug("Page %s extracted %d compliant next-level links", url, len(discovered_links))

                return discovered_links

            finally:
                try:
                    if page and not page.is_closed():
                        await page.close()
                except Exception:
                    pass

    # async def _refresh_page(self, page: Page) -> bool:
    #     """
    #     Try a set of heuristics once: scroll to bottom, attempt to click load-more or next.
    #     Returns True if a clickable element was clicked.
    #     """
    #     try:
    #         # Scroll to bottom to trigger lazy load
    #         try:
    #             await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    #             await asyncio.sleep(0.6)
    #         except Exception:
    #             pass
    #
    #         # Try load-more style buttons/links
    #         for txt in LOAD_MORE_TEXTS:
    #             try:
    #                 loc = page.locator(
    #                     f'xpath=//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
    #                     f' | //a[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
    #                 )
    #                 if await loc.count() > 0:
    #                     try:
    #                         await loc.first.click(timeout=6000)
    #                         logging.info("Attempted to click '%s' button", txt)
    #                         await asyncio.sleep(1.0)
    #                         try:
    #                             await page.wait_for_load_state("networkidle", timeout=4000)
    #                         except Exception:
    #                             pass
    #                         return True
    #                     except Exception:
    #                         continue
    #             except Exception:
    #                 continue
    #
    #         # If pagination mode, try next-type links
    #         if self.refresh_mode == "pagination":
    #             for txt in NEXT_PAGE_TEXTS:
    #                 try:
    #                     loc = page.locator(
    #                         f'xpath=//a[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
    #                     )
    #                     if await loc.count() > 0:
    #                         try:
    #                             await loc.first.click(timeout=6000)
    #                             logging.info("Clicked next-page element matching '%s'", txt)
    #                             await asyncio.sleep(1.0)
    #                             try:
    #                                 await page.wait_for_load_state("networkidle", timeout=4000)
    #                             except Exception:
    #                                 pass
    #                             return True
    #                         except Exception:
    #                             continue
    #                 except Exception:
    #                     continue
    #
    #         # Try clicking elements that have rel="next" or aria-label containing next
    #         try:
    #             rel_next = page.locator('a[rel="next"]')
    #             if await rel_next.count() > 0:
    #                 try:
    #                     await rel_next.first.click(timeout=6000)
    #                     logging.info("Clicked rel=next link")
    #                     await asyncio.sleep(1.0)
    #                     return True
    #                 except Exception:
    #                     pass
    #         except Exception:
    #             pass
    #
    #     except Exception as e:
    #         logging.debug("_click_one_load_more helper error: %s", e)
    #
    #     return False

    async def _refresh_page(self, page: Page) -> bool:
        """
        Try to refresh page content based on refresh_mode:
        - "pagination": Focus on next-page navigation elements
        - "pull": Focus on load-more style buttons
        - "none": Only trigger lazy loading by scrolling
        Returns True if any refresh action was successfully performed.
        """
        try:
            # Step 1: Trigger lazy loading by scrolling to bottom (common for all modes)
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.6)
                logging.debug("Scrolled to bottom to trigger lazy loading")
            except Exception as e:
                logging.debug("Failed to scroll to bottom: %s", e)
                # Continue even if scrolling fails

            # Handle different refresh modes
            if self.refresh_mode == "pagination":
                # Mode 1: Pagination - focus on next-page elements
                logging.info("Refresh: pagination")

                # Try next-page style buttons/links
                for txt in NEXT_PAGE_TEXTS:
                    try:
                        loc = page.locator(
                            f'xpath=//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                            f' | //a[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                        )
                        if await loc.count() > 0:
                            try:
                                await loc.first.click(timeout=6000)
                                # logging.info("Clicked next-page element matching '%s'", txt)
                                await asyncio.sleep(1.0)
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=4000)
                                except Exception:
                                    pass
                                return True
                            except Exception:
                                continue
                    except Exception:
                        continue

                # Try elements with rel="next" (standard pagination marker)
                try:
                    rel_next = page.locator('a[rel="next"]')
                    if await rel_next.count() > 0:
                        try:
                            await rel_next.first.click(timeout=6000)
                            logging.info("Clicked rel=next link")
                            await asyncio.sleep(1.0)
                            return True
                        except Exception:
                            pass
                except Exception:
                    pass

            elif self.refresh_mode == "pull":
                # Mode 2: Pull - focus on load-more elements
                logging.info("Refresh: pull")

                # Try load-more style buttons/links
                for txt in LOAD_MORE_TEXTS:
                    try:
                        loc = page.locator(
                            f'xpath=//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                            f' | //a[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "{txt}")]'
                        )
                        if await loc.count() > 0:
                            try:
                                await loc.first.click(timeout=6000)
                                logging.info("Clicked load-more element matching '%s'", txt)
                                await asyncio.sleep(1.0)
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=4000)
                                except Exception:
                                    pass
                                return True
                            except Exception:
                                continue
                    except Exception:
                        continue

            else:
                # Mode 3: None - only lazy loading, no button clicks
                logging.info("No refresh mode specified, only attempted lazy loading by scrolling")
                # No additional actions beyond scrolling (return True only if scrolling is considered a success)
                return True

        except Exception as e:
            logging.debug("Error in _refresh_page helper: %s", e)

        # If no actions were successful
        return False

    async def run(self):
        """
        Process the portal website and save the web pages corresponding to the valid links as PDF.
        """
        await self.load_history()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)

        try:
            portal_page = await self.browser.new_page()
            try:
                # Open portal website
                logging.info("Opening portal website %s (timeout=%d ms)", self.start_url, self.timeout_ms)
                try:
                    await portal_page.goto(self.start_url, timeout=self.timeout_ms, wait_until="networkidle")
                except PlaywrightTimeoutError:
                    logging.warning("Portal.goto timeout for %s", self.start_url)
                except Exception as e:
                    logging.warning("Error navigating to portal website %s: %s", self.start_url, e)

                if self.deal_cookie is True:
                    await self._handle_cookie_popup(portal_page)

                # Mark portal as seen to prevent reprocessing
                self.seen_urls.add(self.start_url)

                consecutive_no_new = 0
                iteration = 0

                while True:
                    iteration += 1
                    logging.info("Portal page starting collection round %d", iteration)

                    # Extract links that meet the specified criteria
                    links = await self._extract_links_from_page(portal_page)
                    logging.debug("Portal extracted %d links", len(links))

                    if not links:
                        # Try refreshing portal page to see if new links appears
                        clicked = await self._refresh_page(portal_page)
                        if clicked:
                            logging.info("Refresh but found no immediately-new links; will re-extract")
                            consecutive_no_new = 0
                            # allow some time for new content to appear
                            await asyncio.sleep(1.0)
                            continue
                        else:
                            consecutive_no_new += 1
                            logging.info("No page updates (access count=%d)", consecutive_no_new)
                            if consecutive_no_new >= self.no_new_limit:
                                logging.info("Portal appears exhausted (no new content after %d attempts). Stopping.",
                                             self.no_new_limit)
                                break
                            # small backoff then try again to be robust to slow loading
                            await asyncio.sleep(1.0)
                            continue

                    # Reset consecutive_no_new for new items
                    consecutive_no_new = 0

                    current_level = links
                    current_depth = 1
                    while current_level and current_depth <= self.max_depth:
                        logging.info("Processing %d links at level %d", len(current_level), current_depth)

                        # Process all links at current level (save PDFs), collect sub-links returned by each link
                        tasks = []
                        for u in current_level:
                            tasks.append(asyncio.create_task(self._process_page_task(u, current_depth)))

                        if not tasks:
                            break

                        # Wait for all tasks at current level to complete, get sub-links returned by each task
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Uniformly filter next-level links (process next level only after current level is processed)
                        next_level = []
                        for res in results:
                            if isinstance(res, Exception) or res is None:
                                continue
                            # Iterate through sub-links extracted from current link
                            for l in res:
                                # l = normalize_url(l)
                                # # Filter conditions: unprocessed, not seen, compliant with prefix rules
                                # if l in self.processed_urls:
                                #     continue
                                # if l in self.seen_urls:
                                #     continue
                                # if self.prefixes and not self.prefix_allowed(l):
                                #     continue
                                # # Mark as seen to avoid duplicates
                                # self.seen_urls.add(l)
                                next_level.append(l)

                        # 3. Proceed to next level
                        current_level = next_level
                        current_depth += 1

                    # After completion of processing the current URL queue, try to refresh and continue
                    clicked = await self._refresh_page(portal_page)
                    if clicked:
                        logging.info("Clicked load-more after processing batch; continuing portal loop")
                        # small stabilization time
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        logging.info("No load-more clickable after processing batch; will re-extract to be safe")

            finally:
                try:
                    await portal_page.close()
                except Exception:
                    pass

        finally:
            try:
                if self.browser:
                    await self.browser.close()
                if self.playwright:
                    await self.playwright.stop()
            finally:
                logging.info("Task completed")


async def main():

    CONFIG_LIST = [
        # "config/config2.yaml",
        "config/config3.yaml",
    ]

    for config_path in CONFIG_LIST:
        try:
            # Read current configuration file
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            # Set logging level
            if config["verbose"]:
                logging.getLogger().setLevel(logging.DEBUG)
            else:
                logging.getLogger().setLevel(logging.INFO)

            logging.info(f"===== Start to process: {config_path} =====")

            # Initialize crawler
            crawler = Crawler(
                start_url=config["start_url"],
                output_dir=config["output_dir"],
                history_path=config["history_path"],
                concurrency=config["concurrency"],
                max_depth=config["max_depth"],
                timeout_ms=config["timeout"],
                delay_s=config["delay"],
                prefixes=config["prefixes"],
                refresh_mode=config["refresh_mode"],
                obey_robots=config["obey_robot"],
                no_new_limit=config["no_new_limit"],
                deal_cookie=config["deal_cookie"]
            )

            # Run crawler
            await crawler.run()
            logging.info(f"===== Work done: {config_path} =====")

        except Exception as e:
            logging.error(f"Error processing configuration file {config_path}: {str(e)}", exc_info=True)
            # Continue processing next configuration file after error
            continue


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
