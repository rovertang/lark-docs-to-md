---
name: lark-docs-to-md
description: "Download Feishu/Lark Docx or Wiki documents as offline Markdown with local images - single URL, URL-list batch, or recursive child documents - and optionally serve a bundled local web UI for the same job. Use when the user provides feishu.cn / larksuite.com / docx/ or /wiki/ URLs and asks to download, archive, export, mirror, or batch-download them, including 批量下载飞书文档, 导出飞书文档为 Markdown, 递归下载子文档, 下载文档图片. Not for legacy docs, sheets, Base, attachments, or whiteboards."
metadata:
  short-description: "Export Lark Docx/Wiki documents to offline Markdown"
license: MIT
---

# Lark Docx to Markdown

Export Feishu/Lark Docx and Wiki pages to offline Markdown plus local images, by
driving the local `lark-cli`. Everything runs through the scripts bundled in this
skill directory; there is no build step and no third-party Python package.

## Prerequisites

Run the environment check first. It reports Python, `lark-cli`, the login state,
and the exact command to fix anything missing:

```bash
python3 scripts/check_env.py            # human-readable report
python3 scripts/check_env.py --json     # machine-readable report (exit 0 = ready)
```

Interpret the exit code: `0` ready, `1` login required, `2` prerequisite missing.

If login is required, guide the user through the device flow instead of asking
them to read CLI docs:

```bash
python3 scripts/check_env.py --login                # prints URL + QR, then waits
python3 scripts/check_env.py --login --no-wait      # print URL + QR, end your turn
python3 scripts/check_env.py --device-code <code>   # finish after the user confirms
```

Never print, copy, or store access tokens. The device code expires after about
10 minutes; rerun `--login` for a fresh URL. If `lark-cli` is missing entirely,
install it with `npm install -g @larksuite/cli` (Node 18+).

## Run

All commands are relative to this skill directory; resolve it before running.

One document, no recursion (default and safest):

```bash
python3 scripts/download_docx_tree.py "<docx-or-wiki-url>" --output-dir "<dir>"
```

Recursive: also download Docx/Wiki documents linked or cited from the page:

```bash
python3 scripts/download_docx_tree.py "<docx-or-wiki-url>" --output-dir "<dir>" --recursive
```

Several independent URLs (same as the web UI's list mode, no recursion):

```bash
python3 scripts/batch_download.py --urls-file "<urls.txt>" --output-dir "<dir>"
```

Web UI, when the user prefers a browser and wants to click through the download:

```bash
python3 web/lark_download_web.py            # prints http://127.0.0.1:8765/
python3 web/lark_download_web.py --open --port 9000
```

The web service binds `127.0.0.1` only, keeps every job in memory, streams the
child-process log to the page, lists and previews the generated files, and offers
a ZIP of the results. Never bind it to a public address without the user's
explicit consent.

Output always lands in `<output-dir>/<root-token>/` with the document title as
the Markdown filename, `assets/` (or `assets/<token>/` when recursive) for
images, and `_download-manifest.json` for the machine-readable result.

Useful options: `--retries N` (default 2), `--timeout SECONDS` (default 120),
`--max-docs N` (recursive safety cap, 0 = unlimited), `--identity user|bot`
(default `user`), `--lark-cli PATH` (or `$LARK_CLI`).

## Report

- Give the absolute output directory, how many documents and images were
  downloaded, and every URL that failed.
- Exit code `0` means complete, `1` means incomplete, `2` means invalid input.
  Treat images that failed as incomplete output.
- Read `_download-manifest.json` when the user needs source-to-file mappings or
  failure reasons; it records `failures`, `image_failures`, and `title_failures`.
- Do not claim a full archive when a document failed: descendants reachable only
  through that document were never discovered.
- Prefer `--recursive` only when the user asked for linked/cited documents; it
  can pull in a large graph and each child enforces its own permissions.

## References

- `references/agent-install.md` - where each agent (Codex, Claude Code, DSH,
  OpenClaw, Hermes) expects this skill and how to install it. Read when the user
  asks to install, move, or share the skill.
- `references/troubleshooting.md` - login, permission, image, Windows/PowerShell
  and WSL failure modes with the matching fix. Read when a run fails.
- `references/output-format.md` - how Feishu XML is converted (titles, bold
  spacing, callouts, citations, manifest fields). Read when the user questions
  the Markdown formatting.
