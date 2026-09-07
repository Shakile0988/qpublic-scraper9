import os
import re
import json
import time
import random
from camoufox.sync_api import Camoufox
from bs4 import BeautifulSoup

COUNTY_NAME = os.environ.get("COUNTY_NAME", "Hall County")
STATE_CODE = os.environ.get("STATE_CODE", "GA")  # e.g. GA, FL, SC, etc.
PARCEL_ID = os.environ.get("PARCEL_ID", "15025A000047")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = [30, 60, 90]  # wait before attempt 2, 3, 4


def normalize_parcel_id(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.strip().upper())


def _capitalize_word(word: str) -> str:
    return "-".join(part.capitalize() for part in word.split("-"))


def to_app_name(county: str, state_code: str) -> str:
    clean = county.strip().lower()
    clean = re.split(r",", clean)[0].strip()
    clean = re.sub(r"\bcounty\b", "", clean, flags=re.IGNORECASE)
    words = re.split(r"\s+", clean.strip())
    camel = "".join(_capitalize_word(w) for w in words if w)
    return f"{camel}County{state_code.strip().upper()}"


def parse_two_column_table(table) -> dict:
    data = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        value = cells[1].get_text(" ", strip=True)
        if not label:
            continue
        data[label] = value
    return data


def parse_data_table(table) -> list:
    headers = []
    thead = table.find("thead")
    if thead:
        for th in thead.find_all(["th", "td"]):
            headers.append(th.get_text(" ", strip=True) or f"col{len(headers)}")

    rows = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        if headers and len(headers) == len(cells):
            row_dict = {}
            for h, c in zip(headers, cells):
                row_dict[h] = c.get_text(" ", strip=True)
            rows.append(row_dict)
        else:
            rows.append([c.get_text(" ", strip=True) for c in cells])
    return rows


def extract_report_data(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    sections = soup.find_all("section")
    for section in sections:
        header = section.find("header", class_="module-header")
        if not header:
            continue
        title_div = header.find("div", class_="title")
        if not title_div:
            continue
        section_title = title_div.get_text(" ", strip=True)
        if not section_title:
            continue

        content = section.find("div", class_="module-content")
        if not content:
            continue

        tables = content.find_all("table")
        if not tables:
            continue

        section_data = []
        for table in tables:
            classes = table.get("class", [])
            if "tabular-data-two-column" in classes:
                section_data.append(parse_two_column_table(table))
            else:
                section_data.append(parse_data_table(table))

        if len(section_data) == 1 and isinstance(section_data[0], dict):
            result[section_title] = section_data[0]
        else:
            result[section_title] = section_data

    owner_link = soup.select_one("[id*='lnkOwnerName_lnkSearch']")
    if owner_link:
        owner_block = owner_link.find_parent("td")
        owner_name = owner_link.get_text(" ", strip=True)
        owner_address = ""
        owner_citystatezip = ""
        if owner_block:
            addr_span = owner_block.find(id=re.compile(r"lblAddress$"))
            csz_span = owner_block.find(id=re.compile(r"lblCityStateZip$"))
            if addr_span:
                owner_address = addr_span.get_text(" ", strip=True)
            if csz_span:
                owner_citystatezip = csz_span.get_text(" ", strip=True)
        result["OwnerParsed"] = {
            "name": owner_name,
            "address": owner_address,
            "cityStateZip": owner_citystatezip,
        }

    photo_img = soup.select_one("#photogrid img")
    if photo_img and photo_img.get("src"):
        result["PhotoUrl"] = photo_img["src"]

    return result


def is_blocked_page(html: str) -> bool:
    """Detect a Cloudflare (or similar) block/challenge page."""
    if not html:
        return False
    lowered = html.lower()
    signals = [
        "attention required",
        "cf-error-details",
        "cf-wrapper",
        "sorry, you have been blocked",
        "checking your browser",
        "challenges.cloudflare.com",
        "cf-browser-verification",
        "just a moment",
    ]
    return any(s in lowered for s in signals)


def human_delay(min_ms=400, max_ms=1400):
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


def run_attempt(app_name: str, search_url: str, target_normalized: str, attempt_num: int) -> dict:
    """Runs a single scrape attempt. Raises Exception on failure/block."""
    with Camoufox(
        headless="virtual",   # runs behind a real virtual display (Xvfb), harder to fingerprint than plain headless
        humanize=True,        # simulates realistic human mouse movement
        geoip=True,           # matches fingerprint (timezone/locale) to a plausible real location
        os=("windows", "macos", "linux"),
    ) as browser:
        page = browser.new_page()
        page.set_default_timeout(60000)

        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        human_delay(1000, 2500)

        html_check = page.content()
        if is_blocked_page(html_check):
            raise Exception("BLOCKED_BY_CLOUDFLARE")

        # Terms and Conditions "Agree" button, if present
        try:
            agree_btn = page.locator("a.button-1")
            if agree_btn.count() > 0 and agree_btn.first.is_visible():
                agree_btn.first.click()
                human_delay(800, 1800)
        except Exception:
            pass

        # Find the Parcel ID search input
        parcel_input = page.locator("input[id$='_txtParcelID']")
        parcel_input.wait_for(state="visible", timeout=20000)
        parcel_input.click()
        human_delay(400, 900)

        parcel_id_for_search = PARCEL_ID.replace("-", "")

        parcel_input.fill("")
        # type with randomized human-like delay per character
        for ch in parcel_id_for_search:
            parcel_input.press_sequentially(ch, delay=random.randint(70, 180))
        human_delay(1200, 2200)

        # Click the parcel search button, if present
        try:
            search_btn = page.locator("a.tt-upm-parcelid-search-btn")
            if search_btn.count() > 0:
                search_btn.first.click(timeout=3500)
        except Exception:
            pass

        match_result = {"success": False}
        max_wait_ms = 25000
        poll_interval_ms = 1000
        elapsed = 0
        while elapsed < max_wait_ms:
            page.wait_for_timeout(poll_interval_ms)
            elapsed += poll_interval_ms

            # bail out early if we got blocked mid-wait
            if elapsed % 5000 == 0 and is_blocked_page(page.content()):
                raise Exception("BLOCKED_BY_CLOUDFLARE")

            match_result = page.evaluate(
                """(target) => {
                    function normalize(value) {
                        return String(value || '')
                            .trim()
                            .toUpperCase()
                            .replace(/[^A-Z0-9]/g, '');
                    }
                    const candidates = [...document.querySelectorAll('a, td, li, div')];
                    let exactMatch = null;
                    for (const el of candidates) {
                        const text = String(el.textContent || '').trim();
                        if (!text || text.length > 40) continue;
                        if (normalize(text) === target) {
                            exactMatch = el;
                            break;
                        }
                    }
                    if (!exactMatch) {
                        return { success: false };
                    }
                    const link = exactMatch.tagName.toLowerCase() === 'a'
                        ? exactMatch
                        : exactMatch.closest('a');
                    if (link) {
                        link.click();
                    } else {
                        exactMatch.click();
                    }
                    return {
                        success: true,
                        matchedText: String(exactMatch.textContent || '').trim()
                    };
                }""",
                target_normalized,
            )
            if match_result.get("success"):
                break

        if not match_result.get("success"):
            final_html = page.content()
            if is_blocked_page(final_html):
                raise Exception("BLOCKED_BY_CLOUDFLARE")
            raise Exception(f"EXACT PARCEL ID MATCH NOT FOUND: {PARCEL_ID}")

        print(f"[Attempt {attempt_num}] Matched result: {match_result.get('matchedText')}")

        page.wait_for_timeout(8000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        html = page.content()
        if is_blocked_page(html):
            raise Exception("BLOCKED_BY_CLOUDFLARE")

        with open("report_debug.html", "w", encoding="utf-8") as f:
            f.write(html)

        data = extract_report_data(html)
        return {
            "county": COUNTY_NAME,
            "state": STATE_CODE,
            "appName": app_name,
            "parcelId": PARCEL_ID,
            "reportUrl": page.url,
            "data": data,
        }


def main():
    app_name = to_app_name(COUNTY_NAME, STATE_CODE)
    search_url = (
        f"https://qpublic.schneidercorp.com/Application.aspx"
        f"?App={app_name}&Layer=Parcels&PageType=Search"
    )
    target_normalized = normalize_parcel_id(PARCEL_ID)

    print(f"County: {COUNTY_NAME}, State: {STATE_CODE} -> App: {app_name}")
    print(f"Parcel ID: {PARCEL_ID} (normalized: {target_normalized})")
    print(f"Search URL: {search_url}")

    output = {"error": None}
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"--- Attempt {attempt}/{MAX_ATTEMPTS} ---")
        try:
            output = run_attempt(app_name, search_url, target_normalized, attempt)
            last_error = None
            break
        except Exception as e:
            last_error = str(e)
            print(f"Attempt {attempt} failed: {last_error}")
            if attempt < MAX_ATTEMPTS:
                wait_s = BACKOFF_SECONDS[attempt - 1]
                print(f"Waiting {wait_s}s before retry...")
                time.sleep(wait_s)

    if last_error:
        output = {"error": last_error}
        # best-effort debug artifacts using a fresh quick camoufox screenshot attempt
        try:
            with Camoufox(headless="virtual") as browser:
                page = browser.new_page()
                page.goto(search_url, timeout=30000)
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
        except Exception:
            pass

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(json.dumps(output, indent=2, ensure_ascii=False))

    if WEBHOOK_URL:
        import urllib.request

        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(output).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"Webhook response: {resp.status}")
        except Exception as e:
            print(f"Webhook send failed: {e}")


if __name__ == "__main__":
    main()
