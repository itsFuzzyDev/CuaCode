package main

// What each menu is made of and what choosing a row does.
//
// Permission is the one that matters: the worker blocks on the answer, so a
// question that goes up must always come back down with a reply. Every exit
// from the prompt answers it — there is no way to dismiss one unanswered.

import (
	"encoding/json"
	"io/fs"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"cuacode/core/session"
)

// ---------------------------------------------------------------------------
// permission

// What the user chose, and for how long. There is deliberately no standing
// deny: a refusal is always for this one call, so nothing can quietly keep
// answering "no" on your behalf for the rest of the session.
const (
	permAllowOnce   = "allow-once"
	permAllowAlways = "allow-always"
	permDenyOnce    = "deny-once"
)

// permScopeArg names the argument that makes one call to a tool meaningfully
// different from another. A standing allow is granted for that value alone, so
// approving `shell` once for `ls` is not approving it for `rm -rf`, and
// approving a `file` read is not approving a write.
//
// A tool with no entry here scopes by name, which is the right answer when
// every call to it does the same kind of thing.
var permScopeArg = map[string]string{
	"shell": "command",
	"file":  "action",
	"tasks": "action",
}

// permScope returns the key a standing allow is filed under and the phrase that
// describes it on the prompt.
func permScope(name string, args map[string]any) (key, label string) {
	arg, scoped := permScopeArg[name]
	if !scoped {
		return name, "every " + name + " call"
	}
	value := text(args, arg)
	if value == "" {
		return name, "every " + name + " call"
	}
	if arg == "command" {
		return name + "\x00" + value, "every " + name + " " + strconv.Quote(clip(value, 48))
	}
	return name + "\x00" + value, "every " + name + " " + value
}

// askPermission handles a permission request from the worker. A standing
// answer for that tool is applied without asking again; otherwise the request
// joins the queue and the prompt goes up.
func (m *model) askPermission(id string, data json.RawMessage) {
	var req struct {
		Name string          `json:"name"`
		Args json.RawMessage `json:"args"`
		// What the tool says the call would do, when it can say: a summary
		// line, and for a write or an edit the patch itself. Sent as its own
		// field, so a prompt that has never seen one still draws the arguments.
		Preview struct {
			Summary string `json:"summary"`
			Diff    string `json:"diff"`
		} `json:"preview"`
	}
	_ = json.Unmarshal(data, &req)
	if req.Name == "" {
		req.Name = "?"
	}

	args := decodeArgs(req.Args)
	key, scope := permScope(req.Name, args)

	p := permRequest{
		id:      id,
		name:    req.Name,
		args:    formatArgs(req.Name, args),
		full:    args,
		summary: req.Preview.Summary,
		diff:    req.Preview.Diff,
		key:     key,
		scope:   scope,
	}
	// A file being created has no patch to show — there is nothing to diff it
	// against — but its content is right here, and every line of it is new.
	if p.diff == "" && text(args, "action") == "write" {
		p.content, _ = args["content"].(string)
	}
	p.lang = langOf(text(args, "path"))

	if standing, known := m.permPolicy[p.key]; known {
		m.answerPermission(p, standing)
		return
	}

	m.permQueue = append(m.permQueue, p)
	m.nextPermission()
}

// nextPermission raises the prompt for the next queued request, if the screen
// is free to show it.
func (m *model) nextPermission() {
	if m.overlayActive() || len(m.permQueue) == 0 {
		return
	}
	p := m.permQueue[0]

	m.openOverlay(ovPermission, p.name, []option{
		{label: "Allow once", value: permAllowOnce, tone: cOK},
		{label: "Allow " + p.scope + " this session", value: permAllowAlways, tone: cOK, hint: "not asked again"},
		{label: "Deny", value: permDenyOnce, tone: cErr, hint: "this call only"},
	}, -1)

	m.ov.perm = p
	m.ov.title = "allow " + p.name + "?"
	m.ov.note = p.args
}

// answerPermission replies to the worker and records a standing answer if one
// was chosen. The reply is the only thing that unblocks the run.
func (m *model) answerPermission(p permRequest, decision string) {
	allow := decision == permAllowOnce || decision == permAllowAlways
	if decision == permAllowAlways {
		m.permPolicy[p.key] = decision
	}
	if m.sess != nil {
		m.sess.Reply(p.id, "permission", map[string]any{"allow": allow})
	}
}

// resolvePermission answers the request the prompt is showing and moves on to
// whatever is behind it.
func (m *model) resolvePermission(decision string) {
	p := m.ov.perm
	m.closeOverlay()

	if len(m.permQueue) > 0 && m.permQueue[0].id == p.id {
		m.permQueue = m.permQueue[1:]
	}
	m.answerPermission(p, decision)
	m.nextPermission()
}

// ---------------------------------------------------------------------------
// slash commands

type command struct {
	name string
	help string
}

var commands = []command{
	{"help", "keys and commands"},
	{"context", "what is filling the window"},
	{"new", "start a fresh session"},
	{"provider", "switch provider"},
	{"effort", "how hard the model thinks"},
	{"model", "switch the model on the current provider"},
	{"vision", "which provider looks at images for a blind model"},
	{"permissions", "toggle asking before tool calls"},
	{"clear", "clear the feed"},
	{"quit", "leave"},
}

// effortLadder mirrors handler/agent/effort.py. The worker validates the value
// anyway and rejects anything else, so the risk of them drifting apart is a
// rejected setting rather than a silently wrong one.
var effortLadder = []struct {
	name, hint string
	tone       string
}{
	{"off", "answers straight away, no thinking at all", cFaint},
	{"low", "a moment's thought before answering", cOK},
	{"medium", "thinks things through", cLook},
	{"high", "works at it, and takes its time", cCall},
	{"max", "as far as it will go — expect to wait", cErr},
}

// effortRows is the selection model behind the meter: one option per rung, in
// order. What it looks like is renderEffort's business.
func (m *model) effortRows() []option {
	opts := make([]option, 0, len(effortLadder))
	for _, rung := range effortLadder {
		opts = append(opts, option{label: rung.name, value: rung.name, tone: rung.tone})
	}
	return opts
}

// The meter's geometry. Five columns of a fixed width, drawn as bars of
// increasing height — the shape of the thing you are setting, rather than a
// list of five words that happen to be in order.
const (
	effortCol  = 9 // cells per column
	effortBar  = 5 // rows of bar above the baseline
	effortWide = 5 // cells of solid bar within a column
)

// renderEffort draws the effort meter: columns rising left to right, the one
// you are on lit and the rest left as a dim scale behind it. The top rung
// burns; with ultracode on, the whole instrument does.
func (m *model) renderEffort() []string {
	width := m.bodyW()
	sel := 0
	if opt, ok := m.ov.selected(); ok {
		for i, rung := range effortLadder {
			if rung.name == opt.value {
				sel = i
			}
		}
	}

	deck := effortCol * len(effortLadder)
	lead := margin + strings.Repeat(" ", max((width-deck)/2, 0))
	bar := strings.Repeat("█", effortWide)

	// A column is lit when it is the one selected. Everything else stays a
	// scale: present enough to read the shape, quiet enough not to compete.
	paintCol := func(i int, s string) string {
		switch {
		case m.ultra:
			return gradient(s, ultraRamp, m.anim()+time.Duration(i)*120*time.Millisecond)
		case i != sel:
			return paint(cRule, s)
		case effortLadder[i].name == "max":
			return gradient(s, emberRamp, m.anim())
		}
		return paint(effortLadder[i].tone+bold, s)
	}

	rows := make([]string, 0, effortBar+4)
	for r := range effortBar {
		var b strings.Builder
		b.WriteString(lead)
		for i := range effortLadder {
			cell := strings.Repeat(" ", effortWide)
			if i+1 >= effortBar-r { // bars grow with the rung
				cell = bar
			}
			b.WriteString(center(paintCol(i, cell), effortWide, effortCol))
		}
		rows = append(rows, b.String())
	}

	// A baseline under the columns, so an empty rung still has somewhere to be.
	var base, names strings.Builder
	base.WriteString(lead)
	names.WriteString(lead)
	for i, rung := range effortLadder {
		// The baseline thickens under the column you are on, so the selection
		// is legible without relying on colour to carry it.
		rule, tone := strings.Repeat("─", effortWide), cRule
		if i == sel {
			rule, tone = strings.Repeat("━", effortWide), effortLadder[i].tone
		}
		base.WriteString(center(paint(tone, rule), effortWide, effortCol))

		nameTone := cFaint
		if i == sel {
			nameTone = cInk + bold
		}
		names.WriteString(center(paint(nameTone, rung.name), vw(rung.name), effortCol))
	}
	rows = append(rows, base.String(), names.String(), "")

	// One line about the rung you are on, which is the only one worth reading.
	caption := effortLadder[sel].hint
	if effortLadder[sel].name == m.effort {
		caption += "   ·   in force"
	}
	return append(rows, margin+"  "+paint(cMuted, trunc(caption, width-2)))
}

// center pads s into a field of w cells. The visible width is passed in because
// s may already be painted, and colour must never decide a column's width.
func center(s string, visible, w int) string {
	pad := max(w-visible, 0)
	left := pad / 2
	return strings.Repeat(" ", left) + s + strings.Repeat(" ", pad-left)
}

func (m *model) openCommands(anchor int) {
	opts := make([]option, 0, len(commands))
	for _, c := range commands {
		opts = append(opts, option{label: "/" + c.name, hint: c.help, value: c.name})
	}
	m.openOverlay(ovCommands, "command", opts, anchor)
}

// runCommand acts on a chosen slash command. The ones the worker owns are sent
// as commands and answered asynchronously; the rest are local.
func (m *model) runCommand(name string) {
	switch name {
	case "help":
		m.notice(cGhost, helpText)

	case "context":
		// Answered from what the worker already holds, so it costs nothing and
		// can be asked mid-run without disturbing anything.
		m.command("context.report", nil)

	case "clear":
		m.reset()
		m.notice(cGhost, "feed cleared — the conversation itself is untouched")

	case "quit":
		m.quitting = true

	case "permissions":
		m.askMode = !m.askMode
		mode := "auto"
		if m.askMode {
			mode = "ask"
		}
		if m.sess != nil {
			m.sess.Command("permission.mode", map[string]any{"mode": mode})
		}
		if m.askMode {
			m.notice(cGhost, "asking before file and shell calls")
		} else {
			m.notice(cGhost, "running tool calls without asking")
		}

	case "new":
		m.command("session.new", nil)

	case "provider":
		// Asking for the picker outranks the startup read: whichever listing
		// comes back next is the one that goes up.
		m.probing = false
		m.command("provider.list", nil)

	case "model":
		m.command("model.list", nil)

	case "vision":
		// Same reply as /provider — the worker has one listing and it carries
		// everything both pickers need — so the flag says which one to open.
		m.pickVision, m.probing = true, false
		m.command("provider.list", nil)

	case "effort":
		// The ladder is fixed, so there is nothing to ask the worker for: the
		// menu goes straight up and the choice is what gets sent.
		m.openOverlay(ovEffort, "how hard should it think?", m.effortRows(), -1)
	}
}

func (m *model) command(action string, fields map[string]any) {
	if m.sess != nil {
		m.sess.Command(action, fields)
	}
}

const helpText = "enter send  ·  esc stop the run  ·  ctrl+b background the running tool call  ·  " +
	"ctrl+c quit  ·  ctrl+t thinking  ·  tab fold tool calls  ·  shift+tab spell out their arguments  ·  " +
	"ctrl+o inspect a call in full  ·  / commands  ·  @ files  ·  shift+enter newline  ·  " +
	"↑↓ pgup pgdn scroll"

// openSessions and openProviders turn a worker reply into a picker. The
// session list is asked for at startup by --resume rather than by a command:
// which conversation you are in is a thing you decide on the way in, not
// something to change halfway through one.
func (m *model) openSessions(data json.RawMessage) {
	var reply struct {
		Sessions []struct {
			ID       string `json:"id"`
			Provider string `json:"provider"`
			Model    string `json:"model"`
			Messages int    `json:"messages"`
			Updated  string `json:"updated"`
		} `json:"sessions"`
		Active string `json:"active"`
	}
	if json.Unmarshal(data, &reply) != nil || len(reply.Sessions) == 0 {
		m.notice(cGhost, "no earlier sessions")
		return
	}

	opts := make([]option, 0, len(reply.Sessions))
	for _, s := range reply.Sessions {
		label := s.ID
		if s.ID == reply.Active {
			label += "  (current)"
		}
		hint := strings.TrimSpace(s.Provider + " " + s.Model)
		if s.Messages > 0 {
			hint = plural(s.Messages, "msg", "msgs") + "  " + hint
		}
		opts = append(opts, option{label: label, hint: strings.TrimSpace(hint), value: s.ID})
	}
	m.openOverlay(ovSessions, "session", opts, -1)
}

func (m *model) openProviders(data json.RawMessage) {
	var reply struct {
		Providers []struct {
			Name    string `json:"name"`
			Model   string `json:"model"`
			Active  bool   `json:"active"`
			HasKey  bool   `json:"has_key"`
			Vision  bool   `json:"vision"`
			Default string `json:"default_model"`
		} `json:"providers"`
		// Which provider was named to look at images, and which one that
		// actually resolves to right now — they differ when nothing was named,
		// or when what was named has since lost its key.
		Named   string `json:"vision"`
		Sighted string `json:"sighted"`
	}
	if json.Unmarshal(data, &reply) != nil || len(reply.Providers) == 0 {
		quiet := m.probing // the startup read says nothing, even when it fails
		m.pickVision, m.probing = false, false
		if !quiet {
			m.notice(cGhost, "no providers configured")
		}
		return
	}

	// Whichever listing this is, it is also the freshest word on who is
	// answering — the status bar takes it from here rather than from the choice
	// that was made, because the worker is the one that decides what stuck.
	for _, p := range reply.Providers {
		if !p.Active {
			continue
		}
		m.provider, m.modelID = p.Name, p.Model
		if m.modelID == "" {
			m.modelID = p.Default
		}
	}

	// The startup read only wanted that much. Nothing goes up.
	if m.probing {
		m.probing = false
		return
	}

	if m.pickVision {
		m.pickVision = false
		// Only the ones that could actually do the looking. A picker offering a
		// choice the worker will refuse is a worse answer than a short list.
		opts := []option{{label: "auto", hint: "whichever provider can see", value: ""}}
		for _, p := range reply.Providers {
			if !p.Vision || !p.HasKey {
				continue
			}
			label := p.Name
			if p.Name == reply.Named {
				label += "  (current)"
			}
			hint := p.Model
			if hint == "" {
				hint = p.Default
			}
			opts = append(opts, option{label: label, hint: hint, value: p.Name})
		}
		if len(opts) == 1 {
			m.notice(cGhost, "no configured provider can see images")
			return
		}
		if reply.Named == "" && reply.Sighted != "" {
			opts[0].hint = "whichever provider can see — now " + reply.Sighted
		}
		m.openOverlay(ovVision, "who looks at images?", opts, -1)
		return
	}

	opts := make([]option, 0, len(reply.Providers))
	for _, p := range reply.Providers {
		label := p.Name
		if p.Active {
			label += "  (current)"
		}
		hint := p.Model
		if hint == "" {
			hint = p.Default
		}
		if !p.HasKey {
			hint = strings.TrimSpace(hint + "  no key")
		}
		// Worth saying here rather than only after switching: picking a blind
		// provider turns off computer use, which is most of what this does.
		if !p.Vision {
			hint = strings.TrimSpace(hint + "  no vision")
		} else if p.Name == reply.Sighted && reply.Sighted != "" {
			hint = strings.TrimSpace(hint + "  eyes")
		}
		opts = append(opts, option{label: label, hint: hint, value: p.Name})
	}
	m.openOverlay(ovProviders, "provider", opts, -1)
}

// openModels turns a model.list reply into a picker. The list comes from the
// provider itself and can run to several hundred rows, which is what the
// overlay's subsequence filter is for — type to narrow rather than scroll.
func (m *model) openModels(data json.RawMessage) {
	var reply struct {
		Provider string   `json:"provider"`
		Models   []string `json:"models"`
		Listed   bool     `json:"listed"`
		Active   string   `json:"active"`
		Blind    []string `json:"blind"`
	}
	if json.Unmarshal(data, &reply) != nil {
		m.notice(cGhost, "could not read the model list")
		return
	}
	if !reply.Listed {
		// Different from an empty catalog, and worth saying differently: the
		// provider did not answer, so nothing was learned about what it has.
		m.notice(cGhost, reply.Provider+" does not list its models — set one in config.json")
		return
	}

	blind := make(map[string]bool, len(reply.Blind))
	for _, b := range reply.Blind {
		blind[b] = true
	}

	opts := make([]option, 0, len(reply.Models)+1)
	seen := false
	for _, id := range reply.Models {
		if id == reply.Active {
			seen = true
		}
		opts = append(opts, option{label: modelLabel(id, reply.Active), hint: visionHint(blind[id]), value: id})
	}
	// A configured model the catalog does not list still has to be shown as
	// current, or the picker looks like it switched out from under you.
	if !seen && reply.Active != "" {
		opts = append([]option{{label: modelLabel(reply.Active, reply.Active),
			hint: strings.TrimSpace(visionHint(blind[reply.Active]) + " not listed")}}, opts...)
	}
	if len(opts) == 0 {
		m.notice(cGhost, "no models available on "+reply.Provider)
		return
	}
	m.modelProvider = reply.Provider
	if reply.Active != "" {
		m.provider, m.modelID = reply.Provider, reply.Active
	}
	m.openOverlay(ovModels, reply.Provider+" model", opts, -1)
}

func modelLabel(id, active string) string {
	if id == active {
		return id + "  (current)"
	}
	return id
}

func visionHint(blind bool) string {
	if blind {
		return "no vision"
	}
	return ""
}

// ---------------------------------------------------------------------------
// @file

// fileScanLimit bounds the walk. A picker that hangs on a huge tree is worse
// than one that admits it stopped looking.
const fileScanLimit = 4000

var skipDirs = map[string]bool{
	".git": true, "node_modules": true, "venv": true, ".venv": true,
	"__pycache__": true, "dist": true, "build": true, "target": true,
	".next": true, ".cache": true, "bin": true,
}

// scanFiles walks the working directory once and caches the result: the picker
// opens on a keystroke and cannot afford to walk a tree first.
func (m *model) scanFiles() []option {
	if m.files != nil {
		return m.files
	}

	// Where the user launched from, not where this process happens to be
	// running: under run.sh the frontend runs from go/, and a picker offering
	// that instead of the directory you were standing in is worse than
	// useless. Same value the worker is told, so the agent can reach
	// everything listed here.
	root := session.WorkingDir()

	var paths []string
	truncated := false
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil //nolint:nilerr // an unreadable directory is not a reason to stop
		}
		name := d.Name()
		if d.IsDir() {
			if path != root && (skipDirs[name] || strings.HasPrefix(name, ".")) {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasPrefix(name, ".") {
			return nil
		}
		if len(paths) >= fileScanLimit {
			truncated = true
			return filepath.SkipAll
		}
		paths = append(paths, path)
		return nil
	})

	sort.Strings(paths)
	m.files = make([]option, 0, len(paths))
	for _, abs := range paths {
		// Shown relative because that is what is readable, inserted absolute
		// because that is what is unambiguous: the worker resolves a relative
		// path against its own working directory, which is not guaranteed to
		// be this one, and a wrong-but-existing path is worse than an error.
		label := abs
		if rel, err := filepath.Rel(root, abs); err == nil {
			label = rel
		}
		m.files = append(m.files, option{label: label, value: abs})
	}
	m.filesCut = truncated
	return m.files
}

func (m *model) openFiles(anchor int) {
	opts := m.scanFiles()
	m.openOverlay(ovFiles, "file", opts, anchor)
	if m.filesCut {
		m.ov.note = "showing the first " + itoa(fileScanLimit) + " files"
	}
}

// insertChoice replaces the trigger and everything typed after it with the
// chosen value, so "@dec" becomes the path and the cursor lands after it.
func (m *model) insertChoice(value string) {
	anchor := m.ov.anchor
	if anchor < 0 || anchor > len(m.input) {
		return
	}
	tail := append([]rune(value+" "), m.input[min(m.cursor, len(m.input)):]...)
	m.input = append(m.input[:anchor], tail...)
	m.cursor = anchor + len([]rune(value)) + 1
}
