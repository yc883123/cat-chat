"""Deterministic, read-only GOAL plan preview, with no App/job dependencies.

API::

    preview(message, sources, workspace, attachments=()) -> dict
    current_sources(plan_or_sources, workspace, attachments=()) -> list[dict]
    verify_sources_current(plan_or_sources, workspace, attachments=()) -> bool

``sources`` is a list (at most 8) of explicit paths or ``{path, name?}``.
Workspace-relative/absolute in-workspace paths are allowed. An outside file
must match an exact path in this request's ``attachments``; attachments are
never scanned or implicitly read. Each source is a regular, non-linked UTF-8
file <= 2 MB. No inferred files, includes, globbing or commands are followed.

Standalone ``验收文件：relative/path`` lines (optionally a Markdown bullet or
checkbox) produce existence checks. ``goal-criteria`` fences contain a JSON
criterion list or ``{version?: 1, criteria: [...]}``, validated by
``goal_checks.normalize_criteria``. Other prose/checklists/code is retained
as manual obligations, including checked boxes: a plan cannot attest its own
completion. Headings become stages. Unsupported structured checks fail
closed instead of being silently dropped. All extraction has explicit limits.

The result contains version/message/sources/criteria/stages/manual_items/
completion_policy/fingerprint. Sources additionally carry original ``text``
and its raw-byte ``sha256`` (UTF-8 BOM and CRLF preserved), parser_version,
and their extracted items. Fingerprints are deterministic, not permissions.
Before commit use ``verify_sources_current``. It also accepts a persisted
``{sources: [...]}`` without the rest of the preview. ``current_sources``
re-reads and re-parses exactly those same paths and returns their current
records; neither function writes or updates the supplied preview.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from naiba.core.contracts import GOAL_PLAN_VERSION
from naiba.core.paths import path_within, resolved_path_key
from naiba.run.goal_checks import (
    MAX_FILE_BYTES, _digest, _fingerprint, _guard_path, _input_paths, _json_bytes,
    _read_bytes, _relative_path, _safe_path, _text, _workspace, normalize_criteria,
)

MAX_SOURCES = 8
MAX_MANUAL_ITEMS = 512
MAX_STAGES = 128
_FILE = re.compile(r"验收文件[：:]\s*(.*?)\s*\Z")
_PREFIX = re.compile(r"^(?:[-+*]|\d+[.)])\s+(?:\[([ xX])\]\s*)?")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")


def _source_specs(sources: Any) -> list[dict[str, str]]:
    if not isinstance(sources, (list, tuple)) or len(sources) > MAX_SOURCES:
        raise ValueError("GOAL allows at most 8 explicit plan sources")
    result = []
    for source in sources:
        if isinstance(source, (str, os.PathLike)):
            source = {"path": os.fspath(source)}
        if not isinstance(source, dict) or not set(source) <= {"path", "name"}:
            raise ValueError("Plan source must specify only path and optional name")
        path = _text(source.get("path"), "source path", 32768)
        name = _text(source.get("name", Path(path).name), "source name", 512)
        result.append({"path": path, "name": name})
    return result


def _source_path(value: str, root: Path, attachments: Any) -> Path:
    if not isinstance(attachments, (list, tuple)):
        raise ValueError("attachments must be a list")
    path = Path(value)
    if not path.is_absolute():
        path = root / _relative_path(value)
    # Keep lexical components until after link checks; resolve must not hide
    # a link, sensitive spelling, '..' traversal or an alternate data stream.
    if ".." in path.parts or any(":" in part for part in path.parts[1:]):
        raise ValueError("Invalid explicit source path")
    _guard_path(path)
    # Validate the basename even for absolute attachment paths (ADS, device
    # names, trailing-dot aliases and control characters are never files).
    _relative_path(path.name)
    path = path.resolve()
    if not path_within(path, root):
        permitted = set()
        for item in attachments:
            raw = item.get("path") if isinstance(item, dict) else item
            if isinstance(raw, (str, os.PathLike)) and Path(raw).is_absolute():
                # Authorization is exact, not a parent directory grant.
                permitted.add(resolved_path_key(raw))
        if resolved_path_key(path) not in permitted:
            raise ValueError("Outside plan source must be an explicit current attachment")
    return path


def _manual(text: str, source: str, line: int, stage: str, *, checked: bool = False) -> dict:
    return {"id": "manual-" + _fingerprint([source, line, text])[:20], "text": text,
            "source": source, "source_line": line, "stage_id": stage, "checked_in_plan": checked}


def _parse(text: str, origin: str) -> dict[str, list[dict]]:
    criteria: list[dict] = []
    stages: list[dict] = []
    manual: list[dict] = []
    stage = ""
    lines = text.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = index + 1
        index += 1
        if not raw.strip():
            continue
        if raw.startswith("    ") or raw.startswith("\t"):
            manual.append(_manual(raw, origin, line, stage))
            if len(manual) > MAX_MANUAL_ITEMS:
                raise ValueError("Plan extraction exceeds item limits")
            continue
        fence = _FENCE.fullmatch(raw)
        if fence:
            marker, info = fence.groups()
            body = []
            closed = False
            while index < len(lines):
                candidate = lines[index]
                index += 1
                if re.fullmatch(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", candidate):
                    closed = True
                    break
                body.append(candidate)
            if info.strip() == "goal-criteria":
                if not closed:
                    raise ValueError("Unclosed goal-criteria fence")
                try:
                    payload = _json_bytes("\n".join(body).encode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError("Invalid goal-criteria JSON") from None
                if isinstance(payload, dict):
                    if (set(payload) - {"version", "criteria"} or "criteria" not in payload
                            or type(payload.get("version", GOAL_PLAN_VERSION)) is not int
                            or payload.get("version", GOAL_PLAN_VERSION) != GOAL_PLAN_VERSION):
                        raise ValueError("Unsupported goal-criteria envelope")
                    payload = payload["criteria"]
                if not isinstance(payload, list):
                    raise ValueError("goal-criteria must contain a criterion list")
                for offset, item in enumerate(payload):
                    if not isinstance(item, dict):
                        raise ValueError("goal-criteria entries must be objects")
                    item = dict(item)
                    item.setdefault("source_line", line)
                    item.setdefault("stage_id", stage)
                    item.setdefault("id", "criterion-" + _fingerprint([origin, line, offset])[:20])
                    criteria.extend(normalize_criteria([item]))
            else:
                block = "\n".join([raw, *body, marker] if closed else [raw, *body])
                manual.append(_manual(block, origin, line, stage))
            if len(criteria) > 64 or len(manual) > MAX_MANUAL_ITEMS:
                raise ValueError("Plan extraction exceeds item limits")
            continue
        heading = _HEADING.fullmatch(raw)
        if heading:
            stage = "stage-" + _fingerprint([origin, line, heading[2]])[:20]
            stages.append({"id": stage, "title": heading[2], "level": len(heading[1]),
                           "source": origin, "source_line": line})
            if len(stages) > MAX_STAGES:
                raise ValueError("Plan has too many stages")
            continue
        stripped = raw.strip()
        prefix = _PREFIX.match(stripped)
        content = stripped[prefix.end():] if prefix else stripped
        match = _FILE.fullmatch(content)
        if match:
            path = match[1]
            if len(path) >= 2 and path[0] in '`"' and path[-1] == path[0]:
                path = path[1:-1]
            item = {"id": "criterion-" + _fingerprint([origin, line, path])[:20],
                    "kind": "file", "path": path, "source_line": line, "stage_id": stage}
            criteria.extend(normalize_criteria([item]))
        else:
            manual.append(_manual(content, origin, line, stage,
                                  checked=bool(prefix and prefix[1] and prefix[1].lower() == "x")))
        if len(criteria) > 64 or len(manual) > MAX_MANUAL_ITEMS:
            raise ValueError("Plan extraction exceeds item limits")
    return {"criteria": normalize_criteria(criteria), "stages": stages, "manual_items": manual}


def _capture(sources: Any, root: Path, attachments: Any) -> list[dict]:
    result = []
    seen = set()
    for spec in _source_specs(sources):
        path = _source_path(spec["path"], root, attachments)
        key = resolved_path_key(path)
        if key in seen:
            raise ValueError("Duplicate explicit plan source")
        seen.add(key)
        data = _read_bytes(path, root if path_within(path, root) else None)
        assert data is not None
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ValueError("Plan sources must be UTF-8 text") from None
        if "\x00" in text:
            raise ValueError("Plan source is not text")
        origin = str(path)
        result.append({"path": origin, "name": spec["name"], "sha256": _digest(data),
                       "parser_version": GOAL_PLAN_VERSION, "text": text, **_parse(text, origin)})
    return result


def preview(message: str, sources: Any, workspace: Any, attachments: Any = ()) -> dict[str, Any]:
    """Freeze explicit source bytes and extract a conservative completion policy.

    No automatic source discovery is performed, even if message mentions a
    path. Callers own source selection, permission checks and persistence.
    machine_all requires >=1 machine criterion and no manual obligation;
    subjective text always prevents automatic machine-only completion.
    """
    _text(message, "GOAL message", MAX_FILE_BYTES, empty=True)
    if len(message.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("GOAL message exceeds 2 MB")
    root = _workspace(workspace)
    source_rows = _capture(sources, root, attachments)
    parsed = _parse(message, "message")
    for source in source_rows:
        for key in ("criteria", "stages", "manual_items"):
            parsed[key].extend(source[key])
    parsed["criteria"] = normalize_criteria(parsed["criteria"])
    # Check existing path components for every declared machine input without
    # opening artifacts. A criterion cannot quietly bless an existing link.
    for relative in _input_paths(parsed["criteria"]):
        _safe_path(root, relative)
    if len(parsed["manual_items"]) > MAX_MANUAL_ITEMS or len(parsed["stages"]) > MAX_STAGES:
        raise ValueError("Combined plan exceeds extraction limits")
    policy = "mixed" if parsed["criteria"] and parsed["manual_items"] else "machine_all" if parsed["criteria"] else "manual"
    result = {"version": GOAL_PLAN_VERSION, "message": message, "workspace": str(root),
              "sources": source_rows, **parsed, "completion_policy": policy}
    return {**result, "fingerprint": _fingerprint(result)}


def _frozen_sources(plan_or_sources: Any) -> list[dict]:
    rows = plan_or_sources.get("sources") if isinstance(plan_or_sources, dict) else plan_or_sources
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_SOURCES:
        raise ValueError("Invalid frozen GOAL sources")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
            raise ValueError("Invalid frozen source hash")
        if row.get("parser_version") != GOAL_PLAN_VERSION or type(row.get("parser_version")) is not int:
            raise ValueError("Unsupported source parser version")
        if "text" in row and (not isinstance(row["text"], str)
                              or _digest(row["text"].encode("utf-8")) != row["sha256"]):
            raise ValueError("Frozen source text/hash mismatch")
    return list(rows)


def current_sources(plan_or_sources: Any, workspace: Any, attachments: Any = ()) -> list[dict]:
    """Read and parse only original source paths; errors are explicit ValueError/OSError.

    A full preview's fingerprint is checked if supplied; persisted
    ``{sources: [...]}`` records can also be used without a preview envelope.
    """
    rows = _frozen_sources(plan_or_sources)
    if isinstance(plan_or_sources, dict) and "fingerprint" in plan_or_sources:
        if _fingerprint({k: v for k, v in plan_or_sources.items() if k != "fingerprint"}) != plan_or_sources["fingerprint"]:
            raise ValueError("Plan fingerprint mismatch")
    root = _workspace(workspace)
    if isinstance(plan_or_sources, dict) and "workspace" in plan_or_sources:
        if resolved_path_key(plan_or_sources["workspace"]) != resolved_path_key(root):
            raise ValueError("Plan workspace changed")
    return _capture([{"path": row.get("path"), "name": row.get("name")} for row in rows], root, attachments)


def verify_sources_current(plan_or_sources: Any, workspace: Any, attachments: Any = ()) -> bool:
    """Return False on changed bytes, vanished/unsafe sources or invalid snapshot."""
    try:
        frozen = _frozen_sources(plan_or_sources)
        current = current_sources(plan_or_sources, workspace, attachments)
        return all(old["sha256"] == new["sha256"] and resolved_path_key(old["path"]) == resolved_path_key(new["path"])
                   for old, new in zip(frozen, current)) and len(frozen) == len(current)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False
