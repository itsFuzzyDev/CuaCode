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

# ---- scratch space ----

# Mirrors handler.session.Session.notebook, derived from ctx rather than from a
# session object because a tool is handed the one and not the other. The home
# fallback matches file undo: a frontend that reports no session still gets a
# scratch directory, rather than the exemption quietly existing or not
# depending on who launched the app.
def scratch_dir(ctx) -> Path:
    base = (ctx or {}).get("session_dir") if hasattr(ctx, "get") else getattr(ctx, "session_dir", None)
    return Path(base) / "notebook" if base else Path.home() / ".cuacode" / "notebook"

def is_scratch(path, ctx) -> bool:
    """Whether `path` is inside this session's scratch directory.

    What makes writing there safe is not the verb but the place: nothing under
    it is the user's, it is thrown out with the session, and asking about every
    intermediate file the agent writes to its own workspace trains the user to
    click through prompts that do matter.

    Compared resolved, so a scratch path holding ../ is judged by where it
    lands.
    """
    try:
        d = scratch_dir(ctx).resolve()
        p = Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError): return False
    return p == d or d in p.parents
