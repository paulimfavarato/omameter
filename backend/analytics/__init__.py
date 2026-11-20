"""Omameter Analytics backend (Phase 1 quota history + Phase 2 local token & cost analytics).

Python 3 standard library only. Local harness scanning is strictly opt-in and
reads only local token counters and session metadata from installed harnesses
(Claude Code, GPT Codex, Antigravity, OpenCode) with zero external services.
"""

from __future__ import annotations
