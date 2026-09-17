#!/usr/bin/env python3
"""Download each Lark/Feishu document URL listed in a text file."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from download_docx_tree import non_negative_int, parse_document_url, positive_float


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_URLS_FILE = SCRIPT_DIR / "document_urls.txt"
DOWNLOAD_SCRIPT = SCRIPT_DIR / "download_docx_tree.py"


@dataclass(frozen=True)
class UrlEntry:
    line_number: int
    document_type: str
    token: str
    url: str


@dataclass(frozen=True)
class UrlList:
    entries: list[UrlEntry]
    invalid: list[tuple[int, str]]
    duplicate_lines: list[int]
    listed_count: int


def load_url_list(path: Path) -> UrlList:
    text = path.read_text(encoding="utf-8-sig")
    entries: list[UrlEntry] = []
    invalid: list[tuple[int, str]] = []
    duplicate_lines: list[int] = []
    seen_urls: set[str] = set()
    output_tokens: dict[str, str] = {}
    listed_count = 0

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        listed_count += 1
        try:
            document_type, token, canonical_url = parse_document_url(value)
        except ValueError as exc:
            invalid.append((line_number, str(exc)))
            continue

        if canonical_url in seen_urls:
            duplicate_lines.append(line_number)
            continue

        previous_url = output_tokens.get(token)
        if previous_url and previous_url != canonical_url:
            invalid.append(
                (
                    line_number,
                    f"token {token} would reuse an output directory already assigned "
                    f"to {previous_url}",
                )
            )
            continue

        seen_urls.add(canonical_url)
        output_tokens[token] = canonical_url
        entries.append(UrlEntry(line_number, document_type, token, canonical_url))

    return UrlList(entries, invalid, duplicate_lines, listed_count)


def build_download_command(entry: UrlEntry, args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(DOWNLOAD_SCRIPT),
        entry.url,
        "-o",
        str(args.output_dir),
        "-i",
        args.identity,
        "--retries",
        str(args.retries),
        "--timeout",
        str(args.timeout),
        "--lark-cli",
        args.lark_cli,
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Docx/Wiki URLs from a text file without following linked documents."
        ),
        epilog=(
            "examples:\n"
            "  python batch_download.py\n"
            "  python batch_download.py -f my_urls.txt -o C:\\Temp"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-f",
        "--urls-file",
        type=Path,
        default=DEFAULT_URLS_FILE,
        help="URL list file; defaults to document_urls.txt beside this script",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="parent output directory; defaults to the current working directory",
    )
    parser.add_argument(
        "-i", "--identity", choices=("user", "bot"), default="user"
    )
    parser.add_argument("--retries", type=non_negative_int, default=2)
    parser.add_argument("--timeout", type=positive_float, default=120)
    parser.add_argument(
        "--lark-cli", default=os.environ.get("LARK_CLI", "lark-cli")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    urls_file = args.urls_file.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()

    try:
        url_list = load_url_list(urls_file)
    except OSError as exc:
        print(f"error: unable to read URL list {urls_file}: {exc}", file=sys.stderr)
        return 2

    for line_number, error in url_list.invalid:
        print(f"[invalid] line {line_number}: {error}", file=sys.stderr)
    for line_number in url_list.duplicate_lines:
        print(f"[duplicate] line {line_number}: skipped")

    if not url_list.entries:
        print(f"error: no valid document URLs found in {urls_file}", file=sys.stderr)
        return 2

    failures: list[tuple[UrlEntry, int]] = []
    for index, entry in enumerate(url_list.entries, 1):
        print(
            f"[batch {index}/{len(url_list.entries)}] "
            f"{entry.document_type}/{entry.token}",
            flush=True,
        )
        try:
            process = subprocess.run(build_download_command(entry, args), check=False)
        except KeyboardInterrupt:
            print("\ninterrupted by user", file=sys.stderr)
            return 130
        except OSError as exc:
            print(f"[batch-failed] {entry.url}: {exc}", file=sys.stderr)
            failures.append((entry, 2))
            continue
        if process.returncode != 0:
            failures.append((entry, process.returncode))
            print(
                f"[batch-failed] {entry.url}: child exit code {process.returncode}",
                file=sys.stderr,
            )

    succeeded = len(url_list.entries) - len(failures)
    print(
        f"batch summary: file={urls_file} output={args.output_dir} "
        f"listed={url_list.listed_count} valid={len(url_list.entries)} "
        f"succeeded={succeeded} failed={len(failures)} "
        f"invalid={len(url_list.invalid)} "
        f"duplicates={len(url_list.duplicate_lines)} recursive=False"
    )
    return 0 if not failures and not url_list.invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())
