"""Paths that are asked about even when the operation only reads.

Adaptive permission turns on a distinction between what a call does and what it
touches. Reading a file is harmless in general and not harmless at all when the
file is a private key, and a permission model that only looked at the verb
would hand over ~/.ssh/id_rsa without a word. So the verb decides whether a
call can be safe and this decides whether this particular one is.

Matched on the resolved string, so ../ and ~ cannot walk around a rule, and
matched loosely: a false positive costs one prompt the user would probably have
clicked through, a false negative costs a secret.
"""
import re
from pathlib import Path

from tools._safety import rules

PATTERNS = (
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.gnupg(/|$)",
    r"(^|/)\.aws(/|$)",
    r"(^|/)\.kube(/|$)",
    r"(^|/)\.docker/config\.json$",
    r"(^|/)\.config/gh(/|$)",
    r"(^|/)\.env($|\.)",
    r"(^|/)\.netrc$",
    r"(^|/)\.npmrc$",
    r"(^|/)\.pypirc$",
    r"(^|/)\.git-credentials$",
    # The agent's own config, which holds the API keys in plaintext.
    r"(^|/)\.cuacode/config\.json$",
    r"id_(rsa|dsa|ecdsa|ed25519)",
    r"\.(pem|key|p12|pfx|keystore|jks)$",
    r"(^|/)(credentials?|secrets?|token|password|passwd)(\.[a-z0-9]+)?$",
    r"(^|/)Library/Keychains(/|$)",
    r"(^|/)etc/(shadow|sudoers)",
)

_RX = tuple(re.compile(p, re.IGNORECASE) for p in PATTERNS)

def is_sensitive(path) -> bool:
    s = str(path)
    try: s = str(Path(s).expanduser())
    except (OSError, ValueError, RuntimeError): pass
    if any(r.search(s) for r in _RX): return True
    return any(extra and (extra in s) for extra in rules.extra("sensitive_paths"))
