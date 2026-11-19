"""Exercise the live viewer and its self-contained demo with Chromium."""

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or None)
    page = browser.new_page(viewport={"width": 1360, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    expect(page.locator(".verdict strong")).to_have_text("Block")
    expect(page.locator(".details")).to_contain_text("Retain signed receipts")
    page.get_by_label("Show highlights").uncheck()
    expect(page.locator(".box")).to_have_count(0)
    page.get_by_role("button", name="Visual OCR").click()
    expect(page.locator(".transcript")).to_contain_text("Retain signed receipts")
    page.get_by_role("button", name="Inspect document").click()
    page.locator("input[type=file]").set_input_files(
        str(ROOT / "fixtures/searchable.pdf")
    )
    page.get_by_label("OCR language").select_option("eng")
    page.get_by_role("button", name="Run inspection").click()
    expect(page.locator(".verdict strong")).to_have_text("Allow", timeout=75000)
    expect(page.locator(".details")).to_contain_text("Delivery receipt")
    with page.expect_download() as dl:
        page.get_by_role("button", name="JSON", exact=True).click()
    assert dl.value.suggested_filename == "searchable.pdf.filetruth.json"
    page.goto((ROOT / "examples/demo.html").as_uri())
    page.get_by_label("Example report").select_option("revised")
    expect(page.locator(".verdict strong")).to_have_text("Review")
    expect(page.locator(".transcript mark")).to_have_text("Delivery east")
    page.get_by_label("Example report").select_option("handover")
    expect(page.locator(".verdict strong")).to_have_text("Block")
    if os.getenv("FILETRUTH_SCREENSHOT"):
        page.screenshot(path=os.environ["FILETRUTH_SCREENSHOT"], full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors, errors
    browser.close()
print(
    "Live upload, policy, OCR, highlighting, export, offline samples and mobile layout passed."
)
