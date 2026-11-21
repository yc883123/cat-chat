"""Bounded, read-only GOAL checks, independent of App, jobs and workspace code.

Public API (all returned values are JSON serializable)::

    normalize_criteria(criteria) -> list[dict]
    capture_inputs(workspace, criteria) -> dict
    execute(snapshot, cancel_event=None) -> dict
    verify_snapshot_current(snapshot, workspace=None) -> bool

Criteria accept legacy ``{path, sha256?, contains?}`` or ``kind='file'``.
``kind='local_check', check_id='json_structure', params={path, schema}``
checks a small data-only schema (type/required/properties/items, depth <= 8).
``check_id='file_count', params={paths: [...], count: N}`` counts regular
files in an explicit list, never a glob or directory walk. The check IDs can
also be used as ``kind`` shorthand. Unknown fields/check IDs are errors.

Snapshots contain base64 bytes, raw SHA-256s, missing-file records and a
fingerprint. Execution uses ONLY those bytes, not the filesystem. Recheck
``verify_snapshot_current`` before accepting evidence. Fingerprints detect
mutation; they are not signatures, permission grants or user approval.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from naiba.core.contracts import GOAL_CHECK_VERSION
from naiba.core.paths import path_within, resolved_path_key

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CRITERIA = 64
MAX_INPUT_FILES = 64
MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_NODES = 256
CHECK_IDS = ("file", "json_structure", "file_count")
_SENSITIVE_DIRS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".zcode", ".venv"})
_COMMON = {"id", "kind", "label", "stage_id", "source_line", "check_id"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(value: Any) -> str:
    return _digest(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode("ascii"))


def _keys(value: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be an object")
    if set(value) - allowed:
        raise ValueError(f"Unknown {name} fields: {sorted(set(value) - allowed)}")
    return value


def _text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or "\x00" in value:
        raise ValueError(f"Invalid {name}")
    return value


def _sensitive(parts: Any) -> bool:
    return any(p.casefold() in _SENSITIVE_DIRS or p.casefold().startswith(".env") for p in parts)


def _relative_path(raw: Any) -> str:
    """Cross-platform lexical validation, including Windows ADS/UNC paths."""
    value = _text(raw, "relative path", 512)
    windows = PureWindowsPath(value)
    parts = value.replace("\\", "/").split("/")
    if (value != value.strip() or windows.drive or windows.root or Path(value).is_absolute()
            or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", ".."} for p in parts)
            or any(any(c in p for c in ':*?<>|"') or p.endswith((" ", ".")) for p in parts)
            or _sensitive(parts)):
        raise ValueError("GOAL requires a non-sensitive workspace-relative file path")
    # Windows device names must not turn a nominal file read into a device read.
    if any(p.split(".")[0].casefold() in {"con", "prn", "aux", "nul", "conin$", "conout$"}
           or re.fullmatch(r"(?:com|lpt)[1-9¹²³]", p.split(".")[0], re.I) for p in parts):
        raise ValueError("Device paths are not GOAL files")
    return "/".join(parts)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _workspace(value: Any) -> Path:
    raw = Path(value).absolute()
    if _sensitive(raw.parts) or _is_link(raw.lstat()):
        raise ValueError("GOAL workspace must not be sensitive or a link")
    root = raw.resolve(strict=True)
    if not root.is_dir() or _sensitive(root.parts):
        raise ValueError("GOAL workspace must be a non-sensitive directory")
    return root


def _safe_path(root: Path, relative: str) -> Path:
    candidate = root / _relative_path(relative)
    _guard_path(candidate, root)
    return candidate


def _guard_path(path: Path, root: Path | None = None) -> None:
    """Reject links/reparse points in the target chain, including missing tails."""
    if _sensitive(path.parts):
        raise ValueError("GOAL cannot read configuration or credential paths")
    if root is not None and not path_within(path.resolve(), root):
        raise ValueError("GOAL path escapes workspace")
    chain = [path, *path.parents]
    for part in chain:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise ValueError("GOAL does not read links or reparse points")
        if root is not None and part == root:
            break
    if root is not None and not path_within(path.resolve(), root):
        raise ValueError("GOAL path escapes workspace")


def _read_bytes(path: Path, root: Path | None = None, *, missing: bool = False) -> bytes | None:
    """Read a regular, single-link file once, with limits and identity rechecks.

    Parent directories are checked before and after opening. O_NOFOLLOW is
    used where supported. No directory enumeration or file writes occur.
    """
    _guard_path(path, root)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError("Explicit GOAL source does not exist") from None
    if _is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("GOAL reads only regular, non-linked files")
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError("GOAL file exceeds 2 MB")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("GOAL file identity changed during capture")
        _guard_path(path, root)
        data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    _guard_path(path, root)
    final = path.lstat()
    # Windows path stat reports birth time in st_ctime, whereas descriptor
    # fstat can report last-change time. Compare ctime within each API, never
    # across the two; identity/size/mtime are portable across both APIs.
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
    if (fields(before) != fields(opened) or fields(opened) != fields(after)
            or fields(after) != fields(final) or before.st_ctime_ns != final.st_ctime_ns
            or opened.st_ctime_ns != after.st_ctime_ns):
        raise ValueError("GOAL file changed during capture")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("GOAL file exceeds 2 MB")
    return data


def _schema(value: Any, depth: int = 0, budget: list[int] | None = None) -> dict:
    budget = [MAX_SCHEMA_NODES] if budget is None else budget
    budget[0] -= 1
    if depth > MAX_SCHEMA_DEPTH or budget[0] < 0:
        raise ValueError("JSON schema exceeds bounded depth/node budget")
    item = _keys(value, {"type", "required", "properties", "items"}, "JSON schema")
    result: dict[str, Any] = {}
    if "type" in item:
        if not isinstance(item["type"], str) or item["type"] not in _TYPES:
            raise ValueError("Unsupported JSON type")
        result["type"] = item["type"]
    if "required" in item:
        required = item["required"]
        if not isinstance(required, list) or len(required) > 64:
            raise ValueError("required must be a bounded key list")
        required = [_text(k, "required key", 256) for k in required]
        if len(set(required)) != len(required):
            raise ValueError("Duplicate required key")
        result["required"] = sorted(required)
    if "properties" in item:
        props = item["properties"]
        if not isinstance(props, dict) or len(props) > 64 or any(not isinstance(k, str) for k in props):
            raise ValueError("properties must be a bounded object with string keys")
        result["properties"] = {_text(k, "property key", 256): _schema(v, depth + 1, budget)
                                for k, v in sorted(props.items())}
    if "items" in item:
        result["items"] = _schema(item["items"], depth + 1, budget)
    if ("required" in result or "properties" in result) and result.get("type", "object") != "object":
        raise ValueError("Object keys require object type")
    if "items" in result and result.get("type", "array") != "array":
        raise ValueError("items requires array type")
    if not result:
        raise ValueError("Empty JSON schema is not an acceptance condition")
    return result


def validate_criterion(raw: Any) -> dict[str, Any]:
    """Validate and canonicalize one criterion; performs no filesystem I/O."""
    if not isinstance(raw, dict):
        raise ValueError("Criterion must be an object")
    kind = raw.get("kind", "file")
    if not isinstance(kind, str) or kind not in {"file", "local_check", "json_structure", "file_count"}:
        raise ValueError("Unknown criterion kind")
    check_id = raw.get("check_id", kind if kind != "local_check" else "")
    if not isinstance(check_id, str) or check_id not in CHECK_IDS:
        raise ValueError("Unknown or missing check_id")
    if (kind == "file" and check_id != "file") or (kind in {"json_structure", "file_count"} and check_id != kind):
        raise ValueError("Criterion kind/check_id mismatch")
    item = _keys(raw, _COMMON | ({"path", "sha256", "contains"} if check_id == "file" else {"params"}), "criterion")
    result: dict[str, Any] = {"kind": "file" if check_id == "file" else "local_check", "check_id": check_id}
    for key in ("label", "stage_id"):
        if key in item:
            result[key] = _text(item[key], key, 1024 if key == "label" else 128, empty=True)
    if "source_line" in item:
        if type(item["source_line"]) is not int or not 1 <= item["source_line"] <= 2_000_001:
            raise ValueError("Invalid source_line")
        result["source_line"] = item["source_line"]
    if check_id == "file":
        result["path"] = _relative_path(item.get("path"))
        if "sha256" in item:
            expected = _text(item["sha256"], "sha256", 64, empty=True).lower()
            if expected and not _SHA.fullmatch(expected):
                raise ValueError("Invalid sha256")
            if expected:
                result["sha256"] = expected
        if "contains" in item:
            contains = _text(item["contains"], "contains", 4096, empty=True)
            if contains:
                result["contains"] = contains
    elif check_id == "json_structure":
        params = _keys(item.get("params"), {"path", "schema"}, "json_structure params")
        result["params"] = {"path": _relative_path(params.get("path")), "schema": _schema(params.get("schema"))}
    else:
        params = _keys(item.get("params"), {"paths", "count"}, "file_count params")
        paths = params.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_INPUT_FILES:
            raise ValueError("file_count requires 1..64 explicit paths")
        paths = sorted(_relative_path(p) for p in paths)
        if len({os.path.normcase(p) for p in paths}) != len(paths):
            raise ValueError("Duplicate file_count path")
        count = params.get("count")
        if type(count) is not int or not 0 <= count <= len(paths):
            raise ValueError("Invalid file_count count")
        result["params"] = {"paths": paths, "count": count}
    criterion_id = item.get("id")
    if criterion_id is None:
        criterion_id = "criterion-" + _fingerprint(result)[:20]
    if not isinstance(criterion_id, str) or not _ID.fullmatch(criterion_id):
        raise ValueError("Invalid criterion id")
    return {"id": criterion_id, **result}


def normalize_criteria(criteria: Any) -> list[dict[str, Any]]:
    """Strict, idempotent normalization; unknown conditions never disappear."""
    if not isinstance(criteria, (list, tuple)) or len(criteria) > MAX_CRITERIA:
        raise ValueError("GOAL criteria must be a list of at most 64 items")
    result = [validate_criterion(item) for item in criteria]
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("Duplicate criterion id")
    return result


def _input_paths(criteria: list[dict]) -> list[str]:
    paths: dict[str, str] = {}
    for item in criteria:
        values = ([item["path"]] if item["check_id"] == "file" else
                  item["params"]["paths"] if item["check_id"] == "file_count" else [item["params"]["path"]])
        for value in values:
            paths.setdefault(os.path.normcase(value), value)
    if len(paths) > MAX_INPUT_FILES:
        raise ValueError("GOAL input list exceeds 64 files")
    return sorted(paths.values())


def capture_inputs(workspace: Any, criteria: Any) -> dict[str, Any]:
    """Capture at most 64 files, 2 MB each / 16 MB total; no directory scan.

    Missing files are evidence, not capture errors. Unsafe paths, links,
    oversized/nonregular files and concurrent mutations fail closed.
    """
    root = _workspace(workspace)
    normalized = normalize_criteria(criteria)
    inputs = []
    total = 0
    for relative in _input_paths(normalized):
        data = _read_bytes(_safe_path(root, relative), root, missing=True)
        if data is None:
            inputs.append({"path": relative, "exists": False, "size": 0, "sha256": "", "data_b64": ""})
        else:
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("GOAL input snapshot exceeds 16 MB")
            inputs.append({"path": relative, "exists": True, "size": len(data), "sha256": _digest(data),
                           "data_b64": base64.b64encode(data).decode("ascii")})
    snapshot = {"version": GOAL_CHECK_VERSION, "workspace": str(root), "criteria": normalized, "inputs": inputs}
    return {**snapshot, "fingerprint": _fingerprint(snapshot)}


def _snapshot_data(snapshot: Any) -> tuple[list[dict], dict[str, tuple[dict, bytes | None]]]:
    item = _keys(snapshot, {"version", "workspace", "criteria", "inputs", "fingerprint"}, "check snapshot")
    if item.get("version") != GOAL_CHECK_VERSION or type(item.get("version")) is not int:
        raise ValueError("Unsupported check snapshot version")
    _text(item.get("workspace"), "snapshot workspace", 32768)
    if _fingerprint({k: v for k, v in item.items() if k != "fingerprint"}) != item.get("fingerprint"):
        raise ValueError("Check snapshot fingerprint mismatch")
    criteria = normalize_criteria(item.get("criteria"))
    if criteria != item["criteria"]:
        raise ValueError("Check snapshot criteria are not normalized")
    rows = item.get("inputs")
    if not isinstance(rows, list) or len(rows) > MAX_INPUT_FILES:
        raise ValueError("Invalid check inputs")
    inputs = {}
    total = 0
    for row in rows:
        _keys(row, {"path", "exists", "size", "sha256", "data_b64"}, "check input")
        relative = _relative_path(row.get("path"))
        if relative in inputs or type(row.get("exists")) is not bool or type(row.get("size")) is not int:
            raise ValueError("Invalid check input record")
        if not 0 <= row["size"] <= MAX_FILE_BYTES or not isinstance(row.get("data_b64"), str):
            raise ValueError("Invalid input size/bytes")
        if len(row["data_b64"]) > (MAX_FILE_BYTES + 2) // 3 * 4:
            raise ValueError("Encoded input exceeds 2 MB")
        try:
            data = base64.b64decode(row["data_b64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid snapshot base64") from None
        if len(data) != row["size"] or row.get("sha256") != (_digest(data) if row["exists"] else ""):
            raise ValueError("Input hash/size mismatch")
        if not row["exists"] and data:
            raise ValueError("Missing input has bytes")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("GOAL input snapshot exceeds 16 MB")
        inputs[relative] = (row, data if row["exists"] else None)
    if sorted(inputs) != _input_paths(criteria):
        raise ValueError("Snapshot inputs do not exactly match declared criteria")
    return criteria, inputs


def _json_bytes(data: bytes) -> Any:
    """Strict JSON with bounded nesting/nodes; rejects duplicate keys and NaN."""
    text = data.decode("utf-8-sig")
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > 32:
                raise ValueError("JSON input nesting exceeds 32")
        elif char in "]}":
            depth -= 1
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Non-finite JSON number")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    stack = [value]
    count = 0
    while stack:
        node = stack.pop()
        count += 1
        if count > 100_000:
            raise ValueError("JSON input exceeds node budget")
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError("Non-finite JSON number")
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return value


def _matches(value: Any, schema: dict, budget: list[int], cancel_event: Any) -> bool:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("JSON validation exceeds node budget")
    if cancel_event is not None and cancel_event.is_set():
        return False
    type_name = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "number": type(value) in (int, float),
               "integer": type(value) is int, "boolean": type(value) is bool, "null": value is None}
    if type_name and not matches[type_name]:
        return False
    if "required" in schema or "properties" in schema:
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            return False
        for key, child in schema.get("properties", {}).items():
            if key in value and not _matches(value[key], child, budget, cancel_event):
                return False
    if "items" in schema:
        if not isinstance(value, list):
            return False
        if any(not _matches(child, schema["items"], budget, cancel_event) for child in value):
            return False
    return True


def execute(snapshot: Any, cancel_event: Any = None) -> dict[str, Any]:
    """Run only the fixed data checks against frozen bytes, never open files.

    Returns ``passed``, ``cancelled``, per-item ``evidence`` (id, check_id,
    passed, hash, reason, inputs), ``input_fingerprint`` and evidence
    ``fingerprint``. Cancellation keeps every criterion with passed=False
    for unexecuted items. Empty criteria never count as machine completion.
    """
    criteria, inputs = _snapshot_data(snapshot)
    evidence = []
    cancelled = False
    for criterion in criteria:
        paths = _input_paths([criterion])
        records = [{k: inputs[p][0][k] for k in ("path", "exists", "size", "sha256")} for p in paths]
        row = {"id": criterion["id"], "check_id": criterion["check_id"], "passed": False,
               "hash": records[0]["sha256"] if len(records) == 1 and records[0]["exists"] else _fingerprint(records),
               "inputs": records, "reason": ""}
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            row["reason"] = "cancelled"
        else:
            try:
                check_id = criterion["check_id"]
                if check_id == "file_count":
                    count = sum(r["exists"] for r in records)
                    row["observed_count"] = count
                    row["passed"] = count == criterion["params"]["count"]
                    row["reason"] = "matched" if row["passed"] else "count_mismatch"
                else:
                    data = inputs[paths[0]][1]
                    if data is None:
                        row["reason"] = "missing"
                    elif check_id == "file":
                        passed = (not criterion.get("sha256") or criterion["sha256"] == _digest(data))
                        if criterion.get("contains"):
                            passed = criterion["contains"] in data.decode("utf-8-sig") and passed
                        row["passed"] = passed
                        row["reason"] = "matched" if passed else "condition_mismatch"
                    else:
                        row["passed"] = _matches(_json_bytes(data), criterion["params"]["schema"], [100_000], cancel_event)
                        row["reason"] = "matched" if row["passed"] else "structure_mismatch"
            except (ValueError, UnicodeError, RecursionError):
                row["passed"] = False
                row["reason"] = "invalid_input"
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                row.update(passed=False, reason="cancelled")
        evidence.append(row)
    result = {"version": GOAL_CHECK_VERSION, "input_fingerprint": snapshot["fingerprint"],
              "passed": bool(evidence) and all(row["passed"] for row in evidence) and not cancelled,
              "cancelled": cancelled, "evidence": evidence}
    return {**result, "fingerprint": _fingerprint(result)}


def verify_snapshot_current(snapshot: Any, workspace: Any = None) -> bool:
    """Re-read declared files and compare raw hashes/missing states; fail closed.

    An optional workspace must resolve to the original root. Validation or
    read errors return False; no partial/current evidence is accepted.
    """
    try:
        _snapshot_data(snapshot)
        root = _workspace(snapshot["workspace"] if workspace is None else workspace)
        if resolved_path_key(root) != resolved_path_key(snapshot["workspace"]):
            return False
        return capture_inputs(root, snapshot["criteria"])["fingerprint"] == snapshot["fingerprint"]
    except (OSError, ValueError, TypeError, RuntimeError):
        return False
