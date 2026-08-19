"""AppKit, with the worker kept out of the Dock.

The first call into AppKit registers the process with LaunchServices, and the
framework Python this runs under carries no LSUIElement -- so the worker Go
spawned came up as a *regular* app: a rocket-ship Dock tile named "Python",
sitting next to the frontend, whose Quit killed the session out from under it.

Prohibited is the right policy for a process that drives other apps and owns no
windows of its own. No tile, no menu bar, and it can never take focus away from
the app being driven -- which the screenshot path depends on anyway.

Every AppKit user goes through here rather than importing it directly, because
the policy has to be set on the first connection: set late, the tile appears
and is then withdrawn, which is the flicker this exists to avoid.
"""

_APPKIT = None


def appkit():
    """The AppKit module, policy already set. Raises if pyobjc is missing."""
    global _APPKIT
    if _APPKIT is None:
        import AppKit
        try:
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyProhibited)
        except Exception:
            # An older pyobjc without the constant, or a policy change refused.
            # A Dock icon is worth less than the capture that came for it.
            pass
        _APPKIT = AppKit
    return _APPKIT
