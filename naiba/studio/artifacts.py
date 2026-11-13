"""Immutable task-bound TXT segments and verified result snapshots.

An fsync precedes each acknowledgement and manifest replacement. Windows does
not offer a portable directory fsync: this protects process crashes, without
claiming that directory metadata survives every hardware power failure.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from naiba.core.contracts import (
    STUDIO_OUTPUT_CONTRACT_VERSION_KEY, STUDIO_OUTPUT_CONTRACT_VERSION,
    STUDIO_RESULT_ARTIFACT_KEY, STUDIO_LOGICAL_GENERATION_ID_KEY,
)
from naiba.core.exceptions import TaskCancelled

MAX_SEGMENT_BYTES = 512 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024


class ResultArtifactError(ValueError):
    """A declared artifact is incomplete, corrupt or belongs to another task."""


def has_result_contract(task: dict[str, Any]) -> bool:
    raw = task.get("input")
    data = raw if isinstance(raw, dict) else {}
    return any(key in task or key in data for key in (
        STUDIO_OUTPUT_CONTRACT_VERSION_KEY, STUDIO_RESULT_ARTIFACT_KEY,
        STUDIO_LOGICAL_GENERATION_ID_KEY,
    ))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def input_digest(data: dict[str, Any]) -> str:
    return _digest(json.dumps(data, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _check_cancel(cancel: Any) -> None:
    if cancel is not None and cancel.is_set():
        raise TaskCancelled("任务已取消")


class ResultArtifacts:
    def __init__(self, data_dir: Path, lock: Any = None):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "studio" / "results"
        self.lock = lock if lock is not None else threading.RLock()

    def _path(self, task_id: str, name: str = "manifest.json") -> Path:
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ResultArtifactError("TXT 任务身份非法")
        path = self.root / task_id / name
        if not path.resolve().is_relative_to(self.data_dir):
            raise ResultArtifactError("TXT 路径超出受管目录")
        for part in (self.root.parent, self.root, path.parent, path):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ResultArtifactError("TXT 目录不允许链接")
        return path

    def _write(self, path: Path, data: bytes, *, immutable: bool = False) -> None:
        self._path(path.parent.name, path.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if immutable and path.exists():
            if path.read_bytes() != data:
                raise ResultArtifactError("TXT 不可变片段内容冲突")
            return
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            raise ResultArtifactError("TXT持久写入失败，已确认片段仍保留") from exc
        finally:
            temp.unlink(missing_ok=True)

    def _save(self, task_id: str, manifest: dict[str, Any]) -> None:
        self._write(self._path(task_id), json.dumps(manifest, ensure_ascii=False,
                    sort_keys=True, allow_nan=False).encode("utf-8"))

    @staticmethod
    def _identity(task: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": task["id"],
                "logical_generation_id": task.get(STUDIO_LOGICAL_GENERATION_ID_KEY) or task["id"],
                "input_sha256": input_digest(task.get("input") or {})}

    def create(self, task: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            path = self._path(task["id"])
            if path.exists():
                return self.manifest(task)
            chunked = (task.get("input") or {}).get("chunked") or {}
            blocks = chunked.get("blocks") if isinstance(chunked, dict) else None
            count = len([row for row in blocks if str(row)]) if isinstance(blocks, list) else 1
            count = max(1, count)
            manifest = {"schema_version": STUDIO_OUTPUT_CONTRACT_VERSION,
                        **self._identity(task), "status": "partial",
                        "separator": "\n\n" if count > 1 else "",
                        "blocks": [{"segments": [], "final": False} for _ in range(count)],
                        "attempts": []}
            self._save(task["id"], manifest)
            return manifest

    def manifest(self, task: dict[str, Any]) -> dict[str, Any]:
        try:
            if (not isinstance(task.get("input"), dict)
                    or type(task.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY)) is not int
                    or task.get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY) != STUDIO_OUTPUT_CONTRACT_VERSION
                    or (task.get("input") or {}).get(STUDIO_OUTPUT_CONTRACT_VERSION_KEY) != STUDIO_OUTPUT_CONTRACT_VERSION
                    or not isinstance(task.get(STUDIO_LOGICAL_GENERATION_ID_KEY), str)
                    or not task.get(STUDIO_LOGICAL_GENERATION_ID_KEY)
                    or not isinstance(task.get(STUDIO_RESULT_ARTIFACT_KEY), dict)):
                raise ResultArtifactError("TXT任务声明缺失或协议不支持")
            manifest = json.loads(self._path(task["id"]).read_bytes().decode("utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                    or manifest.get("status") not in {"partial", "final"}
                    or not isinstance(manifest.get("blocks"), list) or not manifest["blocks"]
                    or any(manifest.get(key) != value for key, value in self._identity(task).items())):
                raise ResultArtifactError("TXT 清单身份或协议不匹配")
            if manifest.get("separator") != ("\n\n" if len(manifest["blocks"]) > 1 else ""):
                raise ResultArtifactError("TXT块连接协议损坏")
            if (not isinstance(manifest.get("attempts"), list)
                    or any(not isinstance(attempt, dict) for attempt in manifest["attempts"])):
                raise ResultArtifactError("TXT请求诊断清单损坏")
            for block in manifest["blocks"]:
                if (not isinstance(block, dict) or not isinstance(block.get("segments"), list)
                        or not isinstance(block.get("final"), bool)):
                    raise ResultArtifactError("TXT业务块清单损坏")
                for segment in block["segments"]:
                    if (not isinstance(segment, dict) or not isinstance(segment.get("receipt"), dict)
                            or not isinstance(segment.get("chars"), int) or isinstance(segment.get("chars"), bool)
                            or segment["chars"] < 1
                            or not isinstance(segment.get("sha256"), str)
                            or not re.fullmatch(r"[0-9a-f]{64}", segment["sha256"])):
                        raise ResultArtifactError("TXT片段清单损坏")
                    receipt = segment["receipt"]
                    if (not isinstance(receipt.get("receipt_id"), str)
                            or not re.fullmatch(r"[0-9a-f]{32}", receipt["receipt_id"])
                            or any(key not in receipt for key in ("segments", "chars", "sha256"))):
                        raise ResultArtifactError("TXT回执清单损坏")
            return manifest
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ResultArtifactError("TXT 清单缺失或损坏，未使用旧正文替代") from exc

    def _block_bytes(self, task_id: str, block_no: int, block: dict[str, Any]) -> bytes:
        parts = []
        cumulative = hashlib.sha256()
        total_chars = 0
        segments = block.get("segments")
        if not isinstance(segments, list):
            raise ResultArtifactError("TXT 片段清单损坏")
        for expected, segment in enumerate(segments, 1):
            if not isinstance(segment, dict) or segment.get("segment") != expected:
                raise ResultArtifactError("TXT 片段序号不连续")
            try:
                data = self._path(task_id, f"block-{block_no:04d}-segment-{expected:06d}.txt").read_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ResultArtifactError("TXT 片段缺失或损坏") from exc
            if (_digest(data) != segment.get("sha256") or len(text) != segment.get("chars")
                    or len(data) != segment.get("bytes")):
                raise ResultArtifactError("TXT 片段摘要或字符数不符")
            cumulative.update(data)
            total_chars += len(text)
            receipt = segment["receipt"]
            if (receipt.get("segments") != expected or receipt.get("next_segment") != expected + 1
                    or receipt.get("chars") != total_chars or receipt.get("sha256") != cumulative.hexdigest()):
                raise ResultArtifactError("TXT累计回执校验失败")
            parts.append(data)
        return b"".join(parts)

    def read(self, task: dict[str, Any], *, require_final: bool = True) -> tuple[str, dict[str, Any]]:
        with self.lock:
            manifest = self.manifest(task)
            if require_final and manifest["status"] != "final":
                raise ResultArtifactError("TXT 结果尚未完成，不能保存或发送下游")
            blocks = [self._block_bytes(task["id"], n, block)
                      for n, block in enumerate(manifest["blocks"], 1)]
            # A not-yet-started later block contributes no implicit separator.
            active = [data for data, block in zip(blocks, manifest["blocks"]) if block["segments"]]
            data = manifest["separator"].encode("utf-8").join(active)
            text = data.decode("utf-8")
            if manifest["status"] == "final":
                try:
                    snapshot = self._path(task["id"], "result.txt").read_bytes()
                except OSError as exc:
                    raise ResultArtifactError("TXT 完整文件缺失") from exc
                if (not all(block.get("final") for block in manifest["blocks"])
                        or snapshot != data or _digest(data) != manifest.get("sha256")
                        or len(text) != manifest.get("chars")):
                    raise ResultArtifactError("TXT 完整文件校验失败")
            summary = {"schema_version": 1, "status": manifest["status"], "chars": len(text),
                       "segments": sum(len(block["segments"]) for block in manifest["blocks"]),
                       "sha256": _digest(data), "blocks": len(blocks),
                       "completed_blocks": sum(bool(block.get("final")) for block in manifest["blocks"])}
            return text, summary

    def write_segment(self, task: dict[str, Any], block_no: int, segment: int, text: str,
                      *, cancel: Any = None) -> dict[str, Any]:
        if isinstance(segment, bool) or not isinstance(segment, int) or segment < 1:
            raise ResultArtifactError("TXT 段号必须是正整数")
        if not isinstance(text, str) or not text:
            raise ResultArtifactError("TXT 片段不能为空")
        data = text.encode("utf-8")
        if len(data) > MAX_SEGMENT_BYTES:
            raise ResultArtifactError("TXT 单段过大，请拆成更小片段")
        with self.lock:
            _check_cancel(cancel)
            manifest = self.manifest(task)
            if not 1 <= block_no <= len(manifest["blocks"]):
                raise ResultArtifactError("TXT 块号非法")
            block = manifest["blocks"][block_no - 1]
            segments = block["segments"]
            if segment <= len(segments):
                old = segments[segment - 1]
                if old["sha256"] != _digest(data) or old["chars"] != len(text):
                    raise ResultArtifactError("TXT 同号片段内容不同，拒绝覆盖")
                self._block_bytes(task["id"], block_no, block)
                return copy.deepcopy(old["receipt"])
            if block.get("final") or manifest["status"] == "final":
                raise ResultArtifactError("TXT 已确认完成，不能追加")
            if segment != len(segments) + 1:
                raise ResultArtifactError("TXT 段号跳跃，请按回执中的下一段号写入")
            if any(not prior.get("final") for prior in manifest["blocks"][:block_no - 1]):
                raise ResultArtifactError("TXT 前一块尚未完成")
            old_data = self._block_bytes(task["id"], block_no, block)
            if sum(row.get("bytes", 0) for b in manifest["blocks"] for row in b["segments"]) + len(data) > MAX_RESULT_BYTES:
                raise ResultArtifactError("TXT 总结果达到大小上限，已保留确认片段")
            combined = old_data + data
            receipt = {"segment": segment, "segments": segment,
                       "chars": len(combined.decode("utf-8")), "sha256": _digest(combined),
                       "next_segment": segment + 1, "receipt_id": uuid.uuid4().hex}
            self._write(self._path(task["id"], f"block-{block_no:04d}-segment-{segment:06d}.txt"), data, immutable=True)
            segments.append({"segment": segment, "chars": len(text), "bytes": len(data),
                             "sha256": _digest(data), "receipt": receipt})
            _check_cancel(cancel)
            self._save(task["id"], manifest)
            return copy.deepcopy(receipt)

    def complete_block(self, task: dict[str, Any], block_no: int, receipt: dict[str, Any],
                       *, finish_reason: str = "", cancel: Any = None) -> dict[str, Any]:
        with self.lock:
            _check_cancel(cancel)
            manifest = self.manifest(task)
            if not 1 <= block_no <= len(manifest["blocks"]):
                raise ResultArtifactError("TXT 块号非法")
            block = manifest["blocks"][block_no - 1]
            if not block["segments"]:
                raise ResultArtifactError("TXT 尚无已确认片段")
            expected = block["segments"][-1]["receipt"]
            if (type(receipt.get("segments")) is not int or type(receipt.get("chars")) is not int
                    or any(receipt.get(key) != expected[key] for key in ("receipt_id", "segments", "chars", "sha256"))):
                raise ResultArtifactError("TXT 完成回执与最新已确认片段不符")
            if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
                raise ResultArtifactError("本轮达到输出上限，TXT 仍未完成，请继续补齐后重新确认")
            self._block_bytes(task["id"], block_no, block)
            block["final"] = True
            block["finish_reason"] = str(finish_reason or "unknown")
            if all(row.get("final") for row in manifest["blocks"]):
                data = manifest["separator"].encode("utf-8").join(
                    self._block_bytes(task["id"], n, row) for n, row in enumerate(manifest["blocks"], 1))
                self._write(self._path(task["id"], "result.txt"), data, immutable=True)
                manifest.update(status="final", chars=len(data.decode("utf-8")), sha256=_digest(data))
            _check_cancel(cancel)
            self._save(task["id"], manifest)
            return self.read(task, require_final=False)[1]

    def record_attempt(self, task: dict[str, Any], finish_reason: str, *,
                       failed: bool = False, usage_known: bool = False) -> None:
        with self.lock:
            manifest = self.manifest(task)
            manifest["attempts"] = (manifest.get("attempts", []) + [{"finish_reason": str(finish_reason or "unknown"),
                "failed": failed, "usage_known": usage_known}])[-256:]
            self._save(task["id"], manifest)

    def clone(self, source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.read(source, require_final=False)
            original = self.manifest(source)
            if input_digest(source["input"]) != input_digest(target["input"]):
                raise ResultArtifactError("TXT 恢复不能改变冻结输入")
            target_manifest = copy.deepcopy(original)
            target_manifest.update(self._identity(target))
            for n, block in enumerate(original["blocks"], 1):
                for segment in block["segments"]:
                    name = f"block-{n:04d}-segment-{segment['segment']:06d}.txt"
                    self._write(self._path(target["id"], name), self._path(source["id"], name).read_bytes(), immutable=True)
            if original["status"] == "final":
                self._write(self._path(target["id"], "result.txt"), self._path(source["id"], "result.txt").read_bytes(), immutable=True)
            target_manifest["resumed_from"] = source["id"]
            self._save(target["id"], target_manifest)
            return self.read(target, require_final=False)[1]

    def remove(self, task_id: str) -> None:
        with self.lock:
            directory = self._path(task_id).parent
            if directory.exists():
                for path in directory.iterdir():
                    checked = self._path(task_id, path.name)
                    if checked.is_file():
                        checked.unlink()
                directory.rmdir()
