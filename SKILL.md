---
name: lark-docs-to-md
description: "Download Feishu/Lark Docx or Wiki documents as offline Markdown with local images - single URL, URL-list batch, recursive child documents, or a whole wiki space (knowledge base) with legacy docs, spreadsheets and attachments. Includes a local web UI. Use when given feishu.cn / larksuite.com /docx/ or /wiki/ URLs or a wiki space id and asked to download, archive, export or mirror them, including 批量下载飞书文档, 导出飞书知识库, 递归下载子文档, 下载文档图片. Not for Base, mindnotes, slides or whiteboards."
metadata:
  short-description: "Export Lark Docx/Wiki documents to offline Markdown"
  version: "1.2.1"
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

A whole wiki space (knowledge base), when the user names a space rather than a
single page. This walks the wiki node tree, mirrors the classification hierarchy
on disk, and exports every node type it can (`docx` to Markdown plus local
images, legacy `doc` as plain text, `sheet` to one CSV per visible sub-sheet,
attachments original-first, everything else listed as unsupported):

```bash
python3 scripts/download_wiki_space.py --space-id "<SPACE_ID>" --output-dir "<dir>"
python3 scripts/download_wiki_space.py --space-id "<SPACE_ID>" --node-token "<wikcn...>" -o "<dir>"
python3 scripts/download_wiki_space.py --space-id "<SPACE_ID>" --dry-run    # plan only
```

Useful options for the space export: `--attachments original|preview|skip`
(default `original`), `--workers N` (default 4; higher values cause intermittent
API failures), `--resume` (reuse `state.json` and skip finished nodes),
`--flat`, `--max-nodes N`, `--types a,b,c`, `--doc-host HOST`, `--lark-cli PATH`.
The space export writes `<output-dir>/<space name>/` with `_INDEX.md` (tree plus
status badges), `_failures.md` (failed / degraded / empty / unsupported /
skipped), `_manifest.json`, `_manifest.csv` (UTF-8 with BOM, so Excel opens
Chinese correctly) and `state.json` when `--resume` is used. Multi-sheet
workbooks become one CSV per visible sub-sheet named after the sub-sheet
(`<doc>__<sheet name>.csv`, with the `sheet_id -> sheet_name -> file` mapping kept
in `nodes[].sheets`); the reported file counts and sizes cover images and
attachments, not just the node products. `--resume` without an existing
`state.json` cannot skip anything and now says so explicitly.

Web UI, when the user prefers a browser and wants to click through the download.
It offers both modes - a list of document URLs, or one whole wiki space by
`space_id` (with attachments, workers, resume and flat toggles):

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
- Warnings never change the exit code. A document whose title could not be read
  is written under a fallback name (`未命名-<token[:8]>`, the same placeholder the
  space exporter uses) and logged as `[title-fallback]`;
  the title is rescued first from the wiki node (`wiki +node-get`) and then from
  `drive +inspect`, and `title_fallbacks[].source` records which one worked. A
  document whose exported body is blank is logged as `[empty]`. Both are recorded
  in the manifest and printed to stderr, and the run still exits `0`. Do not
  report either one as a failure.
- `scripts/download_wiki_space.py` has its own exit codes: `0` when no node
  failed, `1` when at least one node failed or `--max-nodes` stopped the walk
  early, `2` for invalid arguments or nothing walkable. Read `_failures.md` and
  `_manifest.json` before claiming a full archive.
- Read `_download-manifest.json` when the user needs source-to-file mappings or
  failure reasons; it records `failures`, `image_failures`, `documents[]`
  (each with `status: "ok"|"empty"`, `empty`, `title_fallback`),
  `title_fallbacks`, `empty_documents`, and the legacy aliases `title_failures`
  / `title_failed_count`.
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
  spacing, callouts, citations, manifest fields, the space-export layout). Read
  when the user questions the Markdown formatting or the manifest keys.
