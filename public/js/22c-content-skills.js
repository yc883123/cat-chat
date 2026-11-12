// 内容生成共享 Skill 多选；只选文档，权限由服务端任务快照限定。
import { api } from "./01-core.js";

// A request receipt survives network failures and page reloads in this tab.
// Keep it until the server confirms acceptance; identical retries reuse the key.
const FOLLOWUP_RECEIPTS_KEY = "naibaStudioFollowupReceipts";
function followupReceipts() {
  const raw = sessionStorage.getItem(FOLLOWUP_RECEIPTS_KEY);
  const rows = raw ? JSON.parse(raw) : [];
  if (!Array.isArray(rows)) throw new Error("追问请求记录损坏");
  return rows.filter((row) => row && typeof row.task_id === "string"
    && typeof row.request_key === "string" && typeof row.user_request === "string" && Array.isArray(row.images));
}

export function getContentFollowup(taskId) {
  return followupReceipts().reverse().find((row) => row.task_id === taskId) || null;
}

export function prepareContentFollowup(taskId, text, images) {
  const rows = followupReceipts();
  const refs = images.map(({ path, thumb_path = "", name = "" }) => ({ path, thumb_path, name }));
  const existing = rows.find((row) => row.task_id === taskId && row.user_request === text
    && JSON.stringify(row.images) === JSON.stringify(refs));
  if (existing) {
    return { user_request: text, images: refs, request_key: existing.request_key };
  }
  const request = { user_request: text, images: refs, request_key: crypto.randomUUID() };
  sessionStorage.setItem(FOLLOWUP_RECEIPTS_KEY, JSON.stringify([
    ...rows, { task_id: taskId, ...request },
  ]));
  return request;
}

export function clearContentFollowup(taskId, requestKey) {
  const rows = followupReceipts();
  sessionStorage.setItem(FOLLOWUP_RECEIPTS_KEY,
    JSON.stringify(rows.filter((row) => row.task_id !== taskId || row.request_key !== requestKey)));
}

export function createContentSkillPicker(container, { selected = [], onChange = () => {} } = {}) {
  let ids = [...new Set(selected.map(String).filter(Boolean))];
  let skills = [];
  let generation = 0;
  let destroyed = false;
  const root = document.createElement("div");
  root.className = "content-skill-picker";
  const picked = document.createElement("div");
  picked.className = "content-skill-selected";
  picked.setAttribute("aria-label", "已选 Skill");
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "搜索 Skill，可选择多个";
  search.setAttribute("aria-label", "搜索 Skill");
  const list = document.createElement("div");
  list.className = "content-skill-options";
  list.setAttribute("aria-label", "可选 Skill");
  const status = document.createElement("small");
  status.setAttribute("role", "status");
  const reload = document.createElement("button");
  reload.type = "button";
  reload.className = "control-button tiny";
  reload.textContent = "刷新 Skill 列表";
  root.append(picked, search, list, status, reload);
  container.replaceChildren(root);

  function label(skill) { return String(skill?.alias || skill?.name || skill?.id || ""); }
  function render() {
    picked.replaceChildren();
    for (const id of ids) {
      const skill = skills.find((entry) => entry.id === id);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "content-skill-chip";
      button.title = skill ? `${label(skill)}\n${skill.name || ""}` : `未找到 Skill：${id}`;
      const text = document.createElement("span");
      text.textContent = skill ? label(skill) : `未找到：${id}`;
      const remove = document.createElement("span");
      remove.textContent = "×";
      button.setAttribute("aria-label", `移除 ${text.textContent}`);
      button.append(text, remove);
      button.addEventListener("click", () => {
        if (destroyed || !ids.includes(id)) return;
        ids = ids.filter((value) => value !== id); render(); onChange([...ids]);
      });
      picked.append(button);
    }
    const query = search.value.trim().toLowerCase();
    const matches = skills.filter((skill) => !ids.includes(skill.id)
      && [label(skill), skill.name, skill.description, skill.note].join(" ").toLowerCase().includes(query));
    list.replaceChildren();
    for (const skill of matches) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "content-skill-option";
      button.textContent = label(skill);
      button.title = [skill.name, skill.description].filter(Boolean).join("\n");
      button.addEventListener("click", () => {
        if (destroyed || ids.includes(skill.id) || !skills.includes(skill)) return;
        ids.push(skill.id); render(); onChange([...ids]);
      });
      list.append(button);
    }
    if (!matches.length) {
      const empty = document.createElement("p");
      empty.textContent = query ? "没有匹配的 Skill" : "没有更多可选 Skill";
      list.append(empty);
    }
    status.textContent = ids.length ? `已选 ${ids.length} 个，主文共同生效，参考文档按需读取。` : "请选择至少一个 Skill。";
  }
  async function refresh() {
    const token = ++generation;
    status.textContent = "正在读取 Skill…";
    reload.disabled = true;
    try {
      const result = await api("/api/skills/scan", { method: "POST", body: {} });
      if (destroyed || token !== generation) return;
      skills = Array.isArray(result.skills) ? result.skills : [];
      render();
    } catch (error) {
      if (!destroyed && token === generation) status.textContent = `Skill 列表读取失败：${error.message}，可点击刷新重试。`;
    } finally { if (!destroyed && token === generation) reload.disabled = false; }
  }
  search.addEventListener("input", render);
  reload.addEventListener("click", () => void refresh());
  render();
  void refresh();
  return {
    setSelected(values) { ids = [...new Set((values || []).map(String).filter(Boolean))]; render(); },
    getSelected() { return [...ids]; },
    refresh,
    destroy() { destroyed = true; generation += 1; root.remove(); },
  };
}

export function renderContentThread(container, task) {
  if (!container) return;
  container.replaceChildren();
  const input = task?.input || {};
  for (const message of input.history_messages || []) {
    if (!["user", "assistant"].includes(message.role) || message.tool_calls) continue;
    const value = typeof message.content === "string" ? message.content
      : (message.content || []).filter((part) => part.type === "text").map((part) => part.text).join("\n");
    if (!value) continue;
    const block = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${message.role === "user" ? "我的输入" : "生成结果"} · ${value.slice(0, 60).replace(/\s+/g, " ")}`;
    const body = document.createElement("pre");
    body.textContent = value;
    block.append(summary, body);
    container.append(block);
  }
  container.hidden = !container.childElementCount;
}
