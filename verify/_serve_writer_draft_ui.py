# -*- coding: utf-8 -*-
"""Offline writer draft fixture. Run with project venv, --port 0 (default).

Serves the real writer markup/module and real WriterService preview/save API,
using a fresh TemporaryDirectory. Pipelines are recorded, NEVER executed.
No browser driver, provider, app startup or outbound request is used.
Open the printed URL with the official browser tool. Scenario controls and a
live request log are above the writer page; GET /__fixture/status is JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.writer.service import WriterService
from naiba.writer.pipeline import WriterPipelineManager
from naiba.writer.store import WriterStore


class Fixture:
    def __init__(self, root, *, outline_only=False):
        self.service = object.__new__(WriterService)
        self.service.store = WriterStore(root)
        # Static validation only; never start its worker or create pipeline tasks.
        self.service.pipeline = WriterPipelineManager(None, self.service.store)
        self.service._context_window = lambda _key: 131072
        self.service.data_location = lambda: {'data_dir': str(root), 'writer_root': str(self.service.store.root())}
        self.lock = threading.RLock()
        self.scenario = 'success'
        self.delay = 0
        self.saved = False
        self.requests = []
        self.previews = []
        self.pipelines = []
        self.book_ids = []
        for title in ('离线正文验收', '第二本隔离作品'):
            book = self.service.store.create_book(title=title, setup={'title': title, 'idea': '雨夜，港口传来敲门声。'})
            bid = book['id']
            self.book_ids.append(bid)
            self.service.store.save_outline(bid, sync=True, chapters=[
                {'no': 1, 'title': '旧港', 'beat': '打开信封'},
                {'no': 2, 'title': '来信', 'beat': '辨认敲门者'},
            ])
            if outline_only:
                self.service.store.save_outline(bid, parts=[
                    {'no': 1, 'title': '旧港篇', 'text': '导出大纲原文：雨夜来信。\n分部内容完整保留。'},
                    {'no': 2, 'title': '远航篇', 'text': '第二部大纲原文：扬帆出港。'},
                ])
            else:
                self.service.store.save_chapter_version(bid, 1, content='旧港原文。' * 80 + '上一章原文结束。')
                self.service.store.save_chapter_version(bid, 2, content='本章旧稿。她走到门前，听见敲门声。')

    def status(self):
        with self.lock:
            return {'scenario': self.scenario, 'delay': self.delay, 'book_ids': self.book_ids,
                    'requests': self.requests[:], 'previews': self.previews[:], 'pipelines': self.pipelines[:]}


STUB = r'''
export const $ = (selector) => document.querySelector(selector);
export const state = {bootstrap:{model_profiles:[{model_key:'offline:fixture',name:'离线替身（不执行）',model:'offline-model'}],default_model_key:'offline:fixture'}};
export function toast(text) { document.querySelector('#fixtureMessage').textContent = text; }
export async function api(url, opts={}) {
  const response = await fetch(url, {...opts, headers:{'Content-Type':'application/json'}, body:opts.body === undefined ? undefined : JSON.stringify(opts.body)});
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error || response.statusText), {status:response.status});
  return body;
}
export function applyModelSkinFor() {}
export function localFileUrl(file) { return '/__fixture/export?path=' + encodeURIComponent(file); }
export function isPywebview() { return false; }
export function saveLocalFile(url, name) {
  const link = document.createElement('a');
  link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
  return 'download';
}
let listener = null;
export function onStudioPageChange(callback) { listener = callback; }
export function setStudioPage(next) { listener?.(next, {}); }
'''


def page_html():
    source = (ROOT / 'public' / 'index.html').read_text(encoding='utf-8')
    start = source.index('<div class="studio-page-view" id="studioPageWriter"')
    end = source.index('\n      </section>', source.index('id="writerImportFile"', start))
    fragment = source[start:end].replace('data-studio-page-view="writer" hidden', 'data-studio-page-view="writer"')
    return '''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cat Chat</title>
<link rel="stylesheet" href="/styles.css"><style>body{display:block;overflow:auto;padding:12px}#studioPageWriter{display:flex;height:calc(100dvh - 160px);min-height:460px}#fixtureControls{display:flex;flex-wrap:wrap;gap:8px;padding:8px}#fixtureLog{white-space:pre-wrap;max-height:200px;overflow:auto}#fixtureMessage{color:#c65}</style></head><body>
<strong>织书台隔离验收：不连接供应商，不执行流水线</strong>
<div id="fixtureControls"><select id="fixtureScenario"><option value="success">保存成功</option><option value="save_fail">保存失败</option><option value="conflict">409 冲突</option><option value="chapter_fail">保存后章节刷新失败</option><option value="index_fail">保存后作品刷新失败</option><option value="preview_fail">预检失败</option><option value="over_budget">预算超窗</option></select>
<label>请求延迟秒 <input id="fixtureDelay" type="number" value="0" min="0" max="10" style="width:60px"></label><button id="fixtureApply">应用场景并清日志</button><button id="fixtureOtherBook">切换隔离作品</button></div>
<p id="fixtureMessage" role="status"></p><details><summary>请求顺序与预览（实时）</summary><pre id="fixtureLog"></pre></details>
''' + fragment + '''
<script type="module">
import {bindWriterEvents} from '/js/27-writer.js';
let status = await (await fetch('/__fixture/status')).json();
localStorage.removeItem('naibaWriterLastBookId');
bindWriterEvents();
window.dispatchEvent(new CustomEvent('naiba-open-domain',{detail:{domain:'writer',bookId:status.book_ids[0]}}));
document.querySelector('#fixtureApply').onclick = async () => {
 await fetch('/__fixture/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario:document.querySelector('#fixtureScenario').value,delay:Number(document.querySelector('#fixtureDelay').value)})});
 document.querySelector('#fixtureMessage').textContent='场景已应用；请编辑正文后点击操作';
};
document.querySelector('#fixtureOtherBook').onclick = () => window.dispatchEvent(new CustomEvent('naiba-open-domain',{detail:{domain:'writer',bookId:status.book_ids[1]}}));
setInterval(async () => {status=await(await fetch('/__fixture/status')).json();document.querySelector('#fixtureLog').textContent=JSON.stringify(status,null,2);},500);
window.addEventListener('error',e=>document.querySelector('#fixtureMessage').textContent='JS ERROR: '+e.message);
window.addEventListener('unhandledrejection',e=>document.querySelector('#fixtureMessage').textContent='PROMISE ERROR: '+e.reason);
</script></body></html>'''


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, fixture, **kwargs):
        self.fixture = fixture
        super().__init__(*args, directory=str(ROOT / 'public'), **kwargs)

    def log_message(self, *_args):
        pass

    def send_json(self, body, status=200):
        self.send_bytes(json.dumps(body, ensure_ascii=False).encode(), 'application/json', status)

    def send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            return self.send_bytes(page_html().encode(), 'text/html; charset=utf-8')
        if path == '/__fixture/stub.js':
            return self.send_bytes(STUB.encode(), 'text/javascript')
        if path == '/js/27-writer.js':
            text = (ROOT / 'public' / 'js' / '27-writer.js').read_text(encoding='utf-8')
            for name in ('01-core', '03-media', '22-studio'):
                text = text.replace(f'"./{name}.js"', '"/__fixture/stub.js"')
            return self.send_bytes(text.encode(), 'text/javascript; charset=utf-8')
        if path == '/__fixture/status':
            return self.send_json(self.fixture.status())
        if path == '/__fixture/export':
            target = Path(parse_qs(urlsplit(self.path).query).get('path', [''])[0]).resolve()
            allowed = (self.fixture.service.store._root / '_exports').resolve()
            if target.parent != allowed or not target.is_file():
                return self.send_json({'error': 'Fixture export not found'}, 404)
            return self.send_bytes(target.read_bytes(), 'application/octet-stream')
        if path.startswith('/api/'):
            return self.dispatch('GET', path, {})
        return super().do_GET()

    def do_POST(self):
        self.handle_body('POST')

    def do_PATCH(self):
        self.handle_body('PATCH')

    def handle_body(self, method):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        path = urlsplit(self.path).path
        if path == '/__fixture/control':
            with self.fixture.lock:
                self.fixture.scenario = str(body.get('scenario', 'success'))
                self.fixture.delay = min(10, max(0, float(body.get('delay', 0))))
                self.fixture.saved = False
                self.fixture.requests.clear()
                self.fixture.previews.clear()
                self.fixture.pipelines.clear()
            return self.send_json({'ok': True})
        self.dispatch(method, path, body)

    def dispatch(self, method, path, body):
        f = self.fixture
        with f.lock:
            f.requests.append({'method': method, 'path': path, 'body': body})
            scenario, delay = f.scenario, f.delay
        if delay and (method == 'PATCH' or path.endswith('/preview')):
            time.sleep(delay)
        if path == '/api/providers/models':
            return self.send_json({'models': ['offline-model']})
        if path == '/api/writer/location':
            return self.send_json({'writer_root': 'temporary isolated fixture', 'can_open_folder': False})
        if path == '/api/writer/books':
            return self.send_json({'books': [f.service.store.get_book(bid)['book'] for bid in f.book_ids]})
        if path == '/api/writer/generate/preview':
            if scenario == 'preview_fail':
                return self.send_json({'error': '模拟预检失败'}, 500)
            payload, status = f.service.api_preview(body)
            if scenario == 'over_budget' and status == 200:
                payload['budget'].update(context_known=True, fits=False, suggestions=['模拟超窗，禁止提交'])
            with f.lock:
                f.previews.append({'request': body, 'response': payload})
            return self.send_json(payload, status)
        parts = path.strip('/').split('/')
        if len(parts) >= 4 and parts[:3] == ['api', 'writer', 'books']:
            bid = parts[3]
            if len(parts) == 4 and method == 'GET':
                if f.saved and scenario == 'index_fail':
                    return self.send_json({'error': '模拟作品刷新失败'}, 500)
                return self.send_json(*f.service.api_get_book(bid))
            if len(parts) == 6 and parts[4] == 'chapters':
                no = int(parts[5])
                if method == 'PATCH':
                    if scenario in ('save_fail', 'conflict'):
                        return self.send_json({'error': '模拟正文保存失败/冲突'}, 409 if scenario == 'conflict' else 500)
                    payload, status = f.service.api_save_chapter(bid, no, body)
                    f.saved = status < 300
                    return self.send_json(payload, status)
                if method == 'GET':
                    if f.saved and scenario == 'chapter_fail':
                        return self.send_json({'error': '模拟章节刷新失败'}, 500)
                    return self.send_json(*f.service.api_get_chapter(bid, no))
            if len(parts) == 5 and parts[4] == 'export-file' and method == 'POST':
                return self.send_json(*f.service.api_export_file(bid, body))
            if len(parts) == 5 and parts[4] == 'pipelines' and method == 'POST':
                with f.lock:
                    f.pipelines.append({'book_id': bid, **body})
                return self.send_json({'pipeline': {'id': 'offline-recorded-only', 'kind': body['kind'],
                    'status': 'done', 'steps': [], 'request_snapshot': body}}, 201)
        # Unknown APIs fail closed; this server never proxies anything.
        return self.send_json({'error': 'Offline fixture rejects this API'}, 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--outline-only', action='store_true', help='Seed parts outlines without chapter bodies for export checks')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='writer-draft-ui-') as root:
        fixture = Fixture(Path(root), outline_only=args.outline_only)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, fixture=fixture))
        server.daemon_threads = True
        print(f'Writer draft fixture: http://127.0.0.1:{server.server_port}/', flush=True)
        print('Open 正文, choose 第2章; select scenario, edit content, then use the four draft actions.', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
