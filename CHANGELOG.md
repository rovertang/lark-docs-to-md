# 更新日志

本项目的所有重要变更都记录在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

暂无。

## [1.2.1] - 2026-09-21

修复 v1.2.0 验收测试报告（`公司管理制度` 279 节点全量回归，ok=172 / partial=103 / empty=2 / failed=2）
提出的 5 个问题。回归基线与独立实现逐位吻合，本版只修缺陷，不改变既有能力边界。

### 修复

- **B1（中高）多子表工作簿丢失子表名**：真实的 `lark-cli sheets +workbook-info` 返回字段是
  **`sheet_name`**，没有 `title`；旧代码先读 `title`，于是多子表全部落到不可读的 `sheet_id`，
  导出成 `公司协议酒店__0hLOFe.csv` 这类文件，子表名到文件的映射彻底丢失。
  现在按 `sheet_name` → `sheetName` → `title` → `sheet_id` 取值，并新增
  `sheets: [{sheet_id, sheet_name, file}]` 记录，即使将来再出现命名碰撞也能追溯；
  `detail` 里也会列出子表名。（单子表工作簿走 `final` 分支，所以旧自测没暴露该问题。）
- **B2（中）`_INDEX.md` 与 summary 的文件数/体积少算图片**：旧实现只统计「有产物的节点数」和
  节点自身产物的字节数，docx 节点下载的图片（`assets/<token>/`）完全没计入——
  279 节点归档实测 277 个 / 51.3 MB，而磁盘实况是 424 个 / 72.3 MB，看起来小了 29%，
  容易被误判成"图片没下下来"。
  现在 `file_count` / `total_size_bytes` 表示**磁盘实况**，并另外给出
  `disk_file_count` / `disk_size_bytes` / `node_file_count` / `node_size_bytes` /
  `asset_count` / `asset_size_bytes`，每个 docx 节点还有自己的 `asset_count` / `asset_size_bytes`；
  `_INDEX.md` 与 summary 会写成「节点产物 N + 图片等资源 M」两段式。
  本工具自己的台账文件（`_INDEX.md` / `_failures.md` / `_manifest.*` / `state.json`）不计入，
  这样数字不依赖测量时机。
- **B3（低）`--resume` 在没有 `state.json` 时静默不生效**：失败后加 `--resume` 重跑会全量重下。
  现在 `--resume` 未找到可用检查点时会明确提示
  「未找到可用的 state.json（不存在或与本轮参数不匹配）：本次不会跳过任何节点。检查点只有在带 `--resume` 运行时才会写入」；
  反向情况（存在 `state.json` 但没加 `--resume`）也会提示「本次将重新处理所有节点」。
- **B4（低）两个入口的无标题回退命名不一致**：`download_docx_tree.py` 用 `<token>.md`，
  整库导出用 `未命名-<token[:8]>.md`。现统一为 **`未命名-<token[:8]>`**（人可读），
  两个入口对同一篇无标题文档产出同名文件。
- **B5（提示）旧版 doc 的 `format` 字段为 `null`**：现在每条清单都带 `format`
  （`markdown` / `text` / `csv` / 附件后缀 / 预览类型），`_manifest.csv` 也新增 `format` 列。

### 文档

- README、`SKILL.md`、`references/output-format.md`、`references/troubleshooting.md`
  同步修订：回退命名、子表命名、文件与体积口径、`--resume` 提示、新增 manifest 字段与 CSV 列。

### 兼容性

- 既有 CLI 参数、退出码契约、`title_failures` / `title_failed_count` 别名均未破坏。
- 两处**行为变更**（均为修缺陷，已在此说明）：
  1. 无标题文档的回退文件名由 `<token>.md` 变为 `未命名-<token[:8]>.md`；
  2. `_manifest.json` 的 `file_count` / `total_size_bytes` 从"节点产物"改为"磁盘实况（含图片）"，
     原口径可通过 `node_file_count` / `node_size_bytes` 取回。

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
[1.2.1]: http://192.168.3.88:10082/rovertang/lark-docs-to-md/releases/tag/v1.2.1
[1.2.0]: http://192.168.3.88:10082/rovertang/lark-docs-to-md/releases/tag/v1.2.0
