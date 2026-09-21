#!/usr/bin/env python3
"""Unit tests for `download_wiki_space.py` (whole-wiki-space export).

Everything is hermetic: no lark-cli process is started and no real network or
user file is touched. The module-level seams (`cli_json`, `run_lark_cli`,
`download_tree`) are replaced with fakes shaped like real lark-cli envelopes.

Note on walk order: `walk_space()` discovers the tree breadth-first (so a parent is
always known before its children) and then returns the nodes **depth-first**, so a
subtree appears contiguously in `_INDEX.md`. The tests below pin that order.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from download_wiki_space import (
    Context,
    StateStore,
    WikiNode,
    assign_layout,
    base_entry,
    export_attachment,
    export_legacy_doc,
    export_sheet,
    list_children,
    parse_args,
    process_node,
    run,
    safe_stem,
    walk_space,
    write_failures,
    write_index,
    write_manifest,
)


class _Process:
    """Minimal stand-in for `subprocess.CompletedProcess[str]`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_args(output_dir, **overrides):
    overrides.setdefault("space_id", "7000000000000000001")
    overrides.setdefault("space_name", "测试空间")
    argv = ["--output-dir", str(output_dir)]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is False or value is None:
            continue
        else:
            argv.extend([flag, str(value)])
    return parse_args(argv)


def make_context(output_dir, **overrides) -> Context:
    return Context(make_args(output_dir, **overrides), Path(output_dir))


def make_node(
    node_token: str,
    title: str,
    *,
    obj_type: str = "docx",
    obj_token: str | None = None,
    has_child: bool = False,
    depth: int = 0,
    parent: WikiNode | None = None,
) -> WikiNode:
    return WikiNode(
        node_token=node_token,
        obj_token=obj_token or f"obj-{node_token}",
        obj_type=obj_type,
        title=title,
        has_child=has_child,
        parent_node_token=parent.node_token if parent is not None else None,
        depth=depth,
        parent=parent,
    )


DENIED_ENVELOPE = json.dumps(
    {
        "ok": False,
        "error": {
            "type": "api",
            "subtype": "permission_denied",
            "message": "does not have export permission",
        },
    }
)


class DownloadWikiSpaceTests(unittest.TestCase):
    # -- tree walking ------------------------------------------------------
    def test_walk_wiki_space_mirrors_tree(self) -> None:
        raw = {
            None: [
                {
                    "node_token": "root1",
                    "obj_token": "obj-root1",
                    "obj_type": "docx",
                    "title": "财务管理制度",
                    "has_child": True,
                },
                {
                    "node_token": "root2",
                    "obj_token": "obj-root2",
                    "obj_type": "docx",
                    "title": "网络安全管理制度",
                    "has_child": False,
                },
            ],
            "root1": [
                {
                    "node_token": "child1",
                    "obj_token": "obj-child1",
                    "obj_type": "docx",
                    "title": "收入确认财经要素V1.0",
                    "has_child": False,
                    "parent_node_token": "root1",
                }
            ],
        }

        def fake_list_children(parent_node_token, *, ctx):
            return list(raw.get(parent_node_token, [])), None

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            with patch(
                "download_wiki_space.list_children", side_effect=fake_list_children
            ) as fake:
                nodes, issues = walk_space(ctx)

            # Depth-first flat list: root1, its child, then root2.
            self.assertEqual(
                [node.node_token for node in nodes], ["root1", "child1", "root2"]
            )
            self.assertEqual([node.depth for node in nodes], [0, 1, 0])
            self.assertIsNone(nodes[0].parent)
            self.assertIs(nodes[1].parent, nodes[0])
            self.assertIsNone(nodes[2].parent)
            self.assertEqual(nodes[1].parent_node_token, "root1")
            self.assertEqual([node.node_token for node in nodes[0].children], ["child1"])
            self.assertTrue(nodes[0].has_child)
            self.assertEqual(issues, [])
            self.assertEqual(fake.call_count, 2)

            assign_layout(nodes, ctx=ctx)
            by_token = {node.node_token: node for node in nodes}

            self.assertEqual(
                by_token["root1"].rel_file.as_posix(), "财务管理制度/_分类页.md"
            )
            self.assertEqual(by_token["root1"].child_dir.as_posix(), "财务管理制度")
            self.assertTrue(by_token["root1"].is_container)
            self.assertEqual(
                by_token["child1"].rel_file.as_posix(),
                "财务管理制度/收入确认财经要素V1.0.md",
            )
            self.assertEqual(by_token["child1"].child_dir.as_posix(), "财务管理制度")
            self.assertFalse(by_token["child1"].is_container)
            self.assertEqual(
                by_token["root2"].rel_file.as_posix(), "网络安全管理制度.md"
            )

    def test_list_children_parses_node_envelope(self) -> None:
        envelope = {
            "ok": True,
            "data": {
                "nodes": [
                    {"node_token": "n1", "title": "子节点"},
                    "not-a-dict",
                ]
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            with patch(
                "download_wiki_space.cli_json", return_value=(envelope, None)
            ) as fake:
                nodes, error = list_children("wikcnParent", ctx=ctx)

            self.assertEqual(nodes, [{"node_token": "n1", "title": "子节点"}])
            self.assertIsNone(error)
            arguments = fake.call_args.args[0]
            self.assertIn("+node-list", arguments)
            self.assertEqual(
                arguments[arguments.index("--parent-node-token") + 1], "wikcnParent"
            )

            with patch("download_wiki_space.cli_json", return_value=(envelope, None)) as fake:
                list_children(None, ctx=ctx)
            self.assertNotIn("--parent-node-token", fake.call_args.args[0])

    # -- layout: unique names ---------------------------------------------
    def test_duplicate_titles_get_token_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            first = make_node("wikcnAAAA0000000001", "同名文档")
            second = make_node("wikcnBBBB0000000002", "同名文档")
            assign_layout([first, second], ctx=ctx)

            self.assertEqual(first.rel_file.as_posix(), "同名文档.md")
            self.assertEqual(second.rel_file.as_posix(), "同名文档--wikcnBBBB0.md")
            self.assertIn(second.node_token[:10], second.display_name)
            self.assertNotEqual(first.rel_file, second.rel_file)

            # Neither file overwrites the other.
            (ctx.output_dir / first.rel_file).write_text("first", encoding="utf-8")
            (ctx.output_dir / second.rel_file).write_text("second", encoding="utf-8")
            self.assertEqual(
                (ctx.output_dir / first.rel_file).read_text(encoding="utf-8"), "first"
            )
            self.assertEqual(
                (ctx.output_dir / second.rel_file).read_text(encoding="utf-8"), "second"
            )

    def test_empty_title_fallback_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            for token in ("wikcnC3D4E5F6", "wikcnABCDEFGH"):
                node = make_node(token, "   ", obj_type="docx")
                assign_layout([node], ctx=ctx)
                expected = f"未命名-{token[:8]}"
                self.assertEqual(node.display_name, expected)
                self.assertEqual(node.rel_file.as_posix(), f"{expected}.md")
                self.assertEqual(safe_stem("", fallback=expected), expected)

    def test_safe_stem_handles_reserved_and_illegal_names(self) -> None:
        self.assertEqual(safe_stem("CON", fallback="x"), "_CON")
        self.assertEqual(
            safe_stem('A/B:C*D?E"F<G>H|I', fallback="x"), "A_B_C_D_E_F_G_H_I"
        )
        self.assertEqual(safe_stem("   ", fallback="未命名-abcd"), "未命名-abcd")

    # -- attachments -------------------------------------------------------
    def test_attachment_preview_fallback(self) -> None:
        calls: list[list[str]] = []

        def fake_run(arguments, *, lark_cli, timeout, cwd=None):
            calls.append(arguments)
            if arguments[:2] == ["drive", "+download"]:
                return _Process(1, "", DENIED_ENVELOPE)
            if arguments[:2] == ["drive", "+preview"] and "--list-only" in arguments:
                return _Process(
                    0,
                    json.dumps({"ok": True, "data": {"candidates": [{"type": "pdf"}]}}),
                    "",
                )
            if arguments[:2] == ["drive", "+preview"]:
                name = arguments[arguments.index("--output") + 1].removeprefix("./")
                Path(cwd, name).write_bytes(b"%PDF-1.4 fake preview")
                return _Process(0, "", "")
            raise AssertionError(f"unexpected lark-cli call: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnFile00000001", "年度报告.pdf", obj_type="file", obj_token="objf1"
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.run_lark_cli", side_effect=fake_run):
                entry = export_attachment(node, ctx)

            self.assertEqual(entry["status"], "partial")
            self.assertEqual(entry["method"], "preview-pdf")
            self.assertEqual(entry["fallback"], "preview-pdf")
            self.assertEqual(entry["preview_type"], "pdf")
            self.assertTrue(
                entry["local_file"].endswith("__preview.pdf"), entry["local_file"]
            )
            self.assertTrue((ctx.output_dir / entry["local_file"]).is_file())
            self.assertIn("does not have export permission", entry["detail"])
            self.assertFalse((ctx.output_dir / "年度报告.pdf").exists())

        preview_call = next(
            call for call in calls if call[:2] == ["drive", "+preview"] and "--type" in call
        )
        self.assertEqual(preview_call[preview_call.index("--type") + 1], "pdf")

    def test_attachment_no_preview_is_failed(self) -> None:
        def fake_run(arguments, *, lark_cli, timeout, cwd=None):
            if arguments[:2] == ["drive", "+download"]:
                return _Process(1, "", DENIED_ENVELOPE)
            if arguments[:2] == ["drive", "+preview"] and "--list-only" in arguments:
                return _Process(0, json.dumps({"ok": True, "data": {"candidates": []}}), "")
            raise AssertionError(f"unexpected lark-cli call: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnFile00000002", "合同终稿.docx", obj_type="file", obj_token="objf2"
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.run_lark_cli", side_effect=fake_run):
                entry = export_attachment(node, ctx)

            self.assertEqual(entry["status"], "failed")
            self.assertIsNone(entry["local_file"])
            self.assertIn("does not have export permission", entry["detail"])
            self.assertFalse((ctx.output_dir / "合同终稿.docx").exists())

    def test_attachment_skip_mode_does_not_call_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary, attachments="skip")
            node = make_node(
                "wikcnFile00000003", "备份.zip", obj_type="file", obj_token="objf3"
            )
            assign_layout([node], ctx=ctx)
            with patch(
                "download_wiki_space.run_lark_cli",
                side_effect=AssertionError("skip mode must not invoke lark-cli"),
            ):
                entry = export_attachment(node, ctx)

            self.assertEqual(entry["status"], "skipped")
            self.assertEqual(entry["method"], "drive-download")
            self.assertIsNone(entry["local_file"])

    # -- legacy doc --------------------------------------------------------
    def test_legacy_doc_raw_content(self) -> None:
        envelope = {
            "ok": True,
            "data": {"content": "私车公用管理办法\n第一章 总则\n"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnLegacy000001",
                "私车公用管理办法",
                obj_type="doc",
                obj_token="doccnLegacy000001",
            )
            assign_layout([node], ctx=ctx)
            with patch(
                "download_wiki_space.cli_json", return_value=(envelope, None)
            ) as fake:
                entry = export_legacy_doc(node, ctx)

            written = (ctx.output_dir / entry["local_file"]).read_text(encoding="utf-8")

        self.assertEqual(entry["method"], "legacy-raw-content")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["format"], "text")
        self.assertIn("私车公用管理办法\n第一章 总则", written)
        # The format-loss warning must be present: raw content is plain text only.
        self.assertIn("已丢失", written)
        self.assertIn("raw_content", written)
        self.assertIn("纯文本", written)
        arguments = fake.call_args.args[0]
        self.assertIn("/open-apis/doc/v2/doccnLegacy000001/raw_content", arguments)

    # -- sheets ------------------------------------------------------------
    def test_sheet_reads_annotated_csv(self) -> None:
        workbook = {
            "ok": True,
            "data": {
                "sheets": [
                    {"sheet_id": "sheet1", "title": "可见表", "is_hidden": False},
                    {"sheet_id": "sheet2", "title": "隐藏表", "is_hidden": True},
                ]
            },
        }
        csv_envelope = {
            "ok": True,
            "data": {"annotated_csv": "a,b\n1,2\n", "csv": "decoy,text\n9,9\n"},
        }
        seen: list[list[str]] = []
        csv_calls: list[list[str]] = []

        def fake_cli_json(arguments, *, ctx, retries=None):
            seen.append(arguments)
            if "+workbook-info" in arguments:
                return workbook, None
            if "+csv-get" in arguments:
                csv_calls.append(arguments)
                return csv_envelope, None
            raise AssertionError(f"unexpected cli_json call: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnSheet000001",
                "数据汇总",
                obj_type="sheet",
                obj_token="shtcnSheet000001",
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.cli_json", side_effect=fake_cli_json):
                entry = export_sheet(node, ctx)

            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["method"], "sheets-csv")
            written = (ctx.output_dir / entry["local_file"]).read_text(encoding="utf-8")

        # `annotated_csv` is the real field; the `csv` decoy must be ignored.
        self.assertEqual(written, "a,b\n1,2\n")
        self.assertNotIn("decoy", written)
        # The hidden sub-sheet is skipped: exactly one csv-get, for sheet1.
        self.assertEqual(len(csv_calls), 1)
        self.assertEqual(
            csv_calls[0][csv_calls[0].index("--sheet-id") + 1], "sheet1"
        )
        self.assertNotIn("sheet2", json.dumps(csv_calls))
        self.assertEqual(entry["files"], [entry["local_file"]])

    # -- unsupported types -------------------------------------------------
    def test_unsupported_type_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnBase000001",
                "多维表格",
                obj_type="bitable",
                obj_token="bascnBase000001",
            )
            assign_layout([node], ctx=ctx)
            entry = process_node(node, ctx)

            self.assertEqual(entry["status"], "unsupported")
            self.assertEqual(entry["method"], "none")
            self.assertIn("bitable", entry["detail"])

            write_failures(ctx, [node], {node.node_token: entry})
            failures = (ctx.output_dir / "_failures.md").read_text(encoding="utf-8")

        self.assertIn("多维表格", failures)
        self.assertIn("https://feishu.cn/wiki/wikcnBase000001", failures)
        self.assertIn("不支持的类型", failures)

    def test_type_filter_marks_node_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary, types="docx")
            node = make_node(
                "wikcnSheet000002", "表格", obj_type="sheet", obj_token="shtcn2"
            )
            assign_layout([node], ctx=ctx)
            entry = process_node(node, ctx)

        self.assertEqual(entry["status"], "skipped")
        self.assertEqual(entry["method"], "type-filter")
        self.assertIn("sheet", entry["detail"])

    def test_parse_args_rejects_unknown_types(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--space-id", "S1", "--types", "docx,nonsense"])

    # -- index rendering ---------------------------------------------------
    def test_index_links_use_angle_brackets(self) -> None:
        titles = [
            "博泰车联网职级体系管理办法（试行).pdf",
            "季度 报告 (草稿).pdf",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            nodes = [
                make_node(f"wikcnFile000{i:04d}", title, obj_type="file")
                for i, title in enumerate(titles, 10)
            ]
            assign_layout(nodes, ctx=ctx)
            entries: dict[str, dict[str, object]] = {}
            for node in nodes:
                entry = base_entry(node, ctx)
                entry["status"] = "partial"
                entry["method"] = "preview-pdf"
                entry["local_file"] = node.rel_file.as_posix()
                entry["detail"] = "原件无下载权限"
                entries[node.node_token] = entry
            write_index(ctx, nodes, entries)
            index = (ctx.output_dir / "_INDEX.md").read_text(encoding="utf-8")

        for title in titles:
            self.assertIn("](<", index)
            self.assertIn(f"](<{title}>)", index)
            # The un-bracketed form would break on the half-width ")".
            self.assertNotIn(f"]({title})", index)
        # sanity: both paths really contain the risky characters
        self.assertIn(" ", titles[1])
        self.assertIn(")", titles[0])

    # -- resume and flat layout -------------------------------------------
    def test_resume_skips_completed_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_parent = Path(temporary)
            args = make_args(output_parent, space_id="S1", space_name="SpaceX", resume=True)
            ctx = Context(args, output_parent)
            space_dir = output_parent / "SpaceX"
            space_dir.mkdir(parents=True, exist_ok=True)

            signature = {
                "space_id": ctx.space_id,
                "root_node_token": ctx.root_node_token,
                "flat": ctx.flat,
                "attachments": ctx.attachments,
                "types": sorted(ctx.types),
            }
            completed_entry = {
                "node_token": "wikcnDone0000001",
                "obj_token": "obj-done",
                "obj_type": "docx",
                "title": "已完成文档",
                "path": ["已完成文档"],
                "depth": 0,
                "is_container": False,
                "status": "ok",
                "method": "docs-fetch-markdown",
                "local_file": "已完成文档.md",
                "size_bytes": 12,
                "images": 0,
                "detail": "来自上次运行",
                "url": "https://feishu.cn/wiki/wikcnDone0000001",
            }
            (space_dir / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "signature": signature,
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "nodes": {"wikcnDone0000001": completed_entry},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            raw = {
                None: [
                    {
                        "node_token": "wikcnDone0000001",
                        "obj_token": "obj-done",
                        "obj_type": "docx",
                        "title": "已完成文档",
                        "has_child": False,
                    },
                    {
                        "node_token": "wikcnTodo0000001",
                        "obj_token": "obj-todo",
                        "obj_type": "docx",
                        "title": "待处理文档",
                        "has_child": False,
                    },
                ]
            }
            fresh_entry = {
                "node_token": "wikcnTodo0000001",
                "obj_token": "obj-todo",
                "obj_type": "docx",
                "title": "待处理文档",
                "path": ["待处理文档"],
                "depth": 0,
                "is_container": False,
                "status": "ok",
                "method": "docs-fetch-markdown",
                "local_file": "待处理文档.md",
                "size_bytes": 7,
                "images": 0,
                "detail": "本次运行",
                "url": "https://feishu.cn/wiki/wikcnTodo0000001",
            }

            with patch(
                "download_wiki_space.list_children",
                side_effect=lambda parent, *, ctx: (list(raw.get(parent, [])), None),
            ), patch(
                "download_wiki_space.process_node", return_value=fresh_entry
            ) as fake_process:
                exit_code = run(args)

            self.assertEqual(fake_process.call_count, 1)
            self.assertEqual(
                fake_process.call_args.args[0].node_token, "wikcnTodo0000001"
            )
            manifest = json.loads(
                (space_dir / "_manifest.json").read_text(encoding="utf-8")
            )
            rendered = json.dumps(manifest, ensure_ascii=False)
            self.assertIn("来自上次运行", rendered)
            self.assertEqual(manifest["counts"]["ok"], 2)
            self.assertEqual(manifest["counts"]["failed"], 0)
            self.assertEqual(exit_code, 0)

    def test_flat_mode_has_no_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary, flat=True)
            container = make_node("wikcnContainer01", "制度目录", has_child=True)
            leaf = make_node(
                "wikcnLeaf000001",
                "制度正文",
                has_child=False,
                depth=1,
                parent=container,
            )
            container.children.append(leaf)
            nodes = [container, leaf]
            assign_layout(nodes, ctx=ctx)

            for node in nodes:
                self.assertEqual(node.child_dir, Path("."))
                self.assertEqual(node.rel_file.parent, Path("."))
            self.assertEqual(container.rel_file.as_posix(), "制度目录.md")
            self.assertEqual(leaf.rel_file.as_posix(), "制度正文.md")
            self.assertTrue(container.is_container)

            entries: dict[str, dict[str, object]] = {}
            for node in nodes:
                entry = base_entry(node, ctx)
                entry["local_file"] = node.rel_file.as_posix()
                entries[node.node_token] = entry
            write_index(ctx, nodes, entries)
            index = (ctx.output_dir / "_INDEX.md").read_text(encoding="utf-8")
            # Only _INDEX.md was produced at the root: no per-node subdirectories.
            produced = sorted(
                path.relative_to(temporary).as_posix()
                for path in Path(temporary).rglob("*")
            )

        self.assertIn("](<制度目录.md>)", index)
        self.assertIn("](<制度正文.md>)", index)
        self.assertEqual(produced, ["_INDEX.md"])

    # -- regression tests for reported defects -----------------------------
    def test_duplicate_sheet_titles_get_distinct_names(self) -> None:
        """Two visible sub-sheets with the same title must not overwrite each other."""
        workbook = {
            "ok": True,
            "data": {
                "sheets": [
                    {"sheet_id": "sheetAAAAAA1", "title": "Sheet1"},
                    {"sheet_id": "sheetBBBBBB2", "title": "Sheet1"},
                ]
            },
        }

        def fake_cli_json(arguments, *, ctx, retries=None):
            if "+workbook-info" in arguments:
                return workbook, None
            if "+csv-get" in arguments:
                sheet_id = arguments[arguments.index("--sheet-id") + 1]
                return {"ok": True, "data": {"annotated_csv": f"{sheet_id}\n"}}, None
            raise AssertionError(f"unexpected cli_json call: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnSheet000002", "数据汇总", obj_type="sheet", obj_token="shtcn1"
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.cli_json", side_effect=fake_cli_json):
                entry = export_sheet(node, ctx)
            files = sorted(entry["files"])
            contents = {
                name: (ctx.output_dir / name).read_text(encoding="utf-8") for name in files
            }

        self.assertEqual(entry["status"], "ok")
        self.assertEqual(len(files), 2, files)
        self.assertEqual(len(set(files)), 2, "duplicate sheet names overwrote each other")
        self.assertEqual(sorted(contents.values()), ["sheetAAAAAA1\n", "sheetBBBBBB2\n"])

    def test_attachment_download_without_file_is_failed(self) -> None:
        """lark-cli exiting 0 without writing the file must not be reported as ok."""

        class Result:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnFile0000001",
                "没有落盘.pdf",
                obj_type="file",
                obj_token="boxcnMissing",
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.run_lark_cli", return_value=Result()):
                entry = export_attachment(node, ctx)
            exists = (ctx.output_dir / node.rel_file).is_file()

        self.assertFalse(exists)
        self.assertEqual(entry["status"], "failed")
        self.assertIn("未生成文件", entry["detail"])
        self.assertIsNone(entry["local_file"])

    # -- v1.2.1 regressions ------------------------------------------------
    def test_multi_sheet_uses_sheet_name(self) -> None:
        """Real `+workbook-info` payloads carry `sheet_name`, not `title`."""
        workbook = {
            "ok": True,
            "data": {
                "sheets": [
                    {"sheet_id": "0hLOFe", "sheet_name": "华住企业会员", "is_hidden": False},
                    {"sheet_id": "1dWNpS", "sheet_name": "上海", "is_hidden": False},
                ]
            },
        }

        def fake_cli_json(arguments, *, ctx, retries=None):
            if "+workbook-info" in arguments:
                return workbook, None
            if "+csv-get" in arguments:
                sheet_id = arguments[arguments.index("--sheet-id") + 1]
                return {"ok": True, "data": {"annotated_csv": f"{sheet_id}\n"}}, None
            raise AssertionError(f"unexpected cli_json call: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            node = make_node(
                "wikcnSheet000003", "公司协议酒店", obj_type="sheet", obj_token="shtcn2"
            )
            assign_layout([node], ctx=ctx)
            with patch("download_wiki_space.cli_json", side_effect=fake_cli_json):
                entry = export_sheet(node, ctx)
            names = sorted(Path(name).name for name in entry["files"])
            contents = {
                Path(name).name: (ctx.output_dir / name).read_text(encoding="utf-8")
                for name in entry["files"]
            }

        self.assertEqual(
            names, ["公司协议酒店__上海.csv", "公司协议酒店__华住企业会员.csv"]
        )
        # no unreadable sheet_id may leak into a file name
        self.assertNotIn("0hLOFe", " ".join(names))
        self.assertEqual(contents["公司协议酒店__上海.csv"], "1dWNpS\n")
        self.assertEqual(
            [record["sheet_name"] for record in entry["sheets"]],
            ["华住企业会员", "上海"],
        )
        self.assertEqual(entry["sheets"][0]["sheet_id"], "0hLOFe")
        self.assertEqual(entry["sheets"][0]["file"], entry["files"][0])
        self.assertEqual(entry["format"], "csv")

    def test_manifest_totals_include_asset_files(self) -> None:
        """Reported file count and size must cover images, not just node products."""
        node = make_node("wikcnDocx000004", "带图文档", obj_type="docx")
        with tempfile.TemporaryDirectory() as temporary:
            ctx = make_context(temporary)
            assign_layout([node], ctx=ctx)
            assets = ctx.output_dir / "assets" / node.node_token
            assets.mkdir(parents=True)
            (assets / "image-001.png").write_bytes(b"x" * 2048)
            markdown = ctx.output_dir / "带图文档.md"
            markdown.write_text("# 带图文档\n", encoding="utf-8")

            entry = base_entry(node, ctx)
            entry.update(
                {
                    "status": "ok",
                    "method": "docs-fetch-markdown",
                    "format": "markdown",
                    "local_file": "带图文档.md",
                    "size_bytes": markdown.stat().st_size,
                    "asset_count": 1,
                    "asset_size_bytes": 2048,
                }
            )
            entries = {node.node_token: entry}
            manifest = write_manifest(ctx, [node], entries, issues=[], limited=False)
            write_index(ctx, [node], entries)
            index_text = (ctx.output_dir / "_INDEX.md").read_text(encoding="utf-8")
            markdown_size = markdown.stat().st_size

        self.assertEqual(manifest["node_file_count"], 1)
        self.assertEqual(manifest["node_size_bytes"], markdown_size)
        self.assertEqual(manifest["asset_count"], 1)
        self.assertEqual(manifest["asset_size_bytes"], 2048)
        # archive totals cover the Markdown and the image; the exporter's own
        # bookkeeping files are excluded on purpose
        self.assertEqual(manifest["disk_file_count"], 2)
        self.assertEqual(manifest["total_size_bytes"], markdown_size + 2048)
        self.assertEqual(manifest["file_count"], manifest["disk_file_count"])
        self.assertGreaterEqual(manifest["total_size_bytes"], manifest["asset_size_bytes"])
        self.assertIn("图片等资源 1", index_text)

    def test_state_store_reports_missing_checkpoint(self) -> None:
        """`--resume` must be able to tell "no checkpoint" from "nothing to skip"."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            signature = {"space_id": "s1", "flat": False}

            fresh = StateStore(path, enabled=True, signature=signature)
            self.assertFalse(fresh.loaded)
            fresh.record({"node_token": "n1", "status": "ok"})
            self.assertTrue(path.is_file())

            reloaded = StateStore(path, enabled=True, signature=signature)
            self.assertTrue(reloaded.loaded)
            self.assertEqual(reloaded.completed, {"n1"})

            mismatched = StateStore(path, enabled=True, signature={"space_id": "s2"})
            self.assertFalse(mismatched.loaded)
            self.assertEqual(mismatched.completed, set())

            disabled = StateStore(path, enabled=False, signature=signature)
            self.assertFalse(disabled.loaded)


if __name__ == "__main__":
    unittest.main()
