"""Real index/CSS review with synthetic API facts and no production data."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json
import re
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from knowledge_bridge.prompt_templates import knowledge_wiki_guide, daily_organization_prompt, daily_schedule_setup_prompt
from knowledge_bridge.workspace import initialize_workspace

REVIEW = ROOT / "review"
GUIDE = {}


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            html = (ROOT / "static/index.html").read_text("utf-8")
            html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
            html = html.replace('<body class="first-run-pending">', '<body>')
            boot = """<style>#brandSequence{display:none!important}</style><script>
            document.querySelector('#archivesHub').innerHTML='<section id="archiveProgress" class="archive-progress-overview"><h2>自动归档进度</h2><p>合成验收数据：Hermes 历史仍在补齐。</p></section>';
            window.fixtureGuide=__GUIDE__;
            async function api(url){
              if(url.startsWith('/api/prompt-workflow/distillation/prompt'))throw {status:409};
              if(url.startsWith('/api/prompt-workflow/distillation'))return {ready:false,sources:['Hermes'],reasons:['Hermes 历史尚未完成（running）']};
              if(url.startsWith('/api/prompt-workflow/startup'))return {prompt:'当前工作区的默认启动指令',mode:'default',source_files:[]};
              if(url.startsWith('/api/wiki/guide'))return window.fixtureGuide;
              if(url.startsWith('/api/wiki/status'))return {enabled:true,schedule:{arrangement:'paused',label:'调度已暂停'}};
            }
            async function apiPost(){return {}}
            async function copyText(value){window.copiedPrompt=value;return true}
            function showToast(){}
            </script><script defer src='/static/prompt-workflow.js'></script>""".replace("__GUIDE__", json.dumps(GUIDE, ensure_ascii=False).replace("</", "<\\/"))
            html = html.replace("</body>", boot + "</body>")
            body, kind = html.encode("utf-8"), "text/html"
        elif path.startswith("/static/"):
            file = (ROOT / path.lstrip("/")).resolve()
            if not file.is_relative_to(ROOT / "static") or not file.is_file():
                self.send_error(404)
                return
            body = file.read_bytes()
            kind = "text/css" if file.suffix == ".css" else "text/javascript" if file.suffix == ".js" else "image/png"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    from playwright.sync_api import sync_playwright
    global GUIDE
    workspace_temp = tempfile.TemporaryDirectory()
    context = initialize_workspace(Path(workspace_temp.name) / "synthetic-wiki")
    GUIDE = {"guide": knowledge_wiki_guide(), "workspace_id": context.workspace_id,
             "workspace_root": str(context.root), "daily_prompt": daily_organization_prompt(context),
             "schedule_setup_prompt": daily_schedule_setup_prompt(context)}

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    Thread(target=server.serve_forever, daemon=True).start()
    REVIEW.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1320, "height": 900}, device_scale_factor=1)
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        page.evaluate("document.querySelectorAll('.page.active').forEach(el => el.classList.remove('active'));document.querySelector('#wiki').classList.add('active')")
        page.locator("#wikiGuide .wiki-guide-run-summary summary").click()
        page.locator("#wikiGuide .wiki-guide-schedule").wait_for()
        assert "暂停" in page.locator("#wikiGuide .wiki-guide-schedule").inner_text()
        assert "GPT-6.1 Sol" in page.locator("#wikiGuide").inner_text()
        button = page.get_by_role("button", name="复制安排每日任务完整指令")
        assert button.is_visible()
        page.locator("#wikiSchedulePromptPreview").locator("xpath=..").locator("summary").click()
        button.click()
        assert page.evaluate("window.copiedPrompt === document.querySelector('#wikiSchedulePromptPreview').textContent")
        page.screenshot(path=str(REVIEW / "wiki-guide-real-style-synthetic.png"), full_page=True)
        page.evaluate("document.querySelectorAll('.page.active').forEach(el => el.classList.remove('active'));document.querySelector('#archives').classList.add('active')")
        page.locator("#refreshMotherGate").click()
        page.wait_for_function("document.querySelector('#motherDistillationStatus').textContent.includes('仍在补齐')")
        assert page.locator("#copyMotherPrompt").is_disabled()
        page.screenshot(path=str(REVIEW / "mother-gate-real-style-synthetic.png"), full_page=True)
        browser.close()
    server.shutdown()
    workspace_temp.cleanup()
    print("prompt workflow real-style synthetic review: OK")


if __name__ == "__main__":
    main()
