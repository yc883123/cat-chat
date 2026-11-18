# -*- coding: utf-8 -*-
"""隔离冒烟：造梦机「对话参数」弹窗（计划 2026-11-17 §5.4/§8；不入库）。

临时根目录实例 + 预置一份 API 工作流 + Playwright：工作流卡「对话参数」入口、
绑定添加/保存/重置、指纹提示、桌面与 375px 视口，无 JS 报错。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = int(os.environ.get("NAIBA_CHAT_CONFIG_PORT", "8833"))
BASE = f"http://127.0.0.1:{PORT}"
WORKFLOW_ID = "cw-chatcfg01"


def wait_health(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def seed_workflow(data_dir: Path) -> None:
    from naiba.comfyui.store import ComfyUIStore

    prompt = {
        "5": {"class_type": "PromptText", "_meta": {"title": "正向提示词"},
              "inputs": {"text": "hello"}},
        "6": {"class_type": "EmptyLatentImage", "_meta": {"title": "空潜空间"},
              "inputs": {"width": 832, "height": 1216}},
        "7": {"class_type": "KSampler", "_meta": {"title": "采样"},
              "inputs": {"seed": -1, "steps": 20, "cfg": 7.0}},
    }
    from naiba.comfyui.params import scan_params

    store = ComfyUIStore(data_dir)
    store.save_workflow({
        "id": WORKFLOW_ID,
        "name": "对话参数冒烟流",
        "prompt": prompt,
        "workflow_fingerprint": ComfyUIStore.payload_hash(prompt),
        "params": scan_params(prompt),
        "params_version": 6,
        "created_at": 1,
        "updated_at": 1,
    })


def main() -> int:
    server = None
    log = (ROOT / "verify" / "_comfyui_chat_config_server.log").open("w", encoding="utf-8")
    code = 1
    with tempfile.TemporaryDirectory(prefix="comfyui_chatcfg_", dir=ROOT / "verify") as tmp:
        try:
            root = Path(tmp)
            data_dir = root / "data"
            seed_workflow(data_dir)
            from naiba.config import ConfigStore
            from naiba.paths import PathContext

            paths = PathContext.local(root, root / "config.json")
            paths.public_dir = ROOT / "public"
            config = ConfigStore(paths.config_path, paths=paths)
            config.data.update({
                "host": "127.0.0.1", "port": PORT, "access_token": "",
                "data_dir": str(data_dir), "workspace_dir": str(root / "workspace"),
                "skills_dirs": [str(ROOT / "skills")], "mcp_servers": [],
                # 带非空 api_key：跳过首启引导弹窗（否则它挡住一切点击）。
                "providers": [{"id": "smoke-chatcfg", "name": "冒烟供应商",
                               "model": "smoke", "api_key": "smoke-key"}],
                "comfyui_url": "http://127.0.0.1:9",
            })
            config.save()
            env = dict(os.environ)
            env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                        "NAIBA_SMOKE_ROOT": str(root)})
            server = subprocess.Popen(  # noqa: S603 - 固定 argv
                [sys.executable, str(ROOT / "verify" / "_serve_chat_config_smoke.py")],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            )
            if not wait_health():
                print("FAIL  服务未在 60 秒内就绪")
                return 1
            env_cjs = dict(os.environ)
            env_cjs["NAIBA_SMOKE_BASE"] = BASE
            env_cjs["NAIBA_SMOKE_WORKFLOW"] = WORKFLOW_ID
            env_cjs["NODE_PATH"] = os.path.expanduser("~\\node_modules")
            import shutil

            node = shutil.which("node") or "node"
            result = subprocess.run(  # noqa: S603 - 固定 argv
                [node, str(ROOT / "verify" / "comfyui_chat_config_smoke.cjs")],
                cwd=str(ROOT), env=env_cjs,
            )
            code = result.returncode
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
            log.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
