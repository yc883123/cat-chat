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
import copy
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
    def __init__(self, root, *, outline_only=False, decisions=False):
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
        self.decisions = decisions
        self.active = {}
        self.receipts = {}
        self.pipeline_sequence = 0
        self.last_book = None
        for title in ('离线正文验收', '第二本隔离作品'):
            book = self.service.store.create_book(title=title, setup={'title': title, 'idea': '雨夜，港口传来敲门声。'})
            bid = book['id']
            self.book_ids.append(bid)
            self.seed_chapters(bid, outline_only=outline_only)
        if decisions:
            self.seed_pending(self.book_ids[0], 'draft')
            self.seed_pending(self.book_ids[1], 'genre')

    def seed_chapters(self, bid, *, outline_only=False):
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

    def reset_decisions(self):
        for bid in self.book_ids:
            self.service.store.clear_chapters(bid)
            self.seed_chapters(bid)
        self.receipts.clear()
        self.seed_pending(self.book_ids[0], 'draft')
        self.seed_pending(self.book_ids[1], 'genre')

    def seed_pending(self, bid, step, *, previous=None, history=None):
        self.pipeline_sequence += 1
        fixtures = {
            'draft': ('text', None, '', ['补完正文结尾'], {'chars': 0}),
            'genre': ('genre', {'tone': '冷峻'}, '<img id="model-injection" src="invalid">', ['cat'], {}),
            'title': ('list', ['原书名'], '["原书名"]', ['补充候选'], {'actual': 1, 'target': 2}),
            'blurb': ('list', ['一段简介。\n另起一段。'], '', [], {'actual': 1, 'target': 2}),
            'chars': ('characters', [{'name': '阿舟'}, {'identity': '收信人'}], '', ['1 个角色缺少 name'], {'actual': 1, 'target': 2}),
            'split': ('chapters', [{'title': '已识别章节'}, {'beat': '一条待补标题的梗概'}], '', ['1 章缺少标题'], {'actual': 1, 'target': 2}),
            'state': ('state', {'events': [], 'characters': [{'name': '阿舟', 'location': '港口'}]}, '{"events":[]}',
                      ['至少一条章末事实'], {}),
        }
        editor, partial, raw, missing, counts = fixtures[step]
        steps = ['draft', 'state'] if step in ('draft', 'state') else [step]
        pipeline = {**(previous or {}), 'id': (previous or {}).get('id', f'offline-pipeline-{self.pipeline_sequence}'),
                    'kind': 'chapter' if step in ('draft', 'state') else 'split' if step == 'split' else 'setup_all',
                    'status': 'needs_user', 'steps': steps, 'step_index': steps.index(step),
                    'attempt': (previous or {}).get('attempt', 1), 'decision_revision': self.pipeline_sequence,
                    'decision_history': history or [], 'request_snapshot': {'model_name': 'offline-model'},
                    'error': '模拟：模型结果需要处理', 'current_task_id': ''}
        pipeline['pending_user'] = {'step': step, 'step_index': pipeline['step_index'], 'attempt': pipeline['attempt'],
            'issue': pipeline['error'], 'category': 'insufficient', 'raw': raw, 'raw_truncated': False, 'partial': partial,
            'missing': missing, 'counts': counts, 'available_actions': ['retry', 'supplement', 'skip']
            + (['accept'] if step in ('title', 'chars') else []), 'editor': editor, 'body_saved': step == 'state',
            'chapter_no': 2 if step in ('draft', 'state') else 0, 'part_no': 1 if step == 'split' else 0,
            'skip_consequence': '跳过后不写入章末状态；已保存的正文不会改动。' if step == 'state' else '跳过后本步不写回作品。'}
        self.active[bid] = pipeline
        return pipeline

    def apply_decision(self, bid, body):
        pipeline = self.active.get(bid) or {}
        pending = pipeline.get('pending_user') or {}
        key = body.get('request_key')
        old = self.receipts.get((bid, pipeline.get('id'), key))
        if old:
            return ({'pipeline': pipeline, 'decision': old[1]}, 200) if old[0] == body else ({'error': '不同操作用了同一请求标识'}, 409)
        if pipeline.get('status') != 'needs_user' or body.get('pipeline_id') != pipeline['id'] or any(
            body.get(key) != value for key, value in {'step': pending.get('step'), 'step_index': pipeline.get('step_index'),
                'attempt': pipeline.get('attempt'), 'decision_revision': pipeline.get('decision_revision')}.items()
        ):
            return {'error': '待处理状态已改变'}, 409
        action = body.get('action')
        if action not in pending['available_actions'] or not isinstance(key, str) or not key:
            return {'error': '操作不允许或缺少请求标识'}, 400
        if action == 'skip' and (body.get('confirmed') is not True or not str(body.get('reason', '')).strip()):
            return {'error': '跳过需要确认和原因'}, 400
        if action == 'supplement' and self.scenario == 'decision_invalid':
            return {'error': '模拟：补充内容未通过校验'}, 400
        step = pending['step']
        summary = {key: pending[key] for key in ('step', 'step_index', 'attempt', 'counts', 'missing', 'chapter_no', 'part_no')}
        summary.update(action=action, at=1763600000000, reason=body.get('reason', ''),
                       consequence=pending['skip_consequence'] if action == 'skip' else '')
        history = pipeline['decision_history'] + [summary]
        self.receipts[(bid, pipeline['id'], key)] = (dict(body), summary)
        if action == 'retry':
            self.seed_pending(bid, step, previous={**pipeline, 'attempt': pipeline['attempt'] + 1}, history=history)
        elif step == 'draft' and action in ('supplement', 'accept'):
            self.service.store.save_chapter_version(bid, 2, content=body['content'])
            self.seed_pending(bid, 'state', previous=pipeline, history=history)
        else:
            self.active[bid] = {**pipeline, 'status': 'done', 'step_index': len(pipeline['steps']),
                               'pending_user': None, 'decision_history': history, 'error': ''}
        return {'pipeline': self.active[bid], 'decision': summary}, 200

    def status(self):
        with self.lock:
            return {'scenario': self.scenario, 'delay': self.delay, 'book_ids': self.book_ids,
                    'requests': self.requests[:], 'previews': self.previews[:], 'pipelines': self.pipelines[:],
                    'active': copy.deepcopy(self.active)}


STUB = r'''
export const $ = (selector) => document.querySelector(selector);
export const state = {bootstrap:{model_profiles:[{model_key:'offline:fixture',name:'离线替身（不执行）',model:'offline-model'}],default_model_key:'offline:fixture'}};
export function toast(text) { document.querySelector('#fixtureMessage').textContent = text; }
export function onProvidersChanged() {}
export function randomUuid() { return crypto.randomUUID(); }
export async function loadModelCatalog() { return ['offline-model']; }
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


def page_html(decisions=False):
    source = (ROOT / 'public' / 'index.html').read_text(encoding='utf-8')
    start = source.index('<div class="studio-page-view" id="studioPageWriter"')
    end = source.index('\n      </section>', source.index('id="writerImportFile"', start))
    fragment = source[start:end].replace('data-studio-page-view="writer" hidden', 'data-studio-page-view="writer"')
    decision_controls = '''
<label>待处理阶段 <select id="fixtureStep"><option value="draft">正文</option><option value="genre">题材</option><option value="title">书名</option><option value="blurb">简介</option><option value="chars">角色</option><option value="split">章节</option><option value="state">章末状态</option></select></label>
<button id="fixtureSeed">在第一本作品加载待处理阶段</button><button id="fixtureFirstBook">切回第一本作品</button>
''' if decisions else ''
    return '''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cat Chat</title>
<link rel="stylesheet" href="/styles.css"><style>body{display:block;overflow:auto;padding:12px}#studioPageWriter{display:flex;height:calc(100dvh - 160px);min-height:460px}#fixtureControls{display:flex;flex-wrap:wrap;gap:8px;padding:8px}#fixtureLog{white-space:pre-wrap;max-height:200px;overflow:auto}#fixtureMessage{color:#c65}</style></head><body>
<strong>织书台隔离验收：不连接供应商，不执行流水线</strong>
<div id="fixtureControls"><select id="fixtureScenario"><option value="success">保存成功</option><option value="save_fail">保存失败</option><option value="conflict">409 冲突</option><option value="chapter_fail">保存后章节刷新失败</option><option value="index_fail">保存后作品刷新失败</option><option value="preview_fail">预检失败</option><option value="over_budget">预算超窗</option><option value="decision_invalid">补充校验失败</option><option value="decision_drop">处理后丢失网络响应</option></select>
<label>请求延迟秒 <input id="fixtureDelay" type="number" value="0" min="0" max="10" style="width:60px"></label><button id="fixtureApply">应用场景并清日志</button><button id="fixtureOtherBook">切换隔离作品</button>''' + decision_controls + '''</div>
<p id="fixtureMessage" role="status"></p><details><summary>请求顺序与预览（实时）</summary><pre id="fixtureLog"></pre></details>
''' + fragment + '''
<script type="module">
import {bindWriterEvents} from '/js/27-writer.js';
let status = await (await fetch('/__fixture/status')).json();
localStorage.removeItem('naibaWriterLastBookId');
bindWriterEvents();
const openBook = (index) => window.dispatchEvent(new CustomEvent('naiba-open-domain',{detail:{domain:'writer',bookId:status.book_ids[index]}}));
openBook(0);
const first = document.querySelector('#fixtureFirstBook');
if (first) first.onclick = () => openBook(0);
const seed = document.querySelector('#fixtureSeed');
if (seed) seed.onclick = async () => {
  await fetch('/__fixture/control',{method:'POST',body:JSON.stringify({step:document.querySelector('#fixtureStep').value})});
  openBook(0);
};
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
            return self.send_bytes(page_html(self.fixture.decisions).encode(), 'text/html; charset=utf-8')
        if path == '/__fixture/stub.js':
            return self.send_bytes(STUB.encode(), 'text/javascript')
        if path.startswith('/js/') and path.endswith('.js'):
            source = ROOT / 'public' / 'js' / path.removeprefix('/js/')
            if source.parent == ROOT / 'public' / 'js' and source.is_file():
                text = source.read_text(encoding='utf-8')
                for name in ('01-core', '03-media', '07-models-agents', '22-studio'):
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
                if self.fixture.decisions and body.get('reset') is True:
                    self.fixture.reset_decisions()
                if self.fixture.decisions and 'step' in body:
                    self.fixture.seed_pending(self.fixture.book_ids[0], body['step'])
                    return self.send_json({'ok': True})
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
        if path.endswith('/continuity'):
            return self.send_json({})
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
                payload, code = f.service.api_get_book(bid)
                if f.decisions and code == 200:
                    with f.lock:
                        payload['book']['active_pipeline'] = copy.deepcopy(f.active.get(bid))
                return self.send_json(payload, code)
            if len(parts) == 4 and method == 'PATCH':
                return self.send_json(*f.service.api_patch_book(bid, body))
            if len(parts) == 6 and parts[4:] == ['chapters', 'clear'] and method == 'POST':
                return self.send_json(*f.service.api_clear_chapters(bid, body))
            if len(parts) == 5 and parts[4] == 'pipeline':
                with f.lock:
                    return self.send_json({'pipeline': f.active.get(bid)})
            if f.decisions and len(parts) == 6 and parts[4:] == ['pipeline', 'decision'] and method == 'POST':
                if delay:
                    time.sleep(delay)
                with f.lock:
                    payload, code = f.apply_decision(bid, body)
                    if scenario == 'decision_drop' and code == 200:
                        f.scenario = 'success'
                        self.close_connection = True
                        return
                    return self.send_json(payload, code)
            if f.decisions and len(parts) == 6 and parts[4:] == ['pipeline', 'cancel'] and method == 'POST':
                with f.lock:
                    pipeline = f.active.get(bid) or {}
                    if pipeline.get('status') != 'needs_user' or any(body.get(key) != pipeline.get(field) for key, field in (
                        ('pipeline_id', 'id'), ('step_index', 'step_index'), ('attempt', 'attempt'), ('decision_revision', 'decision_revision'),
                    )):
                        return self.send_json({'error': '待处理状态已改变'}, 409)
                    f.active[bid] = {**pipeline, 'status': 'cancelled', 'pending_user': None, 'error': ''}
                    return self.send_json({'pipeline': f.active[bid]})
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
                    if f.active.get(bid, {}).get('status') == 'needs_user':
                        return self.send_json({'error': '请先处理本轮生成'}, 409)
                    f.pipelines.append({'book_id': bid, **body})
                    pipeline = {'id': 'offline-recorded-only', 'kind': body['kind'], 'status': 'done',
                                'steps': [], 'request_snapshot': body}
                    if f.decisions:
                        f.active[bid] = pipeline
                    return self.send_json({'pipeline': pipeline}, 201)
        # Unknown APIs fail closed; this server never proxies anything.
        return self.send_json({'error': 'Offline fixture rejects this API'}, 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--outline-only', action='store_true', help='Seed parts outlines without chapter bodies for export checks')
    parser.add_argument('--decisions', action='store_true', help='Enable simulated pending-decision scenarios')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='writer-draft-ui-') as root:
        fixture = Fixture(Path(root), outline_only=args.outline_only, decisions=args.decisions)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, fixture=fixture))
        server.daemon_threads = True
        print(f'Writer draft fixture: http://127.0.0.1:{server.server_port}/', flush=True)
        print('Use the pending-decision controls to test the Writer UI.' if args.decisions else
              'Open 正文, choose 第2章; select scenario, edit content, then use the four draft actions.', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
