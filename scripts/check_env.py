#!/usr/bin/env python3
"""Check the local environment needed by lark-docs-to-md and guide login.

This script answers one question: *can this machine download Feishu/Lark
documents right now?*  It verifies the Python version, locates ``lark-cli``,
inspects the current authentication state, and (optionally) drives the
`lark-cli` device-flow login so a first-time user never has to read the CLI
documentation.

Usage
-----
    python scripts/check_env.py                  # human-readable report
    python scripts/check_env.py --json           # machine-readable report for agents
    python scripts/check_env.py --login          # authorize, then wait for completion
    python scripts/check_env.py --login --no-wait  # print URL + QR code only (agent flow)
    python scripts/check_env.py --device-code <code>  # finish a --no-wait login

Exit codes
----------
    0  ready to download
    1  environment is usable but an interactive login is still required
    2  a prerequisite is missing (Python too old, lark-cli not found, ...)

The script never prints or stores access tokens.  ``--json`` output contains
only status metadata reported by ``lark-cli auth status``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 10)
DEFAULT_LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
# `docs` covers docx export, `wiki` the node tree of a knowledge base, `drive` the
# attachment download/preview APIs and `sheets` the CSV export.
DEFAULT_DOMAIN = "docs,wiki,drive,sheets"
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
LOGIN_EXPIRES_HINT = (
    "Device codes expire after about 10 minutes; run the command again for a "
    "fresh verification URL."
)

OK = "ok"
WARN = "warn"
FAIL = "fail"


def read_version() -> str:
    """Project version, kept in the repository's VERSION file."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _decode_json(text: str) -> dict[str, Any] | None:
    value = (text or "").lstrip("\ufeff\r\n\t ")
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            return None
        try:
            decoded, _ = json.JSONDecoder().raw_decode(value[start:])
        except json.JSONDecodeError:
            return None
    return decoded if isinstance(decoded, dict) else None


class Runner:
    """Thin subprocess wrapper that never raises on non-zero exits."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(command, 127, "", str(exc))
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                command, 124, "", f"command timed out after {self.timeout:g}s"
            )


def resolve_lark_cli(name: str) -> str | None:
    """Resolve ``lark-cli`` the same way the download scripts do."""
    if os.name == "nt" and not Path(name).suffix:
        for suffix in (".exe", ".ps1", ".cmd", ".bat", ".com"):
            found = shutil.which(f"{name}{suffix}")
            if found:
                return found
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(name).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return None


def check_python() -> dict[str, Any]:
    current = sys.version_info[:3]
    ok = current >= MIN_PYTHON
    return {
        "name": "python",
        "status": OK if ok else FAIL,
        "detail": f"Python {'.'.join(str(part) for part in current)} at {sys.executable}",
        "fix": None
        if ok
        else f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and re-run with that interpreter.",
    }


def check_lark_cli(name: str, runner: Runner) -> dict[str, Any]:
    path = resolve_lark_cli(name)
    if not path:
        return {
            "name": "lark-cli",
            "status": FAIL,
            "detail": f"lark-cli executable not found (looked for {name!r})",
            "fix": "npm install -g @larksuite/cli  # provides the lark-cli executable",
        }
    version = runner.run([path, "--version"])
    text = (version.stdout or version.stderr).strip().splitlines()
    detail = text[0] if text else f"exit code {version.returncode}"
    return {
        "name": "lark-cli",
        "status": OK if version.returncode == 0 else WARN,
        "detail": f"{detail} ({path})",
        "fix": None
        if version.returncode == 0
        else "Reinstall lark-cli, or point --lark-cli / LARK_CLI at a working executable.",
    }


def check_auth(path: str, identity: str, runner: Runner) -> dict[str, Any]:
    process = runner.run([path, "auth", "status", "--json", "--verify"])
    envelope = _decode_json(process.stdout) or _decode_json(process.stderr) or {}
    identities = envelope.get("identities") if isinstance(envelope.get("identities"), dict) else {}
    entry = identities.get(identity) if isinstance(identities.get(identity), dict) else {}
    available = bool(entry.get("available"))
    status = str(entry.get("status", "unknown"))
    if available:
        detail = (
            f"{identity} identity ready"
            f" ({entry.get('userName') or entry.get('appId') or 'unknown account'}"
            f", token {entry.get('tokenStatus', 'ok')})"
        )
    else:
        detail = f"{identity} identity not usable: {status}"
        if entry.get("message"):
            detail = f"{detail} ({entry['message']})"
    return {
        "name": "auth",
        "status": OK if available else FAIL,
        "detail": detail,
        "available": available,
        "identity": identity,
        "account": entry.get("userName") or entry.get("appId"),
        "token_status": entry.get("tokenStatus") or status,
        "expires_at": entry.get("expiresAt"),
        "note": entry.get("hint") or envelope.get("note"),
        "fix": None
        if available
        else "python scripts/check_env.py --login  # authorize as a Feishu user",
    }


def check_output_dir(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "name": "output-dir",
            "status": WARN,
            "detail": "not checked (pass --output-dir to verify a directory)",
            "fix": None,
        }
    target = path.expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".lark-docs-to-md-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return {
            "name": "output-dir",
            "status": FAIL,
            "detail": f"{target} is not writable: {exc}",
            "fix": "Choose another --output-dir or fix permissions.",
        }
    return {
        "name": "output-dir",
        "status": OK,
        "detail": f"{target.resolve()} is writable",
        "fix": None,
    }


def initiate_login(path: str, domain: str, runner: Runner) -> dict[str, Any] | None:
    process = runner.run([path, "auth", "login", "--domain", domain, "--no-wait", "--json"])
    envelope = _decode_json(process.stdout) or _decode_json(process.stderr)
    if not envelope or not envelope.get("verification_url"):
        print(
            f"error: unable to start login: {(process.stderr or process.stdout).strip()}",
            file=sys.stderr,
        )
        return None
    return envelope


def print_qr(path: str, url: str, runner: Runner) -> None:
    process = runner.run([path, "auth", "qrcode", url, "--ascii"])
    qr = (process.stdout or "").rstrip()
    if qr:
        print()
        print(qr)
        print()


def login_and_wait(path: str, domain: str, runner: Runner) -> int:
    envelope = initiate_login(path, domain, runner)
    if envelope is None:
        return 2
    url = str(envelope["verification_url"])
    print("Open this URL in a browser and approve access:")
    print()
    print(f"  {url}")
    print_qr(path, url, runner)
    print("Waiting for authorization in the browser ... (Ctrl+C to abort)")
    process = runner.run(
        [path, "auth", "login", "--device-code", str(envelope.get("device_code", "")), "--json"]
    )
    if process.returncode == 0:
        print("Login completed.")
        return 0
    print(f"Login failed: {(process.stderr or process.stdout).strip()}", file=sys.stderr)
    print(LOGIN_EXPIRES_HINT, file=sys.stderr)
    return 2


def login_no_wait(path: str, domain: str, runner: Runner) -> int:
    envelope = initiate_login(path, domain, runner)
    if envelope is None:
        return 2
    url = str(envelope["verification_url"])
    code = str(envelope.get("device_code", ""))
    print("Ask the user to open this URL and approve access:")
    print()
    print(f"  {url}")
    print_qr(path, url, runner)
    print(f"device_code: {code}")
    print()
    print("After the user confirms authorization, finish with:")
    print(f"  python scripts/check_env.py --device-code {code}")
    print(LOGIN_EXPIRES_HINT)
    return 0


def login_with_device_code(path: str, device_code: str, runner: Runner) -> int:
    process = runner.run([path, "auth", "login", "--device-code", device_code, "--json"])
    if process.returncode == 0:
        print("Login completed.")
        return 0
    print(f"Login failed: {(process.stderr or process.stdout).strip()}", file=sys.stderr)
    print(LOGIN_EXPIRES_HINT, file=sys.stderr)
    return 2


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    runner = Runner(args.timeout)
    checks: list[dict[str, Any]] = [check_python()]

    python_ok = checks[0]["status"] == OK
    cli_check = check_lark_cli(args.lark_cli, runner)
    checks.append(cli_check)
    cli_ok = cli_check["status"] != FAIL
    cli_path = resolve_lark_cli(args.lark_cli) if cli_ok else None

    auth_check: dict[str, Any] = {"name": "auth", "status": FAIL, "available": False}
    if cli_path:
        auth_check = check_auth(cli_path, args.identity, runner)
    checks.append(auth_check)

    checks.append(check_output_dir(args.output_dir))

    ready = python_ok and cli_ok and bool(auth_check.get("available"))
    next_steps: list[str] = []
    if not python_ok:
        next_steps.append(checks[0]["fix"])
    if not cli_ok:
        next_steps.append(cli_check["fix"])
    elif not auth_check.get("available"):
        next_steps.append("python scripts/check_env.py --login")
    if not ready and not next_steps:
        next_steps.append("Review the failing checks above.")

    exit_code = 0 if ready else (2 if (not python_ok or not cli_ok) else 1)
    report = {
        "version": read_version(),
        "ok": ready,
        "ready": ready,
        "exit_code": exit_code,
        "python": checks[0]["detail"],
        "lark_cli": cli_path,
        "identity": args.identity,
        "auth_status": auth_check.get("token_status"),
        "account": auth_check.get("account"),
        "checks": checks,
        "next_steps": next_steps,
    }
    return report, exit_code


def print_report(report: dict[str, Any]) -> None:
    symbols = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]"}
    print(f"lark-docs-to-md environment check (v{report.get('version', 'unknown')})")
    print("=" * 40)
    for check in report["checks"]:
        print(f"{symbols.get(check['status'], '[????]')} {check['name']}: {check['detail']}")
        if check.get("fix"):
            print(f"       fix: {check['fix']}")
    print()
    if report["ready"]:
        print("Result: ready. Examples:")
        print('  python scripts/download_docx_tree.py "<docx-or-wiki-url>" -o ./downloads')
        print("  python scripts/download_wiki_space.py --space-id <id> -o ./downloads")
    else:
        print("Result: not ready yet. Next steps:")
        for step in report["next_steps"]:
            print(f"  - {step}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the lark-docs-to-md runtime environment and help with login.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/check_env.py\n"
            "  python scripts/check_env.py --json\n"
            "  python scripts/check_env.py --login\n"
            "  python scripts/check_env.py --login --no-wait\n"
            "  python scripts/check_env.py --device-code <device-code>"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--version", action="store_true", help="print the project version and exit"
    )
    parser.add_argument(
        "--lark-cli",
        default=DEFAULT_LARK_CLI,
        help="lark-cli executable; defaults to $LARK_CLI or lark-cli",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="also verify that this download directory is writable",
    )
    parser.add_argument(
        "-i", "--identity", choices=("user", "bot"), default="user",
        help="identity to verify; default user",
    )
    parser.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help=f"scope domain(s) to request during login; default {DEFAULT_DOMAIN}",
    )
    parser.add_argument(
        "--timeout", type=float, default=60, help="timeout per command; default 60s"
    )
    parser.add_argument(
        "--login", action="store_true",
        help="start device-flow login and wait for the browser authorization",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="with --login: only print the verification URL, do not wait",
    )
    parser.add_argument(
        "--device-code", help="complete a login started earlier with --login --no-wait",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = Runner(args.timeout)

    if args.version:
        print(read_version())
        return 0

    if args.device_code:
        cli_path = resolve_lark_cli(args.lark_cli)
        if not cli_path:
            print(f"error: lark-cli not found ({args.lark_cli})", file=sys.stderr)
            return 2
        return login_with_device_code(cli_path, args.device_code, runner)

    if args.login:
        cli_path = resolve_lark_cli(args.lark_cli)
        if not cli_path:
            print(
                f"error: lark-cli not found ({args.lark_cli}); install it first "
                "(npm install -g @larksuite/cli)",
                file=sys.stderr,
            )
            return 2
        if args.no_wait:
            code = login_no_wait(cli_path, args.domain, runner)
        else:
            code = login_and_wait(cli_path, args.domain, runner)
        if code != 0:
            return code
        if args.no_wait:
            return 0
        return build_report(args)[1]

    report, exit_code = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
