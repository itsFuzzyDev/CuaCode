package main

import "fmt"

// Component gallery, printed by `./tui -demo`. Add each Figma component
// here as you transcribe it so you can eyeball it without launching the TUI.

// ArrowSprite is the 6x5 cursor arrow from the design.
var ArrowSprite = Sprite{
	Rows: []string{
		"sbbbbt",
		"sbbbt.",
		".sbbt.",
		".sbt..",
		"..st..",
	},
	Palette: map[byte]Color{'b': CatBlue, 't': Teal, 's': Surface1},
}

// ArrowButton is the arrow centered on a padded panel.
func ArrowButton(fill Color) Box {
	return Box{W: 12, H: 10, Fill: &fill, C: ArrowSprite}
}

func runDemo() {
	gallery := VStack{Gap: 2, Items: []Component{
		// Raw sprite on base.
		ArrowSprite,
		// Button states: rest, hover, pressed.
		HStack{Gap: 3, Items: []Component{
			ArrowButton(Surface0),
			ArrowButton(Surface1),
			ArrowButton(Surface2),
		}},
		// Free-form layering example.
		(&Stack{}).
			Add(0, 0, Rect{W: 30, H: 12, C: Surface0}).
			Add(2, 2, Rect{W: 8, H: 8, C: Mauve}).
			Add(6, 4, ArrowSprite),
	}}

	// Pad the gallery a little so it doesn't touch the terminal edge.
	frame := (&Stack{}).Add(2, 2, gallery)
	w, h := frame.Size()
	buf := NewPixelBuf(w+2, h+2, Base)
	frame.Draw(buf, 0, 0)
	fmt.Println(buf.String())
}
