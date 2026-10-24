"""ComfyUI HTTP 客户端：造梦机工作台对 ComfyUI REST API 的唯一出口。

口径与聊天域 ``naiba/jobs.py`` 的 ComfyUI worker 一致（seed 上限、提交失败要带
服务端原因、历史+队列双查才判丢失），但**代码独立**——聊天域是 2026-09-20 事故
修复沉淀的稳定路径，不为工作台重构它；两边合并另立评审。

**所有请求强制直连**（2026-10-24 用户拍板）：ComfyUI 是本机/局域网渲染引擎，
不走 net_io 的代理策略，也不吃系统代理/环境变量——即使用户填的地址是域名或
被 VPN fake-IP DNS 接管的主机名，也直接对目标发起连接。注意这管不到操作系统
层面的 TUN 全局接管（那发生在路由层，应用拦不了）：同机部署用 127.0.0.1 地址
最稳，loopback 流量不经过任何物理/虚拟网卡。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import urllib.request as _urllib_request

# 硬直连 opener：空 ProxyHandler = 对任何地址都不经代理（覆盖运行设置代理、
# 系统代理与环境变量）。ComfyUI 请求一律走它。
_DIRECT_OPENER = _urllib_request.build_opener(_urllib_request.ProxyHandler({}))

# 负 seed 随机的保守上限（2026-09-20 事故教训）：核心节点 seed 是 0 ~ 2^64-1，
# 但 rgthree 等第三方节点把 seed 声明成 ±2^50，取交集上限对两类节点都合法。
SEED_RANDOM_MAX = 2 ** 50

_REJECT_MAX_CHARS = 500


def normalize_base_url(raw: Any) -> str:
    """去掉末尾斜杠；空值回落到 ComfyUI 默认地址。"""
    text = str(raw or "").strip().rstrip("/")
    return text or "http://127.0.0.1:8188"


def _summarize_reject(status: int, body: str) -> str:
    """把 ComfyUI 拒绝提交的响应压成一句可读原因（node_errors 优先）。

    与 ``naiba/jobs.py::_summarize_comfyui_reject`` 同口径：400 响应形如
    ``{"error": {...}, "node_errors": {"20": {...}}}``，必须带上节点号、节点类型
    与具体校验错误，而不是一句笼统的「提交失败」。
    """
    text = (body or "").strip()
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 - 非 JSON 响应按原文截断
        return f"HTTP {status} {text[:_REJECT_MAX_CHARS]}"
    if not isinstance(parsed, dict):
        return f"HTTP {status} {text[:_REJECT_MAX_CHARS]}"
    parts: list[str] = []
    err = parsed.get("error")
    if isinstance(err, dict) and err.get("message"):
        parts.append(str(err["message"]))
    node_errors = parsed.get("node_errors")
    if isinstance(node_errors, dict):
        for node_id, item in list(node_errors.items())[:3]:
            class_type = ""
            reasons: list[str] = []
            if isinstance(item, dict):
                class_type = str(item.get("class_type") or "")
                for node_err in (item.get("errors") or [])[:3]:
                    if isinstance(node_err, dict) and node_err.get("message"):
                        detail = str(node_err.get("details") or "").strip()
                        reasons.append(
                            str(node_err["message"]) + (f"（{detail}）" if detail else "")
                        )
            parts.append(f"节点 {node_id}" + (f"（{class_type}）" if class_type else "")
                         + (f"：{'；'.join(reasons)}" if reasons else ""))
    summary = "；".join(part for part in parts if part)
    return summary or f"HTTP {status} {text[:_REJECT_MAX_CHARS]}"


def reachable(base: str, timeout: int = 10) -> dict[str, Any] | None:
    """探测 /system_stats；可达时返回摘要（版本 + 设备），不可达返回 None。"""
    try:
        with _DIRECT_OPENER.open(f"{normalize_base_url(base)}/system_stats", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 任何网络层失败都等价于不可达
        return None
    if not isinstance(data, dict):
        return None
    system = data.get("system") if isinstance(data.get("system"), dict) else {}
    devices: list[dict[str, Any]] = []
    for item in data.get("devices") or []:
        if isinstance(item, dict):
            devices.append({
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "vram_total": item.get("vram_total"),
                "vram_free": item.get("vram_free"),
            })
    return {
        "version": str(system.get("comfyui_version") or ""),
        "python_version": str(system.get("python_version") or ""),
        "os": str(system.get("os") or ""),
        "devices": devices,
    }


def submit(base: str, workflow: dict[str, Any]) -> tuple[str, str]:
    """提交单个工作流到 /prompt，返回 (prompt_id, 失败原因)；成功时原因空串。"""
    payload = {"prompt": workflow, "client_id": uuid.uuid4().hex}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{normalize_base_url(base)}/prompt", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _DIRECT_OPENER.open(request, timeout=60) as resp:
            body = json.loads(resp.read(100000).decode("utf-8", errors="replace"))
        prompt_id = str(body.get("prompt_id") or "")
        if not prompt_id:
            return "", "ComfyUI 响应缺少 prompt_id"
        return prompt_id, ""
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不到响应体时只报状态码
            detail = ""
        reason = _summarize_reject(exc.code, detail)
        return "", reason
    except Exception as exc:  # noqa: BLE001 - 网络异常带上类型便于诊断
        return "", f"{type(exc).__name__}: {exc}"


def queue_ids(base: str) -> set[str] | None:
    """当前队列中 pending/running 的 prompt_id 集合；查询失败返回 None（不判丢失）。"""
    try:
        with _DIRECT_OPENER.open(f"{normalize_base_url(base)}/queue", timeout=15) as resp:
            data = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 查询失败由调用方保守处理
        return None
    if not isinstance(data, dict):
        return None
    ids: set[str] = set()
    for key in ("queue_running", "queue_pending"):
        for item in data.get(key) or []:
            # 队列项形如 [number, prompt_id, workflow, extra, outputs]。
            if isinstance(item, (list, tuple)) and len(item) > 1:
                ids.add(str(item[1]))
    return ids


def history_entry(base: str, prompt_id: str) -> dict[str, Any] | None:
    """读取一个 prompt 的历史条目；不在历史里返回 None（可能仍在排队/执行）。

    网络失败抛 OSError 由调用方按「暂时查不到」处理，不据此判失败。
    """
    with _DIRECT_OPENER.open(f"{normalize_base_url(base)}/history/{prompt_id}", timeout=30) as resp:
        history = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
    entry = history.get(prompt_id) if isinstance(history, dict) else None
    return entry if isinstance(entry, dict) else None


def entry_status(entry: dict[str, Any]) -> str:
    """历史条目的状态串：success / error / …（空表示未上报）。"""
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    return str((status or {}).get("status_str") or "")


def entry_error(entry: dict[str, Any]) -> str:
    """从历史条目的 status.messages 里摘一段可读错误（异常类型 + 节点名）。"""
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    messages = (status or {}).get("messages") or []
    for message in messages:
        if not (isinstance(message, list) and message):
            continue
        if str(message[0]) != "execution_error":
            continue
        details = message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
        node_type = str(details.get("node_type") or "")
        node_id = str(details.get("node_id") or "")
        exception = str(details.get("exception_message")
                        or details.get("exception_type") or "执行出错")
        where = f"节点 {node_id}（{node_type}）" if node_type else f"节点 {node_id}" if node_id else ""
        return f"{where}：{exception}" if where else exception
    return ""


def output_items(entry: dict[str, Any]) -> list[dict[str, str]]:
    """历史条目的产物清单：filename / subfolder / type（images/gifs/videos/audio）。"""
    items: list[dict[str, str]] = []
    outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    for node in (outputs or {}).values():
        if not isinstance(node, dict):
            continue
        for kind in ("images", "gifs", "videos", "audio"):
            for item in node.get(kind, []) or []:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                items.append({
                    "filename": str(item["filename"]),
                    "subfolder": str(item.get("subfolder") or ""),
                    "type": str(item.get("type") or "output"),
                })
    return items


def view_url(base: str, item: dict[str, str]) -> str:
    query = urllib.parse.urlencode({
        "filename": item["filename"],
        "subfolder": item.get("subfolder") or "",
        "type": item.get("type") or "output",
    })
    return f"{normalize_base_url(base)}/view?{query}"


def download_output(base: str, item: dict[str, str], timeout: int = 120) -> bytes:
    """拉取一个产物字节（``/view``）；失败抛 OSError/HTTPError 由调用方处理。"""
    with _DIRECT_OPENER.open(view_url(base, item), timeout=timeout) as resp:
        return resp.read()


def interrupt(base: str) -> bool:
    """请求 ComfyUI 中断**正在执行**的 prompt（两步取消的第一步）。"""
    request = urllib.request.Request(
        f"{normalize_base_url(base)}/interrupt", data=b"", method="POST",
    )
    try:
        with _DIRECT_OPENER.open(request, timeout=15) as resp:
            return resp.status < 400
    except Exception:  # noqa: BLE001 - 中断失败不阻断取消流程（还有第二步）
        return False


def delete_queue_items(base: str, prompt_ids: list[str]) -> bool:
    """从 ComfyUI 队列移除指定 prompt（两步取消的第二步）。"""
    if not prompt_ids:
        return True
    data = json.dumps({"delete": [str(pid) for pid in prompt_ids]}).encode("utf-8")
    request = urllib.request.Request(
        f"{normalize_base_url(base)}/queue", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _DIRECT_OPENER.open(request, timeout=15) as resp:
            return resp.status < 400
    except Exception:  # noqa: BLE001 - 队列删除失败只记日志语义，不影响本地终态
        return False


def _enum_choices(base: str, class_type: str, field_name: str) -> list[str]:
    """读一个节点在 ``/object_info/<类名>`` 里的枚举候选；失败一律空列表。

    单节点响应比全量 object_info 小得多；任何网络/解析失败都返回空列表，
    调用方按「候选暂不可用」处理，不阻断工作流操作。
    """
    url = f"{normalize_base_url(base)}/object_info/{urllib.parse.quote(class_type)}"
    try:
        with _DIRECT_OPENER.open(url, timeout=10) as resp:
            data = json.loads(resp.read(1000000).decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 候选拿不到不算错误
        return []
    if not isinstance(data, dict):
        return []
    node = data.get(class_type)
    if not isinstance(node, dict):
        return []
    inputs = node.get("input") if isinstance(node.get("input"), dict) else {}
    required = inputs.get("required") if isinstance(inputs.get("required"), dict) else {}
    spec = required.get(field_name)
    # 枚举输入的声明形如 [[候选...], {选项}]；候选项本身是字符串列表。
    choices = spec[0] if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], (list, tuple)) else []
    return [str(item) for item in choices if str(item or "").strip()]


def checkpoint_names(base: str) -> list[str]:
    """Checkpoint 模型候选（models/checkpoints 的文件清单，去重排序）。"""
    return sorted(dict.fromkeys(_enum_choices(base, "CheckpointLoaderSimple", "ckpt_name")))


def lora_names(base: str) -> list[str]:
    """LoRA 候选（models/loras 的文件清单，去重排序）。

    两个标准加载器的 ``lora_name`` 枚举都来自同一个目录，合并去重即覆盖
    第三方加载器场景（它们的枚举内容一致）。
    """
    names: list[str] = []
    for class_type in ("LoraLoader", "LoraLoaderModelOnly"):
        names.extend(_enum_choices(base, class_type, "lora_name"))
    return sorted(dict.fromkeys(names))


def random_seed() -> int:
    return secrets.randbelow(SEED_RANDOM_MAX + 1)


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
