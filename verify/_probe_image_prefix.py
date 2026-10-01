# -*- coding: utf-8 -*-
"""只读探针：本地图片滑窗在「每轮重算」与「旗标回放」下的差异实测。

跑法：``.venv\\Scripts\\python.exe verify/_probe_image_prefix.py``
（探针不进测试套件，结论写进维护说明 §九 与测试注释。）

场景 A：批量出图会话——每轮 user 带 3 张真图，本地图片上限压到 4 张，只追加消息。
场景 B：同上加一轮「编辑/撤回」把上一轮那条 user 删掉 ⇒ 保留窗口往回滑。
量的是相邻两轮 history 的**公共前缀长度**（逐条按 content 的 json 字节比对），
以及每轮 ``encode_image_for_model`` 的调用次数（磁盘读 + PIL 编码的成本代理）。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from naiba.core import history as history_mod  # noqa: E402
from naiba.vision.runtime import VisionRouter  # noqa: E402

_ENCODE_CALLS = {"count": 0}


def _make_images(tmp: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        path = tmp / f"img{index}.png"
        image = Image.new("RGB", (24, 24), (index * 7 % 255, index * 13 % 255, 40))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        path.write_bytes(buffer.getvalue())
        paths.append(str(path))
    return paths


def _message(message_id: str, paths: list[str]) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": f"第 {message_id} 轮",
        "metadata": {"attachments": [{"path": path, "name": Path(path).name} for path in paths]},
    }


def _turn(messages: list[dict], *, replay_flags: bool, limit: int) -> list[dict]:
    real_encoder = history_mod.encode_image_for_model

    def counting_encode(source: str):
        _ENCODE_CALLS["count"] += 1
        return real_encoder(source)

    with mock.patch.object(history_mod, "encode_image_for_model", counting_encode):
        history = history_mod.build_model_history(messages, local_image_brain=replay_flags)
    saved = VisionRouter.LOCAL_REQUEST_IMAGE_LIMIT
    VisionRouter.LOCAL_REQUEST_IMAGE_LIMIT = limit
    try:
        capped, _note, demotions = VisionRouter._cap_local_history_images(history)
    finally:
        VisionRouter.LOCAL_REQUEST_IMAGE_LIMIT = saved
    for entry in demotions:
        message_id = entry.get("message_id") or ""
        for message in messages:
            if message["id"] != message_id:
                continue
            existing = (message["metadata"].get("local_images_capped") or {}).get("names") or []
            merged = list(dict.fromkeys([str(name) for name in existing] + list(entry["names"])))
            message["metadata"]["local_images_capped"] = {"names": merged}
    return capped


def _prefix_len(left: list[dict], right: list[dict]) -> int:
    def key(item: dict) -> str:
        return json.dumps(item.get("content"), ensure_ascii=False, sort_keys=True)

    count = 0
    for a, b in zip(left, right):
        if key(a) != key(b):
            break
        count += 1
    return count


def _run_case(title: str, *, trim_at: int | None, trim_count: int = 1) -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        paths = _make_images(tmp, 18)
        for label, replay in (("每轮重算（旗标关）", False), ("旗标回放（本次改动）", True)):
            messages: list[dict] = []
            previous: list[dict] | None = None
            line: list[str] = []
            encodes = 0
            for turn in range(6):
                messages.append(_message(f"m{turn}", paths[turn * 3:(turn + 1) * 3]))
                if trim_at is not None and turn >= trim_at:
                    # 模拟「回滚到更早的轮次重发」：删掉前面几条 user（保留窗口往回滑）。
                    for _ in range(trim_count):
                        messages.pop(-2)
                _ENCODE_CALLS["count"] = 0
                capped = _turn(messages, replay_flags=replay, limit=4)
                encodes += _ENCODE_CALLS["count"]
                if previous is not None:
                    line.append(f"轮{turn + 1} 前缀={_prefix_len(previous, capped)}/{len(previous)}")
                previous = capped
            print(f"{label} [{title}]: " + " | ".join(line) + f" | 累计编码={encodes} 次")


def main() -> None:
    _run_case("只追加", trim_at=None)
    print()
    _run_case("第 6 轮回滚 1 条", trim_at=5, trim_count=1)
    print()
    _run_case("第 6 轮回滚 3 条", trim_at=5, trim_count=3)


if __name__ == "__main__":
    main()
