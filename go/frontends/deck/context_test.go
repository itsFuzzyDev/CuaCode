package main

import (
	"strings"
	"testing"
)

const ctxReply = `{"provider":"anthropic","model":"claude-opus-5","window":1000000,
"used":31200,"free":968800,"measured":31200,"calibrated":true,
"parts":[
 {"key":"system","label":"System prompt","tokens":4100},
 {"key":"environment","label":"Environment","tokens":780},
 {"key":"tools","label":"Tool definitions","tokens":18100,"count":14},
 {"key":"skills","label":"Skills index","tokens":3100},
 {"key":"memory","label":"Memory index","tokens":124},
 {"key":"messages","label":"Messages","tokens":8400,"count":12},
 {"key":"images","label":"Images","tokens":3200,"count":2}],
"sections":[
 {"key":"tools","count":17,"noun":"tool","tokens":22134},
 {"key":"skills","count":24,"noun":"skill","tokens":3100,"hint":"bodies load on demand"}],
"last":{"input":31200,"out_tokens":1840,"thinking_tokens":620,"reply_tokens":1220,
 "thinking_est":true,"tps":41.3,"think_tps":18.2,"reply_tps":62.4,
 "gen_secs":44.6,"thinking_secs":34.1,"reply_secs":19.5}}`

// A grid that reported more than it had would draw a full window on an empty
// conversation, and one that dropped a category would hide whatever was eating
// the room. Neither is allowed at any size, including the sizes that only turn
// up when a window is nearly full.
func TestAllocateFitsAndKeepsEveryCategory(t *testing.T) {
	cases := [][]ctxPart{
		{{Tokens: 4100}, {Tokens: 780}, {Tokens: 18100}, {Tokens: 124}},
		{{Tokens: 990000}, {Tokens: 5000}, {Tokens: 1}},
		{{Tokens: 1}, {Tokens: 1}, {Tokens: 0}},
	}
	for _, parts := range cases {
		got := allocate(parts, 1_000_000, ctxCells)
		sum := 0
		for i, n := range got {
			if parts[i].Tokens > 0 && n < 1 {
				t.Errorf("category %d present but drawn as nothing", i)
			}
			if parts[i].Tokens == 0 && n != 0 {
				t.Errorf("category %d absent but given %d cells", i, n)
			}
			sum += n
		}
		if sum > ctxCells {
			t.Errorf("grid overflows: %d cells of %d", sum, ctxCells)
		}
	}
}

// The page is drawn into the feed like any other block, so it obeys the feed's
// one hard rule: no row wider than the terminal, at any width.
func TestContextPageFitsEveryWidth(t *testing.T) {
	for w := 30; w <= 160; w += 7 {
		m := initialModel()
		m.width, m.height = w, 50
		m.takeContext([]byte(ctxReply))

		for _, row := range m.renderContext(m.blocks[len(m.blocks)-1]) {
			if got := vw(plainOf(row)); got > w {
				t.Fatalf("width %d: row of %d cells: %q", w, got, plainOf(row))
			}
		}
	}
}

// The whole point of the last-turn rows: the two halves of the round are
// reported separately, so a wait spent thinking cannot read as a slow answer.
func TestContextSplitsTheRound(t *testing.T) {
	m := initialModel()
	m.width, m.height = 100, 50
	m.takeContext([]byte(ctxReply))
	page := plainOf(strings.Join(m.renderContext(m.blocks[len(m.blocks)-1]), "\n"))

	for _, want := range []string{"thinking", "18 tok/s", "answer", "62 tok/s", "~620 tokens"} {
		if !strings.Contains(page, want) {
			t.Errorf("last turn does not report %q:\n%s", want, page)
		}
	}
}

// A live rate prices the thinking that is still being written; the round's own
// count replaces it when the provider bills it. Both land on the same row.
func TestRatePricesThinkingLive(t *testing.T) {
	m := initialModel()
	m.width, m.height = 100, 50
	m.push(&block{kind: kUser, text: "go"})
	m.fold(parse(t, `{"type":"token","id":"m1","data":{"state":"thinking","token":"weighing it up","status":"running"}}`))

	m.fold(parse(t, `{"type":"status","id":"r1","data":{"state":"rate","phase":"thinking","tps":38.4,`+
		`"tps_est":true,"thinking_tokens":420,"thinking_est":true,"think_tps":38.4}}`))
	row := plainOf(m.renderThinking(m.tail(kThink), false, false)[0])
	if !strings.Contains(row, "~420 tokens") || !strings.Contains(row, "38 tok/s") {
		t.Errorf("live estimate missing from the thinking row: %q", row)
	}

	m.fold(parse(t, `{"type":"token","id":"m1","data":{"state":"done","token":"done","status":"done",`+
		`"thinking_tokens":620,"thinking_est":true,"think_tps":18.2}}`))
	row = plainOf(m.renderThinking(m.tail(kThink), false, false)[0])
	if !strings.Contains(row, "~620 tokens") || !strings.Contains(row, "18 tok/s") {
		t.Errorf("billed count did not replace the estimate: %q", row)
	}
}
