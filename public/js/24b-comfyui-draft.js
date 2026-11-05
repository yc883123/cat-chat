// 批量草稿只存白名单输入；运行对象、凭证和媒体字节不进入本机持久化。
export const BATCH_DRAFT_KEY = "cfBatchDraft:v1";
export const BATCH_DRAFT_MAX_CHARS = 1024 * 1024;
const VERSION = 1;
const unsafeKeys = new Set(["__proto__", "prototype", "constructor"]);
const record = (value) => value && typeof value === "object" && !Array.isArray(value);

function text(value, limit = 100000) {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string" || value.length > limit) throw new Error("草稿字段格式不正确或内容过长");
  return value;
}

function list(value, limit) {
  if (!Array.isArray(value) || value.length > limit) throw new Error("草稿条目数量或格式不正确");
  return value;
}

function address(value) {
  if (value === undefined || value === null) return null;
  if (!record(value)) throw new Error("草稿映射格式不正确");
  const node_id = text(value.node_id, 160), field_name = text(value.field_name, 240);
  if (!node_id || !field_name || unsafeKeys.has(node_id) || unsafeKeys.has(field_name)) {
    throw new Error("草稿映射地址不正确");
  }
  return { node_id, field_name };
}

function mapping(value) {
  if (value === undefined || value === null) return null;
  if (!record(value)) throw new Error("草稿映射格式不正确");
  return {
    prompt_slot: address(value.prompt_slot),
    duration_slot: address(value.duration_slot),
    resolution_slot: address(value.resolution_slot),
    media_ports: list(value.media_ports || [], 200).map((port) => ({
      ...address(port), checked: port.checked === true,
    })),
  };
}

function fixed(value) {
  if (value === undefined || value === null) return {};
  if (!record(value) || Object.keys(value).length > 1000) throw new Error("固定参数格式不正确");
  const result = {};
  for (const [key, raw] of Object.entries(value)) {
    if (key.length > 401 || !key.includes("|") || unsafeKeys.has(key)) throw new Error("固定参数地址不正确");
    if (/(?:^|[|_.-])(?:api[_-]?key|access[_-]?token|token|password|secret|credential)(?:$|[|_.-])/i.test(key)) {
      throw new Error("固定参数包含口令字段，未保存到本机");
    }
    if (typeof raw === "boolean" || (typeof raw === "number" && Number.isFinite(raw))) result[key] = raw;
    else result[key] = text(raw);
  }
  return result;
}

function files(value) {
  if (value === undefined || value === null) return {};
  if (!record(value) || Object.keys(value).length > 200) throw new Error("草稿素材格式不正确");
  const result = {};
  for (const [key, file] of Object.entries(value)) {
    if (!/^(0|[1-9][0-9]{0,2})$/.test(key) || !record(file)) throw new Error("草稿素材编号不正确");
    const path = text(file.path, 4096);
    // 上传接口返回的本地受管路径；不缓存 URL、data URI、File/Blob 或外部文件引用。
    if (!path || /^(?:[a-z]+:)?\/\//i.test(path) || /^(?:data|blob):/i.test(path)
        || !/(?:^|[\\/])uploads[\\/]/i.test(path) || /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(path)) {
      throw new Error("草稿素材不是有效的已上传文件引用");
    }
    const thumb = text(file.thumb_path, 4096);
    result[key] = { path, name: text(file.name, 512), thumb_path: thumb && !/^(?:[a-z]+:)?\/\//i.test(thumb)
      && !/^(?:data|blob):/i.test(thumb) && /(?:^|[\\/])uploads[\\/]/i.test(thumb)
      && !/(?:^|[\\/])\.\.(?:[\\/]|$)/.test(thumb) ? thumb : "" };
  }
  return result;
}

function item(value) {
  if (!record(value)) throw new Error("草稿条目格式不正确");
  const shots = value.shots === undefined ? 1 : value.shots;
  if (!Number.isInteger(shots) || shots < 1 || shots > 200) throw new Error("条目数量须在 1 到 200 之间");
  return { name: text(value.name, 512), script: text(value.script), prompt: text(value.prompt),
    duration: text(value.duration, 128), resolution: text(value.resolution, 2048), shots, files: files(value.files) };
}

function scheme(value) {
  if (!record(value)) throw new Error("草稿方案格式不正确");
  // 详情仍在恢复时，保留尚未校验的原输入，不能把加载中的空映射反写覆盖。
  const inputs = value.restoreDraft || value;
  const fingerprint = value.detail?.workflow_fingerprint || value.detail?.fingerprint || inputs.fingerprint;
  return { workflow_id: text(value.workflow_id, 256), fingerprint: text(fingerprint, 256),
    mapping: mapping(inputs.mapping), fixed: fixed(inputs.fixed),
    collapsed: value.collapsed === true, fixedOpen: value.fixedOpen === true,
    needs_review: value.restoreNeedsReview === true || inputs.needs_review === true,
    items: list(value.items, 200).map(item) };
}

function payload(draft, convert) {
  if (!record(draft) || !record(convert)) throw new Error("草稿格式不正确");
  return { version: VERSION,
    draft: { title: text(draft.title, 240), unload_after: draft.unload_after === true,
      schemes: list(draft.schemes, 50).map(scheme) },
    convert: { template: text(convert.template, 512), provider: text(convert.provider, 256), model: text(convert.model, 512) } };
}

export function encodeBatchDraft(draft, convert) {
  const raw = JSON.stringify(payload(draft, convert));
  // localStorage 通常按 UTF-16 计费，1 Mi 字符约占 2 MiB；超限报错，不截断用户文字。
  if (raw.length > BATCH_DRAFT_MAX_CHARS) throw new Error("草稿超过本机保存上限，请减少条目或文字");
  return raw;
}

export function decodeBatchDraft(raw) {
  if (typeof raw !== "string" || raw.length > BATCH_DRAFT_MAX_CHARS) throw new Error("草稿内容过大或格式不正确");
  const value = JSON.parse(raw);
  if (!record(value) || value.version !== VERSION) throw new Error("草稿版本不受支持");
  return payload(value.draft, value.convert);
}

export function batchDraftMappingValid(saved, detail, mediaKind = null) {
  const fingerprint = detail?.workflow_fingerprint || detail?.fingerprint || "";
  if (!saved.mapping) return !saved.fingerprint && !Object.keys(saved.fixed || {}).length;
  if (!saved.fingerprint || saved.fingerprint !== fingerprint) return false;
  const slots = detail?.params || [];
  const slotOf = (point) => slots.find((slot) => String(slot.node_id) === point.node_id && String(slot.field_name) === point.field_name);
  const kindOf = mediaKind || ((slot) => slot?.media || slot?.field_type || "");
  for (const name of ["prompt_slot", "duration_slot", "resolution_slot"]) {
    if (saved.mapping[name] && !slotOf(saved.mapping[name])) return false;
  }
  if (saved.mapping.media_ports.some((port) => !["image", "video", "audio"].includes(kindOf(slotOf(port))))) return false;
  return Object.keys(saved.fixed || {}).every((key) => {
    const slot = slots.find((row) => `${row.node_id}|${row.field_name}` === key);
    return slot && slot.control !== "media" && !["image", "video", "audio"].includes(kindOf(slot));
  });
}
