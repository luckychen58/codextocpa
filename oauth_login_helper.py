#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Codex OAuth helper for the local CLIProxyAPI.

This helper does not automate credential entry. It only:
1. Requests a fresh OAuth URL and state from the local management API.
2. Optionally opens the URL in the system browser.
3. Polls the local auth-status endpoint until the flow completes.
4. Locates the newly written auth file in the local auth directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


DEFAULT_BASE_URL = "http://127.0.0.1:8317"
DEFAULT_AUTH_DIR = Path(r"C:\Users\Administrator\Documents\Playground\CLIProxyAPI\auths")
DEFAULT_ENV_KEYS = ("CLIPROXY_MANAGEMENT_KEY", "CLI_PROXY_PASSWORD")


def _browser_candidates() -> list[tuple[str, list[str]]]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")

    return [
        (
            "chrome",
            [
                shutil.which("chrome.exe") or "",
                shutil.which("chrome") or "",
                str(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                str(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            ],
        ),
        (
            "edge",
            [
                shutil.which("msedge.exe") or "",
                shutil.which("msedge") or "",
                str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            ],
        ),
        (
            "firefox",
            [
                shutil.which("firefox.exe") or "",
                shutil.which("firefox") or "",
                str(Path(program_files) / "Mozilla Firefox" / "firefox.exe"),
                str(Path(program_files_x86) / "Mozilla Firefox" / "firefox.exe"),
            ],
        ),
    ]


def _private_browser_command(url: str) -> list[str] | None:
    flags = {
        "chrome": "--incognito",
        "edge": "--inprivate",
        "firefox": "--private-window",
    }
    for browser_name, candidates in _browser_candidates():
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if not path.is_file():
                continue
            return [str(path), flags[browser_name], url]
    return None


def open_browser(url: str, private: bool = True) -> tuple[bool, str]:
    if private:
        command = _private_browser_command(url)
        if command:
            try:
                subprocess.Popen(command)
                return True, f"private:{Path(command[0]).name}"
            except OSError as exc:
                return False, f"failed to start private browser: {exc}"
        opened = webbrowser.open(url)
        if opened:
            return True, "fallback:default-browser"
        return False, "no supported private browser found"

    opened = webbrowser.open(url)
    return (opened, "default-browser" if opened else "default-browser-failed")


def get_management_key(cli_value: str) -> str:
    if cli_value.strip():
        return cli_value.strip()
    for env_name in DEFAULT_ENV_KEYS:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def build_headers(key: str) -> dict[str, str]:
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }


def request_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, object]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc

    try:
        return status, json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response: {body[:200]}") from exc


def fetch_auth_request(base_url: str, headers: dict[str, str]) -> dict[str, str]:
    query_url = f"{base_url.rstrip('/')}/v0/management/codex-auth-url?is_webui=true"
    status, payload = request_json(query_url, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"unexpected auth-url response: {payload!r}")

    auth_url = (
        payload.get("url")
        or payload.get("auth_url")
        or payload.get("login_url")
        or ""
    )
    state = payload.get("state", "")
    if not auth_url:
        raise RuntimeError(f"auth URL missing in response: {payload!r}")
    if not state:
        parsed = urllib.parse.urlsplit(auth_url)
        state = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)).get("state", "")
    if not state:
        raise RuntimeError("OAuth state missing in response")

    return {"url": auth_url, "state": state}


def fetch_auth_status(base_url: str, headers: dict[str, str], state: str) -> dict[str, str]:
    params = urllib.parse.urlencode({"state": state})
    status_url = f"{base_url.rstrip('/')}/v0/management/get-auth-status?{params}"
    http_status, payload = request_json(status_url, headers=headers)
    if http_status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"unexpected status response: {payload!r}")
    return {
        "status": str(payload.get("status", "")).strip().lower(),
        "error": str(payload.get("error", "")).strip(),
    }


def snapshot_auth_files(auth_dir: Path) -> dict[str, float]:
    snapshot: dict[str, float] = {}
    if not auth_dir.is_dir():
        return snapshot
    for path in auth_dir.glob("codex-*.json"):
        try:
            snapshot[str(path)] = path.stat().st_mtime
        except OSError:
            continue
    return snapshot


def find_updated_auth_file(auth_dir: Path, before: dict[str, float]) -> Path | None:
    if not auth_dir.is_dir():
        return None

    updated: list[Path] = []
    for path in auth_dir.glob("codex-*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        old_mtime = before.get(str(path))
        if old_mtime is None or mtime > old_mtime:
            updated.append(path)

    if updated:
        updated.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return updated[0]

    existing = sorted(auth_dir.glob("codex-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return existing[0] if existing else None


def summarize_auth_file(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "error": f"failed to parse auth file: {exc}"}

    return {
        "path": str(path),
        "type": str(payload.get("type", "")),
        "email": str(payload.get("email", "")),
        "account_id": str(payload.get("account_id", "")),
        "expired": str(payload.get("expired", "")),
        "disabled": str(payload.get("disabled", "")),
    }


def print_summary(title: str, rows: dict[str, str]) -> None:
    print()
    print(title)
    print("-" * len(title))
    for key, value in rows.items():
        print(f"{key}: {value}")


def poll_until_complete(
    base_url: str,
    headers: dict[str, str],
    state: str,
    timeout: int,
    interval: float,
) -> dict[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_info = fetch_auth_status(base_url, headers, state)
        status = status_info["status"]
        if status == "wait":
            print(f"[wait] state={state} remaining={int(deadline - time.time())}s")
            time.sleep(interval)
            continue
        return status_info
    return {"status": "error", "error": f"timeout after {timeout}s"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex OAuth helper for local CLIProxyAPI")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"management base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument(
        "--auth-dir",
        default=str(DEFAULT_AUTH_DIR),
        help=f"auth directory for saved codex files (default: {DEFAULT_AUTH_DIR})",
    )
    parser.add_argument("--management-key", default="", help="management key for the local 8317 API")
    parser.add_argument("--no-open", action="store_true", help="do not open the system browser automatically")
    parser.add_argument("--normal-open", action="store_true", help="open the default browser normally instead of private mode")
    parser.add_argument("--probe-only", action="store_true", help="only fetch auth URL and verify that status starts as wait")
    parser.add_argument("--timeout", type=int, default=300, help="max seconds to wait for OAuth completion")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    management_key = get_management_key(args.management_key)
    if not management_key:
        print("management key missing; use --management-key or set an environment variable:")
        for env_name in DEFAULT_ENV_KEYS:
            print(f"  - {env_name}")
        return 2

    headers = build_headers(management_key)
    auth_dir = Path(args.auth_dir)
    before = snapshot_auth_files(auth_dir)

    auth_request = fetch_auth_request(args.base_url, headers)
    print_summary("OAuth Request", auth_request)

    if args.probe_only:
        status_info = fetch_auth_status(args.base_url, headers, auth_request["state"])
        print_summary("Initial Status", status_info)
        return 0 if status_info["status"] == "wait" else 1

    if args.no_open:
        print("\nOpen the URL above in your browser, complete login, then let this program keep waiting.")
    else:
        opened, mode = open_browser(auth_request["url"], private=not args.normal_open)
        print(f"\nBrowser open requested: {opened} ({mode})")
        if not opened:
            print("Open the URL manually if your browser did not launch.")

    print("\nWaiting for OAuth completion...")
    status_info = poll_until_complete(
        args.base_url,
        headers,
        auth_request["state"],
        timeout=args.timeout,
        interval=args.interval,
    )
    print_summary("Final Status", status_info)

    if status_info["status"] != "ok":
        return 1

    auth_file = find_updated_auth_file(auth_dir, before)
    if not auth_file:
        print("\nOAuth completed, but no auth file was found.")
        return 1

    print_summary("Auth File", summarize_auth_file(auth_file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
