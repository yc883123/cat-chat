# -*- coding: utf-8 -*-
"""为「媒体就地内嵌」冒烟播种一条带 run.media 的助手消息（服务停止时执行）。

会话结构（与 P2 落库形态一致）：
- 工具 1：write_file 产出一张图（run.media 1 条 + media_truncated 自述）；
- 工具 2：vision_image_ops 产出两张图（run.media 2 条）；
- metadata.attachments = 三条的派生汇总（应被前端"末尾去重"过滤掉）；
- 另播一条旧格式消息（只有 metadata.attachments、无 run.media）验证旧会话仍走末尾网格。

用法：先确保没有源码 server 占用 data/chat.db，然后
    python verify/seed_media_message.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from naiba.storage.store import ChatStorage  # noqa: E402

TITLE = "媒体内嵌冒烟"
STORAGE = ChatStorage(ROOT / "data" / "chat.db")
# 媒体文件必须放在 /api/file 的允许根内（数据目录），否则浏览器取图 403。
FIXTURE_DIR = ROOT / "data" / "generated" / "seed_fixture"


def _make_png(name: str, color: tuple[int, int, int]) -> dict:
    """在数据目录生成一张真实 PNG + 缩略图，返回媒体记录。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    main = FIXTURE_DIR / name
    Image.new("RGB", (320, 200), color).save(main)
    thumb = FIXTURE_DIR / f"{main.stem}_thumb.webp"
    Image.new("RGB", (160, 100), color).save(thumb, format="WEBP", quality=82)
    return {"kind": "image", "name": main.name, "source": str(main), "thumb_path": str(thumb)}


def _make_gif(name: str) -> Path:
    """多帧 GIF（D2：验证首帧缩略图推导路径不再 404）。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("P", (120, 90), color) for color in (1, 2, 3)]
    main = FIXTURE_DIR / name
    frames[0].save(main, format="GIF", save_all=True, append_images=frames[1:], duration=120, loop=0)
    return main


def _ffmpeg(*args: str) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("缺少 ffmpeg：无法生成真实 mp4/mp3 冒烟素材")
    subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def _make_video(name: str) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    main = FIXTURE_DIR / name
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p", str(main))
    return main


def _make_audio(name: str) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    main = FIXTURE_DIR / name
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(main))
    return main


def _insert_run_row(run_id: str, conversation_id: str, message_id: str) -> None:
    """插一条指定 id 的 Run 行（真实链路里由 create_run 生成；播种要固定 id 便于断言）。"""
    import time

    now = int(time.time() * 1000)
    with STORAGE._connect() as db:  # noqa: SLF001 - 播种脚本专用
        db.execute(
            "INSERT INTO background_tasks(id, conversation_id, kind, message, agent_id, agent_name, "
            "status, snapshot, detail, created_at, updated_at, parent_job_id, owner_session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, conversation_id, "chat", "后台批量生成", "", "Agent", "completed", "{}",
             json.dumps({"message_id": message_id}, ensure_ascii=False), now, now, "", conversation_id),
        )


def main() -> int:
    for conversation in STORAGE.list_conversations():
        if "媒体内嵌" in str(conversation.get("title") or ""):
            STORAGE.delete_conversation(str(conversation["id"]))

    first = _make_png("seed_a.png", (200, 60, 60))
    second = _make_png("seed_b.png", (60, 160, 90))
    third = _make_png("seed_c.png", (70, 90, 200))
    gif_main = _make_gif("seed_anim.gif")
    # 首帧缩略图（真实上传管线会生成；播种时手工造，用于验证前端"推导路径"不再 404）
    frames = Image.open(gif_main)
    frames.seek(0)
    Image.open(gif_main).convert("RGB").save(FIXTURE_DIR / f"{gif_main.stem}_thumb.webp", format="WEBP", quality=82)
    video_main = _make_video("seed_video.mp4")
    audio_main = _make_audio("seed_audio.mp3")

    conversation = STORAGE.create_conversation(TITLE)
    conversation_id = str(conversation["id"])
    # 首条用户消息会覆盖会话标题（create_chat_run/add_message 口径），故标记词写进消息，
    # 冒烟脚本按标题包含"媒体内嵌"定位会话。
    STORAGE.add_message(conversation_id, "user", "媒体内嵌冒烟：生成三张图给我看看")
    STORAGE.add_message(
        conversation_id,
        "assistant",
        "已生成三张图。",
        {
            "activity": [
                {
                    "type": "tool",
                    "request_index": 1,
                    "run": {
                        "tool": "write_file",
                        "success": True,
                        "result": f"已写入 {first['source']}（12 字符）",
                        "arguments": {"path": first["source"]},
                        "media": [first],
                        "media_truncated": {"total": 25, "shown": 1, "kinds": {"image": {"total": 25, "shown": 1}}},
                    },
                },
                {
                    "type": "tool",
                    "request_index": 2,
                    "run": {
                        "tool": "vision_image_ops",
                        "success": True,
                        "result": json.dumps({"paths": [second["source"], third["source"]]}, ensure_ascii=False),
                        "arguments": {"op": "crop"},
                        "media": [second, third],
                    },
                },
                {"type": "prose", "request_index": 2, "text": "三张图已生成。"},
            ],
            "tool_runs": [
                {"tool": "write_file", "success": True, "result": f"已写入 {first['source']}（12 字符）",
                 "arguments": {"path": first["source"]}, "media": [first],
                 "media_truncated": {"total": 25, "shown": 1, "kinds": {"image": {"total": 25, "shown": 1}}}},
                {"tool": "vision_image_ops", "success": True,
                 "result": json.dumps({"paths": [second["source"], third["source"]]}, ensure_ascii=False),
                 "arguments": {"op": "crop"}, "media": [second, third]},
            ],
            # 派生汇总（后端 union_run_media 的产物）：前端应把它过滤掉，避免与就地媒体重复
            "attachments": [first, second, third],
        },
    )

    legacy = _make_png("seed_legacy.png", (150, 150, 150))
    STORAGE.add_message(conversation_id, "user", "旧格式消息（无 run.media）")
    STORAGE.add_message(
        conversation_id,
        "assistant",
        "旧格式回复",
        {"attachments": [legacy]},
    )

    # 用户上传的音视频附件（D1）：真实 mp4/mp3，浏览器可就地播放（元数据经 Range 拉取）。
    STORAGE.add_message(
        conversation_id,
        "user",
        "这三个附件能播吗",
        {
            "attachments": [
                {"name": video_main.name, "path": str(video_main), "size": video_main.stat().st_size, "thumb_path": ""},
                {"name": audio_main.name, "path": str(audio_main), "size": audio_main.stat().st_size, "thumb_path": ""},
                # GIF 故意不给 thumb_path：前端按 <主图 stem>_thumb.webp 推导（D2 的 404 通道）
                {"name": gif_main.name, "path": str(gif_main), "size": gif_main.stat().st_size, "thumb_path": ""},
            ],
            "display_content": "这三个附件能播吗",
        },
    )
    STORAGE.add_message(conversation_id, "assistant", "可以，点开就能播放。")

    # P5：异步 Job 写回冒烟——先播一条"只提交了 Job、产物还没出现"的助手消息。
    p5_run_id = "seedjobrun0001"
    STORAGE.add_message(conversation_id, "user", "后台批量生成一张图")
    p5_assistant = STORAGE.add_message(
        conversation_id,
        "assistant",
        "已提交后台生成，完成后会自动显示。",
        {
            "run_id": p5_run_id,
            "tool_runs": [{
                "tool": "comfyui_batch",
                "success": True,
                "result": json.dumps({"job_id": "seedjob0001", "status": "queued", "total": 1}, ensure_ascii=False),
                "arguments": {"wait": False},
            }],
            "attachments": [],
        },
    )
    _insert_run_row(p5_run_id, conversation_id, str(p5_assistant["id"]))

    # 「已附媒体卡」短注冒烟（计划 2026-10-21）：真实落库形态里，show_media 的展示
    # result 末尾带一条固定短注（core.tool_results.media_attached_note），media 单条。
    shown = _make_png("seed_shown.png", (120, 200, 120))
    note_tail = "[媒体附卡] 本轮已附媒体卡 1 张：图片 " + shown["name"]
    STORAGE.add_message(conversation_id, "user", "把 seed_shown.png 展示出来")
    STORAGE.add_message(
        conversation_id,
        "assistant",
        "已经把 seed_shown.png 作为媒体卡附上。",
        {
            "tool_runs": [{
                "tool": "show_media",
                "success": True,
                # 展示 run 的脱敏 result（_show_media_summary 形态）+ 短注尾行——模型上下文与此一字节。
                "result": (
                    json.dumps({
                        "status": "shown", "kind": "image", "name": shown["name"], "reason": "",
                    }, ensure_ascii=False) + "\n" + note_tail
                ),
                "arguments": {"paths": [shown["name"]]},
                "reason": "用户指名展示",
                "media": [shown],
            }],
            "attachments": [shown],
        },
    )

    print(json.dumps({
        "conversation_id": conversation_id,
        "title": TITLE,
        "inline_images": 3,
        "legacy_images": 1,
        "note_tail": note_tail,
        "user_attachments": [video_main.name, audio_main.name, gif_main.name],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
