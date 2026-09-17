#!/usr/bin/env python3
"""Install this repository as an agent skill into the right skill directories.

This repository *is* the skill: ``SKILL.md`` sits next to ``scripts/`` and
``web/``.  Installing therefore means linking (or copying) the repository root
into one or more agent skill roots, for example::

    ~/.agents/skills/lark-docs-to-md     -> Codex (newer), DSH, OpenClaw, Hermes
    ~/.codex/skills/lark-docs-to-md      -> Codex CLI
    ~/.claude/skills/lark-docs-to-md     -> Claude Code
    ~/.dsh/skills/lark-docs-to-md        -> DeepSeek Harness
    ~/.openclaw/skills/lark-docs-to-md   -> OpenClaw
    ~/.hermes/skills/lark-docs-to-md     -> Hermes

Examples
--------
    python3 scripts/install_skill.py --dry-run
    python3 scripts/install_skill.py --targets agents,codex,claude
    python3 scripts/install_skill.py --targets all --copy
    python3 scripts/install_skill.py --project /path/to/project

Symlinks are used by default so that one clone stays the single source of truth;
``--copy`` writes a lean copy (``SKILL.md``, ``agents/``, ``scripts/``,
``references/``, ``web/``) that must be refreshed by re-running this script.

Exit codes: 0 installed (or already installed), 1 at least one target failed,
2 invalid usage or an invalid SKILL.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COPY_ENTRIES = ("SKILL.md", "agents", "scripts", "references", "web", "LICENSE")
IGNORED_NAMES = {"__pycache__", ".git", ".DS_Store"}

TARGETS: dict[str, tuple[str, str]] = {
    "agents": ("~/.agents/skills", "Codex (newer), DSH, OpenClaw, Hermes"),
    "codex": ("~/.codex/skills", "Codex CLI"),
    "claude": ("~/.claude/skills", "Claude Code"),
    "dsh": ("~/.dsh/skills", "DeepSeek Harness"),
    "openclaw": ("~/.openclaw/skills", "OpenClaw"),
    "hermes": ("~/.hermes/skills", "Hermes"),
}
DETECT_DIRS = {
    "agents": "~/.agents",
    "codex": "~/.codex",
    "claude": "~/.claude",
    "dsh": "~/.dsh",
    "openclaw": "~/.openclaw",
    "hermes": "~/.hermes",
}


# --------------------------------------------------------------------------
# SKILL.md validation (frontmatter mistakes make skills fail silently)
# --------------------------------------------------------------------------
def read_frontmatter(path: Path) -> tuple[dict[str, str], list[str]]:
    problems: list[str] = []
    if not path.is_file():
        return {}, [f"{path} not found"]
    text = path.read_text(encoding="utf-8")
    if text.startswith("\ufeff"):
        problems.append("SKILL.md starts with a UTF-8 BOM; frontmatter then fails to parse")
        text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, problems + ["SKILL.md must start with '---' on the first line"]
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)
    else:
        return {}, problems + ["frontmatter is not terminated by a second '---' line"]

    fields: dict[str, str] = {}
    seen: set[str] = set()
    for raw in body:
        if "\t" in raw:
            problems.append("frontmatter uses a tab for indentation")
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in seen:
            problems.append(f"duplicate frontmatter key: {key}")
        seen.add(key)
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) > 1:
            value = value[1:-1]
        fields[key] = value
    return fields, problems


def validate_skill(skill_dir: Path) -> tuple[list[str], list[str], dict[str, str]]:
    problems: list[str] = []
    warnings: list[str] = []
    skill_dir = skill_dir.resolve()
    fields, parse_problems = read_frontmatter(skill_dir / "SKILL.md")
    problems.extend(parse_problems)

    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name:
        problems.append("frontmatter 'name' is required")
    elif not SKILL_NAME_RE.match(name):
        problems.append(
            f"frontmatter 'name' must match {SKILL_NAME_RE.pattern} (got {name!r})"
        )
    elif name != skill_dir.name:
        problems.append(
            f"frontmatter name {name!r} must equal the directory name {skill_dir.name!r} "
            "(required by the Agent Skills spec and by OpenClaw's node host)"
        )
    if len(name) > 64:
        problems.append("frontmatter 'name' must be at most 64 characters")
    if not description:
        problems.append("frontmatter 'description' is required")
    else:
        if len(description) > 1024:
            problems.append("frontmatter 'description' must be at most 1024 characters")
        elif len(description) > 500:
            warnings.append(
                "description is longer than 500 characters; DeepSeek Harness truncates it"
            )
        if "<" in description or ">" in description:
            problems.append("frontmatter 'description' must not contain angle brackets")
        description_line: str | None = None
        for line in (skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                description_line = line
                break
        if description_line is not None and not description_line.split(":", 1)[1].strip().startswith(('"', "'")):
            warnings.append(
                "quote the description value to protect against YAML parsing differences"
            )
    for legacy in ("disableModelInvocation", "modelInvocable", "userInvocable"):
        if legacy in fields:
            problems.append(
                f"legacy camelCase key {legacy!r} makes DeepSeek Harness drop the skill"
            )
    if "metadata" not in fields:
        warnings.append("frontmatter 'metadata' (for example short-description) is missing")
    for required in ("scripts/download_docx_tree.py", "web/lark_download_web.py"):
        if not (skill_dir / required).is_file():
            problems.append(f"bundled file missing: {required}")
    return problems, warnings, fields


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------
def create_link(source: Path, destination: Path) -> str:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise OSError(f"mklink /J failed: {(result.stderr or result.stdout).strip()}")
        return "junction"
    destination.symlink_to(source, target_is_directory=True)
    return "symlink"


def copy_skill(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in COPY_ENTRIES:
        origin = source / entry
        if not origin.exists():
            continue
        target = destination / entry
        if origin.is_dir():
            shutil.copytree(
                origin,
                target,
                ignore=shutil.ignore_patterns(*IGNORED_NAMES),
                dirs_exist_ok=True,
            )
        else:
            shutil.copy2(origin, target)


def same_source(destination: Path, source: Path) -> bool:
    try:
        if destination.is_symlink():
            return Path(os.readlink(destination)).resolve() == source.resolve()
        if destination.exists():
            return destination.resolve() == source.resolve()
    except OSError:
        return False
    return False


def install_one(
    target: str,
    root: Path,
    name: str,
    source: Path,
    *,
    mode: str,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    destination = root / name
    entry: dict[str, Any] = {
        "target": target,
        "path": str(destination),
        "action": "",
        "ok": True,
    }
    if same_source(destination, source):
        entry["action"] = "already installed"
        return entry
    if destination.exists() or destination.is_symlink():
        if not force:
            entry["ok"] = False
            entry["action"] = "skipped: destination exists (use --force to replace)"
            return entry
        entry["action"] = "replaced"
        if not dry_run:
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            else:
                shutil.rmtree(destination)
    else:
        entry["action"] = "created"

    if dry_run:
        entry["action"] += f" ({mode}, dry-run)"
        return entry
    try:
        root.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            copy_skill(source, destination)
            entry["method"] = "copy"
        else:
            try:
                entry["method"] = create_link(source, destination)
            except OSError as exc:
                entry["method"] = f"copy (link failed: {exc})"
                copy_skill(source, destination)
    except OSError as exc:
        entry["ok"] = False
        entry["action"] = f"failed: {exc}"
    return entry


def detect_targets() -> list[str]:
    detected = [
        name for name, path in DETECT_DIRS.items() if Path(path).expanduser().exists()
    ]
    if not detected:
        return ["agents"]
    if "agents" not in detected:
        detected.insert(0, "agents")
    return detected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install lark-docs-to-md as a skill for one or more agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "targets: " + ", ".join(sorted(TARGETS)) + "\n"
            "examples:\n"
            "  python3 scripts/install_skill.py --dry-run\n"
            "  python3 scripts/install_skill.py --targets agents,codex,claude\n"
            "  python3 scripts/install_skill.py --targets all --copy\n"
            "  python3 scripts/install_skill.py --project /path/to/project"
        ),
    )
    parser.add_argument(
        "--targets",
        default="",
        help="comma-separated targets, or 'all'; defaults to the agent directories that exist",
    )
    parser.add_argument("--all", action="store_true", help="shorthand for --targets all")
    parser.add_argument("--project", help="also install into <PROJECT>/.agents/skills")
    parser.add_argument(
        "--project-only", action="store_true", help="install only into --project"
    )
    parser.add_argument("--copy", action="store_true", help="copy files instead of linking")
    parser.add_argument("--name", default=REPO_ROOT.name, help="installed folder name")
    parser.add_argument("--force", action="store_true", help="replace an existing install")
    parser.add_argument("--dry-run", action="store_true", help="show actions only")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = REPO_ROOT
    name = args.name

    problems, warnings, fields = validate_skill(source)
    if name != source.name:
        problems.append(
            f"install name {name!r} must equal the repository directory name {source.name!r} "
            "so that frontmatter, folder, and install path agree"
        )

    requested = args.targets
    if args.all:
        requested = "all"
    if requested:
        wanted = (
            sorted(TARGETS)
            if requested.strip().lower() == "all"
            else [item.strip() for item in requested.split(",") if item.strip()]
        )
        unknown = [item for item in wanted if item not in TARGETS]
        if unknown:
            print(f"error: unknown target(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
    else:
        wanted = detect_targets()

    if args.project_only and not args.project:
        print("error: --project-only requires --project", file=sys.stderr)
        return 2

    mode = "copy" if args.copy else "link"
    results: list[dict[str, Any]] = []
    if not args.project_only:
        for target in wanted:
            root = Path(TARGETS[target][0]).expanduser()
            results.append(
                install_one(target, root, name, source, mode=mode, force=args.force, dry_run=args.dry_run)
            )
    if args.project:
        root = Path(args.project).expanduser().resolve() / ".agents" / "skills"
        results.append(
            install_one("project", root, name, source, mode=mode, force=args.force, dry_run=args.dry_run)
        )

    report = {
        "skill": name,
        "source": str(source),
        "mode": mode,
        "dry_run": args.dry_run,
        "valid": not problems,
        "problems": problems,
        "warnings": warnings,
        "results": results,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"skill     : {name}")
        print(f"source    : {source}")
        print(f"mode      : {mode}" + (" (dry-run)" if args.dry_run else ""))
        for problem in problems:
            print(f"[fail] {problem}")
        for warning in warnings:
            print(f"[warn] {warning}")
        for entry in results:
            mark = "[ ok ]" if entry["ok"] else "[fail]"
            method = f" via {entry['method']}" if entry.get("method") else ""
            print(f"{mark} {entry['target']}: {entry['path']} - {entry['action']}{method}")
        if problems:
            print("SKILL.md validation failed; nothing was installed.")

    if problems:
        return 2
    return 0 if all(entry["ok"] for entry in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
