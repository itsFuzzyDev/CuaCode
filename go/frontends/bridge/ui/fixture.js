// The demo conversation, as a script rather than a fetch: a page that loads its
// own fixture has finished replaying by the time load fires, so a screenshot of
// it is deterministic. See the demo block at the end of app.js.
window.__FIXTURE = {
  "note": "A scripted conversation for ?demo. It is not a mock of the UI: every entry is a real worker envelope, replayed through the same window.__cua.push the bridge uses, so what the page draws here is what it draws in the app. Keep it exercising one of everything — thinking, prose with markup, a batch of calls that partly fails, a notice, and the end of a round.",
  "batches": [
    {
      "delay": 0,
      "status": {
        "State": "idle"
      },
      "events": [
        {
          "state": "ready"
        }
      ]
    },
    {
      "delay": 300,
      "status": {
        "State": "running"
      },
      "events": [
        {
          "state": "user",
          "token": "open notes and write down the three failing tests",
          "images": [
            {
              "name": "failing-tests.png"
            }
          ]
        }
      ]
    },
    {
      "delay": 400,
      "status": {
        "State": "running",
        "ThinkTokens": 118,
        "ThinkTPS": 47.2,
        "ThinkEst": true,
        "TPS": 47.2,
        "TPSEst": true
      },
      "events": [
        {
          "state": "thinking",
          "token": "The user wants Notes open and three test names written into it. I should check what is on screen first rather than assuming Notes is closed — opening an app that is already frontmost costs a round and looks clumsy.\n\nSo: screenshot, then decide."
        }
      ]
    },
    {
      "delay": 500,
      "status": {
        "State": "tools",
        "ThinkTokens": 118,
        "ThinkTPS": 47.2,
        "ThinkEst": true,
        "ContextUsed": 24100,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_calls",
          "token": "[{\"name\":\"screenshot\",\"input\":{}},{\"name\":\"app_open\",\"input\":{\"app\":\"Notes\"}}]"
        }
      ]
    },
    {
      "delay": 700,
      "status": {
        "State": "tools",
        "ContextUsed": 26800,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_output",
          "token": "screenshot",
          "data": {
            "index": 0,
            "result": {
              "result": {
                "n": 1
              }
            }
          }
        }
      ]
    },
    {
      "delay": 600,
      "status": {
        "State": "tools",
        "ContextUsed": 27200,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_output",
          "token": "app_open",
          "data": {
            "index": 1,
            "result": {
              "result": {
                "ok": true
              }
            }
          }
        }
      ]
    },
    {
      "delay": 500,
      "status": {
        "State": "tools",
        "ContextUsed": 27600,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_calls",
          "token": "[{\"function\":{\"name\":\"click\",\"arguments\":\"{\\\"x\\\":512,\\\"y\\\":288}\"}},{\"function\":{\"name\":\"type_text\",\"arguments\":\"{\\\"text\\\":\\\"TestSessionCancel, TestRoundTrip, TestMenuWrap\\\"}\"}},{\"function\":{\"name\":\"shell\",\"arguments\":\"{\\\"command\\\":\\\"go test ./... -run TestMenuWrap\\\"}\"}}]"
        }
      ]
    },
    {
      "delay": 650,
      "status": {
        "State": "tools",
        "ContextUsed": 28100,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_output",
          "token": "click",
          "data": {
            "index": 2,
            "result": {
              "result": {}
            }
          }
        },
        {
          "state": "tool_output",
          "token": "type_text",
          "data": {
            "index": 3,
            "result": {
              "result": {}
            }
          }
        }
      ]
    },
    {
      "delay": 900,
      "status": {
        "State": "tools",
        "ContextUsed": 29400,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "tool_output",
          "token": "shell",
          "data": {
            "index": 4,
            "result": {
              "result": {
                "exit_code": 1,
                "timeout": false
              }
            }
          }
        }
      ]
    },
    {
      "delay": 400,
      "status": {
        "State": "running",
        "ContextUsed": 29800,
        "ContextMax": 200000
      },
      "events": [
        {
          "state": "notice",
          "token": "permission: shell was allowed for this session\n\nThe user granted this once. Do not ask again for shell."
        }
      ]
    },
    {
      "delay": 500,
      "status": {
        "State": "running",
        "ContextUsed": 30200,
        "ContextMax": 200000,
        "TPS": 62.4
      },
      "events": [
        {
          "state": "content",
          "token": "Notes is open and the three names are written down.\n\nOne thing worth flagging: `TestMenuWrap` **still fails** when I ran it to confirm — exit 1, not a flake. The failure is in the wrap column, not the menu:\n\n```go\nif width < nameCol {\n\treturn narrowCalls(b, head)  // never reached at width 80\n}\n```\n\n- the guard uses `<` where the row needs `<=`\n- so an exactly-80-column terminal takes the wide path and overflows\n\nWant me to fix it, or leave it on the list?"
        }
      ]
    },
    {
      "delay": 300,
      "status": {
        "State": "done",
        "ContextUsed": 30900,
        "ContextMax": 200000,
        "ThinkTokens": 118,
        "ThinkTPS": 47.2,
        "OutTokens": 402,
        "TPS": 61.8
      },
      "events": [
        {
          "state": "done"
        }
      ]
    }
  ]
};
