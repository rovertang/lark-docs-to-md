# lark-docs-to-md

把飞书 / Lark 的 **Docx**、**Wiki** 文档批量导出成本地 **Markdown**（含图片），并用同一个内核提供三种用法：

| 用法 | 入口 | 适合谁 |
| --- | --- | --- |
| 命令行脚本 | `scripts/download_docx_tree.py`、`scripts/batch_download.py` | 想写进定时任务、CI、自己拼命令的人 |
| 本地 Web 服务 | `web/lark_download_web.py`（单文件、零依赖） | 想打开浏览器、粘贴链接、点按钮下载的人 |
| Agent Skill | `SKILL.md`（本仓库根目录就是 skill 目录） | Codex / Claude Code / DeepSeek Harness / OpenClaw / Hermes 等 Agent |

三种用法共用同一套脚本：Web 服务不会另写一份下载逻辑，它用 `subprocess` 调用 `scripts/download_docx_tree.py`，并直接复用其中的 URL 解析、文件名清洗和 manifest 读取函数。**仓库里只有一份下载实现，不存在版本分叉。**

---

## 目录结构

```text
lark-docs-to-md/                 # 仓库根目录 == skill 目录（folder 名必须等于 SKILL.md 的 name）
├── SKILL.md                     # Agent Skill 定义（可移植 frontmatter）
├── README.md                    # 本文件（人读；对 Agent 发现 skill 无影响）
├── agents/openai.yaml           # Codex 专用的 UI 元数据（可选，缺失也不影响运行）
├── references/                  # 按需加载的参考文档（Agent 渐进披露）
│   ├── agent-install.md         #   各 Agent 的 skill 目录与安装方式
│   ├── troubleshooting.md       #   登录/权限/图片/Windows/WSL 排错
│   └── output-format.md         #   Markdown 转换规则与 manifest 字段
├── scripts/
│   ├── download_docx_tree.py    # 单文档 / 递归下载（核心）
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

- 默认申请 `docs` 域权限，也就是下载文档所需的最小集合；确有需要再用 `--domain` 追加。
- Token 由 `lark-cli` 保存，本项目的任何脚本**都不读取、不打印、不落盘** token。
- Device code 约 **10 分钟**过期；过期就重新跑一次 `--login` 拿新链接，不需要重装任何东西。
- 用户 token 的 refresh token 也会过期（本机实测遇到过 `missing (refresh token expired)`），此时同样只需重新 `--login`。
- 需要机器人身份时加 `--identity bot`，但只有在该机器人对所有目标文档都有权限时才有意义；**用户自己的文档请用 `user`**。

### 3. 下载

```bash
# 单个文档（默认不跟随正文里引用的其他文档，最安全）
python3 scripts/download_docx_tree.py "https://xxx.feishu.cn/docx/<token>" -o ./downloads

# 递归：连正文中引用/链接的 Docx、Wiki 子文档一起下载
python3 scripts/download_docx_tree.py "https://xxx.feishu.cn/docx/<token>" -o ./downloads -r --max-docs 50

# 多个独立链接（每行一个 URL，支持 # 注释和空行）
python3 scripts/batch_download.py -f ./urls.txt -o ./downloads
```

输出固定在 `<输出目录>/<根文档 token>/` 下：Markdown 用文档真实标题命名，图片进 `assets/`（递归模式下为 `assets/<token>/`），结果摘要写入 `_download-manifest.json`。

---

## Web 服务（浏览器里下载，支持批量）

```bash
python3 web/lark_download_web.py                 # 默认 http://127.0.0.1:8765/
python3 web/lark_download_web.py --open --port 9000
python3 web/lark_download_web.py --output-dir ~/Downloads/lark
```

- **零依赖**：只用 Python 标准库 + 内嵌前端，`python3 web/lark_download_web.py` 起来就能用，不需要 `pip install`、不需要 Node 构建。
- **页面功能**：粘贴多个链接（一行一个，也支持直接粘贴一个 `urls.txt` 的**路径**）、递归开关、身份、输出目录、重试、超时、文档数上限；实时日志；每个 URL 的成功/失败卡片；文件列表与 Markdown 预览；一键打包 ZIP。
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
5. 报告：绝对输出目录、成功文档数、图片数、失败 URL 及原因、退出码。
   任何图片失败都视为未完整下载，不要声称"全部完成"。
```

Agent 端需要知道的约定（已写进 `SKILL.md` 与 `references/`）：

- **先自检再下载**：`check_env.py` 的退出码就是决策依据，不要靠猜。
- **不重写逻辑**：一律调用 `scripts/` 里的脚本；Web 服务同理，它是子进程调用同一个脚本。
- **不碰 token**：任何情况下都不打印、不复制、不写入 token；授权只走 `--login` / `--device-code`。
- **如实汇报**：退出码 `1` 或存在 `image_failures` 时必须说"未完整"，并给出失败 URL；详情读 `_download-manifest.json`。
- **递归要克制**：只有用户明确要求下载子文档时才加 `--recursive`，并建议配 `--max-docs`。

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

`check_env.py`：`--json`、`--login`、`--no-wait`、`--device-code`、`--domain`、`--identity`、`--output-dir`、`--lark-cli`、`--timeout`。

`install_skill.py`：`--targets`、`--all`、`--copy`、`--project`、`--project-only`、`--name`、`--force`、`--dry-run`、`--json`。

Web 服务：`--host`、`--port`、`--output-dir`、`--lark-cli`、`--identity`、`--open`、`--allow-remote`。

---

## 输出与退出码

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
| `0` | 发现的文档和图片全部下载成功 |
| `1` | 部分失败，或触发 `--max-docs` 提前停止——**不要当作完整归档**，请读 manifest |
| `2` | URL/参数非法，或输出目录读写失败 |

---

## 参考脚本的一致性核对

`tmp/` 里原有的两份参考材料（`lark-docx-batch-download/` 与 `20260717-飞书文档批量下载skill/lark-docx-batch-download/scripts/`）中的脚本**完全一致**，已用 md5 核对：

| 文件 | md5 | 结果 |
| --- | --- | --- |
| `batch_download.py` | `c2e1b3bc88261948e2f0079dfe8ccc3f` | 两份相同 |
| `download_docx_tree.py` | `8ae3a3140a0c1b1b89324a40fb7aca8c` | 两份相同 |

因此本仓库只保留**一份**权威副本放在 `scripts/`，不再保留第二份拷贝；skill 目录通过"仓库根目录即 skill 目录"的方式直接使用这份脚本，从机制上杜绝版本分叉。（`tmp/` 已由 `.gitignore` 忽略，仅作本地参考资料。）

工作区里的这两份脚本与参考材料**逐字节一致**（上表 md5 即取自这些文件）；提交入库时由 `.gitattributes` 的 `* text=auto eol=lf` 统一规范为 LF 换行，**内容不变**，只是不再保留 CRLF。

---

## 开发与自测

```bash
python3 -m py_compile scripts/*.py web/lark_download_web.py   # 语法检查
python3 -m unittest discover -s scripts -p "test_*.py" -v     # 单元测试（6 项）
python3 scripts/check_env.py --output-dir /tmp/x              # 环境自检
python3 scripts/install_skill.py --dry-run                    # skill 校验 + 安装预演
python3 web/lark_download_web.py --port 8765                  # Web 服务
```

端到端实测（2026-09-18，用户身份授权后）：

- 单文档下载：`Qj58dcHFAoOcOVx5l7mcEK5Hnjb` → `20260809宁波旅游规划.md`，exit 0。
- 批量下载 3 个文档：全部成功，图片 25 + 4 张，`image_failed=0`。
- 递归模式：CLI `-r --max-docs 4` 正常执行（这 3 篇文档正文中没有 Docx/Wiki 子文档链接，因此未产生子文档）。
- Web 服务：粘贴 URL 列表 → 任务 `ok=3`、预览正常、ZIP 打包 35 个文件、取消任务生效、目录逃逸请求返回 404。
- Skill 安装：`~/.dsh/skills/lark-docs-to-md`（符号链接）被 DeepSeek Harness 立即识别并成功加载。

## License

[MIT](LICENSE) © 2026 RoverTang

选择 MIT 的原因：这个仓库的定位是"给人用、也给 Agent 装的工具"，MIT 是最宽松、最通用、附加条件最少的许可——允许任何人不带负担地复制、修改、商用、再分发，也能被放进闭源项目；它兼容几乎所有下游许可，不需要像 GPL 那样传递传染性，也不需要像 Apache-2.0 那样附带 NOTICE 文件维护义务。对脚本 + skill 这种希望被到处引用、被 Agent 自动抓取安装的项目，MIT 的分发阻力最小。

`SKILL.md` 的 `license` 字段已同步为 `MIT`（该字段在 Agent Skills 规范与各 Agent 的白名单内，可安全保留）。
