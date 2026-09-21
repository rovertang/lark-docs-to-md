# lark-docs-to-md

**版本 1.2.1**（见 [`VERSION`](VERSION) 与 [`CHANGELOG.md`](CHANGELOG.md)）。

把飞书 / Lark 的 **Docx**、**Wiki** 文档批量导出成本地 **Markdown**（含图片），并用同一个内核提供三种用法：

| 用法 | 入口 | 适合谁 |
| --- | --- | --- |
| 命令行脚本 | `scripts/download_docx_tree.py`、`scripts/batch_download.py`、`scripts/download_wiki_space.py` | 想写进定时任务、CI、自己拼命令的人 |
| 本地 Web 服务 | `web/lark_download_web.py`（单文件、零依赖） | 想打开浏览器、粘贴链接、点按钮下载的人 |
| Agent Skill | `SKILL.md`（本仓库根目录就是 skill 目录） | Codex / Claude Code / DeepSeek Harness / OpenClaw / Hermes 等 Agent |

三种用法共用同一套脚本：Web 服务不会另写一份下载逻辑，它用 `subprocess` 调用 `scripts/download_docx_tree.py`，并直接复用其中的 URL 解析、文件名清洗和 manifest 读取函数；整库导出脚本 `scripts/download_wiki_space.py` 同样复用 `download_tree()` 与 `run_lark_cli()`。**仓库里只有一份下载实现，不存在版本分叉。**

---

## 目录结构

```text
lark-docs-to-md/                 # 仓库根目录 == skill 目录（folder 名必须等于 SKILL.md 的 name）
├── SKILL.md                     # Agent Skill 定义（可移植 frontmatter）
├── README.md                    # 本文件（人读；对 Agent 发现 skill 无影响）
├── CHANGELOG.md                 # 版本变更记录（Keep a Changelog 风格）
├── VERSION                      # 单行版本号，check_env.py --version 读它
├── agents/openai.yaml           # Codex 专用的 UI 元数据（可选，缺失也不影响运行）
├── references/                  # 按需加载的参考文档（Agent 渐进披露）
│   ├── agent-install.md         #   各 Agent 的 skill 目录与安装方式
│   ├── troubleshooting.md       #   登录/权限/图片/Windows/WSL 排错
│   └── output-format.md         #   Markdown 转换规则与 manifest 字段
├── scripts/
│   ├── download_docx_tree.py    # 单文档 / 递归下载（核心；被其他脚本复用）
│   ├── download_wiki_space.py   # 整库导出：遍历 wiki 节点树并镜像目录结构（新增）
│   ├── batch_download.py        # URL 列表批量下载
│   ├── check_env.py             # 环境自检 + 登录引导（新增）
│   ├── install_skill.py         # 一键安装到各 Agent 的 skill 目录（新增）
│   ├── document_urls.txt        # batch_download.py 的默认 URL 列表
│   └── test_download_docx_tree.py
└── web/
    └── lark_download_web.py     # 单文件标准库 Web 服务（新增）
```

> **为什么仓库根目录就是 skill 目录？**
> 这样 clone 下来即可安装，`SKILL.md` 里的 `scripts/...` 相对路径永远有效，也不必在仓库里放两份脚本副本。代价是 skill 目录里多了一个 `README.md`——它对 skill 发现没有任何影响（各 Agent 只读 `SKILL.md` 及其引用文件）。若某个工具确实排斥多余文件，用 `python3 scripts/install_skill.py --copy` 可生成只含 `SKILL.md`、`agents/`、`scripts/`、`references/`、`web/` 的精简副本。

---

## 快速开始（3 步）

### 1. 检查环境（先做这一步）

```bash
python3 scripts/check_env.py
```

它按顺序检查 Python 版本、`lark-cli`、登录状态、输出目录可写性，并给出**可直接复制的修复命令**。退出码即为结论：

- `0` 就绪，可以下载
- `1` 环境没问题，但需要**登录授权**
- `2` 缺前置依赖（Python < 3.10 或没有 `lark-cli`）

给 Agent 用时加 `--json`，得到结构化结果：

```bash
python3 scripts/check_env.py --json --output-dir ./downloads
```

### 2. 授权登录（用户形态）

```bash
python3 scripts/check_env.py --login          # 打印链接 + 二维码，然后等待浏览器授权
```

首次使用需要 Device Flow 授权。脚本把整个流程包好了，你不需要读 `lark-cli` 文档：

| 场景 | 命令 |
| --- | --- |
| 本地交互式（推荐） | `python3 scripts/check_env.py --login` |
| Agent 场景，需要先结束本轮再等用户 | `python3 scripts/check_env.py --login --no-wait` |
| 用户确认已授权后收尾 | `python3 scripts/check_env.py --device-code <device_code>` |

说明与注意点：

- 默认申请 `docs,wiki,drive,sheets` 四个域：`docs` 覆盖 Docx 导出，`wiki` 覆盖知识库节点树遍历，`drive` 覆盖附件下载/预览，`sheets` 覆盖电子表格 CSV 导出。整库导出（`download_wiki_space.py`）四个都要用到；确有需要再用 `--domain` 追加或收窄。
- Token 由 `lark-cli` 保存，本项目的任何脚本**都不读取、不打印、不落盘** token。
- Device code 约 **10 分钟**过期；过期就重新跑一次 `--login` 拿新链接，不需要重装任何东西。
- 用户 token 的 refresh token 也会过期（本机实测遇到过 `missing (refresh token expired)`），此时同样只需重新 `--login`。
- 需要机器人身份时加 `--identity bot`，但只有在该机器人对所有目标文档都有权限时才有意义；**用户自己的文档请用 `user`**。
- 旧版 `doc` 节点的纯文本接口还需要一个默认域集合之外的 scope。缺少时脚本会直接打印修复命令：`当前登录缺少该接口所需 scope，重新授权即可：python3 scripts/check_env.py --login --domain docs,wiki,drive,sheets`。这类失败只影响旧版文档，其余类型照常导出。

### 3. 下载

```bash
# 单个文档（默认不跟随正文里引用的其他文档，最安全）
python3 scripts/download_docx_tree.py "https://xxx.feishu.cn/docx/<token>" -o ./downloads

# 递归：连正文中引用/链接的 Docx、Wiki 子文档一起下载
python3 scripts/download_docx_tree.py "https://xxx.feishu.cn/docx/<token>" -o ./downloads -r --max-docs 50

# 多个独立链接（每行一个 URL，支持 # 注释和空行）
python3 scripts/batch_download.py -f ./urls.txt -o ./downloads

# 整个知识库（wiki 空间）：遍历节点树，镜像分类目录并导出所有节点类型
python3 scripts/download_wiki_space.py --space-id <SPACE_ID> -o ./downloads
python3 scripts/download_wiki_space.py --space-id <SPACE_ID> --node-token <wikcn...> -o ./downloads
python3 scripts/download_wiki_space.py --space-id <SPACE_ID> --dry-run          # 只看计划不下载
```

单文档/批量输出固定在 `<输出目录>/<根文档 token>/` 下：Markdown 用文档真实标题命名，图片进 `assets/`（递归模式下为 `assets/<token>/`），结果摘要写入 `_download-manifest.json`。

整库导出输出在 `<输出目录>/<空间名>/` 下，按 wiki 层级镜像目录，并额外生成 `_INDEX.md`（分类层级 + 状态徽标 + 本地相对链接）、`_failures.md`（失败/降级/空/不支持/已跳过）、`_manifest.json`、`_manifest.csv`（UTF-8 带 BOM，Excel 可直接打开）；加 `--resume` 时再写一份 `state.json`。细节见 [`references/output-format.md`](references/output-format.md)。

---

## Web 服务（浏览器里下载，支持批量与整库）

```bash
python3 web/lark_download_web.py                 # 默认 http://127.0.0.1:8765/
python3 web/lark_download_web.py --open --port 9000
python3 web/lark_download_web.py --output-dir ~/Downloads/lark
```

- **零依赖**：只用 Python 标准库 + 内嵌前端，`python3 web/lark_download_web.py` 起来就能用，不需要 `pip install`、不需要 Node 构建。
- **两种模式**（页面顶部切换）：
  - **文档链接**：粘贴多个链接（一行一个，也支持直接粘贴一个 `urls.txt` 的**路径**）、递归开关、输出目录、重试、超时、文档数上限。
  - **知识库空间**：填 `space_id`（可选填某个 `wikcn…` 节点只导子树）、附件策略 `original/preview/skip`、并发数、节点类型过滤、`--resume` / `--flat` 开关、最大节点数——等价于 `download_wiki_space.py` 的常用参数。
- 实时日志（含 `[title-fallback]` / `[empty]` / 节点级进度）、每个文档或空间的状态卡片（成功/降级/空/失败、图片数、标题回退数）、文件列表与文本预览（Markdown/CSV/HTML）、一键打包 ZIP。
- **登录面板**：页面顶部显示当前环境与登录状态，点「授权登录」直接给出授权链接和二维码，授权完成点「我已完成授权」即收尾，全程不用切终端。
- **安全**：默认只监听 `127.0.0.1`，因为服务会以当前登录用户的身份执行 `lark-cli`。绑定非回环地址必须显式加 `--allow-remote`，请只在可信网络下使用。
- 任务只存在内存里，服务重启即清空；文件预览/ZIP 做了目录逃逸校验，只允许访问该任务输出目录内的文件。

---

## 把仓库 URL 丢给 Agent 安装（重点）

你可以直接把仓库地址发给 Agent，让它自己装好、检查环境、引导你登录、然后自测。**下面这段可以直接整段粘贴给 Agent**：

```text
请把这个仓库安装为你的 skill 并完成初始化，然后自测：

1. 克隆仓库到你的 skill 目录（skill 目录名必须是 lark-docs-to-md，与 SKILL.md 的 name 一致）：
   git clone <仓库URL> <你的 skill 目录>/lark-docs-to-md
   常见位置：~/.codex/skills/、~/.claude/skills/、~/.dsh/skills/、~/.openclaw/skills/、
   ~/.hermes/skills/、~/.agents/skills/（一个共享位置供多个 Agent 复用）。
   也可以直接执行仓库里的：python3 scripts/install_skill.py --dry-run 先看计划，
   再执行 python3 scripts/install_skill.py --targets <你所在的工具>。
2. 读取 SKILL.md，按其中说明操作；不要自己重写下载逻辑。
3. 运行 python3 scripts/check_env.py --json。
   - 退出码 0：环境就绪，继续第 4 步。
   - 退出码 1：需要登录，执行 python3 scripts/check_env.py --login，
     把授权链接和二维码发给我，我完成后你再收尾（--device-code）。
   - 退出码 2：按输出的 fix 提示安装依赖（如 npm install -g @larksuite/cli）后重试。
4. 用下面 3 个文档做批量下载自测，输出目录用 ./downloads：
   https://rovertang.feishu.cn/docx/Qj58dcHFAoOcOVx5l7mcEK5Hnjb
   https://rovertang.feishu.cn/docx/Dwhudsgy8oKOUmx03AXcHiX8nvg
   https://rovertang.feishu.cn/docx/X7nndBYZRoC3MJxQKd7c7B71nhh
   再跑一次整库导出的计划态验证（不下载、不写文件）：
   python3 scripts/download_wiki_space.py --space-id <SPACE_ID> --dry-run --max-nodes 20
5. 报告：绝对输出目录、成功文档数、图片数、失败 URL 及原因、退出码。
   任何图片失败都视为未完整下载，不要声称"全部完成"。
   标题回退（`title_fallback`）和空文档（`empty`）只是警告，退出码仍为 0，
   不要把这两种情况报成失败。
```

Agent 端需要知道的约定（已写进 `SKILL.md` 与 `references/`）：

- **先自检再下载**：`check_env.py` 的退出码就是决策依据，不要靠猜。
- **不重写逻辑**：一律调用 `scripts/` 里的脚本；Web 服务同理，它是子进程调用同一个脚本；整库导出也复用 `download_tree()`。
- **不碰 token**：任何情况下都不打印、不复制、不写入 token；授权只走 `--login` / `--device-code`。
- **如实汇报**：退出码 `1` 或存在 `image_failures` 时必须说"未完整"，并给出失败 URL；详情读 `_download-manifest.json`（整库导出读 `_manifest.json` 与 `_failures.md`）。
- **分清失败与警告**：`title_fallback` / `empty` / 预览件 `partial` 不是下载失败，但要在报告里如实说明；`download_wiki_space.py` 的退出码独立为 `0`/`1`/`2`。
- **递归要克制**：只有用户明确要求下载子文档时才加 `--recursive`，并建议配 `--max-docs`。
- **整库导出同样要克制**：先 `--dry-run` 看规模，大空间加 `--max-nodes` 与 `--resume`，`--workers` 保持默认 4。

### 各 Agent 的安装位置

| Agent | 用户级 skill 目录 | 项目级 skill 目录 | 备注 |
| --- | --- | --- | --- |
| Codex CLI | `~/.codex/skills/`（始终支持）、`~/.agents/skills/`（较新版本） | `<repo>/.agents/skills/`、`<repo>/.codex/skills/` | 支持符号链接；只读 `name` + `description`；启动时 skill 列表占上下文约 2%，描述要短 |
| Claude Code | `~/.claude/skills/<name>/` | `<repo>/.claude/skills/` | `/命令名` 取自目录名；上传 claude.ai 时只接受规范里的 6 个字段，本 skill 未越界 |
| DeepSeek Harness | `~/.dsh/skills/`、`~/.agents/skills/` | `<project>/.dsh/skills/`、`<project>/.agents/skills/` | 只扫描**一层**目录；`name` 必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`；旧式 camelCase 字段会让 skill 静默失效 |
| OpenClaw | `~/.openclaw/skills/`、`~/.agents/skills/` | `<workspace>/skills/`、`<workspace>/.agents/skills/` | `openclaw skills install git:owner/repo`；node 托管模式要求目录名 == `name` |
| Hermes | `~/.hermes/skills/` | `<project>/.hermes/skills/`、`<project>/.agents/skills/` | `hermes skills install <url>`；项目级 skill 需要 `hermes skills trust` |

一键安装（默认**符号链接**，一处 clone 处处生效；`--copy` 生成独立副本）：

```bash
python3 scripts/install_skill.py --dry-run                          # 先看计划
python3 scripts/install_skill.py --targets agents,codex,claude      # 指定目标
python3 scripts/install_skill.py --targets all --copy               # 全部目标，复制模式
python3 scripts/install_skill.py --project /path/to/repo            # 装到某个项目的 .agents/skills
```

安装前后脚本都会校验 `SKILL.md`：目录名与 `name` 是否一致、`description` 是否带引号/超长/含尖括号、是否误用旧式字段、随包脚本是否存在——这些正是各 Agent **静默忽略整个 skill** 的常见原因。

写跨 Agent skill 的注意事项汇总在 [`references/agent-install.md`](references/agent-install.md)。

---

## 环境与初始化说明

### 依赖一览

| 依赖 | 版本 | 用途 | 安装 |
| --- | --- | --- | --- |
| Python | 3.10+ | 所有脚本与 Web 服务（仅标准库） | 系统包管理器 / python.org |
| Node.js | 18+ | 仅用于安装 `lark-cli` | nvm / fnm / 官方安装包 |
| lark-cli | 1.0.x | 实际调用飞书开放接口 | `npm install -g @larksuite/cli` |
| 飞书授权域 | `docs,wiki,drive,sheets` | `--login` 的默认申请集合：Docx 导出、wiki 节点树、附件下载、表格导出；旧版 `doc` 纯文本接口还需额外 scope | `python3 scripts/check_env.py --login` |

**没有 Python 第三方依赖**，不需要 `pip install`、不需要虚拟环境、不需要 `package.json`。

### 常见环境坑

- **`python` vs `python3`**：Linux/macOS 用 `python3`；Windows 若 `python` 打开应用商店，用 `py -3`。
- **找不到 `lark-cli`**：`check_env.py` 会提示 `npm install -g @larksuite/cli`。若用 fnm/nvm，注意 GUI 或 Agent 的 `PATH` 里可能没有 node 目录，可用 `--lark-cli /绝对路径/lark-cli` 或设置环境变量 `LARK_CLI`。
- **Windows + npm 包装脚本**：npm 生成的 `lark-cli.ps1` 需要 PowerShell Core 7.6+（`pwsh`）。没有就装 pwsh，或指向原生可执行文件，或在已登录的 WSL2 里跑。
- **WSL2**：Windows 主机没装 `lark-cli` 但 WSL 里已登录时，用 `/mnt/c/...` 作为输出路径在 WSL 内执行；不要让两套环境共用凭证。
- **输出目录**：默认是当前工作目录；`check_env.py --output-dir DIR` 可提前验证可写性（Web 服务同理，默认落在仓库的 `downloads/`，已加入 `.gitignore`）。
- **npm 全局安装慢或失败**：可换镜像（如 `npm config set registry https://registry.npmmirror.com`），安装后重新运行 `check_env.py`。

排错清单（含错误码 `3380004` / `99991679`、`html_response`、`media_download_failed` 等具体含义）见 [`references/troubleshooting.md`](references/troubleshooting.md)。

---

## 参数速查

`download_docx_tree.py`：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `URL` / `--url URL` | 必填 | 根文档 Docx 或 Wiki 链接，两种写法互斥 |
| `-o, --output-dir DIR` | 当前工作目录 | 下载父目录，内容落在 `DIR/<根token>/` |
| `-r, --recursive` | 关 | 递归下载正文中链接/引用的 Docx、Wiki 子文档 |
| `-i, --identity user\|bot` | `user` | 调用 `lark-cli` 的身份 |
| `--retries N` | `2` | 失败重试次数；认证/参数/权限错误不重试 |
| `--timeout SECONDS` | `120` | 单次请求超时 |
| `--max-docs N` | `0`（不限） | 递归模式下的文档数上限 |
| `--lark-cli PATH` | `lark-cli` | 指定可执行文件，亦可用环境变量 `LARK_CLI` |

`batch_download.py`：`-f/--urls-file`（默认脚本旁的 `document_urls.txt`）、`-o/--output-dir`，其余同上。它按列表顺序逐个调用 `download_docx_tree.py`，**不递归**，单个失败会继续，最后统一汇总。

`download_wiki_space.py`（整库导出）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--space-id ID` | 必填 | wiki 空间 ID，可用 `lark-cli wiki +space-list` 查看 |
| `--node-token TOKEN` | 空 | 只导出该节点子树（`wikcn...` 节点 token），不传则遍历整个空间 |
| `-o, --output-dir DIR` | 当前工作目录 | 父目录，内容落在 `DIR/<空间名>/` |
| `--space-name NAME` | 自动探测 | 覆盖探测到的空间名（决定输出目录名） |
| `--types a,b,c` | 全部已知类型 | 逗号分隔的节点类型白名单 |
| `--attachments original\|preview\|skip` | `original` | 附件策略：原件（被拒时回退预览）/ 只取预览 / 跳过 |
| `--workers N` | `4` | 并行节点数；飞书接口有频率限制，调大反而会间歇性失败（最小 1） |
| `--max-nodes N` | `0`（不限） | 遍历节点数上限，触发即视为提前停止 |
| `--resume` | 关 | 复用 `state.json`，跳过已完成（`ok`/`empty`/`unsupported`/`skipped`）节点，失败与降级节点重试 |
| `--flat` | 关 | 不镜像层级，全部放进一个目录 |
| `--dry-run` | 关 | 只遍历并打印计划，不下载 |
| `-i, --identity user\|bot` | `user` | 调用 `lark-cli` 的身份 |
| `--retries N` | `2` | 失败重试次数 |
| `--timeout SECONDS` | `120` | 单次请求超时 |
| `--doc-host HOST` | `feishu.cn` | 拼 wiki URL 时用的主机名 |
| `--lark-cli PATH` | `lark-cli` | 指定可执行文件，亦可用环境变量 `LARK_CLI` |

`check_env.py`：`--json`、`--login`、`--no-wait`、`--device-code`、`--domain`（默认 `docs,wiki,drive,sheets`）、`--identity`、`--output-dir`、`--lark-cli`、`--timeout`、`--version`。

`install_skill.py`：`--targets`、`--all`、`--copy`、`--project`、`--project-only`、`--name`、`--force`、`--dry-run`、`--json`。

Web 服务：`--host`、`--port`、`--output-dir`、`--lark-cli`、`--identity`、`--open`、`--allow-remote`。

---

## 输出与退出码

### 单文档 / 递归 / 批量（`download_docx_tree.py`、`batch_download.py`）

```text
downloads/<根文档token>/
├── <文档标题>.md
├── <子文档标题>.md              # 仅递归模式
├── <重名标题>--<token>.md       # 仅标题冲突时
├── assets/[<token>/]image-001.png
└── _download-manifest.json
```

Markdown 会被规范化：`<title>` 转 H1、`<callout>` 降级为带 emoji 的引用块、`<cite>` 文档引用转标准链接、加粗标记缺少空格时自动修复（代码块与行内代码不动）、远程图片改写为本地相对路径。细节见 [`references/output-format.md`](references/output-format.md)。

| 退出码 | 含义 |
| --- | --- |
| `0` | 发现的文档和图片全部下载成功（含标题回退与空文档，它们只是警告） |
| `1` | 部分失败，或触发 `--max-docs` 提前停止——**不要当作完整归档**，请读 manifest |
| `2` | URL/参数非法，或输出目录读写失败 |

**警告语义（1.2.0 修正，1.2.1 统一命名）**：标题读不出来不再是失败。文件名按 `<title>` 元素 → `wiki +node-get` 节点元数据（wiki 文档）→ `drive +inspect` 文档元数据（docx 等，**一次轻量元数据调用，不是整篇重导**）→ `未命名-<token[:8]>` 的顺序回退（该占位名与整库导出完全一致，见 1.2.1 的 B4），stderr 打印 `[title-fallback]` 并注明标题来源，manifest 记入 `title_fallbacks` / `title_fallback_count`（每条含 `source`：`wiki-node-get` / `drive-inspect` / `token`）。正文为空的文档同理：`[empty]` 打印、记入 `empty_documents` / `empty_count`、`documents[]` 里 `status: "empty"`，退出码仍为 `0`。汇总行形如：

```text
summary: output=... downloaded=N failed=N images=N image_failed=N title_fallback=N empty=N recursive=False limited=False
```

### 整库导出（`download_wiki_space.py`）

```text
<输出目录>/<空间名>/
├── _INDEX.md          分类层级 + 状态徽标 + 本地相对路径链接
├── _failures.md       失败 / 降级 / 空 / 不支持 / 已跳过，分组列出原因与飞书链接
├── _manifest.json     机器可读，逐节点一条
├── _manifest.csv      UTF-8 带 BOM，Excel 可直接打开
├── state.json         仅 --resume 时写，记录已完成节点
├── 财务管理制度/
│   ├── _分类页.md                容器节点自身的正文
│   ├── 博泰逾期应收款管理制度.docx
│   ├── 收入确认财经要素V1.0.md
│   ├── assets/收入确认财经要素V1.0/image-001.png
│   └── PT IT-A0.01.OPR IT运维类制度/
└── 网络安全管理制度.md
```

命名规则：非法字符 `/ \ : * ? " < > |` 与控制字符替换为 `_`，去掉首尾空格和点，长度上限 90 字符；空标题变成 `未命名-<node_token[:8]>`；同目录重名追加 `--<node_token[:10]>`；容器节点自身正文写进 `<目录>/_分类页.md`，因此 `<目录>/` 与 `<目录>.md` 不会同时出现。`_INDEX.md` 的链接使用 CommonMark 尖括号形式 `[标题](<路径>)`，因为真实文件名里含空格和半角 `)`；`_manifest.csv` 写成 UTF-8 **带 BOM**，避免 Excel 打开时中文乱码。

**子表命名（1.2.1 修 B1）**：`sheets +workbook-info` 的真实字段是 `sheet_name`（没有 `title`），
多子表工作簿导出为 `<文档名>__<子表名>.csv`，例如 `重要负债表__总览.csv`、`公司协议酒店__上海.csv`；
`nodes[].sheets` 保留 `sheet_id → sheet_name → file` 的完整映射，`_manifest.csv` 的 `sheets` 列也会列出子表名。
隐藏子表跳过；只有当同名子表真的冲突时才追加 `--<sheet_id[:10]>`。

**文件数与体积口径（1.2.1 修 B2）**：manifest 的 `file_count` / `total_size_bytes` 现在是**磁盘实况**，
含 docx 节点下载的图片（`assets/<token>/`）与附件原件，并按
`disk_file_count` / `node_file_count` / `asset_count` / `node_size_bytes` / `asset_size_bytes` 给出拆分；
每个 docx 节点还有自己的 `asset_count` / `asset_size_bytes`。
本工具自己的台账文件（`_INDEX.md`、`_failures.md`、`_manifest.*`、`state.json`）不计入，保证数字不随测量时机变化。
`_INDEX.md` 与 summary 会写成「节点产物 N + 图片等资源 M」两段式，避免把 51.3 MB 的归档误读成 72.3 MB 的缺失。

**断点续传（1.2.1 改进 B3）**：检查点只在带 `--resume` 运行时写入；若 `--resume` 找不到可用 `state.json`
（不存在或与本轮参数不匹配），会明确提示本次不会跳过任何节点——**失败重跑请从一开始就加 `--resume`**；
反之若存在 `state.json` 却没加 `--resume`，也会提示本次将重新处理全部节点。

各类型处理方式：`docx` → Markdown + 本地图片（复用 `download_tree()`，不存在第二份下载实现）；`doc`（旧版）→ `GET /open-apis/doc/v2/<obj_token>/raw_content` 纯文本，写成 `<名字>.md` 并在文件顶部显式警告全部格式（加粗、表格、图片、编号）已丢失，manifest 方法为 `legacy-raw-content`；`sheet` → 按可见子表各导出一个 CSV（取自 `data.annotated_csv`，隐藏子表跳过，单子表为 `<名字>.csv`，多子表为 `<名字>__<子表名>.csv`）；`file`（附件）→ `lark-cli drive +download` 取原件，被拒时回退 `drive +preview --list-only` 再按 `--type source_file|pdf|pdf_lin|html` 取预览，`source_file` 命中按真名保存为 `ok`，其他预览存成 `<名字>__preview.pdf` 并记为 `partial` / `fallback: "preview-<type>"`；`bitable`/`mindnote`/`slides`/`whiteboard` 与未知类型记为 `unsupported` 写进 `_failures.md`，不会被静默跳过。

附件状态语义：`ok` 取到原件，`partial` 用预览件代替原件，`failed` 原件被拒且没有任何预览候选，`skipped` 由 `--attachments skip` 主动跳过。`--attachments preview` 会跳过取原件的步骤直接走预览链。

| 退出码 | 含义 |
| --- | --- |
| `0` | 没有任何节点失败 |
| `1` | 至少一个节点失败，或 `--max-nodes` 提前停止遍历 |
| `2` | 参数非法，或一个可遍历的节点都没有 |

`_manifest.json` 顶层字段：`version`、`space_id`、`space_name`、`root_node_token`、`output_dir`、`generated_at`、`flat`、`attachments`、`identity`、`complete`、`total_nodes`、`file_count`、`total_size_bytes`、`counts`（`ok`/`partial`/`empty`/`failed`/`unsupported`/`skipped`）、`limited`、`issues`、`nodes[]`；`nodes[]` 每条含 `node_token`、`obj_token`、`obj_type`、`title`、`path`（数组）、`depth`、`is_container`、`status`、`method`、`local_file`、`size_bytes`、`images`、`detail`、`url`，预览附件另有 `fallback`/`preview_type`，多子表工作簿另有 `files`。`complete` 仅在无节点失败且遍历未被截断时为 `true`。

---

## 参考脚本的一致性核对

`tmp/` 里原有的两份参考材料（`lark-docx-batch-download/` 与 `20260717-飞书文档批量下载skill/lark-docx-batch-download/scripts/`）中的脚本**完全一致**，已用 md5 核对：

| 文件 | md5 | 结果 |
| --- | --- | --- |
| `batch_download.py` | `c2e1b3bc88261948e2f0079dfe8ccc3f` | 两份相同 |
| `download_docx_tree.py` | `8ae3a3140a0c1b1b89324a40fb7aca8c` | 两份相同 |

因此本仓库只保留**一份**权威副本放在 `scripts/`，不再保留第二份拷贝；skill 目录通过"仓库根目录即 skill 目录"的方式直接使用这份脚本，从机制上杜绝版本分叉。（`tmp/` 已由 `.gitignore` 忽略，仅作本地参考资料。）

关于这两份脚本与上方基线的当前关系：

- `scripts/batch_download.py` 仍然与参考材料**内容逐字节一致**：参考文件是 CRLF、仓库里由 `.gitattributes` 的 `* text=auto eol=lf` 统一为 LF，把参考文件去掉 CR 后的 md5 与工作区文件相同（`ac96f76de8eb5793f979e5d95838a85f`），差异只是换行符。
- `scripts/download_docx_tree.py` **不再与基线逐字节一致**：它现在是基线的**超集**，承载了 P5/1.2.0 的缺陷修复与新能力（标题回退链、空文档、新的 manifest 字段与可复用函数 `run_lark_cli()` / `cli_error_details()` / `is_permission_error()` / `fetch_wiki_node_title()` / `fallback_document_title()`、`download_tree()` 的 `group_assets` 参数），完整清单见 [`CHANGELOG.md`](CHANGELOG.md)。退出码契约保持向后兼容，`title_failures` / `title_failed_count` 仍作为别名存在。
- 新增的 `scripts/download_wiki_space.py` 没有对应的参考材料，它复用 `download_tree()` 与 `run_lark_cli()`，因此下载实现仍然只有一份。

---

## 开发与自测

```bash
python3 -m py_compile scripts/*.py web/lark_download_web.py   # 语法检查
python3 -m unittest discover -s scripts -p "test_*.py" -v     # 单元测试（含 test_download_wiki_space.py）
python3 scripts/check_env.py --output-dir /tmp/x              # 环境自检
python3 scripts/check_env.py --version                        # 读取 VERSION
python3 scripts/install_skill.py --dry-run                    # skill 校验 + 安装预演
python3 scripts/download_wiki_space.py --space-id X --dry-run # 整库导出计划态
python3 web/lark_download_web.py --port 8765                  # Web 服务
```

端到端实测（2026-09-18 / 1.2.0 复核 2026-09-21，均在用户身份授权后）：

- 单文档下载：`Qj58dcHFAoOcOVx5l7mcEK5Hnjb` → `20260809宁波旅游规划.md`，exit 0。
- 批量下载 3 个文档：全部成功，图片 25 + 4 张，`image_failed=0`。
- 标题回退（1.2.0 修复验证）：`docx/X7nndBYZRoC3MJxQKd7c7B71nhh` 的 markdown 导出没有 `<title>`
  （v1.1 靠整篇重导 XML 才拿到标题）→ 现在由 `drive +inspect` 元数据取回
  `工程技术：在智能体优先的世界中利用 Codex.md`，`title_fallback=1`、`complete=True`、**exit 0**。
- 递归模式：CLI `-r --max-docs 4` 正常执行（这 3 篇文档正文中没有 Docx/Wiki 子文档链接，因此未产生子文档）。
- **整库导出**：`--space-id 7108212423034667011`（罗孚传说，5 个 docx 节点）→
  `_INDEX.md` / `_failures.md` / `_manifest.json` / `_manifest.csv` 齐全，`ok=5 complete=True exit 0`；
  嵌套层级用 `示例知识库`（10 节点、含 2 层容器）验证，容器正文正确落在 `<目录>/_分类页.md`。
- **附件与表格**（用真实 token 定向验证）：`sheets +csv-get` 导出 `看房记录202609.csv`（`data.annotated_csv` 字段命中）；
  `drive +download` 取回真实附件原件（52 KB）；无预览候选的类型正确落到 `failed` 分支。
- **Web 服务**：文档模式批量任务 `ok=3`、预览/ZIP/取消/目录逃逸校验（404）全部通过；
  知识库模式任务识别 `wiki-space://<id>`，`_INDEX.md`、`_manifest.csv`、文件列表与 ZIP 均正常。
- Skill 安装：`~/.dsh/skills/lark-docs-to-md`（符号链接）被 DeepSeek Harness 立即识别并成功加载，
  更新 `description` 后目录热更新生效。

1.2.0 的缺陷修复与新特性由单元测试覆盖（`scripts/test_download_docx_tree.py` 14 项 +
`scripts/test_download_wiki_space.py` 21 项 = 35 项，`python3 -m unittest discover -s scripts -p "test_*.py"` 全绿），
包括标题回退（`drive-inspect` / 节点元数据 / `未命名-<token[:8]>`）、空文档写入但不影响退出码、
重名子表不互相覆盖、多子表按 `sheet_name` 命名、`drive +download` 未落盘判失败、
归档文件数/体积包含图片、`--resume` 能区分「没有检查点」与「无需跳过」、整库导出的命名与 manifest 断言。

**独立验收（用户侧，2026-09-21）**：`公司管理制度`（space `7007715075855450113`，279 节点）全量回归
与本仓库实现逐位吻合——`ok=172 / partial=103 / empty=2 / failed=2`；图片 136 个、附件 113 个
做 MD5 多重集比对，**零缺失、零重复**；Markdown 内部链接 409 条、断链 0；16 篇旧版 doc 全部
`legacy-raw-content` 成功；8 子表工作簿、`--flat`、`--attachments skip/preview`、`--max-nodes`、
退出码契约（含失败子树 → `EXIT=1`）逐项通过。该报告提出的 5 个问题已在 1.2.1 修复。

1.2.1 本机复核：真实 8 子表工作簿（`重要负债表`、`资产核算表`）导出为
`重要负债表__总览.csv` 等可读名并带 `sheets` 映射；归档统计与 `find`/`du` 口径一致（排除台账文件）；
`--resume` 首跑给出「未找到可用的 state.json」提示、二跑正确「跳过 5 个已完成节点」。

仍有限制：本机登录缺少旧版 `doc` 的 `raw_content` scope（`missing_scope`），
因此**旧版 doc 的纯文本导出在本环境只有 mock 测试覆盖**（用户侧账号已真实跑通 16/16）；
本机可访问的知识库里没有带图片的 wiki 空间，图片计入统计这部分由单元测试覆盖。

## License

[MIT](LICENSE) © 2026 RoverTang

选择 MIT 的原因：这个仓库的定位是"给人用、也给 Agent 装的工具"，MIT 是最宽松、最通用、附加条件最少的许可——允许任何人不带负担地复制、修改、商用、再分发，也能被放进闭源项目；它兼容几乎所有下游许可，不需要像 GPL 那样传递传染性，也不需要像 Apache-2.0 那样附带 NOTICE 文件维护义务。对脚本 + skill 这种希望被到处引用、被 Agent 自动抓取安装的项目，MIT 的分发阻力最小。

`SKILL.md` 的 `license` 字段已同步为 `MIT`（该字段在 Agent Skills 规范与各 Agent 的白名单内，可安全保留）。
