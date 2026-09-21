# Installing this skill in different agents

This repository **is** the skill: the repository root contains `SKILL.md`, and the
bundled Python lives in `scripts/` and `web/` next to it. Installing the skill
therefore means "make this folder visible to the agent under one of its skill
roots" - there is no second copy of the scripts to keep in sync.

The folder must be named `lark-docs-to-md` to match the `name` field in
`SKILL.md`. All five tools below discover the plain `<root>/<name>/SKILL.md`
layout, so one clone fits everywhere.

## Recommended: one shared root for four tools

`~/.agents/skills/lark-docs-to-md` is read by Codex (newer versions), DeepSeek
Harness, OpenClaw, and Hermes (via `skills.external_dirs`). Clone once, then
link it into the other roots that need their own directory:

```bash
git clone <repo-url> ~/.agents/skills/lark-docs-to-md
ln -s ~/.agents/skills/lark-docs-to-md ~/.codex/skills/lark-docs-to-md   # Codex (all versions)
ln -s ~/.agents/skills/lark-docs-to-md ~/.claude/skills/lark-docs-to-md  # Claude Code
```

Windows (PowerShell), when the directories exist:

```powershell
git clone <repo-url> "$env:USERPROFILE\.agents\skills\lark-docs-to-md"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.codex\skills\lark-docs-to-md" `
  -Target "$env:USERPROFILE\.agents\skills\lark-docs-to-md"
```

Bundled helper (symlinks by default, `--copy` for a private copy, `--dry-run`
to preview; run it from the clone):

```bash
python3 scripts/install_skill.py --targets agents,codex,claude
python3 scripts/install_skill.py --targets all --copy
python3 scripts/install_skill.py --project /path/to/repo --project-only
```

## Per-tool reference

| Tool | User skill root | Project / workspace root | Notes |
| --- | --- | --- | --- |
| Codex CLI | `~/.codex/skills/` (always supported), `~/.agents/skills/` (newer builds) | `<repo>/.agents/skills/`, `<repo>/.codex/skills/` | Folder symlinks are followed. Reads only `name` + `description` (+ `metadata.short-description`); `agents/openai.yaml` is optional UI metadata. The skill list is capped at ~2% of the context window, so keep descriptions short. |
| Claude Code | `~/.claude/skills/<name>/` | `<repo>/.claude/skills/<name>/`, parent dirs are also scanned | The `/command` name comes from the directory name. For claude.ai uploads only `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools` are accepted - this skill stays inside that set, and `metadata.version` counts as part of the accepted `metadata` map rather than an extra field. `description` + `when_to_use` are truncated at 1536 characters. |
| DeepSeek Harness (DSH) | `~/.dsh/skills/` and `~/.agents/skills/` | `<project>/.dsh/skills/`, `<project>/.agents/skills/` | Discovery is exactly one level deep (`<root>/<name>/SKILL.md`); nested `**/SKILL.md` is ignored. Frontmatter `name` is authoritative and must match `^[a-z0-9]+(-[a-z0-9]+)*$`. Legacy camelCase invocation keys such as `modelInvocable` make the skill load fail silently, so never add them - keys inside the `metadata` map (for example `metadata.version`) are fine. Descriptions show at most 500 characters. |
| OpenClaw | `~/.openclaw/skills/` | `<workspace>/skills/`, `<workspace>/.agents/skills/` | Also `~/.agents/skills/`. Install with `openclaw skills install git:owner/repo` or `openclaw skills install ./path/to/skill --as lark-docs-to-md`. Node-hosted skills require the directory name to equal `name`. `{baseDir}` is an OpenClaw-only placeholder; this skill uses plain relative paths instead. |
| Hermes | `~/.hermes/skills/` | `<project>/.hermes/skills/`, `<project>/.agents/skills/` | Install from a repo or a raw `SKILL.md` URL with `hermes skills install <url>`; add `~/.agents/skills` to `skills.external_dirs` to reuse the shared clone. Project skills require `hermes skills trust`. |

Verify after installing: ask the agent to list its skills, or run the skill's own
check (`python3 scripts/check_env.py`) to confirm the bundled scripts are
reachable from the skill directory.

## Portability rules this skill follows

Keep these rules when editing anything here:

1. Only `name` and `description` are required frontmatter; everything else is
   optional decoration. `description` is double-quoted, has no angle brackets,
   and stays well under 1024 characters.
2. Frontmatter starts at line 1 with `---`, uses spaces (never tabs), and avoids
   duplicate keys.
3. Optional decoration belongs inside the spec's `metadata` map, never as a new
   top-level key. `metadata.version: "1.2.0"` is safe there - the spec reserves
   the map for exactly this kind of annotation, all five agents ignore unknown
   keys inside it, and it is where the project version is recorded (matching the
   repository's `VERSION` file and `CHANGELOG.md`). What is *not* safe is a
   camelCase **invocation** key at the top level (`modelInvocable`,
   `disableModelInvocation`, `userInvocable`): DeepSeek Harness drops the whole
   skill silently when it sees one. Never add those, and never promote a
   `metadata` child such as `version` to the top level.
4. Bundled files are referenced **relative to the skill directory**
   (`scripts/...`, `references/...`), never with absolute paths or `../`.
5. Scripts are standard library only, invoked as `python3 scripts/x.py`, and
   accept `--help`. Nothing depends on the executable bit, `bash`, GNU-only
   flags, or `/tmp`.
6. Text is read and written with `encoding="utf-8"`; `.gitattributes` forces LF so
   CRLF can never reach a shebang or a YAML parser.
7. No interactive `input()` prompts: arguments, environment variables, and clear
   non-zero exit codes carry all the state an agent needs.
8. `agents/openai.yaml` is Codex-only sugar and is never required for the skill
   to work.

## Known trade-off

Tool authors recommend that a skill directory contain no extra documentation.
This repository deliberately keeps `README.md` at the root because it is also a
normal open-source project that people browse on GitHub. `CHANGELOG.md` and
`VERSION` are the same kind of inert extra: no agent reads them to discover or
run the skill, and `VERSION` is only consumed by `scripts/check_env.py --version`.
`scripts/install_skill.py --copy` can produce a lean copy
(`SKILL.md`, `agents/`, `scripts/`, `references/`, `web/`) if a tool ever
complains about them.
