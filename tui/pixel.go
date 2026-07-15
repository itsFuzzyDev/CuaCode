package main

import (
	"fmt"
	"strings"
)

// Color is a 24-bit RGB pixel value.
type Color struct{ R, G, B uint8 }

// Catppuccin Mocha palette (https://catppuccin.com/palette/).
var (
	Rosewater = Color{0xF5, 0xE0, 0xDC}
	Flamingo  = Color{0xF2, 0xCD, 0xCD}
	Pink      = Color{0xF5, 0xC2, 0xE7}
	Mauve     = Color{0xCB, 0xA6, 0xF7}
	CatRed    = Color{0xF3, 0x8B, 0xA8}
	Maroon    = Color{0xEB, 0xA0, 0xAC}
	Peach     = Color{0xFA, 0xB3, 0x87}
	CatYellow = Color{0xF9, 0xE2, 0xAF}
	CatGreen  = Color{0xA6, 0xE3, 0xA1}
	Teal      = Color{0x94, 0xE2, 0xD5}
	Sky       = Color{0x89, 0xDC, 0xEB}
	Sapphire  = Color{0x74, 0xC7, 0xEC}
	CatBlue   = Color{0x89, 0xB4, 0xFA}
	Lavender  = Color{0xB4, 0xBE, 0xFE}
	Text      = Color{0xCD, 0xD6, 0xF4}
	Subtext1  = Color{0xBA, 0xC2, 0xDE}
	Subtext0  = Color{0xA6, 0xAD, 0xC8}
	Overlay2  = Color{0x93, 0x9A, 0xB7}
	Overlay1  = Color{0x7F, 0x84, 0x9C}
	Overlay0  = Color{0x6C, 0x70, 0x86}
	Surface2  = Color{0x58, 0x5B, 0x70}
	Surface1  = Color{0x45, 0x47, 0x5A}
	Surface0  = Color{0x31, 0x32, 0x44}
	Base      = Color{0x1E, 0x1E, 0x2E}
	Mantle    = Color{0x18, 0x18, 0x25}
	Crust     = Color{0x11, 0x11, 0x1B}
)

// Sprite is a palette-indexed pixel grid. Each row string is one pixel row,
// one byte per pixel. Bytes missing from Palette (use '.') are transparent.
type Sprite struct {
	Rows    []string
	Palette map[byte]Color
}

func (s Sprite) Width() int {
	w := 0
	for _, r := range s.Rows {
		if len(r) > w {
			w = len(r)
		}
	}
	return w
}

func (s Sprite) Height() int { return len(s.Rows) }

// PixelBuf is a pixel drawing surface serialized to half-block ANSI:
// each terminal cell holds two vertically stacked pixels ('▀' with
// fg = top pixel, bg = bottom pixel), so a WxH pixel buffer occupies
// W cells by ceil(H/2) rows. Heights are most convenient kept even.
type PixelBuf struct {
	W, H int
	px   []Color
}

func NewPixelBuf(w, h int, fill Color) *PixelBuf {
	p := &PixelBuf{W: w, H: h, px: make([]Color, w*h)}
	for i := range p.px {
		p.px[i] = fill
	}
	return p
}

func (p *PixelBuf) Set(x, y int, c Color) {
	if x < 0 || y < 0 || x >= p.W || y >= p.H {
		return
	}
	p.px[y*p.W+x] = c
}

func (p *PixelBuf) At(x, y int) Color {
	if x < 0 || y < 0 || x >= p.W || y >= p.H {
		return Base
	}
	return p.px[y*p.W+x]
}

// Blit draws a sprite with its top-left pixel at (x, y). Transparent
// sprite pixels leave the buffer untouched.
func (p *PixelBuf) Blit(s Sprite, x, y int) {
	for dy, row := range s.Rows {
		for dx := 0; dx < len(row); dx++ {
			if c, ok := s.Palette[row[dx]]; ok {
				p.Set(x+dx, y+dy, c)
			}
		}
	}
}

// FillRect paints a solid pixel rectangle (panels, hover states, etc.).
func (p *PixelBuf) FillRect(x, y, w, h int, c Color) {
	for dy := 0; dy < h; dy++ {
		for dx := 0; dx < w; dx++ {
			p.Set(x+dx, y+dy, c)
		}
	}
}

// Lines serializes the buffer to one ANSI string per terminal row.
// Slot these into the frame wherever the pixel region lives.
func (p *PixelBuf) Lines() []string {
	lines := make([]string, 0, (p.H+1)/2)
	var b strings.Builder
	for y := 0; y < p.H; y += 2 {
		b.Reset()
		for x := 0; x < p.W; x++ {
			top := p.At(x, y)
			bot := Base
			if y+1 < p.H {
				bot = p.At(x, y+1)
			}
			fmt.Fprintf(&b, "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀",
				top.R, top.G, top.B, bot.R, bot.G, bot.B)
		}
		b.WriteString(Reset)
		lines = append(lines, b.String())
	}
	return lines
}

// String joins Lines with newlines: a complete pixel frame.
func (p *PixelBuf) String() string {
	return strings.Join(p.Lines(), "\n")
}

// CellToPx converts a terminal mouse coordinate (cell col/row) to the
// pixel coordinate of the TOP pixel in that cell. The click could also be
// on y+1; hitboxes should tolerate ±1 px vertically.
func CellToPx(cx, cy int) (x, y int) {
	return cx, cy * 2
}
