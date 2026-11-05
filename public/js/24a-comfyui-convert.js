// 批量转换与提词库共用同一份内置模板；图片顺序就是发给模型的图号顺序。
export const BATCH_H3_REFERENCE_TEMPLATE = "builtin:h3-ref2va";

export function buildBatchConvertRequest(detail, mapping, item, convert, fixed = {}) {
  const template = String(convert.template || "");
  const body = {
    model_key: convert.provider,
    model_name: convert.model,
    user_request: `剧本：\n${String(item.script || "").trim()}`,
  };
  if (!template) body.item_id = "h3-007-h3-i2va";
  else if (template === BATCH_H3_REFERENCE_TEMPLATE) body.item_id = "h3-014-h3-ref2va";
  else body.custom_id = template;

  const slotOf = (address) => (detail?.params || []).find((slot) =>
    String(slot.node_id) === String(address?.node_id) && String(slot.field_name) === String(address?.field_name));
  const ports = (mapping?.media_ports || []).filter((port) => port.checked);
  const images = [];
  const imageRoles = [];
  const unsent = [];
  ports.forEach((port, index) => {
    const file = item.files?.[String(index)];
    if (!file?.path) return;
    const slot = slotOf(port);
    const kind = slot?.media || slot?.field_type || "image";
    if (kind !== "image") { unsent.push(kind); return; }
    images.push({ path: file.path, name: file.name || "", thumb_path: file.thumb_path || "" });
    const downstream = (slot?.connections || []).map(connection =>
      `${connection.target_node_id || "?"}.${connection.target_field || "?"}`).join("、");
    imageRoles.push(`图${images.length}：节点 ${port.node_id}，字段 ${port.field_name}，标签 ${slot?.label || "未提供"}${downstream ? `，下游 ${downstream}` : ""}。`);
  });
  if (images.length > 6) throw new Error("转换最多分析 6 张图片，请减少本条启用的图片素材口");
  if (template === BATCH_H3_REFERENCE_TEMPLATE && unsent.length) {
    throw new Error("参考/编辑转换目前只分析图片，尚未发送视频或音频；不能据此分析原片，请改用已提供的图片参考和文字剧本");
  }
  if (images.length) body.images = images;
  body.user_request += `\n\n本次实际发送 ${images.length} 张图片${images.length ? "，图号按素材口顺序编号" : "，未提供关键帧"}。`;
  if (imageRoles.length) body.user_request += `\n工作流图片槽位用途证据：\n${imageRoles.join("\n")}\n根据用户意图与槽位用途区分首帧、尾帧或普通参考；用途未知时不得猜成首帧。`;
  if (unsent.length) body.user_request += "视频和音频素材未发送给转换模型，不得声称看过或听过这些素材；只按剧本文字改写。";

  const durationSlot = slotOf(mapping?.duration_slot);
  const savedDuration = durationSlot ? fixed[`${durationSlot.node_id}|${durationSlot.field_name}`]
    ?? detail?.prompt?.[durationSlot.node_id]?.inputs?.[durationSlot.field_name] ?? durationSlot.default : undefined;
  const duration = Number(String(item.duration ?? "").trim() || savedDuration);
  if (Number.isFinite(duration) && duration > 0) body.user_request += `\n目标时长：${duration} 秒。`;
  return body;
}
