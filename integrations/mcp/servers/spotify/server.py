#!/usr/bin/env python3
"""Spotify MCP server -- drives the macOS Spotify desktop app over AppleScript.

No OAuth, no developer account, no network. Everything here goes through
`osascript` to the Spotify app already running on this machine, which is the
only control surface that actually answers: the desktop app's local HTTP API
(port 4371 and friends) does not respond, so nothing below depends on it.

That choice sets the boundaries. AppleScript exposes the player and the track
it is playing, and nothing about where that track came from -- there is no
playlist property in Spotify's scripting dictionary, so `get_current_context`
reports what it can and says plainly what it cannot. Anything needing real
playback context needs the Web API and a developer account.

stdlib only, stdio transport. Run it with any python3:

    python3 server.py --selftest      # talk to it without an MCP client
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys

NAME = "spotify"
VERSION = "1.0.0"

# Sent if the client asks for something we do not know; otherwise we echo the
# client's choice back, which is what the spec asks for when we support it.
PREFERRED_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# Unit separator. AppleScript has no JSON, so a script returns its fields joined
# by one character that will not appear inside a track name.
SEP = "\x1f"
OSA_TIMEOUT = 10


class SpotifyError(RuntimeError):
    """Something the model can act on: app closed, wrong OS, nothing playing."""


# --------------------------------------------------------------------------
# AppleScript
# --------------------------------------------------------------------------

def _clean(stderr: str) -> str:
    """osascript's own wrapping, taken back off.

    Its failures arrive as `execution error: Spotify got an error: ... (-1728)`,
    and the middle is the only part worth handing to a model.
    """
    msg = (stderr or "").strip().splitlines()
    msg = msg[-1] if msg else ""
    for prefix in ("execution error: ", "script error: "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    if msg.endswith(")") and "(-" in msg:
        msg = msg[:msg.rfind("(-")].strip()
    return msg.strip()


def _osa(script: str, timeout: float = OSA_TIMEOUT) -> str:
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SpotifyError("osascript not found -- this server only runs on macOS")
    except subprocess.TimeoutExpired:
        raise SpotifyError(f"osascript timed out after {timeout}s")
    if proc.returncode != 0:
        raise SpotifyError(_clean(proc.stderr) or f"osascript exited {proc.returncode}")
    return proc.stdout.rstrip("\n")


def _require_spotify():
    """Checked before every `tell application "Spotify"`, and in this order.

    `tell` on a closed app launches it. A model asking what is playing should
    get an answer or an error, never a Spotify window it did not ask for, so
    the running check -- which does not launch anything -- comes first.
    """
    if platform.system() != "Darwin":
        raise SpotifyError(f"this server drives the macOS Spotify app; host is {platform.system()}")
    if _osa('application "Spotify" is running').strip().lower() != "true":
        raise SpotifyError("Spotify is not running -- open the Spotify app first")


def _tell(body: str) -> str:
    _require_spotify()
    return _osa(f'tell application "Spotify"\n set d to character id 31\n{body}\nend tell')


def _flag(text: str) -> bool:
    return text.strip().lower() == "true"


def _num(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def _clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_PLAYER_FIELDS = ("player state", "player position", "sound volume",
                  "shuffling", "repeating", "shuffling enabled", "repeating enabled")

_TRACK_FIELDS = ("name of t", "artist of t", "album of t", "album artist of t",
                 "duration of t", "track number of t", "disc number of t",
                 "popularity of t", "id of t", "spotify url of t", "artwork url of t")


def _joined(fields) -> str:
    return " & d & ".join(f"({f} as text)" for f in fields)


def _player() -> dict:
    parts = _tell(f" return {_joined(_PLAYER_FIELDS)}").split(SEP)
    if len(parts) < len(_PLAYER_FIELDS):
        raise SpotifyError(f"unexpected player reply: {parts!r}")
    return {
        "state": parts[0].strip(),
        "position_seconds": round(_num(parts[1]), 3),
        "position": _clock(_num(parts[1])),
        "volume": int(_num(parts[2])),
        "shuffle": _flag(parts[3]),
        "repeat": _flag(parts[4]),
        "shuffle_available": _flag(parts[5]),
        "repeat_available": _flag(parts[6]),
    }


def _track() -> dict | None:
    """None rather than an error when the player is empty.

    A stopped Spotify with nothing loaded fails on `current track`, and that is
    an answer to "what is playing", not a fault.
    """
    try:
        parts = _tell(f" set t to current track\n return {_joined(_TRACK_FIELDS)}").split(SEP)
    except SpotifyError as exc:
        if "current track" in str(exc) or "Can't get" in str(exc):
            return None
        raise
    if len(parts) < len(_TRACK_FIELDS):
        raise SpotifyError(f"unexpected track reply: {parts!r}")
    ms = _num(parts[4])
    return {
        "name": parts[0],
        "artist": parts[1],
        "album": parts[2],
        "album_artist": parts[3],
        "duration_seconds": round(ms / 1000, 3),
        "duration": _clock(ms / 1000),
        "track_number": int(_num(parts[5])),
        "disc_number": int(_num(parts[6])),
        "popularity": int(_num(parts[7])),
        "id": parts[8],
        "url": parts[9],
        "artwork_url": parts[10],
    }


def _snapshot() -> dict:
    player = _player()
    track = _track()
    out = {"player": player, "track": track}
    if track and track["duration_seconds"]:
        out["progress_percent"] = round(
            100 * player["position_seconds"] / track["duration_seconds"], 1)
    return out


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _quote(value: str) -> str:
    """A Python string as an AppleScript literal."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def t_get_current_track(_):
    track = _track()
    if not track:
        return {"playing": False, "message": "Spotify has no track loaded"}
    player = _player()
    return {"playing": player["state"] == "playing", "state": player["state"],
            "position": player["position"], "track": track,
            "summary": f'{track["name"]} - {track["artist"]} ({track["album"]})'}


def t_get_playback_state(_):
    return _snapshot()


def t_get_playback_position(_):
    player, track = _player(), _track()
    out = {"position": player["position"], "position_seconds": player["position_seconds"],
           "state": player["state"]}
    if track:
        out |= {"duration": track["duration"], "duration_seconds": track["duration_seconds"],
                "remaining": _clock(track["duration_seconds"] - player["position_seconds"])}
    return out


def _simple(command: str):
    def run(_):
        _tell(f" {command}")
        return t_get_current_track({})
    return run


def t_playpause(_):
    _tell(" playpause")
    return t_get_current_track({})


def t_set_volume(args):
    level = args.get("level")
    if level is None:
        raise SpotifyError("level is required (0-100)")
    level = max(0, min(100, int(level)))
    _tell(f" set sound volume to {level}")
    return {"volume": _player()["volume"]}


def _nudge(sign: int):
    def run(args):
        step = int(args.get("step", 10))
        level = max(0, min(100, _player()["volume"] + sign * abs(step)))
        _tell(f" set sound volume to {level}")
        return {"volume": _player()["volume"]}
    return run


def t_seek(args):
    position = args.get("position_seconds")
    if position is None:
        raise SpotifyError("position_seconds is required")
    position = max(0.0, float(position))
    track = _track()
    if track and track["duration_seconds"] and position > track["duration_seconds"]:
        raise SpotifyError(
            f'position {position}s is past the end of this track ({track["duration"]})')
    _tell(f" set player position to {position}")
    return t_get_playback_position({})


def _switch(prop: str, available_key: str, label: str):
    """shuffle and repeat differ only in the word, so they are written once.

    `enabled` left out means toggle -- the model usually wants the other state,
    not a state it has to read first.
    """
    def run(args):
        player = _player()
        if not player[available_key]:
            raise SpotifyError(f"{label} is not available for what Spotify is playing now")
        want = args.get("enabled")
        want = (not player[label]) if want is None else bool(want)
        _tell(f" set {prop} to {str(want).lower()}")
        return {label: _player()[label]}
    return run


def t_play_uri(args):
    uri = (args.get("uri") or "").strip()
    if not uri:
        raise SpotifyError("uri is required, e.g. spotify:track:… or spotify:playlist:…")
    if not uri.startswith("spotify:"):
        raise SpotifyError(f"not a Spotify URI: {uri!r}")
    context = (args.get("context_uri") or "").strip()
    if context:
        _tell(f" play track {_quote(uri)} in context {_quote(context)}")
    else:
        _tell(f" play track {_quote(uri)}")
    return t_get_current_track({})


def t_get_current_context(_):
    """The playlist question, answered honestly.

    Spotify's scripting dictionary has no playlist or context property -- the
    app simply does not publish where the current track came from. Saying so
    beats returning the album and letting it be mistaken for the answer.
    """
    track = _track()
    return {
        "available": False,
        "reason": ("Spotify's AppleScript dictionary exposes no playlist or playback "
                   "context. The desktop app publishes the track, not where it came from. "
                   "Real context needs the Spotify Web API (OAuth + developer account)."),
        "closest_available": {"album": track["album"], "album_artist": track["album_artist"],
                              "track_url": track["url"]} if track else None,
    }


NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = [
    {"name": "get_current_track", "handler": t_get_current_track, "inputSchema": NO_ARGS,
     "description": "What Spotify is playing right now: name, artist, album, duration, "
                    "position and a one-line summary. Returns playing:false when the app "
                    "has no track loaded."},
    {"name": "get_playback_state", "handler": t_get_playback_state, "inputSchema": NO_ARGS,
     "description": "Everything at once: player state, position, volume, shuffle, repeat "
                    "and the current track. One call instead of several."},
    {"name": "get_playback_position", "handler": t_get_playback_position, "inputSchema": NO_ARGS,
     "description": "Where the playhead is, with the track's duration and time remaining."},
    {"name": "play", "handler": _simple("play"), "inputSchema": NO_ARGS,
     "description": "Resume playback."},
    {"name": "pause", "handler": _simple("pause"), "inputSchema": NO_ARGS,
     "description": "Pause playback."},
    {"name": "playpause", "handler": t_playpause, "inputSchema": NO_ARGS,
     "description": "Toggle between playing and paused."},
    {"name": "next_track", "handler": _simple("next track"), "inputSchema": NO_ARGS,
     "description": "Skip to the next track and return what started playing."},
    {"name": "previous_track", "handler": _simple("previous track"), "inputSchema": NO_ARGS,
     "description": "Go back to the previous track and return what started playing. Spotify "
                    "restarts the current track first if it is already some way in."},
    {"name": "set_volume", "handler": t_set_volume,
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100,
                                              "description": "Target volume, 0-100."}},
                     "required": ["level"]},
     "description": "Set Spotify's own volume (not the system volume), 0-100."},
    {"name": "volume_up", "handler": _nudge(+1),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"step": {"type": "integer", "minimum": 1, "maximum": 100,
                                             "description": "How much louder. Default 10."}}},
     "description": "Raise Spotify's volume by a step, clamped at 100."},
    {"name": "volume_down", "handler": _nudge(-1),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"step": {"type": "integer", "minimum": 1, "maximum": 100,
                                             "description": "How much quieter. Default 10."}}},
     "description": "Lower Spotify's volume by a step, clamped at 0."},
    {"name": "seek", "handler": t_seek,
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"position_seconds": {"type": "number", "minimum": 0,
                                                         "description": "Seconds from the start of the track."}},
                     "required": ["position_seconds"]},
     "description": "Jump to a position in the current track."},
    {"name": "set_shuffle", "handler": _switch("shuffling", "shuffle_available", "shuffle"),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"enabled": {"type": "boolean",
                                                "description": "Leave this out to toggle."}}},
     "description": "Turn shuffle on or off. Omit `enabled` to flip whatever it is now."},
    {"name": "set_repeat", "handler": _switch("repeating", "repeat_available", "repeat"),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"enabled": {"type": "boolean",
                                                "description": "Leave this out to toggle."}}},
     "description": "Turn repeat on or off. Omit `enabled` to flip whatever it is now."},
    {"name": "get_current_context", "handler": t_get_current_context, "inputSchema": NO_ARGS,
     "description": "The current playlist, if the app exposed one -- it does not. Call this "
                    "to get the reason in words plus the album and track URL, rather than "
                    "guessing that the album is the playlist."},
    {"name": "play_uri", "handler": t_play_uri,
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {
                         "uri": {"type": "string", "description": "spotify:track:… , spotify:album:… , spotify:playlist:…"},
                         "context_uri": {"type": "string",
                                         "description": "Optional playlist or album URI to play the track within."}},
                     "required": ["uri"]},
     "description": "Play a Spotify URI, optionally inside a playlist or album context."},
]

BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

def _write(message: dict):
    """stdout carries protocol and nothing else -- anything else is a parse
    error at the far end. Diagnostics go to stderr."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _reply(mid, result):
    _write({"jsonrpc": "2.0", "id": mid, "result": result})


def _fail(mid, code, message):
    _write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def _text_result(payload, is_error=False) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    tool = BY_NAME.get(name)
    if not tool:
        return _text_result(f"unknown tool: {name!r} (have {', '.join(sorted(BY_NAME))})", True)
    try:
        return _text_result(tool["handler"](params.get("arguments") or {}))
    except SpotifyError as exc:
        # A tool that failed is a result, not a protocol error: the model has to
        # see "Spotify is not running" as something it can fix.
        return _text_result(str(exc), True)
    except Exception as exc:                                    # noqa: BLE001
        return _text_result(f"{type(exc).__name__}: {exc}", True)


def handle(message: dict):
    mid, method = message.get("id"), message.get("method")
    if method is None:
        return                                                  # a response to us; nothing to do
    if mid is None:
        return                                                  # notification: initialized, cancelled

    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        _reply(mid, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else PREFERRED_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions": ("Controls the Spotify desktop app on this Mac over AppleScript. "
                             "Spotify must already be open. No playlist/context is available."),
        })
    elif method == "ping":
        _reply(mid, {})
    elif method == "tools/list":
        _reply(mid, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                               for t in TOOLS]})
    elif method == "tools/call":
        _reply(mid, _call_tool(message.get("params") or {}))
    else:
        _fail(mid, -32601, f"method not found: {method}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(None, -32700, f"parse error: {exc}")
            continue
        try:
            handle(message)
        except Exception as exc:                                # noqa: BLE001
            # One bad request must not take the server down; the client would
            # see a dead pipe instead of a reason.
            print(f"[{NAME}] {type(exc).__name__}: {exc}", file=sys.stderr)
            if message.get("id") is not None:
                _fail(message["id"], -32603, f"internal error: {exc}")


def selftest():
    """Enough of a client to prove the server works, without one."""
    print(f"tools: {len(TOOLS)}")
    for tool in TOOLS:
        print(f"  - {tool['name']}")
    print("\nget_current_track ->")
    print(json.dumps(_call_tool({"name": "get_current_track", "arguments": {}}), indent=2))
    print("\nget_playback_state ->")
    print(json.dumps(_call_tool({"name": "get_playback_state", "arguments": {}}), indent=2))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        try:
            serve()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
