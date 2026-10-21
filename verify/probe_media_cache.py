# -*- coding: utf-8 -*-
"""媒体附卡改动（计划 2026-10-21）的真实供应商预热对照探针。

对照口径（计划 §5）：
- 变体 base = 当前部署版本（HEAD 的 naiba/，经 git archive 解包后以
  ``NAIBA_PROBE_IMPORT_PATH`` 优先导入，与 _probe_cache_readfile_guard.py 同法）；
  变体 new = 工作区当前代码。
- 每变体跑同一组**固定语料**的代表性会话：S0 纯文本（无媒体）、S1 Pwsh 扫描出卡、
  S2 指名出卡（show_media）。每场景重复采样 N 个会话（逐字节相同的用户输入），
  第一个会话视为冷启动排除。
- 指标取同一来源：应用自身的 answer metadata.usage.requests_detail（每次模型请求
  的 input/cached/output，缓存字段由供应商 usage 提供；不报缓存字段的供应商
  只能以请求字节稳定性做应用侧验收，不做"命中率已保证"的结论）。

用法（先 base 后 new）::

    git archive HEAD naiba | tar -x -C %TEMP%\naiba-base
    set NAIBA_PROBE_IMPORT_PATH=%TEMP%\naiba-base
    .venv\\Scripts\\python.exe verify\\probe_media_cache.py --variant base
    set NAIBA_PROBE_IMPORT_PATH=
    .venv\\Scripts\\python.exe verify\\probe_media_cache.py --variant new

供应商/模型选择（默认自动探测，也可显式指定）::

    --provider-tag teynex --model-tag deepseek   # 分别按名称子串筛选
    --provider-name … --model …                  # exact 值覆盖子串

供应商与 Key 的权威来源：正式版实例配置（``%LOCALAPPDATA%\\NaibaChat\\config.json``
经 LOCALAPPDATA 语义解析，不落盘、不打印任何密钥）。本探针只发少量短会话
（每变体 3 场景 × 2 会话 × 每 respond 约 1-3 次请求），并复用应用自身的用量台账。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("NAIBA_PROBE_IMPORT_PATH") or str(ROOT))

# 内置最小化 1x1 PNG（场景装置：扫描结果要能通过落地校验）。
_MIN_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc000000301010018dd8db00000000049454e44ae426082"
)

# 固定语料（变体之间逐字节相同；发送时不作任何运行期拼接）。
# 指令刻意点名目标工具：真实模型遇到含糊措辞会四处 list_directory/绕道（基线排障实测）。
SCENARIOS: dict[str, list[str]] = {
    "s0_plain": [
        "请只回答一个字：好。不要调用任何工具。",
        "上一条你回复的是什么？只回答那一个字，不要调用任何工具。",
    ],
    "s1_scan": [
        "用 pwsh 工具执行：Get-ChildItem '<SCAR_DIR1>' | Select-Object -ExpandProperty FullName。"
        "执行完直接告诉我目录里有哪些图片文件，不要调用 vision_analyze 或其他工具，不要分析图片内容。",
        "上一轮列出的图片文件名和数量我已经知道了。只回答：pwsh 那条命令返回的行数。不要调用任何工具。",
    ],
    "s2_show": [
        "用 show_media 工具把 <CLIP_FI1> 这个视频展示出来，不要做内容分析，直接出卡。",
        "刚才那个视频已展示了。只回答：这个视频的文件名。不要调用任何工具。",
    ],
}
REPEAT_CONVS = 2
TURN_TIMEOUT_SECONDS = 240.0


def _read_release_config() -> dict:
    """正规版配置（供应商与 Key 的权威来源；只读，不打印密钥）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or "").expanduser() / "NaibaChat"
    payload = json.loads((base / "config.json").read_text(encoding="utf-8"))
    known = [item for item in (payload.get("providers") or []) if str((item or {}).get("kind")) == "online"]
    if not known:
        raise SystemExit("正式版配置里没有 online 供应商")
    proxy = {"proxy": payload["proxy"]} if isinstance(payload.get("proxy"), dict) else {}
    return {"providers": known, **proxy}


def _pick_provider(providers: list[dict], provider_tag: str) -> dict:
    """按名称/base 子串挑一个在线供应商（含 Key；绝不打印）。"""
    candidates = [
        item for item in providers
        if provider_tag and provider_tag.lower() in (
            f"{item.get('name') or ''} {item.get('base_url') or ''}".lower()
        )
    ]
    if not candidates:
        raise SystemExit(f"没有匹配 --provider-tag 的供应商：{provider_tag}")
    return dict(candidates[0])


def _pick_model(provider: dict, app: object, model_tag: str, exact: str) -> str:
    try:
        models = app.models.list_online_models(provider)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 列表失败并不致命：允许 --model 显式指定
        print(f"[probe] 模型列表获取失败（可 --model 显式指定）：{exc}")
        return ""
    names = []
    for item in models or []:
        name = item.get("id") if isinstance(item, dict) else str(item)
        if isinstance(name, str) and name:
            names.append(name)
    if exact:
        return next((name for name in names if name == exact), "")
    hit = next((name for name in names if model_tag and model_tag.lower() in name.lower()), "")
    if not hit and names:
        hit = names[0]
    if hit:
        print(f"[probe] 选择模型：{hit}（候选 {len(names)} 个）")
    return hit


def _build_app(root: Path, provider: dict, proxy: dict) -> object:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.paths import PathContext

    root.mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(root, root / "config.json")
    paths.public_dir = ROOT / "public"
    (root / "data").mkdir(parents=True, exist_ok=True)
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": 0,
        "access_token": "",
        "data_dir": str(root / "data"),
        "workspace_dir": str(ws),
        "skills_dirs": [],
        "mcp_servers": [],
        "providers": [provider],
        **proxy,
    })
    config.save()
    config.set_default_model_key(f"online:{provider.get('id')}")
    app = NaibaChatApp(paths=paths)
    return app


def _write_fixtures(root: Path) -> dict[str, str]:
    """场景装置（真实存在、可通过落地校验的媒体文件），返回语料占位替换值。"""
    ws = root / "workspace"
    frames = ws / "frames"
    clips = ws / "clips"
    frames.mkdir(parents=True, exist_ok=True)
    clips.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        for name in ("a.png", "b.png"):
            Image.new("RGB", (2, 2), (240, 244, 248)).save(frames / name, format="PNG")
    except Exception:
        raise SystemExit("无法生成合法 PNG 装置（坏图会诱发 vision_analyze 干扰场景）")
    (clips / "01-09.mp4").write_bytes(
        bytes.fromhex("00000018667479706d703432000000006d70343269736f6d00") + b"\x00" * 1024
    )
    return {
        "<SCAR_DIR1>": str(frames.resolve()),
        "<CLIP_FI1>": "01-09.mp4",
    }


def _await_run(app: object, run_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        task = app.storage.get_background_task(run_id) or {}  # type: ignore[attr-defined]
        status = str(task.get("status") or "")
        if status in {"completed", "failed", "cancelled", "error"} or (
            task.get("finished") and status not in {"running", "queued", "waiting", "cancelling"}
        ):
            return task
        time.sleep(0.2)
    return {"status": f"timeout(status={status})"}


def _run_turn(app: object, conversation_id: str, message: str, model_key: str, model_name: str) -> dict:
    run = app.runs.submit({  # type: ignore[attr-defined]
        "conversation_id": conversation_id,
        "message": message,
        "attachments": [],
        "model_key": model_key,
        "model_name": model_name,
    })
    task = _await_run(app, str(run["id"]), TURN_TIMEOUT_SECONDS)
    return {"run_id": str(run["id"]), "status": str(task.get("status") or "")}


def _collect_conversation(app: object, conversation_id: str) -> dict:
    """读回本轮会话的全部 assistant 消息：逐请求用量 + 工具调用事实 + 附卡注。"""
    conversation = app.storage.get_conversation(conversation_id)  # type: ignore[attr-defined]
    turns: list[dict] = []
    index = 0
    for message in conversation.get("messages") or []:
        if str(message.get("role") or "") != "assistant":
            continue
        index += 1
        metadata = message.get("metadata") or {}
        usage = metadata.get("usage") or {}
        tools = [str(run.get("tool") or "") for run in metadata.get("tool_runs") or [] if isinstance(run, dict)]
        notes = [
            str(run.get("result") or "").rsplit("\n", 1)[-1]
            for run in metadata.get("tool_runs") or []
            if isinstance(run, dict) and str(run.get("result") or "").find("[媒体附卡]") >= 0
        ]
        turns.append({
            "turn": index,
            "tools": tools,
            "note_tail": notes,
            "usage": {
                "cache_hit_rate": usage.get("cache_hit_rate"),
                "requests_detail": usage.get("requests_detail") or [],
            },
        })
    return {"conversation_id": conversation_id, "turns": turns}


def _summary(rows: list[dict]) -> dict:
    """预热采样的小结：turn2（历史回放轮）的命中率/缓存 token 中位数。

    ``rows`` 是 metadata.usage.requests_detail 的明细行（只有
    input/cached/output token，没有顶层 cache_hit_rate 键）；命中率在场内现算。
    """
    rows = [
        row for row in rows
        if int(row.get("input_tokens") or 0) > 0
        and int(row.get("cached_tokens") or 0) is not None
    ]
    rates = [int(row["cached_tokens"]) / int(row["input_tokens"]) * 100 for row in rows]
    cached = [int(row["cached_tokens"]) for row in rows]
    inputs = [int(row["input_tokens"]) for row in rows]
    return {
        "samples": len(rows),
        "cache_hit_median_pct": round(statistics.median(rates), 1) if rates else None,
        "cached_median": int(statistics.median(cached)) if cached else None,
        "input_median": int(statistics.median(inputs)) if inputs else None,
        "uncached_median": (
            int(statistics.median(inputs)) - int(statistics.median(cached))
            if inputs and cached else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["base", "new"], default="new")
    parser.add_argument("--provider-tag", default="teynex")
    parser.add_argument("--provider-name", default="")
    parser.add_argument("--model-tag", default="deepseek")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--synthetic-base", default="",
        help="不读正式版配置，改用这个假地址构造冒烟供应商（不可达地址=链路冒烟）",
    )
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "_probe_media_cache.out.json"))
    args = parser.parse_args()

    release = _read_release_config() if not args.synthetic_base else {"providers": [], "proxy": {}}
    if args.synthetic_base:
        provider = {"id": "probe-smoke", "kind": "online", "name": "smoke", "base_url": args.synthetic_base,
                    "api_key": "sk-smoke", "request_format": "openai_chat", "model": "fake-model"}
    elif args.provider_name:
        provider = next(
            (dict(p) for p in release["providers"] if p.get("name") == args.provider_name),
            {},
        )
        if not provider:
            raise SystemExit(f"没有名为 {args.provider_name} 的在线供应商")
    else:
        provider = _pick_provider(release["providers"], args.provider_tag)

    root = Path(tempfile.mkdtemp(prefix="naiba-probe-cache-"))
    app = _build_app(root, provider, {k: v for k, v in release.items() if k != "providers"})
    values = _write_fixtures(root)
    model_key = f"online:{provider.get('id')}"

    # 模型名：--model 显式优先，否则按 tag 从 /models 列表确定性挑选；列表失败回落 tag 直拼。
    chosen_model = args.model or _pick_model(provider, app, args.model_tag, "") or args.model_tag
    if not chosen_model:
        raise SystemExit("没有可用模型名；请用 --model 显式指定")
    report: dict = {
        "variant": args.variant,
        "provider_id": provider.get("id"),
        "provider_name": provider.get("name"),
        "provider_base": provider.get("base_url"),
        "request_format": provider.get("request_format"),
        "model": chosen_model,
        "scenarios": {},
    }
    print(json.dumps({k: report[k] for k in ("variant", "provider_name", "request_format", "model")}, ensure_ascii=False))

    for name, script in SCENARIOS.items():
        convs = []
        for repeat in range(REPEAT_CONVS):
            conversation = app.storage.create_conversation(  # type: ignore[attr-defined]
                title=f"probe-{name}-{repeat}", agent_id="", model_key=model_key,
                model_name=chosen_model, permission_mode="full",
            )
            cid = str(conversation["id"])
            turn_logs = []
            for index, template in enumerate(script, 1):
                message = template
                for key, value in values.items():
                    message = message.replace(key, value)
                result = _run_turn(app, cid, message, model_key, chosen_model)
                conversation = app.storage.get_conversation(cid)  # type: ignore[attr-defined]
                assistant_messages = [
                    item for item in (conversation.get("messages") or [])
                    if str(item.get("role") or "") == "assistant"
                ]
                current = assistant_messages[-1].get("metadata") or {} if assistant_messages else {}
                usage = current.get("usage") or {}
                tools = [str(run.get("tool") or "") for run in (current.get("tool_runs") or []) if isinstance(run, dict)]
                note = ""
                for run in (current.get("tool_runs") or []):
                    text = str((run or {}).get("result") or "")
                    if "[媒体附卡]" in text:
                        note = text.rsplit("\n", 1)[-1]
                        break
                detail = usage.get("requests_detail") or []
                print(f"[{name}#{repeat} turn{index}] {result['status']} tools={tools} "
                      f"requests={[(d.get('input_tokens'), d.get('cached_tokens')) for d in detail]}")
                turn_logs.append({
                    "turn": index, "status": result["status"], "tool_runs": tools,
                    "note": note, "requests": detail,
                })
            convs.append(_collect_conversation(app, cid))
        report["scenarios"][name] = {"conversations": convs}

    # 按场景给 turn2（含全部历史回放的那一轮）取预热小样。
    samples_by_scenario: dict = {}
    for name, block in report["scenarios"].items():
        rows: list[dict] = []
        for conv in block["conversations"]:
            for turn in conv["turns"]:
                if turn["turn"] < 2:
                    continue
                rows.extend(turn["usage"]["requests_detail"])
        samples_by_scenario[name] = _summary(rows)
    report["turn2_summary"] = samples_by_scenario
    out = Path(args.out)
    payload = json.dumps(report, ensure_ascii=False, indent=1)
    out.write_text(payload, encoding="utf-8")
    print("== turn2 summary ==")
    for name, block in samples_by_scenario.items():
        print(f"  {name}: {json.dumps(block, ensure_ascii=False)}")
    print(f"[probe] 完整报告写入 {out}")


if __name__ == "__main__":
    main()
