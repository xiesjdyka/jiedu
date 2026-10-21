"""Synthetic headless review of the one-time update summary; never opens user data."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "review"
HTML = (ROOT / "static/index.html").read_text("utf-8")
NOTICE = HTML[HTML.index('<section id="updateNotice"'):HTML.index('<section id="aboutJiedu"')]
SETTINGS = HTML[HTML.index('<section id="updateNoticeSettings"'):HTML.index('<section class="protection-hero"')]


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/static/style.css":
            body = (ROOT / "static/style.css").read_bytes()
            content_type = "text/css"
        elif self.path == "/static/update-notice.js":
            body = (ROOT / "static/update-notice.js").read_bytes()
            content_type = "text/javascript"
        else:
            body = ("<!doctype html><meta charset='utf-8'><link rel='stylesheet' href='/static/style.css'>"
                    "<script defer src='/static/update-notice.js'></script><body>"
                    "<div id='brandSequence' class='brand-sequence done'></div>"
                    "<section id='firstRunGuide' hidden></section>" + NOTICE
                    + "<aside class='sidebar'></aside><main><section id='protection'>"
                    + SETTINGS + "</section></main><div id='toast'></div></body>").encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def initialize(page, *, sources, clients, access, index, fail_index=False):
    page.evaluate("""([sources, clients, access, index, failIndex]) => {
      window.fixtureAck = 0;
      window.api = async url => {
        if (url.startsWith('/api/local-sources')) return sources;
        if (url.startsWith('/api/local-ai')) return clients;
        if (url.startsWith('/api/client-access')) return access;
        if (url.startsWith('/api/knowledge-base')) { if (failIndex) throw Error('offline'); return index; }
        if (url.startsWith('/api/update-notice')) return window.fixtureNotice;
        throw Error(url);
      };
      window.apiPost = async () => { window.fixtureAck++; return {...window.fixtureNotice, show_update:false}; };
      window.copyText = async text => { window.fixtureCopied=text; return true; };
      window.showToast = text => document.querySelector('#toast').textContent=text;
    }""", [sources, clients, access, index, fail_index])


def main():
    from playwright.sync_api import sync_playwright
    REVIEW.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
        page.goto(url)
        notice = {"state":"ready","kind":"upgrade","version":"1.1.6",
                  "previous_version":"1.1.5","show_update":True}
        only_hermes = {"sources":[{"id":"codex","detected":True,"readable":True},
                                  {"id":"hermes","detected":True,"readable":True,
                                   "configured":False,"history":{"status":"paused"},"live":{"status":"paused"}}]}
        clients = {"clients":[{"id":"codex","installed":False},
                              {"id":"hermes","installed":True,"configured":True}]}
        access = {"registered":[{"claimed_id":"hermes","status":"read_verified",
                                 "last_read_at":"2026-10-20T01:00:00Z",
                                 "policies":[{"decision":"deny"}]}]}
        index = {"markdown_count":0,"content_markdown_count":0,"workspace_index":{"ready":False,"vector_ready":False}}
        initialize(page,sources=only_hermes,clients=clients,access=access,index=index)
        page.evaluate("value => window.fixtureNotice = value",notice)
        page.evaluate("jieduUpdateNotice.initialize({},window.fixtureNotice)")
        page.wait_for_function("document.querySelector('#updateNoticeRetry').disabled === false")
        assert page.locator("#updateNotice").is_visible()
        assert "已保留" in page.locator("#updateNoticeCheck").inner_text()
        assert "Codex" not in page.locator("#updateNoticeFacts").inner_text()
        assert page.locator("#updateNoticePromptWrap").is_hidden()
        page.screenshot(path=str(REVIEW / "update-notice-hermes-paused-synthetic.png"), full_page=True)
        page.locator("#updateNoticeClose").click()
        page.wait_for_function("window.fixtureAck === 1")
        assert page.locator("#updateNotice").is_hidden()
        page.locator("#reopenUpdateNotice").click()
        assert page.locator("#updateNotice").is_visible()

        dual = {"sources":[{"id":"codex","detected":True,"readable":True,"configured":False},
                           {"id":"hermes","detected":True,"readable":True,"configured":True}]}
        dual_clients = {"clients":[{"id":"codex","installed":True,"configured":False},
                                   {"id":"hermes","installed":True,"configured":True}]}
        dual_access = {"registered":[{"claimed_id":"hermes","status":"authenticated",
                                      "last_read_at":None,"policies":[{"decision":"deny"}]}]}
        keyword = {"markdown_count":4,"content_markdown_count":4,"workspace_index":{"ready":True,"vector_ready":False}}
        page2 = browser.new_page(viewport={"width":1280,"height":900},device_scale_factor=1)
        page2.goto(url)
        initialize(page2,sources=dual,clients=dual_clients,access=dual_access,index=keyword)
        page2.evaluate("value => window.fixtureNotice = value",notice)
        page2.evaluate("jieduUpdateNotice.initialize({},window.fixtureNotice)")
        page2.wait_for_function("document.querySelector('#updateNoticeRetry').disabled === false")
        assert "无需操作" not in page2.locator("#updateNoticeCheck").inner_text()
        assert "按关键词搜索" in page2.locator("#updateNoticeFacts").inner_text()
        prompt = page2.locator("#updateNoticePrompt").input_value()
        assert "Codex" in prompt and "Hermes" in prompt
        assert "已经授权的一小段资料" in prompt and "不在报告中公开密钥或私人正文" in prompt
        page2.screenshot(path=str(REVIEW / "update-notice-dual-review-synthetic.png"), full_page=True)
        assert len(page2.evaluate("jieduUpdateNotice.visibleChanges({...window.fixtureNotice,previous_version:'1.1.4'})")) == 5
        assert len(page2.evaluate("jieduUpdateNotice.visibleChanges(window.fixtureNotice)")) == 4

        page3 = browser.new_page()
        page3.goto(url)
        initialize(page3,sources=dual,clients=dual_clients,access=dual_access,index=keyword,fail_index=True)
        page3.evaluate("value => window.fixtureNotice = value",notice)
        page3.evaluate("jieduUpdateNotice.initialize({},window.fixtureNotice)")
        page3.wait_for_function("document.querySelector('#updateNoticeRetry').disabled === false")
        assert "无法确认" in page3.locator("#updateNoticeCheck").inner_text()
        browser.close()
    server.shutdown()
    print("update notice synthetic headless review: OK")


if __name__ == "__main__":
    main()
