#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Open Codex OAuth in a fresh private browser and log in with a saved account.

This script:
1. Launches a separate Chrome window with a temporary profile and incognito mode.
2. Opens the local CPA management page and enters the management key slowly.
3. Opens the Codex OAuth flow from the UI.
4. Reads the latest account from registered_accounts.txt by default.
5. Types the OpenAI email and password with human-like delays.
6. Waits for the local OAuth flow to finish and reports the newest auth file.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_BASE_URL = "http://127.0.0.1:8317"
DEFAULT_MANAGEMENT_PAGE = f"{DEFAULT_BASE_URL}/management.html#/oauth"
DEFAULT_ACCOUNTS_FILE = Path(
    r"C:\Users\Administrator\Documents\Playground\AI-Account-Toolkit\chatgpt_register_duckmail\registered_accounts.txt"
)
DEFAULT_AUTH_DIR = Path(r"C:\Users\Administrator\Documents\Playground\CLIProxyAPI\auths")
DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_DUCKMAIL_API_BASE = "https://api.duckmail.sbs"
FAILED_PREFIX = "失败@"
PROCESSED_PREFIX = "已处理@"
_LOG_LOCK = threading.Lock()
_ACTIVE_TASK_LOCK = threading.Lock()
_ACTIVE_TASKS = 0
_OAUTH_SLOT_LOCK = threading.Lock()


@dataclass
class Account:
    email: str
    password: str
    mail_password: str
    line_number: int
    raw_line: str
    extra_fields: list[str]
    processed: bool = False
    failed: bool = False


def mask_email(value: str) -> str:
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def log(message: str) -> None:
    now = time.strftime("%H:%M:%S")
    thread_name = threading.current_thread().name
    prefix = f"[{thread_name}] " if thread_name != "MainThread" else ""
    with _LOG_LOCK:
        print(f"[{now}] {prefix}{message}", flush=True)


def reset_active_task_counter() -> None:
    global _ACTIVE_TASKS
    with _ACTIVE_TASK_LOCK:
        _ACTIVE_TASKS = 0


def mark_task_started(account: Account, active_limit: int) -> None:
    global _ACTIVE_TASKS
    with _ACTIVE_TASK_LOCK:
        _ACTIVE_TASKS += 1
        current_active = _ACTIVE_TASKS
    log(f"已启动登录流程: line {account.line_number} / {mask_email(account.email)}")
    log(f"当前活跃任务数: {current_active} / {active_limit}")


def mark_task_finished(active_limit: int) -> None:
    global _ACTIVE_TASKS
    with _ACTIVE_TASK_LOCK:
        _ACTIVE_TASKS = max(0, _ACTIVE_TASKS - 1)
        current_active = _ACTIVE_TASKS
    log(f"当前活跃任务数: {current_active} / {active_limit}")


def human_pause(min_seconds: float = 0.12, max_seconds: float = 0.35) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def resolve_chrome_path(cli_value: str) -> Path:
    candidates = [
        Path(cli_value) if cli_value else None,
        Path(shutil.which("chrome.exe") or ""),
        Path(shutil.which("chrome") or ""),
        DEFAULT_CHROME_PATH,
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and str(candidate) and candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not find Chrome. Pass --chrome-path explicitly.")


def parse_accounts(path: Path) -> list[Account]:
    if not path.is_file():
        raise FileNotFoundError(f"accounts file not found: {path}")

    accounts: list[Account] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("----")
        if len(parts) < 3:
            continue
        email, password, mail_password = parts[0].strip(), parts[1].strip(), parts[2].strip()
        extra_fields = [part.strip() for part in parts[3:] if part.strip()]
        processed = any(
            field.startswith(PROCESSED_PREFIX) or field.lower().startswith("processed")
            for field in extra_fields
        )
        failed = any(
            field.startswith(FAILED_PREFIX) or field.lower().startswith("failed")
            for field in extra_fields
        )
        if email and password:
            accounts.append(
                Account(
                    email=email,
                    password=password,
                    mail_password=mail_password,
                    line_number=line_number,
                    raw_line=raw_line,
                    extra_fields=extra_fields,
                    processed=processed,
                    failed=failed,
                )
            )

    if not accounts:
        raise RuntimeError(f"no usable accounts found in: {path}")
    return accounts


def format_processed_marker() -> str:
    return f"{PROCESSED_PREFIX}{time.strftime('%Y-%m-%d %H:%M:%S')}"


def format_failed_marker() -> str:
    return f"{FAILED_PREFIX}{time.strftime('%Y-%m-%d %H:%M:%S')}"


def build_account_line(
    account: Account,
    processed_marker: str | None = None,
    failed_marker: str | None = None,
) -> str:
    parts = [account.email, account.password, account.mail_password]
    extra_fields = [
        field for field in account.extra_fields
        if not (
            field.startswith(PROCESSED_PREFIX)
            or field.lower().startswith("processed")
            or field.startswith(FAILED_PREFIX)
            or field.lower().startswith("failed")
        )
    ]
    parts.extend(extra_fields)
    if processed_marker:
        parts.append(processed_marker)
    if failed_marker:
        parts.append(failed_marker)
    return "----".join(parts)


def mark_account_processed(accounts_file: Path, account: Account) -> None:
    lines = accounts_file.read_text(encoding="utf-8", errors="replace").splitlines()
    target_index = account.line_number - 1
    if target_index < 0 or target_index >= len(lines):
        raise IndexError(f"account line {account.line_number} is out of range for {accounts_file}")

    marker = format_processed_marker()
    lines[target_index] = build_account_line(account, processed_marker=marker)
    accounts_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    account.extra_fields = [
        field
        for field in account.extra_fields
        if not (
            field.startswith(PROCESSED_PREFIX)
            or field.lower().startswith("processed")
            or field.startswith(FAILED_PREFIX)
            or field.lower().startswith("failed")
        )
    ]
    account.extra_fields.append(marker)
    account.processed = True
    account.failed = False


def mark_account_failed(accounts_file: Path, account: Account) -> None:
    lines = accounts_file.read_text(encoding="utf-8", errors="replace").splitlines()
    target_index = account.line_number - 1
    if target_index < 0 or target_index >= len(lines):
        raise IndexError(f"account line {account.line_number} is out of range for {accounts_file}")

    marker = format_failed_marker()
    lines[target_index] = build_account_line(account, failed_marker=marker)
    accounts_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    account.extra_fields = [
        field
        for field in account.extra_fields
        if not (
            field.startswith(PROCESSED_PREFIX)
            or field.lower().startswith("processed")
            or field.startswith(FAILED_PREFIX)
            or field.lower().startswith("failed")
        )
    ]
    account.extra_fields.append(marker)
    account.failed = True
    account.processed = False


def launch_chrome(
    chrome_path: Path,
    port: int,
    start_url: str,
    headless: bool = False,
) -> tuple[subprocess.Popen[bytes], Path]:
    profile_dir = Path(tempfile.mkdtemp(prefix="codex-incognito-"))
    command = [
        str(chrome_path),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--incognito",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
    ]
    if headless:
        command.extend(
            [
                "--headless=new",
                "--window-size=1600,900",
                "--hide-scrollbars",
            ]
        )
    else:
        command.append("--new-window")
    command.append(start_url)
    mode = "headless incognito" if headless else "incognito"
    log(f"Launching Chrome {mode}: {chrome_path}")
    log(f"Profile dir: {profile_dir}")
    process = subprocess.Popen(command)
    return process, profile_dir


def wait_for_cdp(port: int, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Chrome remote debugging endpoint did not open on port {port}")


def request_json(url: str, headers: dict[str, str] | None = None) -> object:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers or {}, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def request_json_post(url: str, payload: dict[str, str], headers: dict[str, str] | None = None) -> object:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            body = json.dumps(payload).encode("utf-8")
            merged_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            }
            if headers:
                merged_headers.update(headers)
            req = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def build_headers(management_key: str) -> dict[str, str]:
    if not management_key:
        return {}
    return {
        "Authorization": f"Bearer {management_key}",
        "X-Management-Key": management_key,
    }


def fetch_auth_status(base_url: str, management_key: str, state: str) -> tuple[str, str]:
    params = urllib.parse.urlencode({"state": state})
    url = f"{base_url.rstrip('/')}/v0/management/get-auth-status?{params}"
    payload = request_json(url, headers=build_headers(management_key))
    if not isinstance(payload, dict):
        return "error", f"unexpected payload: {payload!r}"
    return str(payload.get("status", "")).strip().lower(), str(payload.get("error", "")).strip()


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
        if before.get(str(path), 0) < mtime:
            updated.append(path)

    if updated:
        updated.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return updated[0]

    existing = sorted(auth_dir.glob("codex-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    return existing[0] if existing else None


def duckmail_get_token(email: str, mail_password: str, api_base: str) -> str:
    payload = request_json_post(
        f"{api_base.rstrip('/')}/token",
        {"address": email, "password": mail_password},
    )
    if not isinstance(payload, dict) or not payload.get("token"):
        raise RuntimeError("DuckMail token response did not contain a token")
    return str(payload["token"])


def duckmail_list_messages(token: str, api_base: str) -> list[dict[str, object]]:
    try:
        payload = request_json(
            f"{api_base.rstrip('/')}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        if not isinstance(payload, dict):
            return []
        messages = payload.get("hydra:member") or payload.get("member") or payload.get("data") or []
        return messages if isinstance(messages, list) else []
    except Exception:
        return []


def duckmail_get_message_detail(token: str, api_base: str, message_id: str) -> dict[str, object] | None:
    try:
        message_id = message_id.split("/")[-1]
        payload = request_json(
            f"{api_base.rstrip('/')}/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def extract_verification_code(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"Your ChatGPT code is\s*(\d{6})",
        r"Verification code:?\s*(\d{6})",
        r"code is\s*(\d{6})",
        r"验证码[:：]?\s*(\d{6})",
        r"(?<![#&])\b(\d{6})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def snapshot_duckmail_message_ids(token: str, api_base: str) -> set[str]:
    message_ids: set[str] = set()
    for message in duckmail_list_messages(token, api_base):
        msg_id = str(message.get("id") or message.get("@id") or "").strip()
        if msg_id:
            message_ids.add(msg_id)
    return message_ids


def wait_for_duckmail_code(
    token: str,
    api_base: str,
    seen_ids: set[str],
    timeout_seconds: int = 120,
) -> tuple[str, set[str]]:
    deadline = time.time() + timeout_seconds
    known_ids = set(seen_ids)
    while time.time() < deadline:
        messages = duckmail_list_messages(token, api_base)
        for message in messages:
            msg_id = str(message.get("id") or message.get("@id") or "").strip()
            if not msg_id or msg_id in known_ids:
                continue

            known_ids.add(msg_id)
            subject = str(message.get("subject") or "")
            sender = message.get("from") if isinstance(message.get("from"), dict) else {}
            sender_address = str(sender.get("address") or "")
            code = extract_verification_code(subject)
            if not code:
                detail = duckmail_get_message_detail(token, api_base, msg_id)
                if detail:
                    content = str(detail.get("text") or detail.get("html") or "")
                    code = extract_verification_code(content)
            if code and ("openai" in sender_address.lower() or "chatgpt code" in subject.lower()):
                return code, known_ids
        time.sleep(3.0)
    raise RuntimeError("Timed out while waiting for a new OpenAI verification email")


def human_click(locator, description: str) -> None:
    locator.wait_for(state="visible", timeout=30000)
    locator.scroll_into_view_if_needed(timeout=30000)
    human_pause(0.2, 0.6)
    locator.hover(timeout=30000)
    human_pause(0.1, 0.25)
    locator.click(timeout=30000)
    log(f"Clicked: {description}")


def clear_and_type(locator, value: str, description: str) -> None:
    locator.wait_for(state="visible", timeout=30000)
    locator.scroll_into_view_if_needed(timeout=30000)
    human_click(locator, description)
    try:
        locator.press("Control+A")
        human_pause(0.05, 0.12)
        locator.press("Backspace")
    except Exception:
        pass

    for char in value:
        locator.type(char, delay=random.randint(90, 220))
        if char in "@._-!#$%^&*":
            human_pause(0.08, 0.18)
    human_pause(0.18, 0.4)
    log(f"Typed: {description}")


def find_first_visible(page, selectors: list[str], timeout_ms: int = 2500):
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible():
                    return locator
            except Exception:
                continue
        time.sleep(0.15)
    return None


def press_submit(page, description: str) -> None:
    button_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Continue')",
        "button:has-text('继续')",
        "button:has-text('Next')",
        "button:has-text('下一步')",
        "button:has-text('Log in')",
        "button:has-text('登录')",
        "button:has-text('Authorize')",
        "button:has-text('Allow')",
        "button:has-text('同意')",
        "button:has-text('允许')",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Log in')",
    ]
    locator = find_first_visible(page, button_selectors, timeout_ms=1800)
    if locator:
        human_click(locator, description)
        return
    page.keyboard.press("Enter")
    log(f"Pressed Enter: {description}")


def ensure_management_login(page, management_key: str) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    human_pause(1.0, 1.8)

    if "#/login" in page.url:
        password_locator = find_first_visible(
            page,
            [
                "input[type='password']",
                "input[placeholder*='管理密钥']",
            ],
            timeout_ms=8000,
        )
        if not password_locator:
            raise RuntimeError("Could not find management password input")
        clear_and_type(password_locator, management_key, "management key")
        login_button = find_first_visible(
            page,
            [
                "button:has-text('登录')",
                "button.btn-primary",
            ],
            timeout_ms=3000,
        )
        if not login_button:
            raise RuntimeError("Could not find management login button")
        human_click(login_button, "management login")
        page.wait_for_timeout(2500)
        log("Management page logged in")


def open_oauth_page(page) -> None:
    page.evaluate("window.location.hash = '#/oauth'")
    page.wait_for_timeout(2500)
    body_text = page.locator("body").inner_text(timeout=30000)
    if "Codex OAuth" not in body_text:
        raise RuntimeError("OAuth page did not load correctly")
    log("OAuth page is ready")


def click_codex_oauth_login(page) -> str:
    script = """
    (() => {
      const header = [...document.querySelectorAll('.card-header')]
        .find(el => (el.innerText || '').includes('Codex OAuth'));
      if (!header) return { ok: false, reason: 'header-not-found' };
      const button = header.querySelector('button');
      if (!button) return { ok: false, reason: 'button-not-found' };
      button.click();
      return { ok: true };
    })()
    """
    result = page.evaluate(script)
    if not result or not result.get("ok"):
        raise RuntimeError(f"Failed to click Codex OAuth login: {result}")
    page.wait_for_timeout(1500)

    auth_url = page.evaluate(
        """
        (() => {
          const text = document.body.innerText || '';
          const match = text.match(/https:\\/\\/auth\\.openai\\.com\\S+/);
          return match ? match[0] : '';
        })()
        """
    )
    if not auth_url:
        raise RuntimeError("Codex OAuth auth URL did not appear")
    log("Codex OAuth auth URL generated")
    return auth_url


def wait_for_auth_page_ready(page, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        human_pause(0.6, 1.0)

        title = ""
        body = ""
        try:
            title = page.title().strip()
        except Exception:
            pass
        try:
            body = page.locator("body").inner_text(timeout=3000).strip()
        except Exception:
            pass

        waiting_markers = (
            "Just a moment",
            "请稍候",
            "正在进行安全验证",
            "Checking your browser",
        )
        if not any(marker in title or marker in body for marker in waiting_markers):
            return
        log("OpenAI auth page is still in security check, waiting...")
        time.sleep(2.0)
    raise RuntimeError("OpenAI auth page stayed on the security-check screen for too long")


def maybe_fill_email(page, email: str) -> None:
    email_locator = find_first_visible(
        page,
        [
            "input[type='email']",
            "input[name='email']",
            "input[name='username']",
            "input[autocomplete='username']",
            "input[inputmode='email']",
        ],
        timeout_ms=8000,
    )
    if not email_locator:
        continue_with_email = find_first_visible(
            page,
            [
                "button:has-text('Continue with email')",
                "button:has-text('Use email')",
                "button:has-text('email')",
                "button:has-text('邮箱')",
                "[role='button']:has-text('Continue with email')",
                "[role='button']:has-text('email')",
            ],
            timeout_ms=5000,
        )
        if continue_with_email:
            human_click(continue_with_email, "continue with email")
            page.wait_for_timeout(2500)
            email_locator = find_first_visible(
                page,
                [
                    "input[type='email']",
                    "input[name='email']",
                    "input[name='username']",
                    "input[autocomplete='username']",
                    "input[inputmode='email']",
                ],
                timeout_ms=8000,
            )
    if not email_locator:
        return

    current = ""
    try:
        current = email_locator.input_value()
    except Exception:
        current = ""
    if current.strip().lower() == email.strip().lower():
        log("Email field is already populated")
        return

    clear_and_type(email_locator, email, "OpenAI email")
    press_submit(page, "submit email")
    page.wait_for_timeout(2500)


def maybe_fill_password(page, password: str) -> bool:
    password_locator = find_first_visible(
        page,
        [
            "input[type='password']",
            "input[name='password']",
            "input[autocomplete='current-password']",
        ],
        timeout_ms=12000,
    )
    if not password_locator:
        return False

    clear_and_type(password_locator, password, "OpenAI password")
    press_submit(page, "submit password")
    page.wait_for_timeout(2500)
    return True


def get_visible_one_time_code_inputs(page) -> list:
    inputs = []
    locator = page.locator("input")
    count = locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if not item.is_visible():
                continue
            input_type = (item.get_attribute("type") or "").lower()
            autocomplete = (item.get_attribute("autocomplete") or "").lower()
            name = (item.get_attribute("name") or "").lower()
            inputmode = (item.get_attribute("inputmode") or "").lower()
            maxlength = item.get_attribute("maxlength") or ""
            if autocomplete == "one-time-code":
                inputs.append(item)
                continue
            if "code" in name:
                inputs.append(item)
                continue
            if inputmode == "numeric" and input_type in {"text", "", "tel", "number"}:
                inputs.append(item)
                continue
            if maxlength == "1":
                inputs.append(item)
        except Exception:
            continue
    return inputs


def page_requests_email_code(page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""
    markers = [
        "verification code",
        "enter code",
        "check your email",
        "chatgpt code",
        "验证码",
        "输入代码",
        "输入验证码",
        "验证代码",
    ]
    return any(marker in body_text for marker in markers)


def maybe_fill_email_verification_code(
    page,
    duckmail_token: str | None,
    duckmail_api_base: str,
    seen_duckmail_ids: set[str],
) -> set[str]:
    code_inputs = get_visible_one_time_code_inputs(page)
    if not code_inputs or not page_requests_email_code(page):
        return seen_duckmail_ids
    if not duckmail_token:
        raise RuntimeError("OpenAI requested an email verification code, but DuckMail token is unavailable")

    log("OpenAI requested an email verification code; waiting for a fresh DuckMail email")
    code, updated_ids = wait_for_duckmail_code(
        token=duckmail_token,
        api_base=duckmail_api_base,
        seen_ids=seen_duckmail_ids,
        timeout_seconds=120,
    )
    log("DuckMail verification email received")

    if len(code_inputs) >= 4:
        digits = list(code.strip())
        for index, digit in enumerate(digits[: len(code_inputs)]):
            clear_and_type(code_inputs[index], digit, f"verification digit {index + 1}")
        page.wait_for_timeout(1200)
    else:
        clear_and_type(code_inputs[0], code, "OpenAI verification code")
    press_submit(page, "submit verification code")
    page.wait_for_timeout(2500)
    return updated_ids


def maybe_accept_consent(page) -> bool:
    for _ in range(4):
        if get_visible_one_time_code_inputs(page) and page_requests_email_code(page):
            return False
        consent_button = find_first_visible(
            page,
            [
                "button:has-text('Continue')",
                "button:has-text('Authorize')",
                "button:has-text('Allow')",
                "button:has-text('同意')",
                "button:has-text('允许')",
                "button:has-text('继续')",
                "[role='button']:has-text('Continue')",
                "[role='button']:has-text('继续')",
                "div[role='button']:has-text('Continue')",
                "div[role='button']:has-text('继续')",
            ],
            timeout_ms=2500,
        )
        if not consent_button:
            return False
        try:
            label = consent_button.inner_text(timeout=1000).strip() or "consent button"
        except Exception:
            label = "consent button"
        human_click(consent_button, label)
        page.wait_for_timeout(2500)
        return True
    return False


def page_shows_codex_consent(page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    markers = [
        "使用 ChatGPT 登录到 Codex",
        "login to codex",
        "continue 后，chatgpt 将向 codex 提供",
        "继续操作后，chatgpt 将向 codex 提供",
        "codex 不会收到你的聊天历史记录",
        "codex will receive",
    ]
    lowered = body_text.lower()
    return any(marker in body_text or marker in lowered for marker in markers)


def retry_codex_consent_if_needed(page, retry_index: int) -> bool:
    if not page_shows_codex_consent(page):
        return False
    log(f"Consent page still visible, retrying Continue ({retry_index}/2)")
    return maybe_accept_consent(page)


def complete_openai_login(
    page,
    account: Account,
    duckmail_token: str | None,
    duckmail_api_base: str,
    seen_duckmail_ids: set[str],
) -> set[str]:
    page.bring_to_front()
    page.wait_for_timeout(2000)
    wait_for_auth_page_ready(page)
    maybe_fill_email(page, account.email)
    if not maybe_fill_password(page, account.password):
        wait_for_auth_page_ready(page, timeout_seconds=20.0)
        if not maybe_fill_password(page, account.password):
            raise RuntimeError("Could not find the OpenAI password field")

    for _ in range(6):
        updated_ids = maybe_fill_email_verification_code(
            page=page,
            duckmail_token=duckmail_token,
            duckmail_api_base=duckmail_api_base,
            seen_duckmail_ids=seen_duckmail_ids,
        )
        code_used = updated_ids != seen_duckmail_ids
        seen_duckmail_ids = updated_ids
        consent_clicked = maybe_accept_consent(page)
        if not code_used and not consent_clicked:
            break
    return seen_duckmail_ids


def wait_for_oauth_completion(
    base_url: str,
    management_key: str,
    state: str,
    timeout_seconds: int,
    auth_dir: Path,
    auth_snapshot: dict[str, float],
    auth_page=None,
    continue_retry_limit: int = 2,
) -> Path | None:
    deadline = time.time() + timeout_seconds
    continue_retries_used = 0
    last_retry_at = 0.0
    while time.time() < deadline:
        status, error = fetch_auth_status(base_url, management_key, state)
        log(f"OAuth status: {status or 'unknown'}")
        if status in {"ok", "success", "done"}:
            return find_updated_auth_file(auth_dir, auth_snapshot)
        if status in {"error", "failed"}:
            raise RuntimeError(f"OAuth failed: {error or 'unknown error'}")

        if (
            status == "wait"
            and auth_page is not None
            and continue_retries_used < continue_retry_limit
            and time.time() - last_retry_at >= 4.0
        ):
            try:
                if retry_codex_consent_if_needed(auth_page, continue_retries_used + 1):
                    continue_retries_used += 1
                    last_retry_at = time.time()
                    time.sleep(2.5)
                    continue
            except Exception as exc:
                log(f"Consent retry check failed: {exc}")

        if (
            status == "wait"
            and auth_page is not None
            and continue_retries_used >= continue_retry_limit
            and page_shows_codex_consent(auth_page)
        ):
            raise RuntimeError("Clicked Continue 3 times total, but the Codex consent page is still not progressing")
        time.sleep(3.0)
    raise RuntimeError(f"OAuth status did not complete within {timeout_seconds}s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex OAuth incognito browser login")
    parser.add_argument("--management-key", required=True, help="local CPA management password")
    parser.add_argument("--accounts-file", default=str(DEFAULT_ACCOUNTS_FILE), help="registered_accounts.txt path")
    parser.add_argument("--account-line", type=int, default=None, help="1-based account line to use; -1 means last line")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="local CPA base URL")
    parser.add_argument("--chrome-path", default="", help="explicit Chrome path")
    parser.add_argument("--auth-dir", default=str(DEFAULT_AUTH_DIR), help="Codex auth directory")
    parser.add_argument("--duckmail-api-base", default=DEFAULT_DUCKMAIL_API_BASE, help="DuckMail API base URL")
    parser.add_argument("--timeout", type=int, default=180, help="OAuth completion timeout in seconds")
    parser.add_argument("--keep-browser", action="store_true", help="do not close the temporary incognito browser")
    parser.add_argument("--headless", action="store_true", help="run the incognito Chrome window invisibly")
    parser.add_argument("--max-workers", type=int, default=None, help="parallel worker count; if omitted, prompt in console (max 10)")
    parser.add_argument("--stop-on-error", action="store_true", help="stop the batch on the first failed account")
    return parser.parse_args()


def select_account(accounts: list[Account], account_line: int) -> Account:
    if account_line == -1:
        return accounts[-1]
    if account_line < 1 or account_line > len(accounts):
        raise IndexError(f"account line {account_line} is out of range 1..{len(accounts)}")
    return accounts[account_line - 1]


def select_accounts(accounts: list[Account], account_line: int | None) -> list[Account]:
    if account_line is not None:
        return [select_account(accounts, account_line)]
    return [account for account in accounts if not account.processed and not account.failed]


def prompt_parallel_workers(cli_value: int | None, selected_count: int) -> int:
    if selected_count <= 0:
        return 0

    if cli_value is not None:
        if cli_value < 1:
            log("并发数最少为 1，已自动改为 1")
            return 1
        if cli_value > 10:
            log("并发数最多十个，已自动改为 10")
            return min(10, selected_count)
        return min(cli_value, selected_count)

    while True:
        raw = input(f"请输入并发数(1-10，当前待处理 {selected_count} 个): ").strip()
        if not raw:
            print("请输入 1 到 10 之间的数字。", flush=True)
            continue
        try:
            value = int(raw)
        except ValueError:
            print("输入无效，请输入数字。", flush=True)
            continue
        if value < 1:
            print("并发数最少为 1，请重新输入。", flush=True)
            continue
        if value > 10:
            print("最多十个，请重新输入。", flush=True)
            continue
        return min(value, selected_count)


def print_batch_summary(
    total_accounts: int,
    selected_count: int,
    success_count: int,
    failure_count: int,
    skipped_processed_count: int,
    skipped_failed_count: int,
) -> None:
    success_rate = (success_count / selected_count * 100.0) if selected_count else 0.0
    print("\n" + "=" * 52, flush=True)
    print("批处理结果汇总", flush=True)
    print("=" * 52, flush=True)
    print(f"账号总数: {total_accounts}", flush=True)
    print(f"本次处理账号数: {selected_count}", flush=True)
    print(f"已跳过(已处理): {skipped_processed_count}", flush=True)
    print(f"已跳过(失败): {skipped_failed_count}", flush=True)
    print(f"成功数: {success_count}", flush=True)
    print(f"失败数: {failure_count}", flush=True)
    print(f"成功率: {success_rate:.2f}%", flush=True)
    print("=" * 52, flush=True)


def run_exclusive_oauth_flow(
    args: argparse.Namespace,
    account: Account,
    management_page,
    context,
    auth_dir: Path,
    duckmail_token: str,
    seen_duckmail_ids: set[str],
) -> Path | None:
    auth_snapshot = snapshot_auth_files(auth_dir)
    auth_page = None

    try:
        open_oauth_page(management_page)
        auth_url = click_codex_oauth_login(management_page)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query).get("state", [""])[0]
        if not state:
            raise RuntimeError("OAuth state was missing from the generated auth URL")

        auth_page = context.new_page()
        auth_page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
        log("OpenAI authorization page opened in the incognito browser")
        complete_openai_login(
            page=auth_page,
            account=account,
            duckmail_token=duckmail_token or None,
            duckmail_api_base=args.duckmail_api_base,
            seen_duckmail_ids=seen_duckmail_ids,
        )

        newest_auth = wait_for_oauth_completion(
            base_url=args.base_url,
            management_key=args.management_key,
            state=state,
            timeout_seconds=args.timeout,
            auth_dir=auth_dir,
            auth_snapshot=auth_snapshot,
            auth_page=auth_page,
        )

        log("OAuth flow completed")
        if newest_auth:
            log(f"Newest auth file: {newest_auth}")
        return newest_auth

    except Exception:
        if auth_page is not None:
            try:
                debug_png = Path.cwd() / "openai_auth_failure.png"
                auth_page.screenshot(path=str(debug_png), full_page=True)
                log(f"Saved auth-page screenshot: {debug_png}")
            except Exception:
                pass
            try:
                body = auth_page.locator("body").inner_text(timeout=3000).strip()
                if body:
                    log(f"Auth page body snippet: {body[:1200]}")
            except Exception:
                pass
        raise


def run_account_flow(args: argparse.Namespace, account: Account, chrome_path: Path) -> Path | None:
    auth_dir = Path(args.auth_dir)
    duckmail_token = ""
    seen_duckmail_ids: set[str] = set()

    log(f"Using account: {mask_email(account.email)}")
    if account.mail_password:
        duckmail_token = duckmail_get_token(account.email, account.mail_password, args.duckmail_api_base)
        seen_duckmail_ids = snapshot_duckmail_message_ids(duckmail_token, args.duckmail_api_base)
        log(f"DuckMail token is ready; existing messages: {len(seen_duckmail_ids)}")

    chrome_process = None
    profile_dir = None
    browser = None
    playwright = None

    try:
        port = find_free_port()
        chrome_process, profile_dir = launch_chrome(
            chrome_path,
            port,
            DEFAULT_MANAGEMENT_PAGE,
            headless=args.headless,
        )
        wait_for_cdp(port)

        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if not browser.contexts:
            raise RuntimeError("No Chrome context found after connecting over CDP")
        context = browser.contexts[0]

        management_page = None
        deadline = time.time() + 30
        while time.time() < deadline and management_page is None:
            for page in context.pages:
                if "management.html" in page.url or page.url in {"about:blank", ""}:
                    management_page = page
                    break
            if management_page is None:
                time.sleep(0.2)
        if management_page is None:
            management_page = context.new_page()
            management_page.goto(DEFAULT_MANAGEMENT_PAGE, wait_until="domcontentloaded", timeout=30000)
        else:
            management_page.bring_to_front()
            if management_page.url in {"about:blank", ""}:
                management_page.goto(DEFAULT_MANAGEMENT_PAGE, wait_until="domcontentloaded", timeout=30000)

        ensure_management_login(management_page, args.management_key)
        open_oauth_page(management_page)
        log(f"Prepared browser and management OAuth page: line {account.line_number} / {mask_email(account.email)}")
        log(f"Waiting for exclusive Codex OAuth slot: line {account.line_number} / {mask_email(account.email)}")

        with _OAUTH_SLOT_LOCK:
            log(f"Acquired exclusive Codex OAuth slot: line {account.line_number} / {mask_email(account.email)}")
            try:
                return run_exclusive_oauth_flow(
                    args=args,
                    account=account,
                    management_page=management_page,
                    context=context,
                    auth_dir=auth_dir,
                    duckmail_token=duckmail_token,
                    seen_duckmail_ids=seen_duckmail_ids,
                )
            finally:
                log(f"Released exclusive Codex OAuth slot: line {account.line_number} / {mask_email(account.email)}")
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        if not args.keep_browser and chrome_process is not None:
            try:
                chrome_process.terminate()
            except Exception:
                pass
        if profile_dir and profile_dir.exists():
            for _ in range(20):
                try:
                    shutil.rmtree(profile_dir)
                    break
                except Exception:
                    time.sleep(0.25)


def process_account_task(
    args: argparse.Namespace,
    account: Account,
    chrome_path: Path,
    active_limit: int,
) -> tuple[Account, Path | None]:
    mark_task_started(account, active_limit)
    try:
        newest_auth = run_account_flow(args, account, chrome_path)
        return account, newest_auth
    finally:
        mark_task_finished(active_limit)


def main() -> int:
    args = parse_args()
    accounts_file = Path(args.accounts_file)
    accounts = parse_accounts(accounts_file)
    selected_accounts = select_accounts(accounts, args.account_line)
    chrome_path = resolve_chrome_path(args.chrome_path)
    total_accounts = len(accounts)
    skipped_processed_count = sum(1 for account in accounts if account.processed)
    skipped_failed_count = sum(1 for account in accounts if account.failed)
    log(f"Accounts file: {args.accounts_file}")
    if not selected_accounts:
        log("No unprocessed accounts found. Nothing to do.")
        print_batch_summary(
            total_accounts=total_accounts,
            selected_count=0,
            success_count=0,
            failure_count=0,
            skipped_processed_count=skipped_processed_count,
            skipped_failed_count=skipped_failed_count,
        )
        return 0

    log(f"Accounts selected this run: {len(selected_accounts)}")
    failures: list[str] = []
    success_count = 0
    max_workers = prompt_parallel_workers(args.max_workers, len(selected_accounts))
    log(f"Parallel workers: {max_workers}")
    reset_active_task_counter()

    future_to_account = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="acct") as executor:
        for index, account in enumerate(selected_accounts, start=1):
            log(f"Queueing [{index}/{len(selected_accounts)}] line {account.line_number}: {mask_email(account.email)}")
            future = executor.submit(process_account_task, args, account, chrome_path, max_workers)
            future_to_account[future] = account

        stop_after_failure = False
        for future in as_completed(future_to_account):
            account = future_to_account[future]
            try:
                _, newest_auth = future.result()
                mark_account_processed(accounts_file, account)
                log(f"Marked processed: line {account.line_number}")
                if newest_auth:
                    log(f"Completed line {account.line_number} with auth file: {newest_auth}")
                success_count += 1
            except Exception as exc:
                log(f"FAILED line {account.line_number} ({mask_email(account.email)}): {exc}")
                mark_account_failed(accounts_file, account)
                log(f"Marked failed: line {account.line_number}")
                failures.append(account.email)
                if args.stop_on_error:
                    stop_after_failure = True
                    break

        if stop_after_failure:
            for future, account in future_to_account.items():
                if future.done():
                    continue
                cancelled = future.cancel()
                if cancelled:
                    log(f"Cancelled pending line {account.line_number}: {mask_email(account.email)}")

    print_batch_summary(
        total_accounts=total_accounts,
        selected_count=len(selected_accounts),
        success_count=success_count,
        failure_count=len(failures),
        skipped_processed_count=skipped_processed_count,
        skipped_failed_count=skipped_failed_count,
    )

    if failures:
        log(f"Completed with failures: {len(failures)}")
        return 1

    log("All selected accounts completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
