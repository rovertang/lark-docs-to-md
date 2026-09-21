# Troubleshooting

Start with `python3 scripts/check_env.py`. Its exit code tells you which class of
problem you have: `0` ready, `1` login required, `2` prerequisite missing.

## Login and permissions

| Symptom | Cause | Fix |
| --- | --- | --- |
| `authentication/token_missing`, `need_user_authorization` | No usable identity, or the refresh token expired | `python3 scripts/check_env.py --login` (device flow, prints URL + QR; it requests `docs,wiki,drive,sheets` by default) |
| `identity not usable: missing (refresh token expired)` | Same as above; the account is remembered but the token is dead | Re-run `--login`; no need to reinstall anything |
| Login URL expired | Device codes live ~10 minutes | Re-run `--login` for a fresh URL and device code |
| `permission denied`, `no permission`, error code `3380004` / `99991679` | The logged-in user cannot open that document | Open the failing URL in Feishu as that user; document permissions do not inherit from the parent page. Authentication errors are never retried automatically |
| `missing_scope` while walking a wiki space | The current token was issued with a narrower `--domain` set | Re-authorize with `python3 scripts/check_env.py --login --domain docs,wiki,drive,sheets`; the whole-space export needs `wiki` for the node tree, `drive` for attachments and `sheets` for CSV |
| Child documents fail while the root succeeds | Each child enforces its own permissions | Give the user the exact failing URLs and continue with the rest |
| `html_response` on an image | The image URL needs a browser session | Confirm the document is visible to the user, then re-run; the manifest records the URL |
| `missing_scope`, or `当前登录缺少该接口所需 scope` on a legacy document | A wiki space may contain legacy `doc` nodes; their `raw_content` API needs a scope that the default domain set does not grant | Re-authorize with the command the exporter prints: `python3 scripts/check_env.py --login --domain docs,wiki,drive,sheets`. Only legacy-document export fails; docx, sheets and attachments keep working, and the node is listed under "不支持/失败" in `_failures.md` |
| A title-fallback warning (`[title-fallback]`) | The exported Markdown had no `<title>` element, so the name came from `wiki +node-get` node metadata or the bare token | Not an error and not a failure: the file is `<token>.md`, the run still exits `0`, and the entry is recorded in `title_fallbacks`. Re-run later to pick up a real title |
| `[empty]` on a document | The exported body was blank | Not an error: the file is still written, `status` is `empty`, and the run still exits `0` |

## Tools and platform

| Symptom | Cause | Fix |
| --- | --- | --- |
| `lark-cli executable not found` | Not installed or not on `PATH` | `npm install -g @larksuite/cli` (Node 18+), or pass `--lark-cli /path/to/lark-cli` / set `LARK_CLI` |
| `pwsh 7.6+ is required` (Windows) | npm generated a `.ps1` wrapper but PowerShell Core is missing | Install PowerShell 7.6+, or point `--lark-cli` at the native executable, or run everything inside WSL2 |
| `python: command not found` on Linux/macOS | Only `python3` exists | Use `python3`; on Windows use `py -3` if `python` opens the Store stub |
| `SyntaxError` on `str \| None` / f-strings | Python older than 3.10 | Install Python 3.10+ and re-run with that interpreter |
| Output directory not writable | Permission or a read-only mount | Pass `--output-dir` to a writable path; WSL2 can write to `/mnt/c/...` when automount is on |

## Download behaviour

| Symptom | Cause | Fix |
| --- | --- | --- |
| `error: URL must contain /docx/<token> or /wiki/<token>` | Legacy docs, sheets, Base, whiteboards, or a truncated URL | Only modern `/docx/` and `/wiki/` URLs are supported |
| Exit code `2` | Invalid URL, invalid argument, or unwritable output directory | Fix the input, then re-run |
| Exit code `1` with `failed=1` | At least one document or image failed | Read `_download-manifest.json` (`failures`, `image_failures`) and report the URLs to the user |
| Repeated runs duplicate work | Not a bug: output is keyed by root token and files are atomically replaced | Same token directory is reused on every run |
| A file is named `<token>.md` | The title fetch fell back | It is recorded in `title_fallbacks` (alias `title_failures`); it is a warning, not a failure, and the exit code is still `0`. Re-run later to pick up the real title (the fallback file is removed automatically) |
| Cancelled job leaves an incomplete folder | Cancellation kills the child process | Treat the folder as partial; re-run the same URL to finish it |

## Whole-space export (`download_wiki_space.py`)

| Symptom | Cause | Fix |
| --- | --- | --- |
| `--output must be a relative path within the current directory` from `drive +download` | The CLI refuses absolute `--output` paths | Not something to pass by hand: the exporter sets the working directory to the target folder and passes `./<name>`. If you call `drive +download` yourself, `cd` into the destination first and use a relative `--output` |
| Intermittent, non-reproducible failures with `--workers 8`/`16` | The Feishu API rate-limits concurrent requests | Keep the default `--workers 4` (or lower it). Re-running with `--resume` retries only the failed and `partial` nodes |
| A node has `status: partial` and a `__preview.pdf` file | The original attachment was refused, but a preview was available; `fallback` records `preview-<type>` | Expected degradation, not a crash. Ask the user for download permission on that attachment if the original is required |
| Several CSVs for one sheet node | The workbook has more than one visible sub-sheet | Expected: `<name>.csv` for a single sub-sheet, `<name>__<sheet title>.csv` for several. Hidden sub-sheets are skipped |
| Everything is skipped after adding `--resume` | `state.json` already marks those nodes `ok`/`empty`/`unsupported`/`skipped` | Delete `state.json` (or change the run signature) to redo them; `failed` and `partial` nodes are retried automatically |
| Exit code `2` with `没有遍历到任何节点` | Wrong `--space-id`, no permission, or not logged in | Check `lark-cli wiki +space-list`, then re-run `scripts/check_env.py --login --domain docs,wiki,drive,sheets` |
| `_manifest.csv` opens with garbled Chinese | The reader ignored UTF-8 without a BOM | The exporter writes UTF-8 **with BOM**; open it with Excel directly or import as UTF-8 |
| A container's own text is missing | It is written to `<dir>/_分类页.md` rather than `<dir>.md` | Expected: `<dir>/` and `<dir>.md` are never siblings |

## Web service

| Symptom | Cause | Fix |
| --- | --- | --- |
| `unable to bind 127.0.0.1:8765` | Port already used | `--port 9000` |
| `refusing to bind <host>` | Non-loopback address without permission | Only add `--allow-remote` on a trusted network; the service runs `lark-cli` as the local user |
| Browser shows "需要授权登录" | Same as the CLI login state | Click 授权登录 in the page, or run `scripts/check_env.py --login` |
| ZIP download is large or slow | Images are included | Expected; images dominate the archive |

## Reporting a run to the user

Always include: absolute output directory, downloaded document count, image
count, failed URLs with reasons, and whether the run was recursive. For a
whole-space export, give the space name, the per-status counts from
`_manifest.json`, and read `_failures.md` for the degraded/unsupported list.

Never claim completeness when the exit code is `1` or when any image failed. Do
not report title fallbacks (`title_fallbacks` / `[title-fallback]`) or empty
documents (`empty_documents` / `[empty]`) as failures: they are warnings, they
are recorded in the manifest, and they leave the exit code at `0`. Mention them
as caveats instead.

Exit codes differ per script: `download_docx_tree.py` and `batch_download.py`
use `0`/`1`/`2` with the meaning above, and `download_wiki_space.py` uses its own
`0`/`1`/`2` (`1` = at least one node failed or `--max-nodes` truncated the walk,
`2` = invalid arguments or nothing walkable).
