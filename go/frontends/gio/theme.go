package main

import (
	"image/color"

	"gioui.org/text"
	"gioui.org/unit"
	"gioui.org/widget/material"

	"cuacode/core/session"
)

// The window is a wire console: the raw envelopes flowing between this
// frontend and the Python worker, plus a box to send one. Wire content is set
// in mono, the chrome around it in sans, so a glance tells you which is which.

// Palette. Cool slate base; the row prefix carries the only hue, so tool calls
// and failures can be found in a wall of tokens without reading them.
var (
	colInk   = rgb(0x12141c) // window base
	colPanel = rgb(0x171a24) // status and input bars
	colLine  = rgb(0x262b3a) // hairlines and the trace rail
	colText  = rgb(0xd7dbe8)
	colMute  = rgb(0x767e96)

	colIris  = rgb(0x8b8fd6) // the operator (you)
	colCyan  = rgb(0x6fb2d2) // the agent speaking
	colAmber = rgb(0xe8b046) // the agent acting on the machine
	colMint  = rgb(0x7cc4a4) // finished
	colRed   = rgb(0xe0687a) // failed
)

// faceMono is the wire face. It is FaceMono unless $CUACODE_FONT replaced it,
// which newTheme resolves at startup.
var faceMono = FaceMono

// Type scale. One size for the wire, two smaller ones for chrome — the log is
// the content, so nothing on screen competes with it.
const (
	sizeWire = unit.Sp(14) // log rows and the input
	sizeStat = unit.Sp(12) // counter values, kept mono so digits align
	sizeMeta = unit.Sp(10) // uppercase labels

	wireScale = 1.4
)

// Spacing scale.
const (
	sp4  = unit.Dp(4)
	sp8  = unit.Dp(8)
	sp12 = unit.Dp(12)
	sp16 = unit.Dp(16)
)

func newTheme() *material.Theme {
	faces, mono := collection()
	faceMono = mono

	th := material.NewTheme()
	th.Shaper = text.NewShaper(text.WithCollection(faces))
	th.Face = FaceUI
	th.Palette = material.Palette{
		Bg:         colInk,
		Fg:         colText,
		ContrastBg: colIris,
		ContrastFg: colInk,
	}
	th.TextSize = sizeWire
	return th
}

// stateColor tints the status lamp and its label.
func stateColor(st session.State) color.NRGBA {
	switch st {
	case session.Running:
		return colCyan
	case session.Tools:
		return colAmber
	case session.Done:
		return colMint
	case session.Cancelled:
		return colIris
	case session.Error:
		return colRed
	default:
		return colMute
	}
}

func rgb(c uint32) color.NRGBA {
	return color.NRGBA{R: uint8(c >> 16), G: uint8(c >> 8), B: uint8(c), A: 0xff}
}
