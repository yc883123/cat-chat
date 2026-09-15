// 任务面板渲染校验（无浏览器）：把 06 / 08 里的面板渲染函数抽出来**真执行**，断言产出 HTML。
//
// 本沙箱里 Playwright 未安装（Edge 也起不来），所以按既有做法（见 media_markup_check.mjs）
// 用"真实执行 + 字符串断言"补上最小防线。断言的是这次改造的四条承诺：
//   1. 面板只列后台作业——chat/plan_execute 那些"回答记录"不得出现；
//   2. 组标题 = 类型 + 时间，且**不引用用户消息原文**；
//   3. 状态一律中文（含「停止中」），不把英文状态码漏到界面上；
//   4. 行内停止按钮只对活动态出现；徽标在没有活动任务时回落为总数。
//
// 用法：node verify/tasks_panel_render_check.mjs
import { readFileSync } from 'node:fs';

const root = new URL('../public/js/', import.meta.url);
const read = (name) => readFileSync(new URL(name, root), 'utf8');

/** 抽出顶层声明（到下一个顶层 function/const/let 之前）。 */
function extract(source, signature) {
  const start = source.indexOf(signature);
  if (start < 0) throw new Error(`未找到声明：${signature}`);
  const rest = source.slice(start);
  const next = rest.slice(1).search(/\n(?:export )?(?:function|const|let) /);
  return next < 0 ? rest : rest.slice(0, next + 1);
}

const tasksJs = read('06-tasks-plans.js');
const conversationsJs = read('08-conversations.js');

const code = [
  extract(tasksJs, 'export const TASK_KIND_LABELS'),
  extract(tasksJs, 'export function taskKindLabel('),
  extract(tasksJs, 'export function taskStatusLabel('),
  extract(conversationsJs, 'export function taskElapsed('),
  extract(conversationsJs, 'export function formatTaskTime('),
  extract(conversationsJs, 'export function taskGroupTitle('),
  extract(conversationsJs, 'const TASK_DETAIL_KEY_LABELS'),
  extract(conversationsJs, 'function taskDetailRows('),
  extract(conversationsJs, 'function taskRowMarkup('),
  extract(conversationsJs, 'function renderTaskSummary('),
  extract(conversationsJs, 'export function renderRunTasks('),
].join('\n').replace(/^export /gm, '');

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));

const activeTaskStatuses = new Set(['queued', 'running', 'waiting', 'stopping', 'cancelling']);

// 极简 DOM 桩：只需要 textContent / innerHTML / hidden / classList。
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    const classes = new Set();
    elements.set(selector, {
      selector,
      textContent: '',
      innerHTML: '',
      hidden: false,
      classList: {
        toggle: (name, on) => { if (on) classes.add(name); else classes.delete(name); },
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
      },
    });
  }
  return elements.get(selector);
}

const state = { tasks: [], conversationId: 'conv-1', taskSyncFailed: '', taskSyncedAt: 0 };

const factory = new Function(
  'escapeHtml', 'activeTaskStatuses', 'state', '$',
  `${code}\n;return { renderRunTasks, taskGroupTitle, taskStatusLabel, taskKindLabel };`,
);
const panel = factory(escapeHtml, activeTaskStatuses, state, element);

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${!ok && detail ? `  -> ${detail}` : ''}`);
  if (!ok) failures.push(label);
}

const USER_MESSAGE = '这句话不该出现在任务面板里';
const jobRunning = {
  id: 'job-run', kind: 'comfyui', conversation_id: 'conv-1', parent_job_id: 'run-1',
  status: 'running', created_at: Date.now() - 60000, updated_at: Date.now(),
  current_step: '已提交 2/3', progress: 40, attempt: 3, detail: {}, checkpoint: {},
};
const jobFailed = {
  id: 'job-fail', kind: 'comfyui', conversation_id: 'conv-1', parent_job_id: 'run-1',
  status: 'failed', created_at: Date.now() - 60000, updated_at: Date.now(),
  error: '第 1 段提交失败', detail: {}, checkpoint: {},
};
const jobDone = {
  id: 'job-done', kind: 'comfyui', conversation_id: 'conv-1', parent_job_id: 'run-2',
  status: 'completed', created_at: Date.now() - 120000, updated_at: Date.now(),
  detail: {}, checkpoint: {},
};
const replyRow = {
  id: 'reply-1', kind: 'chat', conversation_id: 'conv-1', parent_job_id: '',
  status: 'completed', message: USER_MESSAGE, detail: {}, checkpoint: {},
};

state.tasks = [replyRow, jobRunning, jobFailed, jobDone];
panel.renderRunTasks();
const html = element('#taskList').innerHTML;
const summary = element('#taskSummary');

check('面板列出后台作业', html.includes('job-run') && html.includes('job-fail'), html.slice(0, 200));
check('回答记录（chat 行）不出现在面板', !html.includes('reply-1'), html.slice(0, 200));
check('组标题 = 类型 + 时间（同批显示 ×N）', /ComfyUI 生成 ×2 · /.test(html), html.slice(0, 300));
check('组标题不引用用户消息原文', !html.includes(USER_MESSAGE), html.slice(0, 300));
check('状态一律中文', html.includes('运行中') && html.includes('失败') && html.includes('已完成'), html.slice(0, 300));
check('行内错误/步骤可见', html.includes('第 1 段提交失败') && html.includes('已提交 2/3'), html.slice(0, 400));
check('活动作业有停止按钮、终态没有',
  html.includes('data-task-cancel="job-run"')
  && !html.includes('data-task-cancel="job-fail"')
  && !html.includes('data-task-cancel="job-done"'));
check('详情按钮只在有内容时出现', html.includes('data-task-detail="job-fail"') && !html.includes('data-task-detail="job-done"'));
check('统计条口径', summary.textContent === '共 3 · 运行中 1 · 失败 1 · 已完成 1', summary.textContent);
check('徽标显示活动数', element('#taskCount').textContent === '1', element('#taskCount').textContent);

// 停止中：既要有中文，也要有活动态按钮
state.tasks = [{ ...jobRunning, status: 'stopping' }, jobFailed, jobDone];
panel.renderRunTasks();
const stoppingHtml = element('#taskList').innerHTML;
check('stopping 显示为「停止中」', stoppingHtml.includes('停止中'), stoppingHtml.slice(0, 300));
check('stopping 仍算活动态（保留停止按钮）', stoppingHtml.includes('data-task-cancel="job-run"'));

// 全部终态：徽标回落为总数并弱化（否则跑完就变 0，用户以为从来没有过任务）
state.tasks = [jobFailed, jobDone];
panel.renderRunTasks();
check('无活动任务时徽标回落为总数并弱化',
  element('#taskCount').textContent === '2' && element('#taskCount').classList.contains('is-idle'),
  `${element('#taskCount').textContent} idle=${element('#taskCount').classList.contains('is-idle')}`);

// 轮询失败：如实说出原因与最后成功更新
state.taskSyncFailed = 'NetworkError';
state.taskSyncedAt = Date.now();
panel.renderRunTasks();
check('同步失败有提示与最后成功时间',
  element('#taskSummary').textContent.includes('同步失败：NetworkError')
  && element('#taskSummary').textContent.includes('最后成功更新：'),
  element('#taskSummary').textContent);

// 空态
state.tasks = [];
state.taskSyncFailed = '';
panel.renderRunTasks();
check('空态文案', element('#taskList').innerHTML.includes('暂无异步任务'));

console.log(`\n结果：${failures.length ? `失败 ${failures.length} 项：${failures.join(' / ')}` : '全部通过'}`);
process.exit(failures.length ? 1 : 0);
