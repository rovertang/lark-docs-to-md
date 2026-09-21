#!/usr/bin/env python3
"""Single-file web service for downloading Feishu/Lark documents as Markdown.

Start it and open the printed URL in a browser:

    python web/lark_download_web.py
    python web/lark_download_web.py --port 9000 --open
    python web/lark_download_web.py --output-dir ./downloads

The server is intentionally implemented with the Python standard library only
(no pip install, no Node build step).  It reuses the command-line scripts in
``scripts/`` as the single source of truth:

* every download is executed through ``scripts/download_docx_tree.py``;
* URL parsing, filename sanitising and manifest reading are imported from
  ``scripts/download_docx_tree.py`` instead of being re-implemented;
* the environment/login panel reuses ``scripts/check_env.py``.

Endpoints
---------
    GET  /                              web UI
    GET  /api/config                    server defaults
    GET  /api/env                       environment + login status
    POST /api/login                     start device-flow login
    POST /api/login/complete            finish device-flow login
    POST /api/jobs                      create a download job
    GET  /api/jobs                      list jobs
    GET  /api/jobs/<id>?log_offset=N     job state and new log lines
    POST /api/jobs/<id>/cancel          cancel a running job
    GET  /api/jobs/<id>/files           generated files
    GET  /api/jobs/<id>/file?path=...   preview one generated file
    GET  /api/jobs/<id>/archive         download all job output as a zip

Safety: the server can execute ``lark-cli`` with the local user's credentials,
so it binds to ``127.0.0.1`` by default and refuses a non-loopback ``--host``
unless ``--allow-remote`` is given explicitly.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEFAULT_DOWNLOAD_SCRIPT = SCRIPTS_DIR / "download_docx_tree.py"
SPACE_SCRIPT = SCRIPTS_DIR / "download_wiki_space.py"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "downloads"
MAX_LOG_LINES = 20000
TERMINATE_GRACE_SECONDS = 5.0


# --------------------------------------------------------------------------
# Reuse the command-line scripts instead of duplicating their logic
# --------------------------------------------------------------------------
def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


download_docx_tree = _load_module("download_docx_tree", SCRIPTS_DIR / "download_docx_tree.py")
check_env = _load_module("check_env", SCRIPTS_DIR / "check_env.py")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Job model
# --------------------------------------------------------------------------
@dataclass
class JobItem:
    url: str
    kind: str = ""
    token: str = ""
    status: str = "pending"  # pending running ok partial failed invalid cancelled
    exit_code: int | None = None
    output_dir: str | None = None
    downloaded: int = 0
    failed: int = 0
    images: int = 0
    image_failed: int = 0
    empty: int = 0
    fallbacks: int = 0
    titles: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["failures"] = self.failures[:20]
        return data


@dataclass
class Job:
    id: str
    created_at: str
    options: dict[str, Any]
    output_root: str
    items: list[JobItem]
    status: str = "queued"  # queued running done cancelled failed
    log: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    process: subprocess.Popen[str] | None = None
    finished_at: str | None = None
    error: str | None = None

    def append_log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]

    def to_json(self, log_offset: int = 0) -> dict[str, Any]:
        offset = max(0, min(log_offset, len(self.log)))
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "id": self.id,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error": self.error,
            "options": self.options,
            "output_root": self.output_root,
            "items": [item.to_json() for item in self.items],
            "counts": counts,
            "log_offset": len(self.log),
            "log": self.log[offset:],
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
JOB_COUNTER = 0


def _new_job_id() -> str:
    global JOB_COUNTER
    with JOBS_LOCK:
        JOB_COUNTER += 1
        counter = JOB_COUNTER
    return f"{datetime.now().strftime('%H%M%S')}-{counter:03d}"


# --------------------------------------------------------------------------
# Job execution
# --------------------------------------------------------------------------
def build_command(
    url: str,
    output_root: Path,
    options: dict[str, Any],
    lark_cli: str,
) -> list[str]:
    command = [
        sys.executable,
        str(DEFAULT_DOWNLOAD_SCRIPT),
        url,
        "-o",
        str(output_root),
        "-i",
        str(options.get("identity", "user")),
        "--retries",
        str(options.get("retries", 2)),
        "--timeout",
        str(options.get("timeout", 120)),
        "--lark-cli",
        lark_cli,
    ]
    if options.get("recursive"):
        command.append("--recursive")
        max_docs = int(options.get("max_docs") or 0)
        if max_docs > 0:
            command.extend(["--max-docs", str(max_docs)])
    return command


def build_space_command(
    output_root: Path,
    options: dict[str, Any],
    lark_cli: str,
) -> list[str]:
    """Whole knowledge-base export, driven by scripts/download_wiki_space.py."""
    command = [
        sys.executable,
        str(SPACE_SCRIPT),
        "--space-id",
        str(options.get("space_id", "")),
        "-o",
        str(output_root),
        "-i",
        str(options.get("identity", "user")),
        "--attachments",
        str(options.get("attachments", "original")),
        "--workers",
        str(options.get("workers", 4)),
        "--retries",
        str(options.get("retries", 2)),
        "--timeout",
        str(options.get("timeout", 120)),
        "--doc-host",
        str(options.get("doc_host", "feishu.cn")),
        "--lark-cli",
        lark_cli,
    ]
    if options.get("space_node_token"):
        command.extend(["--node-token", str(options["space_node_token"])])
    if options.get("space_types"):
        command.extend(["--types", str(options["space_types"])])
    if options.get("resume"):
        command.append("--resume")
    if options.get("flat"):
        command.append("--flat")
    max_nodes = int(options.get("max_nodes") or 0)
    if max_nodes > 0:
        command.extend(["--max-nodes", str(max_nodes)])
    return command


def _journal_command(command: list[str]) -> str:
    return "$ " + " ".join(shlex.quote(part) for part in command)


def _parse_progress(item: JobItem, line: str, job: Job) -> None:
    match = re.match(r"^\[downloaded\]\s+(\S+)\s+->\s+(.*)$", line)
    if match:
        item.downloaded += 1
        return
    if line.startswith("[failed]"):
        item.failed += 1
        return
    if line.startswith("[image-failed]"):
        item.image_failed += 1
        return
    if line.startswith("[title-fallback]"):
        item.fallbacks += 1
        job.append_log("    (title fallback used for one document)")
        return
    if line.startswith("[title-failed]"):  # legacy prefix from v1.1 and older
        item.fallbacks += 1
        job.append_log("    (title fallback used for one document)")
        return
    if line.startswith("[empty]"):
        item.empty += 1
        job.append_log("    (exported document has no content)")
        return
    if line.startswith("[warn]"):
        job.append_log("    (warning from the space exporter; see _manifest.json)")
        return


def _read_manifest(output_dir: Path) -> dict[str, Any] | None:
    manifest_path = output_dir / "_download-manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def finalize_item(item: JobItem, exit_code: int, manifest: dict[str, Any] | None) -> None:
    item.exit_code = exit_code
    if manifest:
        documents = manifest.get("documents") or []
        item.downloaded = int(manifest.get("downloaded_count", len(documents)))
        item.failed = int(manifest.get("failed_count", 0))
        item.images = int(manifest.get("image_downloaded_count", 0))
        item.image_failed = int(manifest.get("image_failed_count", 0))
        item.empty = int(manifest.get("empty_count", 0))
        item.fallbacks = int(
            manifest.get("title_fallback_count", manifest.get("title_failed_count", 0))
        )
        item.titles = [str(doc.get("title", "")) for doc in documents if doc.get("title")]
        item.failures = [
            {"url": failure.get("url"), "error": failure.get("error")}
            for failure in (manifest.get("failures") or [])
        ]
    if exit_code == 0:
        item.status = "ok"
        item.message = "complete"
    elif manifest and item.downloaded:
        item.status = "partial"
        item.message = "finished with failures; see the manifest"
    elif exit_code == 2:
        item.status = "invalid"
        item.message = "invalid URL, argument or output directory"
    else:
        item.status = "failed"
        item.message = "download failed"


def _find_space_manifest(output_root: Path) -> tuple[dict[str, Any] | None, Path | None]:
    """Locate the `_manifest.json` written by download_wiki_space.py."""
    candidates = sorted(
        output_root.glob("*/_manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("space_id"):
            return data, path.parent
    return None, None


def finalize_space_item(item: JobItem, exit_code: int, output_root: Path) -> None:
    """A whole-space export reports totals through its own manifest."""
    item.exit_code = exit_code
    manifest, space_dir = _find_space_manifest(output_root)
    if manifest is None:
        item.status = "failed" if exit_code != 0 else "ok"
        item.message = "空间导出未生成 _manifest.json" if exit_code != 0 else "complete"
        return
    counts = manifest.get("counts") or {}
    item.downloaded = int(counts.get("ok", 0))
    item.failed = int(counts.get("failed", 0))
    item.empty = int(counts.get("empty", 0))
    item.titles = [str(manifest.get("space_name") or item.token)]
    item.output_dir = str(space_dir) if space_dir else None
    partial = int(counts.get("partial", 0))
    unsupported = int(counts.get("unsupported", 0))
    skipped = int(counts.get("skipped", 0))
    item.message = (
        f"节点 {manifest.get('total_nodes', 0)}：成功 {item.downloaded}、"
        f"降级 {partial}、空 {item.empty}、失败 {item.failed}、"
        f"不支持 {unsupported}、跳过 {skipped}"
    )
    if exit_code == 0:
        item.status = "ok"
        item.message = "complete; " + item.message
    elif item.downloaded or partial:
        item.status = "partial"
    else:
        item.status = "failed"


def run_job(job: Job, lark_cli: str) -> None:
    options = job.options
    output_root = Path(job.output_root)
    job.status = "running"
    job.append_log(
        f"job {job.id} started: {len(job.items)} url(s), "
        f"recursive={bool(options.get('recursive'))}, output={output_root}"
    )
    try:
        for index, item in enumerate(job.items, 1):
            if job.cancel_requested:
                item.status = "cancelled"
                item.message = "cancelled before start"
                continue
            item.status = "running"
            item.started_at = utc_now()
            job.append_log(f"[{index}/{len(job.items)}] {item.url}")
            if options.get("mode") == "space":
                command = build_space_command(output_root, options, lark_cli)
            else:
                command = build_command(item.url, output_root, options, lark_cli)
            job.append_log(_journal_command(command))
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(REPO_ROOT),
                )
            except OSError as exc:
                item.status = "failed"
                item.message = f"unable to start the download script: {exc}"
                job.append_log(f"!! {item.message}")
                item.finished_at = utc_now()
                continue

            job.process = process
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if line:
                    job.append_log(line)
                    _parse_progress(item, line, job)
                if job.cancel_requested and process.poll() is None:
                    job.append_log("!! cancel requested; terminating the download ...")
                    process.terminate()
            exit_code = process.wait()
            job.process = None

            if job.cancel_requested and item.status == "running":
                item.status = "cancelled"
                item.exit_code = exit_code
                item.message = "cancelled"
                item.finished_at = utc_now()
                job.append_log(f"-- cancelled (exit code {exit_code})")
                continue

            if options.get("mode") == "space":
                finalize_space_item(item, exit_code, output_root)
            else:
                output_dir = output_root / item.token if item.token else output_root
                item.output_dir = str(output_dir)
                finalize_item(item, exit_code, _read_manifest(output_dir))
            item.finished_at = utc_now()
            job.append_log(
                f"-- {item.status}: downloaded={item.downloaded} failed={item.failed} "
                f"images={item.images} image_failed={item.image_failed} "
                f"empty={item.empty} title_fallback={item.fallbacks} exit={exit_code}"
            )
    except Exception as exc:  # pragma: no cover - defensive
        job.error = f"{type(exc).__name__}: {exc}"
        job.append_log(f"!! unexpected error: {job.error}")
    finally:
        job.process = None
        if job.cancel_requested:
            for item in job.items:
                if item.status in {"pending", "running"}:
                    item.status = "cancelled"
                    item.message = item.message or "cancelled"
        statuses = {item.status for item in job.items}
        if job.error:
            job.status = "failed"
        elif job.cancel_requested:
            job.status = "cancelled"
        elif statuses <= {"ok"}:
            job.status = "done"
        elif "partial" in statuses or "ok" in statuses:
            job.status = "partial"
        else:
            job.status = "failed"
        job.finished_at = utc_now()
        ok = sum(1 for item in job.items if item.status == "ok")
        job.append_log(
            f"job {job.id} {job.status}: ok={ok} "
            f"partial={sum(1 for item in job.items if item.status == 'partial')} "
            f"failed={sum(1 for item in job.items if item.status == 'failed')} "
            f"invalid={sum(1 for item in job.items if item.status == 'invalid')} "
            f"cancelled={sum(1 for item in job.items if item.status == 'cancelled')}"
        )


def collect_urls(raw_lines: list[str], job: Job) -> list[str]:
    """Normalise pasted URLs; also expand lines that point at an existing file."""
    urls: list[str] = []
    seen: set[str] = set()
    for raw in raw_lines:
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if not value.lower().startswith(("http://", "https://")):
            candidate = Path(value).expanduser()
            if candidate.is_file():
                try:
                    nested = candidate.read_text(encoding="utf-8-sig").splitlines()
                except OSError as exc:
                    job.append_log(f"[invalid] {value}: {exc}")
                    continue
                job.append_log(f"[list] expanded {candidate} ({len(nested)} line(s))")
                for nested_url in collect_urls(nested, job):
                    if nested_url.lower() not in seen:
                        seen.add(nested_url.lower())
                        urls.append(nested_url)
                continue
        try:
            _, _, canonical = download_docx_tree.parse_document_url(value)
        except ValueError as exc:
            job.append_log(f"[invalid] {value}: {exc}")
            continue
        if canonical.lower() in seen:
            job.append_log(f"[duplicate] {canonical}: skipped")
            continue
        seen.add(canonical.lower())
        urls.append(canonical)
    return urls


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------
def env_namespace(output_dir: Path | None = None) -> argparse.Namespace:
    """Build the argument namespace expected by scripts/check_env.py."""
    return argparse.Namespace(
        lark_cli=SERVER_CONFIG["lark_cli"],
        identity=SERVER_CONFIG["identity"],
        output_dir=output_dir,
        timeout=60,
    )


def qr_ascii(lark_cli: str, url: str) -> str:
    path = check_env.resolve_lark_cli(lark_cli)
    if not path:
        return ""
    runner = check_env.Runner(30)
    process = runner.run([path, "auth", "qrcode", url, "--ascii"])
    return (process.stdout or "").strip("\n")


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "lark-docs-to-md"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if self.path.startswith("/api/jobs/"):
            return
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        return decoded if isinstance(decoded, dict) else {}

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _path_parts(self) -> list[str]:
        parsed = urllib.parse.urlparse(self.path)
        return [part for part in parsed.path.split("/") if part]

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_get(self) -> None:
        parts = self._path_parts()
        if not parts:
            body = PAGE.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if parts[0] == "api":
            self._route_api_get(parts[1:])
            return
        self._error(404, "not found")

    def _route_api_get(self, parts: list[str]) -> None:
        if not parts:
            self._error(404, "not found")
            return
        if parts[0] == "config":
            self._json(
                {
                    "output_dir": str(SERVER_CONFIG["output_dir"]),
                    "lark_cli": SERVER_CONFIG["lark_cli"],
                    "identity": SERVER_CONFIG["identity"],
                    "repo_root": str(REPO_ROOT),
                    "script": str(DEFAULT_DOWNLOAD_SCRIPT),
                }
            )
            return
        if parts[0] == "env":
            report, exit_code = check_env.build_report(env_namespace())
            report["exit_code"] = exit_code
            self._json(report)
            return
        if parts[0] == "jobs" and len(parts) == 1:
            with JOBS_LOCK:
                jobs = [
                    {
                        "id": job.id,
                        "created_at": job.created_at,
                        "finished_at": job.finished_at,
                        "status": job.status,
                        "urls": len(job.items),
                    }
                    for job in JOBS.values()
                ]
            self._json({"jobs": jobs})
            return
        if parts[0] == "jobs" and len(parts) >= 2:
            job = JOBS.get(parts[1])
            if job is None:
                self._error(404, f"unknown job {parts[1]}")
                return
            if len(parts) == 2:
                offset = int(self._query().get("log_offset", "0") or 0)
                self._json(job.to_json(offset))
                return
            if parts[2] == "files":
                self._handle_files(job)
                return
            if parts[2] == "file":
                self._handle_file(job)
                return
            if parts[2] == "archive":
                self._handle_archive(job)
                return
        self._error(404, "not found")

    def _route_api_post(self, parts: list[str]) -> None:
        if parts == ["login"]:
            path = check_env.resolve_lark_cli(SERVER_CONFIG["lark_cli"])
            if not path:
                self._error(400, "lark-cli executable not found")
                return
            runner = check_env.Runner(60)
            envelope = check_env.initiate_login(path, "docs", runner)
            if envelope is None:
                self._error(500, "unable to start the device-flow login")
                return
            url = str(envelope.get("verification_url", ""))
            self._json(
                {
                    "verification_url": url,
                    "device_code": envelope.get("device_code", ""),
                    "expires_in": envelope.get("expires_in"),
                    "qr_ascii": qr_ascii(SERVER_CONFIG["lark_cli"], url),
                }
            )
            return
        if parts == ["login", "complete"]:
            body = self._read_json_body()
            device_code = str(body.get("device_code") or "").strip()
            if not device_code:
                raise ValueError("device_code is required")
            path = check_env.resolve_lark_cli(SERVER_CONFIG["lark_cli"])
            if not path:
                self._error(400, "lark-cli executable not found")
                return
            runner = check_env.Runner(120)
            process = runner.run([path, "auth", "login", "--device-code", device_code, "--json"])
            ok = process.returncode == 0
            self._json(
                {
                    "ok": ok,
                    "message": (process.stderr or process.stdout).strip()[:2000],
                },
                200 if ok else 400,
            )
            return
        if parts == ["jobs"]:
            self._create_job()
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            job = JOBS.get(parts[1])
            if job is None:
                self._error(404, f"unknown job {parts[1]}")
                return
            job.cancel_requested = True
            process = job.process
            if process is not None and process.poll() is None:
                threading.Thread(target=_kill_process, args=(process,), daemon=True).start()
            self._json({"ok": True, "status": job.status})
            return
        self._error(404, "not found")

    def _route_post(self) -> None:
        parts = self._path_parts()
        if parts and parts[0] == "api":
            self._route_api_post(parts[1:])
            return
        self._error(404, "not found")

    # -- job endpoints ---------------------------------------------------
    def _create_job(self) -> None:
        body = self._read_json_body()
        mode = str(body.get("mode") or "docs").strip().lower()
        if mode not in {"docs", "space"}:
            mode = "docs"

        output_dir = str(body.get("output_dir") or SERVER_CONFIG["output_dir"]).strip()
        output_root = Path(output_dir).expanduser()
        if not output_root.is_absolute():
            output_root = (Path.cwd() / output_root).resolve()
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._error(400, f"unable to create output directory {output_root}: {exc}")
            return

        identity = str(body.get("identity") or SERVER_CONFIG["identity"])
        if identity not in {"user", "bot"}:
            identity = "user"
        attachments = str(body.get("attachments") or "original").strip().lower()
        if attachments not in {"original", "preview", "skip"}:
            attachments = "original"
        options = {
            "mode": mode,
            "recursive": bool(body.get("recursive")),
            "identity": identity,
            "retries": max(0, int(body.get("retries", 2))),
            "timeout": max(1.0, float(body.get("timeout", 120))),
            "max_docs": max(0, int(body.get("max_docs", 0))),
            "max_nodes": max(0, int(body.get("max_nodes", 0))),
            "space_id": str(body.get("space_id") or "").strip(),
            "space_node_token": str(body.get("space_node_token") or "").strip(),
            "space_types": str(body.get("space_types") or "").strip(),
            "attachments": attachments,
            "workers": max(1, int(body.get("workers", 4))),
            "resume": bool(body.get("resume")),
            "flat": bool(body.get("flat")),
            "doc_host": str(body.get("doc_host") or "feishu.cn").strip() or "feishu.cn",
        }

        job = Job(
            id=_new_job_id(),
            created_at=utc_now(),
            options=options,
            output_root=str(output_root),
            items=[],
        )

        if mode == "space":
            space_id = options["space_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", space_id or ""):
                raise ValueError(
                    "提供一个知识库 space_id（可用 `lark-cli wiki +space-list` 查询）"
                )
            job.items.append(
                JobItem(
                    url=f"wiki-space://{space_id}",
                    kind="wiki-space",
                    token=space_id,
                )
            )
        else:
            raw_urls = body.get("urls")
            if isinstance(raw_urls, str):
                raw_lines = raw_urls.splitlines()
            elif isinstance(raw_urls, list):
                raw_lines = [str(item) for item in raw_urls]
            else:
                raw_lines = []
            raw_lines = [line for line in raw_lines if line.strip()]
            if not raw_lines:
                raise ValueError("provide at least one document URL")
            urls = collect_urls(raw_lines, job)
            for url in urls:
                try:
                    kind, token, _ = download_docx_tree.parse_document_url(url)
                except ValueError:
                    continue
                job.items.append(JobItem(url=url, kind=kind, token=token))
            if not job.items:
                hint = "\n".join(job.log[-10:]) or "no valid /docx/ or /wiki/ URL found"
                self._error(400, hint)
                return

        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(
            target=run_job, args=(job, SERVER_CONFIG["lark_cli"]), daemon=True
        ).start()
        self._json(job.to_json(0), 201)

    def _job_output_root(self, job: Job) -> Path:
        return Path(job.output_root).resolve()

    def _item_directories(self, job: Job) -> list[Path]:
        """Directories this job may read files from (preview/zip containment)."""
        root = self._job_output_root(job)
        directories: list[Path] = []
        for item in job.items:
            candidate = Path(item.output_dir) if item.output_dir else None
            if candidate is None and item.token and item.kind != "wiki-space":
                candidate = root / item.token
            if candidate is not None and candidate.is_dir():
                directories.append(candidate.resolve())
        return directories or [root]

    def _resolve_within_root(self, job: Job, relative: str) -> Path | None:
        root = self._job_output_root(job)
        candidate = (root / relative).resolve()
        # Allow only paths inside a directory created by this job.
        for base in self._item_directories(job):
            try:
                candidate.relative_to(base)
                return candidate
            except ValueError:
                continue
        return None

    def _handle_files(self, job: Job) -> None:
        root = self._job_output_root(job)
        item_by_dir: dict[str, JobItem] = {}
        for item in job.items:
            directory = Path(item.output_dir) if item.output_dir else (
                root / item.token if item.token and item.kind != "wiki-space" else None
            )
            if directory is not None:
                item_by_dir[str(directory.resolve())] = item
        directories = self._item_directories(job)
        files: list[dict[str, Any]] = []
        for directory in directories:
            item = item_by_dir.get(str(directory))
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.name in {"_download-manifest.json", "_manifest.json"}:
                    continue
                if path.suffix.lower() not in {
                    ".md", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
                    ".csv", ".pdf", ".html", ".txt", ".docx", ".doc", ".xlsx", ".zip",
                }:
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                files.append(
                    {
                        "token": item.token if item else "",
                        "title": (item.titles[0] if item and item.titles else path.stem),
                        "path": relative,
                        "name": path.name,
                        "size": path.stat().st_size,
                        "is_markdown": path.suffix.lower() in {".md", ".csv"},
                    }
                )
        self._json({"files": files, "output_root": str(root)})

    def _handle_file(self, job: Job) -> None:
        relative = self._query().get("path", "")
        if not relative:
            self._error(400, "path is required")
            return
        resolved = self._resolve_within_root(job, relative)
        if resolved is None or not resolved.is_file():
            self._error(404, "file not found")
            return
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            self._error(500, str(exc))
            return
        text_suffixes = {".md", ".csv", ".txt", ".html"}
        content_type = (
            "text/plain; charset=utf-8"
            if resolved.suffix.lower() in text_suffixes
            else "application/octet-stream"
        )
        self._send(200, data, content_type)

    def _handle_archive(self, job: Job) -> None:
        root = self._job_output_root(job)
        directories = [path for path in self._item_directories(job) if path.is_dir()]
        if not directories:
            self._error(404, "nothing to archive")
            return
        buffer = io.BytesIO()
        base = f"lark-docs-{job.id}"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for directory in directories:
                try:
                    within = directory.relative_to(root).as_posix()
                except ValueError:
                    within = directory.name
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        archive.write(
                            path,
                            arcname=posixpath.join(
                                base, within, path.relative_to(directory).as_posix()
                            ),
                        )
        body = buffer.getvalue()
        filename = f"{base}.zip"
        self._send(
            200,
            body,
            "application/zip",
            {"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _kill_process(process: subprocess.Popen[str]) -> None:
    deadline = time.time() + TERMINATE_GRACE_SECONDS
    while time.time() < deadline and process.poll() is None:
        time.sleep(0.2)
    if process.poll() is None:
        process.kill()


# --------------------------------------------------------------------------
# Front-end (single file, no build step)
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞书文档批量下载 · lark-docs-to-md</title>
<link rel="icon" href="data:,">
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --panel-2: #1e222b; --line: #2a2f3a;
    --text: #e7ebf3; --muted: #97a0b3; --accent: #4c8dff; --ok: #35c47a;
    --warn: #e2b53f; --err: #f2695c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
  header { padding: 18px 22px; border-bottom: 1px solid var(--line); display: flex;
    align-items: center; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  h1 small { color: var(--muted); font-weight: 400; margin-left: 8px; font-size: 12px; }
  main { display: grid; grid-template-columns: minmax(340px, 460px) 1fr; gap: 18px;
    padding: 18px 22px 40px; align-items: start; }
  @media (max-width: 1050px) { main { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 12px; font-weight: 600; letter-spacing: .02em; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
  textarea, input, select { width: 100%; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 7px; padding: 9px 10px; font: inherit; }
  textarea { min-height: 132px; resize: vertical; font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 12.5px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > div { flex: 1 1 120px; }
  button { background: var(--accent); color: #fff; border: 0; border-radius: 7px;
    padding: 9px 14px; font: inherit; font-weight: 600; cursor: pointer; }
  button.ghost { background: transparent; color: var(--text); border: 1px solid var(--line); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 12px; color: var(--text); }
  .check input { width: auto; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
    padding: 3px 9px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .dot.ok { background: var(--ok); } .dot.err { background: var(--err); }
  .dot.warn { background: var(--warn); } .dot.run { background: var(--accent); }
  #envDetail { font-size: 12px; color: var(--muted); margin-top: 10px; white-space: pre-wrap; }
  .item { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px;
    background: var(--panel-2); }
  .item .url { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
    word-break: break-all; }
  .item .meta { font-size: 12px; color: var(--muted); margin-top: 4px; }
  pre#log { background: #0b0d11; border: 1px solid var(--line); border-radius: 8px;
    padding: 10px; height: 300px; overflow: auto; font-size: 12px; margin: 0;
    white-space: pre-wrap; word-break: break-all; }
  .files a { color: var(--accent); text-decoration: none; cursor: pointer; }
  .files li { margin-bottom: 4px; font-size: 13px; }
  .files .size { color: var(--muted); font-size: 11px; margin-left: 6px; }
  #preview { position: fixed; inset: 0 0 0 auto; width: min(760px, 92vw); background: #12151b;
    border-left: 1px solid var(--line); padding: 16px; overflow: auto; display: none; z-index: 20; }
  #preview header { padding: 0 0 10px; border: 0; }
  #previewBody { white-space: pre-wrap; word-break: break-word; font-size: 13px;
    font-family: ui-monospace, Menlo, Consolas, monospace; }
  .hint { color: var(--muted); font-size: 12px; }
  .warnbox { border-left: 3px solid var(--warn); padding-left: 10px; color: var(--warn);
    font-size: 12.5px; }
  #qr { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 6px; line-height: 6px;
    white-space: pre; overflow: auto; max-height: 260px; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>飞书文档批量下载 <small>lark-docs-to-md · 本地 Web 服务</small></h1>
  <span class="status" id="envBadge"><span class="dot" id="envDot"></span><span id="envText">检查环境…</span></span>
  <button class="ghost" id="btnEnv">重新检查</button>
  <button class="ghost" id="btnLogin">授权登录</button>
</header>

<main>
  <section>
    <div class="card">
      <h2>1 · 下载内容</h2>
      <div class="row" style="margin-bottom:6px">
        <div>
          <label for="mode">模式</label>
          <select id="mode">
            <option value="docs">文档链接（单篇 / 批量 / 递归）</option>
            <option value="space">知识库空间（整库镜像，含附件与表格）</option>
          </select>
        </div>
      </div>
      <div id="docsFields">
        <label for="urls">每行一个飞书 /docx/ 或 /wiki/ 链接（也支持直接粘贴一个 URL 列表文本文件的路径）</label>
        <textarea id="urls" placeholder="https://xxx.feishu.cn/docx/Qj58dcHFAoOcOVx5l7mcEK5Hnjb&#10;https://xxx.feishu.cn/docx/Dwhudsgy8oKOUmx03AXcHiX8nvg"></textarea>
        <div class="check">
          <input type="checkbox" id="recursive">
          <label for="recursive" style="margin:0">递归下载文档内引用的子文档（关闭时等价于批量脚本 batch_download.py 的行为）</label>
        </div>
      </div>
      <div id="spaceFields" style="display:none">
        <div class="row">
          <div>
            <label for="spaceId">知识库 space_id</label>
            <input id="spaceId" placeholder="7007715075855450113">
          </div>
          <div>
            <label for="spaceNodeToken">仅导出某棵子树（可选，wikcn… 节点 token）</label>
            <input id="spaceNodeToken" placeholder="留空表示整个空间">
          </div>
        </div>
        <div class="row">
          <div>
            <label for="attachments">附件（file 节点）</label>
            <select id="attachments">
              <option value="original">original（下原件，失败时回退预览件）</option>
              <option value="preview">preview（只存预览件）</option>
              <option value="skip">skip（不下附件）</option>
            </select>
          </div>
          <div>
            <label for="workers">并发数（默认 4，过高会被限流）</label>
            <input id="workers" type="number" min="1" value="4">
          </div>
          <div>
            <label for="spaceTypes">节点类型（逗号分隔，留空=全部）</label>
            <input id="spaceTypes" placeholder="docx,doc,file,sheet">
          </div>
        </div>
        <div class="check">
          <input type="checkbox" id="resume">
          <label for="resume" style="margin:0">断点续传 --resume（跳过已完成节点）</label>
          <input type="checkbox" id="flat" style="margin-left:12px">
          <label for="flat" style="margin:0">--flat（不镜像层级）</label>
        </div>
        <p class="hint">知识库导出会生成 <code>_INDEX.md</code>、<code>_failures.md</code>、<code>_manifest.json/csv</code>，并按 wiki 目录层级镜像到 <code>&lt;输出目录&gt;/&lt;空间名&gt;/</code>。</p>
      </div>
      <div class="row">
        <div>
          <label for="outputDir">输出目录</label>
          <input id="outputDir" placeholder="./downloads">
        </div>
        <div>
          <label for="identity">身份</label>
          <select id="identity">
            <option value="user">user（用户身份，推荐）</option>
            <option value="bot">bot（机器人身份）</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div><label for="retries">失败重试次数</label><input id="retries" type="number" min="0" value="2"></div>
        <div><label for="timeout">单次超时（秒）</label><input id="timeout" type="number" min="1" value="120"></div>
        <div><label for="maxDocs">上限（0=不限：递归模式的文档数 / 知识库的节点数）</label><input id="maxDocs" type="number" min="0" value="0"></div>
      </div>
      <div class="row" style="margin-top:14px">
        <button id="btnStart">开始下载</button>
        <button class="ghost" id="btnCancel" disabled>取消任务</button>
      </div>
      <div id="formError" class="warnbox" style="margin-top:10px; display:none"></div>
    </div>

    <div class="card">
      <h2>2 · 任务</h2>
      <div id="items"><p class="hint">还没有任务。</p></div>
      <div id="files" class="files"></div>
      <div class="row" style="margin-top:10px">
        <button class="ghost" id="btnZip" disabled>打包下载 ZIP</button>
        <button class="ghost" id="btnRefresh">刷新文件列表</button>
      </div>
    </div>
  </section>

  <section>
    <div class="card">
      <h2>任务日志</h2>
      <pre id="log">等待任务…</pre>
    </div>
    <div class="card">
      <h2>环境与授权</h2>
      <div id="envDetail">—</div>
      <div id="loginBox" style="display:none; margin-top:12px">
        <p class="hint">在浏览器打开下面的链接（或用飞书扫二维码）完成授权，然后点击“我已完成授权”。</p>
        <p><a id="loginUrl" href="#" target="_blank" rel="noopener">—</a></p>
        <pre id="qr"></pre>
        <button id="btnLoginDone">我已完成授权</button>
      </div>
    </div>
  </section>
</main>

<div id="preview">
  <header>
    <strong id="previewTitle">预览</strong>
    <button class="ghost" id="btnClosePreview">关闭</button>
  </header>
  <div id="previewBody"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let currentJob = null;
let logOffset = 0;
let pollTimer = null;
let loginDeviceCode = null;

async function api(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }
  if (!response.ok) throw new Error((data && data.error) || response.statusText);
  return data;
}

function showError(message) {
  const box = $('formError');
  box.textContent = message;
  box.style.display = message ? 'block' : 'none';
}

async function loadConfig() {
  const config = await api('/api/config');
  $('outputDir').value = config.output_dir;
  return config;
}

function setEnvBadge(state, text) {
  $('envDot').className = 'dot ' + state;
  $('envText').textContent = text;
}

async function refreshEnv() {
  setEnvBadge('', '检查环境…');
  try {
    const report = await api('/api/env');
    const failing = report.checks.filter((c) => c.status === 'fail');
    if (report.ready) setEnvBadge('ok', '环境就绪' + (report.account ? '：' + report.account : ''));
    else if (failing.some((c) => c.name === 'auth')) setEnvBadge('warn', '需要授权登录');
    else setEnvBadge('err', '缺少运行依赖');
    const lines = report.checks.map((c) => {
      const mark = c.status === 'ok' ? '[ok]  ' : (c.status === 'warn' ? '[warn]' : '[fail]');
      return mark + ' ' + c.name + ': ' + c.detail + (c.fix ? '\n       修复：' + c.fix : '');
    });
    $('envDetail').textContent = lines.join('\n');
    if (report.ready) $('loginBox').style.display = 'none';
  } catch (error) {
    setEnvBadge('err', '检查失败');
    $('envDetail').textContent = String(error.message || error);
  }
}

async function startLogin() {
  showError('');
  try {
    const data = await api('/api/login', { method: 'POST' });
    loginDeviceCode = data.device_code;
    $('loginUrl').textContent = data.verification_url;
    $('loginUrl').href = data.verification_url;
    $('qr').textContent = data.qr_ascii || '';
    $('loginBox').style.display = 'block';
    window.open(data.verification_url, '_blank', 'noopener');
  } catch (error) {
    showError('无法开始授权：' + error.message);
  }
}

async function completeLogin() {
  if (!loginDeviceCode) return;
  $('btnLoginDone').disabled = true;
  try {
    await api('/api/login/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_code: loginDeviceCode }),
    });
    $('loginBox').style.display = 'none';
    loginDeviceCode = null;
    await refreshEnv();
  } catch (error) {
    showError('授权完成失败（设备码约 10 分钟过期，可重新点击“授权登录”）：' + error.message);
  } finally {
    $('btnLoginDone').disabled = false;
  }
}

async function startJob() {
  showError('');
  const mode = $('mode').value;
  const outputDir = $('outputDir').value;
  const retries = Number($('retries').value || 0);
  const timeout = Number($('timeout').value || 120);
  const maxDocs = Number($('maxDocs').value || 0);
  let payload;
  if (mode === 'space') {
    const spaceId = $('spaceId').value.trim();
    if (!spaceId) { showError('请填写知识库 space_id（可用 lark-cli wiki +space-list 查询）。'); return; }
    payload = {
      mode: 'space',
      space_id: spaceId,
      space_node_token: $('spaceNodeToken').value.trim(),
      space_types: $('spaceTypes').value.trim(),
      attachments: $('attachments').value,
      workers: Number($('workers').value || 4),
      resume: $('resume').checked,
      flat: $('flat').checked,
      max_nodes: maxDocs,
      identity: $('identity').value,
      output_dir: outputDir,
      retries: retries,
      timeout: timeout,
    };
  } else {
    const urls = $('urls').value;
    if (!urls.trim()) { showError('请至少粘贴一个飞书文档链接。'); return; }
    payload = {
      mode: 'docs',
      urls: urls,
      recursive: $('recursive').checked,
      identity: $('identity').value,
      output_dir: outputDir,
      retries: retries,
      timeout: timeout,
      max_docs: maxDocs,
    };
  }
  $('btnStart').disabled = true;
  try {
    const job = await api('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    currentJob = job.id;
    logOffset = 0;
    $('log').textContent = '';
    $('btnCancel').disabled = false;
    $('btnZip').disabled = false;
    renderJob(job);
    startPolling();
  } catch (error) {
    showError(String(error.message || error));
    $('btnStart').disabled = false;
  }
}

const STATUS_TEXT = {
  pending: '排队中', running: '下载中', ok: '成功', partial: '部分成功',
  failed: '失败', invalid: '无效', cancelled: '已取消', done: '完成',
  queued: '排队中', cancelled_job: '已取消',
};

function statusClass(status) {
  if (status === 'ok' || status === 'done') return 'ok';
  if (status === 'running' || status === 'queued' || status === 'pending') return 'run';
  if (status === 'partial') return 'warn';
  return 'err';
}

function renderJob(job) {
  const box = $('items');
  box.innerHTML = '';
  for (const item of job.items) {
    const div = document.createElement('div');
    div.className = 'item';
    const label = STATUS_TEXT[item.status] || item.status;
    const meta = [];
    if (item.kind && item.token) meta.push(item.kind + '/' + item.token);
    meta.push('文档 ' + item.downloaded + ' · 图片 ' + item.images);
    if (item.failed) meta.push('失败文档 ' + item.failed);
    if (item.image_failed) meta.push('失败图片 ' + item.image_failed);
    if (item.empty) meta.push('空文档 ' + item.empty);
    if (item.fallbacks) meta.push('标题回退 ' + item.fallbacks);
    if (item.exit_code !== null && item.exit_code !== undefined) meta.push('exit ' + item.exit_code);
    if (item.message) meta.push(item.message);
    div.innerHTML = '<span class="status"><span class="dot ' + statusClass(item.status) + '"></span>' +
      label + '</span><div class="url">' + escapeHtml(item.url) + '</div>' +
      '<div class="meta">' + escapeHtml(meta.join(' · ')) + '</div>';
    if (item.titles && item.titles.length) {
      const titles = document.createElement('div');
      titles.className = 'meta';
      titles.textContent = '标题：' + item.titles.join('、');
      div.appendChild(titles);
    }
    if (item.failures && item.failures.length) {
      const failures = document.createElement('div');
      failures.className = 'meta';
      failures.textContent = '错误：' + item.failures.map((f) => f.error).join(' | ');
      div.appendChild(failures);
    }
    box.appendChild(div);
  }
  const running = job.status === 'queued' || job.status === 'running';
  $('btnStart').disabled = running;
  $('btnCancel').disabled = !running;
  if (!running && pollTimer) { clearInterval(pollTimer); pollTimer = null; renderFiles(); }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 800);
  poll();
}

async function poll() {
  if (!currentJob) return;
  try {
    const job = await api('/api/jobs/' + currentJob + '?log_offset=' + logOffset);
    logOffset = job.log_offset;
    if (job.log && job.log.length) {
      const log = $('log');
      log.textContent += (log.textContent ? '\n' : '') + job.log.join('\n');
      log.scrollTop = log.scrollHeight;
    }
    renderJob(job);
  } catch (error) {
    /* transient errors are ignored while polling */
  }
}

async function cancelJob() {
  if (!currentJob) return;
  try { await api('/api/jobs/' + currentJob + '/cancel', { method: 'POST' }); } catch (e) {}
}

async function renderFiles() {
  if (!currentJob) return;
  try {
    const data = await api('/api/jobs/' + currentJob + '/files');
    const box = $('files');
    box.innerHTML = '';
    if (!data.files.length) { box.innerHTML = '<p class="hint">该任务暂无生成文件。</p>'; return; }
    const list = document.createElement('ul');
    for (const file of data.files) {
      const li = document.createElement('li');
      const link = document.createElement('a');
      link.textContent = file.path;
      link.onclick = () => previewFile(file.path);
      li.appendChild(link);
      const size = document.createElement('span');
      size.className = 'size';
      size.textContent = formatSize(file.size);
      li.appendChild(size);
      list.appendChild(li);
    }
    box.appendChild(list);
  } catch (error) { /* ignore */ }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

async function previewFile(path) {
  const response = await fetch('/api/jobs/' + currentJob + '/file?path=' + encodeURIComponent(path));
  const text = await response.text();
  $('previewTitle').textContent = path;
  $('previewBody').textContent = text;
  $('preview').style.display = 'block';
}

function downloadZip() {
  if (!currentJob) return;
  window.location.href = '/api/jobs/' + currentJob + '/archive';
}

$('btnEnv').onclick = refreshEnv;
$('btnLogin').onclick = startLogin;
$('btnLoginDone').onclick = completeLogin;
$('btnStart').onclick = startJob;
$('btnCancel').onclick = cancelJob;
$('btnZip').onclick = downloadZip;
$('btnRefresh').onclick = renderFiles;
$('btnClosePreview').onclick = () => { $('preview').style.display = 'none'; };

function applyMode() {
  const mode = $('mode').value;
  $('docsFields').style.display = mode === 'docs' ? '' : 'none';
  $('spaceFields').style.display = mode === 'space' ? '' : 'none';
  $('maxDocs').closest('div').querySelector('label').textContent =
    mode === 'space' ? '最大节点数（0=不限，安全上限）' : '最大文档数（0=不限，仅递归）';
}
$('mode').onchange = applyMode;

(async function init() {
  applyMode();
  try { await loadConfig(); } catch (e) { showError(String(e.message || e)); }
  await refreshEnv();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Server bootstrap
# --------------------------------------------------------------------------
SERVER_CONFIG: dict[str, Any] = {}


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local web service for downloading Feishu/Lark docs as Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python web/lark_download_web.py\n"
            "  python web/lark_download_web.py --open --port 9000\n"
            "  python web/lark_download_web.py --output-dir ./downloads"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address; default 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="port; default 8765")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="default download directory; default <repo>/downloads",
    )
    parser.add_argument(
        "--lark-cli",
        default=os.environ.get("LARK_CLI", "lark-cli"),
        help="lark-cli executable; defaults to $LARK_CLI or lark-cli",
    )
    parser.add_argument("-i", "--identity", choices=("user", "bot"), default="user")
    parser.add_argument("--open", action="store_true", help="open the browser after start")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding to a non-loopback address (the service can act as the logged-in user)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not is_loopback(args.host) and not args.allow_remote:
        print(
            f"refusing to bind {args.host}: the service runs lark-cli with your credentials.\n"
            "Use --allow-remote only on a trusted network.",
            file=sys.stderr,
        )
        return 2
    if not DEFAULT_DOWNLOAD_SCRIPT.is_file():
        print(f"error: {DEFAULT_DOWNLOAD_SCRIPT} not found", file=sys.stderr)
        return 2

    SERVER_CONFIG.update(
        {
            "output_dir": Path(args.output_dir).expanduser(),
            "lark_cli": args.lark_cli,
            "identity": args.identity,
        }
    )

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"error: unable to bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    server.daemon_threads = True

    host_for_url = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{host_for_url}:{server.server_address[1]}/"
    print("lark-docs-to-md web service")
    print(f"  URL         : {url}")
    print(f"  output dir  : {SERVER_CONFIG['output_dir']}")
    print(f"  lark-cli    : {args.lark_cli}")
    print("  stop        : Ctrl+C")
    sys.stdout.flush()

    if args.open:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
