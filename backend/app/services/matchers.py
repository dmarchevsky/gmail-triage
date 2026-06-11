"""Sender pattern matching shared by the ignore list, hard rules, and rules."""

import fnmatch
import re
from email.utils import parseaddr


def sender_matches(patterns: list[str], sender: str) -> bool:
    """Glob or regex match against the full From header and the bare address."""
    sender_l = (sender or "").lower()
    _, addr = parseaddr(sender or "")
    candidates = {sender_l, addr.lower()}
    for pattern in patterns:
        p = pattern.lower().strip()
        if not p:
            continue
        if any(fnmatch.fnmatch(c, p) for c in candidates) or p in sender_l:
            return True
        try:
            if re.search(pattern, sender, re.IGNORECASE):
                return True
        except re.error:
            pass
    return False
