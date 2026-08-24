package main

// What each menu is made of and what choosing a row does.
//
// Permission is the one that matters: the worker blocks on the answer, so a
// question that goes up must always come back down with a reply. Every exit
// from the prompt answers it - there is no way to dismiss one unanswered.

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
	// A file being created has no patch to show - there is nothing to diff it
	// against - but its content is right here, and every line of it is new.
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
	{"usage", "what every conversation has cost"},
	{"new", "start a fresh session"},
	{"provider", "switch provider"},
	{"effort", "how hard the model thinks"},
	{"model", "switch the model on the current provider"},
	{"vision", "which provider looks at images for a blind model"},
	{"params", "sampling params for the current model"},
	{"permissions", "toggle asking before tool calls"},
	{"clear", "clear the feed"},
	{"quit", "leave"},
}

// effortLadder mirrors handler/agent/effort.py. The worker validates the value
// anyway and rejects anything else, so the risk of them drifting apart is a
// rejected setting rather than a silently wrong one.
//
// The tones are one heat ramp rather than five unrelated colours: the meter is
// read left to right, and grey into green into amber into red says what a rung
// costs you without a word of legend. short is what the name shrinks to when
// the column is too narrow for "medium".
var effortLadder = []struct {
	name, short, hint string
	tone              string
}{
	{"off", "off", "answers straight away, no thinking at all", cFaint},
	{"low", "low", "a moment's thought before answering", cOK},
	{"medium", "med", "thinks things through", cWarn},
	{"high", "high", "works at it, and takes its time", cHot},
	{"max", "max", "as far as it will go - expect to wait", cErr},
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

// effortSel is the rung the cursor is on, as an index into the ladder. Found by
// name rather than by position: the menu's own selection indexes whatever the
// filter left visible, which is not the ladder once anything has been typed.
func (m *model) effortSel() int {
	opt, ok := m.ov.selected()
	if !ok {
		return 0
	}
	for i, rung := range effortLadder {
		if rung.name == opt.value {
			return i
		}
	}
	return 0
}

// The meter's geometry: a rail with a stop on it per rung, lit as far as the
// one you are on. Widest first -- only the widest has room for the long names,
// and below the narrowest the labels come off entirely rather than collide.
var effortSizes = []struct {
	seg  int  // rail cells between one stop and the next
	long bool // whether the labels are spelled out
}{{6, true}, {5, true}, {4, false}}

// Rail glyphs. A stop is one cell in every terminal these were checked in, and
// the layout measures what it draws, so a font that disagrees shifts the row
// rather than splitting it.
const (
	stopPast = "●" // a rung the level has passed
	stopHere = "◉" // ...the one it is at
	stopNext = "○" // ...and one it has not reached
	stopGone = "◌" // ...or one this model will not honour
)

// renderEffort draws the effort meter: a rail with five stops, lit from the
// left up to the rung the cursor is on. A level being set, rather than five
// words that happen to be in order. The top stop burns; with ultracode on, the
// whole rail does.
func (m *model) renderEffort() []string {
	width, sel := m.bodyW(), m.effortSel()
	// The rail hangs off the caption's column rather than the title's, so the
	// three lines under the title read as one block.
	lead := margin + "  "
	room := width - 2

	seg, long := 0, false
	for _, s := range effortSizes {
		// Two cells of slack for the inset below and for a last label that
		// reaches past the final stop.
		if 2+len(effortLadder)+(len(effortLadder)-1)*s.seg <= room {
			seg, long = s.seg, s.long
			break
		}
	}
	if seg == 0 {
		return m.renderEffortLine(sel, width)
	}

	// Half of the first label hangs to the left of its stop, so the rail is
	// inset by that much: without it the leftmost label is the one thing on
	// the row that cannot sit under what it names.
	first := effortLadder[0].short
	if long {
		first = effortLadder[0].name
	}
	pad := (vw(first) - 1) / 2

	var rail strings.Builder
	rail.WriteString(lead + strings.Repeat(" ", pad))
	for i := range effortLadder {
		if i > 0 {
			rail.WriteString(m.paintSeg(i, sel, seg))
		}
		rail.WriteString(m.paintStop(i, sel))
	}

	// Labels centred on their stops, and never on top of each other: a name
	// too wide for its share of the rail pushes right rather than overlapping
	// the one before it, which is the only way the row can be wrong.
	var names strings.Builder
	names.WriteString(lead)
	at := 0
	for i, rung := range effortLadder {
		name := rung.short
		if long {
			name = rung.name
		}
		start := max(pad+i*(seg+1)-(vw(name)-1)/2, at)
		if i > 0 {
			start = max(start, at+1)
		}
		names.WriteString(strings.Repeat(" ", start-at))
		names.WriteString(paint(m.nameTone(i, sel), name))
		at = start + vw(name)
	}

	return append([]string{"", rail.String(), names.String(), ""},
		m.effortCaption(sel, width)...)
}

// renderEffortLine is the rail with no room for labels: the stops alone, and
// the name of the rung beside them. A narrow terminal gets a shorter
// instrument rather than a clipped one.
func (m *model) renderEffortLine(sel, width int) []string {
	var b strings.Builder
	b.WriteString(margin + "  ")
	for i := range effortLadder {
		if i > 0 {
			b.WriteString(m.paintSeg(i, sel, 2))
		}
		b.WriteString(m.paintStop(i, sel))
	}
	b.WriteString("  " + paint(m.nameTone(sel, sel), effortLadder[sel].name))
	return append([]string{"", b.String(), ""}, m.effortCaption(sel, width)...)
}

// paintSeg draws the length of rail leading into stop i. Lit rail is heavy and
// takes the colour of the stop it arrives at, so the ladder warms as it climbs;
// rail past the cursor stays a thin scale, and rail leaving a rung this model
// will not honour is drawn broken.
func (m *model) paintSeg(i, sel, n int) string {
	switch {
	case !m.effortOK(effortLadder[i-1].name):
		return paint(cGhost, strings.Repeat("╌", n))
	case m.ultra:
		return gradient(strings.Repeat("━", n), ultraRamp,
			m.anim()+time.Duration(i)*120*time.Millisecond)
	case i <= sel:
		return paint(effortLadder[i].tone, strings.Repeat("━", n))
	}
	return paint(cRule, strings.Repeat("─", n))
}

// paintStop draws one stop: filled behind the cursor, ringed at it, hollow
// ahead of it, and broken where the rung is not on offer at all.
func (m *model) paintStop(i, sel int) string {
	rung := effortLadder[i]
	switch {
	case !m.effortOK(rung.name):
		return paint(cGhost, stopGone)
	case m.ultra:
		glyph := stopPast
		if i == sel {
			glyph = stopHere
		}
		return gradient(glyph, ultraRamp, m.anim()+time.Duration(i)*120*time.Millisecond)
	case i == sel && rung.name == "max":
		return gradient(stopHere, emberRamp, m.anim())
	case i == sel:
		return paint(rung.tone+bold, stopHere)
	case i < sel:
		return paint(rung.tone, stopPast)
	}
	return paint(cRule, stopNext)
}

// nameTone is the three states a label can be in, and the middle one is the
// point: the rung actually in force keeps its colour while the cursor is
// elsewhere, so moving along the rail never loses sight of what the
// conversation is set to.
func (m *model) nameTone(i, sel int) string {
	rung := effortLadder[i]
	switch {
	case !m.effortOK(rung.name):
		return cGhost
	case i == sel:
		return cInk + bold
	case rung.name == m.effort:
		return rung.tone
	}
	return cFaint
}

// effortCaption is what sits under the meter: one line about the rung the
// cursor is on, and -- because a knob whose effect is invisible is a knob
// nobody trusts -- what choosing it actually puts on the wire for the model in
// use. The second line is the worker's own translation of the rung, not a guess
// made here, and it is the one place that says out loud when a rung does
// nothing at all on this model.
func (m *model) effortCaption(sel, width int) []string {
	rung := effortLadder[sel]
	if !m.effortOK(rung.name) {
		// Not a rung with a caveat: a rung that is not there. Saying what the
		// model would do instead is the whole point -- the alternative is a
		// setting that reads as "no thinking at all" and buys the cheapest
		// thinking on offer.
		return []string{margin + "  " + paint(cMuted, trunc(
			shortModel(m.modelID)+" cannot stop thinking - this would quietly mean its lowest rung", width-2))}
	}
	line := rung.hint
	if rung.name == m.effort {
		line += sep + "in force"
	}
	rows := []string{margin + "  " + paint(cMuted, trunc(line, width-2))}

	sends, known := m.effortSends[rung.name]
	if !known {
		return rows
	}
	label := "sends  " + sends
	if sends == "" {
		label = "sends nothing - " + shortModel(m.modelID) + " has no knob for this"
	}
	return append(rows, margin+"  "+paint(cGhost, trunc(label, width-2)))
}

// center pads s into a field of w cells. The visible width is passed in because
// s may already be painted, and colour must never decide a column's width.
func center(s string, visible, w int) string {
	pad := max(w-visible, 0)
	left := pad / 2
	return strings.Repeat(" ", left) + s + strings.Repeat(" ", pad-left)
}

// skillPrefix marks a palette row as a skill rather than a built-in command.
// A skill is not run on the spot: choosing one writes "/name " into the input
// so the task can be typed after it, and the worker loads the instructions
// beside that message when it is sent.
const skillPrefix = "skill:"

// takeSkills reads the worker's listing of what a user may invoke by name.
// Skills marked `disable-user-invocation` never arrive here at all.
func (m *model) takeSkills(data json.RawMessage) {
	var reply struct {
		Skills []struct {
			Name        string `json:"name"`
			Description string `json:"description"`
		} `json:"skills"`
	}
	if json.Unmarshal(data, &reply) != nil {
		return
	}
	m.skills = m.skills[:0]
	for _, s := range reply.Skills {
		// One line, and only the first sentence of it: a description written
		// for a model to decide on is a paragraph, and the palette has a row.
		help := s.Description
		if i := strings.IndexAny(help, ".\n"); i > 0 {
			help = help[:i]
		}
		m.skills = append(m.skills, command{s.Name, help})
	}
}

func (m *model) openCommands(anchor int) {
	opts := make([]option, 0, len(commands)+len(m.skills))
	for _, c := range commands {
		opts = append(opts, option{label: "/" + c.name, hint: c.help, value: c.name})
	}
	// Skills after the built-ins, dimmed: they are the same gesture, but what
	// they do is load instructions rather than work the UI.
	for _, c := range m.skills {
		opts = append(opts, option{label: "/" + c.name, hint: c.help, value: skillPrefix + c.name, tone: cMuted})
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

	case "usage":
		// Every session, not this one: read out of the meta files the worker
		// already writes, which is why it can afford to be asked casually.
		m.command("usage.report", nil)

	case "clear":
		m.reset()
		m.notice(cGhost, "feed cleared - the conversation itself is untouched")

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
		// Same reply as /provider - the worker has one listing and it carries
		// everything both pickers need - so the flag says which one to open.
		m.pickVision, m.probing = true, false
		m.command("provider.list", nil)

	case "params":
		// Third reader of the same listing: it already carries this provider's
		// params and its per-model overrides, so nothing new has to be asked.
		m.pickParams, m.probing = true, false
		m.command("provider.list", nil)

	case "effort":
		// The ladder is fixed, so there is nothing to ask the worker for: the
		// menu goes straight up and the choice is what gets sent.
		m.openOverlay(ovEffort, "how hard should it think?", m.effortRows(), -1)
		// Opened on the rung in force, because this is a setting being
		// adjusted rather than a list being browsed -- and because opening on
		// the bottom rung means enter, the most obvious key in the menu, turns
		// thinking off. Nothing in force lands in the middle rather than at
		// either end: an unset conversation is on the provider's own default,
		// which is nobody's idea of "off".
		m.ov.sel = len(effortLadder) / 2
		for i, rung := range effortLadder {
			if rung.name == m.effort {
				m.ov.sel = i
			}
		}
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
			Title    string `json:"title"`
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
		// The name when there is one, the id when there is not. A session is
		// named a turn or two in, and an unnamed one showing its id is telling
		// the truth about itself -- better than a label made from "hi".
		label := strings.TrimSpace(s.Title)
		hint := s.ID
		if label == "" {
			label, hint = s.ID, ""
		}
		if s.ID == reply.Active {
			label += "  (current)"
		}
		if meta := strings.TrimSpace(s.Provider + " " + s.Model); meta != "" {
			hint = strings.TrimSpace(hint + "  " + meta)
		}
		opts = append(opts, option{label: label, hint: hint, value: s.ID})
	}
	m.openOverlay(ovSessions, "session", opts, -1)
}

// providerRow is one entry of the worker's provider listing. Named rather than
// inline because three menus read it now: the provider picker, the vision
// picker and the params editor.
type providerRow struct {
	Name    string `json:"name"`
	Model   string `json:"model"`
	Active  bool   `json:"active"`
	HasKey  bool   `json:"has_key"`
	Vision  bool   `json:"vision"`
	Default string `json:"default_model"`
	// What this endpoint is sent on every request, and what one model of it is
	// sent on top of that.
	Params      map[string]any            `json:"params"`
	ModelParams map[string]map[string]any `json:"model_params"`
	// ...and what each rung of the effort ladder would put on the wire for
	// this model. Resolved by the worker, which is the only side that knows:
	// the translation is per provider, dialect and model, and it has already
	// had this pairing's rejected keys taken out of it.
	EffortPreview map[string]map[string]any `json:"effort_preview"`
	// ...and whether its bottom rung means anything. Every other rung lands on
	// the nearest thing the model has; only this one can be an outright lie,
	// which is why it is answered separately rather than read out of the
	// preview above.
	EffortOff bool `json:"effort_off"`
}

// effortOK is whether a rung can be set on the model in use. Only the bottom
// one is ever refused, and only once a listing has said so: an unasked worker
// gets the benefit of the doubt rather than a control greyed out on a hunch.
func (m *model) effortOK(level string) bool {
	return level != "off" || m.effortSends == nil || m.effortOff
}

// effortSends renders each rung's native parameters as one line, for the meter
// to show under itself. Nested bags -- openrouter's extra_body, anthropic's
// thinking -- are flattened to dotted keys, so a glance says which knob is
// being turned rather than which shape the provider wraps it in. A rung whose
// entry is empty genuinely sends nothing, and the meter says so; a nil map
// means no listing has arrived yet, which is not the same thing.
func effortSends(preview map[string]map[string]any) map[string]string {
	if preview == nil {
		return nil
	}
	out := make(map[string]string, len(preview))
	for lvl, native := range preview {
		var parts []string
		flatten("", native, &parts)
		sort.Strings(parts)
		out[lvl] = strings.Join(parts, "  ·  ")
	}
	return out
}

// flatten walks a decoded json object into "a.b=c" strings.
func flatten(prefix string, v map[string]any, out *[]string) {
	for k, val := range v {
		// Whether the thinking text is streamed back is a question about this
		// frontend, not about how hard the model is thinking.
		if k == "display" {
			continue
		}
		if nested, ok := val.(map[string]any); ok {
			flatten(prefix+k+".", nested, out)
			continue
		}
		*out = append(*out, prefix+k+"="+scalar(val))
	}
}

// modelName is the model this entry would actually use.
func (p providerRow) modelName() string {
	if p.Model != "" {
		return p.Model
	}
	return p.Default
}

func (m *model) openProviders(data json.RawMessage) {
	var reply struct {
		Providers []providerRow `json:"providers"`
		// Which provider was named to look at images, and which one that
		// actually resolves to right now - they differ when nothing was named,
		// or when what was named has since lost its key.
		Named   string `json:"vision"`
		Sighted string `json:"sighted"`
	}
	if json.Unmarshal(data, &reply) != nil || len(reply.Providers) == 0 {
		quiet := m.probing // the startup read says nothing, even when it fails
		m.pickVision, m.pickParams, m.probing = false, false, false
		if !quiet {
			m.notice(cGhost, "no providers configured")
		}
		return
	}

	// Whichever listing this is, it is also the freshest word on who is
	// answering - the status bar takes it from here rather than from the choice
	// that was made, because the worker is the one that decides what stuck.
	var active providerRow
	for _, p := range reply.Providers {
		if !p.Active {
			continue
		}
		active = p
		m.provider, m.modelID = p.Name, p.modelName()
		m.effortSends, m.effortOff = effortSends(p.EffortPreview), p.EffortOff
	}

	// The startup read only wanted that much. Nothing goes up.
	if m.probing {
		m.probing = false
		return
	}

	if m.pickParams {
		m.pickParams = false
		m.openParams(active)
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
			opts[0].hint = "whichever provider can see - now " + reply.Sighted
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

// openParams lists what this provider's current model is actually sent, and is
// where it gets changed. Two layers in one list: the entry's own `params`,
// which every model under it is sent, and `model_params[<model>]`, which only
// this one is and which wins on a key both set.
//
// There is no text field to build. The filter line is the editor: "temperature=0"
// sets it for this model, "temperature=" drops the override and lets the
// provider-wide value show through again, and Enter on a row with nothing typed
// puts that key in front of the cursor so a value can be typed after it.
func (m *model) openParams(p providerRow) {
	if p.Name == "" {
		m.notice(cGhost, "no active provider")
		return
	}
	model := p.modelName()
	if model == "" {
		m.notice(cGhost, "no model set on "+p.Name+" - pick one with /model first")
		return
	}
	over := p.ModelParams[model]

	seen := map[string]bool{}
	keys := make([]string, 0, len(p.Params)+len(over))
	for _, set := range []map[string]any{over, p.Params} {
		for k := range set {
			if !seen[k] {
				seen[k] = true
				keys = append(keys, k)
			}
		}
	}
	sort.Strings(keys)

	opts := make([]option, 0, len(keys))
	for _, k := range keys {
		// Which layer the value on screen came from, said out loud: the whole
		// point of the menu is the difference between "this model" and "every
		// model on this provider", and a bare number does not carry it.
		v, scope, tone := p.Params[k], "all "+p.Name, cMuted
		if ov, ok := over[k]; ok {
			v, scope, tone = ov, "this model", cOK
		}
		opts = append(opts, option{label: k + " = " + paramText(v), hint: scope, value: k, tone: tone})
	}
	if len(opts) == 0 {
		opts = append(opts, option{label: "nothing set", hint: "type key=value", tone: cGhost})
	}

	m.paramsModel = model
	m.openOverlay(ovParams, p.Name+" / "+model, opts, -1)
	m.ov.note = "key=value sets it for this model · key= clears it"
}

// paramText draws a value the way it would be written back: as JSON, so a
// string keeps its quotes and 0 does not become "0".
func paramText(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "?"
	}
	return string(b)
}

// paramValue reads what was typed. JSON first - 0.7, true and [1,2] all mean
// what they look like - and a bare word that is not JSON stays a string,
// because having to quote a word to send a word is a rule nobody remembers.
func paramValue(s string) any {
	var v any
	if json.Unmarshal([]byte(s), &v) == nil {
		return v
	}
	return s
}

// openModels turns a model.list reply into a picker. The list comes from the
// provider itself and can run to several hundred rows, which is what the
// overlay's subsequence filter is for - type to narrow rather than scroll.
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
		m.notice(cGhost, reply.Provider+" does not list its models - set one in config.json")
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
