"""执行 Studio 轮询代码：终态重试、等待提示与迟到回包隔离。"""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StudioPollingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端逻辑")
    def test_terminal_retry_and_poll_ownership(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/22-studio.js', 'utf8');
const polling = source.slice(source.indexOf('function stopStudioPolling()'), source.indexOf('function updateResultStream(text)'));
const reasoning = source.slice(source.indexOf('function updateReasoningChip()'), source.indexOf('function renderResultPanel(task)'));
const reconcile = source.slice(source.indexOf('async function reconcileActiveTask()'), source.indexOf('async function refreshStudio()'));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
function fixture() {
  const studio = { open: true, activeTaskId: 'task-A', eventCursor: 0, pollBusy: false,
    pollTimer: 1, pollGeneration: 1, taskToken: 1, streamText: '', reasoningText: '' };
  const hint = { textContent: '', dataset: {} }, renders = [], streams = [], calls = [], responses = [];
  let refreshes = 0, timer = 1;
  const context = vm.createContext({
    studio, $: selector => selector === '#studioTaskHint' ? hint : null,
    setInterval: () => ++timer, clearInterval(){},
    api: async url => { calls.push(url); const next = responses.shift(); if (next instanceof Error) throw next; return next; },
    renderResultPanel: task => { renders.push(task); hint.textContent = task.status; },
    refreshTasks: async () => { refreshes += 1; }, updateResultStream: value => streams.push(value),
  });
  vm.runInContext(polling + reasoning + reconcile, context);
  return { studio, hint, renders, streams, calls, responses, refreshes: () => refreshes,
    poll: () => vm.runInContext('pollStudioEvents()', context),
    reconcile: () => vm.runInContext('reconcileActiveTask()', context),
    stop: () => vm.runInContext('stopStudioPolling()', context),
    consume: event => { context.event = event; vm.runInContext('consumeStudioEvent(event)', context); },
  };
}
const events = (status, rows = [], last_seq = 3) => ({ status, events: rows, last_seq });
(async () => {
  const retry = fixture();
  retry.studio.streamText = '已收到正文';
  retry.responses.push(events('failed'), new Error('网络中断'));
  await retry.poll();
  assert.equal(retry.studio.pollTimer, 1, '终态快照失败不能先停轮询');
  assert.equal(retry.studio.eventCursor, 3, '已消费事件游标保留');
  assert.equal(retry.studio.streamText, '已收到正文', '保留已收到正文');
  assert.match(retry.hint.textContent, /已结束.*重试/);
  retry.responses.push(events('failed'), { task: { id: 'task-A', status: 'failed', result_partial: '已收到正文' } });
  await retry.poll();
  assert.match(retry.calls[2], /after=3$/);
  assert.equal(retry.renders.length, 1);
  assert.equal(retry.studio.pollTimer, null);
  assert.equal(retry.refreshes(), 1);

  const lag = fixture();
  lag.responses.push(events('done'), { task: { status: 'running' } });
  await lag.poll();
  assert.equal(lag.renders.length, 0, '旧运行态快照不能覆盖终态信号');
  assert.equal(lag.studio.pollTimer, 1);
  lag.responses.push(events('done'), { task: { status: 'done' } });
  await lag.poll();
  assert.equal(lag.renders.length, 1);

  const progress = fixture();
  progress.consume({ type: 'reasoning_delta', content: '思考片段' });
  assert.match(progress.hint.textContent, /思考中/);
  progress.consume({ type: 'status', message: '等待模型继续输出…（已 15 秒无新内容）' });
  progress.consume({ type: 'reasoning_delta', content: '' });
  progress.consume({ type: 'delta', content: '' });
  assert.match(progress.hint.textContent, /等待模型/);
  assert.equal(progress.studio.reasoningText, '思考片段');
  progress.consume({ type: 'delta', content: '正文' });
  assert.match(progress.hint.textContent, /正在生成.*2 字/);
  progress.consume({ type: 'reasoning_delta', content: '补充思考' });
  assert.match(progress.hint.textContent, /正在生成/);
  assert.deepEqual(progress.streams, ['正文']);

  const switched = fixture(), oldSnapshot = deferred();
  switched.responses.push(events('done'), oldSnapshot.promise);
  const oldPoll = switched.poll();
  await new Promise(resolve => setImmediate(resolve));
  switched.studio.activeTaskId = 'task-B';
  switched.studio.taskToken += 1;
  switched.stop();
  switched.hint.textContent = '新任务提示';
  oldSnapshot.reject(new Error('旧任务读取失败'));
  await oldPoll;
  assert.equal(switched.renders.length, 0);
  assert.equal(switched.hint.textContent, '新任务提示');

  const reopened = fixture(), oldEvents = deferred(), newEvents = deferred();
  reopened.responses.push(oldEvents.promise);
  const oldRequest = reopened.poll();
  reopened.stop();
  reopened.responses.push(newEvents.promise);
  const newRequest = reopened.poll();
  oldEvents.resolve(events('running', [{ type: 'delta', content: '过期正文' }], 9));
  await oldRequest;
  assert.equal(reopened.studio.streamText, '', '关闭后重进同一任务也要隔离旧响应');
  assert.equal(reopened.studio.eventCursor, 0);
  assert.equal(reopened.studio.pollBusy, true, '旧 finally 不能解开新一轮 busy');
  newEvents.resolve(events('running', [{ type: 'delta', content: '当前正文' }], 1));
  await newRequest;
  assert.equal(reopened.studio.streamText, '当前正文');
  assert.equal(reopened.studio.pollBusy, false);

  const reconnect = fixture();
  reconnect.responses.push(new Error('服务暂时断开'), events('running', [{ type: 'status', message: '已恢复模型状态' }]));
  await reconnect.reconcile();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(reconnect.studio.pollTimer, '重进时首次快照失败仍恢复轮询');
  assert.equal(reconnect.hint.textContent, '已恢复模型状态');
  const deleted = fixture();
  deleted.responses.push(Object.assign(new Error('任务不存在'), { status: 404 }));
  await deleted.reconcile();
  assert.equal(deleted.studio.pollTimer, null, '已删除任务停止恢复');
  assert.equal(deleted.studio.activeTaskId, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-"], input=script, cwd=ROOT, text=True,
            encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
