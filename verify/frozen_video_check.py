# -*- coding: utf-8 -*-
"""冻结版视频抽帧实跑（源码模式与打包版都能跑）。

存在理由（维护说明 §6.3 / §九.53）：`av` 是最容易「本地全绿、冻结版崩」的依赖——
Ci 漏装或 PyAV 的 FFmpeg DLL 没被收进产物时，**测试不会红**，只有用户机器上抽帧炸。
所以每轮编译完必须真跑一次本脚本。

用法（打包后必跑）：
    dist\\naiba-chat.exe --run-skill-script verify\\frozen_video_check.py
源码模式对照：
    .venv\\Scripts\\python.exe verify\\frozen_video_check.py

校验：`import av` + PyAV 版本 + 真实解码夹具视频（probe 元信息 / times 抽帧 / 联系表）。
退出码非 0 即失败。
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_cut.mp4"


def main() -> int:
    print(f"frozen={getattr(sys, 'frozen', False)} executable={sys.executable}")
    if not FIXTURE.is_file():
        print(f"缺少夹具视频：{FIXTURE}", file=sys.stderr)
        return 2
    try:
        import av

        print(f"PyAV {av.__version__}")
        from naiba import video as video_svc

        info = video_svc.probe_video(FIXTURE)
        print("probe:", json.dumps(info, ensure_ascii=False))
        assert abs(info["duration"] - 6.0) < 0.1, info
        assert info["width"] == 320 and info["height"] == 240, info
        assert info["video_codec"] == "h264", info

        data_dir = Path(tempfile.mkdtemp(prefix="naiba-frozen-video-"))
        result = video_svc.extract_frames(FIXTURE, data_dir, mode="times", times="0,5.5")
        frames = result["frames"]
        print("times:", [frame["t"] for frame in frames], "sheet:", result["sheet"])
        assert len(frames) == 2, result
        for frame in frames:
            path = Path(frame["path"])
            assert path.is_file() and path.stat().st_size > 0, frame
            assert Path(frame["thumb_path"]).is_file(), frame
        assert Path(result["sheet"]).is_file(), result

        keyframes = video_svc.extract_frames(FIXTURE, data_dir, mode="keyframes", contact_sheet=False)
        print("keyframes:", [frame["t"] for frame in keyframes["frames"]])
        assert [frame["t"] for frame in keyframes["frames"]] == [0.0, 5.0], keyframes

        missing = video_svc.__dict__.get("_load_av")
        assert callable(missing), "naiba.video._load_av 缺失"
        print("OK：冻结版视频抽帧可用")
        return 0
    except BaseException:  # noqa: BLE001 - 这是检查脚本：任何异常都要完整打印后判失败
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
