"""A Model Context Protocol client: JSON-RPC 2.0, both eras of the protocol.

Small on purpose. MCP's stdio transport is newline-delimited JSON in both
directions, and neither era's opening is more than a request or two. That is
little enough to own outright rather than take a dependency for, and owning it
keeps CuaCode installable with the requirements.txt it already has.

Two eras, because the protocol split at revision 2026-07-28. The **legacy**
era (2025-11-25 and earlier) opens with an `initialize` handshake and then
treats the connection as a session -- on HTTP, one the server names with an
`Mcp-Session-Id` header. The **modern** era is stateless: no handshake, no
session header, and every request carries its own protocol version, client
identity and capabilities in `_meta`. State that has to outlive a call is a
handle the server mints and the caller passes back as an ordinary argument.

A connection picks its era once, at startup, by probing with `server/discover`
and falling back to `initialize` on anything that is not a recognised modern
answer -- the procedure the spec prescribes. So a server written years apart
from its neighbour still works with no note in its config. `"protocol"` in a
server's config forces the choice when the probe is unwelcome or too slow.

Connections are kept, not made per call. Starting a server costs an interpreter
launch, and a model that reads the current track twice in a conversation should
pay for that once. `pool()` hands back a live connection or starts one, and
everything is torn down at exit.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextvars import ContextVar

PROTOCOL = "2025-06-18"             # legacy era: the version `initialize` asks for
MODERN = "2026-07-28"               # modern era: the version the probe asks for
MODERN_VERSIONS = ("2026-07-28",)   # every stateless revision this client speaks
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "cuacode", "version": "1.0.0"}

META = "io.modelcontextprotocol/"   # the reserved `_meta` key prefix
# HeaderMismatch, MissingRequiredClientCapability, UnsupportedProtocolVersion.
# Getting one of these back means the server is modern, whatever else went
# wrong -- a legacy server has no way to produce them.
MODERN_ERRORS = {-32020, -32021, -32022}

START_TIMEOUT = 20          # interpreter launch + handshake
PROBE_TIMEOUT = 3           # server/discover, which a legacy server may never answer
CALL_TIMEOUT = 60           # a tool that drives an app can be slow; a hung one must not be forever
STDERR_KEEP = 40            # lines, kept only to explain a server that died


class MCPError(RuntimeError):
    """Anything that stops a call: server missing, dead, slow, or protocol-level.

    `code` and `data` are the JSON-RPC error's own when the server sent one,
    and how the startup probe tells a modern server from a legacy one.
    `status` is the HTTP status when there was one -- its presence is how the
    probe tells "the server answered and said no" from "nothing answered yet".
    """

    def __init__(self, message: str, code: int | None = None,
                 data: dict | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.data = data if isinstance(data, dict) else {}
        self.status = status


class _Legacy(Exception):
    """Internal: the probe decided this server predates the stateless era."""


# --------------------------------------------------------------------------
# HTTP request metadata (modern era)
# --------------------------------------------------------------------------
# Streamable HTTP mirrors selected body fields into headers so a load balancer
# can route without parsing the body, and the server rejects the request if the
# two disagree. Everything below exists to make the mirror exact.

_TCHAR = re.compile(r"^[-!#$%&'*+.^_`|~0-9A-Za-z]+$")   # RFC 9110 field-name token
_SENTINEL = re.compile(r"^=\?base64\?.*\?=$", re.S)
_PRIMITIVE = {"string", "integer", "boolean"}           # `number` is not permitted
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs",
                "definitions", "dependentSchemas")
_INSTANCE_KEYS = ("enum", "const", "default", "examples")


def _header_value(value) -> str:
    """A parameter value as a header value, Base64-wrapped when it cannot be
    carried as plain visible ASCII."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    safe = (text == text.strip()
            and all("\x20" <= ch <= "\x7e" for ch in text)
            and not _SENTINEL.match(text))
    if safe:
        return text
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def _mcp_name(method: str, params: dict) -> str | None:
    """The body field the `Mcp-Name` header mirrors, for the methods that have one."""
    if method in ("tools/call", "prompts/get"):
        value = params.get("name")
    elif method == "resources/read":
        value = params.get("uri")
    else:
        return None
    return None if value is None else str(value)


def _annotated(schema: dict, path: tuple, out: list):
    """Every `x-mcp-header` reachable from the schema root through `properties`
    alone -- the only place the spec allows one."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    for key, sub in props.items():
        if not isinstance(sub, dict):
            continue
        if "x-mcp-header" in sub:
            out.append(((*path, key), sub))
        _annotated(sub, (*path, key), out)


def _anywhere(node, out: list, is_schema: bool = True):
    """Every `x-mcp-header` in the schema at all, reachable or not. An
    annotation the client cannot reach statically invalidates the tool, so the
    two walks have to be compared rather than just the first one trusted."""
    if isinstance(node, dict):
        if is_schema and "x-mcp-header" in node:
            out.append(id(node))
        for key, value in node.items():
            if key in _INSTANCE_KEYS:
                continue                            # instance values, not schemas
            if key in _SCHEMA_MAPS:
                if isinstance(value, dict):
                    for sub in value.values():
                        _anywhere(sub, out, True)
                continue
            _anywhere(value, out, is_schema)
    elif isinstance(node, list):
        for value in node:
            _anywhere(value, out, is_schema)


def _header_params(schema) -> list[tuple[tuple, str]] | None:
    """`(property path, header name)` for one tool, or None if the tool breaks
    the rules and must be dropped from the list rather than called wrongly."""
    if not isinstance(schema, dict):
        return []
    found: list = []
    _annotated(schema, (), found)
    everywhere: list = []
    _anywhere(schema, everywhere)
    if len(everywhere) != len(found):
        return None                                 # annotated somewhere unreachable
    out, seen = [], set()
    for path, sub in found:
        name = sub.get("x-mcp-header")
        if not isinstance(name, str) or not name or not _TCHAR.match(name):
            return None
        if name.lower() in seen:
            return None                             # names are case-insensitively unique
        if sub.get("type") not in _PRIMITIVE:
            return None
        seen.add(name.lower())
        out.append((path, name))
    return out


def _at_path(arguments: dict, path: tuple):
    node = arguments
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


class Connection:
    """One running MCP server, and the conversation with it."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str = ""
        self.protocol: str = ""
        self.era: str = ""                    # "modern" or "legacy", decided at startup
        self._tools: list | None = None
        self._tools_at = 0.0
        self._tools_ttl: float | None = None  # seconds, from the server's own ttlMs
        self._headers: dict[str, list] = {}   # tool name -> its x-mcp-header mirrors
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._stderr = deque(maxlen=STDERR_KEEP)
        self.transport = config.get("transport", "stdio")
        self.url = config.get("url")
        self._session_id: str | None = None   # server-assigned on initialize (legacy HTTP)

    # -- lifecycle ---------------------------------------------------------

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _argv(self) -> list[str]:
        command = self.config.get("command")
        if not command:
            raise MCPError(f"{self.name}: no command in its config")
        # `python3` in a config file is whatever the shell would find, which on
        # a Mac with several installs is a coin toss. The interpreter already
        # running CuaCode is one we know exists.
        if command in ("python", "python3"):
            command = sys.executable
        args = [os.path.expanduser(str(a)) for a in (self.config.get("args") or [])]
        return [command, *args]

    def start(self):
        if self.alive():
            return
        env = {**os.environ, **{str(k): str(v) for k, v in (self.config.get("env") or {}).items()}}
        cwd = self.config.get("cwd")
        argv = self._argv()
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env,
                cwd=os.path.expanduser(cwd) if cwd else None)
        except FileNotFoundError:
            raise MCPError(f"{self.name}: cannot run {argv[0]!r}")
        except OSError as exc:
            raise MCPError(f"{self.name}: {exc}")

        threading.Thread(target=self._read_stderr, daemon=True).start()
        if self.transport != "http":
            threading.Thread(target=self._read_stdout, daemon=True).start()
        try:
            self._handshake()
        except MCPError:
            self.close()
            raise

    def _handshake(self):
        """Which era this server speaks, decided once and kept for its lifetime.

        `protocol` in the config forces the answer, either by era or by naming
        a revision: `legacy` to skip a probe a server will never answer, and
        `modern` to refuse the fallback rather than quietly speak an old
        protocol to a server that was meant to have stopped offering one.
        """
        want = str(self.config.get("protocol") or "auto").lower()
        version = MODERN
        if want in MODERN_VERSIONS:
            version, want = want, "modern"
        elif want in LEGACY_VERSIONS:
            want = "legacy"
        if want == "legacy":
            self._legacy_handshake()
            return
        try:
            self._discover(version, set())
        except _Legacy as fallback:
            if want == "modern":
                raise MCPError(f"{self.name}: configured for the stateless protocol, but "
                               f"server/discover did not answer as a {version} server would "
                               f"({fallback.args[0] if fallback.args else 'no answer'})")
            self._legacy_handshake()

    def _discover(self, version: str, tried: set):
        """The modern opening: ask what the server speaks, and speak it.

        Anything that is not a DiscoverResult or a recognised modern error means
        this is a server from before the stateless era, which is `_Legacy`.
        """
        tried.add(version)
        deadline = time.time() + START_TIMEOUT
        while True:
            if not self.alive():
                raise MCPError(f"{self.name}: server exited during startup{self._why()}")
            try:
                result = self.request("server/discover", {},
                                      timeout=PROBE_TIMEOUT, version=version)
                break
            except MCPError as exc:
                if exc.code in MODERN_ERRORS:
                    return self._unsupported(exc, version, tried)
                # No answer at all from an HTTP endpoint is a server still
                # coming up, not a verdict. Anything the server actually said
                # is a verdict, and it says legacy.
                if (self.transport == "http" and exc.status is None
                        and exc.code is None and time.time() < deadline):
                    time.sleep(0.2)
                    continue
                raise _Legacy(str(exc))

        versions = [v for v in (result.get("supportedVersions") or []) if isinstance(v, str)]
        chosen = next((v for v in MODERN_VERSIONS if v in versions), None)
        if chosen is None:
            if not versions:
                chosen = version            # answered the probe but named nothing: take its word
            elif any(v in LEGACY_VERSIONS for v in versions):
                raise _Legacy(f"speaks only {', '.join(versions)}")
            else:
                raise MCPError(f"{self.name}: server speaks {', '.join(versions)}; "
                               f"CuaCode speaks {MODERN} and {PROTOCOL}")
        self.era = "modern"
        self.protocol = chosen
        self.capabilities = result.get("capabilities") or {}
        self.instructions = result.get("instructions") or ""
        self.server_info = (result.get("_meta") or {}).get(f"{META}serverInfo") or {}

    def _unsupported(self, exc: MCPError, version: str, tried: set):
        """A modern error came back. The server is modern; the version was wrong."""
        if exc.code != -32022:
            raise exc                       # our request was malformed, not our version
        supported = [v for v in (exc.data.get("supported") or []) if isinstance(v, str)]
        chosen = next((v for v in MODERN_VERSIONS if v in supported and v not in tried), None)
        if chosen:
            return self._discover(chosen, tried)
        if any(v in LEGACY_VERSIONS for v in supported):
            raise _Legacy(f"rejected {version}, offers {', '.join(supported)}")
        raise MCPError(f"{self.name}: server rejected {version} and offers "
                       f"{', '.join(supported) or 'nothing this client speaks'}")

    def _legacy_handshake(self):
        """The opening every revision through 2025-11-25 used: one `initialize`,
        one `notifications/initialized`, and a session for the rest of the
        connection's life."""
        if self.transport == "http":
            deadline = time.time() + START_TIMEOUT
            while True:
                if not self.alive():
                    raise MCPError(f"{self.name}: server exited during startup{self._why()}")
                try:
                    hello = self._http_request("initialize", {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {},
                        "clientInfo": CLIENT_INFO,
                    }, timeout=5, version=PROTOCOL)
                    break
                except MCPError as exc:
                    # Retry only while nothing has answered; a server that
                    # replied has given its answer.
                    if exc.status is not None or exc.code is not None or time.time() > deadline:
                        raise
                    time.sleep(0.2)
        else:
            hello = self.request("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            }, timeout=START_TIMEOUT, version=PROTOCOL)

        self.era = "legacy"
        self.server_info = hello.get("serverInfo") or {}
        self.capabilities = hello.get("capabilities") or {}
        self.protocol = hello.get("protocolVersion") or ""
        self.instructions = hello.get("instructions") or ""
        if self.transport == "http":
            self._http_request("notifications/initialized", {})
        else:
            self._notify("notifications/initialized", {})

    def close(self):
        proc, self.proc = self.proc, None
        self._tools = None
        self._tools_ttl = None
        self._headers = {}
        self._session_id = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:                                       # noqa: BLE001
            proc.kill()

    # -- wire --------------------------------------------------------------

    def _read_stdout(self):
        """One reader thread, so a notification arriving between a request and
        its response cannot be mistaken for that response."""
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._stderr.append(f"[non-JSON on stdout] {line[:200]}")
                    continue
                mid = message.get("id")
                if message.get("method") and mid is not None:
                    # A server asking us for something -- sampling, roots. We
                    # advertised no capabilities, so the honest reply is a
                    # refusal rather than a silence it waits on. A modern server
                    # never does this: it asks inside a result instead.
                    self._send({"jsonrpc": "2.0", "id": mid,
                                "error": {"code": -32601, "message": "client has no such capability"}})
                    continue
                if mid is None:
                    continue                                    # server notification: nothing to route
                with self._lock:
                    self._pending[mid] = message
                    event = self._events.get(mid)
                if event:
                    event.set()
        except Exception:                                       # noqa: BLE001
            pass
        finally:
            # Whatever is still waiting will never be answered now.
            with self._lock:
                events = list(self._events.values())
            for event in events:
                event.set()

    def _read_stderr(self):
        proc = self.proc
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip())
        except Exception:                                       # noqa: BLE001
            pass

    def _send(self, message: dict):
        proc = self.proc
        if not proc or proc.poll() is not None:
            raise MCPError(f"{self.name}: server is not running{self._why()}")
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise MCPError(f"{self.name}: server closed its input{self._why()}")

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _why(self) -> str:
        """The server's own last words, when it left any."""
        tail = [l for l in self._stderr if l.strip()]
        return f" -- stderr: {' | '.join(tail[-3:])}" if tail else ""

    def _new_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _version(self, version: str | None) -> tuple[str, bool]:
        """The version a request declares, and whether that is a stateless one."""
        chosen = version or self.protocol or PROTOCOL
        return chosen, chosen in MODERN_VERSIONS

    def _with_meta(self, params: dict, version: str) -> dict:
        """The per-request protocol fields a stateless server has instead of a
        handshake. Required on every request; a server that does not get them
        rejects the request as malformed."""
        meta = dict(params.get("_meta") or {})
        meta.setdefault(f"{META}protocolVersion", version)
        meta.setdefault(f"{META}clientInfo", CLIENT_INFO)
        meta.setdefault(f"{META}clientCapabilities", {})
        return {**params, "_meta": meta}

    def _result(self, message: dict, method: str) -> dict:
        if "error" in message:
            err = message.get("error") or {}
            raise MCPError(f"{self.name}: {err.get('message', 'error')} (code {err.get('code')})",
                           code=err.get("code"), data=err.get("data"))
        result = message.get("result") or {}
        # Absent means "complete": that is what an older server's results mean,
        # and the spec says to read them that way.
        kind = result.get("resultType", "complete")
        if kind != "complete":
            raise MCPError(f"{self.name}: {method} came back as {kind!r} -- the server wants "
                           "more input mid-call, which this client does not carry out")
        return result

    def _read_sse(self, resp, want_id: int, method: str) -> dict:
        """An SSE response stream, read down to the response for this request.

        A modern server may answer any request this way, sending progress and
        log notifications first. Everything that is not the answer is dropped:
        nothing here subscribes to anything.
        """
        answer = None
        data: list[str] = []

        def flush():
            if not data:
                return None
            body = "\n".join(data)
            data.clear()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                self._stderr.append(f"[non-JSON in SSE] {body[:200]}")
                return None

        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                message = flush()
                if message is not None and message.get("id") == want_id:
                    answer = message
                    break
                continue
            if line.startswith(":"):                            # comment: a keep-alive
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
        if answer is None:
            message = flush()
            if message is not None and message.get("id") == want_id:
                answer = message
        if answer is None:
            raise MCPError(f"{self.name}: the response stream for {method} closed "
                           f"before it answered{self._why()}")
        return answer

    def _http_request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT,
                      session: str | None = None, version: str | None = None,
                      headers: dict | None = None) -> dict:
        version, modern = self._version(version)
        mid = self._new_id()
        if modern:
            params = self._with_meta(params, version)
        body = json.dumps({"jsonrpc": "2.0", "id": mid,
                           "method": method, "params": params}).encode()

        head = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if modern:
            # These mirror the body, and the server rejects the request with
            # HeaderMismatch if they do not.
            head["MCP-Protocol-Version"] = version
            head["Mcp-Method"] = method
            name = _mcp_name(method, params)
            if name is not None:
                head["Mcp-Name"] = _header_value(name)
            head.update(headers or {})
        else:
            if self.protocol:       # required from 2025-06-18 on, once negotiated
                head["MCP-Protocol-Version"] = self.protocol
            sid = session or self._session_id
            if sid:
                head["Mcp-Session-Id"] = sid

        req = urllib.request.Request(self.url, data=body, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not modern and not self._session_id:
                    self._session_id = resp.headers.get("Mcp-Session-Id")
                if resp.status == 202:
                    return {}                   # a notification: nothing to answer
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype == "text/event-stream":
                    message = self._read_sse(resp, mid, method)
                else:
                    raw = resp.read()
                    if not raw or not raw.strip():
                        return {}
                    message = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc)
        except OSError as exc:
            raise MCPError(f"{self.name}: {exc}")
        except json.JSONDecodeError as exc:
            raise MCPError(f"{self.name}: unreadable response to {method}: {exc}")
        return self._result(message, method)

    def _http_error(self, exc: urllib.error.HTTPError) -> MCPError:
        """An HTTP failure, with the JSON-RPC error inside it when there is one.

        Reading the body matters: a modern server answers an unsupported
        version, a missing capability or a header mismatch with 400 and a
        JSON-RPC error, and telling that apart from a legacy server's 400 is
        the whole of the HTTP era probe.
        """
        try:
            raw = exc.read()
        except Exception:                                       # noqa: BLE001
            raw = b""
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            message = None
        if isinstance(message, dict) and isinstance(message.get("error"), dict):
            err = message["error"]
            return MCPError(f"{self.name}: {err.get('message', 'error')} (code {err.get('code')})",
                            code=err.get("code"), data=err.get("data"), status=exc.code)
        return MCPError(f"{self.name}: HTTP {exc.code}: {raw[:200]}", status=exc.code)

    def _stdio_request(self, method: str, params: dict, timeout: float,
                       version: str | None) -> dict:
        version, modern = self._version(version)
        if modern:
            params = self._with_meta(params, version)
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            event = threading.Event()
            self._events[mid] = event
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            if not event.wait(timeout):
                raise MCPError(f"{self.name}: {method} timed out after {timeout}s")
            with self._lock:
                message = self._pending.pop(mid, None)
            if message is None:
                raise MCPError(f"{self.name}: server died during {method}{self._why()}")
            return self._result(message, method)
        finally:
            with self._lock:
                self._events.pop(mid, None)
                self._pending.pop(mid, None)

    def request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT,
                session: str | None = None, version: str | None = None,
                headers: dict | None = None) -> dict:
        if self.transport == "http":
            return self._http_request(method, params, timeout=timeout, session=session,
                                      version=version, headers=headers)
        return self._stdio_request(method, params, timeout=timeout, version=version)

    # -- MCP -------------------------------------------------------------

    def _fresh(self) -> bool:
        if self._tools is None:
            return False
        if self._tools_ttl is None:
            return True
        return (time.time() - self._tools_at) < self._tools_ttl

    def list_tools(self, refresh: bool = False) -> list:
        if not refresh and self._fresh():
            return self._tools
        self.start()
        tools, cursor, ttl = [], None, None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(page.get("tools") or [])
            ttl = page.get("ttlMs")
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self._tools = self._mirrors(tools)
        self._tools_at = time.time()
        # ttlMs is the server's own freshness hint. Without one the list is kept
        # for the life of the connection, which is what this client always did.
        self._tools_ttl = ttl / 1000.0 if isinstance(ttl, (int, float)) and ttl > 0 else None
        return self._tools

    def _mirrors(self, tools: list) -> list:
        """Work out each tool's `x-mcp-header` mirrors, and drop the tools whose
        annotations break the rules -- a malformed one must not take the rest of
        the server's tools down with it, and must not be called either."""
        self._headers = {}
        if self.era != "modern" or self.transport != "http":
            return tools
        kept = []
        for tool in tools:
            name = tool.get("name")
            params = _header_params(tool.get("inputSchema"))
            if params is None:
                print(f"[mcp] {self.name}: dropping tool {name!r} -- invalid x-mcp-header "
                      "annotation in its input schema", file=sys.stderr)
                continue
            if params:
                self._headers[name] = params
            kept.append(tool)
        return kept

    def _param_headers(self, tool: str, arguments: dict) -> dict:
        out = {}
        for path, header in self._headers.get(tool, ()):
            value = _at_path(arguments, path)
            if value is None:
                continue                    # absent or null: the header is omitted
            out[f"Mcp-Param-{header}"] = _header_value(value)
        return out

    def call(self, tool: str, arguments: dict | None = None, timeout: float = CALL_TIMEOUT,
             session: str | None = None) -> dict:
        self.start()
        arguments = arguments or {}
        headers = None
        if self.era == "modern" and self.transport == "http":
            if self._tools is None:
                self.list_tools()           # the mirrors live in the tool schemas
            headers = self._param_headers(tool, arguments)
        result = self.request("tools/call", {"name": tool, "arguments": arguments},
                              timeout=timeout, session=session, headers=headers)
        return _flatten(result)

    def has_tool(self, name: str) -> bool:
        try:
            return any(t.get("name") == name for t in self.list_tools())
        except MCPError:
            return False

    def tool_schema(self, name: str) -> dict:
        for tool in (self._tools or ()):
            if tool.get("name") == name:
                return tool.get("inputSchema") or {}
        return {}


def _flatten(result: dict) -> dict:
    """MCP's content blocks, turned into something a tool result can hold.

    A server returns a list of typed blocks; almost always one block of text,
    and for these servers that text is JSON. Parsing it here means the model
    gets an object instead of a string containing an object.
    """
    blocks = result.get("content") or []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    other = [b.get("type") for b in blocks if isinstance(b, dict) and b.get("type") != "text"]
    joined = "\n".join(t for t in texts if t)

    out = {"is_error": bool(result.get("isError"))}
    # One of data or text, never both: they are the same bytes twice, and a
    # tool result that says everything twice is paid for on every later turn.
    if result.get("structuredContent"):
        out["data"] = result["structuredContent"]
    elif joined:
        try:
            out["data"] = json.loads(joined)
        except (json.JSONDecodeError, ValueError):
            out["text"] = joined
    if other:
        out["other_content"] = other
    return out


# --------------------------------------------------------------------------
# Pool
# --------------------------------------------------------------------------

_pool: dict[str, Connection] = {}
_pool_lock = threading.Lock()


def pool(name: str, config: dict) -> Connection:
    """The live connection for this server, started if it is not up yet.

    A server whose config changed since it was started is replaced rather than
    reused -- the old process is running the old command.
    """
    with _pool_lock:
        conn = _pool.get(name)
        if conn and (not conn.alive() or conn.config != config):
            conn.close()
            conn = None
        if conn is None:
            conn = Connection(name, config)
            _pool[name] = conn
    conn.start()
    return conn


def connected() -> dict[str, Connection]:
    with _pool_lock:
        return {n: c for n, c in _pool.items() if c.alive()}


def close(name: str) -> bool:
    with _pool_lock:
        conn = _pool.pop(name, None)
    if not conn:
        return False
    conn.close()
    return True


def close_all():
    with _pool_lock:
        conns = list(_pool.values())
        _pool.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:                                       # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Session scope
# --------------------------------------------------------------------------
# A ContextVar, not a field on the connection: the same server process serves
# many logical sessions, and which one a call belongs to is a property of the
# run making it, not of the connection. The subagent runner sets a fresh scope
# per subagent run, so parallel subagents each get their own browser session
# instead of sharing one page and stomping on each other. The top-level
# conversation leaves it None and uses the server's default session, matching
# the old stdio behaviour.
#
# How the id travels depends on the era. Legacy HTTP servers route by the
# `Mcp-Session-Id` header (lightpanda's transport does). The stateless era has
# no such header and no protocol sessions at all: a server that needs state
# across calls mints a handle and expects it back as an ordinary tool argument,
# so the same scope dict carries a handle instead of a header value.

_mcp_sessions: ContextVar[dict | None] = ContextVar("mcp_sessions", default=None)


def session_scope() -> dict | None:
    return _mcp_sessions.get()


def set_session_scope(scope: dict):
    return _mcp_sessions.set(scope)


def reset_session_scope(token):
    _mcp_sessions.reset(token)


atexit.register(close_all)
