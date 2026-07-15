package main

// Component is anything that can draw itself into a PixelBuf. Sizes and
// positions are in pixels (2 per terminal row). Build small pieces (sprites,
// rects), compose them with Stack/HStack/VStack/Box into larger components,
// then Render the root into a buffer.
type Component interface {
	Size() (w, h int)
	Draw(p *PixelBuf, x, y int)
}

// Size makes Sprite a Component.
func (s Sprite) Size() (int, int) { return s.Width(), s.Height() }

// Draw blits the sprite; transparent pixels leave the buffer untouched.
func (s Sprite) Draw(p *PixelBuf, x, y int) { p.Blit(s, x, y) }

// Rect is a solid rectangle of pixels.
type Rect struct {
	W, H int
	C    Color
}

func (r Rect) Size() (int, int)           { return r.W, r.H }
func (r Rect) Draw(p *PixelBuf, x, y int) { p.FillRect(x, y, r.W, r.H, r.C) }

// Child is a component placed at a pixel offset within a Stack.
type Child struct {
	X, Y int
	C    Component
}

// Stack layers children at absolute offsets; later children draw on top.
type Stack struct {
	Children []Child
}

func (s *Stack) Add(x, y int, c Component) *Stack {
	s.Children = append(s.Children, Child{X: x, Y: y, C: c})
	return s
}

func (s *Stack) Size() (int, int) {
	var w, h int
	for _, ch := range s.Children {
		cw, chh := ch.C.Size()
		w = max(w, ch.X+cw)
		h = max(h, ch.Y+chh)
	}
	return w, h
}

func (s *Stack) Draw(p *PixelBuf, x, y int) {
	for _, ch := range s.Children {
		ch.C.Draw(p, x+ch.X, y+ch.Y)
	}
}

// At returns the topmost child containing pixel (px, py) in stack-local
// coordinates, or -1. Use for mouse hit-testing (convert cells with CellToPx).
func (s *Stack) At(px, py int) int {
	for i := len(s.Children) - 1; i >= 0; i-- {
		ch := s.Children[i]
		w, h := ch.C.Size()
		if px >= ch.X && px < ch.X+w && py >= ch.Y && py < ch.Y+h {
			return i
		}
	}
	return -1
}

// HStack lays items out left to right, tops aligned, Gap pixels apart.
type HStack struct {
	Gap   int
	Items []Component
}

func (h HStack) Size() (int, int) {
	var w, mh int
	for i, c := range h.Items {
		cw, ch := c.Size()
		if i > 0 {
			w += h.Gap
		}
		w += cw
		mh = max(mh, ch)
	}
	return w, mh
}

func (h HStack) Draw(p *PixelBuf, x, y int) {
	for _, c := range h.Items {
		c.Draw(p, x, y)
		cw, _ := c.Size()
		x += cw + h.Gap
	}
}

// VStack lays items out top to bottom, left edges aligned, Gap pixels apart.
type VStack struct {
	Gap   int
	Items []Component
}

func (v VStack) Size() (int, int) {
	var mw, h int
	for i, c := range v.Items {
		cw, ch := c.Size()
		if i > 0 {
			h += v.Gap
		}
		h += ch
		mw = max(mw, cw)
	}
	return mw, h
}

func (v VStack) Draw(p *PixelBuf, x, y int) {
	for _, c := range v.Items {
		c.Draw(p, x, y)
		_, ch := c.Size()
		y += ch + v.Gap
	}
}

// Box is a fixed-size region with an optional fill and a centered child.
// The basic building block for buttons, badges, and panels.
type Box struct {
	W, H int
	Fill *Color // nil = transparent background
	C    Component
}

func (b Box) Size() (int, int) { return b.W, b.H }

func (b Box) Draw(p *PixelBuf, x, y int) {
	if b.Fill != nil {
		p.FillRect(x, y, b.W, b.H, *b.Fill)
	}
	if b.C != nil {
		cw, ch := b.C.Size()
		b.C.Draw(p, x+(b.W-cw)/2, y+(b.H-ch)/2)
	}
}

// Render draws a component into a fresh buffer of exactly its size
// (height rounded up to even so half-block rows don't cut off).
func Render(c Component, bg Color) *PixelBuf {
	w, h := c.Size()
	if h%2 != 0 {
		h++
	}
	p := NewPixelBuf(w, h, bg)
	c.Draw(p, 0, 0)
	return p
}
