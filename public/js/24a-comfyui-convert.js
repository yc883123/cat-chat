// 批量转换与提词库共用同一份内置模板；图片顺序就是发给模型的图号顺序。
export const BATCH_H3_BASE_TEMPLATE = "builtin:h3-base";
export const BATCH_H3_REFERENCE_TEMPLATE = "builtin:h3-ref2va";

const H3_BASE_ITEM = "h3-007-h3-i2va";
const H3_REFERENCE_ITEM = "h3-014-h3-ref2va";

// 槽位证据只用于自动选模板：普通参考图不能按首帧模板硬塞。
// 命中首帧/尾帧关键词时保持基础模板；只有普通参考图时自动走 Ref2VA。
function evidenceSaysKeyframe(text) {
  return /(first[_ -]?frame|last[_ -]?frame|start[_ -]?frame|end[_ -]?frame|首帧|尾帧|起始帧|结束帧)/i.test(text);
}

function evidenceSaysReference(text) {
  return /(ref[_ -]?image|reference|subject|ipadapter|参考图|参考|人物参考|角色参考)/i.test(text);
}

export function buildBatchConvertRequest(detail, mapping, item, convert, fixed = {}) {
  const template = String(convert.template || "");
  const body = {
    model_key: convert.provider,
    model_name: convert.model,
    user_request: `剧本：\n${String(item.script || "").trim()}`,
  };

  const slotOf = (address) => (detail?.params || []).find((slot) =>
    String(slot.node_id) === String(address?.node_id) && String(slot.field_name) === String(address?.field_name));
  const ports = (mapping?.media_ports || []).filter((port) => port.checked);
  const images = [];
  const imageRoles = [];
  const evidenceRows = [];
  const unsent = [];
  ports.forEach((port, index) => {
    const file = item.files?.[String(index)];
    if (!file?.path) return;
    const slot = slotOf(port);
    const kind = slot?.media || slot?.field_type || "image";
    if (kind !== "image") { unsent.push(kind); return; }
    images.push({ path: file.path, name: file.name || "", thumb_path: file.thumb_path || "" });
    const downstream = (slot?.connections || []).map(connection => {
      const targetClass = detail?.prompt?.[connection.target_node_id]?.class_type;
      return `${connection.target_node_id || "?"}.${connection.target_field || "?"}`
        + (targetClass ? ` (${targetClass})` : "");
    }).join("、");
    imageRoles.push(`图${images.length}：节点 ${port.node_id}，字段 ${port.field_name}，标签 ${slot?.label || "未提供"}${downstream ? `，下游 ${downstream}` : ""}。`);
    evidenceRows.push([slot?.label, slot?.node_title, slot?.class_type, downstream]
      .filter(Boolean).join(" "));
  });
  if (images.length > 6) throw new Error("转换最多分析 6 张图片，请减少本条启用的图片素材口");

  const evidence = evidenceRows.join("\n");
  const autoReference = !template && !evidenceSaysKeyframe(evidence) && evidenceSaysReference(evidence);
  if (!template) body.item_id = autoReference ? H3_REFERENCE_ITEM : H3_BASE_ITEM;
  else if (template === BATCH_H3_BASE_TEMPLATE) body.item_id = H3_BASE_ITEM;
  else if (template === BATCH_H3_REFERENCE_TEMPLATE) body.item_id = H3_REFERENCE_ITEM;
  else if (template.startsWith("catalog:")) body.item_id = template.slice("catalog:".length);
  else body.custom_id = template;
  const usesReferenceTemplate = body.item_id === H3_REFERENCE_ITEM;

  if (usesReferenceTemplate && unsent.length) {
    throw new Error("参考/编辑转换目前只分析图片，尚未发送视频或音频；不能据此分析原片，请改用已提供的图片参考和文字剧本");
  }
  if (images.length) body.images = images;
  body.user_request += `\n\n本次实际发送 ${images.length} 张图片${images.length ? "，图号按素材口顺序编号" : "，未提供关键帧"}。`;
  if (imageRoles.length) body.user_request += `\n工作流图片槽位用途证据：\n${imageRoles.join("\n")}\n根据用户意图与槽位用途区分首帧、尾帧或普通参考；用途未知时不得猜成首帧。`;
  if (autoReference) body.user_request += "\n槽位证据表明这些图片是普通参考图：按 Ref2VA 参考写法处理，不要当首帧，也不要向用户提问。";
  if (unsent.length) body.user_request += "视频和音频素材未发送给转换模型，不得声称看过或听过这些素材；只按剧本文字改写。";

  const durationSlot = slotOf(mapping?.duration_slot);
  const savedDuration = durationSlot ? fixed[`${durationSlot.node_id}|${durationSlot.field_name}`]
    ?? detail?.prompt?.[durationSlot.node_id]?.inputs?.[durationSlot.field_name] ?? durationSlot.default : undefined;
  const duration = Number(String(item.duration ?? "").trim() || savedDuration);
  if (Number.isFinite(duration) && duration > 0) body.user_request += `\n目标时长：${duration} 秒。`;
  // 指认了第二段提示词：要求模型按 ---- 分行输出两段，前端拆开分别写入两个写入位。
  const prompt2Point = (mapping?.extra_points || []).find((row) => row?.kind === "prompt" && row?.slot);
  if (prompt2Point) {
    const labelOf = (slot) => (slot ? `${slot.node_id} · ${slot.label || slot.field_name}` : "");
    const anchorOf = (slot) => {
      if (!slot) return "";
      const key = `${slot.node_id}|${slot.field_name}`;
      const saved = fixed?.[key]
        ?? detail?.prompt?.[slot.node_id]?.inputs?.[slot.field_name] ?? slot.default;
      return String(saved ?? "").trim().slice(0, 320);
    };
    const firstSlot = slotOf(mapping?.prompt_slot);
    const secondSlot = slotOf(prompt2Point.slot);
    body.user_request += `\n\n输出协议：这份工作流要把提示词分两处注入，最终输出必须拆成两段，两段之间用只含四个减号的一行（----）分隔；`
      + `第一段给「${labelOf(firstSlot)}」，第二段给「${labelOf(secondSlot)}」；两段都必须是可直接使用的成品提示词正文。`
      + `除两段正文和分隔行外不得输出标题、注释或任何说明文字。`;
    const firstAnchor = anchorOf(firstSlot);
    const secondAnchor = anchorOf(secondSlot);
    if (firstAnchor) body.user_request += `\n第一段写入位的当前内容（口径参考，不是要保留的原文）：${firstAnchor}`;
    if (secondAnchor) body.user_request += `\n第二段写入位的当前内容（口径参考，不是要保留的原文）：${secondAnchor}`;
  }
  // 批量转换只要成品提示词：把这条放在请求末尾，模型最后看到的是输出纪律。
  body.user_request += "\n直接输出最终提示词，不提问、不要求确认、不说明规则；信息不全按现有剧本和素材补齐。";
  return body;
}
