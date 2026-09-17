#!/usr/bin/env python3

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
    normalize_document_citations,
    parse_document_url,
    safe_filename,
)


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
            "RootToken": {
                "title": "Root",
                "content": "https://tenant.feishu.cn/wiki/ChildToken\n",
            },
            "ChildToken": {"title": "Child", "content": "# Child\n"},
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
            "RootToken": {
                "title": "Root",
                "content": '<cite doc-id="ChildToken" file-type="wiki" type="doc"></cite>\n',
            },
            "ChildToken": {
                "title": "Child",
                "content": "https://tenant.feishu.cn/docx/RootToken\n",
            },
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


if __name__ == "__main__":
    unittest.main()
