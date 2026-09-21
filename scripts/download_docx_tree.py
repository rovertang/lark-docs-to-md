#!/usr/bin/env python3
"""Export Lark/Feishu Docx and Wiki documents as offline Markdown."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


DOCUMENT_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]{}\"']+/(?:docx|wiki)/[A-Za-z0-9_-]+"
    r"(?:\?[^\s<>()\[\]{}\"']*)?(?:#[^\s<>()\[\]{}\"']*)?",
    re.IGNORECASE,
)
DOCX_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]{}\"']+/docx/[A-Za-z0-9_-]+"
    r"(?:\?[^\s<>()\[\]{}\"']*)?(?:#[^\s<>()\[\]{}\"']*)?",
    re.IGNORECASE,
)
SUPPORTED_DOCUMENT_TYPES = {"docx", "wiki"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?:\\.|[^\]])*\]\((?P<url>https?://[^)\s]+)\)", re.IGNORECASE
)
BOLD_DELIMITER_RE = re.compile(r"(?<![\\*])\*\*(?!\*)")
FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
TITLE_LINE_RE = re.compile(
    r"^\s*<title(?:\s+[^>]*)?>(?P<title>.*?)</title>\s*$", re.IGNORECASE
)
CALLOUT_OPEN_RE = re.compile(r"^\s*<callout(?:\s+[^>]*)?>\s*$", re.IGNORECASE)
CALLOUT_CLOSE_RE = re.compile(r"^\s*</callout>\s*$", re.IGNORECASE)
EMOJI_ATTRIBUTE_RE = re.compile(
    r"\bemoji\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')",
    re.IGNORECASE,
)
CITE_ELEMENT_RE = re.compile(
    r"<cite\b(?P<attrs>[^>]*)>(?P<body>.*?)</cite\s*>"
    r"|<cite\b(?P<self_attrs>[^>]*)/\s*>",
    re.IGNORECASE,
)
XML_ATTRIBUTE_RE = re.compile(
    r"(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*"
    r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')"
)
KNOWN_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
IMAGE_CONTENT_TYPE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class DocRef:
    token: str
    url: str
    depth: int
    discovered_from: str | None
    kind: str = "docx"


class FetchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ImageDownloadError(OSError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class DocumentTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def parse_document_url(raw_url: str) -> tuple[str, str, str]:
    """Return (type, token, canonical URL), stripping query and fragment."""
    value = html.unescape(raw_url.strip())
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("expected an absolute http(s) URL")

    segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        type_index = next(
            index
            for index, segment in enumerate(segments)
            if segment.lower() in SUPPORTED_DOCUMENT_TYPES
        )
        document_type = segments[type_index].lower()
        token = segments[type_index + 1]
    except (StopIteration, IndexError) as exc:
        raise ValueError("URL must contain /docx/<token> or /wiki/<token>") from exc

    if not TOKEN_RE.fullmatch(token):
        raise ValueError("document token contains unsupported characters")

    canonical = urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            f"/{document_type}/{token}",
            "",
            "",
        )
    )
    return document_type, token, canonical


def normalize_document_url(raw_url: str) -> tuple[str, str]:
    """Return (token, canonical URL) for a Docx or Wiki URL."""
    _, token, canonical = parse_document_url(raw_url)
    return token, canonical


def normalize_docx_url(raw_url: str) -> tuple[str, str]:
    """Backward-compatible Docx-only URL normalizer."""
    document_type, token, canonical = parse_document_url(raw_url)
    if document_type != "docx":
        raise ValueError("URL must contain /docx/<token>")
    return token, canonical


def extract_document_urls(markdown: str) -> list[tuple[str, str, str]]:
    """Extract Docx/Wiki URLs as (type, token, URL), deduplicated by identity."""
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in DOCUMENT_URL_RE.finditer(markdown):
        try:
            document_type, token, url = parse_document_url(match.group(0))
        except ValueError:
            continue
        identity = (document_type, token)
        if identity not in seen:
            seen.add(identity)
            found.append((document_type, token, url))
    return found


def extract_docx_urls(markdown: str) -> list[tuple[str, str]]:
    """Extract canonical Docx URLs in first-seen order, deduplicated by token."""
    return [
        (token, url)
        for document_type, token, url in extract_document_urls(markdown)
        if document_type == "docx"
    ]


def extract_child_document_refs(
    markdown: str, parent_url: str
) -> list[tuple[str, str, str]]:
    """Extract absolute Docx/Wiki URLs and supported citation blocks."""
    return extract_document_urls(normalize_document_citations(markdown, parent_url))


def extract_child_docx_refs(markdown: str, parent_url: str) -> list[tuple[str, str]]:
    """Extract absolute Docx URLs and Docx citation blocks."""
    return [
        (token, url)
        for document_type, token, url in extract_child_document_refs(
            markdown, parent_url
        )
        if document_type == "docx"
    ]


def _decode_json(text: str) -> dict[str, Any] | None:
    value = text.lstrip("\ufeff\r\n\t ")
    if not value:
        return None
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            return None
        try:
            decoded, _ = json.JSONDecoder().raw_decode(value[start:])
            return decoded if isinstance(decoded, dict) else None
        except json.JSONDecodeError:
            return None


PERMANENT_ERROR_TYPES = {"authorization", "validation", "confirmation"}
PERMANENT_ERROR_CODES = {3380004, 99991679}
PERMISSION_SUBTYPES = {"permission_denied", "missing_scope"}
PERMISSION_MARKERS = (
    "no permission",
    "permission denied",
    "lacks view",
    "forbidden",
    "does not have export permission",
    "does not have download permission",
)


def cli_error_details(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Decode a lark-cli failure envelope into {type, subtype, code, message}."""
    envelope = _decode_json(process.stderr) or _decode_json(process.stdout) or {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    message = str(error.get("message") or process.stderr.strip() or process.stdout.strip())
    if not message:
        message = f"lark-cli exited with code {process.returncode}"
    return {
        "type": str(error.get("type", "")),
        "subtype": str(error.get("subtype", "")),
        "code": error.get("code"),
        "message": message,
    }


def is_permission_error(details: Mapping[str, Any]) -> bool:
    """True when a lark-cli failure means "this identity may not read the resource"."""
    message = str(details.get("message", "")).lower()
    return (
        str(details.get("subtype", "")) in PERMISSION_SUBTYPES
        or details.get("code") in PERMANENT_ERROR_CODES
        or any(marker in message for marker in PERMISSION_MARKERS)
    )


def _error_from_process(process: subprocess.CompletedProcess[str]) -> FetchError:
    details = cli_error_details(process)
    message = str(details["message"])
    permanent = (
        details["type"] in PERMANENT_ERROR_TYPES
        or details["code"] in PERMANENT_ERROR_CODES
        or is_permission_error(details)
    )
    if details["subtype"]:
        message = f"{message} ({details['type']}/{details['subtype']})"
    return FetchError(message, retryable=not permanent)


def lark_cli_command(lark_cli: str, arguments: list[str]) -> list[str]:
    resolved = None
    if os.name == "nt" and not Path(lark_cli).suffix:
        for suffix in (".exe", ".ps1", ".cmd", ".bat", ".com"):
            resolved = shutil.which(f"{lark_cli}{suffix}")
            if resolved:
                break
    if not resolved:
        resolved = shutil.which(lark_cli)
    if not resolved:
        candidate = Path(lark_cli).expanduser()
        if candidate.is_file():
            resolved = str(candidate.resolve())
        else:
            raise FetchError(
                f"lark-cli executable not found: {lark_cli}", retryable=False
            )

    suffix = Path(resolved).suffix.lower()
    command = [resolved, *arguments]
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(command)]
    if os.name == "nt" and suffix == ".ps1":
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise FetchError(
                "pwsh 7.6+ is required to launch the lark-cli PowerShell wrapper",
                retryable=False,
            )
        return [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-File",
            resolved,
            *arguments,
        ]
    return command


def lark_cli_environment() -> dict[str, str]:
    """Environment for lark-cli calls: quiet, no update/skill notices on stderr."""
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def run_lark_cli(
    arguments: list[str],
    *,
    lark_cli: str,
    timeout: float,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run lark-cli, capture UTF-8 output, and never raise on a non-zero exit.

    `cwd` matters for commands such as `drive +download`, whose `--output` must be
    a relative path inside the process working directory.
    """
    command = lark_cli_command(lark_cli, arguments)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=lark_cli_environment(),
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FetchError(f"unable to launch lark-cli: {exc}", retryable=False) from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"lark-cli timed out after {timeout:g} seconds") from exc


def fetch_document(
    ref: DocRef,
    *,
    lark_cli: str,
    identity: str,
    timeout: float,
    doc_format: str = "markdown",
) -> dict[str, Any]:
    arguments = [
        "docs",
        "+fetch",
        "--doc",
        ref.url,
        "--doc-format",
        doc_format,
        "--detail",
        "simple",
        "--scope",
        "full",
        "--as",
        identity,
        "--format",
        "json",
    ]
    process = run_lark_cli(arguments, lark_cli=lark_cli, timeout=timeout)

    if process.returncode != 0:
        raise _error_from_process(process)

    envelope = _decode_json(process.stdout)
    if not envelope or envelope.get("ok") is not True:
        raise _error_from_process(process)

    data = envelope.get("data")
    document = data.get("document") if isinstance(data, dict) else None
    if not isinstance(document, dict) or not isinstance(document.get("content"), str):
        raise FetchError("lark-cli response is missing data.document.content", retryable=False)
    return document


def fetch_document_with_retries(
    ref: DocRef,
    *,
    lark_cli: str,
    identity: str,
    timeout: float,
    retries: int,
    doc_format: str,
) -> tuple[dict[str, Any] | None, FetchError | None]:
    last_error: FetchError | None = None
    for attempt in range(retries + 1):
        try:
            return (
                fetch_document(
                    ref,
                    lark_cli=lark_cli,
                    identity=identity,
                    timeout=timeout,
                    doc_format=doc_format,
                ),
                None,
            )
        except FetchError as exc:
            last_error = exc
            if not exc.retryable or attempt >= retries:
                break
            time.sleep(min(2**attempt, 8))
    return None, last_error


def structured_document_title(document: dict[str, Any]) -> str | None:
    # `docs +fetch --detail simple` returns only content/document_id/revision_id, so
    # the `title` branch below is dormant on the download path: the real title lives
    # in the leading <title> element of the exported markdown. The branch is kept for
    # `--detail full` responses and for callers that pass a document with a title.
    title = document.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    parser = DocumentTitleParser()
    parser.feed(str(document.get("content", "")))
    parser.close()
    title = "".join(parser.parts).strip()
    return title or None


def fetch_wiki_node_title(
    node_token: str,
    *,
    lark_cli: str,
    identity: str,
    timeout: float,
) -> tuple[str | None, str | None]:
    """Return (title, error) for a wiki node via the lightweight `wiki +node-get`.

    This replaces the previous full XML re-export. The markdown export never carries
    a title, and a second full export of the same document cannot invent one, so the
    old fallback cost a whole API call and returned nothing.
    """
    arguments = [
        "wiki",
        "+node-get",
        "--node-token",
        node_token,
        "--as",
        identity,
        "--format",
        "json",
    ]
    try:
        process = run_lark_cli(arguments, lark_cli=lark_cli, timeout=timeout)
    except FetchError as exc:
        return None, str(exc)
    if process.returncode != 0:
        details = cli_error_details(process)
        return None, f"wiki +node-get failed: {details['message']}"
    envelope = _decode_json(process.stdout) or {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    node = data.get("node") if isinstance(data.get("node"), dict) else {}
    for value in (data.get("title"), node.get("title")):
        if isinstance(value, str) and value.strip():
            return value.strip(), None
    return None, "wiki node has no title"


def fetch_drive_document_title(
    url: str,
    *,
    lark_cli: str,
    identity: str,
    timeout: float,
) -> tuple[str | None, str | None]:
    """Return (title, error) from the lightweight `drive +inspect` metadata call.

    `drive +inspect` reports the real document title for docx, legacy doc, sheet and
    wiki URLs alike (it unwraps wiki nodes). It replaces the removed XML re-export:
    that call cost a full document download, while this one returns metadata only.
    """
    arguments = [
        "drive",
        "+inspect",
        "--url",
        url,
        "--as",
        identity,
        "--format",
        "json",
    ]
    try:
        process = run_lark_cli(arguments, lark_cli=lark_cli, timeout=timeout)
    except FetchError as exc:
        return None, str(exc)
    if process.returncode != 0:
        details = cli_error_details(process)
        return None, f"drive +inspect failed: {details['message']}"
    envelope = _decode_json(process.stdout) or {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    for value in (data.get("title"), (data.get("wiki_node") or {}).get("title")):
        if isinstance(value, str) and value.strip():
            return value.strip(), None
    return None, "drive +inspect response has no title"


def fallback_document_name(token: str) -> str:
    """Human-readable file stem used when a document has no usable title.

    Shared with `download_wiki_space.py` so the same untitled document gets the
    same file name from either entry point: `未命名-<token[:8]>` rather than a bare
    token that looks like a machine artefact.
    """
    return f"未命名-{token[:8]}"


def fallback_document_title(
    ref: DocRef,
    *,
    lark_cli: str,
    identity: str,
    timeout: float,
) -> tuple[str, str | None, str]:
    """Single title-fallback path, returning (title, error, source).

    Order: the wiki node's own title (cheapest, and the name the user sees in the
    knowledge base), then `drive +inspect` document metadata, then a readable
    `未命名-<token[:8]>` placeholder. The result never carries a `docx-`/`wiki-`
    type prefix - the manifest already records the document type.
    """
    errors: list[str] = []
    if ref.kind == "wiki":
        title, error = fetch_wiki_node_title(
            ref.token, lark_cli=lark_cli, identity=identity, timeout=timeout
        )
        if title:
            return title, None, "wiki-node-get"
        errors.append(error or "wiki node has no title")
    title, error = fetch_drive_document_title(
        ref.url, lark_cli=lark_cli, identity=identity, timeout=timeout
    )
    if title:
        return title, None, "drive-inspect"
    errors.append(error or "drive +inspect has no title")
    return fallback_document_name(ref.token), "；".join(errors), "token"


def safe_filename(title: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)
    value = re.sub(r"\s+", " ", value).strip(" .") or "document"
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    value = value[:180].rstrip(" .") or "document"
    return f"{value}.md"


def unique_filename(
    title: str,
    token: str,
    owners: dict[str, str],
    *,
    owner_id: str | None = None,
) -> str:
    owner_id = owner_id or token
    filename = safe_filename(title)
    key = filename.casefold()
    owner = owners.get(key)
    if owner is None or owner == owner_id:
        owners[key] = owner_id
        return filename

    suffixes = [token, owner_id.replace(":", "-")]
    for suffix_value in suffixes:
        suffix = f"--{suffix_value}"
        base = safe_filename(title)[:-3]
        base = base[: max(20, 180 - len(suffix))].rstrip(" .") or "document"
        filename = f"{base}{suffix}.md"
        key = filename.casefold()
        owner = owners.get(key)
        if owner is None or owner == owner_id:
            owners[key] = owner_id
            return filename

    counter = 2
    while True:
        suffix = f"--{owner_id.replace(':', '-')}-{counter}"
        base = safe_filename(title)[:-3]
        base = base[: max(20, 180 - len(suffix))].rstrip(" .") or "document"
        filename = f"{base}{suffix}.md"
        key = filename.casefold()
        owner = owners.get(key)
        if owner is None or owner == owner_id:
            owners[key] = owner_id
            return filename
        counter += 1


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def redacted_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def feishu_media_token(url: str) -> str | None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not (
        hostname == "feishu.cn"
        or hostname.endswith(".feishu.cn")
        or hostname == "larksuite.com"
        or hostname.endswith(".larksuite.com")
    ):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2 or segments[0].lower() != "file":
        return None
    token = segments[1]
    return token if TOKEN_RE.fullmatch(token) else None


def _image_suffix(url: str, content_type: str) -> str:
    if content_type in IMAGE_CONTENT_TYPE_SUFFIXES:
        return IMAGE_CONTENT_TYPE_SUFFIXES[content_type]
    url_suffix = Path(urlsplit(url).path).suffix.lower()
    if url_suffix in KNOWN_IMAGE_SUFFIXES:
        return url_suffix
    guessed = mimetypes.guess_extension(content_type, strict=False)
    if guessed and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", guessed):
        return guessed
    return ".img"


def download_image(url: str, asset_dir: Path, index: int, timeout: float) -> Path:
    request = Request(url, headers={"User-Agent": "lark-docx-batch-download/1.0"})
    try:
        response = urlopen(request, timeout=timeout)
    except Exception as exc:
        reason = "permission_denied" if getattr(exc, "code", None) in {401, 403} else "http_error"
        raise ImageDownloadError(
            f"image request failed: {exc}", reason=reason
        ) from exc

    with response:
        content_type = response.headers.get_content_type().lower()
        url_suffix = Path(urlsplit(url).path).suffix.lower()
        generic_binary = content_type in {
            "application/octet-stream",
            "binary/octet-stream",
        }
        if not content_type.startswith("image/") and not (
            generic_binary and url_suffix in KNOWN_IMAGE_SUFFIXES
        ):
            reason = "html_response" if content_type == "text/html" else "unsupported_content_type"
            message = f"image URL returned unsupported content type: {content_type}"
            if reason == "html_response":
                message += "; the URL may require authenticated media access"
            raise ImageDownloadError(message, reason=reason)
        suffix = _image_suffix(url, content_type)
        asset_dir.mkdir(parents=True, exist_ok=True)
        destination = asset_dir / f"image-{index:03d}{suffix}"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=asset_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                shutil.copyfileobj(response, handle)
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return destination


def _media_process_error(
    process: subprocess.CompletedProcess[str], action: str
) -> tuple[str, str]:
    envelope = _decode_json(process.stderr) or _decode_json(process.stdout) or {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    code = error.get("code")
    error_type = str(error.get("type", ""))
    message = str(
        error.get("message")
        or process.stderr.strip()
        or process.stdout.strip()
        or f"lark-cli exited with code {process.returncode}"
    )
    reason = (
        "permission_denied"
        if code in {401, 403, 99991679} or error_type == "authorization"
        else "media_download_failed"
    )
    return reason, f"{action}: {message}"


def download_feishu_media(
    media_token: str,
    asset_dir: Path,
    index: int,
    timeout: float,
    *,
    lark_cli: str,
    identity: str,
) -> Path:
    asset_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    errors: list[tuple[str, str]] = []

    for action in ("+media-download", "+media-preview"):
        temporary_stem = f".image-{index:03d}-{secrets.token_hex(6)}"

        def cleanup_temporary_files() -> None:
            for candidate in asset_dir.glob(f"{temporary_stem}*"):
                if candidate.is_file():
                    candidate.unlink(missing_ok=True)

        arguments = [
            "docs",
            action,
            "--token",
            media_token,
            "--output",
            f"./{temporary_stem}",
            "--as",
            identity,
            "--format",
            "json",
        ]
        try:
            command = lark_cli_command(lark_cli, arguments)
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                cwd=asset_dir,
                check=False,
            )
        except FetchError as exc:
            cleanup_temporary_files()
            raise ImageDownloadError(str(exc), reason="tool_unavailable") from exc
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            cleanup_temporary_files()
            errors.append(("media_download_failed", f"{action}: {exc}"))
            continue

        if process.returncode != 0:
            cleanup_temporary_files()
            errors.append(_media_process_error(process, action))
            continue

        envelope = _decode_json(process.stdout)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        saved_path = data.get("saved_path") if isinstance(data, dict) else None
        content_type = str(data.get("content_type", "")) if isinstance(data, dict) else ""
        if not isinstance(saved_path, str) or not saved_path:
            cleanup_temporary_files()
            errors.append(
                ("media_download_failed", f"{action}: response is missing saved_path")
            )
            continue

        saved = Path(saved_path)
        if not saved.is_absolute():
            saved = asset_dir / saved
        saved = saved.resolve()
        resolved_asset_dir = asset_dir.resolve()
        try:
            saved.relative_to(resolved_asset_dir)
        except ValueError:
            cleanup_temporary_files()
            errors.append(
                ("media_download_failed", f"{action}: returned an unsafe saved_path")
            )
            continue
        if not saved.is_file():
            cleanup_temporary_files()
            errors.append(
                ("media_download_failed", f"{action}: downloaded file is missing")
            )
            continue
        if not content_type.startswith("image/"):
            saved.unlink(missing_ok=True)
            cleanup_temporary_files()
            errors.append(
                (
                    "unsupported_content_type",
                    f"{action}: media returned unsupported content type: {content_type or 'unknown'}",
                )
            )
            continue

        suffix = _image_suffix(str(saved), content_type)
        destination = asset_dir / f"image-{index:03d}{suffix}"
        os.replace(saved, destination)
        cleanup_temporary_files()
        return destination

    reason = (
        "permission_denied"
        if errors and all(item[0] == "permission_denied" for item in errors)
        else errors[-1][0] if errors else "media_download_failed"
    )
    detail = "; ".join(message for _, message in errors) or "unknown media error"
    raise ImageDownloadError(detail, reason=reason)


def localize_markdown_images(
    markdown: str,
    output_dir: Path,
    token: str,
    timeout: float,
    *,
    lark_cli: str = "lark-cli",
    identity: str = "user",
    group_assets_by_document: bool = True,
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    matches = list(MARKDOWN_IMAGE_RE.finditer(markdown))
    if not matches:
        return markdown, [], []

    asset_dir = output_dir / "assets"
    if group_assets_by_document:
        asset_dir /= token
    replacements: dict[str, str] = {}
    images: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []

    for match in matches:
        url = match.group("url")
        if url in replacements:
            continue
        try:
            media_token = feishu_media_token(url)
            if media_token:
                destination = download_feishu_media(
                    media_token,
                    asset_dir,
                    len(images) + 1,
                    timeout,
                    lark_cli=lark_cli,
                    identity=identity,
                )
            else:
                destination = download_image(
                    url, asset_dir, len(images) + 1, timeout
                )
            relative_path = destination.relative_to(output_dir).as_posix()
            replacements[url] = relative_path
            images.append({"file": relative_path})
        except ImageDownloadError as exc:
            failures.append(
                {
                    "url": redacted_url(url),
                    "reason": exc.reason,
                    "error": str(exc),
                }
            )
        except OSError as exc:
            failures.append(
                {
                    "url": redacted_url(url),
                    "reason": "filesystem_error",
                    "error": str(exc),
                }
            )

    if not replacements:
        return markdown, images, failures

    parts: list[str] = []
    last_end = 0
    for match in matches:
        parts.append(markdown[last_end : match.start("url")])
        url = match.group("url")
        parts.append(replacements.get(url, url))
        last_end = match.end("url")
    parts.append(markdown[last_end:])
    return "".join(parts), images, failures


def _inline_code_ranges(line: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(line):
        if line[index] != "`" or (index > 0 and line[index - 1] == "\\"):
            index += 1
            continue
        run_end = index
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        marker = line[index:run_end]
        close = line.find(marker, run_end)
        if close < 0:
            index = run_end
            continue
        ranges.append((index, close + len(marker)))
        index = close + len(marker)
    return ranges


def _is_word_character(value: str) -> bool:
    return bool(value) and unicodedata.category(value)[0] in {"L", "N"}


def _normalize_bold_line(line: str) -> str:
    code_ranges = _inline_code_ranges(line)
    delimiters = [
        match
        for match in BOLD_DELIMITER_RE.finditer(line)
        if not any(start <= match.start() < end for start, end in code_ranges)
    ]
    insertions: set[int] = set()
    for index in range(0, len(delimiters) - 1, 2):
        opening = delimiters[index]
        closing = delimiters[index + 1]
        if opening.start() > 0 and _is_word_character(line[opening.start() - 1]):
            insertions.add(opening.start())
        after_closing = closing.end()
        if after_closing < len(line) and _is_word_character(line[after_closing]):
            insertions.add(after_closing)

    if not insertions:
        return line
    parts: list[str] = []
    previous = 0
    for position in sorted(insertions):
        parts.append(line[previous:position])
        parts.append(" ")
        previous = position
    parts.append(line[previous:])
    return "".join(parts)


def normalize_markdown_bold_spacing(markdown: str) -> str:
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not line[fence.end() :].strip()
            ):
                fence_character = None
                fence_length = 0
            lines.append(line)
            continue
        if fence_character is not None or line.startswith(("    ", "\t")):
            lines.append(line)
        else:
            lines.append(_normalize_bold_line(line))
    return "".join(lines)


def _xml_attributes(source: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in XML_ATTRIBUTE_RE.finditer(source):
        value = match.group("double") or match.group("single") or ""
        attributes[match.group("name").lower()] = html.unescape(value)
    return attributes


def _escape_markdown_link_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
    )


def _normalize_document_citations_in_line(line: str, parent_url: str) -> str:
    parent = urlsplit(parent_url)
    code_ranges = _inline_code_ranges(line)

    def replace(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in code_ranges):
            return match.group(0)
        attributes = _xml_attributes(
            match.group("attrs") or match.group("self_attrs") or ""
        )
        file_type = attributes.get("file-type", "").lower()
        if (
            attributes.get("type", "").lower() != "doc"
            or file_type not in SUPPORTED_DOCUMENT_TYPES
        ):
            return match.group(0)
        document_token = attributes.get("doc-id") or attributes.get("token") or ""
        if not TOKEN_RE.fullmatch(document_token):
            return match.group(0)

        body = re.sub(r"<[^>]+>", "", match.group("body") or "")
        title = attributes.get("title") or html.unescape(body) or document_token
        title = re.sub(r"\s+", " ", title).strip() or document_token
        url = urlunsplit(
            (parent.scheme, parent.netloc, f"/{file_type}/{document_token}", "", "")
        )
        return f"[{_escape_markdown_link_text(title)}]({url})"

    return CITE_ELEMENT_RE.sub(replace, line)


def normalize_document_citations(markdown: str, parent_url: str) -> str:
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not line[fence.end() :].strip()
            ):
                fence_character = None
                fence_length = 0
            lines.append(line)
            continue
        if fence_character is not None or line.startswith(("    ", "\t")):
            lines.append(line)
        else:
            lines.append(_normalize_document_citations_in_line(line, parent_url))
    return "".join(lines)


def normalize_docx_citations(markdown: str, parent_url: str) -> str:
    """Backward-compatible alias that now also normalizes Wiki citations."""
    return normalize_document_citations(markdown, parent_url)


def _callout_as_blockquote(lines: list[str], emoji: str) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [f"> {emoji}" if emoji else ">"]

    if emoji:
        lines[0] = f"{emoji} {lines[0]}"
    return [f"> {line}" if line else ">" for line in lines]


def downgrade_docxxml_blocks(markdown: str) -> str:
    had_trailing_newline = markdown.endswith(("\n", "\r"))
    output: list[str] = []
    callout_lines: list[str] | None = None
    callout_opening = ""
    callout_emoji = ""
    fence_character: str | None = None
    fence_length = 0

    for line in markdown.splitlines():
        if callout_lines is not None:
            if CALLOUT_CLOSE_RE.fullmatch(line):
                output.extend(_callout_as_blockquote(callout_lines, callout_emoji))
                callout_lines = None
                callout_opening = ""
                callout_emoji = ""
            else:
                callout_lines.append(line)
            continue

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_character
                and len(marker) >= fence_length
                and not line[fence.end() :].strip()
            ):
                fence_character = None
                fence_length = 0
            output.append(line)
            continue
        if fence_character is not None or line.startswith(("    ", "\t")):
            output.append(line)
            continue

        title_match = TITLE_LINE_RE.fullmatch(line)
        if title_match:
            title = structured_document_title({"content": line})
            output.append(f"# {title}" if title else line)
            continue

        if CALLOUT_OPEN_RE.fullmatch(line):
            callout_lines = []
            callout_opening = line
            emoji_match = EMOJI_ATTRIBUTE_RE.search(line)
            if emoji_match:
                callout_emoji = html.unescape(
                    emoji_match.group("double") or emoji_match.group("single") or ""
                ).strip()
            continue

        output.append(line)

    if callout_lines is not None:
        output.append(callout_opening)
        output.extend(callout_lines)

    result = "\n".join(output)
    if had_trailing_newline:
        result += "\n"
    return result


def download_tree(
    root_url: str,
    output_dir: Path,
    *,
    lark_cli: str = "lark-cli",
    identity: str = "user",
    retries: int = 2,
    timeout: float = 120,
    max_docs: int = 0,
    recursive: bool = False,
    group_assets: bool | None = None,
) -> dict[str, Any]:
    root_kind, root_token, canonical_root = parse_document_url(root_url)
    output_dir = output_dir.expanduser().resolve() / root_token
    output_dir.mkdir(parents=True, exist_ok=True)

    queue: deque[DocRef] = deque(
        [DocRef(root_token, canonical_root, 0, None, root_kind)]
    )
    queued_documents = {(root_kind, root_token)}
    documents: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    image_failures: list[dict[str, Any]] = []
    title_fallbacks: list[dict[str, Any]] = []
    empty_documents: list[dict[str, Any]] = []
    filename_owners: dict[str, str] = {}
    attempted = 0
    limited = False

    while queue:
        if max_docs and attempted >= max_docs:
            limited = True
            break
        ref = queue.popleft()
        attempted += 1

        document, last_error = fetch_document_with_retries(
            ref,
            lark_cli=lark_cli,
            identity=identity,
            timeout=timeout,
            retries=retries,
            doc_format="markdown",
        )

        if document is None:
            failures.append(
                {
                    "token": ref.token,
                    "type": ref.kind,
                    "url": ref.url,
                    "depth": ref.depth,
                    "discovered_from": ref.discovered_from,
                    "error": str(last_error or "unknown fetch error"),
                }
            )
            print(f"[failed] {ref.url}: {last_error}", file=sys.stderr)
            continue

        content = str(document["content"])
        content, images, current_image_failures = localize_markdown_images(
            content,
            output_dir,
            ref.token,
            timeout,
            lark_cli=lark_cli,
            identity=identity,
            group_assets_by_document=recursive if group_assets is None else group_assets,
        )
        content = downgrade_docxxml_blocks(content)
        content = normalize_document_citations(content, ref.url)
        content = normalize_markdown_bold_spacing(content)
        for failure in current_image_failures:
            failure.update(
                {
                    "document_token": ref.token,
                    "document_type": ref.kind,
                    "document_url": ref.url,
                }
            )
            image_failures.append(failure)
            print(
                f"[image-failed] {ref.url} [{failure['reason']}]: "
                f"{failure['error']}",
                file=sys.stderr,
            )
        if content and not content.endswith("\n"):
            content += "\n"
        title = structured_document_title(document)
        title_is_fallback = False
        title_error: str | None = None
        if title is None:
            # Fallback naming is a warning, never a download failure: the file is
            # written either way. See `title_fallbacks` below and the exit-code
            # contract in README.md / SKILL.md.
            title, title_error, title_source = fallback_document_title(
                ref, lark_cli=lark_cli, identity=identity, timeout=timeout
            )
            title_is_fallback = True
            title_fallbacks.append(
                {
                    "token": ref.token,
                    "type": ref.kind,
                    "url": ref.url,
                    "depth": ref.depth,
                    "discovered_from": ref.discovered_from,
                    "error": title_error,
                    "source": title_source,
                }
            )
            if title_error:
                print(f"[title-fallback] {ref.url}: {title_error}", file=sys.stderr)
            else:
                print(
                    f"[title-fallback] {ref.url}: title taken from {title_source}",
                    file=sys.stderr,
                )
        filename = unique_filename(
            title,
            ref.token,
            filename_owners,
            owner_id=f"{ref.kind}:{ref.token}",
        )
        destination = output_dir / filename
        atomic_write_text(destination, content)
        fallback_destination = output_dir / safe_filename(fallback_document_name(ref.token))
        if fallback_destination != destination:
            fallback_destination.unlink(missing_ok=True)

        is_empty = content.strip() == ""
        if is_empty:
            empty_documents.append(
                {
                    "token": ref.token,
                    "type": ref.kind,
                    "url": ref.url,
                    "title": title,
                    "file": filename,
                    "depth": ref.depth,
                    "discovered_from": ref.discovered_from,
                }
            )
            print(f"[empty] {ref.url}: exported document has no content", file=sys.stderr)

        links = extract_child_document_refs(content, ref.url) if recursive else []
        for child_kind, child_token, child_url in links:
            child_identity = (child_kind, child_token)
            if child_identity in queued_documents:
                continue
            queued_documents.add(child_identity)
            queue.append(
                DocRef(
                    child_token,
                    child_url,
                    ref.depth + 1,
                    ref.token,
                    child_kind,
                )
            )

        documents.append(
            {
                "token": ref.token,
                "type": ref.kind,
                "url": ref.url,
                "title": title,
                "file": filename,
                "format": "markdown",
                "status": "empty" if is_empty else "ok",
                "empty": is_empty,
                "title_fallback": title_is_fallback,
                "depth": ref.depth,
                "discovered_from": ref.discovered_from,
                "document_id": document.get("document_id"),
                "revision_id": document.get("revision_id"),
                "links": [url for _, _, url in links],
                "images": images,
            }
        )
        print(f"[downloaded] {ref.url} -> {destination}")

    manifest = {
        "root_url": canonical_root,
        "root_type": root_kind,
        "recursive": recursive,
        "output_dir": str(output_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Only real failures make a run incomplete. Title fallbacks and empty
        # documents are warnings: their content was written successfully.
        "complete": not failures and not image_failures and not limited,
        "downloaded_count": len(documents),
        "failed_count": len(failures),
        "image_downloaded_count": sum(len(item["images"]) for item in documents),
        "image_failed_count": len(image_failures),
        "empty_count": len(empty_documents),
        "title_fallback_count": len(title_fallbacks),
        "title_failed_count": len(title_fallbacks),  # backward-compatible alias
        "limited": limited,
        "pending_count": len(queue),
        "documents": documents,
        "failures": failures,
        "image_failures": image_failures,
        "empty_documents": empty_documents,
        "title_fallbacks": title_fallbacks,
        "title_failures": title_fallbacks,  # backward-compatible alias
    }
    atomic_write_text(
        output_dir / "_download-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Feishu/Lark Docx and Wiki documents as Markdown.",
        epilog=(
            "examples:\n"
            "  python download_docx_tree.py URL\n"
            "  python download_docx_tree.py URL -r\n"
            "  python download_docx_tree.py URL -o OUTPUT_DIR\n"
            "  python download_docx_tree.py --url URL --output-dir OUTPUT_DIR"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "root_url",
        nargs="?",
        help="root Feishu/Lark /docx/ or /wiki/ URL",
    )
    parser.add_argument(
        "--url",
        dest="url_flag",
        help="root URL (alternative to the positional URL)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help=(
            "parent directory; defaults to the current working directory, "
            "with a root-token subdirectory created inside it"
        ),
    )
    parser.add_argument(
        "-i", "--identity", choices=("user", "bot"), default="user"
    )
    parser.add_argument("--retries", type=non_negative_int, default=2)
    parser.add_argument("--timeout", type=positive_float, default=120)
    parser.add_argument(
        "--max-docs",
        type=non_negative_int,
        default=0,
        help="maximum attempted documents in recursive mode; 0 means unlimited",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="download linked Docx/Wiki documents recursively",
    )
    parser.add_argument(
        "--lark-cli", default=os.environ.get("LARK_CLI", "lark-cli")
    )
    args = parser.parse_args(argv)
    if args.root_url and args.url_flag:
        parser.error("provide the URL either positionally or with --url, not both")
    if not args.root_url and not args.url_flag:
        parser.error("a root Feishu/Lark /docx/ or /wiki/ URL is required")
    args.url = args.root_url or args.url_flag
    del args.root_url
    del args.url_flag
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = download_tree(
            args.url,
            args.output_dir,
            lark_cli=args.lark_cli,
            identity=args.identity,
            retries=args.retries,
            timeout=args.timeout,
            max_docs=args.max_docs,
            recursive=args.recursive,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"summary: output={manifest['output_dir']} "
        f"downloaded={manifest['downloaded_count']} "
        f"failed={manifest['failed_count']} "
        f"images={manifest['image_downloaded_count']} "
        f"image_failed={manifest['image_failed_count']} "
        f"title_fallback={manifest['title_fallback_count']} "
        f"empty={manifest['empty_count']} "
        f"recursive={manifest['recursive']} "
        f"limited={manifest['limited']}"
    )
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
