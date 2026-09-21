# 更新日志

本项目的所有重要变更都记录在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

暂无。

## [1.2.0] - 2026-09-21

### 修复

- **标题回退不再算作失败**：`download_docx_tree.py` 的 `complete` 改为
  `not failures and not image_failures and not limited`。文档标题读不出来时文件照常写出，
  退出码仍然是 `0`，只把回退记进 manifest 并打印到 stderr。
- **标题回退链修正**：优先用导出 Markdown 里的 `<title>` 元素，其次用
  `lark-cli wiki +node-get --node-token <token>` 的节点元数据（wiki 文档），
  再用 `lark-cli drive +inspect --url <url>` 读取文档标题（docx 等类型，**只取元数据**），
  最后才退回裸 token。回退文件名不再带 `docx-`/`wiki-` 类型前缀，统一是 `<token>.md`——类型本来就已经记在 manifest 里。
  `title_fallbacks[].source` 会写明标题来自 `wiki-node-get`、`drive-inspect` 还是 `token`。
- **删除无效的二次整篇导出**：旧的"为了找标题把整篇文档再导出一次 XML"调用被移除，
  换成只取元数据的 `drive +inspect`；死代码 `title_from_document()` 一并删除，
  休眠的 `document["title"]` 分支现在有注释说明其存在原因。
  > 实测修正：改进文档 D4 断言"XML 重抓完全无效"。这对 **wiki** 文档成立，但对 **docx** 不成立——
  > `docx/X7nndBYZRoC3MJxQKd7c7B71nhh` 的 markdown 导出没有 `<title>`，标题只能靠第二次调用取得。
  > 因此没有直接删掉了事，而是替换成零下载量的元数据接口，标题质量不退化。
- **空文档不再算作失败**：正文为空白（`content.strip() == ""`）的文档记为 `empty`，
  进入 `empty_documents`，在 `documents[]` 里 `status: "empty"`，但它不是失败，退出码保持 `0`。

### 新增

- **`scripts/download_wiki_space.py`（整库导出）**：按 wiki 节点树遍历并镜像整个知识库，
  输出 `_INDEX.md`（分类层级 + 状态徽标 + 本地相对链接）、`_failures.md`（失败/降级/空/不支持/已跳过）、
  `_manifest.json`、`_manifest.csv`（UTF-8 带 BOM，Excel 直接打开）、`state.json`（仅 `--resume`）。
  - `docx` 节点复用 `download_tree()`（`group_assets=True`，每个文档独立 `assets/<token>/`），
    不存在第二份下载实现。
  - 旧版 `doc` 节点走 `GET /open-apis/doc/v2/<obj_token>/raw_content` 纯文本导出，
    文件顶部写入显式警告块（加粗、表格、图片、编号层级全部丢失），manifest 方法为 `legacy-raw-content`。
  - `sheet` 节点按可见子表各导出一个 CSV，取自 `data.annotated_csv`，隐藏子表跳过；
    单子表为 `<名字>.csv`，多子表为 `<名字>__<子表名>.csv`。
  - `file` 附件节点先取原件，被拒时回退 `drive +preview`；`source_file` 命中按真名保存为 `ok`，
    其他预览存成 `<名字>__preview.pdf` 并把状态记为 `partial`、`fallback: "preview-<type>"`。
  - `bitable`/`mindnote`/`slides`/`whiteboard` 及未知类型统一记为 `unsupported` 写进 `_failures.md`，
    不再被静默跳过。
  - 名称清洗：非法字符与不可见控制字符替换为 `_`，去掉首尾空格和点，长度上限 90 字符；
    空标题变成 `未命名-<node_token[:8]>`；同目录重名追加 `--<node_token[:10]>`；
    容器节点自身正文写进 `<目录>/_分类页.md`，避免 `<目录>/` 与 `<目录>.md` 同时出现。
- **单文档 manifest 新字段**：`title_fallbacks`、`title_fallback_count`、`empty_documents`、
  `empty_count`；`documents[]` 每条新增 `status`（`ok`|`empty`）、`empty`、`title_fallback`。
- **新的进度日志前缀**：`[title-fallback]`（取代 `[title-failed]`）与 `[empty]`；
  汇总行改为
  `summary: output=... downloaded=N failed=N images=N image_failed=N title_fallback=N empty=N recursive=False limited=False`。
- **可复用的模块级函数**：`run_lark_cli()`、`cli_error_details()`、`is_permission_error()`、
  `fetch_wiki_node_title()`、`fallback_document_title()`，以及 `download_tree()` 的新关键字
  `group_assets`（默认取 `recursive` 的值），整库导出正是靠它保留每个文档独立的 `assets/<token>/`。
- **`check_env.py --login` 默认权限域扩展**：默认申请 `docs,wiki,drive,sheets`
  （整库导出需要 wiki 节点读、drive 下载和 sheets 读）。仍然保持三步设备码流程
  （`--login`、`--login --no-wait`、`--device-code`），设备码约 10 分钟过期。
- **`VERSION` 文件**：单行 `1.2.0`，`check_env.py --version` 和报告里的版本号都读它。

### 文档

- `README.md`：新增版本/更新日志指引，更新"目录结构""参数速查""依赖一览""输出与退出码"四处表格，
  补充整库导出用法与警告语义，并改写"参考脚本的一致性核对"。
- `SKILL.md`：`metadata` 块内新增 `version: "1.2.0"`；`## Run` 补充整库导出；
  `## Report` 补充新的退出码/警告契约与新的 manifest 字段。
- `references/output-format.md`：补充单文档 manifest 新字段、空文档/标题回退语义，
  以及整库导出的目录布局与 manifest schema。
- `references/troubleshooting.md`：补充旧版文档 `missing_scope`、
  `drive +download` 的 `--output must be a relative path within the current directory`、
  高 `--workers` 导致间歇失败、`partial`（预览 PDF）与 `--resume` 行为。
- `references/agent-install.md`：说明可选的 `metadata.version` 字段是安全的（位于规范允许的
  `metadata` map 内），但 camelCase 调用键仍然必须避免。

### 兼容性

- 所有既有命令行参数保持不变，`download_docx_tree.py` 的退出码契约也不变：
  `0` 发现的文档与图片全部下载成功，`1` 部分失败或 `--max-docs` 提前停止，
  `2` URL/参数非法或输出目录不可写。
- 既有 manifest 字段全部保留；`title_failures` 与 `title_failed_count` 继续作为
  `title_fallbacks` / `title_fallback_count` 的向后兼容别名存在。
- 标题回退和空文档不再影响退出码，只写进 manifest 并打印到 stderr；
  依赖旧行为把标题回退当失败来判断"未完整"的调用方，需要改读 `title_fallbacks` / `empty_documents`。

[未发布]: http://192.168.3.88:10082/rovertang/lark-docs-to-md/commits/main
[1.2.0]: http://192.168.3.88:10082/rovertang/lark-docs-to-md/releases/tag/v1.2.0
