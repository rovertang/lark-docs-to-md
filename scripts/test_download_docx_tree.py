#!/usr/bin/env python3
"""Unit tests for the single-document / recursive downloader.

The fakes below deliberately mirror the real `lark-cli docs +fetch --detail simple`
response, which contains only `content`, `document_id` and `revision_id`. The real
response has **no** `title` field: the document title arrives inside the leading
`<title>` element of the exported Markdown. Earlier fakes invented a `title` key,
which is why the untitled-document defect (a fallback name reported as a failure)
was invisible to the test suite.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch_download import load_url_list
from download_docx_tree import (
    DocRef,
    download_tree,
    extract_child_document_refs,
    fetch_wiki_node_title,
    main,
    normalize_document_citations,
    parse_document_url,
    safe_filename,
)


def document(content: str, name: str = "X") -> dict[str, object]:
    """Build a document payload shaped like a real `docs +fetch` response."""
    return {
        "content": content,
        "document_id": f"doxcn-{name}",
        "revision_id": f"rev-{name}",
    }


class DownloadDocxTreeTests(unittest.TestCase):
    def test_parses_docx_and_wiki_urls(self) -> None:
        self.assertEqual(
            parse_document_url("https://tenant.feishu.cn/docx/DocxToken?from=link#part"),
            ("docx", "DocxToken", "https://tenant.feishu.cn/docx/DocxToken"),
        )
        self.assertEqual(
            parse_document_url("https://tenant.feishu.cn/wiki/WikiToken"),
            ("wiki", "WikiToken", "https://tenant.feishu.cn/wiki/WikiToken"),
        )

    def test_safe_filename_handles_windows_characters(self) -> None:
        self.assertEqual(safe_filename("A: title / with * chars?"), "A_ title _ with _ chars_.md")

    def test_normalizes_docx_and_wiki_citations(self) -> None:
        markdown = (
            '<cite doc-id="ChildDocx" file-type="docx" title="Child" type="doc"></cite>\n'
            '<cite doc-id="ChildWiki" file-type="wiki" type="doc">Wiki child</cite>'
        )
        normalized = normalize_document_citations(
            markdown, "https://tenant.feishu.cn/wiki/RootWiki"
        )
        self.assertEqual(
            normalized,
            "[Child](https://tenant.feishu.cn/docx/ChildDocx)\n"
            "[Wiki child](https://tenant.feishu.cn/wiki/ChildWiki)",
        )
        self.assertEqual(
            extract_child_document_refs(markdown, "https://tenant.feishu.cn/wiki/RootWiki"),
            [
                ("docx", "ChildDocx", "https://tenant.feishu.cn/docx/ChildDocx"),
                ("wiki", "ChildWiki", "https://tenant.feishu.cn/wiki/ChildWiki"),
            ],
        )

    def test_downloads_only_root_without_recursive_flag(self) -> None:
        documents = {
            "RootToken": document(
                "<title>Root</title>\n\nhttps://tenant.feishu.cn/wiki/ChildToken\n", "root"
            ),
            "ChildToken": document("<title>Child</title>\n\n# Child\n", "child"),
        }

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch):
                manifest = download_tree(
                    "https://tenant.feishu.cn/docx/RootToken",
                    Path(temporary),
                    retries=0,
                )

        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["recursive"])
        self.assertEqual(manifest["downloaded_count"], 1)
        self.assertEqual(manifest["documents"][0]["links"], [])

    def test_downloads_recursive_graph_once(self) -> None:
        documents = {
            "RootToken": document(
                '<title>Root</title>\n\n<cite doc-id="ChildToken" file-type="wiki" type="doc"></cite>\n',
                "root",
            ),
            "ChildToken": document(
                "<title>Child</title>\n\nhttps://tenant.feishu.cn/docx/RootToken\n", "child"
            ),
        }

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            output_parent = Path(temporary)
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch):
                manifest = download_tree(
                    "https://tenant.feishu.cn/docx/RootToken",
                    output_parent,
                    retries=0,
                    recursive=True,
                )
            saved = json.loads(
                (output_parent / "RootToken" / "_download-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["downloaded_count"], 2)
        self.assertEqual([item["type"] for item in saved["documents"]], ["docx", "wiki"])

    def test_load_url_list_skips_comments_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            urls_file = Path(temporary) / "urls.txt"
            urls_file.write_text(
                "# archive list\n"
                "https://tenant.feishu.cn/docx/First\n"
                "https://tenant.feishu.cn/docx/First\n"
                "https://tenant.feishu.cn/wiki/Second\n"
                "invalid\n",
                encoding="utf-8",
            )
            url_list = load_url_list(urls_file)

        self.assertEqual([entry.token for entry in url_list.entries], ["First", "Second"])
        self.assertEqual(url_list.duplicate_lines, [3])
        self.assertEqual(len(url_list.invalid), 1)

    # -- title handling ---------------------------------------------------
    def test_title_comes_from_content_title_element(self) -> None:
        documents = {"RootToken": document("<title>财务管理制度</title>\n\n正文\n", "root")}

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch):
                manifest = download_tree(
                    "https://tenant.feishu.cn/wiki/RootToken", Path(temporary), retries=0
                )
            files = sorted(path.name for path in Path(temporary).rglob("*.md"))

        entry = manifest["documents"][0]
        self.assertEqual(entry["title"], "财务管理制度")
        self.assertFalse(entry["title_fallback"])
        self.assertEqual(manifest["title_fallbacks"], [])
        self.assertEqual(manifest["title_fallback_count"], 0)
        self.assertEqual(files, ["财务管理制度.md"])

    def test_untitled_document_is_not_a_failure(self) -> None:
        """A fallback name must never turn a successful download into exit code 1."""
        documents = {"RootToken": document("只有正文，没有 title 元素\n", "root")}

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch), patch(
                "download_docx_tree.fetch_wiki_node_title",
                return_value=(None, "wiki node has no title"),
            ), patch(
                "download_docx_tree.fetch_drive_document_title",
                return_value=(None, "drive +inspect has no title"),
            ):
                exit_code = main(
                    [
                        "https://tenant.feishu.cn/wiki/RootToken",
                        "-o",
                        temporary,
                        "--retries",
                        "0",
                    ]
                )
                manifest = json.loads(
                    (Path(temporary) / "RootToken" / "_download-manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
            files = sorted(path.name for path in Path(temporary).rglob("*.md"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["downloaded_count"], 1)
        self.assertEqual(manifest["failed_count"], 0)
        self.assertEqual(manifest["title_fallback_count"], 1)
        self.assertEqual(manifest["title_fallbacks"][0]["source"], "token")
        # fallback file name is the readable placeholder, shared with the space exporter
        self.assertEqual(files, ["未命名-RootToke.md"])
        self.assertEqual(manifest["documents"][0]["title"], "未命名-RootToke")
        # `title_failures` stays as a backward-compatible alias of `title_fallbacks`
        self.assertEqual(manifest["title_failures"], manifest["title_fallbacks"])

    def test_title_fallback_uses_node_get(self) -> None:
        documents = {"RootToken": document("正文没有 title 元素\n", "root")}

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch), patch(
                "download_docx_tree.fetch_wiki_node_title",
                return_value=("来自节点元数据的标题", None),
            ) as node_get:
                manifest = download_tree(
                    "https://tenant.feishu.cn/wiki/RootToken", Path(temporary), retries=0
                )
            files = sorted(path.name for path in Path(temporary).rglob("*.md"))

        node_get.assert_called_once()
        self.assertEqual(files, ["来自节点元数据的标题.md"])
        self.assertEqual(manifest["documents"][0]["title"], "来自节点元数据的标题")
        self.assertTrue(manifest["documents"][0]["title_fallback"])
        self.assertEqual(manifest["title_fallback_count"], 1)

    def test_title_fallback_records_depth(self) -> None:
        documents = {
            "RootToken": document(
                '<title>根文档</title>\n\n<cite doc-id="ChildToken" file-type="wiki" type="doc"></cite>\n',
                "root",
            ),
            "ChildToken": document("子文档没有 title 元素\n", "child"),
        }

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch), patch(
                "download_docx_tree.fetch_wiki_node_title",
                return_value=(None, "wiki node has no title"),
            ), patch(
                "download_docx_tree.fetch_drive_document_title",
                return_value=(None, "drive +inspect has no title"),
            ):
                manifest = download_tree(
                    "https://tenant.feishu.cn/wiki/RootToken",
                    Path(temporary),
                    retries=0,
                    recursive=True,
                )

        self.assertEqual(manifest["title_fallback_count"], 1)
        fallback = manifest["title_fallbacks"][0]
        self.assertEqual(fallback["token"], "ChildToken")
        self.assertEqual(fallback["depth"], 1)
        self.assertEqual(fallback["discovered_from"], "RootToken")
        self.assertEqual(fallback["type"], "wiki")
        self.assertEqual(fallback["url"], "https://tenant.feishu.cn/wiki/ChildToken")

    def test_title_fallback_uses_drive_inspect(self) -> None:
        """A docx URL has no wiki node: `drive +inspect` supplies the title."""
        documents = {"RootToken": document("正文没有 title 元素\n", "root")}

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch), patch(
                "download_docx_tree.fetch_drive_document_title",
                return_value=("工程技术：在智能体优先的世界中利用 Codex", None),
            ) as inspect:
                manifest = download_tree(
                    "https://tenant.feishu.cn/docx/RootToken", Path(temporary), retries=0
                )
            files = sorted(path.name for path in Path(temporary).rglob("*.md"))

        inspect.assert_called_once()
        self.assertEqual(files, ["工程技术：在智能体优先的世界中利用 Codex.md"])
        self.assertEqual(manifest["title_fallbacks"][0]["source"], "drive-inspect")
        self.assertIsNone(manifest["title_fallbacks"][0]["error"])

    def test_empty_document_counted(self) -> None:
        documents = {"RootToken": document("", "root")}

        def fake_fetch(ref: DocRef, **_: object) -> dict[str, object]:
            return documents[ref.token]

        with tempfile.TemporaryDirectory() as temporary:
            with patch("download_docx_tree.fetch_document", side_effect=fake_fetch), patch(
                "download_docx_tree.fetch_wiki_node_title",
                return_value=("空文档", None),
            ):
                manifest = download_tree(
                    "https://tenant.feishu.cn/wiki/RootToken", Path(temporary), retries=0
                )
            written = (Path(temporary) / "RootToken" / "空文档.md").read_text(encoding="utf-8")

        self.assertEqual(written, "")
        self.assertEqual(manifest["empty_count"], 1)
        self.assertEqual(manifest["empty_documents"][0]["token"], "RootToken")
        self.assertEqual(manifest["empty_documents"][0]["title"], "空文档")
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(manifest["documents"][0]["status"], "empty")
        self.assertTrue(manifest["complete"])

    # -- wiki +node-get ---------------------------------------------------
    def test_wiki_node_get_reads_data_title(self) -> None:
        class Result:
            returncode = 0
            stdout = 'Fetching wiki node...\n{"ok": true, "data": {"title": "节点标题"}}'
            stderr = ""

        with patch("download_docx_tree.run_lark_cli", return_value=Result()):
            title, error = fetch_wiki_node_title(
                "wikcnToken", lark_cli="lark-cli", identity="user", timeout=10
            )

        self.assertEqual(title, "节点标题")
        self.assertIsNone(error)

    def test_wiki_node_get_failure_is_reported(self) -> None:
        class Result:
            returncode = 1
            stdout = ""
            stderr = json.dumps(
                {"ok": False, "error": {"type": "api", "subtype": "unknown", "message": "boom"}}
            )

        with patch("download_docx_tree.run_lark_cli", return_value=Result()):
            title, error = fetch_wiki_node_title(
                "wikcnToken", lark_cli="lark-cli", identity="user", timeout=10
            )

        self.assertIsNone(title)
        self.assertIn("boom", error or "")


if __name__ == "__main__":
    unittest.main()
