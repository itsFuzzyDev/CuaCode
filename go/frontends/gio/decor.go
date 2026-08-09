package main

import (
	"image"
	"image/color"
	"math"
	"time"

	"gioui.org/font"
	"gioui.org/layout"
	"gioui.org/op"
	"gioui.org/op/clip"
	"gioui.org/op/paint"
	"gioui.org/unit"
	"gioui.org/widget/material"
)

// bar paints col behind w across the full available width.
func bar(gtx layout.Context, col color.NRGBA, w layout.Widget) layout.Dimensions {
	macro := op.Record(gtx.Ops)
	dims := w(gtx)
	content := macro.Stop()

	size := image.Pt(gtx.Constraints.Max.X, dims.Size.Y)
	paint.FillShape(gtx.Ops, col, clip.Rect{Max: size}.Op())
	content.Add(gtx.Ops)

	return layout.Dimensions{Size: size, Baseline: dims.Baseline}
}

// hairline is a one-pixel rule across the full width.
func hairline(gtx layout.Context, col color.NRGBA) layout.Dimensions {
	size := image.Pt(gtx.Constraints.Max.X, 1)
	paint.FillShape(gtx.Ops, col, clip.Rect{Max: size}.Op())
	return layout.Dimensions{Size: size}
}

// lamp draws the status indicator. While the worker is live it breathes, so a
// run in progress is distinguishable at a glance from one that has stalled.
func lamp(gtx layout.Context, col color.NRGBA, live bool) layout.Dimensions {
	d := gtx.Dp(sp8)
	if live {
		// One cycle per 1.4s, never fading out completely.
		phase := float64(gtx.Now.UnixMilli()%1400) / 1400
		col.A = uint8(0x50 + 0xaf*(0.5+0.5*math.Sin(2*math.Pi*phase)))
		gtx.Execute(op.InvalidateCmd{At: gtx.Now.Add(50 * time.Millisecond)})
	}

	paint.FillShape(gtx.Ops, col, clip.Ellipse{Max: image.Pt(d, d)}.Op(gtx.Ops))
	return layout.Dimensions{Size: image.Pt(d, d)}
}

// tag draws a short uppercase label in the UI face with letter tracking, the
// register used for everything the worker did not send. Gio has no tracking
// control, so each rune is placed individually — cheap here, because every tag
// is a handful of characters.
func tag(th *material.Theme, s string, col color.NRGBA) layout.Widget {
	return func(gtx layout.Context) layout.Dimensions {
		children := make([]layout.FlexChild, 0, len(s)*2)
		for _, r := range s {
			l := material.Label(th, sizeMeta, string(r))
			l.Font.Typeface = FaceUI
			l.Font.Weight = font.Medium
			l.Color = col
			children = append(children,
				layout.Rigid(l.Layout),
				layout.Rigid(layout.Spacer{Width: unit.Dp(1)}.Layout),
			)
		}
		return layout.Flex{}.Layout(gtx, children...)
	}
}
