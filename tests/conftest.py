import faulthandler
import json
import logging
import os
import signal
import threading
import urllib.request

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from cua.surface.playwright_surface import PlaywrightSurface
from mock_app import create_app

# `kill -USR1 <pytest pid>` dumps every thread's stack: for diagnosing a stuck browser call.
faulthandler.register(signal.SIGUSR1, all_threads=True)

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


class RecordingApprover:
    """Stands in for the human operator: records every held request, answers with a fixed decision.

    `on_request` lets a test inspect the live app *while* the action is held.
    """

    def __init__(self, approve: bool, *, human: bool = True, on_request=None):
        self.approve, self.human, self.on_request = approve, human, on_request
        self.requests = []

    def __call__(self, request):
        from cua.policy.gate import ApprovalDecision

        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        return ApprovalDecision(approved=self.approve, by="test operator" if self.human else "auto-approver",
                                by_human=self.human)
