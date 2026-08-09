package main

import (
	"image/color"
	"strconv"
	"strings"

	"gioui.org/io/event"
	"gioui.org/io/key"
	"gioui.org/layout"
	"gioui.org/op/paint"
	"gioui.org/unit"
	"gioui.org/widget"
	"gioui.org/widget/material"

	"cuacode/core/protocol"
	"cuacode/core/session"
)

// ui owns everything on screen. Worker events come in through onEvent, user
// input goes out through the session. All of it is touched from the window's
// goroutine only.
type ui struct {
	th   *material.Theme
	sess *session.Session

	log  Log
	snap session.Snapshot

	list    widget.List
	input   widget.Editor
	focused bool
}

func newUI(th *material.Theme, sess *session.Session) *ui {
	u := &ui{th: th, sess: sess, snap: session.Snapshot{State: session.Idle}}
	u.list.Axis = layout.Vertical
	u.list.ScrollToEnd = true
	u.input.Submit = true
	return u
}

// onEvent folds one worker event into the log and the status strip.
func (u *ui) onEvent(ev session.Event) {
	if ev.ParseErr == nil {
		u.snap = ev.Snapshot
	}
	u.log.Received(ev)
}

// send hands the editor's contents to the worker and logs the envelope that
// went out, so both directions of the wire are on screen.
func (u *ui) send() {
	text := strings.TrimSpace(u.input.Text())
	if text == "" {
		return
	}
	u.input.SetText("")

	id, err := u.sess.SendChat(text)
	if err != nil {
		u.log.Note(err.Error())
		return
	}
	u.log.Sent(id, cmdPayload(protocol.CmdData{Action: "chat", Text: text}))
	u.snap = u.sess.Snapshot()
}

// cancel stops the run in flight, leaving the worker up. Ignored when nothing
// is running, so a stray Escape can't queue a cancel against the next message.
func (u *ui) cancel() {
	if !session.Busy(u.snap.State) {
		return
	}
	id, err := u.sess.Cancel()
	if err != nil {
		u.log.Note(err.Error())
		return
	}
	u.log.Sent(id, cmdPayload(protocol.CmdData{Action: "cancel"}))
}

func (u *ui) Layout(gtx layout.Context) layout.Dimensions {
	u.update(gtx)
	paint.Fill(gtx.Ops, colInk)

	return layout.Flex{Axis: layout.Vertical}.Layout(gtx,
		layout.Rigid(u.layoutStatus),
		layout.Flexed(1, u.layoutLog),
		layout.Rigid(u.layoutInput),
	)
}

// update drains editor and key events. The input keeps focus for the window's
// whole life, so typing works without clicking first; Escape is filtered with
// no Focus tag so it reaches us even though the editor holds focus.
func (u *ui) update(gtx layout.Context) {
	if !u.focused {
		gtx.Execute(key.FocusCmd{Tag: &u.input})
		u.focused = true
	}

	event.Op(gtx.Ops, u)
	for {
		ev, ok := gtx.Event(key.Filter{Name: key.NameEscape})
		if !ok {
			break
		}
		if ke, ok := ev.(key.Event); ok && ke.State == key.Press {
			u.cancel()
		}
	}

	for {
		ev, ok := u.input.Update(gtx)
		if !ok {
			return
		}
		if _, ok := ev.(widget.SubmitEvent); ok {
			u.send()
		}
	}
}

// layoutStatus is the instrument strip: what the worker is doing on the left,
// what the session has cost on the right.
func (u *ui) layoutStatus(gtx layout.Context) layout.Dimensions {
	state := u.snap.State
	if state == "" {
		state = session.Idle
	}
	col := stateColor(state)

	return bar(gtx, colPanel, func(gtx layout.Context) layout.Dimensions {
		return layout.Flex{Axis: layout.Vertical}.Layout(gtx,
			layout.Rigid(func(gtx layout.Context) layout.Dimensions {
				return layout.Inset{Top: sp12, Bottom: sp12, Left: sp16, Right: sp16}.Layout(gtx,
					func(gtx layout.Context) layout.Dimensions {
						return layout.Flex{Alignment: layout.Middle}.Layout(gtx,
							layout.Rigid(func(gtx layout.Context) layout.Dimensions {
								return lamp(gtx, col, session.Busy(state))
							}),
							layout.Rigid(layout.Spacer{Width: sp8}.Layout),
							layout.Rigid(tag(u.th, strings.ToUpper(string(state)), col)),
							layout.Flexed(1, layout.Spacer{}.Layout),
							layout.Rigid(u.layoutStats),
						)
					})
			}),
			layout.Rigid(func(gtx layout.Context) layout.Dimensions {
				return hairline(gtx, colLine)
			}),
		)
	})
}

// layoutStats prints the session counters, values bright and units dim so the
// numbers read first.
func (u *ui) layoutStats(gtx layout.Context) layout.Dimensions {
	type stat struct{ value, unit string }

	stats := []stat{
		{strconv.Itoa(len(u.log.Lines())), "LINES"},
		{strconv.Itoa(u.snap.Msgs), "MSGS"},
	}
	if u.snap.Turns > 0 {
		stats = append(stats, stat{strconv.Itoa(u.snap.Turns), "TURNS"})
	}
	if u.snap.ContextLeft > 0 {
		stats = append(stats, stat{strconv.Itoa(u.snap.ContextLeft), "CTX"})
	}

	children := make([]layout.FlexChild, 0, len(stats)*6)
	for i, s := range stats {
		if i > 0 {
			children = append(children,
				layout.Rigid(layout.Spacer{Width: sp12}.Layout),
				layout.Rigid(u.mono(sizeStat, "·", colMute)),
				layout.Rigid(layout.Spacer{Width: sp12}.Layout),
			)
		}
		children = append(children,
			layout.Rigid(u.mono(sizeStat, s.value, colText)),
			layout.Rigid(layout.Spacer{Width: sp4}.Layout),
			layout.Rigid(tag(u.th, s.unit, colMute)),
		)
	}
	return layout.Flex{Alignment: layout.Middle}.Layout(gtx, children...)
}

func (u *ui) layoutLog(gtx layout.Context) layout.Dimensions {
	lines := u.log.Lines()
	if len(lines) == 0 {
		return u.layoutEmpty(gtx)
	}

	return layout.Inset{Top: sp12, Bottom: sp12, Left: sp16, Right: sp8}.Layout(gtx,
		func(gtx layout.Context) layout.Dimensions {
			return material.List(u.th, &u.list).Layout(gtx, len(lines),
				func(gtx layout.Context, i int) layout.Dimensions {
					return u.layoutLine(gtx, lines[i])
				})
		})
}

// layoutLine draws one envelope: colored fixed-width prefix, raw payload.
func (u *ui) layoutLine(gtx layout.Context, ln Line) layout.Dimensions {
	return layout.Inset{Bottom: sp4}.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
		return layout.Flex{}.Layout(gtx,
			layout.Rigid(u.mono(sizeWire, ln.Prefix, ln.Color)),
			layout.Rigid(layout.Spacer{Width: sp8}.Layout),
			layout.Flexed(1, func(gtx layout.Context) layout.Dimensions {
				l := material.Label(u.th, sizeWire, ln.Payload)
				l.Font.Typeface = faceMono
				l.Color = colText
				l.LineHeightScale = wireScale
				return l.Layout(gtx)
			}),
		)
	})
}

// layoutEmpty tells a new session what to do with it.
func (u *ui) layoutEmpty(gtx layout.Context) layout.Dimensions {
	return layout.Center.Layout(gtx, func(gtx layout.Context) layout.Dimensions {
		return layout.Flex{Axis: layout.Vertical, Alignment: layout.Middle}.Layout(gtx,
			layout.Rigid(tag(u.th, "WIRE IDLE", colMute)),
			layout.Rigid(layout.Spacer{Height: sp12}.Layout),
			layout.Rigid(u.mono(sizeWire, "Send an instruction to start the stream.", colMute)),
		)
	})
}

func (u *ui) layoutInput(gtx layout.Context) layout.Dimensions {
	hint, hintCol := "RETURN TO SEND", colMute
	if session.Busy(u.snap.State) {
		hint, hintCol = "ESC TO STOP", colIris
	}

	return bar(gtx, colPanel, func(gtx layout.Context) layout.Dimensions {
		return layout.Flex{Axis: layout.Vertical}.Layout(gtx,
			layout.Rigid(func(gtx layout.Context) layout.Dimensions {
				return hairline(gtx, colLine)
			}),
			layout.Rigid(func(gtx layout.Context) layout.Dimensions {
				return layout.Inset{Top: sp12, Bottom: sp12, Left: sp16, Right: sp16}.Layout(gtx,
					func(gtx layout.Context) layout.Dimensions {
						return layout.Flex{Alignment: layout.Middle}.Layout(gtx,
							layout.Rigid(u.mono(sizeWire, "▸", colIris)),
							layout.Rigid(layout.Spacer{Width: sp12}.Layout),
							layout.Flexed(1, func(gtx layout.Context) layout.Dimensions {
								ed := material.Editor(u.th, &u.input, "Tell the agent what to do")
								ed.Font.Typeface = faceMono
								ed.TextSize = sizeWire
								ed.Color = colText
								ed.HintColor = colMute
								ed.SelectionColor = colLine
								return ed.Layout(gtx)
							}),
							layout.Rigid(layout.Spacer{Width: sp12}.Layout),
							layout.Rigid(tag(u.th, hint, hintCol)),
						)
					})
			}),
		)
	})
}

// mono is a one-off machine-voice label.
func (u *ui) mono(size unit.Sp, s string, col color.NRGBA) layout.Widget {
	return func(gtx layout.Context) layout.Dimensions {
		l := material.Label(u.th, size, s)
		l.Font.Typeface = faceMono
		l.Color = col
		return l.Layout(gtx)
	}
}
