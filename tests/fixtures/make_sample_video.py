# -*- coding: utf-8 -*-
"""生成 tests/fixtures/sample_cut.mp4（视频抽帧测试的固定夹具）。

夹具设计（供 tests/test_video.py 硬编码基准值使用）：
- 320x240、24 fps、6.0 秒；
- 前 5 秒**完全静止**（深蓝底 + 左上角橙色方块）；
- 第 5.0 秒**硬切镜**到白色底 + 黑色大 "B"（用于验证 keyframes 模式恰好选在切点）。

需要重新生成时（当前夹具已入库，一般不需要）：
    项目根\\.venv\\Scripts\\python.exe tests\\fixtures\\make_sample_video.py

基准值（生成后由 tests/test_video.py 硬编码，CI 上不调用 ffprobe）：
    duration ≈ 6.0 秒   fps = 24.0   320x240
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "sample_cut.mp4"
WIDTH, HEIGHT, FPS = 320, 240, 24
CUT_SECONDS = 5.0
TOTAL_SECONDS = 6.0


def _frames():
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=150)
    except Exception:  # noqa: BLE001 - 老版本 Pillow 无 size 参数
        font = ImageFont.load_default()
    total = int(TOTAL_SECONDS * FPS)
    for index in range(total):
        image = Image.new("RGB", (WIDTH, HEIGHT), (18, 24, 58))
        draw = ImageDraw.Draw(image)
        if index / FPS < CUT_SECONDS:
            # 静止阶段：只有左上角一个固定方块（帧间差异 ≈ 0）
            draw.rectangle([20, 20, 120, 120], fill=(243, 146, 42))
        else:
            # 切镜后：白底 + 黑色大字母，与上一阶段差异极大
            draw.rectangle([0, 0, WIDTH, HEIGHT], fill=(250, 250, 250))
            draw.text((90, 30), "B", fill=(12, 12, 12), font=font)
        yield image


def main() -> int:
    try:
        import av
    except ImportError:
        print("需要 PyAV：\\.venv\\Scripts\\python.exe -m pip install av", file=sys.stderr)
        return 2
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(TARGET), mode="w") as container:
        stream = container.add_stream("libx264", rate=FPS)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {"g": str(FPS), "crf": "20"}  # 每秒一个关键帧，seek 更精确
        for image in _frames():
            frame = av.VideoFrame.from_image(image)
            frame.pts = None
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    print(f"已生成 {TARGET}（{TARGET.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
