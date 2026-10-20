"""Small browser fixture for the first-run gate and archive progress cards."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "review"
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "static/app.js").read_text(encoding="utf-8")
GUIDE = HTML[HTML.index('<section id="firstRunGuide"'):HTML.index('<section id="aboutJiedu"')]
SCRIPT = APP[APP.index("const FIRST_RUN_KEY="):APP.index("function initTaskShell")]
PROGRESS = APP[APP.index("function archiveProgressLine"):APP.index("async function loadArchiveProgress")]


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/static/style.css":
            body = (ROOT / "static/style.css").read_bytes()
            content_type = "text/css"
        else:
            body = ("<!doctype html><link rel='stylesheet' href='/static/style.css'>"
                    "<body class='first-run-pending'><div id='brandSequence' class='brand-sequence done'></div>"
                    + GUIDE + "<aside class='sidebar' style='display:none'></aside><main id='appMain'>HOME</main><div id='toast'></div></body>").encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def initialize(page, sources):
    page.evaluate("""([script, progress, sources]) => {
      window.$ = s => document.querySelector(s);
      window.esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.formatTime = s => String(s || '');
      window.showToast = s => document.querySelector('#toast').textContent = s;
      window.api = async url => url.startsWith('/api/knowledge-base')
        ? {vault_path:'D:/Shared Notes'} : sources;
      eval(script + progress);
      window.showGuide = showFirstRunGuide;
      window.sourceLabel = firstRunSourceSummary;
      window.progressCard = archiveProgressCard;
      window.copyText = async value => {
        try { await navigator.clipboard.writeText(value); return true }
        catch { return !!document.execCommand('copy') }
      };
    }""", [SCRIPT, PROGRESS, sources])


def main():
    from playwright.sync_api import sync_playwright
    REVIEW.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        both = {"sources": [{"id": "codex", "detected": True, "configured": True,
                              "locations": [{"kind": "jsonl", "detected": True, "readable": True, "root": "C:/Fixture/Codex"}]},
                            {"id": "hermes", "detected": True, "configured": False,
                             "locations": [{"kind": "sqlite", "detected": True, "readable": True, "path": "C:/Fixture/Hermes/state.db"}]}]}
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        assert page.locator("#appMain").evaluate("e => getComputedStyle(e).visibility") == "hidden"
        initialize(page, both)
        page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>true}})")
        page.evaluate("showGuide({workspace_id:'one',workspace_root:'D:/Shared Notes'})")
        assert page.locator("#firstRunGuide").is_visible()
        assert page.locator("#appMain").evaluate("e => e.inert")
        assert page.locator("#firstRunEnter").is_disabled()
        assert "两种客户端均已发现" in page.locator("#firstRunWorkspace").inner_text()
        assert "C:/Fixture/Hermes/state.db" in page.locator("#firstRunPrompt").input_value()
        page.screenshot(path=str(REVIEW / "first-run-guide-synthetic.png"), full_page=True)
        page.evaluate("document.querySelector('#firstRunCopy').click()")
        page.wait_for_function("!document.querySelector('#firstRunEnter').disabled")
        page.evaluate("document.querySelector('#firstRunEnter').click()")
        assert page.locator("#firstRunGuide").is_hidden()
        assert not page.locator("#appMain").evaluate("e => e.inert")
        page.evaluate("""() => {
          document.body.classList.remove('first-run-pending');
          document.querySelector('#appMain').innerHTML = `<section id="archiveProgress" class="archive-progress-overview"><div class="archive-progress-title"><div><h2>自动归档进度</h2><p>合成数据 · 历史补齐与新消息监听分别计算</p></div></div><div class="archive-progress-grid">${progressCard('ChatGPT 工作（Codex）',{detected:true,readable:true,history:{status:'running',by_source:[{source:'jsonl',unit:'files',checked:3,known_total:6,archived:14,failed_missing:1,excluded:0},{source:'sqlite',unit:'records',checked:8,known_total:null,archived:6,failed_missing:0,excluded:2}]},live:{status:'watching',saved_messages:4}})}${progressCard('Hermes',{detected:true,readable:true,history:{status:'running',unit:'records',checked:9,known_total:12,archived:7,failed_missing:1,excluded:1},live:{status:'watching',saved_messages:2}})}</div></section>`;
        }""")
        page.screenshot(path=str(REVIEW / "archive-progress-synthetic.png"), full_page=True)
        page.evaluate("showGuide({workspace_id:'one'})")
        assert page.locator("#firstRunGuide").is_hidden()
        page.evaluate("showGuide({workspace_id:'two'})")
        assert page.locator("#firstRunGuide").is_visible()
        assert page.locator("#firstRunEnter").is_disabled()

        manual = browser.new_context().new_page()
        manual.goto(url)
        initialize(manual, {"sources": [{"id": "hermes", "detected": True, "configured": False}]})
        manual.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}}); document.execCommand=()=>false")
        manual.evaluate("showGuide({workspace_id:'hermes-only'})")
        assert "仅发现 Hermes" in manual.locator("#firstRunWorkspace").inner_text()
        manual.evaluate("document.querySelector('#firstRunCopy').click()")
        manual.wait_for_function("!document.querySelector('#firstRunManualWrap').hidden")
        assert manual.locator("#firstRunEnter").is_disabled()
        manual.evaluate("document.querySelector('#firstRunSelect').click(); const c=document.querySelector('#firstRunManualConfirm');c.checked=true;c.dispatchEvent(new Event('change'))")
        assert manual.locator("#firstRunEnter").is_enabled()
        assert "未发现 Codex 或 Hermes" in manual.evaluate("sourceLabel({sources:[]})")
        known = manual.evaluate("progressCard('Codex',{history:{checked:4,known_total:8,archived:3,failed_missing:1,excluded:0},live:{status:'watching'}})")
        unknown = manual.evaluate("progressCard('Hermes',{history:{checked:4,known_total:null,archived:3,failed_missing:1,excluded:0},live:{status:'watching'}})")
        assert "50%" in known and 'max="8"' in known
        assert "50%" not in unknown and 'aria-label="历史检查进度，总量待确定"' in unknown
        mixed = manual.evaluate("progressCard('Codex',{detected:true,readable:true,history:{by_source:[{source:'jsonl',unit:'files',checked:2,known_total:4,archived:11},{source:'sqlite',unit:'records',checked:3,known_total:null,archived:2}]},live:{status:'watching'}})")
        assert "份来源文件" in mixed and "条消息" in mixed and "条来源记录" in mixed
        browser.close()
    server.shutdown()
    print("frontend onboarding/progress smoke: OK")


if __name__ == "__main__":
    main()
