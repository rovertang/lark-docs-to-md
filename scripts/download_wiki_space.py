#!/usr/bin/env python3
"""Export a whole Feishu/Lark wiki space (knowledge base) to local Markdown.

`download_docx_tree.py` starts from one URL and can only follow links inside the
document body. A knowledge base is organised as a **wiki node tree** instead, and
that structure carries the classification the documents belong to. This script
walks that tree and mirrors it on disk.

What it reuses (there is still only one download implementation):
    * `download_tree()` for `docx` nodes, with `group_assets=True` so each
      document keeps its own `assets/<token>/` folder;
    * `parse_document_url`, `safe_filename`, `run_lark_cli`, `_decode_json`,
      `cli_error_details`, `is_permission_error`, `atomic_write_text`.

Node handling
-------------
    docx            Markdown + local images, via download_tree()
    doc (legacy)    plain text via /open-apis/doc/v2/<token>/raw_content
    sheet           one CSV per visible sub-sheet (data.annotated_csv)
    file            the attachment itself; original first, preview PDF as fallback
    other types     recorded as `unsupported` in _failures.md, never silently skipped

Output layout (mirrors the wiki hierarchy)::

    <output-dir>/<space name>/
    ├── _INDEX.md          tree with status badges and local links
    ├── _failures.md       failed / degraded / empty / unsupported / skipped
    ├── _manifest.json     machine-readable, one entry per node
    ├── _manifest.csv      UTF-8 with BOM, opens cleanly in Excel
    ├── state.json         resume checkpoints (only with --resume)
    ├── 财务管理制度/
    │   ├── _分类页.md          the container node's own content
    │   ├── 博泰逾期应收款管理制度.docx
    │   ├── 收入确认财经要素V1.0.md
    │   ├── assets/收入确认财经要素V1.0/image-001.png
    │   └── PT IT-A0.01.OPR IT运维类制度/
    └── 网络安全管理制度.md

Exit codes: 0 when no node failed, 1 when at least one node failed or --max-nodes
stopped the run early, 2 for invalid arguments.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from download_docx_tree import (
    WINDOWS_RESERVED,
    FetchError,
    _decode_json,
    atomic_write_text,
    cli_error_details,
    download_tree,
    is_permission_error,
    non_negative_int,
    parse_document_url,
    positive_float,
    run_lark_cli,
)

VERSION = "1.2.0"

CONTENT_TYPES = {"docx", "doc"}
SHEET_TYPES = {"sheet"}
FILE_TYPES = {"file"}
UNSUPPORTED_TYPES = {
    "bitable",
    "mindnote",
    "slides",
    "whiteboard",
    "board",
    "docs",
    "unknown",
}
ALL_TYPES = CONTENT_TYPES | SHEET_TYPES | FILE_TYPES | UNSUPPORTED_TYPES
PREVIEW_PRIORITY = ("source_file", "pdf", "pdf_lin", "html", "text", "image")
PREVIEW_SUFFIX = {
    "source_file": "",
    "pdf": ".pdf",
    "pdf_lin": ".pdf",
    "html": ".html",
    "text": ".txt",
    "image": ".png",
}
ATTACHMENT_MODES = ("original", "preview", "skip")
MISSING_SCOPE_HINT = (
    "当前登录缺少该接口所需 scope，重新授权即可："
    "python3 scripts/check_env.py --login --domain docs,wiki,drive,sheets"
)
RESERVED_NAMES = {"_index", "_failures", "_manifest", "state"}
# Bookkeeping written by this exporter; excluded from the reported archive totals so
# the numbers are deterministic (they do not depend on when they are measured).
BOOKKEEPING_FILES = {
    "_INDEX.md",
    "_failures.md",
    "_manifest.json",
    "_manifest.csv",
    "state.json",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_stem(title: str, *, fallback: str, max_length: int = 90) -> str:
    """Turn a node title into a single safe path component (no extension)."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    value = value[:max_length].rstrip(" .")
    return value or fallback


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def short_text(text: str, limit: int = 90) -> str:
    """Keep console lines readable; the full reason stays in the manifest."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def tree_stats(
    directory: Path, *, skip_names: Iterable[str] = ()
) -> tuple[int, int]:
    """Return (file count, total bytes) for a directory tree."""
    skipped = set(skip_names)
    files = 0
    size = 0
    if directory.is_dir():
        for path in directory.rglob("*"):
            if not path.is_file() or path.name in skipped:
                continue
            files += 1
            try:
                size += path.stat().st_size
            except OSError:
                continue
    return files, size


def archive_stats(ctx: "Context", entries: Mapping[str, dict[str, Any]]) -> dict[str, int]:
    """Separate what the nodes produced from what is actually on disk.

    A `docx` node's Markdown is tiny; its images live in `assets/<token>/` and used
    to be missing from the reported totals, which made an archive look ~29% smaller
    than it really is. This exporter's own bookkeeping files (`_INDEX.md`,
    `_manifest.*`, `state.json`) are excluded so the totals stay deterministic.
    """
    node_files = sum(1 for entry in entries.values() if entry.get("local_file"))
    node_size = sum(int(entry.get("size_bytes") or 0) for entry in entries.values())
    asset_files = sum(int(entry.get("asset_count") or 0) for entry in entries.values())
    asset_size = sum(int(entry.get("asset_size_bytes") or 0) for entry in entries.values())
    disk_files, disk_size = tree_stats(ctx.output_dir, skip_names=BOOKKEEPING_FILES)
    return {
        "disk_file_count": disk_files,
        "disk_size_bytes": disk_size,
        "node_file_count": node_files,
        "node_size_bytes": node_size,
        "asset_count": asset_files,
        "asset_size_bytes": asset_size,
    }


# --------------------------------------------------------------------------
# lark-cli plumbing (all calls go through download_docx_tree.run_lark_cli)
# --------------------------------------------------------------------------
def cli_json(
    arguments: list[str],
    *,
    ctx: "Context",
    retries: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run lark-cli and decode its JSON envelope; returns (envelope, error)."""
    attempts = ctx.retries if retries is None else retries
    last_error: str | None = None
    for attempt in range(attempts + 1):
        try:
            process = run_lark_cli(arguments, lark_cli=ctx.lark_cli, timeout=ctx.timeout)
        except FetchError as exc:
            return None, str(exc)
        if process.returncode == 0:
            envelope = _decode_json(process.stdout)
            if envelope is None:
                return None, f"无法解析 lark-cli 输出：{(process.stdout or '')[:200]!r}"
            return envelope, None
        details = cli_error_details(process)
        last_error = str(details["message"])
        if is_permission_error(details) or details["type"] in {
            "authorization",
            "validation",
            "confirmation",
        }:
            if details["subtype"] == "missing_scope":
                last_error = f"{last_error}（{MISSING_SCOPE_HINT}）"
            break
        if attempt < attempts:
            time.sleep(min(2**attempt, 8))
    return None, last_error


def _first_dict_list(
    data: Mapping[str, Any], *, require_any: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Return the first list-of-dicts value in `data` that looks like the payload."""
    wanted = tuple(require_any)
    for value in data.values():
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            if not wanted or any(key in item for item in value for key in wanted):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_csv_text(data: Mapping[str, Any]) -> str | None:
    """The CSV text lives in `data.annotated_csv`; stay tolerant about the key."""
    for key in ("annotated_csv", "csv", "content", "text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    for key, value in data.items():
        if "csv" in key.lower() and isinstance(value, str):
            return value
    return None


def _is_hidden(entry: Mapping[str, Any]) -> bool:
    for key in ("is_hidden", "hidden", "isHidden"):
        value = entry.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
            return True
    return False


# --------------------------------------------------------------------------
# Tree walking
# --------------------------------------------------------------------------
@dataclass
class WikiNode:
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    has_child: bool
    parent_node_token: str | None
    depth: int
    children: list["WikiNode"] = field(default_factory=list)
    parent: "WikiNode | None" = None
    display_name: str = ""
    child_dir: Path = field(default_factory=lambda: Path("."))
    rel_file: Path | None = None
    is_container: bool = False


class Context:
    """Everything a worker needs; also the settings echoed into the manifest."""

    def __init__(self, args: argparse.Namespace, output_dir: Path) -> None:
        self.space_id: str = args.space_id
        self.space_name: str = args.space_name or ""
        self.output_dir: Path = output_dir
        self.root_node_token: str | None = args.node_token
        self.lark_cli: str = args.lark_cli
        self.identity: str = args.identity
        self.retries: int = args.retries
        self.timeout: float = args.timeout
        self.workers: int = args.workers
        self.max_nodes: int = args.max_nodes
        self.flat: bool = args.flat
        self.resume: bool = args.resume
        self.dry_run: bool = args.dry_run
        self.attachments: str = args.attachments
        self.types: set[str] = set(args.types) if args.types else set(ALL_TYPES)
        self.doc_host: str = args.doc_host
        self._lock = threading.Lock()

    def node_url(self, node: WikiNode) -> str:
        return f"https://{self.doc_host}/wiki/{node.node_token}"

    # -- thread-safe progress printing ----------------------------------
    def log(self, message: str, *, error: bool = False) -> None:
        with self._lock:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)


def list_children(
    parent_node_token: str | None,
    *,
    ctx: Context,
) -> tuple[list[dict[str, Any]], str | None]:
    arguments = [
        "wiki",
        "+node-list",
        "--space-id",
        ctx.space_id,
        "--page-all",
        "--page-limit",
        "0",
        "--page-size",
        "50",
        "--as",
        ctx.identity,
        "--format",
        "json",
    ]
    if parent_node_token:
        arguments += ["--parent-node-token", parent_node_token]
    envelope, error = cli_json(arguments, ctx=ctx)
    if envelope is None:
        return [], error
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    return [node for node in nodes if isinstance(node, dict)], None


def fetch_node(node_token: str, *, ctx: Context) -> tuple[dict[str, Any] | None, str | None]:
    arguments = [
        "wiki",
        "+node-get",
        "--node-token",
        node_token,
        "--as",
        ctx.identity,
        "--format",
        "json",
    ]
    envelope, error = cli_json(arguments, ctx=ctx)
    if envelope is None:
        return None, error
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    if not data:
        return None, "wiki +node-get 未返回节点信息"
    return data, None


def resolve_space_name(ctx: Context) -> str:
    if ctx.space_name:
        return ctx.space_name
    envelope, error = cli_json(
        ["wiki", "+space-list", "--as", ctx.identity, "--format", "json"], ctx=ctx, retries=0
    )
    if envelope is not None:
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        for space in data.get("spaces") or []:
            if isinstance(space, dict) and str(space.get("space_id")) == str(ctx.space_id):
                name = space.get("name")
                if isinstance(name, str) and name.strip():
                    ctx.space_name = name.strip()
                    return ctx.space_name
    if error:
        ctx.log(f"[warn] 无法读取空间名称（{error}），将使用 space-<id>", error=True)
    ctx.space_name = f"space-{ctx.space_id}"
    return ctx.space_name


def walk_space(ctx: Context) -> tuple[list[WikiNode], list[str]]:
    """Walk the wiki tree; return (nodes in depth-first order, issues).

    The tree is *discovered* breadth-first (so a parent is always known before its
    children), then flattened depth-first: `_INDEX.md` should show a subtree
    contiguously instead of interleaving it with unrelated root-level nodes.
    """
    issues: list[str] = []
    nodes: list[WikiNode] = []
    seen: set[str] = set()

    if ctx.root_node_token:
        data, error = fetch_node(ctx.root_node_token, ctx=ctx)
        if data is None:
            return [], [f"无法读取起始节点 {ctx.root_node_token}：{error}"]
        roots = [data]
    else:
        roots, error = list_children(None, ctx=ctx)
        if error:
            return [], [f"无法列出空间 {ctx.space_id} 的顶层节点：{error}"]

    queue: list[tuple[dict[str, Any], WikiNode | None, int]] = [
        (raw, None, 0) for raw in roots
    ]
    root_nodes: list[WikiNode] = []
    while queue:
        raw, parent, depth = queue.pop(0)
        node_token = str(raw.get("node_token") or "")
        if not node_token or node_token in seen:
            continue
        seen.add(node_token)
        node = WikiNode(
            node_token=node_token,
            obj_token=str(raw.get("obj_token") or ""),
            obj_type=str(raw.get("obj_type") or "unknown").lower(),
            title=str(raw.get("title") or "").strip(),
            has_child=bool(raw.get("has_child")),
            parent_node_token=parent.node_token if parent else (raw.get("parent_node_token") or None),
            depth=depth,
            parent=parent,
        )
        if parent is not None:
            parent.children.append(node)
        else:
            root_nodes.append(node)
        nodes.append(node)
        if ctx.max_nodes and len(nodes) >= ctx.max_nodes:
            issues.append(f"已达到 --max-nodes {ctx.max_nodes}，其余节点未遍历")
            break
        if node.has_child:
            children, error = list_children(node_token, ctx=ctx)
            if error:
                issues.append(f"无法列出「{node.title or node_token}」的子节点：{error}")
                continue
            queue.extend((child, node, depth + 1) for child in children)

    # BFS above guarantees every parent exists before its children; the flat list is
    # then re-flattened depth-first so `_INDEX.md` shows each subtree contiguously.
    ordered: list[WikiNode] = []
    stack: list[WikiNode] = list(reversed(root_nodes))
    while stack:
        node = stack.pop()
        ordered.append(node)
        stack.extend(reversed(node.children))
    return ordered, issues


# --------------------------------------------------------------------------
# Layout: mirror the tree, with stable, collision-free names
# --------------------------------------------------------------------------
def content_extension(node: WikiNode) -> str:
    if node.obj_type in CONTENT_TYPES:
        return ".md"
    if node.obj_type in SHEET_TYPES:
        return ".csv"
    # Attachments already carry their extension inside the title, so adding
    # `Path(title).suffix` here would produce `report.pdf.pdf`.
    return ""


def assign_layout(nodes: list[WikiNode], *, ctx: Context) -> None:
    taken: dict[str, set[str]] = {Path(".").as_posix(): set(RESERVED_NAMES)}

    def unique(directory: Path, stem: str, node_token: str) -> str:
        bucket = taken.setdefault(directory.as_posix(), set())
        candidate = stem
        if candidate.casefold() in bucket:
            candidate = f"{stem}--{node_token[:10]}"
        index = 2
        while candidate.casefold() in bucket:
            candidate = f"{stem}--{node_token[:10]}-{index}"
            index += 1
        bucket.add(candidate.casefold())
        return candidate

    for node in nodes:
        fallback = f"未命名-{node.node_token[:8]}"
        stem = safe_stem(node.title, fallback=fallback)
        parent_dir = node.parent.child_dir if node.parent is not None else Path(".")
        node.is_container = bool(node.children)
        if node.is_container and not ctx.flat:
            stem = unique(parent_dir, stem, node.node_token)
            node.display_name = stem
            node.child_dir = parent_dir / stem
            if node.obj_type in CONTENT_TYPES | SHEET_TYPES:
                node.rel_file = node.child_dir / f"_分类页{content_extension(node)}"
        else:
            node.display_name = unique(parent_dir, stem, node.node_token)
            node.child_dir = parent_dir
            if node.obj_type in CONTENT_TYPES | SHEET_TYPES | FILE_TYPES:
                node.rel_file = parent_dir / f"{node.display_name}{content_extension(node)}"


def node_path_parts(node: WikiNode) -> list[str]:
    parts: list[str] = []
    current: WikiNode | None = node
    while current is not None:
        parts.append(current.display_name)
        current = current.parent
    parts.reverse()
    return parts


# --------------------------------------------------------------------------
# Per-node export
# --------------------------------------------------------------------------
def base_entry(node: WikiNode, ctx: Context) -> dict[str, Any]:
    return {
        "node_token": node.node_token,
        "obj_token": node.obj_token,
        "obj_type": node.obj_type,
        "title": node.title,
        "path": node_path_parts(node),
        "depth": node.depth,
        "is_container": node.is_container,
        "status": "ok",
        "method": "",
        "format": None,
        "local_file": None,
        "size_bytes": 0,
        "asset_count": 0,
        "asset_size_bytes": 0,
        "images": 0,
        "detail": "",
        "url": ctx.node_url(node),
    }


def _final_path(node: WikiNode, ctx: Context) -> Path | None:
    if node.rel_file is None:
        return None
    return ctx.output_dir / node.rel_file


def _merge_assets(source: Path, target: Path) -> int:
    """Merge `<staging>/assets/<token>/...` into the destination `assets/`."""
    if not source.is_dir():
        return 0
    moved = 0
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        destination = target / child.name
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        shutil.move(str(child), str(destination))
        moved += 1
    shutil.rmtree(source, ignore_errors=True)
    return moved


def export_docx(node: WikiNode, ctx: Context) -> dict[str, Any]:
    entry = base_entry(node, ctx)
    entry["method"] = "docs-fetch-markdown"
    final = _final_path(node, ctx)
    if final is None:
        entry["status"] = "unsupported"
        entry["detail"] = "节点类型不支持导出"
        return entry

    doc_url = ctx.node_url(node)
    try:
        manifest = download_tree(
            doc_url,
            ctx.output_dir,
            lark_cli=ctx.lark_cli,
            identity=ctx.identity,
            retries=ctx.retries,
            timeout=ctx.timeout,
            recursive=False,
            group_assets=True,
        )
    except (OSError, ValueError) as exc:
        entry["status"] = "failed"
        entry["detail"] = f"下载失败：{exc}"
        return entry

    staging = ctx.output_dir / node.node_token
    try:
        documents = manifest.get("documents") or []
        if not documents:
            failures = manifest.get("failures") or []
            reason = failures[0].get("error") if failures else "未获取到文档内容"
            entry["status"] = "failed"
            entry["detail"] = str(reason)
            return entry
        produced_name = str(documents[0].get("file") or "")
        produced = staging / produced_name
        if not produced.is_file():
            entry["status"] = "failed"
            entry["detail"] = f"导出文件缺失：{produced_name}"
            return entry

        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(produced, final)
        assets = _merge_assets(staging / "assets", final.parent / "assets")

        entry["local_file"] = final.relative_to(ctx.output_dir).as_posix()
        entry["format"] = "markdown"
        entry["size_bytes"] = final.stat().st_size
        entry["images"] = int(manifest.get("image_downloaded_count", 0))
        asset_files, asset_size = tree_stats(final.parent / "assets" / node.node_token)
        entry["asset_count"] = asset_files
        entry["asset_size_bytes"] = asset_size
        empty = bool(manifest.get("empty_count"))
        entry["status"] = "empty" if empty else "ok"
        details = [f"标题：{documents[0].get('title') or node.title}"]
        if assets:
            details.append(f"图片目录 {assets}")
        if entry["images"]:
            details.append(f"图片 {entry['images']}")
        if empty:
            details.append("文档正文为空")
        if manifest.get("title_fallback_count"):
            details.append("文件名取自节点元数据（正文无 <title>）")
        image_failed = int(manifest.get("image_failed_count", 0))
        if image_failed:
            details.append(f"图片失败 {image_failed}")
        entry["detail"] = "；".join(details)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return entry


def export_legacy_doc(node: WikiNode, ctx: Context) -> dict[str, Any]:
    """Legacy `doc` nodes: the v2 raw_content API returns plain text only."""
    entry = base_entry(node, ctx)
    entry["method"] = "legacy-raw-content"
    final = _final_path(node, ctx)
    arguments = [
        "api",
        "GET",
        f"/open-apis/doc/v2/{node.obj_token}/raw_content",
        "--as",
        ctx.identity,
        "--format",
        "json",
    ]
    envelope, error = cli_json(arguments, ctx=ctx)
    if envelope is None:
        entry["status"] = "failed"
        entry["detail"] = f"旧版文档正文读取失败：{error}"
        return entry
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    content = data.get("content")
    if not isinstance(content, str):
        entry["status"] = "failed"
        entry["detail"] = "raw_content 响应缺少 data.content"
        return entry
    if final is None:
        entry["status"] = "unsupported"
        entry["detail"] = "节点类型不支持导出"
        return entry
    heading = node.title or node.display_name
    body = (
        f"# {heading}\n\n"
        "> ⚠️ 本文件由飞书旧版文档纯文本接口导出（`/open-apis/doc/v2/<token>/raw_content`），"
        "仅保留纯文字，已丢失加粗、表格、图片、编号层级等全部格式。\n\n"
        f"{content.strip()}\n"
    )
    atomic_write_text(final, body)
    entry["local_file"] = final.relative_to(ctx.output_dir).as_posix()
    entry["format"] = "text"
    entry["size_bytes"] = final.stat().st_size
    entry["status"] = "empty" if not content.strip() else "ok"
    entry["detail"] = "纯文本导出，格式已丢失" + ("；正文为空" if not content.strip() else "")
    return entry


def _sheet_csv(node: WikiNode, sheet_id: str, ctx: Context) -> tuple[str | None, str | None]:
    arguments = [
        "sheets",
        "+csv-get",
        "--spreadsheet-token",
        node.obj_token,
        "--sheet-id",
        sheet_id,
        "--include-row-prefix=false",
        "--as",
        ctx.identity,
        "--format",
        "json",
    ]
    envelope, error = cli_json(arguments, ctx=ctx)
    if envelope is None:
        return None, error
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    text = _extract_csv_text(data)
    if text is None:
        return None, "CSV 响应缺少 data.annotated_csv"
    return text, None


def export_sheet(node: WikiNode, ctx: Context) -> dict[str, Any]:
    entry = base_entry(node, ctx)
    entry["method"] = "sheets-csv"
    final = _final_path(node, ctx)
    if final is None:
        entry["status"] = "unsupported"
        entry["detail"] = "节点类型不支持导出"
        return entry

    envelope, error = cli_json(
        [
            "sheets",
            "+workbook-info",
            "--spreadsheet-token",
            node.obj_token,
            "--as",
            ctx.identity,
            "--format",
            "json",
        ],
        ctx=ctx,
    )
    if envelope is None:
        entry["status"] = "failed"
        entry["detail"] = f"读取工作簿信息失败：{error}"
        return entry
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    sheets = _first_dict_list(
        data, require_any=("sheet_id", "sheetId", "sheet_name", "title")
    )
    visible = [sheet for sheet in sheets if not _is_hidden(sheet)]
    if not sheets:
        entry["status"] = "failed"
        entry["detail"] = "工作簿没有子表信息"
        return entry
    if not visible:
        entry["status"] = "failed"
        entry["detail"] = "工作簿的全部子表均被隐藏"
        return entry

    written: list[str] = []
    failures: list[str] = []
    sheet_records: list[dict[str, Any]] = []
    total_size = 0
    used_names: set[str] = set()
    for index, sheet in enumerate(visible, 1):
        sheet_id = str(sheet.get("sheet_id") or sheet.get("sheetId") or "")
        # The real `sheets +workbook-info` payload carries `sheet_name`; `title`
        # only exists in some wrappers. Reading `title` first silently produced
        # CSVs named after the unreadable sheet_id.
        sheet_title = str(
            sheet.get("sheet_name")
            or sheet.get("sheetName")
            or sheet.get("title")
            or sheet_id
            or f"sheet{index}"
        ).strip() or f"sheet{index}"
        if not sheet_id:
            failures.append(f"{sheet_title}：缺少 sheet_id")
            continue
        text, error = _sheet_csv(node, sheet_id, ctx)
        if text is None:
            failures.append(f"{sheet_title}：{error}")
            continue
        if len(visible) == 1:
            destination = final
        else:
            stem = safe_stem(sheet_title, fallback=f"sheet{index}")
            candidate_name = f"{final.stem}__{stem}.csv"
            if candidate_name.casefold() in used_names:
                candidate_name = f"{final.stem}__{stem}--{sheet_id[:10]}.csv"
            counter = 2
            while candidate_name.casefold() in used_names:
                candidate_name = f"{final.stem}__{stem}--{sheet_id[:10]}-{counter}.csv"
                counter += 1
            destination = final.parent / candidate_name
        used_names.add(destination.name.casefold())
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, text)
        relative = destination.relative_to(ctx.output_dir).as_posix()
        written.append(relative)
        total_size += destination.stat().st_size
        sheet_records.append(
            {"sheet_id": sheet_id, "sheet_name": sheet_title, "file": relative}
        )

    if not written:
        entry["status"] = "failed"
        entry["detail"] = "；".join(failures) or "没有可导出的子表"
        return entry
    entry["local_file"] = written[0]
    entry["files"] = written
    # Keep the sheet_id -> sheet_name -> file mapping in the manifest so a renamed
    # CSV can always be traced back to the sub-sheet it came from.
    entry["sheets"] = sheet_records
    entry["format"] = "csv"
    entry["size_bytes"] = total_size
    entry["status"] = "partial" if failures else "ok"
    detail = f"子表 {len(written)}/{len(visible)}"
    if written:
        detail += "：" + "、".join(str(record["sheet_name"]) for record in sheet_records)
    if failures:
        detail += "；失败：" + "；".join(failures)
    entry["detail"] = detail
    return entry


def _preview_candidates(node: WikiNode, ctx: Context) -> tuple[list[dict[str, Any]], str | None]:
    envelope, error = cli_json(
        [
            "drive",
            "+preview",
            "--file-token",
            node.obj_token,
            "--list-only",
            "--as",
            ctx.identity,
            "--format",
            "json",
        ],
        ctx=ctx,
    )
    if envelope is None:
        return [], error
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    candidates = _first_dict_list(data, require_any=("type", "preview_type", "file_type"))
    return candidates, None


def _candidate_type(candidate: Mapping[str, Any]) -> str:
    for key in ("type", "preview_type", "file_type", "name"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_preview(candidates: list[dict[str, Any]]) -> str | None:
    available = [_candidate_type(candidate) for candidate in candidates]
    for wanted in PREVIEW_PRIORITY:
        if wanted in available:
            return wanted
    return available[0] if available else None


def export_attachment(node: WikiNode, ctx: Context) -> dict[str, Any]:
    entry = base_entry(node, ctx)
    final = _final_path(node, ctx)
    if final is None:
        entry["status"] = "unsupported"
        entry["detail"] = "附件节点缺少文件名"
        return entry
    if ctx.attachments == "skip":
        entry["status"] = "skipped"
        entry["method"] = "drive-download"
        entry["detail"] = "已按 --attachments skip 跳过附件下载"
        return entry

    final.parent.mkdir(parents=True, exist_ok=True)
    original_error: str | None = None
    if ctx.attachments == "original":
        process = run_lark_cli(
            [
                "drive",
                "+download",
                "--file-token",
                node.obj_token,
                "--output",
                f"./{final.name}",
                "--overwrite",
                "--as",
                ctx.identity,
            ],
            lark_cli=ctx.lark_cli,
            timeout=ctx.timeout,
            cwd=str(final.parent),
        )
        if process.returncode == 0:
            if not final.is_file():
                # lark-cli can exit 0 without writing anything; never claim success
                # for a file that does not exist on disk.
                entry["status"] = "failed"
                entry["detail"] = "drive +download 返回成功但未生成文件"
                return entry
            entry["method"] = "drive-download"
            entry["local_file"] = final.relative_to(ctx.output_dir).as_posix()
            entry["format"] = final.suffix.lstrip(".").lower() or "binary"
            entry["size_bytes"] = final.stat().st_size
            entry["status"] = "ok"
            entry["detail"] = "已下载原件"
            return entry
        final.unlink(missing_ok=True)
        details = cli_error_details(process)
        original_error = f"{details['message']}"
        if details["subtype"] == "missing_scope":
            original_error = f"{original_error}（{MISSING_SCOPE_HINT}）"

    candidates, error = _preview_candidates(node, ctx)
    preview_type = _pick_preview(candidates)
    if preview_type is None:
        entry["status"] = "failed"
        entry["detail"] = (
            f"原件下载被拒（{original_error}），预览候选为空（{error or '无候选'}）"
            if original_error
            else f"预览候选为空（{error or '无候选'}）"
        )
        return entry

    suffix = PREVIEW_SUFFIX.get(preview_type, ".bin")
    # `source_file` means the original file is available through the preview API:
    # save it under its real name and keep the status at ok.
    if preview_type == "source_file":
        preview_name = final.name
    else:
        preview_name = f"{Path(final.name).stem}__preview{suffix}"
    process = run_lark_cli(
        [
            "drive",
            "+preview",
            "--file-token",
            node.obj_token,
            "--type",
            preview_type,
            "--output",
            f"./{preview_name}",
            "--if-exists",
            "overwrite",
            "--as",
            ctx.identity,
        ],
        lark_cli=ctx.lark_cli,
        timeout=ctx.timeout,
        cwd=str(final.parent),
    )
    if process.returncode != 0:
        details = cli_error_details(process)
        entry["status"] = "failed"
        entry["detail"] = f"预览件下载失败：{details['message']}"
        return entry
    preview_path = final.parent / preview_name
    if not preview_path.is_file():
        entry["status"] = "failed"
        entry["detail"] = f"预览接口返回成功但未生成文件（{preview_type}）"
        return entry
    entry["local_file"] = preview_path.relative_to(ctx.output_dir).as_posix()
    entry["format"] = preview_type
    entry["size_bytes"] = preview_path.stat().st_size
    if preview_type == "source_file":
        entry["method"] = "preview-source_file"
        entry["status"] = "ok"
        entry["detail"] = "原件下载被拒，已通过 source_file 预览接口取回原件"
        return entry
    entry["method"] = f"preview-{preview_type}"
    entry["status"] = "partial"
    entry["fallback"] = f"preview-{preview_type}"
    entry["preview_type"] = preview_type
    entry["detail"] = (
        f"原件无下载权限，已保存 {preview_type} 预览件"
        + (f"（原件错误：{original_error}）" if original_error else "")
    )
    return entry


def process_node(node: WikiNode, ctx: Context) -> dict[str, Any]:
    if node.obj_type not in ctx.types:
        entry = base_entry(node, ctx)
        entry["status"] = "skipped"
        entry["method"] = "type-filter"
        entry["detail"] = f"类型 {node.obj_type} 已被 --types 排除"
        return entry
    try:
        if node.obj_type in CONTENT_TYPES and node.obj_type == "docx":
            return export_docx(node, ctx)
        if node.obj_type == "doc":
            return export_legacy_doc(node, ctx)
        if node.obj_type in SHEET_TYPES:
            return export_sheet(node, ctx)
        if node.obj_type in FILE_TYPES:
            return export_attachment(node, ctx)
    except (OSError, ValueError, FetchError) as exc:  # pragma: no cover - defensive
        entry = base_entry(node, ctx)
        entry["status"] = "failed"
        entry["detail"] = f"{type(exc).__name__}: {exc}"
        return entry

    entry = base_entry(node, ctx)
    entry["status"] = "unsupported"
    entry["method"] = "none"
    entry["detail"] = (
        f"节点类型 {node.obj_type} 暂不支持导出"
        + ("；已镜像其子节点" if node.children else "")
    )
    return entry


# --------------------------------------------------------------------------
# state.json (resume) and artifacts
# --------------------------------------------------------------------------
class StateStore:
    """Checkpoint store so a long export can be resumed after a failure."""

    def __init__(self, path: Path, *, enabled: bool, signature: dict[str, Any]) -> None:
        self.path = path
        self.enabled = enabled
        self.signature = signature
        self._lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.completed: set[str] = set()
        self.loaded = False
        if enabled and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if data.get("signature") == signature:
                self.loaded = True
                self.entries = {
                    str(key): value
                    for key, value in (data.get("nodes") or {}).items()
                    if isinstance(value, dict)
                }
                self.completed = {
                    key
                    for key, value in self.entries.items()
                    if value.get("status") in {"ok", "empty", "unsupported", "skipped"}
                }

    def record(self, entry: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.entries[entry["node_token"]] = entry
            self._write()

    def _write(self) -> None:
        payload = {
            "version": 1,
            "signature": self.signature,
            "generated_at": utc_now(),
            "nodes": self.entries,
        }
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


STATUS_BADGE = {
    "ok": "✅",
    "partial": "⚠️",
    "empty": "⬜",
    "failed": "❌",
    "unsupported": "⛔",
    "skipped": "⏭️",
}
STATUS_LABEL = {
    "ok": "成功",
    "partial": "降级（预览件/部分子表）",
    "empty": "空文档",
    "failed": "失败",
    "unsupported": "不支持的类型",
    "skipped": "已跳过",
}


def _link(label: str, target: str) -> str:
    """CommonMark link with angle brackets so spaces and `)` cannot break it."""
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_label}](<{target}>)"


def summarize(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {status: 0 for status in STATUS_BADGE}
    for entry in entries:
        status = str(entry.get("status") or "failed")
        counts[status] = counts.get(status, 0) + 1
    return counts


def write_index(ctx: Context, nodes: list[WikiNode], entries: dict[str, dict[str, Any]]) -> None:
    counts = summarize(list(entries.values()))
    stats = archive_stats(ctx, entries)
    lines = [
        f"# {ctx.space_name}",
        "",
        f"- 空间 ID：`{ctx.space_id}`",
        f"- 导出时间：{utc_now()}",
        f"- 节点数：{len(nodes)}（"
        + " / ".join(f"{STATUS_LABEL[key]} {counts.get(key, 0)}" for key in STATUS_BADGE)
        + "）",
        f"- 本地文件：{stats['disk_file_count']} 个"
        f"（节点产物 {stats['node_file_count']} + 图片等资源 {stats['asset_count']} + 导出清单），"
        f"共 {human_size(stats['disk_size_bytes'])}",
        f"- 节点产物体积：{human_size(stats['node_size_bytes'])}"
        f"；图片等资源体积：{human_size(stats['asset_size_bytes'])}",
        f"- 输出目录：`{ctx.output_dir}`",
        "",
        "图例：" + " ".join(f"{badge} {STATUS_LABEL[key]}" for key, badge in STATUS_BADGE.items()),
        "",
        "## 目录结构",
        "",
    ]
    # pre-order by construction of `nodes`, indentation follows depth
    for node in nodes:
        entry = entries.get(node.node_token)
        if entry is None:
            continue
        status = str(entry.get("status"))
        badge = STATUS_BADGE.get(status, "❔")
        indent = "  " * node.depth
        label = str(entry.get("title") or node.display_name or node.node_token)
        local = entry.get("local_file")
        if local:
            lines.append(f"{indent}- {badge} {_link(label, str(local))}")
        else:
            lines.append(f"{indent}- {badge} {label}")
        detail = str(entry.get("detail") or "")
        if status in {"failed", "unsupported"}:
            lines.append(f"{indent}  - {detail} · {_link('飞书原文', str(entry.get('url')))}")
        elif status in {"partial", "empty"}:
            lines.append(f"{indent}  - {detail}")
    atomic_write_text(ctx.output_dir / "_INDEX.md", "\n".join(lines) + "\n")


def write_failures(ctx: Context, nodes: list[WikiNode], entries: dict[str, dict[str, Any]]) -> None:
    order = ["failed", "partial", "empty", "unsupported", "skipped"]
    lines = ["# 未完整归档清单", ""]
    counts = summarize(list(entries.values()))
    lines.append(
        "统计：" + " / ".join(f"{STATUS_LABEL[key]} {counts.get(key, 0)}" for key in order)
    )
    lines.append("")
    for status in order:
        group = [node for node in nodes if entries.get(node.node_token, {}).get("status") == status]
        if status == "ok" or not group:
            continue
        lines.append(f"## {STATUS_LABEL[status]}（{len(group)}）")
        lines.append("")
        for node in group:
            entry = entries[node.node_token]
            title = str(entry.get("title") or node.display_name or node.node_token)
            detail = str(entry.get("detail") or "")
            line = f"- {_link(title, str(entry.get('url')))}"
            if detail:
                line += f" — {detail}"
            lines.append(line)
            local = entry.get("local_file")
            if local:
                lines.append(f"  - 本地：`{local}`")
        lines.append("")
    atomic_write_text(ctx.output_dir / "_failures.md", "\n".join(lines).rstrip() + "\n")


def write_manifest(
    ctx: Context,
    nodes: list[WikiNode],
    entries: dict[str, dict[str, Any]],
    *,
    issues: list[str],
    limited: bool,
) -> dict[str, Any]:
    ordered = [entries[node.node_token] for node in nodes if node.node_token in entries]
    counts = summarize(ordered)
    stats = archive_stats(ctx, entries)
    manifest = {
        "version": VERSION,
        "space_id": ctx.space_id,
        "space_name": ctx.space_name,
        "root_node_token": ctx.root_node_token,
        "output_dir": str(ctx.output_dir),
        "generated_at": utc_now(),
        "flat": ctx.flat,
        "attachments": ctx.attachments,
        "identity": ctx.identity,
        "complete": counts.get("failed", 0) == 0 and not limited,
        "total_nodes": len(nodes),
        # Disk truth first: `file_count`/`total_size_bytes` count everything that is
        # actually on disk (images included), while the node/asset split below keeps
        # the breakdown visible.
        "file_count": stats["disk_file_count"],
        "total_size_bytes": stats["disk_size_bytes"],
        "disk_file_count": stats["disk_file_count"],
        "disk_size_bytes": stats["disk_size_bytes"],
        "node_file_count": stats["node_file_count"],
        "node_size_bytes": stats["node_size_bytes"],
        "asset_count": stats["asset_count"],
        "asset_size_bytes": stats["asset_size_bytes"],
        "counts": counts,
        "limited": limited,
        "issues": issues,
        "nodes": ordered,
    }
    atomic_write_text(
        ctx.output_dir / "_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "path",
            "title",
            "obj_type",
            "status",
            "method",
            "format",
            "local_file",
            "size_bytes",
            "asset_count",
            "asset_size_bytes",
            "images",
            "sheets",
            "detail",
            "url",
            "node_token",
            "obj_token",
        ]
    )
    for entry in ordered:
        sheet_names = "、".join(
            str(record.get("sheet_name") or "")
            for record in (entry.get("sheets") or [])
            if isinstance(record, dict)
        )
        writer.writerow(
            [
                " / ".join(entry.get("path") or []),
                entry.get("title") or "",
                entry.get("obj_type") or "",
                entry.get("status") or "",
                entry.get("method") or "",
                entry.get("format") or "",
                entry.get("local_file") or "",
                entry.get("size_bytes") or 0,
                entry.get("asset_count") or 0,
                entry.get("asset_size_bytes") or 0,
                entry.get("images") or 0,
                sheet_names,
                entry.get("detail") or "",
                entry.get("url") or "",
                entry.get("node_token") or "",
                entry.get("obj_token") or "",
            ]
        )
    # UTF-8 with BOM: Excel only detects UTF-8 in CSV when the BOM is present
    (ctx.output_dir / "_manifest.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    return manifest


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a Feishu/Lark wiki space (knowledge base) to local Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 scripts/download_wiki_space.py --space-id 7007715075855450113 -o ./downloads\n"
            "  python3 scripts/download_wiki_space.py --space-id <id> --node-token <wikcn...> -o ./downloads\n"
            "  python3 scripts/download_wiki_space.py --space-id <id> --resume --workers 4 -o ./downloads\n"
            "  python3 scripts/download_wiki_space.py --space-id <id> --dry-run --max-nodes 20\n"
            "\n"
            "exit codes: 0 complete, 1 some nodes failed, 2 invalid arguments\n"
        ),
    )
    parser.add_argument("--space-id", required=True, help="wiki space id (see `lark-cli wiki +space-list`)")
    parser.add_argument(
        "--node-token",
        help="export only this subtree instead of the whole space (a wikcn... node token)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="parent directory; a <space name> subdirectory is created inside it",
    )
    parser.add_argument(
        "--space-name",
        help="override the detected space name (used for the output directory)",
    )
    parser.add_argument(
        "--types",
        help=f"comma-separated node types to export; default all ({','.join(sorted(ALL_TYPES))})",
    )
    parser.add_argument(
        "--attachments",
        choices=ATTACHMENT_MODES,
        default="original",
        help="file nodes: original = download, fall back to a preview; preview = only preview; skip = do not download",
    )
    parser.add_argument("--workers", type=non_negative_int, default=4, help="parallel node workers; default 4")
    parser.add_argument(
        "--max-nodes", type=non_negative_int, default=0, help="safety cap on walked nodes; 0 = unlimited"
    )
    parser.add_argument("--resume", action="store_true", help="reuse state.json and skip finished nodes")
    parser.add_argument("--flat", action="store_true", help="do not mirror the hierarchy; one flat directory")
    parser.add_argument("--dry-run", action="store_true", help="walk and print the plan without downloading")
    parser.add_argument("-i", "--identity", choices=("user", "bot"), default="user")
    parser.add_argument("--retries", type=non_negative_int, default=2)
    parser.add_argument("--timeout", type=positive_float, default=120)
    parser.add_argument(
        "--doc-host",
        default="feishu.cn",
        help="host used to build wiki URLs for the underlying CLI calls; default feishu.cn",
    )
    parser.add_argument("--lark-cli", default=os.environ.get("LARK_CLI", "lark-cli"))
    args = parser.parse_args(argv)

    if args.types:
        wanted = {item.strip().lower() for item in args.types.split(",") if item.strip()}
        unknown = wanted - ALL_TYPES
        if unknown:
            parser.error(f"unknown --types value(s): {', '.join(sorted(unknown))}")
        args.types = sorted(wanted)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def run(args: argparse.Namespace) -> int:
    output_parent = args.output_dir.expanduser().resolve()
    ctx = Context(args, output_parent)

    name = resolve_space_name(ctx)
    space_dir = output_parent / safe_stem(name, fallback=f"space-{ctx.space_id}")
    ctx.output_dir = space_dir

    nodes, issues = walk_space(ctx)
    for issue in issues:
        ctx.log(f"[warn] {issue}", error=True)
    if not nodes:
        ctx.log("error: 没有遍历到任何节点，请检查 --space-id、权限与登录状态", error=True)
        return 2

    assign_layout(nodes, ctx=ctx)
    ctx.log(
        f"space: {name} ({ctx.space_id}) nodes={len(nodes)} output={space_dir}"
        + (" [dry-run]" if ctx.dry_run else "")
    )

    if ctx.dry_run:
        for node in nodes[:200]:
            target = node.rel_file.as_posix() if node.rel_file else (node.child_dir.as_posix() + "/")
            ctx.log(f"[plan] {node.obj_type:<9} {node.title or '(无标题)':<40} -> {target}")
        if len(nodes) > 200:
            ctx.log(f"[plan] ... 其余 {len(nodes) - 200} 个节点未显示")
        return 0

    space_dir.mkdir(parents=True, exist_ok=True)
    signature = {
        "space_id": ctx.space_id,
        "root_node_token": ctx.root_node_token,
        "flat": ctx.flat,
        "attachments": ctx.attachments,
        "types": sorted(ctx.types),
    }
    state = StateStore(space_dir / "state.json", enabled=ctx.resume, signature=signature)
    if ctx.resume:
        if state.loaded:
            ctx.log(f"[resume] 跳过 {len(state.completed)} 个已完成节点")
        else:
            ctx.log(
                "[resume] 未找到可用的 state.json（不存在或与本轮参数不匹配）："
                "本次不会跳过任何节点。检查点只有在带 --resume 运行时才会写入，"
                "所以失败重跑请从一开始就加上 --resume。"
            )
    elif (space_dir / "state.json").is_file():
        ctx.log("[hint] 检测到 state.json 但未加 --resume，本次将重新处理所有节点")

    pending = [node for node in nodes if node.node_token not in state.completed]
    entries: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if node.node_token in state.completed:
            entries[node.node_token] = state.entries[node.node_token]

    counter = {"done": 0}
    counter_lock = threading.Lock()
    total = len(pending)

    def work(node: WikiNode) -> dict[str, Any]:
        entry = process_node(node, ctx)
        state.record(entry)
        with counter_lock:
            counter["done"] += 1
            index = counter["done"]
        badge = STATUS_BADGE.get(str(entry.get("status")), "?")
        target = entry.get("local_file") or " / ".join(entry.get("path") or [])
        status = str(entry.get("status"))
        suffix = ""
        if status in {"failed", "unsupported", "partial"}:
            suffix = f"  ← {short_text(str(entry.get('detail') or ''))}"
        ctx.log(f"[{index:>4}/{total}] {badge} {node.obj_type:<9} {target}{suffix}")
        if status in {"failed", "unsupported"}:
            ctx.log(f"        {entry.get('url')}", error=True)
        return entry

    if pending:
        workers = max(1, min(ctx.workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for entry in executor.map(work, pending):
                entries[entry["node_token"]] = entry

    limited = bool(ctx.max_nodes and len(nodes) >= ctx.max_nodes)
    write_index(ctx, nodes, entries)
    write_failures(ctx, nodes, entries)
    manifest = write_manifest(ctx, nodes, entries, issues=issues, limited=limited)

    counts = manifest["counts"]
    ctx.log(
        "summary: "
        f"space={name} space_id={ctx.space_id} output={space_dir} "
        f"nodes={manifest['total_nodes']} "
        + " ".join(f"{key}={counts.get(key, 0)}" for key in STATUS_BADGE)
        + f" files={manifest['disk_file_count']}"
        f" (nodes={manifest['node_file_count']} assets={manifest['asset_count']})"
        f" size={human_size(manifest['disk_size_bytes'])}"
        f" (nodes={human_size(manifest['node_size_bytes'])}"
        f" assets={human_size(manifest['asset_size_bytes'])})"
        f" complete={manifest['complete']} resume={ctx.resume}"
    )
    return 0 if manifest["complete"] else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
