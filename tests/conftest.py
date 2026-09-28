import json
import logging
import os
import threading
import urllib.request

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from cua.surface.playwright_surface import PlaywrightSurface
from mock_app import create_app

MOCK_USER = "op-teller-7731"
MOCK_PASSWORD = "pw-for-tests"


def post_json(url: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="session")
def mock_server():
    """The mock app on an ephemeral localhost port, for browser-driven tests."""
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, create_app(username=MOCK_USER, password=MOCK_PASSWORD), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def browser():
    headed = os.environ.get("HEADED") == "1"  # HEADED=1 pytest ... to watch the browser
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=400 if headed else 0)
        yield browser
        browser.close()


@pytest.fixture
def surface(browser, mock_server):
    post_json(f"{mock_server}/__reset")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    yield PlaywrightSurface(context.new_page(), base_url=mock_server, timeout_ms=3000)
    context.close()
