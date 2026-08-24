package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"

	"cuacode/core/session"
)

// TestPermissionAlwaysAnswered is the one that matters: the worker blocks
// forever on a question, so no path may leave the prompt without deciding.
func TestPermissionAlwaysAnswered(t *testing.T) {
	exits := []struct {
		name string
		key  tea.KeyPressMsg
		want bool // was it an allow?
	}{
		{"enter on allow once", tea.KeyPressMsg{Code: tea.KeyEnter}, true},
		{"esc denies", tea.KeyPressMsg{Code: tea.KeyEsc}, false},
	}

	for _, e := range exits {
		m := initialModel()
		m.width, m.height = 80, 24
		m.askPermission("p1", json.RawMessage(`{"name":"shell","args":{"command":"rm -rf /"}}`))

		if !m.overlayActive() || m.ov.kind != ovPermission {
			t.Fatalf("%s: no prompt went up", e.name)
		}
		if !strings.Contains(plainOf(strings.Join(m.renderOverlay(), "\n")), "rm -rf /") {
			t.Errorf("%s: prompt does not show what it is about to run", e.name)
		}

		m.Update(e.key)
		if m.overlayActive() {
			t.Errorf("%s: prompt still up after answering", e.name)
		}
		if len(m.permQueue) != 0 {
			t.Errorf("%s: request left in the queue", e.name)
		}
	}
}

// TestPermissionScope is the point of the whole design: a standing allow
// covers the specific thing that was approved and nothing else. Approving a
// file read is not approving a file write, and approving one shell command is
// not approving shell.
func TestPermissionScope(t *testing.T) {
	allowAlways := func(m *model, id, payload string) {
		t.Helper()
		m.askPermission(id, json.RawMessage(payload))
		if !m.overlayActive() {
			t.Fatalf("%s: expected a prompt", id)
		}
		m.ov.sel = 1 // the scoped "allow ... this session" row
		m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	}

	prompts := func(m *model, id, payload string) bool {
		t.Helper()
		m.askPermission(id, json.RawMessage(payload))
		asked := m.overlayActive()
		if asked {
			m.Update(tea.KeyPressMsg{Code: tea.KeyEsc})
		}
		return asked
	}

	m := initialModel()
	m.width, m.height = 80, 24

	allowAlways(m, "f1", `{"name":"file","args":{"action":"read","path":"a.txt"}}`)

	if prompts(m, "f2", `{"name":"file","args":{"action":"read","path":"b.txt"}}`) {
		t.Error("another file read prompted despite a standing allow")
	}
	if !prompts(m, "f3", `{"name":"file","args":{"action":"write","path":"a.txt"}}`) {
		t.Error("a file WRITE was allowed by a standing allow for READ")
	}
	if !prompts(m, "s1", `{"name":"shell","args":{"command":"ls"}}`) {
		t.Error("a file allow leaked to shell")
	}

	allowAlways(m, "s2", `{"name":"shell","args":{"command":"ls -la"}}`)

	if prompts(m, "s3", `{"name":"shell","args":{"command":"ls -la"}}`) {
		t.Error("the same shell command prompted twice")
	}
	if !prompts(m, "s4", `{"name":"shell","args":{"command":"rm -rf /"}}`) {
		t.Error("a standing allow for one command allowed a different one")
	}

	// A tool with nothing worth scoping on falls back to the whole tool.
	allowAlways(m, "c1", `{"name":"click","args":{"x":1,"y":2}}`)
	if prompts(m, "c2", `{"name":"click","args":{"x":9,"y":9}}`) {
		t.Error("an unscoped tool prompted again after allow-always")
	}
}

// TestNoStandingDeny checks that refusing is always for one call only - there
// is no way to leave a rule behind that keeps saying no.
func TestNoStandingDeny(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24

	m.askPermission("p1", json.RawMessage(`{"name":"shell","args":{"command":"ls"}}`))
	for _, opt := range m.ov.all {
		if strings.Contains(strings.ToLower(opt.label), "deny every") {
			t.Fatalf("a standing deny is on offer: %q", opt.label)
		}
	}

	m.ov.sel = len(m.ov.all) - 1 // the deny row
	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if len(m.permPolicy) != 0 {
		t.Errorf("denying left a standing rule: %v", m.permPolicy)
	}

	if m.askPermission("p2", json.RawMessage(`{"name":"shell","args":{"command":"ls"}}`)); !m.overlayActive() {
		t.Error("the same call was refused without asking")
	}
}

// TestPermissionQueue checks that questions arriving while one is up are asked
// in turn rather than dropped - each is a run waiting to continue.
func TestPermissionQueue(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24

	m.askPermission("p1", json.RawMessage(`{"name":"shell","args":{"command":"one"}}`))
	m.askPermission("p2", json.RawMessage(`{"name":"shell","args":{"command":"two"}}`))

	if len(m.permQueue) != 2 {
		t.Fatalf("queued %d, want 2", len(m.permQueue))
	}
	if m.ov.perm.id != "p1" {
		t.Errorf("showing %q, want p1", m.ov.perm.id)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter}) // allow once
	if !m.overlayActive() || m.ov.perm.id != "p2" {
		t.Fatalf("second question not asked, overlay=%v id=%q", m.overlayActive(), m.ov.perm.id)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if m.overlayActive() || len(m.permQueue) != 0 {
		t.Error("queue not drained")
	}
}

// TestSkillPalette covers the other half of the palette: skills the worker
// listed, which open a message rather than running a command.
func TestSkillPalette(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.takeSkills(json.RawMessage(`{"skills":[{"name":"unslop","description":"Cut AI tells from any writing. Must always apply."}]}`))
	if len(m.skills) != 1 || m.skills[0].help != "Cut AI tells from any writing" {
		t.Fatalf("skill row is %+v", m.skills)
	}

	m.Update(tea.KeyPressMsg{Code: '/', Text: "/"})
	for _, r := range "unsl" {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	opt, ok := m.ov.selected()
	if !ok || opt.value != skillPrefix+"unslop" {
		t.Fatalf("filter %q selected %q, want the skill", m.ov.filter, opt.value)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if m.overlayActive() {
		t.Error("palette still open after choosing a skill")
	}
	// The name stays put: a skill is the opening of a message, not a command
	// that has already run.
	if got := string(m.input); got != "/unslop " {
		t.Errorf("input is %q, want %q", got, "/unslop ")
	}
	if m.cursor != len(m.input) {
		t.Errorf("cursor at %d, want %d", m.cursor, len(m.input))
	}
}

// TestSlashCommands drives the palette the way a user does: type, filter, pick.
func TestSlashCommands(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24

	m.Update(tea.KeyPressMsg{Code: '/', Text: "/"})
	if !m.overlayActive() || m.ov.kind != ovCommands {
		t.Fatal("/ did not open the palette")
	}

	for _, r := range "cle" {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	opt, ok := m.ov.selected()
	if !ok || opt.value != "clear" {
		t.Fatalf("filter %q selected %q, want clear", m.ov.filter, opt.value)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if m.overlayActive() {
		t.Error("palette still open after running a command")
	}
	if len(m.input) != 0 {
		t.Errorf("command left %q in the input", string(m.input))
	}

	// A slash mid-sentence is just a slash.
	for _, r := range "a/b" {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	if m.overlayActive() {
		t.Error("/ opened the palette mid-word")
	}
}

// TestFileMenu checks the @ trigger and that choosing replaces the trigger and
// the text typed after it, rather than appending to it.
func TestFileMenu(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.files = []option{{label: "go/frontends/deck/view.go", value: "go/frontends/deck/view.go"}}

	for _, r := range "look at " {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	m.Update(tea.KeyPressMsg{Code: '@', Text: "@"})
	if !m.overlayActive() || m.ov.kind != ovFiles {
		t.Fatal("@ did not open the file picker")
	}

	for _, r := range "view" {
		m.Update(tea.KeyPressMsg{Code: r, Text: string(r)})
	}
	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})

	if got, want := string(m.input), "look at go/frontends/deck/view.go "; got != want {
		t.Errorf("input %q, want %q", got, want)
	}
	if m.cursor != len([]rune(string(m.input))) {
		t.Errorf("cursor at %d, want end (%d)", m.cursor, len(m.input))
	}

	// Escaping takes the trigger back out rather than leaving a stray @.
	m.Update(tea.KeyPressMsg{Code: '@', Text: "@"})
	m.Update(tea.KeyPressMsg{Code: tea.KeyEsc})
	if strings.Contains(string(m.input), "@") {
		t.Errorf("abandoned trigger left in the input: %q", string(m.input))
	}
}

// TestSessionReplay covers what /sessions and /new do to the feed: a session
// change wipes what was on screen, and a load redraws the stored conversation
// through the same path a live run uses.
func TestSessionReplay(t *testing.T) {
	m := play(t, 88, 24) // starts with a conversation already on screen
	if len(m.blocks) < 3 {
		t.Fatalf("expected a populated feed, got %d blocks", len(m.blocks))
	}

	// /new: the feed goes, and says so.
	m.fold(parse(t, `{"type":"status","id":"c1","data":{"state":"session","session_id":"s-new"}}`))
	m.rebuild()

	// The masthead survives - it is what an empty screen looks like - but the
	// conversation does not.
	if n := len(m.blocks); n != 2 {
		t.Fatalf("after /new the feed holds %d blocks, want the masthead and the notice", n)
	}
	if m.blocks[0].kind != kHint {
		t.Error("the masthead did not come back after clearing")
	}
	got := plainOf(strings.Join(m.wrapped, "\n"))
	if !strings.Contains(got, "new session") {
		t.Errorf("no 'new session' line after /new:\n%s", got)
	}
	if !strings.Contains(got, "ctrl+t thinking") {
		t.Errorf("the key legend is missing from an emptied screen:\n%s", got)
	}

	// /sessions: the status clears, then the stored records arrive as the same
	// events a live run sends and rebuild the conversation.
	m.loading = true
	for _, line := range []string{
		`{"type":"status","id":"c2","data":{"state":"session","session_id":"s-7"}}`,
		`{"type":"token","id":"c2","data":{"state":"user","token":"open safari","status":"running"}}`,
		`{"type":"token","id":"c2","data":{"state":"thinking","token":"needs focus first","status":"running"}}`,
		`{"type":"token","id":"c2","data":{"state":"content","token":"Opening it now.","status":"running"}}`,
		`{"type":"token","id":"c2","data":{"state":"tool_calls","status":"tooling","token":[{"function":{"name":"app_open","arguments":{"app":"Safari"}}}]}}`,
		`{"type":"token","id":"c2","data":{"state":"tool_output","token":"app_open","result":{"result":{"ok":true}},"status":"tooling"}}`,
		`{"type":"token","id":"c2","data":{"state":"done","token":"done","status":"done","msg_count":4}}`,
	} {
		m.fold(parse(t, line))
	}
	m.rebuild()

	kinds := map[blockKind]int{}
	for _, b := range m.blocks {
		kinds[b.kind]++
	}
	for kind, name := range map[blockKind]string{kUser: "user", kThink: "thinking", kProse: "prose", kCalls: "tool calls"} {
		if kinds[kind] == 0 {
			t.Errorf("replayed conversation has no %s block", name)
		}
	}

	frame := plainOf(strings.Join(m.wrapped, "\n"))
	for _, want := range []string{"resumed session s-7", "open safari", "Opening it now.", "app_open"} {
		if !strings.Contains(frame, want) {
			t.Errorf("replay is missing %q:\n%s", want, frame)
		}
	}
	if kinds[kResumed] != 1 {
		t.Errorf("got %d resume markers, want exactly one", kinds[kResumed])
	}
	if m.calls != nil {
		t.Error("replay left a tool-call batch open")
	}
}

// TestResumeFlag covers the command line, which is now the only way to reopen
// an earlier conversation.
func TestResumeFlag(t *testing.T) {
	cases := []struct {
		args       []string
		wantResume bool
		wantID     string
		wantHelp   bool
	}{
		{args: nil},
		{args: []string{"--resume"}, wantResume: true},
		{args: []string{"-r"}, wantResume: true},
		{args: []string{"--resume", "20260807-154254-6dbf"}, wantResume: true, wantID: "20260807-154254-6dbf"},
		{args: []string{"--resume=20260807-154254-6dbf"}, wantResume: true, wantID: "20260807-154254-6dbf"},
		// A flag after --resume is a flag, not a session id - and --help wins
		// outright, since the program only prints usage and leaves.
		{args: []string{"--resume", "--help"}, wantHelp: true},
		{args: []string{"-h"}, wantHelp: true},
		{args: []string{"--nonsense"}},
	}

	for _, c := range cases {
		resume, id, help := resumeFlag(c.args)
		if resume != c.wantResume || id != c.wantID || help != c.wantHelp {
			t.Errorf("%v: got (%v, %q, %v), want (%v, %q, %v)",
				c.args, resume, id, help, c.wantResume, c.wantID, c.wantHelp)
		}
	}
}

// TestEffortSends checks the line under the meter that says what a rung
// actually puts on the wire -- including the case that matters most, a rung
// this model has no knob for, which has to say so rather than look set.
func TestEffortSends(t *testing.T) {
	m := play(t, 80, 24)
	m.modelID = "glm-5.3"
	m.effortSends = effortSends(map[string]map[string]any{
		"low": {"extra_body": map[string]any{"reasoning": map[string]any{"effort": "low"}}},
		"max": {},
	})
	m.runCommand("effort")

	for _, c := range []struct{ rung, want string }{
		{"low", "extra_body.reasoning.effort=low"},
		{"max", "sends nothing"},
	} {
		m.ov.sel = 0
		for i, rung := range effortLadder {
			if rung.name == c.rung {
				m.ov.sel = i
			}
		}
		if got := plainOf(strings.Join(m.renderEffort(), "\n")); !strings.Contains(got, c.want) {
			t.Errorf("%s: meter is missing %q:\n%s", c.rung, c.want, got)
		}
	}

	// A rung the worker has said nothing about yet claims nothing on its
	// behalf: no listing has arrived, which is not the same as an empty one.
	m.effortSends = nil
	if got := plainOf(strings.Join(m.renderEffort(), "\n")); strings.Contains(got, "sends") {
		t.Errorf("the meter invented what a rung sends before hearing it:\n%s", got)
	}

	// Too narrow for labels: a shorter rail, not a clipped one.
	m.width = 24
	got := plainOf(strings.Join(m.renderEffort(), "\n"))
	if strings.Contains(got, "medium   high") {
		t.Errorf("the labelled rail was drawn into a narrow frame:\n%s", got)
	}
	if !strings.Contains(got, stopHere) {
		t.Errorf("the narrow rail lost its cursor:\n%s", got)
	}
}

// TestEffortOffRefused covers the one rung that can be a lie rather than an
// approximation: a model with no way to stop thinking must not accept "off"
// and quietly buy its cheapest thinking instead.
func TestEffortOffRefused(t *testing.T) {
	m := play(t, 80, 24)
	m.modelID = "glm-5.2"
	m.effortSends = effortSends(map[string]map[string]any{"off": {"reasoning_effort": "low"}})
	m.effortOff = false
	m.runCommand("effort")
	m.ov.sel = 0

	meter := plainOf(strings.Join(m.renderEffort(), "\n"))
	if !strings.Contains(meter, "╌") {
		t.Errorf("the unreachable rung is drawn as an ordinary one:\n%s", meter)
	}
	if !strings.Contains(meter, "cannot stop thinking") {
		t.Errorf("the meter does not say why the rung is out:\n%s", meter)
	}

	m.chooseMenu()
	if !m.overlayActive() {
		t.Error("choosing a rung the model cannot honour closed the menu")
	}

	// ...and it is only ever that rung, and only once the worker has said so.
	m.ov.sel = 4
	if !m.effortOK("max") {
		t.Error("max was refused along with off")
	}
	m.effortSends = nil
	if !m.effortOK("off") {
		t.Error("off was refused before any listing said it should be")
	}
}

// TestEffortMenu checks the ladder the worker will accept, and that /sessions
// is gone from the palette now that resuming is a startup flag.
func TestEffortMenu(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.effort = "high"

	m.Update(tea.KeyPressMsg{Code: '/', Text: "/"})
	for _, name := range []string{"effort", "help", "new", "provider"} {
		found := false
		for _, opt := range m.ov.all {
			if opt.value == name {
				found = true
			}
		}
		if !found {
			t.Errorf("/%s missing from the palette", name)
		}
	}
	for _, opt := range m.ov.all {
		if opt.value == "sessions" {
			t.Error("/sessions is still in the palette")
		}
	}

	m.ov.filter = "effort"
	m.ov.refilter()
	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})

	if m.ov.kind != ovEffort {
		t.Fatalf("/effort opened %v", m.ov.kind)
	}
	var levels []string
	for _, opt := range m.ov.all {
		levels = append(levels, opt.value)
	}
	if got, want := strings.Join(levels, ","), "off,low,medium,high,max"; got != want {
		t.Errorf("ladder is %q, want %q", got, want)
	}
	// The meter opens on the rung in force rather than at the bottom of the
	// ladder, so the most obvious key in the menu does not turn thinking off.
	if got := effortLadder[m.effortSel()].name; got != "high" {
		t.Errorf("the meter opened on %q, want the rung in force", got)
	}

	// The meter is a rail lit as far as the rung you are on: everything behind
	// the cursor filled, everything ahead of it hollow, and exactly one stop
	// marking where the cursor is. That ordering is the whole reason it is
	// drawn rather than listed.
	for _, sel := range []int{0, 2, 4} {
		m.ov.sel = sel
		meter := plainOf(strings.Join(m.renderEffort(), "\n"))
		rail := ""
		for _, line := range strings.Split(meter, "\n") {
			if strings.Contains(line, stopHere) {
				rail = strings.TrimSpace(line)
			}
		}
		if rail == "" {
			t.Fatalf("sel=%d: no stop marks the cursor:\n%s", sel, meter)
		}
		if got := strings.Count(rail, stopHere); got != 1 {
			t.Errorf("sel=%d: %d stops claim to be the cursor:\n%s", sel, got, rail)
		}
		if got := strings.Count(rail, stopPast); got != sel {
			t.Errorf("sel=%d: %d stops behind the cursor, want %d:\n%s", sel, got, sel, rail)
		}
		if got, want := strings.Count(rail, stopNext), len(effortLadder)-sel-1; got != want {
			t.Errorf("sel=%d: %d stops ahead of the cursor, want %d:\n%s", sel, got, want, rail)
		}
	}
	m.ov.sel = 3

	// The cursor is marked on the rail itself, so it survives without colour,
	// and the caption follows it.
	for _, c := range []struct {
		sel  int
		want string
	}{{0, "straight away"}, {4, "expect to wait"}} {
		m.ov.sel = c.sel
		got := plainOf(strings.Join(m.renderEffort(), "\n"))
		if !strings.Contains(got, c.want) {
			t.Errorf("sel=%d: caption missing %q:\n%s", c.sel, c.want, got)
		}
		if !strings.Contains(got, stopHere) {
			t.Errorf("sel=%d: nothing marks the rung the cursor is on:\n%s", c.sel, got)
		}
		if strings.Contains(got, "in force") && c.sel != 3 {
			t.Errorf("sel=%d: claims to be in force when high is:\n%s", c.sel, got)
		}
	}

	// Choosing sends it and lets the worker decide; nothing is assumed locally.
	m.effort = ""
	m.Update(tea.KeyPressMsg{Code: tea.KeyEnter})
	if m.overlayActive() {
		t.Error("the menu stayed up after choosing")
	}
	if m.effort != "" {
		t.Error("the frontend recorded the level itself instead of waiting for the worker")
	}

	m.fold(parse(t, `{"type":"status","id":"x","data":{"state":"effort","effort":"max"}}`))
	if m.effort != "max" {
		t.Errorf("effort is %q after the worker confirmed max", m.effort)
	}
}

// TestUltracode covers the hidden switch: typing its name toggles it and the
// word never reaches the agent, which is the whole point of a word being the
// interface.
func TestUltracode(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.sess = session.New(nil, session.Options{})

	type key = tea.KeyPressMsg
	typeWord := func(s string) {
		for _, r := range s {
			m.Update(key{Code: r, Text: string(r)})
		}
		m.Update(key{Code: tea.KeyEnter})
	}

	typeWord("ultracode")
	if !m.ultra {
		t.Fatal("typing the word did not turn it on")
	}
	if last := m.blocks[len(m.blocks)-1]; last.kind == kUser {
		t.Error("the word was sent to the agent instead of being swallowed")
	}
	if !m.wantTick() {
		t.Error("nothing is animating it")
	}

	// Off again, and case does not matter.
	typeWord("ULTRACODE")
	if m.ultra {
		t.Fatal("typing it again did not turn it off")
	}

	// An ordinary message still goes through untouched.
	typeWord("ultracode is a great name")
	if last := m.blocks[len(m.blocks)-1]; last.kind != kUser {
		t.Errorf("an ordinary message was swallowed: %+v", last)
	}
	if m.ultra {
		t.Error("a message merely containing the word toggled it")
	}
}

// TestGradientKeepsWidth is the rule every effect here obeys: colour may never
// change how wide a thing is.
func TestGradientKeepsWidth(t *testing.T) {
	for _, s := range []string{"max", "cuacode · deck", "● ● ● ●", "ULTRACODE"} {
		for _, ramp := range [][]string{emberRamp, ultraRamp} {
			for _, at := range []time.Duration{0, 300 * time.Millisecond, 4 * time.Second} {
				if got := vw(gradient(s, ramp, at)); got != vw(s) {
					t.Errorf("%q at %v: %d cells, want %d", s, at, got, vw(s))
				}
			}
		}
	}
}

// TestFrameNeverOverflows is the invariant that a frame is exactly as tall as
// the terminal - never one row more. A frame that overflows makes the terminal
// scroll, which moves earlier rows up the screen rather than overwriting them,
// and the display fills with the remains of previous frames that no redraw can
// clear. Short windows with a menu and a multi-line message are where it
// happens, which is why the heights here go down to the minimum.
func TestFrameNeverOverflows(t *testing.T) {
	for _, h := range []int{4, 5, 6, 7, 8, 10, 14, 24, 60} {
		for _, w := range []int{24, 40, 80} {
			m := play(t, w, h)

			// The worst case: a permission prompt with more patch in it than
			// any screen holds, opened out to its tallest, and an input long
			// enough to want several rows of its own.
			m.askPermission("p", permEdit(permDiff(200)))
			m.ov.expanded = true
			m.scrollPermBody(40)
			for _, r := range "one\ntwo\nthree\nfour\nfive\nsix\nseven" {
				m.insert(r)
			}
			m.rebuild()

			rows := strings.Split(m.render(), "\n")
			if len(rows) != h {
				t.Errorf("h=%d w=%d: frame is %d rows, want %d", h, w, len(rows), h)
			}
			for i, row := range rows {
				if got := vw(row); got > w {
					t.Errorf("h=%d w=%d row %d: width %d\n%q", h, w, i, got, plainOf(row))
				}
			}
			if got := m.contentHeight(); got < 1 {
				t.Errorf("h=%d w=%d: feed given %d rows", h, w, got)
			}
		}
	}
}

// TestOverlayGeometry re-asserts the frame invariant with each menu up: the
// feed is shortened by exactly the rows the menu draws, at every width.
func TestOverlayGeometry(t *testing.T) {
	open := map[string]func(*model){
		"commands":         func(m *model) { m.openCommands(0) },
		"files":            func(m *model) { m.files = []option{{label: "a/b.go", value: "a/b.go"}}; m.openFiles(0) },
		"permission":       func(m *model) { m.askPermission("p", json.RawMessage(`{"name":"shell","args":{"command":"ls -la"}}`)) },
		"permission patch": func(m *model) { m.askPermission("p", permEdit(permDiff(60))) },
		"permission patch open": func(m *model) {
			m.askPermission("p", permEdit(permDiff(60)))
			m.ov.expanded = true
			m.scrollPermBody(20)
		},
		"empty":        func(m *model) { m.openCommands(0); m.ov.filter = "zzzz"; m.ov.refilter() },
		"effort":       func(m *model) { m.runCommand("effort") },
		"effort ultra": func(m *model) { m.ultra = true; m.runCommand("effort") },
	}

	for name, setup := range open {
		for _, w := range []int{24, 40, 80, 140} {
			m := play(t, w, 24)
			setup(m)
			m.rebuild()

			rows := strings.Split(m.render(), "\n")
			if len(rows) != m.height {
				t.Errorf("%s w=%d: %d rows, want %d", name, w, len(rows), m.height)
			}
			for i, row := range rows {
				if got := vw(row); got > w {
					t.Errorf("%s w=%d row %d: width %d\n%q", name, w, i, got, plainOf(row))
				}
			}
		}
	}
}

// permDiff is a patch too long to fit any prompt, for the tests that care what
// happens to the rest of it.
func permDiff(lines int) string {
	var b strings.Builder
	b.WriteString("--- a/main.go\n+++ b/main.go\n@@ -1," + itoa(lines) + " +1," + itoa(lines) + " @@\n")
	for i := 1; i <= lines; i++ {
		if i%4 == 0 {
			b.WriteString("-\told := " + itoa(i) + "\n+\tnew := " + itoa(i) + "\n")
			continue
		}
		b.WriteString(" \tx := " + itoa(i) + "\n")
	}
	return b.String()
}

func permEdit(diff string) json.RawMessage {
	payload, _ := json.Marshal(map[string]any{
		"name":    "file",
		"args":    map[string]any{"action": "edit", "path": "main.go"},
		"preview": map[string]any{"summary": "edit main.go (9 replacements)", "diff": diff},
	})
	return payload
}

// TestPermissionScrollsItsBody is what a long patch is for: the prompt shows a
// window into it and every line is reachable by scrolling, rather than the
// middle of the change being something you are asked to take on trust. The
// choices stay on screen throughout - the block may never push the answer off.
func TestPermissionScrollsItsBody(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.askPermission("p1", permEdit(permDiff(60)))

	shown := func() string { return plainOf(strings.Join(m.renderOverlay(), "\n")) }

	first := shown()
	if !strings.Contains(first, "Allow once") || !strings.Contains(first, "Deny") {
		t.Fatal("the choices are not on screen")
	}
	if !strings.Contains(first, " of ") {
		t.Error("a windowed patch does not say how much of it is showing")
	}
	if !strings.Contains(first, "x := 1") {
		t.Error("the patch does not start at its first line")
	}

	m.ov.scrollBody(6)
	if mid := shown(); strings.Contains(mid, "x := 1\n") || !strings.Contains(mid, "x := 7") {
		t.Errorf("scrolling did not move the window:\n%s", mid)
	}

	// The end of the patch is reachable, and scrolling past it holds there.
	m.ov.scrollBody(1000)
	last := shown()
	if !strings.Contains(last, "x := 59") {
		t.Errorf("the end of the patch is unreachable:\n%s", last)
	}
	if !strings.Contains(last, "Allow once") || !strings.Contains(last, "Deny") {
		t.Error("the choices went off screen at the end of the patch")
	}

	// Back to the top, and the choices are still the choices: nothing that
	// moves the block may answer the question.
	m.Update(tea.KeyPressMsg{Code: tea.KeyHome})
	if !m.overlayActive() {
		t.Fatal("a scroll key answered the prompt")
	}
	if m.ov.bodyAt != 0 {
		t.Errorf("home left the window at row %d", m.ov.bodyAt)
	}
}

// TestPermissionBodyKeys separates the two things the arrows could mean: the
// bare ones move the choice, the modified ones move the block.
func TestPermissionBodyKeys(t *testing.T) {
	m := initialModel()
	m.width, m.height = 80, 24
	m.askPermission("p1", permEdit(permDiff(60)))

	m.Update(tea.KeyPressMsg{Code: tea.KeyDown})
	if m.ov.sel != 1 || m.ov.bodyAt != 0 {
		t.Errorf("bare down: sel=%d bodyAt=%d, want the choice to move and nothing else", m.ov.sel, m.ov.bodyAt)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyDown, Mod: tea.ModShift})
	if m.ov.sel != 1 || m.ov.bodyAt != 1 {
		t.Errorf("shift+down: sel=%d bodyAt=%d, want the block to move and nothing else", m.ov.sel, m.ov.bodyAt)
	}

	m.Update(tea.KeyPressMsg{Code: tea.KeyDown, Mod: tea.ModAlt})
	if m.ov.bodyAt != 2 {
		t.Errorf("alt+down: bodyAt=%d, want the same as shift", m.ov.bodyAt)
	}

	// ctrl+o still only changes how tall the block is drawn, and answers
	// nothing.
	m.Update(tea.KeyPressMsg{Code: 'o', Mod: tea.ModCtrl})
	if !m.overlayActive() || !m.ov.expanded {
		t.Error("ctrl+o no longer opens the block out")
	}
}
