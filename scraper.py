import os
import re
import json
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup

COUNTY_NAME = os.environ.get("COUNTY_NAME", "Hall County")
STATE_CODE = os.environ.get("STATE_CODE", "GA")  # e.g. GA, FL, SC, etc.
PARCEL_ID = os.environ.get("PARCEL_ID", "15025A000047")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


def normalize_parcel_id(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.strip().upper())


def to_app_name(county: str, state_code: str) -> str:
    clean = county.strip().lower()
    # Strip any trailing ", GA" / state text the user may have included
    clean = re.split(r",", clean)[0]
    words = re.split(r"\s+", clean.strip())
    camel = "".join(w.capitalize() for w in words if w)
    return f"{camel}County{state_code.strip().upper()}"


def parse_two_column_table(table) -> dict:
    """Parses tables like Summary / Owner where each row is label: value."""
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
    """Parses tables with a <thead> of column headers and <tbody> rows."""
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
            # Sections without tables (e.g. links only) - skip data extraction
            continue

        section_data = []
        for table in tables:
            classes = table.get("class", [])
            if "tabular-data-two-column" in classes:
                section_data.append(parse_two_column_table(table))
            else:
                section_data.append(parse_data_table(table))

        # Flatten if this section only has a single two-column table
        if len(section_data) == 1 and isinstance(section_data[0], dict):
            result[section_title] = section_data[0]
        else:
            result[section_title] = section_data

    # Owner name (special-case: it's a link, not a labeled row)
    owner_section = soup.find(
        lambda tag: tag.name == "div"
        and tag.get("id", "").endswith("_lblAddress") is False
    )
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

    # Property photo (if present)
    photo_img = soup.select_one("#photogrid img")
    if photo_img and photo_img.get("src"):
        result["PhotoUrl"] = photo_img["src"]

    return result


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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            page.goto(search_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)

            # Terms and Conditions "Agree" button, if present
            try:
                agree_btn = page.locator("a.button-1")
                if agree_btn.count() > 0 and agree_btn.first.is_visible():
                    agree_btn.first.click()
                    page.wait_for_timeout(2000)
            except PWTimeout:
                pass

            # Find the Parcel ID search input
            parcel_input = page.locator("input[id$='_txtParcelID']")
            parcel_input.wait_for(state="visible", timeout=15000)
            parcel_input.click()
            page.wait_for_timeout(900)

            parcel_id_for_search = PARCEL_ID.replace("-", "")
            parcel_input.fill(parcel_id_for_search)
            page.wait_for_timeout(1000)

            # Click the parcel search button
            search_btn = page.locator("a.tt-upm-parcelid-search-btn")
            search_btn.first.click(timeout=3500)
            page.wait_for_timeout(15000)

            # Find and click the exact match in the results/dropdown
            match_result = page.evaluate(
                """(target) => {
                    function normalize(value) {
                        return String(value || '')
                            .trim()
                            .toUpperCase()
                            .replace(/[^A-Z0-9]/g, '');
                    }
                    const candidates = [...document.querySelectorAll('a, td')];
                    let exactMatch = null;
                    for (const el of candidates) {
                        const text = String(el.textContent || '').trim();
                        if (!text) continue;
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

            if not match_result.get("success"):
                raise Exception(
                    f"EXACT PARCEL ID MATCH NOT FOUND: {PARCEL_ID}"
                )

            print(f"Matched result: {match_result.get('matchedText')}")

            page.wait_for_timeout(10000)
            page.wait_for_load_state("networkidle", timeout=30000)

            html = page.content()
            with open("report_debug.html", "w", encoding="utf-8") as f:
                f.write(html)

            data = extract_report_data(html)
            output = {
                "county": COUNTY_NAME,
                "state": STATE_CODE,
                "appName": app_name,
                "parcelId": PARCEL_ID,
                "reportUrl": page.url,
                "data": data,
            }

        except Exception as e:
            print(f"Scraping error: {e}")
            output = {"error": str(e)}
            try:
                page.screenshot(path="debug.png", full_page=True)
            except Exception:
                pass
            try:
                html = page.content()
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                pass

        finally:
            browser.close()

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
