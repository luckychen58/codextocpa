#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fast Codex OAuth runner.

This wrapper reuses the main OAuth automation flow but removes the
"human-like" pauses and per-character typing delays so inputs happen
as quickly as possible.

If ``--max-workers`` is omitted, the shared main flow will prompt in the
console for a parallel worker count between 1 and 10.
"""

from __future__ import annotations

import sys

import oauth_incognito_browser_login as base


def fast_human_pause(*_args, **_kwargs) -> None:
    return None


def fast_human_click(locator, description: str) -> None:
    locator.wait_for(state="visible", timeout=30000)
    locator.scroll_into_view_if_needed(timeout=30000)
    locator.click(timeout=30000)
    base.log(f"Clicked instantly: {description}")


def fast_clear_and_type(locator, value: str, description: str) -> None:
    locator.wait_for(state="visible", timeout=30000)
    locator.scroll_into_view_if_needed(timeout=30000)
    try:
        locator.click(timeout=30000)
    except Exception:
        pass

    try:
        locator.fill(value, timeout=30000)
    except Exception:
        try:
            locator.press("Control+A")
            locator.press("Backspace")
        except Exception:
            pass
        locator.type(value, delay=0)

    base.log(f"Filled instantly: {description}")


def enable_fast_mode() -> None:
    base.human_pause = fast_human_pause
    base.human_click = fast_human_click
    base.clear_and_type = fast_clear_and_type
    base.log("FAST mode enabled")


def main() -> int:
    enable_fast_mode()
    if "--max-workers" not in sys.argv[1:]:
        base.log("FAST mode: 未传 --max-workers，运行时会在控制台提示输入并发数(最大 10 个)")
    return base.main()


if __name__ == "__main__":
    sys.exit(main())
