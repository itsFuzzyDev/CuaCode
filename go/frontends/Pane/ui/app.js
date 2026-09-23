'use strict';

// Empty on purpose: the old design moved out of the repo to
// ~/Documents/Work/_archive/260920-bridge-ui, and this is the
// blank slate to build the next one on. What is kept is the seam, because it is
// the frontend's contract and not the design's:
//
//   window.__cua.push  - the single entry point. Nothing feeds the page any
//                        other way; pane has no worker yet, so nothing calls
//                        it, and the first feed to arrive will come through
//                        here.
//
// Nothing folds the batches yet, so the last one that arrived just sits in
// `last`. The first thing a new design does is read it.

let last = null;

window.__cua = {
  push(batch) { last = batch; },
};

// go calls a binding the Go side bound on the window; served on its own there
// are none, and this is a no-op there.
function go(name, ...args) {
  const fn = window[name];
  if (typeof fn === 'function') return fn(...args);
}

// Whether this window is really see-through decides which of the two colours
// app.css paints. The host injects the flag before the page loads (main.go,
// glassJS), and the class goes on here rather than there because an injected
// script runs at document start, before there is a document element to hang a
// class on.
if (window.__cuaGlass) document.documentElement.classList.add('glass');

// The window's drag strip. The window is non-opaque, which costs it the
// system's own titlebar drag strip, so the top of the page hands the drag back
// to the host (windowglass_darwin.go, goDrag).
const drag = document.getElementById('drag');
if (drag) drag.addEventListener('mousedown', e => { e.preventDefault(); go('goDrag'); });
