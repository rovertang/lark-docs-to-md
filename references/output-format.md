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
- Markdown filenames use the real title from Feishu's XML export, not the first
  heading in the body. If the title cannot be read, the file falls back to
  `<type>-<token>.md` and the reason is recorded in `title_failures`; a later
  successful run deletes that fallback file.
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

Only `docx` and `wiki` documents are supported. Legacy docs, sheets, Base,
attachments, and whiteboards are ignored.

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
| `complete` | True only when no document, image, or title failure occurred and `--max-docs` was not hit |
| `downloaded_count`, `failed_count` | Document totals |
| `image_downloaded_count`, `image_failed_count` | Image totals |
| `title_failed_count` | Documents that fell back to `<type>-<token>.md` |
| `limited`, `pending_count` | Whether `--max-docs` stopped the run and how many URLs stayed queued |
| `documents[]` | Per document: `token`, `type`, `url`, `title`, `file`, `depth`, `discovered_from`, `document_id`, `revision_id`, `links`, `images` |
| `failures[]` | Failed documents with `url`, `depth`, `discovered_from`, `error` |
| `image_failures[]` | Failed images with `reason`, `error`, and owning document |
| `title_failures[]` | Documents whose title could not be read, with `error` |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every discovered document and image was downloaded |
| `1` | Partial failure, or `--max-docs` stopped the run early: read the manifest |
| `2` | Invalid URL, invalid argument, or output directory read/write error |

When a document fails, only the documents discovered before the failure are
known. Descendants reachable solely through the failed document never enter the
queue, so exit code `1` must never be reported as a complete archive.
