# Output format and manifest reference

## Directory layout

Single document (no `--recursive`):

```text
<output-dir>/<root-token>/
├── <document title>.md
├── assets/
│   ├── image-001.png
│   └── image-002.jpg
└── _download-manifest.json
```

Recursive (`--recursive`, `assets/` is split per document token):

```text
<output-dir>/<root-token>/
├── <root title>.md
├── <child title>.md
├── <child title>--<child-token>.md     # only when two titles collide
├── assets/<root-token>/image-001.png
├── assets/<child-token>/image-001.jpg
└── _download-manifest.json
```

Rules:

- The parent directory defaults to the current working directory, and the
  `<root-token>/` subdirectory is always created inside it.
- Markdown filenames use the real title from Feishu's export, not the first
  heading in the body. The title comes from the leading `<title>` element of the
  exported Markdown. If that is missing, a `wiki` document first asks
  `lark-cli wiki +node-get --node-token <token>` for the node title, then every
  document type asks `lark-cli drive +inspect --url <url>` for the document title
  (metadata only - the old "re-export the whole document as XML" call is gone),
  and only then falls back to `未命名-<token[:8]>` - the same readable placeholder
  the space exporter uses, so both entry points name an untitled document
  identically. There is no `docx-` or `wiki-` type prefix, because the manifest
  already records the type. The reason is recorded in `title_fallbacks` (with
  `source` = `wiki-node-get`, `drive-inspect` or `token`), logged as `[title-fallback]` on
  stderr, and **never** changes the exit code: the file is written either way. On a later
  successful run the fallback file is deleted.
- A document whose exported body is blank (`content.strip() == ""`) is written
  with `status: "empty"`, listed in `empty_documents`, logged as `[empty]` on
  stderr, and still exits `0`. Empty is not a failure.
- Two documents with the same title get a `--<token>` suffix.
- Files are replaced atomically on re-run, so an interrupted run never leaves a
  half-written Markdown file in place of a good one.

## Markdown normalisation

The Feishu XML/markdown export is cleaned up so the result reads well offline:

- `<title>...</title>` becomes an H1 heading.
- `<callout emoji="...">` becomes a blockquote prefixed with the emoji, the
  closest stable Markdown equivalent.
- `<cite doc-id="..." file-type="docx|wiki" title="...">` becomes a normal
  Markdown link whose host is inherited from the current document. Docx/Wiki
  citations are the only ones rewritten; other `<cite>` types are preserved, and
  examples inside code blocks or inline code are never touched.
- Bold markers that lost their separating space are repaired
  (`**结论：**正文` becomes `**结论：** 正文`), while fenced code, indented code,
  and inline code are left untouched.
- Remote image URLs are rewritten to relative local paths under `assets/`.

`download_docx_tree.py` supports only `docx` and `wiki` URLs; legacy docs,
sheets, Base, attachments, and whiteboards are ignored. The whole-space export
(`download_wiki_space.py`) is the script that does handle those other wiki node
types - see "Wiki space export output" below.

## Image download strategy

- Plain `http(s)` image URLs are fetched directly.
- `https://<host>/file/<token>` URLs are Feishu media pages, not public image
  links: the media token is downloaded with
  `lark-cli docs +media-download --as <identity>`, and if that returns 403 the
  preview endpoint is tried.
- If both fail, the original remote URL is kept in the Markdown and the reason is
  written to `image_failures`. Temporary authorisation query strings are stripped
  from the manifest.

Typical failure reasons: `permission_denied`, `html_response`, `http_error`,
`media_download_failed`, `unsupported_content_type`, `filesystem_error`.

## `_download-manifest.json`

| Field | Meaning |
| --- | --- |
| `root_url`, `root_type`, `recursive` | What was requested |
| `output_dir`, `generated_at` | Where and when |
| `complete` | True only when no document or image failure occurred and `--max-docs` was not hit. Title fallbacks and empty documents do **not** clear it |
| `downloaded_count`, `failed_count` | Document totals |
| `image_downloaded_count`, `image_failed_count` | Image totals |
| `title_fallback_count` | Documents whose file name came from the fallback chain |
| `title_failed_count` | Backward-compatible alias of `title_fallback_count` |
| `empty_count` | Documents whose exported body was blank |
| `limited`, `pending_count` | Whether `--max-docs` stopped the run and how many URLs stayed queued |
| `documents[]` | Per document: `token`, `type`, `url`, `title`, `file`, `status` (`"ok"` or `"empty"`), `empty` (bool), `title_fallback` (bool), `depth`, `discovered_from`, `document_id`, `revision_id`, `links`, `images` |
| `failures[]` | Failed documents with `url`, `depth`, `discovered_from`, `error` |
| `image_failures[]` | Failed images with `reason`, `error`, and owning document |
| `title_fallbacks[]` | Documents that used a fallback name, with `token`, `type`, `url`, `depth`, `discovered_from`, `error`, `source` (`"wiki-node-get"` when the wiki node metadata supplied the title, `"drive-inspect"` when `drive +inspect` did, `"token"` when it fell all the way back) |
| `title_failures[]` | Backward-compatible alias of `title_fallbacks` |
| `empty_documents[]` | Empty documents with `token`, `type`, `url`, `title`, `file`, `depth`, `discovered_from` |

`title_fallbacks` entries and `empty_documents` entries are warnings, not
failures: they are also printed to stderr as `[title-fallback]` / `[empty]`.

The summary line on stdout is:

```text
summary: output=<dir> downloaded=N failed=N images=N image_failed=N title_fallback=N empty=N recursive=False limited=False
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every discovered document and image was downloaded (title fallbacks and empty documents included) |
| `1` | Partial failure, or `--max-docs` stopped the run early: read the manifest |
| `2` | Invalid URL, invalid argument, or output directory read/write error |

When a document fails, only the documents discovered before the failure are
known. Descendants reachable solely through the failed document never enter the
queue, so exit code `1` must never be reported as a complete archive.

## Wiki space export output

`scripts/download_wiki_space.py` mirrors the wiki node tree instead of a single
document. Everything lands in `<output-dir>/<space name>/`:

```text
<output-dir>/<space name>/
├── _INDEX.md          classification tree, status badges, local relative links
├── _failures.md       failed / degraded / empty / unsupported / skipped, by reason
├── _manifest.json     machine-readable, one entry per node
├── _manifest.csv      UTF-8 with BOM, opens cleanly in Excel
├── state.json         resume checkpoints; only written with --resume
├── 财务管理制度/
│   ├── _分类页.md                the container node's own content
│   ├── 博泰逾期应收款管理制度.docx
│   ├── 收入确认财经要素V1.0.md
│   ├── assets/收入确认财经要素V1.0/image-001.png
│   └── PT IT-A0.01.OPR IT运维类制度/
└── 网络安全管理制度.md
```

Naming and layout rules:

- Illegal characters (`/ \ : * ? " < > |`) and control characters become `_`;
  leading/trailing spaces and dots are stripped; the stem is capped at 90
  characters.
- An empty title becomes `未命名-<node_token[:8]>`.
- A duplicate name inside the same directory gets `--<node_token[:10]>`.
- A container node's own body is written to `<dir>/_分类页.md`, so `<dir>/` and
  `<dir>.md` never appear side by side.
- `_INDEX.md` links use the CommonMark angle-bracket form `[标题](<路径>)`,
  because real file names contain spaces and half-width `)`.
- `_manifest.csv` is UTF-8 **with BOM**; without it Excel garbles Chinese.
- `--resume` reuses `state.json` and skips nodes already recorded as `ok`,
  `empty`, `unsupported`, or `skipped`; `failed` and `partial` nodes are retried.
  Checkpoints are only written while `--resume` is active, so a run that must be
  resumable has to start with `--resume`. When `--resume` finds no usable
  `state.json` (missing, or written with different options) it says so explicitly
  instead of silently reprocessing everything; the reverse case (a checkpoint
  exists but `--resume` was omitted) is flagged too.
- `--workers` defaults to 4. The Feishu API rate-limits higher concurrency and it
  shows up as intermittent, non-reproducible node failures.

Per node type:

| `obj_type` | Method | Result |
| --- | --- | --- |
| `docx` | `docs-fetch-markdown` | Markdown plus local images, reusing `download_tree()` with `group_assets=True` |
| `doc` (legacy) | `legacy-raw-content` | Plain text from `GET /open-apis/doc/v2/<obj_token>/raw_content`, written as `<name>.md` with an explicit warning block that all formatting is lost |
| `sheet` | `sheets-csv` | One CSV per visible sub-sheet from `data.annotated_csv`; hidden sub-sheets are skipped. One sub-sheet is `<name>.csv`; several are `<name>__<sheet name>.csv`, where the sheet name comes from the real `sheet_name` field of `sheets +workbook-info` (`title` does not exist in that payload), and the `sheet_id -> sheet_name -> file` mapping is kept in `nodes[].sheets` |
| `file` | `drive-download`, `preview-source_file`, `preview-<type>` | The attachment; original first, preview fallback |
| anything else | `none` | Recorded as `unsupported` in `_failures.md`, never silently skipped |

Attachment status semantics:

| Status | Meaning |
| --- | --- |
| `ok` | The original was obtained (`drive +download`, or a `source_file` preview saved under the real name) |
| `partial` | A preview was saved instead of the original; `fallback` is `preview-<type>` and `preview_type` names it |
| `failed` | The original was refused **and** no preview candidate exists |
| `skipped` | `--attachments skip` |

`drive +download` rejects absolute `--output` paths, so the exporter runs it with
the working directory set to the target folder and passes `./<name>`.

## `_manifest.json` (space export)

| Field | Meaning |
| --- | --- |
| `version` | Exporter version, `1.2.1` |
| `space_id`, `space_name`, `root_node_token` | What was exported |
| `output_dir`, `generated_at` | Where and when |
| `flat`, `attachments`, `identity` | The effective options |
| `complete` | True when no node failed and the walk was not truncated |
| `total_nodes` | Number of walked nodes |
| `file_count`, `total_size_bytes` | **Archive totals measured on disk**: node products + images + attachments. The exporter's own bookkeeping (`_INDEX.md`, `_failures.md`, `_manifest.*`, `state.json`) is excluded so the numbers do not depend on when they were measured |
| `disk_file_count`, `disk_size_bytes` | Same values as `file_count` / `total_size_bytes`, spelled out explicitly |
| `node_file_count`, `node_size_bytes` | Only the nodes' own products (the pre-1.2.1 meaning of `file_count` / `total_size_bytes`, before images were counted) |
| `asset_count`, `asset_size_bytes` | Images and other files under `assets/<token>/` |
| `counts` | Per status: `ok`, `partial`, `empty`, `failed`, `unsupported`, `skipped` |
| `limited` | `--max-nodes` stopped the walk early |
| `issues` | Walk-level problems (unreadable subtrees, the cap being hit) |
| `nodes[]` | One entry per node, see below |

Every `nodes[]` entry has `node_token`, `obj_token`, `obj_type`, `title`,
`path` (array of directory names), `depth`, `is_container`, `status`, `method`,
`format` (`markdown` / `text` / `csv` / the attachment suffix / the preview type),
`local_file`, `size_bytes`, `asset_count`, `asset_size_bytes`, `images`, `detail`,
`url`. A previewed attachment adds `fallback` and `preview_type`; a multi-sheet
workbook adds `files` (every written CSV path, with `local_file` pointing at the
first) and `sheets` (`[{sheet_id, sheet_name, file}]`).

`_manifest.csv` carries the same data with one row per node, including the
`format`, `asset_count`, `asset_size_bytes` and `sheets` columns.

Exit codes: `0` when no node failed, `1` when at least one node failed or
`--max-nodes` stopped the walk early, `2` for invalid arguments or when nothing
walkable was found.
