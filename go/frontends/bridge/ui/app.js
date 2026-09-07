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
// mutates - a tool call waiting for its result - already knows which row it is.

const workspace = document.getElementById('workspace');
const input  = document.getElementById('input');
const tray   = document.getElementById('tray');
const statusEl = document.getElementById('status');
const treeEl = document.getElementById('tree');
const sessname  = document.getElementById('sessname');
const projectEl = document.getElementById('project');

// --------------------------------------------------------------- the model

// A session is one conversation: its feed, its stream state, its status. The
// workspace holds several; `cur` is the one on screen. Everything below that
// reads per-session state goes through `cur`, so a batch routed to a background
// session folds into that session's feed without touching the one on screen.
let sessions = {};
let active   = null;
let cur      = null;
const closed = new Set();  // ids closed this window: their late batches are dropped, never resurrected

let showThink = false;  // view prefs, shared across sessions
let foldCalls = false;
let PROJECT = '';       // the directory the app works on, off the first batch

function newSession(id) {
  return {
    id,
    blocks: [],
    callsBlock: null,
    sawOutput: false,
    callCount: 0,
    callFail: 0,
    status: { State: 'idle' },
    runStart: 0,
    lastAct: Date.now(),  // sidebar recency: a sent message or a finished run bumps it; streaming does not
    storeId: null,        // set when this session loads an archived conversation
    replaying: false,     // an archive replay is in flight: its history is not activity
    wasBusy: false,       // last known busy state, for the finished-running edge
    unseen: false,        // a run finished here and nobody has looked since
    project: null,        // this session's own project, off its batches
    pending: [],
    pinned: true,  // stick to the bottom unless the user has scrolled away
    title: '',
    feed: null,
  };
}

// sessionFor returns the session for an id, creating it (and its feed element)
// on first use. A batch without an id belongs to the active session.
function sessionFor(id) {
  if (!id) id = active;
  if (!sessions[id]) {
    const s = newSession(id);
    s.feed = document.createElement('div');
    s.feed.className = 'feed';
    s.feed.tabIndex = -1;
    s.feed.addEventListener('scroll', () => {
      s.pinned = s.feed.scrollTop + s.feed.clientHeight >= s.feed.scrollHeight - 28;
    }, { passive: true });
    workspace.appendChild(s.feed);
    sessions[id] = s;
  }
  return sessions[id];
}

// switchTo shows one session's feed and hides the rest, and tells Go which
// session is active so the bindings (goSend, goCancel, ...) route to the one on
// screen. Without the call the two sides drift the moment a tab is clicked, and
// a message typed in one session goes to another's worker.
function switchTo(id) {
  // Already on screen: clicking its row again (or the palette's row) must
  // not replay the feed's crossfade and yank the scroll to the bottom -
  // that read as the session "refreshing" for no reason.
  if (id === active) return;
  const s = sessionFor(id);
  s.unseen = false;  // looking at it is the check
  active = id;
  cur = s;
  go('goSwitch', id);
  // A switch - or the spawn that follows a "+ new session" click - is the
  // user acting on the tree; the next rebuild may glide. A plain switch moves
  // nothing, so this is usually a no-op.
  sideMotion = true;
  for (const [sid, ss] of Object.entries(sessions)) {
    ss.feed.style.display = sid === id ? '' : 'none';
    if (sid === id) {
      // One fade per switch, replayed by resetting the animation - the stream
      // itself is never animated, only the swap between conversations.
      ss.feed.style.animation = 'none';
      void ss.feed.offsetHeight;
      ss.feed.style.animation = '';
    }
  }
  setTitle(s.title);
  drawSidebar();
  drawStatus();
  drawTray();
  scrollToBottom();
}

// ------------------------------------------------------------- the sidebar

// The sidebar is a tree: projects, and under each the sessions that belong to
// it - the live ones this window spawned, then the store's archive. One row per
// session: a dot (amber, breathing, while that session's worker is busy; green
// when a run finished there and nobody has looked since), the title, and how
// long ago the conversation last moved. Archive rows carry no dot.

let storeList = [];           // the store's meta records, from session.list
let promoted = [];            // dirs the user promoted to projects (project.list)
let projNames = {};           // promoted dir -> custom display name (cosmetics; group key stays the basename)
let projIcons = {};           // promoted dir -> one emoji, riding where the folder glyph would
let storeSort = 'recent';     // sessions within each group: recency, or name
const expanded = new Set();   // projects the user opened - every group starts closed
const moreShown = new Map();  // project -> rows shown; 'Show more' pages it up by 5
const pins = new Map();       // pinned session ids (store ids), ts; persisted on the meta via session.pin
const projPins = new Map();   // pinned project names, in pin order; persisted in projects.json
let pinSeq = 0;
const SHOW_N = 6;
const opened = new Map();     // project name -> dir, picked via folder+ before it has any sessions

// The rail: the sidebar collapsed to its icons. Two sizes only - 268px out,
// 76px in - and the user never resizes; the window forces the rail below
// 800px, where the tree has no room to be a tree.
const sideEl = document.querySelector('.side');
let sideRail = false;         // the user's collapse, this window's own
const narrowQ = matchMedia('(max-width: 800px)');
const railOn = () => sideRail || narrowQ.matches;
document.getElementById('sidetoggle').addEventListener('click', () => {
  editingProj = null;  // collapsing takes the editor with it
  sideRail = !sideRail;
  sideMotion = true;
  drawSidebar();
});
narrowQ.addEventListener('change', () => { editingProj = null; sideMotion = true; drawSidebar(); });

// Tree motion: only the user's own actions animate the tree - what moved
// glides in from where it was, what arrived fades in. Batch redraws never set
// the flag, so streams, dots and titles never move anything, and ?demo&fast
// keeps its deterministic screenshots.
const NOMOTION = location.search.includes('fast');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
let sideMotion = false;       // set by interactive paths, consumed by the next rebuild
let sideBorn = false;         // the first render is the tree's birth: no entrances on it

const GRADS = 6;              // the editor's gradient circles; icon "grad:<n>", CSS .grad-<n>

const ICONS = {
  chev:   '<path d="M9 6l6 6-6 6"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 20h16"/>',
  check:  '<path d="M4 12l5 5L20 7"/>',
  cross:  '<path d="M6 6l12 12M18 6L6 18"/>',
  folder: '<path d="M3 7c0-1.1.9-2 2-2h4l2 2h8c1.1 0 2 .9 2 2v9c0 1.1-.9 2-2 2H5c-1.1 0-2-.9-2-2V7z"/>',
  pin:    '<path d="M12 17v5M9 10.8a2 2 0 0 1-1.1 1.8l-1.8.9A2 2 0 0 0 5 15.3V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.7a2 2 0 0 0-1.1-1.8l-1.8-.9a2 2 0 0 1-1.1-1.8V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/>',
  minus:  '<path d="M5 12h14"/>',
  plus:   '<path d="M12 5v14M5 12h14"/>',
  up:     '<path d="M18 15l-6-6-6 6"/>',
  down:   '<path d="M6 9l6 6 6-6"/>',
  pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  trash:  '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/>',
};

function ic(name, cls) {
  const s = document.createElement('span');
  s.className = 'ic' + (cls ? ' ' + cls : '');
  s.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS[name] + '</svg>';
  return s;
}

// ago renders a timestamp the way the mock says them: just now, 5m, 3h, 1d.
function ago(iso) {
  const t = typeof iso === 'number' ? iso : Date.parse(iso || '');
  if (!t || isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

// projName is the project a store record belongs to: the basename of the
// directory it was launched from, which is what a project is (project >
// sessions). Records with no cwd land in one group at the end.
function projName(cwd) {
  if (!cwd) return 'no project';
  const base = cwd.replace(/\/+$/, '').split('/').pop();
  return base || 'no project';
}

// A promoted project can carry a custom display name and a one-emoji icon
// (projects.json, via project.rename). The group key never changes - the
// basename is what sessions, pins and scopes match on - these only change
// what renders.
function projDirOf(name) {
  const d = promoted.find(x => projName(x) === name);
  if (d) return d;
  // Not promoted: the current project's group still resolves a dir off the
  // store's records, so the launch repo is as editable as any promoted one.
  const m = storeList.find(x => x.cwd && projName(x.cwd) === name);
  return (m && m.cwd) || '';
}
function displayNameOf(name) { const d = projDirOf(name); return (d && projNames[d]) || name; }
function iconOf(name) { const d = projDirOf(name); return (d && projIcons[d]) || ''; }

// The head's / rail's folder glyph for a project's icon: a gradient circle
// ("grad:n"), an uploaded image ("img:data:..."), one emoji, or the plain
// folder when there is none.
function iconNode(name) {
  const icon = iconOf(name);
  if (icon.startsWith('img:')) {
    const i = document.createElement('img');
    i.className = 'projicon-img';
    i.src = icon.slice(4);
    i.alt = '';
    return i;
  }
  if (icon.startsWith('grad:')) return span('projicon-grad grad-' + icon.slice(5), '');
  return icon ? span('projicon', icon) : ic('folder');
}

// drawSidebar renders the tree. It rebuilds rather than diffs - the tree is a
// few dozen rows - but apply() calls it once per batch and a stream is batches
// per frame, so a signature guards the rebuild: nothing moves while a batch
// only carried text.
let sideSig = '';
let editingProj = null;       // the group whose details are being edited; the tree freezes around it
let editMounted = false;      // the editor is on screen; redraws must not steal its focus
let editName = '', editIcon = '';
function drawSidebar() {
  const live = Object.entries(sessions).map(([id, s]) =>
    id + ':' + (s.title || '') + ':' + (BUSY.has(s.status.State) ? 1 : 0) + ':' + (s.storeId || '') + ':' + (s.unseen ? 1 : 0) + ':' + (s.project || '') + ':' + (s.lastAct || ''));
  const sig = active + '|' + live.join() + '|' + PROJECT + '|' + storeSort +
    '|' + [...expanded].join() + '|' + [...moreShown].join() + '|' + [...pins.keys()].join() + '|' +
    storeList.map(m => m.id + '\u0001' + (m.title || '') + '\u0001' + (m.updated || '')).join('\u0002') +
    '|' + [...opened].join() + '|' + [...projPins.keys()].join() + '|' + promoted.join('\u0002') +
    '|' + (railOn() ? 1 : 0) + '|' + (editingProj || '') +
    '|' + JSON.stringify(projNames) + JSON.stringify(projIcons);
  if (sig === sideSig) return;
  sideSig = sig;

  // Merge: one row per session. A store row whose conversation is already
  // loaded into a live session yields to it - the live row is the same
  // conversation with the running dot on it.
  const loaded = new Set(Object.values(sessions).map(s => s.storeId).filter(Boolean));
  const groups = new Map();
  const add = (proj, row) => {
    if (!groups.has(proj)) groups.set(proj, []);
    groups.get(proj).push(row);
  };
  for (const [id, s] of Object.entries(sessions)) {
    add(s.project || PROJECT || 'no project', {
      id, live: true,
      pk: s.storeId || id,  // the pin key: the store id is what persists
      title: s.title || 'New session',
      t: s.lastAct || 0,
      busy: BUSY.has(s.status.State),
      unseen: s.unseen,
    });
  }
  // A project group is the current cwd's, one holding a live session, or one
  // the user promoted (plus the beat before a fresh pick's promotion lands).
  // Every other directory - the demo folder, the one-off - stays out of the
  // tree; the palette still finds all of its sessions by name.
  const promotedNames = new Set(promoted.map(projName));
  const want = proj => proj === PROJECT || promotedNames.has(proj) || opened.has(proj);
  for (const m of storeList) {
    if (loaded.has(m.id)) continue;
    const proj = projName(m.cwd);
    if (!want(proj) && !groups.has(proj)) continue;  // a live session's group always shows
    add(proj, {
      id: m.id, live: false,
      pk: m.id,
      title: m.title || 'New session',
      t: Date.parse(m.updated || '') || 0,
    });
  }
  for (const [name] of opened) {
    if (!groups.has(name)) groups.set(name, []);
  }

  const projs = [...groups.keys()];
  // The groups hold their places - the tree must not reshuffle as sessions
  // open and age. Pinned projects first, in pin order; then the project the
  // window is working in; then the promoted ones in promotion order
  // (projects.json); then whatever else (live-only groups, no-project)
  // alphabetically. The sort toggle orders the sessions inside each group,
  // never the groups themselves.
  const promotedIndex = name => promoted.findIndex(d => projName(d) === name);
  projs.sort((a, b) => {
    const pa = projPins.has(a), pb = projPins.has(b);
    if (pa !== pb) return pa ? -1 : 1;
    if (pa) return projPins.get(a) - projPins.get(b);
    if (a === PROJECT) return -1;
    if (b === PROJECT) return 1;
    const ia = promotedIndex(a), ib = promotedIndex(b);
    if ((ia >= 0) !== (ib >= 0)) return ia >= 0 ? -1 : 1;
    if (ia >= 0) return ia - ib;
    return a.localeCompare(b);
  });

  // Motion setup, before the old tree is discarded: remember where everything
  // stood so what moved can glide in from there. Interactive rebuilds only.
  const animate = sideMotion && !REDUCED && !NOMOTION;
  sideMotion = false;
  const oldTops = new Map();
  if (animate) {
    for (const el of treeEl.children) oldTops.set(el.dataset.key || '', el.offsetTop);
  }
  const enter = animate && sideBorn;
  sideBorn = true;

  treeEl.replaceChildren();
  sideEl.classList.toggle('rail', railOn());
  if (railOn()) {
    // The rail: one folder glyph per project, its news dot riding it - the
    // tree's shape, not its rows. No titles, no pins, no pager; the order is
    // the tree's own (pins first), so the rail never lies about what sits on
    // top. A custom name or emoji renders here too. A click opens the
    // sidebar on that project.
    for (const proj of projs) {
      const rows = groups.get(proj);
      const b = document.createElement('button');
      b.className = 'railrow' + (rows.some(r => r.live && r.id === active) ? ' cur' : '');
      b.title = displayNameOf(proj);
      b.appendChild(iconNode(proj));
      const busy = rows.some(r => r.busy);
      if (busy || rows.some(r => r.unseen)) b.appendChild(span('dot' + (busy ? ' busy' : ''), ''));
      b.addEventListener('click', () => {
        sideRail = false;  // opening a project is the point of the click
        expanded.add(proj);
        sideMotion = true;
        drawSidebar();
        for (const h of treeEl.querySelectorAll('.projhead')) {
          if (h.dataset.key === 'p:' + proj) { h.scrollIntoView({ block: 'nearest' }); break; }
        }
      });
      treeEl.appendChild(b);
    }
    return;
  }
  // The details editor freezes the tree: nothing rebuilds under the user's
  // keystrokes until Enter commits (project.rename; the reply re-stamps) or
  // Esc puts the tree back. Streaming batches wait their beat.
  if (editingProj) {
    if (!editMounted) {
      editMounted = true;
      const box = document.createElement('div');
      box.className = 'projedit';
      const ie = document.createElement('input');
      ie.className = 'pe-icon';
      // Only a literal emoji prefills the field - a gradient or an upload is
      // shown by its selected swatch, never as raw text like "grad:2" sitting
      // where an icon belongs.
      ie.value = editIcon.startsWith('grad:') || editIcon.startsWith('img:') ? '' : editIcon;
      ie.maxLength = 8;
      ie.spellcheck = false;
      ie.placeholder = '\ud83d\ude80';
      ie.title = 'one emoji, or pick an icon below';
      const ne = document.createElement('input');
      ne.className = 'pe-name';
      ne.value = editName;
      ne.maxLength = 60;
      ne.placeholder = editingProj;
      ne.spellcheck = false;
      // The icon strip: a default-folder reset, six gradient circles, then
      // upload for your own image. A circle, an emoji and an upload are
      // exclusive - picking one clears the others.
      const strip = document.createElement('div');
      strip.className = 'pe-swatches';
      const markSel = () => { for (const b of strip.children) b.classList.toggle('sel', !!b.dataset.icon && b.dataset.icon === editIcon); };
      const none = document.createElement('button');
      none.className = 'pe-sw pe-none';
      none.dataset.icon = '';
      none.title = 'default folder';
      none.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.folder + '</svg>';
      none.addEventListener('click', () => {
        editIcon = '';
        ie.value = '';
        markSel();
      });
      strip.appendChild(none);
      for (let g = 1; g <= GRADS; g++) {
        const s = document.createElement('button');
        s.className = 'pe-sw grad-' + g;
        s.dataset.icon = 'grad:' + g;
        s.title = 'gradient ' + g;
        s.addEventListener('click', () => {
          editIcon = editIcon === s.dataset.icon ? '' : s.dataset.icon;
          ie.value = '';
          markSel();
        });
        strip.appendChild(s);
      }
      const up = document.createElement('button');
      up.className = 'pe-up';
      up.title = 'use your own image (png / jpg / webp / gif)';
      up.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.upload + '</svg>';
      up.addEventListener('click', () => {
        const raw = go('goPickIcon');
        if (!raw) return;
        let f;
        try { f = JSON.parse(raw); } catch { return; }
        if (!f || !f.b64) return;
        const img = new Image();
        img.onload = () => {
          // Cover-crop into a 64px canvas: the store caps icon payloads, and
          // 64px is far more than a 16px glyph will ever show.
          const c = document.createElement('canvas');
          c.width = c.height = 64;
          const ctx = c.getContext('2d');
          const side = Math.min(img.width, img.height);
          ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 64, 64);
          const url = c.toDataURL('image/png');
          editIcon = 'img:' + url;
          up.style.backgroundImage = 'url(' + url + ')';
          ie.value = '';
          markSel();
        };
        img.src = 'data:' + (f.mime || 'image/png') + ';base64,' + f.b64;
      });
      strip.appendChild(up);
      ie.addEventListener('input', () => { editIcon = ie.value; strip.querySelectorAll('.sel').forEach(b => b.classList.remove('sel')); });
      ne.addEventListener('input', () => { editName = ne.value; });
      const commit = () => {
        const d = projDirOf(editingProj || '');
        editingProj = null;
        editMounted = false;
        go('goCommand', 'project.rename', { dir: d, name: editName.trim(), icon: editIcon.trim() });
        sideMotion = true;
        drawSidebar();  // drops the editor at once; the reply re-stamps
      };
      const cancel = () => { editingProj = null; editMounted = false; drawSidebar(); };
      // Save and cancel as buttons: Enter and Esc work from anywhere in the
      // editor (below), but nobody should have to know that - the way out is
      // on the screen, in both colors.
      const save = document.createElement('button');
      save.className = 'pe-save';
      save.title = 'save';
      save.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.check + '</svg>';
      save.addEventListener('click', e => { e.stopPropagation(); commit(); });
      const drop = document.createElement('button');
      drop.className = 'pe-drop';
      drop.title = 'cancel';
      drop.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.cross + '</svg>';
      drop.addEventListener('click', e => { e.stopPropagation(); cancel(); });
      // Enter and Esc belong to the whole editor, not to whichever input
      // holds focus - a focused swatch ate Enter as a click, and Esc did
      // nothing at all. Bound on the box, they win over the swatch default.
      box.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); commit(); }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(); }
      });
      box.appendChild(ie);
      box.appendChild(ne);
      box.appendChild(save);
      box.appendChild(drop);
      box.appendChild(strip);
      markSel();
      treeEl.appendChild(box);
      ne.focus();
    }
    return;
  }
  editMounted = false;
  for (const proj of projs) {
    const rows = groups.get(proj).slice().sort((a, b) => {
      // Pinned sessions float above the rest of their project, oldest pin
      // first (the value is the pin's ts). The sort toggle orders the rest of
      // the group: recency, or name. The groups themselves never move.
      const pa = pins.has(a.pk), pb = pins.has(b.pk);
      if (pa !== pb) return pa ? -1 : 1;
      if (pa) return (pins.get(a.pk) || 0) - (pins.get(b.pk) || 0);
      if (storeSort === 'name') return (a.title || '').localeCompare(b.title || '');
      return b.t - a.t;
    });
    const open = expanded.has(proj);
    const head = document.createElement('div');
    head.className = 'projhead' + (open ? ' open' : '');
    head.title = proj;
    head.dataset.key = 'p:' + proj;
    head.appendChild(ic('chev', 'chev'));
    head.appendChild(iconNode(proj));
    head.appendChild(span('projname', displayNameOf(proj)));
    // A closed group still owes the user its news: the dot is a notification,
    // so a busy or unseen session inside one lifts its dot to the head.
    // Amber (running) wins over green (finished, unseen).
    if (!open) {
      const busy = rows.some(r => r.busy);
      if (busy || rows.some(r => r.unseen)) head.appendChild(span('dot' + (busy ? ' busy' : ''), ''));
    }
    // The head's tools, packed as one cluster riding the right end - not one
    // absolute slot per button, or the subset a group actually has leaves
    // holes between the icons. Left to right: move, edit, demote, pin (the
    // pin rides closest to the edge); tools that don't apply are absent.
    const tools = document.createElement('span');
    tools.className = 'projhead-tools';
    const pdir = promoted.find(d => projName(d) === proj) || '';
    const edir = pdir || (proj === PROJECT ? projDirOf(proj) : '');
    // New session in this project: the fastest path there is - hover the
    // group, hit +, and the session spawns in the group's own directory. No
    // palette, no folder hunt.
    if (edir) {
      const nb = document.createElement('button');
      nb.className = 'projhead-new';
      nb.title = 'new session in ' + displayNameOf(proj);
      nb.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.plus + '</svg>';
      nb.addEventListener('click', e => {
        e.stopPropagation();
        expanded.add(proj);
        sideMotion = true;
        go('goNewSession', edir).then(id => { if (id) switchTo(id); });
      });
      tools.appendChild(nb);
    }
    if (pdir || projPins.has(proj)) for (const [delta, ttl] of [[-1, 'move up'], [1, 'move down']]) {
      // One slot up/down within the section the group sits in - pin order for
      // pinned groups, promotion order otherwise. The reply re-stamps both
      // lists; the tree's order is explicit state, moved by explicit
      // commands, never a drag.
      const b = document.createElement('button');
      b.className = 'projhead-mv';
      b.title = ttl;
      b.innerHTML = '<svg viewBox="0 0 24 24">' + (delta < 0 ? ICONS.up : ICONS.down) + '</svg>';
      b.addEventListener('click', e => {
        e.stopPropagation();
        go('goCommand', 'project.move', { name: proj, dir: pdir, delta });
        sideMotion = true;  // the reply redraws the moved pair
      });
      tools.appendChild(b);
    }
    if (edir) {
      const ed = document.createElement('button');
      ed.className = 'projhead-edit';
      ed.title = 'edit name \u0026 icon';
      ed.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.pencil + '</svg>';
      ed.addEventListener('click', e => {
        e.stopPropagation();
        editingProj = proj;
        editName = projNames[edir] || '';
        editIcon = projIcons[edir] || '';
        editMounted = false;
        drawSidebar();
      });
      tools.appendChild(ed);
    }
    if (pdir) {
      // Demote: the minus is the promotion's undo. Only a promoted group
      // carries one - the current cwd's group and live sessions' groups stay
      // in the tree regardless.
      const rm = document.createElement('button');
      rm.className = 'projhead-rm';
      rm.title = 'remove ' + proj + ' from projects';
      rm.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.minus + '</svg>';
      rm.addEventListener('click', e => {
        e.stopPropagation();
        sideMotion = true;  // the removal is the user's own action; the reply redraws
        go('goCommand', 'project.remove', { dir: pdir });
      });
      tools.appendChild(rm);
    }
    // The project pin: this window's ordering, persisted with the projects.
    const pp = document.createElement('button');
    pp.className = 'projhead-pin' + (projPins.has(proj) ? ' on' : '');
    pp.title = projPins.has(proj) ? 'unpin project' : 'pin project';
    pp.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.pin + '</svg>';
    pp.addEventListener('click', e => {
      e.stopPropagation();
      if (projPins.has(proj)) projPins.delete(proj);
      else projPins.set(proj, ++pinSeq);
      go('goCommand', 'project.pin', { name: proj, on: projPins.has(proj) });  // the reply re-stamps it
      sideMotion = true;
      drawSidebar();
    });
    tools.appendChild(pp);
    head.appendChild(tools);
    head.addEventListener('click', () => {
      if (expanded.has(proj)) { expanded.delete(proj); moreShown.delete(proj); }  // a reopen starts at SHOW_N again
      else expanded.add(proj);
      sideMotion = true;
      drawSidebar();
    });
    treeEl.appendChild(head);
    if (!open) continue;

    const n = moreShown.get(proj) || SHOW_N;
    const shown = rows.slice(0, n);
    for (const r of shown) {
      const row = document.createElement('div');
      row.className = 'srow' + (r.live && r.id === active ? ' sel' : '');
      row.title = r.title;
      row.dataset.key = 's:' + r.id;
      // Dot = notification. Amber while that session's worker runs; green
      // when a run finished in it and it has not been opened since; nothing
      // otherwise, with the spacer keeping titles aligned across rows.
      row.appendChild(span('dot' + (r.busy ? ' busy' : r.unseen ? '' : ' off'), ''));
      row.appendChild(span('srow-title', r.title));
      row.appendChild(span('when', ago(r.t)));
      // Pin: persisted with the conversation (a ts on the meta), riding over
      // the time's right end; a set pin stays visible - state, not affordance.
      // Pinned rows sit above their project's Show more cut, so a pin is
      // always visible.
      const p = document.createElement('button');
      p.className = 'srow-pin' + (pins.has(r.pk) ? ' on' : '');
      p.title = pins.has(r.pk) ? 'unpin' : 'pin to top';
      p.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.pin + '</svg>';
      p.addEventListener('click', e => {
        e.stopPropagation();
        if (pins.has(r.pk)) pins.delete(r.pk);
        else pins.set(r.pk, Date.now());
        go('goCommand', 'session.pin', { id: r.pk, on: pins.has(r.pk) });  // the reply re-stamps
        sideMotion = true;
        drawSidebar();
      });
      row.appendChild(p);
      if (r.live) {
        const x = document.createElement('button');
        x.className = 'srow-x';
        x.textContent = '\u00d7';
        x.title = 'close ' + r.title;
        x.addEventListener('click', e => { e.stopPropagation(); closeSession(r.id); });
        row.appendChild(x);
      } else {
        // The trash can: out of the tree, not out of the world - the folder
        // moves to the store's archive and comes back by moving it in.
        const t = document.createElement('button');
        t.className = 'srow-trash';
        t.title = 'archive ' + r.title;
        t.innerHTML = '<svg viewBox="0 0 24 24">' + ICONS.trash + '</svg>';
        t.addEventListener('click', e => {
          e.stopPropagation();
          t.disabled = true;
          go('goCommand', 'session.archive', { id: r.id });
          go('goCommand', 'session.list', null);  // the row leaves when the fresh list lands
        });
        row.appendChild(t);
      }
      row.addEventListener('click', () => {
        if (r.live) switchTo(r.id);
        else if (!BUSY.has(sessions[active].status.State)) loadStore(r.id);
        // An archive click while the worker is mid-run waits for nothing: the
        // load would swap the conversation out from under the run, so it is
        // ignored until the run is done or cancelled.
      });
      treeEl.appendChild(row);
    }
    if (!rows.length) {
      // A picked project with nothing in it yet: one row, and it starts the
      // first session in the folder the project was picked from.
      const n = document.createElement('button');
      n.className = 'srow newrow';
      n.dataset.key = 'n:' + proj;
      n.appendChild(span('dot off', ''));
      n.appendChild(span('srow-title', '+ new session'));
      n.addEventListener('click', () => {
        // One spawn per row: the placeholder lives until the new session's
        // first batch replaces it, and a second click would double-spawn.
        if (n.disabled) return;
        n.disabled = true;
        go('goNewSession', opened.get(proj) || '').then(id => { if (id) switchTo(id); });
      });
      treeEl.appendChild(n);
    }
    if (rows.length > SHOW_N) {
      // Paged: 'Show more' adds five a click while rows remain hidden; once
      // everything is shown it becomes 'Show less', the way back to the first
      // SHOW_N. Collapsing the group resets the pager too.
      const less = n >= rows.length;
      const m = document.createElement('button');
      m.className = 'showmore';
      m.dataset.key = 'm:' + proj;
      m.textContent = less ? 'Show less' : 'Show more';
      m.addEventListener('click', () => {
        less ? moreShown.delete(proj) : moreShown.set(proj, n + 5);
        sideMotion = true;
        drawSidebar();
      });
      treeEl.appendChild(m);
    }
  }

  // The motion pass: what moved glides from where it was, what arrived fades
  // in, staggered like the palette's rows. Everything runs on CSS animations
  // that clean up after themselves - no inline styles outlive the element.
  if (animate) {
    let ni = 0;
    for (const el of treeEl.children) {
      const was = oldTops.get(el.dataset.key || '');
      if (was !== undefined) {
        const d = was - el.offsetTop;
        if (d) {
          el.style.setProperty('--flip', d + 'px');
          el.classList.add('flip');
        }
      } else if (enter) {
        el.style.animationDelay = Math.min(ni++ * 18, 110) + 'ms';
        el.classList.add('enter');
      }
    }
  }
}

// revealProject opens a picked directory in the tree without spawning
// anything: its group expands and scrolls into view, and until a session
// exists there the group carries one "+ new session" row. Exact-dir matching
// only - a session spawned in a subdirectory groups under that basename.
function revealProject(dir) {
  const name = projName(dir);
  opened.set(name, dir);
  expanded.add(name);
  sideMotion = true;
  drawSidebar();
  for (const h of treeEl.querySelectorAll('.projhead')) {
    if (h.dataset.key === 'p:' + name) {
      h.scrollIntoView({ block: 'nearest' });
      break;
    }
  }
}

// loadStore opens an archived conversation in the active session's worker: the
// feed resets and the history replays into it, the same path --resume takes.
// goLoad sets the pump's loading flag first, so the replay announces itself as
// a resume rather than a new conversation.
function loadStore(id) {
  go('goLoad', id);
}

// The header icons: sort flips projects between recency and name, + opens the
// palette, and folder+ promotes a folder into the tree without spawning
// anything.
document.getElementById('projadd').addEventListener('click', () => {
  go('goPickFolder').then(dir => {
    if (!dir) return;
    // Picking a folder promotes it: the tree's whitelist is explicit, and
    // this is the user saying so. revealProject renders the beat before the
    // promotion reply lands.
    go('goCommand', 'project.add', { dir });
    revealProject(dir);
    // The boot list may predate sessions saved at this cwd since - by another
    // window, another frontend, or this one elsewhere. One command on a user
    // action, so the no-idle-work rule holds; the tree renders the folder's
    // history newest-first the moment it lands.
    go('goCommand', 'session.list', null);
  });
});
document.getElementById('projsort').addEventListener('click', () => {
  storeSort = storeSort === 'recent' ? 'name' : 'recent';
  drawSidebar();
});
document.getElementById('projnew').addEventListener('click', palOpen);
document.getElementById('search').addEventListener('click', palOpen);

// ------------------------------------------------------------- the palette

// One surface over everything. It finds sessions across every project (live
// ones first), starts a new session in any project the store knows or in a
// folder picked on the spot, and lists the settings - placeholders until a
// settings surface exists, but searchable from day one. `#name` scopes every
// row to one project. DOM only; webview_go has no menus to hang it from.
const veil  = document.getElementById('veil');
const pq    = document.getElementById('pq');
const prows = document.getElementById('prows');
let palRows = [];  // the list, in order; palSel indexes into it
let palSel  = 0;
let palFreshT = 0;  // the entrance stagger's timer
let palCloseT = 0;  // the close fade's timer

function palOpen() {
  veil.classList.remove('closing');
  veil.hidden = false;
  // Every open is a fresh look at the store: sessions saved by other
  // frontends - or by this one elsewhere - show up without a restart. One
  // command per open, on a user action, so the no-idle-work rule holds; the
  // project list rides along, since the actions section curates from it.
  go('goCommand', 'session.list', null);
  go('goCommand', 'project.list', null);
  pq.value = '';
  palSel = 0;
  // The fresh class gives the rows their one staggered entrance; gone in a
  // beat, so typing filters the list without replaying it.
  prows.classList.add('fresh');
  clearTimeout(palFreshT);
  palFreshT = setTimeout(() => prows.classList.remove('fresh'), 400);
  palDraw();
  pq.focus();
}

function palClose() {
  if (veil.hidden) return;
  veil.classList.add('closing');
  input.focus();
  clearTimeout(palCloseT);
  palCloseT = setTimeout(() => {
    veil.hidden = true;
    veil.classList.remove('closing');
  }, 90);
}

function palToggle() { veil.hidden ? palOpen() : palClose(); }

// palRowsFor builds the menu for a query: an ordered list of sections, each
// rendering only when it has rows. The order is the menu's grammar - actions
// first, because they answer "do something" and never depend on recency;
// sessions second, ranked by true recency; settings last, and only when a
// query names them. New surfaces slot into the list as they exist.
function palRowsFor(q) {
  q = q.trim();
  let scope = '';
  const m = q.match(/^#(\S*)\s*(.*)$/);
  if (m) { scope = m[1].toLowerCase(); q = m[2]; }
  const ql = q.toLowerCase();
  const inScope = p => !scope || (p || '').toLowerCase().startsWith(scope);

  const rows = [];
  for (const [id, s] of Object.entries(sessions)) {
    const title = s.title || 'New session';
    const base = s.project || PROJECT;
    const proj = displayNameOf(base);
    if ((inScope(proj) || inScope(base)) && (!ql || title.toLowerCase().includes(ql) || proj.toLowerCase().includes(ql) || base.toLowerCase().includes(ql)))
      rows.push({ kind: 'session', id, live: true, title, proj, t: s.lastAct || 0, busy: BUSY.has(s.status.State) });
  }
  // A store record whose conversation is already live in this window yields
  // to the live row - same conversation, same store id (stamped off the
  // worker's ready line, or off a load).
  const loaded = new Set(Object.values(sessions).map(s => s.storeId).filter(Boolean));
  for (const r of storeList) {
    if (loaded.has(r.id)) continue;
    const title = r.title || 'New session';
    const base = projName(r.cwd);
    const proj = displayNameOf(base);
    if ((inScope(proj) || inScope(base)) && (!ql || title.toLowerCase().includes(ql) || proj.toLowerCase().includes(ql) || base.toLowerCase().includes(ql)))
      rows.push({ kind: 'session', id: r.id, live: false, title, proj, t: Date.parse(r.updated || '') || 0 });
  }
  rows.sort((a, b) => b.t - a.t);

  // Projects a new session can start in, listed on a BARE open - the menu's
  // whole point is launching without a scavenger hunt: the current project
  // first (empty dir - Go fills it in), then the promoted ones in promotion
  // order, each with its icon. Not every cwd the store ever saw - a directory
  // becomes a project by promotion, and the picked-folder row below is how a
  // new one is born. Typing or #scoping narrows the same list; the cap keeps
  // a long promotion list from pushing the sessions off the menu.
  const acts = [];
  const projs = new Map(PROJECT ? [[PROJECT, '']] : []);
  for (const d of promoted) {
    if (!projs.has(projName(d))) projs.set(projName(d), d);
  }
  for (const [name, dir] of projs) {
    const disp = displayNameOf(name);
    if ((!scope || inScope(disp) || inScope(name)) && (!ql || disp.toLowerCase().includes(ql) || name.toLowerCase().includes(ql)))
      acts.push({ kind: 'new', name, dir, title: 'New session in ' + disp });
  }
  if (acts.length > 8) acts.length = 8;
  if (!scope && acts.length < 8 && (!ql || 'picked folder'.includes(ql)))
    acts.push({ kind: 'pick', title: 'New session in a picked folder\u2026' });

  const soon = [];
  for (const s of ['General', 'Providers', 'Models', 'Default effort', 'Appearance']) {
    if (ql && s.toLowerCase().includes(ql)) soon.push({ kind: 'soon', title: s });
  }
  return [
    { label: 'Actions', rows: acts },
    { label: 'Sessions', rows: rows.slice(0, 8) },
    { label: 'Settings', rows: soon },
  ].filter(s => s.rows.length);
}

function palDraw() {
  palRows = [];
  prows.replaceChildren();

  const push = r => {
    r.idx = palRows.length;
    palRows.push(r);
    prows.appendChild(palRowEl(r));
  };
  for (const s of palRowsFor(pq.value)) {
    const h = document.createElement('div');
    h.className = 'p-head';
    h.textContent = s.label;
    prows.appendChild(h);
    s.rows.forEach(push);
  }
  if (!palRows.length) {
    const e = document.createElement('div');
    e.className = 'p-empty';
    e.textContent = 'nothing matches';
    prows.appendChild(e);
  }

  if (palSel >= palRows.length) palSel = Math.max(0, palRows.length - 1);
  palMark();
}

function palRowEl(r) {
  const el = document.createElement('div');
  el.className = 'p-row' + (r.kind === 'soon' ? ' soon' : '');
  el.dataset.idx = r.idx;
  if (r.kind === 'session') {
    el.appendChild(span('dot' + (r.busy ? ' busy' : ' off'), ''));
    el.appendChild(span('p-title', r.title));
    el.appendChild(span('p-meta', r.proj + (r.t ? ' \u00b7 ' + ago(r.t) : '')));
  } else if (r.kind === 'soon') {
    el.appendChild(span('p-glyph', '\u2699'));
    el.appendChild(span('p-title', r.title));
    el.appendChild(span('p-soon', 'soon'));
  } else if (r.kind === 'pick') {
    el.appendChild(span('p-glyph', '+'));
    el.appendChild(span('p-title', r.title));
  } else {
    // A project action: the project's own icon when it has one, else the +.
    const icon = iconOf(r.name || '');
    if (icon) {
      const g = span('p-glyph', '');
      g.appendChild(iconNode(r.name));
      el.appendChild(g);
    } else el.appendChild(span('p-glyph', '+'));
    el.appendChild(span('p-title', r.title));
  }
  el.addEventListener('click', () => palGo(r));
  return el;
}

function palMark() {
  for (const el of prows.querySelectorAll('.p-row')) {
    const on = Number(el.dataset.idx) === palSel;
    el.classList.toggle('sel', on);
    if (on) el.scrollIntoView({ block: 'nearest' });
  }
}

function palGo(r) {
  palClose();
  if (r.kind === 'session') {
    if (r.live) switchTo(r.id);
    else if (!BUSY.has(sessions[active].status.State)) loadStore(r.id);
  } else if (r.kind === 'new') {
    expanded.add(r.dir ? projName(r.dir) : PROJECT);  // spawning into a group opens it
    go('goNewSession', r.dir).then(id => { if (id) switchTo(id); });
  } else if (r.kind === 'pick') {
    go('goPickFolder').then(dir => {
      if (!dir) return;
      // Picking a folder is how a project is born: promote it, then spawn.
      go('goCommand', 'project.add', { dir });
      expanded.add(projName(dir));
      go('goNewSession', dir).then(id => { if (id) switchTo(id); });
    });
  }
  // kind 'soon': the settings surface does not exist yet; the row only reads.
}

pq.addEventListener('input', () => { palSel = 0; palDraw(); });
pq.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palRows.length - 1, palSel + 1); palMark(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(0, palSel - 1); palMark(); }
  else if (e.key === 'Enter') { e.preventDefault(); if (palRows[palSel]) palGo(palRows[palSel]); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); palClose(); }
});
veil.addEventListener('mousedown', e => { if (e.target === veil) palClose(); });

// drawHeader fills the navbar: which session is on screen, and what it works
// on. Every title change goes through setTitle, so the name is filled there;
// the project arrives on a batch and is app-constant for now.
function drawHeader() {
  sessname.textContent = cur.title || 'New session';
  sessname.classList.toggle('empty', !cur.title);
  const proj = (cur && cur.project) || PROJECT;
  projectEl.hidden = !proj;
  projectEl.textContent = proj ? displayNameOf(proj) : '';
}

// addSession asks Go for a fresh worker, then shows it. Webview bindings
// answer with a promise rather than a value, so the id is awaited: a promise
// object itself is truthy and would switch to a session that does not exist.
async function addSession() {
  // Empty dir: the launch directory. The palette passes its own.
  const id = await go('goNewSession', '');
  if (!id) return;
  switchTo(id);
}

// closeSession closes a session on the Go side and drops its feed, then shows
// whatever is left (or starts a fresh one if the last went). Same promise
// rule: the next active id is the resolution of the close, never the close
// itself - switching to a promise would leave Go's workspace empty and the
// next sent message would walk into a nil session.
async function closeSession(id) {
  const next = await go('goClose', id);
  const s = sessions[id];
  if (s) { s.feed.remove(); delete sessions[id]; }
  closed.add(id);
  if (next) switchTo(next);
  else if (Object.keys(sessions).length) switchTo(Object.keys(sessions)[0]);
  else addSession();
}

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
// silence - the feed simply stops growing and nothing says why. Put it where the
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

  for (const b of batches) {
    // A worker being closed flushes once more on the way down; a batch for a
    // closed id would make sessionFor resurrect its feed and its sidebar row.
    if (b.session && closed.has(b.session)) continue;
    // Each batch belongs to one session; fold it there, then put the active
    // session back before drawing, so the bar always reads the one on screen.
    const s = sessionFor(b.session);
    const prev = cur;
    cur = s;
    s.status = b.status;
    // The row dot is a notification, not a state label: it lights when a run
    // finishes in a session nobody is looking at, and goes out when the
    // session is opened. A running session shows amber instead. The same edge
    // is the session's last activity - a finished run counts, streaming does
    // not, or the sidebar reshuffles every time the model breathes.
    const busy = BUSY.has(s.status.State);
    if (busy) {
      s.unseen = false;
    } else if (s.wasBusy) {
      s.unseen = s.id !== active;
      if (!s.replaying) s.lastAct = Date.now();  // a replay ending is not activity either
    }
    s.wasBusy = busy;
    // Each session carries its own project off its batches - spawned-in or
    // loaded-from, they can differ inside one window. PROJECT stays as the
    // launch project: the palette's "here", and the demo's paint. The header
    // is drawn once below, for the session on screen: per batch it was drawn
    // with cur pointed at a background session, and the navbar flickered to
    // whichever conversation was streaming in the background.
    if (b.project) {
      s.project = b.project;
      if (!PROJECT) { PROJECT = b.project; expanded.add(b.project); }  // the current cwd starts open
    }
    for (const ev of b.events) fold(ev, b.loading);
    cur = prev;
  }
  cur = sessions[active];
  // The close path deletes the active session and only then spawns its
  // replacement, so a batch can apply with nothing active: nothing to draw
  // into, and the sidebar still reads right.
  if (!cur) { drawSidebar(); return; }
  drawHeader();
  drawStatus();
  drawSidebar();
  tickIfBusy();
  if (cur.pinned) scrollToBottom();
}

// --------------------------------------------------------- block management

function push(kind, opts) {
  const b = Object.assign({ kind, text: '', acts: [], open: false, start: 0 }, opts);
  b.el = build(b);
  cur.blocks.push(b);
  cur.feed.appendChild(b.el);
  return b;
}

// tail returns the last block if it is of the given kind, so streamed chunks
// extend it instead of stacking up.
function tail(kind) {
  const b = cur.blocks[cur.blocks.length - 1];
  return b && b.kind === kind ? b : null;
}

// stream appends a chunk to the trailing block of that kind. The text node is
// extended in place - the browser reuses the existing layout for everything
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
  if (cur.sawOutput) closeCalls();
}

function openCalls() {
  if (!cur.callsBlock) {
    cur.callsBlock = push('calls', { open: true, start: performance.now() });
  }
  return cur.callsBlock;
}

function closeCalls() {
  if (cur.callsBlock) {
    const b = cur.callsBlock;
    b.open = false;
    b.end = performance.now();
    for (const a of b.acts) {
      if (a.state === 'pending') {
        settleRow(a, 'fail', 'no result', '');
        cur.callFail++;
      }
    }
    drawCallsHead(b);
    cur.callsBlock = null;
  }
  cur.sawOutput = false;
}

function notice(tone, text) {
  push('notice', { tone, text });
}

// APP_NAME is what the window is called before a conversation has a name of its
// own, and the first half of what it is called afterwards.
const APP_NAME = 'CuaCode';

// setTitle names the window after the work in it. Two places, because the page
// has two hosts: document.title is what a browser tab reads (--serve, ?demo),
// and goTitle is the native window, which does not follow document.title on its
// own. The binding is absent when the page is served without a worker, so it is
// asked for rather than assumed.
function setTitle(name) {
  name = sanitize(name || '').trim();
  cur.title = name;
  // A name belongs to its session's row always, and to the navbar and window
  // title only when that session is the one on screen: a background session
  // getting named mid-run must not steal the header from the conversation
  // being read. switchTo re-renders the header when the session is shown.
  drawSidebar();
  if (cur.id !== active) return;
  drawHeader();
  const full = name ? APP_NAME + ' - ' + clip(name, 60) : APP_NAME;
  document.title = full;
  if (typeof goTitle === 'function') goTitle(full);
}

// Prose is left as plain text while it streams and marked up once it is done:
// re-parsing a growing message on every chunk is quadratic in its length, which
// is exactly the shape of lag that gets worse the more the model says.
function settleProse() {
  for (const b of cur.blocks) {
    if (b.kind === 'prose' && !b.marked && b.text) {
      b.body.innerHTML = inlineMarkdown(b.text);
      b.marked = true;
    }
  }
}

function reset() {
  cur.blocks = [];
  cur.callsBlock = null;
  cur.sawOutput = false;
  cur.callCount = 0;
  cur.callFail = 0;
  cur.feed.replaceChildren();
  cur.status.ContextUsed = 0;
  cur.status.ContextLeft = 0;
  // The window title names the conversation, and the conversation just went.
  setTitle('');
  // Attached to a message in a conversation that is no longer on screen.
  cur.pending = [];
  drawTray();
}

// ------------------------------------------------------------- the folding

// fold turns one worker event into feed. The state names are the worker's own;
// deck switches on exactly the same set.
function fold(ev, loading) {
  if (ev.state === 'bad_line') {
    notice('err', 'unreadable worker line: ' + clip(sanitize(ev.raw || ''), 200));
    return;
  }

  // Replies are typed on the envelope ("sessions" carries no data.state),
  // statuses on data.state. Route by the state when there is one, the type
  // otherwise - deck's fold reads the same two fields (deck/main.go).
  switch (ev.state || ev.type) {
    case 'startup':
    case 'ready':
      // The worker's store id, on the line that opens the conversation. A
      // spawned session is a real store session from birth; without this tie
      // the tree and palette would list it twice once session.list runs -
      // once as the live row, once as the record it committed.
      if (ev.data && ev.data.session_id && !cur.storeId) cur.storeId = ev.data.session_id;
      notice('', 'welcome!');
      break;

    // A live message is echoed through this same path (see send), so it carries
    // its pictures; a reopened conversation is replayed without the payloads,
    // so there is nothing to draw but the names. See main.py's replay().
    case 'user':
      // A message sent into a run in flight is spoken into that round, so the
      // calls block stays open; one that starts a turn closes whatever is
      // left. It is the session's last activity - the sidebar's "just now" -
      // unless it is a replay: an archive's history is not news, and the row
      // keeps the place the store record gave it.
      if (!BUSY.has(cur.status.State)) { boundary(); closeCalls(); }
      if (!cur.replaying) cur.lastAct = Date.now();
      push('user', { text: ev.token || '', shots: (ev.images || []).map(i => ({ name: i.name, b64: i.b64 })) });
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
      cur.callCount += calls.length;
      drawCallsHead(b);
      break;
    }

    case 'tool_output':
      settle(ev.token || '', ev.data);
      cur.sawOutput = true;
      break;

    // The call did not finish, it moved. Worth its own line: the row for it is
    // about to settle with a job id where a result belongs, and that reads as a
    // strange answer with nothing to explain it.
    case 'background':
      notice('call', 'backgrounded · ' + ev.token + ' is still running');
      break;

    // Runtime text put into the conversation - neither the user's nor the
    // model's. Only the first paragraph is shown: the rest is instruction
    // addressed to the model, and on screen it would read as the agent talking
    // to itself.
    case 'notice': {
      const head = (ev.token || '').split('\n\n')[0];
      boundary();
      notice('call', sanitize(head));
      break;
    }

    // Mid-run readings. Nothing goes in the feed for either - the status bar
    // already moved - but they carry what the round's thinking is costing, and
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

    case 'error': {
      closeCalls();
      settleProse();
      // The message travels by whichever field the sender chose: token on a
      // run's error, err on a connection loss, data.error on a failed command
      // reply (project.rename, session.pin, ...). Dropping any of them is how
      // a bare "error: " with nothing behind it gets on screen.
      const msg = clip(sanitize(ev.token || ev.err || (ev.data && ev.data.error) || ''), 400);
      notice('err', 'error: ' + (msg || 'no detail'));
      // A turn the connection ended rather than the model. What streamed before
      // it went is still on screen and still in the history, so the next message
      // carries on from it instead of starting over - which is only obvious if
      // it is said.
      if (ev.data && ev.data.kept) notice('warn', 'partial reply kept \u00b7 say anything to carry on');
      finish();
      break;
    }

    // The request never landed and is going out again. Worth a row of its own:
    // a silent retry and a hung app look identical from this side of the screen.
    case 'retry': {
      const d = ev.data || {};
      notice('warn', 'connection lost · retry ' + (d.attempt || 0) + '/' + (d.of || 0) +
        ' in ' + (d.secs || 0) + 's');
      break;
    }

    // The store's archive, one reply to the session.list asked at startup -
    // and to every re-ask, which the palette and the pin command fire. Fills
    // the sidebar's project tree; the live half is the workspace's own
    // sessions and never comes from here.
    case 'sessions': {
      const d = ev.data || {};
      const fresh = d.sessions || [];
      // Same list, no event: most opens change nothing, and skipping keeps
      // the entrance stagger from replaying under a palette already open.
      if (JSON.stringify(fresh) !== JSON.stringify(storeList)) {
        storeList = fresh;
        // Session pins persist on the meta (a ts, oldest pin first), so the
        // store is the truth: a pin whose command failed drops here, and a
        // new window opens the way the last one was left.
        pins.clear();
        for (const m of fresh) if (m.pinned) pins.set(m.id, Date.parse(m.pinned) || 0);
        drawSidebar();
        if (!veil.hidden) palDraw();
      }
      break;
    }

    // The tree's project list, one reply to project.list / project.add /
    // project.remove / project.pin. A reply, so it must be reachable by type -
    // same wire shape as "sessions", no data.state. The pin order rides along:
    // project pins persist in projects.json, so a new window opens the way the
    // last one was left. Array order is pin age; pinSeq continues above it.
    case 'projects': {
      const d = ev.data || {};
      const fresh = d.projects || [];
      const freshPins = d.pinned || [];
      const freshNames = d.names || {};
      const freshIcons = d.icons || {};
      if (JSON.stringify(fresh) !== JSON.stringify(promoted) ||
          JSON.stringify(freshPins) !== JSON.stringify([...projPins.keys()]) ||
          JSON.stringify(freshNames) !== JSON.stringify(projNames) ||
          JSON.stringify(freshIcons) !== JSON.stringify(projIcons)) {
        promoted = fresh;
        projNames = freshNames;
        projIcons = freshIcons;
        projPins.clear();
        freshPins.forEach((n, i) => projPins.set(n, i));
        pinSeq = Math.max(pinSeq, freshPins.length);
        drawSidebar();
        if (cur) drawHeader();   // the navbar's project tag carries the display name too
        if (!veil.hidden) palDraw();
      }
      break;
    }

    // A session change replaces the conversation, so the feed goes with it:
    // what is on screen belongs to the session that was open.
    case 'session': {
      const d = ev.data || {};
      reset();
      if (!loading) { notice('', 'new session'); break; }
      // The sidebar folds this conversation's archive row away: it is live
      // now, and the live row carries the running dot.
      // The replay below is the conversation's past, not new activity: until
      // it ends, nothing it streams may move the row's recency.
      cur.replaying = true;
      cur.storeId = d.session_id || null;
      // The conversation belongs to the project it was spawned in, which can
      // differ from this worker's own directory: take it from the record.
      const rec = storeList.find(m => m.id === d.session_id);
      if (rec && rec.cwd) cur.project = projName(rec.cwd);
      // The conversation keeps the place its archive row held: lastAct seeds
      // from the record's updated, so reopening a week-old session does not
      // leap to the top of the tree and palette as if it had just moved.
      // Messages and finished runs in this window take over from here.
      if (rec && rec.updated) cur.lastAct = Date.parse(rec.updated) || cur.lastAct;
      let text = 'resumed session ' + (d.session_id || '');
      const n = num(d.msg_count);
      if (n > 0) text += ' · ' + n + (n === 1 ? ' message' : ' messages');
      push('resumed', { text });
      break;
    }

    // What this conversation is called, from whoever called it that: a stub off
    // the first message, the namer a turn or two later, the agent, or the
    // session that was just reopened. It is the window title, so all four
    // matter - see setTitle.
    case 'session_title': {
      const d = ev.data || {};
      setTitle(d.title || '');
      // Said once, quietly, and only for a name somebody chose. A stub is the
      // first few words the user just typed, and announcing it back to them
      // reads as the app repeating itself.
      if (d.title && (d.source === 'auto' || d.source === 'agent' || d.source === 'user')) {
        notice('', 'named · ' + sanitize(d.title));
      }
      break;
    }

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
  const n = num(cur.status.ThinkTokens), rate = num(cur.status.ThinkTPS);
  if (n <= 0 && rate <= 0) return;
  for (let i = cur.blocks.length - 1; i >= 0; i--) {
    const b = cur.blocks[i];
    if (b.kind === 'user') return;
    if (b.kind === 'think') {
      if (n > 0) { b.tokens = n; b.tokEst = !!cur.status.ThinkEst; }
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
  if (!r.ok) cur.callFail++;

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
  cur.callCount++;
  drawCallsHead(b);
}

function finish() {
  cur.runStart = 0;
  cur.replaying = false;  // done/cancelled/error all end a replay as well as a run
}

// ------------------------------------------------------------------ the DOM

// build makes the element for a block once. Everything that changes later -
// streamed text, a call's result, a thinking block's price - is written into a
// node this function has already put in place.
function build(b) {
  const el = document.createElement('div');
  el.className = 'b ' + b.kind;
  // Set before the switch: the head renderers below read b.el, and a block cannot be drawn before it exists.
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
  const busy = !!cur.callsBlock || BUSY.has(cur.status.State);
  if (busy && !ticker) {
    ticker = setInterval(() => {
      if (cur.callsBlock) drawCallsHead(cur.callsBlock);
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
  const st = cur.status.State || 'idle';
  const busy = BUSY.has(st);
  const frag = document.createDocumentFragment();

  frag.appendChild(span('state' + (busy ? ' busy' : st === 'error' ? ' err' : ''),
    STATE_WORD[st] || st));

  if (cur.callCount) {
    frag.appendChild(span('', cur.callCount + (cur.callCount === 1 ? ' call' : ' calls')));
    if (cur.callFail) frag.appendChild(span('fail', cur.callFail + ' failed'));
  }
  if (cur.runStart) frag.appendChild(span('', ((performance.now() - cur.runStart) / 1000).toFixed(1) + 's'));

  const tps = num(cur.status.TPS);
  if (tps > 0) frag.appendChild(span('', tps.toFixed(0) + ' t/s' + (cur.status.TPSEst ? '~' : '')));

  // Pushed to the right, and segmented rather than smooth: the only question it
  // answers is how much room is left, and a smooth bar reads as a download.
  const used = num(cur.status.ContextUsed), max = num(cur.status.ContextMax);
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
// Returns the binding's value, so a call like go('goNewSession') can hand back
// the new session id.
function go(name, ...args) {
  const fn = window[name];
  if (typeof fn === 'function') return fn(...args);
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
      notice('warn', f.name + ' is ' + fmtBytes(f.size) + ' - the limit is ' + fmtBytes(MAX_IMAGE));
      continue;
    }
    try {
      cur.pending.push({ name: f.name || 'clipboard.png', mime: f.type, size: f.size, b64: await b64of(f) });
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
  tray.hidden = cur.pending.length === 0;
  if (tray.hidden) { tray.replaceChildren(); return; }

  const frag = document.createDocumentFragment();
  cur.pending.forEach((a, i) => {
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
    x.addEventListener('click', () => { cur.pending.splice(i, 1); drawTray(); input.focus(); });
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
// it - and preventDefault on both events, because a webview's default answer to
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
// not - WKWebView hands over an empty file list for an image copied by
// anything but itself - and an empty event is indistinguishable from an
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
    if (cur.pending.some(a => a.b64 === img.b64)) return;
    cur.pending.push({ name: img.name, mime: img.mime || 'image/png', size: img.size, b64: img.b64 });
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

  // The palette outranks the app's keys: cmd+K opens it over anything, and
  // while it is up, cancel-on-Escape and tab-for-calls belong to it.
  if (ctrl && e.key.toLowerCase() === 'k') { e.preventDefault(); palToggle(); return; }
  if (!veil.hidden) {
    if (e.key === 'Escape') { palClose(); return; }
    if (e.key === 'Tab' || e.key === 'Enter' || e.key.startsWith('Arrow')) e.preventDefault();
    return;
  }

  // The details editor owns its keys the same way - its Enter and Esc live on
  // its inputs, and the focus-steal below would empty it one keystroke in.
  if (editingProj) return;

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
  if (!text && !cur.pending.length) return;
  const shots = cur.pending;
  cur.pending = [];
  drawTray();
  input.value = '';
  input.style.height = 'auto';

  if (!BUSY.has(cur.status.State)) {
    cur.callCount = 0;
    cur.callFail = 0;
    cur.runStart = performance.now();
  }
  // Echoed locally, not waited for (a round-trip echo would read as lag), and
  // through the same entry point as everything else, so the feed has one way
  // in. Stamped with its session: apply() routes an id-less batch to whatever
  // is active when the frame runs, and a switch in that gap moved the message
  // - and the sender's status snapshot with it - into another session's feed.
  window.__cua.push({ session: cur.id, events: [{ state: 'user', token: text, images: shots }], status: cur.status, loading: false });
  settleProse();
  scrollToBottom();
  // goSend when there is nothing attached, so the common message crosses the
  // binding it always did and a page talking to an older build still works.
  if (shots.length) go('goSendWith', text, shots.map(a => ({ name: a.name, b64: a.b64 })));
  else go('goSend', text);
}

function toggleThink() {
  showThink = !showThink;
  for (const b of cur.blocks) if (b.kind === 'think') b.el.classList.toggle('folded', !showThink);
}

function toggleCalls() {
  foldCalls = !foldCalls;
  for (const b of cur.blocks) if (b.kind === 'calls') b.el.classList.toggle('folded', foldCalls);
}

// --------------------------------------------------------------- scrolling

function scrollToBottom() {
  cur.feed.scrollTop = cur.feed.scrollHeight;
  cur.pinned = true;
}

// ------------------------------------------------------ decoding tool calls

// parseCalls covers all three provider dialects, which cross the wire verbatim:
//
//   ollama     {"function": {"name": ..., "arguments": {...}}}
//   openai     {"id": ..., "function": {"name": ..., "arguments": "<json>"}}
//   anthropic  {"type": "tool_use", "id": ..., "name": ..., "input": {...}}
//
// Anything unrecognised is kept visible rather than dropped - a silent empty
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
// call succeeded - a dispatch failure comes back as {"error": ...} in place of
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

    // The worker keeps the page - or the skill's instructions - off the wire
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
    // never arrives as {"error": ...} - read it off the exit code.
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

// The first session exists before any worker event, so the input has a home and
// the held-back startup line has a feed to land in.
// The binding answers one question about the host: when it exists, the window
// has handed its bar to the page and the native lights sit on the sidebar's
// first row, so the brand row steps over them.
if (typeof goTitle === 'function') document.body.classList.add('native');
sessionFor('default');
switchTo('default');
reset();
input.focus();

// Said last: everything above has to exist before the worker's held-back
// startup line is evaluated into the page.
go('goReady');

// ---------------------------------------------------------------- the demo

// ?demo replays a scripted conversation through the same entry point the worker
// uses, so the page can be looked at - in a browser, by a person or by anything
// driving one - without Python, a window, or a mock of the UI standing in for
// the UI. What it draws here is what it draws in the app.
//
// It exists because a GUI is the one part of this program that cannot report on
// itself. A terminal frontend can be diffed against its own output; this one can
// only be seen, so it has to be servable somewhere something can see it.
//
// ?demo=name picks a scenario from window.__FIXTURES (default, long, resumed,
// cancelled). ?demo=folded runs the default with thinking unfolded and calls
// folded. ?demo&fast collapses every wait and applies each batch on the spot, so
// the conversation is complete before the load event and a screenshot of the page
// is the same picture every time. ?demo&stop halts at the first batch marked
// stop, leaving a batch of calls open - the one live state a finished replay
// cannot show.
function demoName() {
  let name = new URLSearchParams(location.search).get('demo') || 'default';
  return name === 'folded' ? 'default' : name;
}

function demoBatches() {
  const script = (window.__FIXTURES && window.__FIXTURES[demoName()]) || window.__FIXTURE;
  if (!script) return [];
  const out = [];
  for (const b of script.batches) {
    // Text is replayed in pieces rather than whole: streaming is where the feed
    // does its real work, and a fixture handing over finished messages would
    // exercise none of it.
    const events = [];
    for (const ev of (b.events || [])) {
      if (ev.state !== 'thinking' && ev.state !== 'content') { events.push(ev); continue; }
      for (const chunk of chunks(ev.token, 24)) events.push({ state: ev.state, token: chunk });
    }
    out.push({ delay: b.delay || 0, status: b.status || {}, events, stop: !!b.stop, loading: !!b.loading, session: b.session, switch: b.switch });
  }
  return out;
}

function demoFast() {
  const stop = location.search.includes('stop');
  for (const b of demoBatches()) {
    window.__cua.push({ events: b.events, status: b.status, loading: b.loading, session: b.session });
    flushNow();
    if (b.switch) switchTo(b.switch);
    if (stop && b.stop) { document.body.dataset.demo = 'stopped'; return; }
  }
  document.body.dataset.demo = 'done';
}

async function demoTimed() {
  const stop = location.search.includes('stop');
  for (const b of demoBatches()) {
    await new Promise(r => setTimeout(r, b.delay));
    for (const ev of b.events) {
      window.__cua.push({ events: [ev], status: b.status, loading: b.loading, session: b.session });
      if (ev.state === 'thinking' || ev.state === 'content') {
        await new Promise(r => setTimeout(r, 18));
      }
    }
    if (b.switch) switchTo(b.switch);
    if (stop && b.stop) { document.body.dataset.demo = 'stopped'; return; }
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
// of chips - painted here rather than pasted in as base64, which would put a
// picture of a picture in the source.
function demoTray() {
  cur.pending = [
    { name: 'failing-tests.png', mime: 'image/png', size: 184320, b64: swatch('#92b8e0', '#3d6fa9') },
    { name: 'screenshot 2026-08-23 at 14.02.11.png', mime: 'image/png', size: 962560, b64: swatch('#e0a35e', '#ff5c62') },
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

// demoStore paints fake archive rows so ?demo shows the tree with something in
// it. Real data would make screenshots depend on this machine's store.
function demoStore() {
  const h = 3600e3, d = 24 * h;
  const mk = (id, title, back, cwd) => ({
    id, title, cwd,
    updated: new Date(Date.now() - back).toISOString(),
  });
  const core = '/Users/boaz/Work/CuaCode-core';
  // The tree's groups render only for promoted dirs (or the current project),
  // so the demo paints its whitelist too - same reason the store list is fake.
  promoted = [core, '/Users/boaz/Work/northwind', '/Users/boaz/Work/contoso'];
  // One emoji, one gradient, and the custom name, so the shots show the
  // details editor's output. Same reason the store list is fake.
  projNames = { [core]: 'CuaCode Core' };
  projIcons = { '/Users/boaz/Work/northwind': '\ud83d\ude80', '/Users/boaz/Work/contoso': 'grad:2' };
  return [
    mk('d1', 'Fix flaky test', 1 * d + 2 * h, core),
    mk('d2', 'Refactor auth flow', 1 * d, core),
    mk('d3', 'Review open PR', 5 * d, core),
    mk('d4', 'Add csv export', 5 * d + 2 * h, core),
    mk('d5', 'Fix broken build', 5 * d + 5 * h, core),
    mk('d6', 'Trim bundle size', 6 * d, core),
    mk('d7', 'Rename config keys', 7 * d, core),
    mk('d8', 'Add retry logic', 8 * d, core),
    mk('d9', 'Update onboarding docs', 9 * d, core),
    mk('d10', 'Fix pagination bug', 1 * d, '/Users/boaz/Work/northwind'),
    mk('d11', 'Add search bar', 2 * d, '/Users/boaz/Work/northwind'),
    mk('d12', 'Import customer list', 3 * h, '/Users/boaz/Work/contoso'),
    mk('d13', 'Quarterly report draft', 23 * d, '/Users/boaz/Work/contoso'),
  ];
}

if (location.search.includes('demo')) {
  // The project is not part of a conversation, so no fixture can carry it - the
  // same reason demoTray paints its own chips. Painted here for every scenario.
  PROJECT = 'CuaCode';
  storeList = demoStore();
  // The demo photographs rows, not a closed tree: its groups start open.
  // ?demo&closed skips that, for looking at the collapsed tree itself.
  if (!location.search.includes('closed'))
    for (const n of ['CuaCode', 'CuaCode-core', 'northwind', 'contoso']) expanded.add(n);
  drawHeader();
  drawSidebar();
  if (location.search.includes('palette')) palOpen();
  if (location.search.includes('openproj')) revealProject('/Users/boaz/Work/sidequest');
  // The rail (also forced below 800px) and persisted session pins, each their
  // own hook so the older shots stay stable.
  if (location.search.includes('rail')) { sideRail = true; drawSidebar(); }
  if (location.search.includes('pinsess')) {
    pins.set('default', Date.now() - 60000);   // the live conversation
    pins.set('d1', Date.now() - 30000);        // an archive row
    drawSidebar();
  }
  if (location.search.includes('pinproj')) {
    projPins.set('northwind', ++pinSeq);
    projPins.set('sidequest', ++pinSeq);
    drawSidebar();
  }
  const name = new URLSearchParams(location.search).get('demo') || 'default';
  if (name === 'folded') { showThink = true; foldCalls = true; }
  if (name === 'default') demoTray();
  if (location.search.includes('fast')) demoFast();
  else demoTimed();
} else {
  // The store's archive and the project list, for the sidebar. Asked once
  // here; the places that change what they show refresh them. In ?demo both
  // are painted fake above, and there are no bindings to ask anyway.
  go('goCommand', 'session.list', null);
  go('goCommand', 'project.list', null);
}
