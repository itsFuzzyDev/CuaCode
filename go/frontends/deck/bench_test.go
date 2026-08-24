package main

import (
	"strings"
	"testing"

	"cuacode/core/session"
)

// grow builds a model whose feed already holds roughly n rows of finished
// history, which is the state a long session is in when it starts to feel slow.
func grow(n int) *model {
	m := initialModel()
	m.width, m.height = 96, 40
	m.status = session.Snapshot{State: session.Running}

	for len(m.wrapped) < n {
		m.push(&block{kind: kProse, text: strings.Repeat("some earlier answer text ", 12)})
		m.push(&block{kind: kUser, text: "and something the user asked"})
		m.rebuild()
	}
	return m
}

// BenchmarkStreamToken is the hot path: one token arrives, the feed re-renders.
// Reported per token, so a long message must not cost more per token than a
// short one - that difference is what lag is made of.
func BenchmarkStreamToken(b *testing.B) {
	for _, history := range []int{40, 400, 2000} {
		b.Run("history="+itoa(history), func(b *testing.B) {
			m := grow(history)
			m.push(&block{kind: kProse})
			live := m.blocks[len(m.blocks)-1]

			b.ResetTimer()
			for i := 0; b.Loop(); i++ {
				live.text += "token "
				live.touch()
				m.rebuild()

				if len(live.text) > 8000 { // a long answer, then the next one
					live.text = ""
				}
			}
		})
	}
}

// BenchmarkAnimationFrame is what runs 20 times a second while the model works.
func BenchmarkAnimationFrame(b *testing.B) {
	m := grow(2000)
	m.push(&block{kind: kProse, text: strings.Repeat("the answer so far ", 40)})
	m.rebuild()

	b.ResetTimer()
	for b.Loop() {
		m.rebuild()
		_ = m.render()
	}
}

// BenchmarkFormatProse is the layout itself, which the cache exists to avoid
// running more than reflowEvery.
func BenchmarkFormatProse(b *testing.B) {
	text := strings.Repeat("a sentence of the model's answer, with **emphasis** and `code`. ", 60)
	b.ResetTimer()
	for b.Loop() {
		formatProse(text, 78)
	}
}
