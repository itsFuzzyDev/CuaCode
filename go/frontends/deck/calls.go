package main

// Decoding of the two tool payloads that reach the wire: the provider-native
// tool_calls array on a `tool_calls` event, and the dispatch result on a
// `tool_output` event. Both are rendered down to one short line each, because
// the feed shows an action tape — what the agent did — not a JSON dump.

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// wireCall covers all three provider dialects, which cross the wire verbatim:
//
//	ollama     {"function": {"name": ..., "arguments": {...}}}
//	openai     {"id": ..., "function": {"name": ..., "arguments": "<json>"}}
//	anthropic  {"type": "tool_use", "id": ..., "name": ..., "input": {...}}
type wireCall struct {
	Name     string          `json:"name"`
	Input    json.RawMessage `json:"input"`
	Function *struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"function"`
}

// parseCalls turns a tool_calls payload into pending actions. The payload is
// whatever the provider streamed, so anything unrecognized is kept visible
// rather than dropped — a silent empty round would be worse than an ugly one.
func parseCalls(raw string) []act {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}

	var list []wireCall
	if err := json.Unmarshal([]byte(raw), &list); err != nil {
		return []act{{name: "tool", arg: sanitize(raw), args: raw, index: -1}}
	}

	out := make([]act, 0, len(list))
	for _, c := range list {
		name, args := c.Name, c.Input
		if c.Function != nil {
			name, args = c.Function.Name, c.Function.Arguments
		}
		if name == "" {
			name = "?"
		}
		// The arguments are kept whole as well as summarized: the row has space
		// for a shape, and the inspector has space for the call.
		out = append(out, act{name: name, arg: formatArgs(name, decodeArgs(args)),
			args: string(args), index: -1})
	}
	return out
}

// decodeArgs accepts both argument encodings: an object (ollama, anthropic)
// and a JSON string holding an object (openai).
func decodeArgs(raw json.RawMessage) map[string]any {
	if len(raw) == 0 {
		return nil
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		raw = json.RawMessage(s)
	}
	var m map[string]any
	if json.Unmarshal(raw, &m) != nil {
		return nil
	}
	return m
}

// formatArgs renders a call's arguments as one short human line. Tools with a
// known schema get a shape worth reading; everything else falls back to sorted
// key=value.
func formatArgs(name string, m map[string]any) string {
	if len(m) == 0 {
		return ""
	}

	switch name {
	case "click":
		s := point(m, "x", "y")
		if b := text(m, "button"); b != "" && b != "left" {
			s += " " + b
		}
		if n, ok := number(m, "clicks"); ok && n > 1 {
			s += " x" + fmtNum(n)
		}
		return s

	case "mouse_move":
		return point(m, "x", "y")

	case "scroll":
		s := point(m, "x", "y")
		dx, _ := number(m, "dx")
		dy, _ := number(m, "dy")
		if dx != 0 || dy != 0 {
			s = strings.TrimSpace(s + fmt.Sprintf(" %s%s", arrow(dx, dy), fmtNum(math.Abs(dx)+math.Abs(dy))))
		}
		return s

	case "type_text":
		return strconv.Quote(clip(text(m, "text"), 120))

	case "key":
		return text(m, "combo")

	case "app_open":
		return text(m, "app")

	case "wait":
		if n, ok := number(m, "seconds"); ok {
			return fmtNum(n) + "s"
		}

	case "file":
		return strings.TrimSpace(text(m, "action") + " " + text(m, "path"))

	case "shell":
		return clip(text(m, "command"), 120)

	case "todo":
		// The steps themselves are the interesting part of a plan and there is
		// no room for them, so a plan reports how many it holds and everything
		// else reports which item it touched.
		rest := ""
		if n := listLen(m["steps"]); n > 0 {
			rest = fmt.Sprintf("%d %s", n, plural(n, "step", "steps"))
		} else if id, ok := number(m, "id"); ok {
			rest = "#" + fmtNum(id)
		}
		return strings.TrimSpace(text(m, "action") + " " + rest)

	case "WebFetch":
		// Host first, then the goal: the goal is what the row is about, but a
		// wall of goals with no domains is unreadable when several are in
		// flight at once.
		s := host(text(m, "url"))
		if mode := text(m, "mode"); mode == "full" {
			s += " full"
		}
		if g := text(m, "goal"); g != "" {
			s += "  " + clip(g, 80)
		}
		return strings.TrimSpace(s)

	case "agent":
		return strings.TrimSpace(text(m, "agent") + "  " + clip(text(m, "prompt"), 100))

	case "skill":
		return text(m, "skill")

	case "describe_image":
		src := text(m, "source")
		if src == "" {
			src = "screen"
		}
		return strings.TrimSpace(src + "  " + clip(text(m, "question"), 90))

	case "workflow":
		return strings.TrimSpace(text(m, "workflow") + "  " + clip(kvPairs(mapOf(m["args"])), 80))

	case "screenshot":
		var parts []string
		if r := text(m, "region"); r != "" {
			parts = append(parts, r)
		}
		if z, ok := number(m, "zoom"); ok && z != 1 {
			parts = append(parts, "zoom "+fmtNum(z))
		}
		return strings.Join(parts, "  ")
	}

	return kvPairs(m)
}

// resultText summarizes one tool_output payload into the short text for the
// result column, the failure detail that earns its own row, and whether the
// call succeeded — a dispatch failure comes back as {"error": ...} in place of
// {"result": ...}.
func resultText(name string, data json.RawMessage) (short, note string, ok bool) {
	var payload struct {
		Result json.RawMessage `json:"result"`
	}
	if json.Unmarshal(data, &payload) != nil || len(payload.Result) == 0 {
		return "ok", "", true
	}

	var out struct {
		Result json.RawMessage `json:"result"`
		Error  *string         `json:"error"`
	}
	if json.Unmarshal(payload.Result, &out) != nil {
		return "ok", "", true
	}
	if out.Error != nil {
		return "failed", clip(sanitize(*out.Error), 400), false
	}

	var inner map[string]any
	_ = json.Unmarshal(out.Result, &inner)

	switch name {
	case "screenshot", "photos":
		// The worker deliberately keeps images off the wire and sends a count
		// in their place, so a count is all there is to report.
		for _, k := range []string{"n", "count"} {
			if v, found := number(inner, k); found {
				return fmtNum(v) + " img", "", true
			}
		}

	case "app_list":
		return fmt.Sprintf("%d apps", listLen(inner["running"])+listLen(inner["installed"])), "", true

	case "todo":
		// How far through the plan the agent is, which is the one thing about a
		// todo call worth a row in the feed.
		if s := text(inner, "summary"); s != "" {
			// The note column is painted as failure detail, so the step in hand
			// goes in the result text or nowhere.
			if cur, is := inner["current"].(map[string]any); is {
				if t := text(cur, "text"); t != "" {
					return s + " · " + clip(t, 40), "", true
				}
			}
			return s, "", true
		}

	case "wait":
		if v, found := number(inner, "waited"); found {
			return fmtNum(v) + "s", "", true
		}

	case "app_open":
		if done, is := inner["ok"].(bool); is && !done {
			return "failed", "", false
		}

	case "WebFetch", "skill":
		// The worker keeps the page — or the skill's instructions — off the
		// wire and sends the size instead, the same way it does for images and
		// command output.
		if n, found := number(inner, "chars"); found {
			s := fmtNum(n) + " chars"
			if cut, is := inner["truncated"].(bool); is && cut {
				s += " cut"
			}
			return s, "", true
		}
		if f := listLen(inner["fields"]); f > 0 {
			return "digest", "", true
		}

	case "describe_image":
		if seen, is := inner["answers"].(bool); is && !seen {
			return "not in image", "", true
		}
		if p := text(inner, "provider"); p != "" {
			return "described by " + p, "", true
		}

	case "agent", "workflow":
		// stopped says how the run ended, and only two of the four endings are
		// the agent deciding it was done.
		switch text(inner, "stopped") {
		case "max_rounds":
			return "out of rounds", "", false
		case "cancelled":
			return "cancelled", "", false
		}
		if n, found := number(inner, "rounds"); found {
			return fmtNum(n) + " rounds", "", true
		}
		if n, found := number(inner, "agents"); found {
			return fmtNum(n) + " agents", "", true
		}

	case "shell":
		// A non-zero exit is the command's own failure, not a dispatch error,
		// so it never arrives as {"error": ...} — read it off the exit code.
		if killed, is := inner["timeout"].(bool); is && killed {
			return "timeout", "", false
		}
		if code, found := number(inner, "exit_code"); found && code != 0 {
			return "exit " + fmtNum(code), "", false
		}
	}

	return "ok", "", true
}

func point(m map[string]any, xk, yk string) string {
	x, okx := number(m, xk)
	y, oky := number(m, yk)
	if !okx || !oky {
		return ""
	}
	return fmt.Sprintf("(%s, %s)", fmtNum(x), fmtNum(y))
}

// arrow names a scroll direction, preferring the dominant axis.
func arrow(dx, dy float64) string {
	if math.Abs(dy) >= math.Abs(dx) {
		if dy < 0 {
			return "down "
		}
		return "up "
	}
	if dx < 0 {
		return "left "
	}
	return "right "
}

// kvPairs is the fallback rendering for a tool with no special case: sorted so
// the same call always reads the same way.
func kvPairs(m map[string]any) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+clip(scalar(m[k]), 40))
	}
	return strings.Join(parts, " ")
}

func scalar(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return sanitize(t)
	case float64:
		return fmtNum(t)
	case bool:
		return strconv.FormatBool(t)
	default:
		b, err := json.Marshal(t)
		if err != nil {
			return ""
		}
		return sanitize(string(b))
	}
}

// host is the domain of a url, for a row that has no space for the path. Parsed
// by hand rather than with net/url: a malformed url still has to render as
// something, and an error return here would only be turned back into the raw
// string anyway.
func host(u string) string {
	s := strings.TrimPrefix(strings.TrimPrefix(u, "https://"), "http://")
	if i := strings.IndexAny(s, "/?#"); i >= 0 {
		s = s[:i]
	}
	return strings.TrimPrefix(s, "www.")
}

func mapOf(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func text(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return sanitize(s)
}

func number(m map[string]any, k string) (float64, bool) {
	f, ok := m[k].(float64)
	return f, ok
}

func listLen(v any) int {
	l, _ := v.([]any)
	return len(l)
}

func fmtNum(f float64) string {
	if f == math.Trunc(f) && math.Abs(f) < 1e15 {
		return strconv.FormatInt(int64(f), 10)
	}
	return strconv.FormatFloat(f, 'f', -1, 64)
}

// sanitize makes a wire string safe to put in a single row: no control
// characters, no escape sequences, nothing that would desync the layout.
func sanitize(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r == '\n':
			b.WriteString("\\n")
		case r == '\t':
			b.WriteByte(' ')
		case r < 0x20 || r == 0x7f:
			// dropped
		default:
			b.WriteRune(r)
		}
	}
	return strings.TrimSpace(b.String())
}

// clip shortens by rune count, before any width-aware truncation happens at
// render time; it only keeps absurd payloads out of the block model.
func clip(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "..."
}
