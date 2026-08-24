'use strict';

// The page is a feed of blocks and nothing else.
//
// Every block owns one element from the moment it is created, and streamed text
// extends a text node in place. A 5,000-token answer therefore costs one node
// and one appendData per batch rather than a re-render of the message, and the
// cost of the conversation so far does not grow with the cost of the next token.
//
// Nothing here is a framework. There is no diff, no reconciliation and no
// component tree, because the feed only ever appends and the one thing that
// mutates — a tool call waiting for its result — already knows which row it is.

const feed   = document.getElementById('feed');
const input  = document.getElementById('input');
const tray   = document.getElementById('tray');
const statusEl = document.getElementById('status');

// --------------------------------------------------------------- the model

let blocks     = [];    // the feed, in order
let callsBlock = null;  // the batch still collecting results, if any
let sawOutput  = false; // a result has landed since the last prose
let callCount  = 0;
let callFail   = 0;
let status     = { State: 'idle' };
let showThink  = false;
let foldCalls  = false;
let pinned     = true;  // stick to the bottom unless the user has scrolled away
let runStart   = 0;
let pending    = [];    // pictures attached to the message being typed

const TOOL_DRIVE = new Set(['click', 'type_text', 'key', 'scroll', 'mouse_move']);
const TOOL_LOOK  = new Set(['screenshot', 'photos', 'app_list']);

// ------------------------------------------------------------ the bridge in

// Go hands over one batch per frame at most. The batch is queued rather than
// applied, so a flush that arrives mid-frame does not force a second layout:
// the DOM is touched once, inside the animation frame, however many batches
// landed in between.
let queued = [];
let frame  = 0;

window.__cua = {
  push(batch) {
    queued.push(batch);
    if (!frame) frame = requestAnimationFrame(apply);
  },
};

// Nobody can open an inspector inside the app's window, so an uncaught error is
// silence — the feed simply stops growing and nothing says why. Put it where the
// one person who can act on it will see it.
window.addEventListener('error', e => {
  try {
    notice('err', 'ui error: ' + (e.message || e.error) + ' (' + (e.lineno || '?') + ')');
    scrollToBottom();
  } catch { /* the feed itself is broken; there is nowhere left to say so */ }
});

// flushNow applies whatever is queued without waiting for a frame. Only the demo
// uses it: the app always wants the frame, because the frame is what stops a
// fast stream from laying out the page once per token.
function flushNow() {
  if (frame) cancelAnimationFrame(frame);
  frame = 0;
  apply();
}

function apply() {
  frame = 0;
  const batches = queued;
  queued = [];

  const wasPinned = pinned;
  for (const b of batches) {
    // Set before folding, not after: priceThinking reads the round's token
    // count off it, and the reading belongs to the events in this batch.
    status = b.status;
    for (const ev of b.events) fold(ev, b.loading);
  }
  drawStatus();
  tickIfBusy();
  if (wasPinned) scrollToBottom();
}

// --------------------------------------------------------- block management

function push(kind, opts) {
  const b = Object.assign({ kind, text: '', acts: [], open: false, start: 0 }, opts);
  b.el = build(b);
  blocks.push(b);
  feed.appendChild(b.el);
  return b;
}

// tail returns the last block if it is of the given kind, so streamed chunks
// extend it instead of stacking up.
function tail(kind) {
  const b = blocks[blocks.length - 1];
  return b && b.kind === kind ? b : null;
}

// stream appends a chunk to the trailing block of that kind. The text node is
// extended in place — the browser reuses the existing layout for everything
// before the insertion point, which is the whole reason a long answer stays
// cheap to keep on screen.
function stream(kind, chunk) {
  boundary();
  let b = tail(kind);
  if (!b) b = push(kind);
  b.text += chunk;
  b.textNode.appendData(chunk);
  if (kind === 'think') drawThinkHead(b);
}

// boundary closes the open batch once the model starts talking again. The
// worker puts no batch marker on the wire, so the turn from tool results back
// to prose is what separates one batch of calls from the next.
function boundary() {
  if (sawOutput) closeCalls();
}

function openCalls() {
  if (!callsBlock) {
    callsBlock = push('calls', { open: true, start: performance.now() });
  }
  return callsBlock;
}

function closeCalls() {
  if (callsBlock) {
    const b = callsBlock;
    b.open = false;
    b.end = performance.now();
    for (const a of b.acts) {
      if (a.state === 'pending') {
        settleRow(a, 'fail', 'no result', '');
        callFail++;
      }
    }
    drawCallsHead(b);
    callsBlock = null;
  }
  sawOutput = false;
}

function notice(tone, text) {
  push('notice', { tone, text });
}

// Prose is left as plain text while it streams and marked up once it is done:
// re-parsing a growing message on every chunk is quadratic in its length, which
// is exactly the shape of lag that gets worse the more the model says.
function settleProse() {
  for (const b of blocks) {
    if (b.kind === 'prose' && !b.marked && b.text) {
      b.body.innerHTML = inlineMarkdown(b.text);
      b.marked = true;
    }
  }
}

function reset() {
  blocks = [];
  callsBlock = null;
  sawOutput = false;
  callCount = 0;
  callFail = 0;
  feed.replaceChildren();
  status.ContextUsed = 0;
  status.ContextLeft = 0;
  // Attached to a message in a conversation that is no longer on screen.
  pending = [];
  drawTray();
  push('hint');
}

// ------------------------------------------------------------- the folding

// fold turns one worker event into feed. The state names are the worker's own;
// deck switches on exactly the same set.
function fold(ev, loading) {
  if (ev.state === 'bad_line') {
    notice('err', 'unreadable worker line: ' + clip(sanitize(ev.raw || ''), 200));
    return;
  }

  switch (ev.state) {
    case 'startup':
    case 'ready':
      notice('', 'welcome!');
      break;

    // Only ever seen while a reopened conversation replays: a live message is
    // echoed into the feed when it is sent, not when it comes back.
    case 'user':
      boundary();
      closeCalls();
      // Names only on this path: a reopened conversation is replayed without
      // the payloads, so there is nothing to draw but what the files were
      // called. See main.py's replay().
      push('user', { text: ev.token || '', shots: (ev.images || []).map(i => ({ name: i.name })) });
      break;

    case 'thinking':
      stream('think', ev.token || '');
      break;

    case 'content':
      stream('prose', ev.token || '');
      break;

    case 'tool_calls': {
      boundary();
      const calls = parseCalls(ev.token || '');
      const b = openCalls();
      for (const a of calls) addRow(b, a);
      callCount += calls.length;
      drawCallsHead(b);
      break;
    }

    case 'tool_output':
      settle(ev.token || '', ev.data);
      sawOutput = true;
      break;

    // The call did not finish, it moved. Worth its own line: the row for it is
    // about to settle with a job id where a result belongs, and that reads as a
    // strange answer with nothing to explain it.
    case 'background':
      notice('call', 'backgrounded · ' + ev.token + ' is still running');
      break;

    // Runtime text put into the conversation — neither the user's nor the
    // model's. Only the first paragraph is shown: the rest is instruction
    // addressed to the model, and on screen it would read as the agent talking
    // to itself.
    case 'notice': {
      const head = (ev.token || '').split('\n\n')[0];
      boundary();
      notice('call', sanitize(head));
      break;
    }

    // Mid-run readings. Nothing goes in the feed for either — the status bar
    // already moved — but they carry what the round's thinking is costing, and
    // the thinking they are pricing is on screen above.
    case 'rate':
    case 'usage':
      priceThinking(ev);
      break;

    case 'done':
      priceThinking(ev);
      closeCalls();
      settleProse();
      finish();
      break;

    case 'cancelled':
      closeCalls();
      settleProse();
      notice('warn', 'cancelled');
      finish();
      break;

    case 'error':
      closeCalls();
      settleProse();
      notice('err', 'error: ' + clip(sanitize(ev.token || ev.err || ''), 400));
      finish();
      break;

    // A session change replaces the conversation, so the feed goes with it:
    // what is on screen belongs to the session that was open.
    case 'session': {
      const d = ev.data || {};
      reset();
      if (!loading) { notice('', 'new session'); break; }
      let text = 'resumed session ' + (d.session_id || '');
      const n = num(d.msg_count);
      if (n > 0) text += ' · ' + n + (n === 1 ? ' message' : ' messages');
      push('resumed', { text });
      break;
    }

    case 'session_title':
      if (ev.data && ev.data.title) notice('', 'named · ' + sanitize(ev.data.title));
      break;

    case 'provider': {
      const d = ev.data || {};
      if (d.provider) notice('', ('now on ' + d.provider + ' ' + shortModel(d.model || '')).trim());
      break;
    }

    case 'effort':
      notice('', 'thinking effort: ' + ((ev.data && ev.data.effort) || 'default'));
      break;

    // "model" carries the raw provider chunk for debugging, and the bare
    // acknowledgements carry nothing worth a row. Both stay out of the feed.
    default:
      break;
  }
}

// priceThinking puts a round's thinking cost on the thinking it paid for. The
// walk stops at the last user message: a round reports its own thinking, and an
// earlier turn's must never be relabelled with this one's number.
function priceThinking(ev) {
  const n = num(status.ThinkTokens), rate = num(status.ThinkTPS);
  if (n <= 0 && rate <= 0) return;
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    if (b.kind === 'user') return;
    if (b.kind === 'think') {
      if (n > 0) { b.tokens = n; b.tokEst = !!status.ThinkEst; }
      if (rate > 0) b.tps = rate;
      drawThinkHead(b);
      return;
    }
  }
}

// settle attaches a tool_output to the pending call it answers. Results come
// back in call order, so the first pending call with a matching name is the
// right one; an unmatched result still gets a row rather than vanishing.
function settle(name, data) {
  const r = resultText(name, data);
  if (!r.ok) callFail++;

  const b = openCalls();
  for (const a of b.acts) {
    if (a.state === 'pending' && a.name === name) {
      settleRow(a, r.ok ? 'ok' : 'fail', r.short, r.note);
      drawCallsHead(b);
      return;
    }
  }
  const a = { name, arg: '', args: '', state: 'pending' };
  addRow(b, a);
  settleRow(a, r.ok ? 'ok' : 'fail', r.short, r.note);
  callCount++;
  drawCallsHead(b);
}

function finish() {
  runStart = 0;
}

// ------------------------------------------------------------------ the DOM

// build makes the element for a block once. Everything that changes later —
// streamed text, a call's result, a thinking block's price — is written into a
// node this function has already put in place.
function build(b) {
  const el = document.createElement('div');
  el.className = 'b ' + b.kind;
  // Set before the switch: the head renderers below read b.el to put the run's
  // verdict on the rail, and a block cannot be drawn before it exists.
  b.el = el;

  switch (b.kind) {
    case 'user':
      b.textNode = el.appendChild(document.createTextNode(b.text || ''));
      if (b.shots && b.shots.length) el.appendChild(shotsOf(b.shots));
      break;

    case 'prose': {
      el.innerHTML = '<div class="body"></div>';
      b.body = el.firstChild;
      b.textNode = b.body.appendChild(document.createTextNode(''));
      break;
    }

    case 'think': {
      el.className += showThink ? '' : ' folded';
      el.innerHTML = '<div class="head"></div><div class="body"></div>';
      b.head = el.firstChild;
      b.body = el.lastChild;
      b.textNode = b.body.appendChild(document.createTextNode(''));
      b.head.addEventListener('click', () => el.classList.toggle('folded'));
      drawThinkHead(b);
      break;
    }

    case 'calls': {
      el.className += foldCalls ? ' folded' : '';
      el.innerHTML = '<div class="head"></div><div class="rows"></div>';
      b.head = el.firstChild;
      b.rows = el.lastChild;
      b.head.addEventListener('click', () => el.classList.toggle('folded'));
      drawCallsHead(b);
      break;
    }

    case 'notice':
      el.className += b.tone ? ' ' + b.tone : '';
      el.textContent = b.text;
      break;

    case 'resumed':
      el.textContent = b.text;
      break;

    case 'hint':
      el.innerHTML =
        '<dl>' +
          '<dt>the rail</dt><dd class="legend">' +
            '<span><i class="dot"></i>you spoke</span>' +
            '<span><i class="think"></i>the agent thought</span>' +
            '<span><i class="act"></i>the agent touched this machine</span>' +
          '</dd>' +
          '<dt>keys</dt><dd>' +
            '<kbd>enter</kbd> send &nbsp;·&nbsp; <kbd>shift+enter</kbd> newline &nbsp;·&nbsp; <kbd>esc</kbd> stop<br>' +
            '<kbd>ctrl+t</kbd> thinking &nbsp;·&nbsp; <kbd>tab</kbd> fold calls &nbsp;·&nbsp; <kbd>ctrl+b</kbd> background' +
          '</dd>' +
          '<dt>images</dt><dd>drop a file on the window, or paste one</dd>' +
        '</dl>';
      break;
  }
  return el;
}

function drawThinkHead(b) {
  if (!b.head) return;
  const bits = [];
  if (b.tokens > 0) bits.push(b.tokens + (b.tokEst ? '~' : '') + ' tok');
  if (b.tps > 0) bits.push(b.tps.toFixed(0) + ' t/s');
  b.head.textContent = 'thinking';
  if (bits.length) {
    const cost = document.createElement('span');
    cost.className = 'cost';
    cost.textContent = '  ·  ' + bits.join(' · ');
    b.head.appendChild(cost);
  }
}

function drawCallsHead(b) {
  if (!b.head) return;
  const n = b.acts.length;
  const failed = b.acts.some(a => a.state === 'fail');
  b.el.classList.toggle('open', b.open);
  b.el.classList.toggle('failed', failed && !b.open);

  const secs = ((b.open ? performance.now() : b.end || performance.now()) - b.start) / 1000;
  b.head.textContent = n + (n === 1 ? ' call' : ' calls');

  const clock = document.createElement('span');
  clock.className = 'clock';
  clock.textContent = secs.toFixed(1) + 's';
  b.head.appendChild(clock);
}

function addRow(b, a) {
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = '<span class="name"></span><span class="arg"></span><span class="res"></span>';
  const [name, arg, res] = row.children;
  name.textContent = a.name;
  name.className = 'name ' + (TOOL_DRIVE.has(a.name) ? 'drive' : TOOL_LOOK.has(a.name) ? 'look' : '');
  arg.textContent = a.arg || '';
  arg.title = a.args || '';
  res.textContent = '·';

  a.row = row;
  a.resEl = res;
  a.state = 'pending';
  b.acts.push(a);
  b.rows.appendChild(row);
}

// settleRow lights the result as it lands and lets the CSS transition take it
// back down, so a batch reads as a sequence of events rather than a table that
// appeared all at once.
function settleRow(a, state, short, note) {
  a.state = state;
  if (!a.row) return;
  a.row.classList.add(state, 'lit');
  a.resEl.textContent = short;
  requestAnimationFrame(() => requestAnimationFrame(() => a.row.classList.remove('lit')));

  if (note) {
    const n = document.createElement('div');
    n.className = 'note';
    n.textContent = note;
    a.row.appendChild(n);
  }
}

// ------------------------------------------------------- the running clock

// One timer, and only while a batch of calls is open. An idle window schedules
// nothing at all: no animation loop, no polling, no wakeups.
let ticker = 0;

function tickIfBusy() {
  const busy = !!callsBlock || BUSY.has(status.State);
  if (busy && !ticker) {
    ticker = setInterval(() => {
      if (callsBlock) drawCallsHead(callsBlock);
      drawStatus();
    }, 90);
  } else if (!busy && ticker) {
    clearInterval(ticker);
    ticker = 0;
  }
}

// ----------------------------------------------------------- the status bar

const BUSY = new Set(['running', 'tools']);

// The worker's state names describe its own machinery; these describe what is
// happening to the user's computer, which is the thing being watched. ACTING is
// called out because it is the only state in which the machine is being touched.
const STATE_WORD = {
  idle: 'ready',
  running: 'writing',
  tools: 'acting',
  done: 'ready',
  error: 'error',
  cancelled: 'stopped',
};

const GAUGE_CELLS = 14;

function drawStatus() {
  const st = status.State || 'idle';
  const busy = BUSY.has(st);
  const frag = document.createDocumentFragment();

  frag.appendChild(span('state' + (busy ? ' busy' : st === 'error' ? ' err' : ''),
    STATE_WORD[st] || st));

  if (callCount) {
    frag.appendChild(span('', callCount + (callCount === 1 ? ' call' : ' calls')));
    if (callFail) frag.appendChild(span('fail', callFail + ' failed'));
  }
  if (runStart) frag.appendChild(span('', ((performance.now() - runStart) / 1000).toFixed(1) + 's'));

  const tps = num(status.TPS);
  if (tps > 0) frag.appendChild(span('', tps.toFixed(0) + ' t/s' + (status.TPSEst ? '~' : '')));

  // Pushed to the right, and segmented rather than smooth: the only question it
  // answers is how much room is left, and a smooth bar reads as a download.
  const used = num(status.ContextUsed), max = num(status.ContextMax);
  if (max > 0 && used > 0) {
    const pct = Math.min(1, used / max);
    const wrap = span('gap', '');
    wrap.appendChild(document.createTextNode('context'));

    const g = document.createElement('span');
    g.className = 'gauge' + (pct > 0.88 ? ' hot' : pct > 0.66 ? ' warm' : '');
    const on = Math.max(1, Math.round(pct * GAUGE_CELLS));
    for (let i = 0; i < GAUGE_CELLS; i++) {
      const cell = document.createElement('i');
      if (i < on) cell.className = 'on';
      g.appendChild(cell);
    }
    wrap.appendChild(g);
    wrap.appendChild(document.createTextNode(Math.round(pct * 100) + '%'));
    frag.appendChild(wrap);
  }
  statusEl.replaceChildren(frag);
}

// go calls into the Go side when there is one. Served on its own the page has
// no bindings, and the demo replay drives it through window.__cua instead.
function go(name, ...args) {
  const fn = window[name];
  if (typeof fn === 'function') fn(...args);
}

function span(cls, text) {
  const s = document.createElement('span');
  if (cls) s.className = cls;
  if (text) s.textContent = text;
  return s;
}

// --------------------------------------------------------- what is attached

// A window can take a picture two ways a terminal cannot: a file dropped on it,
// and a real clipboard paste. Both arrive as File objects, both end in the same
// list, and the list is what send() puts on the wire.

const MAX_IMAGE = 8 << 20;   // deck/attach.go's cap, for the same reason
const KINDS = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

// attach reads files onto the next message. Asynchronous because reading one
// is, and awaited together so several dropped at once keep their order rather
// than racing into whatever order they finish in.
async function attach(files) {
  const wanted = [...files].filter(f => KINDS.has(f.type));
  if (!wanted.length) {
    if (files.length) notice('warn', 'not an image: png, jpeg, gif or webp only');
    return;
  }
  for (const f of wanted) {
    if (f.size > MAX_IMAGE) {
      notice('warn', f.name + ' is ' + fmtBytes(f.size) + ' — the limit is ' + fmtBytes(MAX_IMAGE));
      continue;
    }
    try {
      pending.push({ name: f.name || 'clipboard.png', mime: f.type, size: f.size, b64: await b64of(f) });
    } catch (e) {
      notice('err', 'could not read ' + (f.name || 'that file') + ': ' + e);
    }
  }
  drawTray();
}

// b64of reads a File as base64. Through a data URL because that is the only
// reader every one of the three webviews implements the same way; the prefix
// is cut because the wire and the providers both want the payload alone.
function b64of(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error);
    r.onload = () => resolve(String(r.result).split(',', 2)[1] || '');
    r.readAsDataURL(file);
  });
}

function fmtBytes(n) {
  if (n >= (1 << 20)) return (n / (1 << 20)).toFixed(1) + 'MB';
  if (n >= (1 << 10)) return Math.round(n / (1 << 10)) + 'KB';
  return n + 'B';
}

function dataURL(a) { return 'data:' + (a.mime || 'image/png') + ';base64,' + a.b64; }

// drawTray redraws the chips. The one place in the app that rebuilds rather
// than appends, and allowed to: the tray holds a handful of items, it is not
// in the feed, and it changes only when a person adds or removes one.
function drawTray() {
  tray.hidden = pending.length === 0;
  if (tray.hidden) { tray.replaceChildren(); return; }

  const frag = document.createDocumentFragment();
  pending.forEach((a, i) => {
    const chip = document.createElement('div');
    chip.className = 'chip';

    const img = document.createElement('img');
    img.src = dataURL(a);
    img.alt = '';
    chip.appendChild(img);
    chip.appendChild(span('name', a.name));
    chip.appendChild(span('size', fmtBytes(a.size)));

    const x = document.createElement('button');
    x.className = 'x';
    x.type = 'button';
    x.textContent = '×';
    x.title = 'remove ' + a.name;
    x.addEventListener('click', () => { pending.splice(i, 1); drawTray(); input.focus(); });
    chip.appendChild(x);

    frag.appendChild(chip);
  });
  tray.replaceChildren(frag);
}

// shotsOf is the same pictures once the message is sent, drawn in the feed.
// A replayed turn has names and no payload, so a name is what it gets.
function shotsOf(shots) {
  const box = document.createElement('div');
  box.className = 'shots';
  for (const a of shots) {
    if (!a.b64) { box.appendChild(span('named', '▣ ' + (a.name || 'image'))); continue; }
    const img = document.createElement('img');
    img.src = dataURL(a);
    img.alt = a.name || '';
    img.title = a.name || '';
    box.appendChild(img);
  }
  return box;
}

// A file dropped anywhere on the window lands on the next message. Anywhere,
// because the target of the gesture is the conversation and not a rectangle in
// it — and preventDefault on both events, because a webview's default answer to
// a dropped file is to navigate the window to it, which ends the session.
document.addEventListener('dragover', e => {
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  document.body.classList.add('dropping');
});
document.addEventListener('dragleave', e => {
  // Only the one that leaves the window itself: dragging across the gaps
  // between elements fires this constantly.
  if (!e.relatedTarget) document.body.classList.remove('dropping');
});
document.addEventListener('drop', e => {
  e.preventDefault();
  document.body.classList.remove('dropping');
  if (e.dataTransfer && e.dataTransfer.files.length) attach(e.dataTransfer.files);
});

// A paste with a picture in it is an attachment; a paste with text in it is
// text, and is left to the textarea to handle as it always did.
//
// Two ways, because one of them is not reliable here. A browser puts the
// picture in clipboardData and this is over in a line. A webview often does
// not — WKWebView hands over an empty file list for an image copied by
// anything but itself — and an empty event is indistinguishable from an
// ordinary text paste. So when the event carries nothing, Go is asked, using
// the same OS-level reader the terminal frontend uses.
document.addEventListener('paste', e => {
  const files = imagesIn(e.clipboardData);
  if (files.length) {
    e.preventDefault();
    attach(files);
    return;
  }
  // Deliberately not preventDefault'd: if there turns out to be no picture,
  // this was a text paste and it has to land in the box like any other.
  askClipboard();
});

// A window with focus anywhere but the textarea gets no paste event at all in
// some webviews, so the keystroke is watched too. askClipboard is idempotent
// for one clipboard, so the two firing together costs a second read and
// attaches once.
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'v' || e.key === 'V')) askClipboard();
});

// imagesIn pulls pictures out of a clipboard event, both ways one can be in
// there: as a file list, and as items that have to be asked for one at a time.
function imagesIn(cd) {
  if (!cd) return [];
  const out = [...(cd.files || [])].filter(f => KINDS.has(f.type));
  if (out.length) return out;
  for (const it of cd.items || []) {
    if (it.kind !== 'file' || !KINDS.has(it.type)) continue;
    const f = it.getAsFile();
    if (f) out.push(f);
  }
  return out;
}

// askClipboard reads the system clipboard through Go and attaches whatever
// picture is on it. A rejection means there was not one, which is what most
// pastes are and is not worth saying anything about.
//
// The same picture is never attached twice: the two triggers above can both
// fire for one keypress, and a clipboard read is by definition repeatable.
async function askClipboard() {
  if (typeof window.goClipboard !== 'function') return;
  try {
    const img = await window.goClipboard();
    if (!img || !img.b64) return;
    if (pending.some(a => a.b64 === img.b64)) return;
    pending.push({ name: img.name, mime: img.mime || 'image/png', size: img.size, b64: img.b64 });
    drawTray();
  } catch { /* nothing on the clipboard that is a picture */ }
}

// --------------------------------------------------------------- the input

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = input.scrollHeight + 'px';
});

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

document.addEventListener('keydown', e => {
  const ctrl = e.ctrlKey || e.metaKey;

  if (e.key === 'Escape') { go('goCancel'); return; }
  if (ctrl && e.key === 'b') { e.preventDefault(); go('goBackground'); return; }
  if (ctrl && e.key === 't') { e.preventDefault(); toggleThink(); return; }
  if (e.key === 'Tab' && !ctrl) { e.preventDefault(); toggleCalls(); return; }

  // Anything else typed anywhere goes to the input, so the window never has a
  // dead keystroke: there is only one place text can go.
  if (document.activeElement !== input && !ctrl && e.key.length === 1) input.focus();
});

function send() {
  const text = input.value.trim();
  // A message that is nothing but a picture is a message: drop a screenshot in,
  // press enter.
  if (!text && !pending.length) return;
  const shots = pending;
  pending = [];
  drawTray();
  input.value = '';
  input.style.height = 'auto';

  // Echoed locally rather than waited for: the worker replays user messages
  // only when a conversation is reopened, and a message that took a round trip
  // to appear would read as lag.
  if (!BUSY.has(status.State)) {
    closeCalls();
    callCount = 0;
    callFail = 0;
    runStart = performance.now();
  }
  push('user', { text, shots });
  settleProse();
  scrollToBottom();
  // goSend when there is nothing attached, so the common message crosses the
  // binding it always did and a page talking to an older build still works.
  if (shots.length) go('goSendWith', text, shots.map(a => ({ name: a.name, b64: a.b64 })));
  else go('goSend', text);
}

function toggleThink() {
  showThink = !showThink;
  for (const b of blocks) if (b.kind === 'think') b.el.classList.toggle('folded', !showThink);
}

function toggleCalls() {
  foldCalls = !foldCalls;
  for (const b of blocks) if (b.kind === 'calls') b.el.classList.toggle('folded', foldCalls);
}

// --------------------------------------------------------------- scrolling

feed.addEventListener('scroll', () => {
  pinned = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 28;
}, { passive: true });

function scrollToBottom() {
  feed.scrollTop = feed.scrollHeight;
  pinned = true;
}

// ------------------------------------------------------ decoding tool calls

// parseCalls covers all three provider dialects, which cross the wire verbatim:
//
//   ollama     {"function": {"name": ..., "arguments": {...}}}
//   openai     {"id": ..., "function": {"name": ..., "arguments": "<json>"}}
//   anthropic  {"type": "tool_use", "id": ..., "name": ..., "input": {...}}
//
// Anything unrecognised is kept visible rather than dropped — a silent empty
// round would be worse than an ugly one.
function parseCalls(raw) {
  raw = (raw || '').trim();
  if (!raw) return [];

  let list;
  try { list = JSON.parse(raw); } catch { list = null; }
  if (!Array.isArray(list)) return [{ name: 'tool', arg: sanitize(raw), args: raw }];

  return list.map(c => {
    let name = c && c.name, args = c && c.input;
    if (c && c.function) { name = c.function.name; args = c.function.arguments; }
    // Kept whole as well as summarised: the row has space for a shape, the
    // tooltip has space for the call.
    const verbatim = typeof args === 'string' ? args : JSON.stringify(args == null ? {} : args);
    return { name: name || '?', arg: formatArgs(name, decodeArgs(args)), args: verbatim };
  });
}

// decodeArgs accepts both encodings: an object (ollama, anthropic) and a JSON
// string holding an object (openai).
function decodeArgs(v) {
  if (v == null) return {};
  if (typeof v === 'string') {
    try { v = JSON.parse(v); } catch { return {}; }
  }
  return (v && typeof v === 'object' && !Array.isArray(v)) ? v : {};
}

// formatArgs renders a call's arguments as one short line. Tools with a known
// schema get a shape worth reading; everything else falls back to sorted
// key=value, so the same call always reads the same way.
function formatArgs(name, m) {
  if (!m || !Object.keys(m).length) return '';

  switch (name) {
    case 'click': {
      let s = point(m, 'x', 'y');
      const b = str(m.button);
      if (b && b !== 'left') s += ' ' + b;
      if (numOf(m.clicks) > 1) s += ' x' + fmtNum(m.clicks);
      return s;
    }

    case 'mouse_move':
      return point(m, 'x', 'y');

    case 'scroll': {
      let s = point(m, 'x', 'y');
      const dx = numOf(m.dx), dy = numOf(m.dy);
      if (dx || dy) s = (s + ' ' + arrow(dx, dy) + fmtNum(Math.abs(dx) + Math.abs(dy))).trim();
      return s;
    }

    case 'type_text':
      return JSON.stringify(clip(str(m.text), 120));

    case 'key':      return str(m.combo);
    case 'app_open': return str(m.app);
    case 'skill':    return str(m.skill);
    case 'wait':     return has(m, 'seconds') ? fmtNum(m.seconds) + 's' : '';
    case 'file':     return (str(m.action) + ' ' + str(m.path)).trim();
    case 'shell':    return clip(str(m.command), 120);

    // The steps themselves are the interesting part of a plan and there is no
    // room for them, so a plan reports how many it holds and everything else
    // reports which item it touched.
    case 'todo': {
      let rest = '';
      const n = len(m.steps);
      if (n > 0) rest = n + (n === 1 ? ' step' : ' steps');
      else if (has(m, 'id')) rest = '#' + fmtNum(m.id);
      return (str(m.action) + ' ' + rest).trim();
    }

    // Host first, then the goal: the goal is what the row is about, but a wall
    // of goals with no domains is unreadable when several are in flight.
    case 'WebFetch': {
      let s = host(str(m.url));
      if (str(m.mode) === 'full') s += ' full';
      if (m.goal) s += '  ' + clip(str(m.goal), 80);
      return s.trim();
    }

    case 'agent':
      return (str(m.agent) + '  ' + clip(str(m.prompt), 100)).trim();

    case 'describe_image':
      return ((str(m.source) || 'screen') + '  ' + clip(str(m.question), 90)).trim();

    case 'workflow':
      return (str(m.workflow) + '  ' + clip(kvPairs(decodeArgs(m.args)), 80)).trim();

    case 'screenshot': {
      const parts = [];
      if (m.region) parts.push(str(m.region));
      if (has(m, 'zoom') && m.zoom !== 1) parts.push('zoom ' + fmtNum(m.zoom));
      return parts.join('  ');
    }
  }
  return kvPairs(m);
}

// resultText summarises one tool_output payload into the short text for the
// result column, the failure detail that earns its own row, and whether the
// call succeeded — a dispatch failure comes back as {"error": ...} in place of
// {"result": ...}.
function resultText(name, data) {
  const fine = { short: 'ok', note: '', ok: true };
  const outer = data && data.result;
  if (outer == null || typeof outer !== 'object') return fine;

  if (typeof outer.error === 'string') {
    return { short: 'failed', note: clip(sanitize(outer.error), 400), ok: false };
  }
  const r = (outer.result && typeof outer.result === 'object') ? outer.result : {};

  switch (name) {
    // The worker deliberately keeps images off the wire and sends a count in
    // their place, so a count is all there is to report.
    case 'screenshot':
    case 'photos':
      for (const k of ['n', 'count']) {
        if (has(r, k)) return { short: fmtNum(r[k]) + ' img', note: '', ok: true };
      }
      break;

    case 'app_list':
      return { short: (len(r.running) + len(r.installed)) + ' apps', note: '', ok: true };

    // How far through the plan the agent is, which is the one thing about a
    // todo call worth a row in the feed.
    case 'todo':
      if (r.summary) {
        const cur = r.current && r.current.text;
        return { short: cur ? str(r.summary) + ' · ' + clip(str(cur), 40) : str(r.summary), note: '', ok: true };
      }
      break;

    case 'wait':
      if (has(r, 'waited')) return { short: fmtNum(r.waited) + 's', note: '', ok: true };
      break;

    case 'app_open':
      if (r.ok === false) return { short: 'failed', note: '', ok: false };
      break;

    // The worker keeps the page — or the skill's instructions — off the wire
    // and sends the size instead, the same way it does for images.
    case 'WebFetch':
    case 'skill':
      if (has(r, 'chars')) {
        return { short: fmtNum(r.chars) + ' chars' + (r.truncated ? ' cut' : ''), note: '', ok: true };
      }
      if (len(r.fields) > 0) return { short: 'digest', note: '', ok: true };
      break;

    case 'describe_image':
      if (r.answers === false) return { short: 'not in image', note: '', ok: true };
      if (r.provider) return { short: 'described by ' + str(r.provider), note: '', ok: true };
      break;

    // stopped says how the run ended, and only two of the four endings are the
    // agent deciding it was done.
    case 'agent':
    case 'workflow':
      if (str(r.stopped) === 'max_rounds') return { short: 'out of rounds', note: '', ok: false };
      if (str(r.stopped) === 'cancelled') return { short: 'cancelled', note: '', ok: false };
      if (has(r, 'rounds')) return { short: fmtNum(r.rounds) + ' rounds', note: '', ok: true };
      if (has(r, 'agents')) return { short: fmtNum(r.agents) + ' agents', note: '', ok: true };
      break;

    // A non-zero exit is the command's own failure, not a dispatch error, so it
    // never arrives as {"error": ...} — read it off the exit code.
    case 'shell':
      if (r.timeout === true) return { short: 'timeout', note: '', ok: false };
      if (has(r, 'exit_code') && r.exit_code !== 0) {
        return { short: 'exit ' + fmtNum(r.exit_code), note: '', ok: false };
      }
      break;
  }
  return fine;
}

// ----------------------------------------------------------------- markdown

// Inline only, and applied once a message is finished. Block structure is left
// as written: the feed renders with pre-wrap, so a list or a heading already
// has the shape the model gave it, and the only things worth marking up are the
// ones a reader cannot see in plain text.
//
// FENCE is a private-use codepoint: it cannot occur in anything a model writes,
// so a fenced block can be lifted out and put back without a sentinel collision.
const FENCE = '\uE000';

function inlineMarkdown(src) {
  const esc = s => s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

  // Fenced code comes out first, so nothing below touches what is inside it.
  const fences = [];
  let out = src.replace(/```[\w+-]*\n?([\s\S]*?)```/g, (_, body) => {
    fences.push('<pre><code>' + esc(body.replace(/\n$/, '')) + '</code></pre>');
    return FENCE + (fences.length - 1) + FENCE;
  });

  out = esc(out)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/^#{1,6}\s+(.+)$/gm, '<strong>$1</strong>')
    .replace(/^(\s*)[-*]\s+/gm, '$1• ');

  return out.replace(new RegExp('\\n?' + FENCE + '(\\d+)' + FENCE + '\\n?', 'g'), (_, i) => fences[i]);
}

// ------------------------------------------------------------------ helpers

function str(v) { return typeof v === 'string' ? sanitize(v) : v == null ? '' : String(v); }
function num(v) { return typeof v === 'number' && isFinite(v) ? v : 0; }
function numOf(v) { return typeof v === 'number' ? v : 0; }
function has(m, k) { return !!m && typeof m[k] === 'number' && isFinite(m[k]); }
function len(v) { return Array.isArray(v) ? v.length : 0; }

function fmtNum(f) {
  const n = Number(f);
  if (!isFinite(n)) return '';
  return Number.isInteger(n) ? String(n) : String(parseFloat(n.toFixed(4)));
}

function point(m, xk, yk) {
  if (!has(m, xk) || !has(m, yk)) return '';
  return '(' + fmtNum(m[xk]) + ', ' + fmtNum(m[yk]) + ')';
}

// arrow names a scroll direction, preferring the dominant axis.
function arrow(dx, dy) {
  if (Math.abs(dy) >= Math.abs(dx)) return dy < 0 ? 'down ' : 'up ';
  return dx < 0 ? 'left ' : 'right ';
}

function kvPairs(m) {
  return Object.keys(m).sort().map(k => k + '=' + clip(scalar(m[k]), 40)).join(' ');
}

function scalar(v) {
  if (v == null) return '';
  if (typeof v === 'string') return sanitize(v);
  if (typeof v === 'number') return fmtNum(v);
  if (typeof v === 'boolean') return String(v);
  try { return sanitize(JSON.stringify(v)); } catch { return ''; }
}

// host is the domain of a url, for a row with no space for the path. Parsed by
// hand rather than with URL: a malformed url still has to render as something.
function host(u) {
  let s = u.replace(/^https?:\/\//, '');
  const i = s.search(/[/?#]/);
  if (i >= 0) s = s.slice(0, i);
  return s.replace(/^www\./, '');
}

function shortModel(id) {
  const i = id.lastIndexOf('/');
  return i >= 0 ? id.slice(i + 1) : id;
}

// sanitize makes a wire string safe to put in a single row: no control
// characters, nothing that would break the text out of its column.
function sanitize(s) {
  return String(s)
    .replace(/\n/g, '\\n')
    .replace(/\t/g, ' ')
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .trim();
}

function clip(s, n) {
  const r = Array.from(String(s));
  return r.length <= n ? String(s) : r.slice(0, n).join('') + '...';
}

// --------------------------------------------------------------------- boot

reset();
drawStatus();
input.focus();

// Said last: everything above has to exist before the worker's held-back
// startup line is evaluated into the page.
go('goReady');

// ---------------------------------------------------------------- the demo

// ?demo replays a scripted conversation through the same entry point the worker
// uses, so the page can be looked at — in a browser, by a person or by anything
// driving one — without Python, a window, or a mock of the UI standing in for
// the UI. What it draws here is what it draws in the app.
//
// It exists because a GUI is the one part of this program that cannot report on
// itself. A terminal frontend can be diffed against its own output; this one can
// only be seen, so it has to be servable somewhere something can see it.
//
// ?demo&fast collapses every wait and applies each batch on the spot, so the
// conversation is complete before the load event and a screenshot of the page is
// the same picture every time.
function demoBatches() {
  const script = window.__FIXTURE;
  if (!script) return [];
  const out = [];
  for (const b of script.batches) {
    // Text is replayed in pieces rather than whole: streaming is where the feed
    // does its real work, and a fixture handing over finished messages would
    // exercise none of it.
    const events = [];
    for (const ev of b.events) {
      if (ev.state !== 'thinking' && ev.state !== 'content') { events.push(ev); continue; }
      for (const chunk of chunks(ev.token, 24)) events.push({ state: ev.state, token: chunk });
    }
    out.push({ delay: b.delay || 0, status: b.status || {}, events });
  }
  return out;
}

function demoFast() {
  for (const b of demoBatches()) {
    window.__cua.push({ events: b.events, status: b.status, loading: false });
    flushNow();
  }
  document.body.dataset.demo = 'done';
}

async function demoTimed() {
  for (const b of demoBatches()) {
    await new Promise(r => setTimeout(r, b.delay));
    for (const ev of b.events) {
      window.__cua.push({ events: [ev], status: b.status, loading: false });
      if (ev.state === 'thinking' || ev.state === 'content') {
        await new Promise(r => setTimeout(r, 18));
      }
    }
  }
  document.body.dataset.demo = 'done';
}

function chunks(text, n) {
  const out = [];
  for (let i = 0; i < text.length; i += n) out.push(text.slice(i, i + n));
  return out;
}

// The tray is not part of the conversation, so the fixture cannot put anything
// in it: a worker never sends an attachment *to* a frontend, it only receives
// one. It is still part of what the app looks like, so the demo draws a couple
// of chips — painted here rather than pasted in as base64, which would put a
// picture of a picture in the source.
function demoTray() {
  pending = [
    { name: 'failing-tests.png', mime: 'image/png', size: 184320, b64: swatch('#7fa8f0', '#a98fc4') },
    { name: 'screenshot 2026-08-23 at 14.02.11.png', mime: 'image/png', size: 962560, b64: swatch('#d9a15c', '#e8615f') },
  ];
  drawTray();
}

function swatch(a, b) {
  const c = document.createElement('canvas');
  c.width = c.height = 48;
  const g = c.getContext('2d').createLinearGradient(0, 0, 48, 48);
  g.addColorStop(0, a);
  g.addColorStop(1, b);
  const ctx = c.getContext('2d');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 48, 48);
  return c.toDataURL('image/png').split(',', 2)[1];
}

if (location.search.includes('demo')) {
  demoTray();
  if (location.search.includes('fast')) demoFast();
  else demoTimed();
}
