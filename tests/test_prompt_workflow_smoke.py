"""Synthetic browser check of the prompt controls; no real workspace is read."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "review"


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/prompt-workflow.js":
            body = (ROOT / "static/prompt-workflow.js").read_bytes()
            kind = "text/javascript"
        else:
            body = """<!doctype html><html><meta charset='utf-8'><body>
            <section id='archives' class='active'><section id='archiveProgress'></section></section>
            <section id='wiki'><div id='wikiHub'></div></section>
            <section id='knowledgeBaseStatus'><div class='startup-instruction'><button id='copyStartupInstruction'>Copy</button></div></section>
            <button data-page='archives'>Archives</button>
            <script>
            let ready=false,copied='',saved='Default prompt',mode='default';
            async function api(url){
              if(url.startsWith('/api/prompt-workflow/distillation/prompt')){if(!ready)throw {status:409};return {prompt:'Mother prompt'};}
              if(url.startsWith('/api/prompt-workflow/distillation'))return {ready,sources:ready?['Hermes']:[],reasons:ready?[]:['Hermes 历史尚未完成']};
              if(url.startsWith('/api/prompt-workflow/startup'))return {prompt:saved,mode,source_files:[]};
              if(url.startsWith('/api/wiki/guide'))return {guide:{title:'知识 Wiki',description:'整理用途',model_choice:'可自选模型',daily_steps:['检查新增','分类证据','提出变更','审核','留结果'],schedule_notice:'以调度状态为准'},daily_prompt:'Daily prompt',schedule_setup_prompt:'Arrange prompt'};
              if(url.startsWith('/api/wiki/status'))return {schedule:{arrangement:'not_arranged'}};
            }
            async function apiPost(url,body){if(url.endsWith('/save')){saved=body.prompt;mode='custom'}else if(url.endsWith('/restore-default')){saved='Default prompt';mode='default'}else if(url.endsWith('/generate')){saved='Generated prompt';mode='generated'}return {prompt:saved,mode,source_files:[]};}
            async function copyText(value){copied=value;return true}
            function showToast(value){document.body.dataset.toast=value}
            </script><script defer src='/prompt-workflow.js'></script></body></html>""".encode("utf-8")
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    from playwright.sync_api import sync_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    Thread(target=server.serve_forever, daemon=True).start()
    REVIEW.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        page.locator("#refreshMotherGate").click()
        assert page.locator("#copyMotherPrompt").is_disabled()
        assert "尚未完成" in page.locator("#motherDistillationStatus").inner_text()
        page.evaluate("ready=true")
        page.locator("#refreshMotherGate").click()
        page.wait_for_function("!document.querySelector('#copyMotherPrompt').disabled")
        page.evaluate("ready=false")  # Changed after the visible status loaded.
        page.locator("#copyMotherPrompt").click()
        assert page.evaluate("copied") == ""
        page.locator("#generateStartupPrompt").click()
        assert page.locator("#startupPromptText").input_value() == "Generated prompt"
        page.locator("#startupPromptText").fill("Edited workspace prompt")
        page.locator("#saveStartupPrompt").click()
        page.locator("#copyStartupInstruction").click()
        page.wait_for_function("copied === 'Edited workspace prompt'")
        page.locator("#restoreStartupPrompt").click()
        assert page.locator("#startupPromptText").input_value() == "Default prompt"
        assert "未安排" in page.locator("#wikiGuide").inner_text()
        assert "检查新增" in page.locator("#wikiGuide").inner_text()
        page.screenshot(path=str(REVIEW / "prompt-workflow-synthetic.png"), full_page=True)
        browser.close()
    server.shutdown()
    print("prompt workflow synthetic headless review: OK")


if __name__ == "__main__":
    main()
