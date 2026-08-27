// The demo conversations, as a script rather than a fetch: a page that loads its
// own fixture has finished replaying by the time load fires, so a screenshot of
// it is deterministic. See the demo block at the end of app.js.
//
// window.__FIXTURES holds one scenario per key, picked by ?demo=name. Every
// entry is a real worker envelope, replayed through the same window.__cua.push
// the bridge uses, so what the page draws here is what it draws in the app.
// A batch marked "stop": true is where ?demo&stop halts, leaving a batch of
// calls open for a live-state screenshot.
window.__FIXTURES = {
  "default": {
    "note": "One short conversation: thinking, prose with markup, a batch of calls that partly fails, a notice, a dropped connection being retried, and the end of a round. The tool_calls batch is marked stop so ?demo&stop can photograph the amber bar and pending rows.",
    "batches": [
      { "delay": 0, "status": { "State": "idle" }, "events": [ { "state": "ready" } ] },
      {
        "delay": 300,
        "status": { "State": "running" },
        "events": [
          { "state": "user", "token": "open notes and write down the three failing tests", "images": [ { "name": "failing-tests.png" } ] }
        ]
      },
      {
        "delay": 120,
        "status": { "State": "running" },
        "events": [ { "state": "session_title", "data": { "title": "write down the three failing tests", "source": "auto" } } ]
      },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "retry", "data": { "attempt": 1, "of": 4, "secs": 1, "error": "Connection error." } } ]
      },
      {
        "delay": 400,
        "status": { "State": "running", "ThinkTokens": 118, "ThinkTPS": 47.2, "ThinkEst": true, "TPS": 47.2, "TPSEst": true },
        "events": [
          { "state": "thinking", "token": "The user wants Notes open and three test names written into it. I should check what is on screen first rather than assuming Notes is closed - opening an app that is already frontmost costs a round and looks clumsy.\n\nSo: screenshot, then decide." }
        ]
      },
      {
        "delay": 500,
        "status": { "State": "tools", "ThinkTokens": 118, "ThinkTPS": 47.2, "ThinkEst": true, "ContextUsed": 24100, "ContextMax": 200000 },
        "stop": true,
        "events": [
          { "state": "tool_calls", "token": "[{\"name\":\"screenshot\",\"input\":{}},{\"name\":\"app_open\",\"input\":{\"app\":\"Notes\"}}]" }
        ]
      },
      {
        "delay": 700,
        "status": { "State": "tools", "ContextUsed": 26800, "ContextMax": 200000 },
        "events": [
          { "state": "tool_output", "token": "screenshot", "data": { "index": 0, "result": { "result": { "n": 1 } } } }
        ]
      },
      {
        "delay": 600,
        "status": { "State": "tools", "ContextUsed": 27200, "ContextMax": 200000 },
        "events": [
          { "state": "tool_output", "token": "app_open", "data": { "index": 1, "result": { "result": { "ok": true } } } }
        ]
      },
      {
        "delay": 500,
        "status": { "State": "tools", "ContextUsed": 27600, "ContextMax": 200000 },
        "events": [
          { "state": "tool_calls", "token": "[{\"function\":{\"name\":\"click\",\"arguments\":\"{\\\"x\\\":512,\\\"y\\\":288}\"}},{\"function\":{\"name\":\"type_text\",\"arguments\":\"{\\\"text\\\":\\\"TestSessionCancel, TestRoundTrip, TestMenuWrap\\\"}\"}},{\"function\":{\"name\":\"shell\",\"arguments\":\"{\\\"command\\\":\\\"go test ./... -run TestMenuWrap\\\"}\"}}]" }
        ]
      },
      {
        "delay": 650,
        "status": { "State": "tools", "ContextUsed": 28100, "ContextMax": 200000 },
        "events": [
          { "state": "tool_output", "token": "click", "data": { "index": 2, "result": { "result": {} } } },
          { "state": "tool_output", "token": "type_text", "data": { "index": 3, "result": { "result": {} } } }
        ]
      },
      {
        "delay": 900,
        "status": { "State": "tools", "ContextUsed": 29400, "ContextMax": 200000 },
        "events": [
          { "state": "tool_output", "token": "shell", "data": { "index": 4, "result": { "result": { "exit_code": 1, "timeout": false } } } }
        ]
      },
      {
        "delay": 400,
        "status": { "State": "running", "ContextUsed": 29800, "ContextMax": 200000 },
        "events": [
          { "state": "notice", "token": "permission: shell was allowed for this session\n\nThe user granted this once. Do not ask again for shell." }
        ]
      },
      {
        "delay": 500,
        "status": { "State": "running", "ContextUsed": 30200, "ContextMax": 200000, "TPS": 62.4 },
        "events": [
          { "state": "content", "token": "Notes is open and the three names are written down.\n\nOne thing worth flagging: `TestMenuWrap` **still fails** when I ran it to confirm - exit 1, not a flake. The failure is in the wrap column, not the menu:\n\n```go\nif width < nameCol {\n\treturn narrowCalls(b, head)  // never reached at width 80\n}\n```\n\n- the guard uses `<` where the row needs `<=`\n- so an exactly-80-column terminal takes the wide path and overflows\n\nWant me to fix it, or leave it on the list?" }
        ]
      },
      {
        "delay": 300,
        "status": { "State": "done", "ContextUsed": 30900, "ContextMax": 200000, "ThinkTokens": 118, "ThinkTPS": 47.2, "OutTokens": 402, "TPS": 61.8 },
        "events": [ { "state": "done" } ]
      }
    ]
  },

  "long": {
    "note": "A long session: four turns, enough blocks to scroll the feed and keep the rail continuous. Exercises the append-not-render path and the scroller under load.",
    "batches": [
      { "delay": 0, "status": { "State": "idle" }, "events": [ { "state": "ready" } ] },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "does the build pass?" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 64, "ThinkTPS": 40.1, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "Quick check: run the build and read the exit code." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 12000, "ContextMax": 200000 },
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"shell\",\"input\":{\"command\":\"go build ./...\"}}]" } ]
      },
      {
        "delay": 500,
        "status": { "State": "tools", "ContextUsed": 12400, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 0, "result": { "result": { "exit_code": 0, "timeout": false } } } } ]
      },
      {
        "delay": 400,
        "status": { "State": "running", "ContextUsed": 12800, "ContextMax": 200000, "TPS": 55.0 },
        "events": [ { "state": "content", "token": "Build passes, exit 0. Nothing to fix there." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 13000, "ContextMax": 200000, "OutTokens": 12, "TPS": 55.0 },
        "events": [ { "state": "done" } ]
      },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "run the tests, tell me what fails" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 90, "ThinkTPS": 44.0, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "Run the full suite. It is slow, so I will read the tail rather than the whole output." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 21000, "ContextMax": 200000 },
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"shell\",\"input\":{\"command\":\"go test ./... 2>&1 | tail -30\"}}]" } ]
      },
      {
        "delay": 800,
        "status": { "State": "tools", "ContextUsed": 26000, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 0, "result": { "result": { "exit_code": 1, "timeout": false } } } } ]
      },
      {
        "delay": 500,
        "status": { "State": "running", "ContextUsed": 27000, "ContextMax": 200000, "TPS": 60.0 },
        "events": [ { "state": "content", "token": "One failure: `TestMenuWrap`. The rest pass. It is the wrap column again - the same `<` vs `<=` guard from before. The row overflows at exactly 80 columns.\n\n```go\nif width < nameCol {\n\treturn narrowCalls(b, head)\n}\n```\n\nI can fix it in one line. Say the word." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 28000, "ContextMax": 200000, "OutTokens": 90, "TPS": 60.0 },
        "events": [ { "state": "done" } ]
      },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "fix it" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 120, "ThinkTPS": 50.0, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "Change `<` to `<=` in the wrap guard, then re-run the one test to confirm. The file is deck/calls.go." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 34000, "ContextMax": 200000 },
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"file\",\"input\":{\"action\":\"edit\",\"path\":\"go/frontends/deck/calls.go\"}},{\"name\":\"shell\",\"input\":{\"command\":\"go test ./frontends/deck/ -run TestMenuWrap\"}}]" } ]
      },
      {
        "delay": 600,
        "status": { "State": "tools", "ContextUsed": 36000, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "file", "data": { "index": 0, "result": { "result": {} } } } ]
      },
      {
        "delay": 700,
        "status": { "State": "tools", "ContextUsed": 38000, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 1, "result": { "result": { "exit_code": 0, "timeout": false } } } } ]
      },
      {
        "delay": 500,
        "status": { "State": "running", "ContextUsed": 39000, "ContextMax": 200000, "TPS": 58.0 },
        "events": [ { "state": "content", "token": "Fixed. `TestMenuWrap` passes now. The guard reads `<=` and the exactly-80-column case takes the narrow path.\n\n```go\nif width <= nameCol {\n\treturn narrowCalls(b, head)\n}\n```\n\nWant me to run the whole suite once more to be sure nothing else moved?" } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 40000, "ContextMax": 200000, "OutTokens": 80, "TPS": 58.0 },
        "events": [ { "state": "done" } ]
      },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "yes, and then commit it" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 70, "ThinkTPS": 45.0, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "Full suite, then stage and commit. The change is one line, so the message is short." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 46000, "ContextMax": 200000 },
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"shell\",\"input\":{\"command\":\"go test ./...\"}},{\"name\":\"shell\",\"input\":{\"command\":\"git add -A && git commit -m \\\"fix: wrap guard uses <= at 80 cols\\\"\"}}]" } ]
      },
      {
        "delay": 900,
        "status": { "State": "tools", "ContextUsed": 50000, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 0, "result": { "result": { "exit_code": 0, "timeout": false } } } } ]
      },
      {
        "delay": 600,
        "status": { "State": "tools", "ContextUsed": 51000, "ContextMax": 200000 },
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 1, "result": { "result": { "exit_code": 0, "timeout": false } } } } ]
      },
      {
        "delay": 500,
        "status": { "State": "running", "ContextUsed": 52000, "ContextMax": 200000, "TPS": 57.0 },
        "events": [ { "state": "content", "token": "All green, and committed as `fix: wrap guard uses <= at 80 cols`. The whole suite passes and the one-line fix is in. That closes the loop on `TestMenuWrap`." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 53000, "ContextMax": 200000, "OutTokens": 40, "TPS": 57.0 },
        "events": [ { "state": "done" } ]
      }
    ]
  },

  "resumed": {
    "note": "A reopened conversation: the session event carries loading, so the resumed banner shows, then the turn replays with names-only images (the worker does not re-send payloads).",
    "batches": [
      { "delay": 0, "status": { "State": "idle" }, "events": [ { "state": "ready" } ] },
      {
        "delay": 200,
        "status": { "State": "idle" },
        "loading": true,
        "events": [ { "state": "session", "data": { "session_id": "20260825-123456-abc", "msg_count": 3 } } ]
      },
      {
        "delay": 200,
        "status": { "State": "idle" },
        "events": [ { "state": "session_title", "data": { "title": "fix the wrap column", "source": "agent" } } ]
      },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "the wrap column overflows at 80", "images": [ { "name": "terminal.png" } ] } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 80, "ThinkTPS": 42.0, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "The screenshot shows the menu wrapping badly at 80 columns. The guard is the likely culprit." } ]
      },
      {
        "delay": 500,
        "status": { "State": "running", "ContextUsed": 15000, "ContextMax": 200000, "TPS": 59.0 },
        "events": [ { "state": "content", "token": "Right, the wrap guard uses `<` where it needs `<=`, so an exactly-80-column terminal takes the wide path and overflows. One-line fix in `deck/calls.go`." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 16000, "ContextMax": 200000, "OutTokens": 30, "TPS": 59.0 },
        "events": [ { "state": "done" } ]
      }
    ]
  },

  "cancelled": {
    "note": "A run the user stops mid-tool: the calls block settles as failed (red rail) and the cancelled notice shows.",
    "batches": [
      { "delay": 0, "status": { "State": "idle" }, "events": [ { "state": "ready" } ] },
      {
        "delay": 200,
        "status": { "State": "running" },
        "events": [ { "state": "user", "token": "deploy the staging build" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 60, "ThinkTPS": 40.0, "ThinkEst": true },
        "events": [ { "state": "thinking", "token": "Deploying is a few steps: build, push, then the deploy command. Let me start." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 18000, "ContextMax": 200000 },
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"shell\",\"input\":{\"command\":\"make build\"}},{\"name\":\"shell\",\"input\":{\"command\":\"make deploy-staging\"}}]" } ]
      },
      {
        "delay": 300,
        "status": { "State": "cancelled", "ContextUsed": 19000, "ContextMax": 200000 },
        "events": [ { "state": "cancelled" } ]
      }
    ]
  },

  "workspace": {
    "note": "Two sessions at once: the default conversation in one, a fresh one in another, switched to the second at the end so the tabs and both feeds show.",
    "batches": [
      { "delay": 0, "status": { "State": "idle" }, "session": "default", "events": [ { "state": "ready" } ] },
      {
        "delay": 200,
        "status": { "State": "running" },
        "session": "default",
        "events": [ { "state": "user", "token": "open notes and write down the three failing tests" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 60, "ThinkTPS": 40.0, "ThinkEst": true },
        "session": "default",
        "events": [ { "state": "thinking", "token": "The user wants Notes open and three test names written into it. Screenshot first, then decide." } ]
      },
      {
        "delay": 400,
        "status": { "State": "running", "ContextUsed": 12000, "ContextMax": 200000, "TPS": 55.0 },
        "session": "default",
        "events": [ { "state": "content", "token": "Notes is open and the three names are written down. One of them, `TestMenuWrap`, still fails - the wrap guard uses `<` where it needs `<=`." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 13000, "ContextMax": 200000, "OutTokens": 30, "TPS": 55.0 },
        "session": "default",
        "events": [ { "state": "done" } ]
      },

      { "delay": 0, "status": { "State": "idle" }, "session": "s2", "events": [ { "state": "ready" } ] },
      {
        "delay": 200,
        "status": { "State": "running" },
        "session": "s2",
        "events": [ { "state": "user", "token": "does the build pass?" } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ThinkTokens": 40, "ThinkTPS": 38.0, "ThinkEst": true },
        "session": "s2",
        "events": [ { "state": "thinking", "token": "Quick check: run the build and read the exit code." } ]
      },
      {
        "delay": 400,
        "status": { "State": "tools", "ContextUsed": 8000, "ContextMax": 200000 },
        "session": "s2",
        "events": [ { "state": "tool_calls", "token": "[{\"name\":\"shell\",\"input\":{\"command\":\"go build ./...\"}}]" } ]
      },
      {
        "delay": 500,
        "status": { "State": "tools", "ContextUsed": 8400, "ContextMax": 200000 },
        "session": "s2",
        "events": [ { "state": "tool_output", "token": "shell", "data": { "index": 0, "result": { "result": { "exit_code": 0, "timeout": false } } } } ]
      },
      {
        "delay": 300,
        "status": { "State": "running", "ContextUsed": 8800, "ContextMax": 200000, "TPS": 52.0 },
        "session": "s2",
        "events": [ { "state": "content", "token": "Build passes, exit 0. Nothing to fix there." } ]
      },
      {
        "delay": 200,
        "status": { "State": "done", "ContextUsed": 9000, "ContextMax": 200000, "OutTokens": 10, "TPS": 52.0 },
        "session": "s2",
        "events": [ { "state": "done" } ]
      },

      { "delay": 0, "status": {}, "switch": "s2" }
    ]
  }
};

// Back-compat alias: the default scenario is what a bare ?demo replays.
window.__FIXTURE = window.__FIXTURES.default;
