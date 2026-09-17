# Troubleshooting

Start with `python3 scripts/check_env.py`. Its exit code tells you which class of
problem you have: `0` ready, `1` login required, `2` prerequisite missing.

## Login and permissions

| Symptom | Cause | Fix |
| --- | --- | --- |
| `authentication/token_missing`, `need_user_authorization` | No usable identity, or the refresh token expired | `python3 scripts/check_env.py --login` (device flow, prints URL + QR) |
| `identity not usable: missing (refresh token expired)` | Same as above; the account is remembered but the token is dead | Re-run `--login`; no need to reinstall anything |
| Login URL expired | Device codes live ~10 minutes | Re-run `--login` for a fresh URL and device code |
| `permission denied`, `no permission`, error code `3380004` / `99991679` | The logged-in user cannot open that document | Open the failing URL in Feishu as that user; document permissions do not inherit from the parent page. Authentication errors are never retried automatically |
| Child documents fail while the root succeeds | Each child enforces its own permissions | Give the user the exact failing URLs and continue with the rest |
| `html_response` on an image | The image URL needs a browser session | Confirm the document is visible to the user, then re-run; the manifest records the URL |

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
| Exit code `1` with `failed=1` | At least one document or image failed | Read `_download-manifest.json` (`failures`, `image_failures`, `title_failures`) and report the URLs to the user |
| Repeated runs duplicate work | Not a bug: output is keyed by root token and files are atomically replaced | Same token directory is reused on every run |
| A file is named `docx-<token>.md` | The title fetch failed | It is recorded in `title_failures`; re-run later to pick up the real title (the fallback file is removed automatically) |
| Cancelled job leaves an incomplete folder | Cancellation kills the child process | Treat the folder as partial; re-run the same URL to finish it |

## Web service

| Symptom | Cause | Fix |
| --- | --- | --- |
| `unable to bind 127.0.0.1:8765` | Port already used | `--port 9000` |
| `refusing to bind <host>` | Non-loopback address without permission | Only add `--allow-remote` on a trusted network; the service runs `lark-cli` as the local user |
| Browser shows "需要授权登录" | Same as the CLI login state | Click 授权登录 in the page, or run `scripts/check_env.py --login` |
| ZIP download is large or slow | Images are included | Expected; images dominate the archive |

## Reporting a run to the user

Always include: absolute output directory, downloaded document count, image
count, failed URLs with reasons, and whether the run was recursive. Never claim
completeness when the exit code is `1` or when any image failed.
