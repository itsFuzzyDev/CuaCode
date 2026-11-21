//! quik: CuaCode's terminal frontend, built to the Figma page 2 design.
//!
//! One screen: three lines of header, the conversation, and the composer -- one
//! card with the message in it -- under a row of counts. The conversation is the same
//! `cuacode::session` and `cuacode::transcript` the app reads, so there is one
//! model of what a conversation is and two drawings of it.
//!
//!   quik                       talk to the worker
//!   quik <message>             open the conversation with it, sent the
//!                                moment the worker is up
//!   quik prompt.txt            the same, with the message read from a file:
//!                                one argument that names one is its contents
//!   quik --resume [id]         carry on with an earlier conversation: ask
//!                                which one, or take the one named. Escape at
//!                                that question starts a new conversation
//!                                instead (--continue and -r are the same flag)
//!   quik --fixture f.jsonl     replay a recorded stream, no worker at all
//!   quik --print --fixture f   fold a stream and print it, no terminal
//!   quik --version             the build id alone: <commits>.<short sha>
//!
//! The header carries that id, and one line under it in yellow when the remote
//! has commits this tree does not. `cuacode doctor` is where the whole answer
//! lives, including the upgrade.
//!
//! The terminal's own background is set to the design's page, so the screen is
//! the design's colour rather than whatever the profile happens to use.

mod keys;
mod menu;
mod scan;
mod view;
#[cfg(windows)]
mod winpaste;

use std::collections::HashMap;
use std::io::{self, Read};
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

use crossterm::event::{
    self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    Event as CEvent, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::style::{Color as TermColor, Print, SetBackgroundColor};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::{CrosstermBackend, TestBackend};
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Clear, Paragraph};
use ratatui::{Frame, Terminal};

use cuacode::attach;
use cuacode::hosts::{self, Host};
use cuacode::session::{self, Session, Snapshot};
use cuacode::transcript::{Row, Transcript};
use cuacode::version;
use cuacode::wire::{self, Event, State};
use cuacode::worker::Target;

use keys::Action;
use menu::{Item, Kind, Menu};
use view::{ink, Drawn};

/// How long two clicks on the same row must be apart to be a fold rather than
/// two clicks.
const DOUBLE_CLICK: Duration = Duration::from_millis(400);

/// An agent has stopped to ask. `fleet` runs this frontend in a pty and draws its
/// screen, but a permission question is raised and answered *inside* here, so
/// without this the control plane never learns the one thing it exists to show:
/// that a pane is blocked on a person.
///
/// OSC 9 is "set the window title" to a terminal that has never heard of us, so a
/// terminal that does not know the sequence does nothing with it, rather than
/// printing a stray string over the screen. It is written to the tty between
/// frames and never through ratatui, so it cannot land in a cell.
///
/// The bytes are a contract with `fleet/src/term.rs`, which reads them back off
/// the emulator's stream: they are written here once, and
/// `the_permission_mark_is_the_bytes_fleet_reads` is what holds both sides to
/// them.
fn needs_you() -> &'static str {
    "\u{1b}]9;cua-code-needs-you\u{7}"
}

/// The question has been answered, whichever way: allowed or refused. It goes
/// out on every exit from the prompt, so a pane that is asked twice ends its mark
/// the same way both times.
fn answered() -> &'static str {
    "\u{1b}]9;cua-code-needs-you-answer\u{7}"
}

/// The box's own line: two cells, a light vertical rule and the cell the text
/// starts in. The same weight the box's two rules are drawn in.
const RAIL: &str = "\u{2502} ";

/// The tallest the box grows. A message of twenty lines is a document, and the
/// conversation behind it is what the screen is for; past this the box scrolls
/// to the caret instead of taking the pane.
const FIELD_MAX: usize = 8;

/// The width the composer opens at on a screen with nothing said in it yet: opencode's
/// own 75, wide enough for a sentence and narrow enough to read as a place to type.
const HOME_WIDTH: usize = 75;
/// The cells a row of the message owes before its text: the prompt glyph and the
/// space after it. The wrap, the caret and a click all count from the same two.
const LEAD: usize = 2;

/// What the launch asked for before the first frame.
#[derive(Clone, PartialEq, Eq, Debug)]
enum Start {
    /// A new conversation, straight away. What no flag at all means.
    Fresh,
    /// Come up and ask which conversation to carry on with.
    Ask,
    /// Carry on with the one that was named.
    This(String),
}

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    // The build stamp, and nothing else: a flag that answers instantly is worth
    // more in a bug report than one that waits on a network to say the same
    // thing the header would. `cuacode doctor` is where the update is asked for.
    if args.iter().any(|a| a == "--version" || a == "-V") {
        println!("quik {}", version::ID);
        return Ok(());
    }
    let fixture = flag(&args, "--fixture");
    let home = flag(&args, "--home").map(PathBuf::from);
    let start = start_of(&args);
    let opening = opening_message(&args);

    // A bare --resume asks which conversation to open, and a message has nowhere
    // to be sent until that question is answered. Refused rather than guessed at:
    // the alternatives are a message quietly running in a conversation nobody
    // chose, or one silently not sent at all.
    if start == Start::Ask && opening.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "--resume with no id asks which conversation, and a message has nowhere to go until it is \
             answered; name the id (--resume <id>) or drop the message",
        ));
    }

    if args.iter().any(|a| a == "--keys") {
        return probe_keys();
    }

    if args.iter().any(|a| a == "--print") {
        let path = fixture.ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "--print needs --fixture <path>")
        })?;
        // Folded is what the screen opens as; `--open` prints it the way ctrl-t
        // leaves it.
        return print_fixture(&path, args.iter().any(|a| a == "--open"));
    }

    // One whole frame, drawn into a buffer and written out: the screen as text,
    // with the header, the card, the field and the colours' shapes. The only way
    // to see the layout without a terminal, and the way a design is checked.
    if args.iter().any(|a| a == "--frame") {
        let path = fixture.ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "--frame needs --fixture <path>")
        })?;
        let (w, h) = frame_size(&args);
        return print_frame(
            &path,
            w,
            h,
            args.iter().any(|a| a == "--open"),
            args.iter().any(|a| a == "--colour"),
        );
    }

    let mut app = match fixture {
        Some(path) => App::replaying(&path),
        None => App::live(home.as_deref(), start, opening)?,
    };
    // The keymap, read here and never watched: a file edited while the screen is
    // open takes the next launch. A complaint about it is the transcript's notice
    // row, one line each, with the defaults standing underneath: a broken file
    // never stops the screen.
    let (bounds, complaints) = keys::load(&cuacode::state_root().join(keys::FILE));
    app.keys = bounds;
    for complaint in complaints {
        app.say(complaint);
    }
    run(app)
}

/// Print what this terminal sends, byte by byte, until ctrl-c.
///
/// The one thing a key that does not work needs is the bytes the terminal
/// actually sent, and they cannot be guessed: shift-enter is its own sequence in
/// the kitty protocol, a different one in xterm's modifyOtherKeys, and plain
/// enter's single byte in a terminal that has neither. One that has the key
/// mapped to a sequence sends whatever the mapping says, which is how VS Code
/// comes to send `1b 0d` (escape, carriage return) for shift-enter.
fn probe_keys() -> io::Result<()> {
    // What the terminal claims, printed first because the wait for an answer is
    // where a keypress would otherwise be eaten: the query reads the terminal
    // itself and drops what it does not recognise. It is a different question
    // from the one this tool answers, and a terminal that claims yes can still
    // send something else for shift-enter. The bytes below are the truth.
    let claims = crossterm::terminal::supports_keyboard_enhancement().unwrap_or(false);
    println!("claims keyboard enhancement: {claims}");
    println!("press keys, ctrl-c to stop\n");
    enable_raw_mode()?;
    let result = loop {
        let mut buf = [0u8; 64];
        match io::stdin().read(&mut buf) {
            Ok(0) => break Ok(()),
            Ok(n) => {
                let got = &buf[..n];
                if got.contains(&3) {
                    break Ok(());
                }
                let hex: Vec<String> = got.iter().map(|b| format!("{b:02x}")).collect();
                let shown: String = got
                    .iter()
                    .map(|b| match b {
                        27 => "\\e".to_string(),
                        32..=126 => (*b as char).to_string(),
                        _ => ".".to_string(),
                    })
                    .collect();
                println!("{:<32} {shown}", hex.join(" "));
            }
            Err(e) => break Err(e),
        }
    };
    disable_raw_mode()?;
    result
}

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

/// Every flag whose value is the word after it, so the message walk can step
/// over that word rather than reading it as words to send. Bare switches are
/// not here: they take nothing, and skipping a word that follows one would eat
/// the first word of the message.
///
/// `--resume` and its aliases are values because when one follows it is the id,
/// and a launch that resumes must not also send the id as a message. A bare
/// `-r` with a message right after it is the one combination this reads wrong,
/// and it reads wrong in the flag parsing itself.
const WITH_VALUE: &[&str] = &["--fixture", "--home", "--resume", "-r", "--continue", "-c"];

/// The message to open the conversation with, from the launch itself, so a
/// session can be handed something to do without a person typing it.
///
/// The first argument that is not a flag starts it, and the words after it are
/// part of it: the message is a sentence, not a word. When it is one word and
/// that word names a file, it is the file's contents -- which is what makes
/// `quik prompt.txt` mean what it reads like. A word that names no file is a
/// word, and nothing says so: `quik why is this failing` is a sentence.
fn opening_message(args: &[String]) -> Option<String> {
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        if arg == "--" {
            i += 1;
            break;
        }
        if arg.starts_with('-') {
            i += if WITH_VALUE.contains(&arg) { 2 } else { 1 };
            continue;
        }
        break;
    }
    let rest = &args[i.min(args.len())..];
    if rest.len() == 1 {
        if let Ok(text) = std::fs::read_to_string(&rest[0]) {
            return nonempty(text);
        }
    }
    nonempty(rest.join(" "))
}

/// A message that carries nothing is not one: a blank prompt.txt would send a
/// turn the model cannot answer.
fn nonempty(text: String) -> Option<String> {
    (!text.trim().is_empty()).then_some(text)
}

/// Read the command line for the one flag that changes what the launch does.
///
/// A bare `--resume` is "show me the list"; an id after it, or `--resume=<id>`,
/// is that conversation. `--continue` and `-c` are the same question under the
/// name people reach for first. Both frontends read it the same way, because
/// they are one program as far as the person typing at them is concerned.
fn start_of(args: &[String]) -> Start {
    let (mut resume, mut id) = (false, None);
    for (i, arg) in args.iter().enumerate() {
        if matches!(arg.as_str(), "--resume" | "-r" | "--continue" | "-c") {
            resume = true;
            // A following flag is not an id.
            if let Some(next) = args.get(i + 1) {
                if !next.starts_with('-') {
                    id = Some(next.clone());
                }
            }
        } else if let Some(rest) = arg.strip_prefix("--resume=") {
            resume = true;
            id = Some(rest.to_string());
        }
    }
    match (resume, id) {
        (false, _) => Start::Fresh,
        (true, Some(id)) => Start::This(id),
        (true, None) => Start::Ask,
    }
}

/// Fold a recorded stream and print it as text. The one way to see what the
/// screen would draw with no terminal, no worker, no model and no network.
fn print_fixture(path: &str, open: bool) -> io::Result<()> {
    let raw = std::fs::read_to_string(path)?;
    let mut log = Transcript::default();
    let mut drawn = 0;
    for line in raw.lines().filter(|l| !l.trim().is_empty()) {
        if let Ok(ev) = wire::parse(line) {
            if log.apply(&ev) {
                drawn += 1;
            }
        }
    }
    // The same pieces the screen wraps, unwrapped for a pipe. Folded by default
    // because that is what the screen opens as: a preview of the screen, not a
    // dump of the conversation. `--open` opens the steps; the thoughts inside them
    // stay lines, which is what the screen shows too.
    let expanded = |_: usize, _: &Row| open;
    let folded = |_: usize| false;
    for piece in view::pieces(&log.rows, &expanded, &folded, &|_| None, None, true, 100, None) {
        println!("{}{}", piece.lead, piece.text());
    }
    println!("\n{} rows from {drawn} events", log.rows.len());
    Ok(())
}

/// The size a `--frame` draws at: `--frame 120x40`, or 100 by 30.
fn frame_size(args: &[String]) -> (u16, u16) {
    let given = args
        .iter()
        .position(|a| a == "--frame")
        .and_then(|at| args.get(at + 1))
        .and_then(|s| s.split_once('x'))
        .and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)));
    given.unwrap_or((100, 30))
}

/// One frame of the conversation, drawn into a test buffer and printed.
///
/// No terminal, no raw mode, no worker: the same `draw` the screen runs, onto a
/// grid that is then written out a line at a time. Styles are not in the text --
/// what this shows is the layout and the characters -- so a colour is checked in a
/// real terminal and a shape is checked here.
fn print_frame(path: &str, w: u16, h: u16, open: bool, colour: bool) -> io::Result<()> {
    let mut app = App::new(None);
    if let Ok(raw) = std::fs::read_to_string(path) {
        for line in raw.lines().filter(|l| !l.trim().is_empty()) {
            if let Ok(ev) = wire::parse(line) {
                app.log.apply(&ev);
            }
        }
    }
    // The frame is of the end of the conversation, which is what the screen
    // follows to when it opens, and a replay has no worker to say anything.
    app.follow = true;
    app.open = open;
    let mut term = Terminal::new(TestBackend::new(w, h)).map_err(io::Error::other)?;
    term.draw(|f| draw(f, &mut app)).map_err(io::Error::other)?;
    if colour {
        print_colours(term.backend().buffer());
    } else {
        print_frame_text(term.backend().buffer());
    }
    Ok(())
}

/// The frame as characters, the way the test backend writes it, with the link
/// sequence a cell carries taken out: an escape in the middle of a row is not
/// layout, and this printing is how a layout is read.
fn print_frame_text(buffer: &Buffer) {
    for cells in buffer.content.chunks(buffer.area.width as usize) {
        let mut row = String::from("\"");
        let mut skip = 0u16;
        for cell in cells {
            let glyph = view::without_link(cell.symbol());
            if skip == 0 {
                row.push_str(&glyph);
            }
            skip = skip.max(view::columns(&glyph) as u16).saturating_sub(1);
        }
        row.push('"');
        println!("{row}");
    }
}

/// The same frame, one letter a cell, the letter standing for the colour that
/// cell is painted in, with the legend under it.
///
/// A test buffer carries no style in its text, so a colour can only be checked
/// off the cells themselves. `#` marks a bold cell, `*` a reversed one.
fn print_colours(buffer: &Buffer) {
    let area = buffer.area;
    let mut keys: Vec<(Color, Color, Modifier)> = Vec::new();
    let mut grid: Vec<Vec<usize>> = Vec::new();
    for y in area.top()..area.bottom() {
        let mut row = Vec::new();
        for x in area.left()..area.right() {
            let (fg, bg, modifier) = match buffer.cell((x, y)) {
                Some(cell) => (cell.fg, cell.bg, cell.modifier),
                None => (Color::Reset, Color::Reset, Modifier::empty()),
            };
            let style = (fg, bg, modifier & (Modifier::BOLD | Modifier::REVERSED));
            let at = match keys.iter().position(|k| *k == style) {
                Some(at) => at,
                None => {
                    keys.push(style);
                    keys.len() - 1
                }
            };
            row.push(at);
        }
        grid.push(row);
    }
    let letter = |i: usize| (b'a' + (i % 26) as u8) as char;
    for row in &grid {
        println!("{}", row.iter().map(|i| letter(*i)).collect::<String>());
    }
    println!();
    for (i, (fg, bg, m)) in keys.iter().enumerate() {
        let mark = if m.contains(Modifier::BOLD) {
            "#"
        } else if m.contains(Modifier::REVERSED) {
            "*"
        } else {
            " "
        };
        let show = |c: &Color| match c {
            Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
            other => format!("{other:?}"),
        };
        println!("{} {mark} fg {}  bg {}", letter(i), show(fg), show(bg));
    }
}

/// What this screen measured about the round in flight, or the last one.
///
/// The wire carries no clock: no event says when a round started, how long it
/// took, or when it ended. This is the frontend's own stopwatch, started the
/// first time the worker says it is running and stopped when it stops being
/// that. It is a measurement rather than something the worker reported, which is
/// why the readout is in seconds and not dressed up as a figure the worker sent.
pub struct Turn {
    started: Instant,
    ended: Option<Instant>,
    pub tokens: Option<i64>,
    pub estimated: bool,
}

impl Turn {
    fn starting() -> Turn {
        Turn { started: Instant::now(), ended: None, tokens: None, estimated: false }
    }

    pub fn secs(&self) -> f64 {
        self.ended.unwrap_or_else(Instant::now).duration_since(self.started).as_secs_f64()
    }
}

/// A question the worker is blocked on.
///
/// One shape for both kinds: a permission, and a question the model asked with
/// `ask_user`. They arrive on the same envelope, so they are held the same way,
/// and the difference is whether `options` carries anything.
#[derive(Clone)]
struct Ask {
    /// The id to answer back under.
    id: String,
    /// "question" or "permission", as the worker said. Never inferred here.
    kind: String,
    /// What the worker says it wants: `ask_user` for a question, a tool's name
    /// for a permission.
    name: String,
    /// What is being asked. For a permission this is the model's own sentence
    /// about why, and empty when there is none.
    question: String,
    /// Context the answer needs: the two diffs, the file list, the error.
    detail: String,
    /// The answers to pick between. Empty means the binary prompt: y or n.
    options: Vec<String>,
    /// Which option the arrows are on. `options.len()` means the free-text row.
    picked: usize,
    /// What the person is typing instead of picking. Non-empty means the caret is
    /// here and the arrows do nothing: typing an answer is the point of not
    /// wanting any of the options.
    typed: String,
    /// Which question this is, and how many there are: "2 of 3". Zero when the
    /// worker did not say, which is the case for a permission.
    n: usize,
    of: usize,
    /// How far down the document is scrolled: the call, the reason, the change. The
    /// rows that answer are never scrolled, so this only ever moves what is behind
    /// them. Clamped where the document is folded into the panel.
    scroll: usize,
    /// A permission's document: why the gate stopped the call, the call itself as
    /// the row names it, what it would do, and the change it would make. Empty for a
    /// question, which has its question and its detail instead.
    body: Vec<Line<'static>>,
}

/// How long the keyboard has to be still before a held prompt is put up.
///
/// A permission arrives in the middle of whatever the person was doing, and taking
/// the keys out of their hands in the middle of a word is what makes a prompt feel
/// thrown at them. The worker is blocked on the answer either way, so the wait costs
/// nothing but the wait.
const ASK_AFTER: Duration = Duration::from_millis(3500);

impl Ask {
    /// Whether this is a question the model asked rather than a permission.
    ///
    /// From the envelope's own `kind`, which the worker sets. Nothing is inferred:
    /// keying this off the tool name was wrong, because a permission can be raised
    /// for a tool called anything, and keying it off "are there options" was worse,
    /// because the question with no options is exactly the one that needs a text
    /// box. An envelope from an older worker carries no kind, and the fallback is
    /// the question mark being present, which only a question sets.
    fn is_question(&self) -> bool { self.kind == "question" }

    /// The row under the cursor, counting the free-text row after the options.
    fn rows(&self) -> usize { self.options.len() + 1 }

    fn on_custom(&self) -> bool { self.picked >= self.options.len() }

    /// What the panel is called. A person asked a question is looking at a
    /// question, not at a tool: naming the box `ask_user` put our word for the
    /// mechanism where their word for what is happening belongs. The count is
    /// there because "am I nearly done" is the first thing anyone wants to know.
    fn title(&self) -> String {
        if !self.is_question() { return format!(" allow {}? ", self.name); }
        if self.of > 1 { format!(" question {} of {} ", self.n, self.of) }
        else { " question ".to_string() }
    }

    /// Whether there is a list to move around in. With no options there is only
    /// the text row, and up/down have nothing to do -- so they must not eat the
    /// keys a text field wants.
    fn has_choices(&self) -> bool { !self.options.is_empty() }
}

/// A call opened in full.
///
/// The row says what a call cost and one line of what came back; this is the
/// document the worker keeps of it, on the records rather than on the wire, which
/// is why opening one is a round trip.
struct Opened {
    title: String,
    lines: Vec<Line<'static>>,
    scroll: u16,
}

/// Everything the screen holds. `sess` is `None` when replaying a file: there is
/// no worker to talk to, and the transcript is the whole story.
struct App {
    sess: Option<Session>,
    log: Transcript,
    input: String,
    /// The pictures attached to this message. Their bytes live here and nowhere
    /// else -- the message holds only the characters a chip covers -- so removing
    /// the chip is what removes the picture.
    shots: Vec<Shot>,
    /// The field's own mouse position, for a chip that is lit because the pointer
    /// is on it. Only the field tracks this: a row of the transcript has nothing
    /// to light.
    hover: Option<(u16, u16)>,
    /// What each drawn row of the field is, put there by the last frame: the row
    /// on screen, the first column of its window, and which chip each column
    /// belongs to. A click needs exactly what the draw used, so it is kept rather
    /// than recomputed -- two derivations of where `[image 1]` landed is one
    /// chance to be wrong about which picture a click takes off.
    field_rows: Vec<(u16, usize, Vec<u8>)>,
    /// Where the field's first row is on screen, put there by the last frame with
    /// `field_rows` and read by the same click.
    field_top: u16,
    /// Where the field's first column is on screen, put there by the same frame. It is
    /// zero when the frame was too small for the card and the message was drawn straight
    /// onto the pane, and the card's edge when it was not: a click counts the prompt
    /// and the text from the column the frame actually drew them at.
    field_left: u16,
    /// Where the caret sits, in characters from the start of the message. The
    /// field is a small editor rather than a line: a message can be several
    /// lines long and a typo in the middle of one has to be reachable.
    caret: usize,
    /// The field as it stood before the deletion going on, for ctrl-y to come back
    /// to. One entry per run of deletions: a key held down and an escape are the
    /// same accident, and either wants one press to undo, not one per character.
    undo: Vec<Held>,
    /// Whether the last key that touched the field was one of those deletions, so
    /// the next one joins the run instead of stacking a snapshot per character.
    cutting: bool,
    /// Whether a step shows its calls and the lines of its thinking. Off, so a
    /// screen opened on a long session is heads rather than a wall of reasoning.
    /// ctrl-t flips them all at once, and a double click beats it for one step.
    open: bool,
    /// The rows the reader unfolded or folded by hand, by index.
    overrides: HashMap<usize, bool>,
    /// Whether a thought inside an opened step shows its text, by row. Off unless
    /// the reader opened it: a step opened on ctrl-t is its calls, and each thought
    /// in it is one line saying what it was about, so the calls are what the eye
    /// finds when it scrolls. Opening one is the same press the step itself takes.
    thoughts: HashMap<usize, bool>,
    /// Rows of the transcript the view is scrolled past, and whether the newest
    /// row is what it is showing. Scrolling up leaves the end behind.
    scroll: u16,
    follow: bool,
    /// What the screen lines of each piece are, and where the transcript starts,
    /// which is what a click has to be turned into a row. `anchors` is the other
    /// half of that: the row a piece was drawn from, and the row its step folds
    /// against, in the same order as `spans`.
    spans: Vec<(usize, usize)>,
    anchors: Vec<(usize, usize)>,
    body_top: u16,
    /// How wide the conversation pane is, put there by the last frame. A panel is
    /// built by a keystroke rather than by a draw, and a code block inside one has
    /// to be sized before it is drawn, so the width it will land in is kept.
    body_width: u16,
    /// The round in flight, or the last one this screen sent.
    turn: Option<Turn>,
    /// What the worker reported for a reasoning row's round, by row. Filed per row
    /// rather than kept once, because the row is the record: the round moves on to
    /// a tool call or an answer and the row keeps saying what its thinking cost.
    figures: HashMap<usize, view::Round>,
    /// A call the reader has opened, drawn over the transcript. The keyboard is
    /// its while it is up, and escape is what closes it.
    opened: Option<Opened>,
    /// A call the worker is blocked on, waiting for a yes or a no: the id to
    /// answer under, and what it wants to run.
    prompt: Option<Ask>,
    /// A prompt held until the keyboard has been still for `ASK_AFTER`. The worker is
    /// blocked on it either way; the difference is whether it lands in the middle of
    /// a sentence.
    pending: Option<Ask>,
    /// When a key was last pressed: the clock a held prompt waits on.
    since_key: Instant,
    /// The permission mark to write to the tty before the next frame, set where a
    /// question is raised and where it is answered. The loop around the draw
    /// takes it and writes it, rather than this writing to the terminal itself:
    /// the escape goes between frames and not into one, and an App with no
    /// terminal -- a `--print`, a test -- never writes to a tty at all.
    mark: Option<&'static str>,
    /// The `/`, `$` or `@` menu over the field, filtered by whatever the
    /// trigger has been followed by.
    menu: Option<Menu>,
    /// The `/` and `$` rows as the worker gave them. Held because neither list
    /// changes while a session is open, and a menu that had to wait a round
    /// trip would come up empty every time it was opened.
    commands: Vec<Item>,
    skills: Vec<Item>,
    /// Something said in the conversation's own voice: a send that failed, a
    /// permission that was refused. Drawn as the transcript's notice row, which
    /// is the vocabulary it already has.
    /// The last click, for telling one from two.
    clicked: Option<(u16, u16, Instant)>,
    /// Whether the launch asked for an older conversation to carry on with and
    /// the question is not up yet. Cleared the moment the rows arrive.
    resume: bool,
    /// When this screen opened. The blink and the spinner are spaced off it rather
    /// than off a loop counter: any event at all makes the loop come round early,
    /// and a dot that blinks once per loop strobes as soon as the mouse moves.
    born: Instant,
    /// What the launch asked to open with, held until the worker says it is
    /// ready: a message typed for the user is drawn by them, and drawing it
    /// means sending it.
    opening: Option<String>,
    /// When the last peer message landed. The row it drew is swept once, right
    /// after it arrives, so the eye is caught by the row rather than by a bell.
    /// Nothing after the sweep, which is what keeps a still screen still.
    peer_at: Option<Instant>,
    /// Whether origin has moved past this tree, once the check that runs beside
    /// the screen has answered. Nothing is drawn from a question still in
    /// flight, and a check that could not be made draws nothing at all: a screen
    /// with one line for news says the news or says nothing.
    news: Option<version::News>,
    /// The answer's door. A fetch is a network round trip and the frame is
    /// thirty a second, so `App::live` asks on a thread that outlives the frame
    /// and the loop collects it here.
    news_rx: Option<Receiver<version::News>>,
    /// `CUACODE_HOME`, when the launch named one. Kept because switching host
    /// builds a whole new conversation and the state root goes with it.
    home: Option<PathBuf>,
    /// The machine the worker on this screen is running on, `None` for this one.
    /// Only this field and the session know it; everything else on the screen is
    /// about a conversation, not about where it happens.
    host: Option<Host>,
    /// The machines `hosts.json` named, read once by the launch. Read here rather
    /// than at the picker so the file is a start-up cost, not a keystroke's.
    hosts: Vec<Host>,
    /// What each global key means. Read from `keys.json` at launch and never
    /// watched, so a keystroke never reads a file and a rebound key takes the next
    /// start.
    keys: keys::Keymap,
    /// Windows only: whether the key just read was followed by more of a paste
    /// in the console's input queue. Crossterm's Windows backend has no
    /// bracketed paste, so this is what tells a newline inside a pasted block
    /// from the enter that sends it.
    #[cfg(target_os = "windows")]
    paste_burst: bool,
    /// What the field is prepared to type. A character from the console is not
    /// proof a key made it: see `cuacode::console::Typing`. Held on the screen
    /// rather than in the loop because it is the field's own state -- a sequence
    /// split across two events has to still be one sequence.
    typing: cuacode::console::Typing,
}

impl App {
    fn live(home: Option<&std::path::Path>, start: Start, opening: Option<String>) -> io::Result<App> {
        App::live_on(None, home, start, opening)
    }

    /// The same screen, with its worker on `host` instead of this machine.
    ///
    /// A box's worker is **hosted**: it lives in a tmux pane there and this screen is a
    /// client of it. That is what makes a box's conversation outlive this terminal: quit
    /// quik, ctrl-c it, shut the laptop, and the pane on that box carries on, turn in
    /// flight and all. The wire is the same wire; what changed is who owns its ends.
    ///
    /// Nothing about the conversation changes over there either. What does not cross is
    /// the working directory -- a local path names nothing on another machine, so a
    /// remote host starts where its own entry says and the launch's own cwd is dropped.
    fn live_on(
        host: Option<Host>,
        home: Option<&std::path::Path>,
        start: Start,
        opening: Option<String>,
    ) -> io::Result<App> {
        let sess = match &host {
            Some(h) => Session::attach_on(h),
            None => {
                let cwd = std::env::current_dir().ok();
                Session::start_on(&Target::Local, home, cwd.as_deref())
            }
        }
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
        let mut app = App::new(Some(sess));
        app.host = host;
        app.home = home.map(std::path::Path::to_path_buf);
        app.hosts = hosts::load(&cuacode::state_root());
        app.opening = opening;
        // Asked now, answered whenever origin gets round to it. A live screen is
        // the only one that asks: a replay and a `--print` have no tree to keep
        // current and no reader waiting on an update.
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(version::check());
        });
        app.news_rx = Some(rx);
        match start {
            Start::Fresh => {}
            // Asked now, answered on the worker's own time: the question is up as
            // soon as the list comes back, and until then the screen is just the
            // screen. Nothing is loaded until a row is chosen, so escape at the
            // question is the new conversation already sitting behind it.
            Start::Ask => {
                app.resume = true;
                app.act("session.list", serde_json::json!({}));
            }
            // Named, so there is nothing to ask: the conversation replays under
            // the first frame. The picker's row takes the same door, so a launch and
            // a choice cannot drift apart.
            Start::This(id) => app.switch(&id),
        }
        Ok(app)
    }

    fn replaying(path: &str) -> App {
        let mut app = App::new(None);
        app.log.rows.push(Row::Notice {
            head: format!("replay of {path}: there is no worker to send to"),
            hidden: 0,
        });
        if let Ok(raw) = std::fs::read_to_string(path) {
            for line in raw.lines().filter(|l| !l.trim().is_empty()) {
                if let Ok(ev) = wire::parse(line) {
                    app.log.apply(&ev);
                }
            }
        }
        app.follow = true;
        app
    }

    fn new(sess: Option<Session>) -> App {
        App {
            sess,
            log: Transcript::default(),
            input: String::new(),
            undo: Vec::new(),
            cutting: false,
            shots: Vec::new(),
            hover: None,
            field_rows: Vec::new(),
            field_top: 0,
            field_left: 0,
            caret: 0,
            open: false,
            overrides: HashMap::new(),
            thoughts: HashMap::new(),
            scroll: 0,
            follow: true,
            spans: Vec::new(),
            anchors: Vec::new(),
            body_top: 0,
            body_width: 0,
            turn: None,
            figures: HashMap::new(),
            opened: None,
            prompt: None,
            pending: None,
            // Now, so a prompt waiting at launch does not arrive before the window
            // has been looked at.
            since_key: Instant::now(),
            mark: None,
            menu: None,
            commands: Vec::new(),
            skills: Vec::new(),
            clicked: None,
            resume: false,
            born: Instant::now(),
            opening: None,
            peer_at: None,
            news: None,
            news_rx: None,
            home: None,
            host: None,
            hosts: Vec::new(),
            keys: keys::defaults(),
            #[cfg(target_os = "windows")]
            paste_burst: false,
            typing: cuacode::console::Typing::default(),
        }
    }

    /// Put a held prompt up, and say whether it went up. A permission waits for the
    /// keyboard to be still for `ASK_AFTER`, which is what makes it arrive at a pause
    /// rather than over a sentence; a question does not wait at all.
    fn raise_ask(&mut self) -> bool {
        if self.prompt.is_some() {
            return false;
        }
        let Some(ask) = self.pending.take() else {
            return false;
        };
        // A permission is an interruption and waits for the keyboard to be still.
        // A question is what the person is waiting on: holding it leaves the screen
        // silent while the worker sits blocked on them, and every keypress pushes it
        // further away. It goes up at once, over whatever is half typed.
        if !ask.is_question() && self.since_key.elapsed() < ASK_AFTER {
            self.pending = Some(ask);
            return false;
        }
        // The one moment the mark goes up: from here the worker is blocked on a
        // person, and the worker's own stream says nothing about it.
        self.mark = Some(needs_you());
        self.prompt = Some(ask);
        true
    }

    /// Take whatever the worker has said since the last look, and keep the turn's
    /// stopwatch in step with it.
    /// Whether anything on screen is moving by itself: a round in flight, which
    /// turns the spinner and the mark, or a turn whose clock is still running.
    fn animating(&self) -> bool {
        self.sess.as_ref().is_some_and(|s| s.snap.state.busy())
            || self.turn.as_ref().is_some_and(|t| t.ended.is_none())
    }

    /// Take everything the worker has said. Says whether anything arrived, which
    /// is what decides a redraw.
    fn pump(&mut self) -> bool {
        // An answer that has landed, taken before anything else so a screen with
        // no worker - never mind why - still hears it.
        if let Some(rx) = &self.news_rx {
            if let Ok(news) = rx.try_recv() {
                self.news = Some(news);
                self.news_rx = None;
            }
        }
        let events = match &mut self.sess {
            Some(sess) => sess.drain(),
            None => return false,
        };
        let arrived = !events.is_empty();
        for ev in events {
            self.feed(ev);
        }
        let (state, tokens, estimated, think) = match &self.sess {
            Some(sess) => (
                sess.snap.state.clone(),
                (sess.snap.out_tokens > 0).then_some(sess.snap.out_tokens),
                sess.snap.tps_est,
                view::Round {
                    tokens: sess.snap.think_tokens,
                    tps: sess.snap.think_tps,
                    estimated: sess.snap.think_est,
                },
            ),
            None => return false,
        };
        self.track(&state, tokens, estimated);
        // File the round's figures against the reasoning row they belong to. That
        // row keeps them: what they say is what its round cost, and the row being
        // written moves on to a tool call or an answer without carrying them off.
        if think.tokens > 0 || think.tps > 0.0 {
            let mine = self.log.rows.iter().rposition(|r| matches!(r, Row::Thinking { .. }));
            if let Some(i) = mine {
                self.figures.insert(i, think);
            }
        }
        let raised = self.raise_ask();
        arrived || raised
    }

    fn feed(&mut self, ev: Event) {
        // A permission question blocks the worker until it is answered, so it is
        // held for the keyboard rather than folded into the transcript. The panel
        // carries what answering needs: the reason, the call, what it would do.
        if ev.env.kind == "permission" {
            let name = ev.env.data.get("name").and_then(|v| v.as_str()).unwrap_or("?").to_string();
            // A question the model asked arrives on this same envelope, carrying
            // the question and the answers to pick between. A permission carries
            // neither, so this is the whole of the difference the screen needs.
            let question = ev.env.data.get("question").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let options: Vec<String> = ev
                .env
                .data
                .get("options")
                .and_then(|v| v.as_array())
                .map(|rows| rows.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                .unwrap_or_default();
            let kind = ev.env.data.get("kind").and_then(|v| v.as_str()).unwrap_or("").to_string();
            // No kind means an older worker; the question mark is the one field only
            // a question carries, so it is the honest fallback.
            let kind = if kind.is_empty() {
                if ev.env.data.get("question").is_some() { "question".to_string() }
                else { "permission".to_string() }
            } else { kind };
            let asked = Ask { id: ev.env.id.clone(), kind, name, question,
                              detail: ev.env.data.get("detail").and_then(|v| v.as_str())
                                  .unwrap_or("").to_string(),
                              // Zero is the first option when there is one, and the
                              // text row when there is not: rows() is options+1, so
                              // picked 0 with no options is already past the list.
                              options, picked: 0, typed: String::new(),
                              n: ev.env.data.get("n").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
                              of: ev.env.data.get("of").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
                              scroll: 0, body: Vec::new() };
            // A permission is the same panel with two answers and no list of its own:
            // the rows are the decision, and the document behind them is what the
            // decision is about. Refusing on a clipped line is refusing blind.
            let asked = if asked.is_question() {
                asked
            } else {
                let body = view::ask_body(&asked.name, &ev.env.data);
                Ask {
                    options: vec!["allow".to_string(), "refuse".to_string()],
                    body,
                    ..asked
                }
            };
            // A permission is held rather than shown: the worker is blocked either
            // way, and a panel that takes the keyboard out of someone's hands
            // mid-sentence is worse than a question that arrives a second later.
            // `raise_ask` puts it up once the keyboard has been still for
            // `ASK_AFTER`. A question is not held: it goes up over the sentence.
            self.pending = Some(asked);
            // The question has the keyboard, so a menu open under it would be a
            // list of things that cannot be chosen.
            self.menu = None;
            return;
        }
        // A menu's own rows. They are not the conversation, so they reach
        // neither the transcript nor the status line: this is the whole of what
        // becomes of them.
        if let Some(reply) = menu::from_reply(&ev.env.kind, &ev.env.data) {
            // The one answer that is not only a menu's: a session list is the worker's
            // own account of the conversations it has, and the header reads a session's
            // directory out of it. Nothing else a menu answers changes a snapshot.
            if reply.kind == Kind::Sessions && let Some(sess) = &mut self.sess {
                sess.on_event(&ev);
            }
            // What the launch asked for: which conversation to carry on with.
            // The question goes up as its rows arrive, and escape at it is the
            // new session already sitting behind it.
            if reply.kind == Kind::Sessions && self.resume {
                self.resume = false;
                self.menu = Some(Menu::open_apart(Kind::Sessions));
            }
            // The directory a new conversation would stand in, read before the menu is
            // borrowed: the picker's own row says it, because on a box that is the one
            // fact a person needs before choosing it.
            let where_new = self.new_session_where();
            if let Some(open) = self.menu.as_mut().filter(|open| open.kind == reply.kind) {
                // Which provider a catalog belongs to travels with it, rather
                // than being guessed from what the frontend last heard.
                open.provider = reply.provider.clone();
                // Why a directory could not be read, which is the difference
                // between an empty folder and a folder that is not there.
                open.error = reply.error.clone();
                // A `/` menu is filled twice: from the rows in hand when it
                // opens, and again from the list that lands under it. The
                // second fill must carry this frontend's own verbs too, or the
                // row being filtered for goes out from under the keystrokes.
                open.fill(match reply.kind {
                    Kind::Commands => menu::with_local(reply.items.clone()),
                    // The list is that machine's own conversations, and the row above
                    // them is the one that is not on it yet.
                    Kind::Sessions => menu::sessions(&where_new, reply.items.clone()),
                    _ => reply.items.clone(),
                });
            }
            // The two lists that cannot change while a session is open are held;
            // a picker asks every time, because what it marks is what changed.
            match reply.kind {
                Kind::Commands => self.commands = reply.items,
                Kind::Skills => self.skills = reply.items,
                _ => {}
            }
            return;
        }
        // The answer to opening a call: the arguments and the result the worker
        // keeps on the records, which the wire only carried a summary of. The
        // title stays the row's own -- the panel should not rename itself out
        // from under the reader the moment the record lands.
        if ev.env.kind == "detail" {
            let mut panel = view::call_detail(&ev.env.data, self.detail_room());
            if let Some(open) = &self.opened {
                panel.title = open.title.clone();
            }
            self.opened = Some(panel);
            return;
        }
        // The worker naming the conversation it now holds, which it sends before the
        // replay of it. This is the one place the screen becomes another conversation:
        // every door ends in a `session.load`, and a load that failed answers `error`
        // and never reaches this line, so the screen that could not switch keeps what
        // it had.
        if ev.env.kind == "status" && ev.state() == "session" {
            self.enter();
        }
        if let Some(sess) = &mut self.sess {
            sess.on_event(&ev);
        }
        // The launch's own message goes the moment the worker says it is up.
        // Ready rather than boot: this frontend's terminal envelope has to be in
        // first, or the worker stamps the turn with the directory this process
        // happens to stand in instead of the one the user was standing in.
        if ev.state() == "ready"
            && let Some(text) = self.opening.take()
        {
            self.input = text;
            self.caret = self.input.chars().count();
            self.send();
        }
        if self.log.apply(&ev) {
            if ev.state() == "peer" {
                self.peer_at = Some(Instant::now());
            }
            if self.follow {
                // A new row while the view is at the end: stay at the end.
                self.scroll = u16::MAX;
            }
        }
    }

    /// Start, run and stop the clock on a round.
    fn track(&mut self, state: &State, tokens: Option<i64>, estimated: bool) {
        // A round that never happened leaves nothing behind. `idle` is the worker
        // saying the message started no loop at all, which is what a command
        // does: the clock was started optimistically the moment the message was
        // sent, and this is where it comes back down. Only this state clears a
        // round -- one that ran keeps its figures after it ends, because they are
        // what it cost.
        if matches!(state, State::Idle) {
            self.turn = None;
            return;
        }
        let running = state.busy();
        // A round that starts once the last one has ended is a new round: the
        // clock, the count and the dot all start over.
        let fresh = match &self.turn {
            Some(t) => running && t.ended.is_some(),
            None => running,
        };
        if fresh {
            self.turn = Some(Turn::starting());
        }
        if let Some(t) = &mut self.turn {
            t.tokens = tokens.or(t.tokens);
            t.estimated = estimated;
            if !running && t.ended.is_none() {
                t.ended = Some(Instant::now());
            }
        }
    }

    /// The row the worker is writing right now.
    ///
    /// The row being streamed into, when there is one: a message typed mid-turn
    /// pushes a row under it, so "the newest row" stops being the worker's own the
    /// moment the person says anything. With nothing streaming -- a call in flight --
    /// the newest row is the one.
    fn writing(&self) -> Option<usize> {
        match &self.sess {
            Some(sess) if sess.snap.state.busy() => {
                self.log.open_row().or_else(|| self.log.rows.len().checked_sub(1))
            }
            _ => None,
        }
    }

    /// Which half of the live row's blink is on. Half a second either way: fast
    /// enough to read as alive, slow enough to read at all.
    fn flash(&self) -> bool {
        (self.ms() / 500) % 2 == 0
    }

    /// How far the highlight travelling over a row has got, or None when nothing on
    /// the screen is moving. Read off this screen's own clock and the session it has.
    fn shimmer(&self) -> Option<usize> {
        shimmer_phase(self.sess.as_ref().map(|s| &s.snap), self.peer_at.map(|t| t.elapsed()), self.ms())
    }

    /// What the screen knows about the round: where the worker is writing right
    /// now, and the figures filed against a row.
    fn figures(&self, i: usize) -> Option<view::Round> {
        self.figures.get(&i).copied()
    }

    /// How long this screen has been up, which is what the blink, the spinner and
    /// the turn's stopwatch are all spaced off.
    fn ms(&self) -> u128 {
        self.born.elapsed().as_millis()
    }

    /// Nothing has been said yet: no record and no panel over the screen, so this is the
    /// opening rather than a conversation. A resumed session always has rows, and so does
    /// one with a turn in it. A menu does not count: `/`, `$` and `@` are typed into the
    /// same box on the opening screen, and switching layouts under the first character
    /// typed is the screen jumping for no reason.
    fn blank(&self) -> bool {
        self.log.rows.is_empty() && self.prompt.is_none() && self.opened.is_none()
    }

    /// The top row of the frame: what this is, and where it is running. The build
    /// id is the one fact a screen cannot show by being used, and the directory is
    /// the conversation's own -- on a box that is a path on that machine, and this
    /// process's cwd is a path on this one.
    fn header(&self) -> Vec<Line<'static>> {
        let mut out = vec![Line::from(vec![
            Span::styled(" Cuacode-RS", Style::new().fg(ink::text()).add_modifier(Modifier::BOLD)),
            Span::raw("  "),
            Span::styled(format!("v{}", version::ID), Style::new().fg(ink::faint())),
        ])];
        let dir = self.working_dir();
        if !dir.is_empty() {
            out.push(Line::from(Span::styled(format!(" {dir}"), Style::new().fg(ink::dim()))));
        }
        out
    }

    /// Where a message typed into this screen would run: the conversation's own
    /// directory, read off the worker's list of its sessions, because on a box that is a
    /// path on that machine and this process's cwd is a path on this one. A conversation
    /// with no record yet is not in the list, and there the launch's directory is the
    /// honest answer.
    fn working_dir(&self) -> String {
        self.sess
            .as_ref()
            .and_then(|s| s.sessions.iter().find(|m| m.id == s.snap.session_id))
            .map(|m| m.cwd.clone())
            .unwrap_or_else(session::working_dir)
    }

    /// The line under the card, as three zones on one row: what the round is doing,
    /// how much of the window the conversation has filled, and the counts the worker
    /// reported.
    ///
    /// `None` is a replay: no worker, so no session, so no row. A session always has
    /// one, even before it has reported anything, because a row that appears the
    /// moment the first number arrives moves the card under the person typing in it.
    /// What is empty stays empty: an absence is not a zero, and the zones say nothing
    /// the wire did not send.
    ///
    /// `width` is the pane, which the right zone is pushed against.
    fn status_text(&self, width: usize) -> Option<Line<'static>> {
        let sess = self.sess.as_ref()?;
        Some(status_line(&sess.snap, width, self.ms()))
    }

    /// The message inside the composer: the rows it wraps to, with the caret in it. The
    /// only place the keyboard types; what the round is doing lives on the model row under
    /// it, not on the line being typed.
    ///
    /// `height` is what the card's interior gave the box, which is one row per row the
    /// message wraps to, up to `FIELD_MAX`. Past that the window follows the caret
    /// rather than the end of the message: the caret is where the next character
    /// lands, so it is what has to be on screen.
    ///
    /// `top` is the field's first row on screen, which is only needed by a chip:
    /// it is the row comparison that says whether the pointer is on this one.
    fn field_lines(&self, width: usize, height: usize, top: u16) -> Vec<Line<'static>> {
        if self.prompt.is_some() {
            // A prompt is drawn as its own panel, not as a line in this box: it has a
            // list in it, and the field is not where it is answered. This line is only
            // what remains of the box while one is up.
            return vec![Line::from("")];
        }
        // The prompt's two cells are the box's, not the text's: a row wraps where the
        // text stops fitting beside them, which is what `field_height` counts too.
        let inner = width.saturating_sub(LEAD).max(1);
        let (cols, rows, caret_col, caret_row) = self.drawn(inner);
        let n = rows.len();
        let shown = height.clamp(1, n.max(1));
        let first_shown = (caret_row + 1).saturating_sub(shown).min(n - shown.min(n));
        let mut out = Vec::new();
        for li in first_shown..first_shown + shown {
            let first = li == 0;
            // The box's own line, down every row of the message: one mark in the
            // accent, the text a cell inside it. `\u{2502}` is the light weight, the same
            // one the box's two rules are drawn in, so the box is one family at one
            // weight and the accent is what says it is a line rather than a border.
            let lead = RAIL;
            let (line, tags): (Vec<char>, Vec<u8>) = cols[rows[li].0..rows[li].1]
                .iter()
                .map(|(c, t)| (*c, *t))
                .unzip();
            let text: String = line.into_iter().collect();
            let caret_at = caret_col.saturating_sub(rows[li].0);
            let mine = li == caret_row && (!self.input.is_empty() || !self.shots.is_empty());
            // Which chip the pointer is on, if the pointer is on this row: the
            // column it is over is a column of the row, since a row is now as wide
            // as the box and never wider.
            let pointer = self.hover.and_then(|(x, y)| {
                let row = y == top + (li - first_shown) as u16;
                let text = self.field_left + LEAD as u16;
                (row && x >= text).then(|| x.saturating_sub(text) as usize)
            });
            let mut spans = vec![Span::styled(lead, Style::new().fg(ink::call()))];
            let cs: Vec<char> = text.chars().collect();
            // Which chip the pointer is over, if it is over one of them at all.
            let under_pointer = pointer.and_then(|p| tags.get(p).copied()).filter(|t| *t != 0);
            let mut run = String::new();
            let mut run_tag = 0u8;
            let flush = |spans: &mut Vec<Span<'static>>, run: &mut String, tag: u8| {
                if run.is_empty() {
                    return;
                }
                spans.push(Span::styled(std::mem::take(run), chip_style(tag, under_pointer)));
            };
            for (k, (c, tag)) in cs.iter().zip(tags.iter()).enumerate() {
                let tag = *tag;
                if c == &'\n' {
                    continue;
                }
                if k == caret_at && mine {
                    flush(&mut spans, &mut run, run_tag);
                    let style = chip_style(tag, under_pointer).add_modifier(Modifier::REVERSED);
                    spans.push(Span::styled(c.to_string(), style));
                    run_tag = tag;
                    continue;
                }
                if tag != run_tag {
                    flush(&mut spans, &mut run, run_tag);
                    run_tag = tag;
                }
                run.push(*c);
            }
            flush(&mut spans, &mut run, run_tag);
            // The caret past the last character of the line, and an empty field:
            // the thin bar it would land beside.
            if mine && caret_at >= cs.len() {
                spans.push(Span::styled(
                    "\u{258f}",
                    Style::new().fg(ink::text()).add_modifier(Modifier::REVERSED),
                ));
            }
            // An empty box says what it is for, the way opencode's does.
            if self.input.is_empty() && self.shots.is_empty() {
                let said = if first { "Ask anything, or /help" } else { "" };
                if !said.is_empty() {
                    // `dim`, not `quiet`: this sits on the raised surface, where the
                    // quietest rung of the page measures 2.2:1 and reads as a smudge.
                    spans.push(Span::styled(said, Style::new().fg(ink::dim())));
                }
            }
            out.push(Line::from(spans));
        }
        out
    }

    /// The message as the columns it is drawn in, the rows those columns wrap to,
    /// and where the caret sits among them.
    ///
    /// A chip is drawn as its label rather than as the characters it covers, so
    /// this is the one place the two get reconciled: `cols` is what goes on the
    /// screen, and `rows` slices it into what fits `width`. Wrapping happens here
    /// and not in the widget, because a chip has to be moved whole to the next row
    /// and a widget would break `[image 1]` in half.
    ///
    /// `keep` is gone: the turn's readout lives on the composer's model row now, so no row
    /// of the message owes anything at its right end.
    fn drawn(&self, width: usize) -> (Vec<(char, u8)>, Vec<(usize, usize)>, usize, usize) {
        let cs: Vec<char> = self.input.chars().collect();
        let mut tags = vec![0u8; cs.len()];
        for (n, shot) in self.shots.iter().enumerate() {
            for i in shot.at..(shot.at + shot.len).min(cs.len()) {
                tags[i] = (n + 1) as u8;
            }
        }
        let mut cols: Vec<(char, u8)> = Vec::new();
        let mut col_of = vec![0usize; cs.len() + 1];
        let mut i = 0;
        while i < cs.len() {
            col_of[i] = cols.len();
            let tag = tags[i];
            if tag == 0 {
                cols.push((cs[i], 0));
                i += 1;
                continue;
            }
            for ch in format!("[image {tag}]").chars() {
                cols.push((ch, tag));
            }
            while i < cs.len() && tags[i] == tag {
                i += 1;
            }
        }
        col_of[cs.len()] = cols.len();
        let caret = col_of[self.caret.min(cs.len())];
        // A newline is a row of its own wherever it falls, and the first row's
        // budget is the box less what it owes the readout. The budget is measured
        // in drawn columns, so a chip counts as the label it is drawn as.
        let mut rows: Vec<(usize, usize)> = Vec::new();
        let mut start = 0usize;
        let mut used = 0usize;
        // The last space inside the row, which is where the wrap breaks if it can:
        // a word goes whole to the next row rather than being cut at the edge.
        let mut space: Option<usize> = None;
        let mut k = 0usize;
        while k < cols.len() {
            // Every row wraps one cell short of the box's edge: the last cell of a row
            // is the caret's when the typing reaches the end of it, so the wrap keeps it.
            // Nothing at the right end of the first row any more -- the round's readout
            // is on the metadata row under it -- so one rule for every row.
            let room = width.saturating_sub(1).max(1);
            let (c, tag) = cols[k];
            if c == '\n' {
                rows.push((start, k));
                start = k + 1;
                used = 0;
                space = None;
                k += 1;
                continue;
            }
            // A chip is one thing on the row or it is not on the row: it moves
            // whole rather than being cut at the edge of the box. A chip with a
            // space in it is a chip, so a chip is never a break point either.
            let mut run = 1usize;
            if tag != 0 {
                while k + run < cols.len() && cols[k + run].1 == tag {
                    run += 1;
                }
            }
            if used + run > room && k > start {
                match space.filter(|sp| *sp >= start) {
                    // Break after the space, and the space itself is the one thing
                    // the wrap eats: it was a separator, not part of the line.
                    Some(sp) => {
                        rows.push((start, sp));
                        start = sp + 1;
                        used = k - start;
                    }
                    // A word longer than a whole row: it is split rather than
                    // allowed to run off the edge, which is the only way to draw
                    // it at all.
                    None => {
                        rows.push((start, k));
                        start = k;
                        used = 0;
                    }
                }
                space = None;
                continue;
            }
            if c == ' ' {
                space = Some(k);
            }
            used += run;
            k += run;
        }
        rows.push((start, cols.len()));
        // The row the caret is on is the row its own drawn column falls in, and
        // the last row takes a caret that sits past everything else.
        let caret_row = rows.iter().rposition(|(s, _)| *s <= caret).unwrap_or(0);
        (cols, rows, caret, caret_row)
    }

    /// The message as its own lines: a newline in the field is a line, the way
    /// it is in the message it becomes. The drawing does not use this -- a chip
    /// is drawn as its label, so the lines come from `drawn()` -- but a test that
    /// is about the message rather than about the picture reads better this way.
    #[cfg(test)]
    #[allow(dead_code)]
    fn lines(&self) -> Vec<String> {
        self.input.split('\n').map(str::to_string).collect()
    }

    /// Whether the message is more than one line, which is what decides whether
    /// the editing keys belong to it or to the conversation. A hard newline, not a
    /// wrap: a row the box made for itself is still the same line to the keys.
    fn multiline(&self) -> bool {
        self.input.contains('\n')
    }

    /// How tall the box wants to be, before the frame decides how much it can
    /// spare: one row per row the message wraps to at this width, up to the
    /// ceiling.
    fn field_height(&self, width: usize) -> u16 {
        let inner = width.saturating_sub(LEAD).max(1);
        (self.drawn(inner).1.len()).clamp(1, FIELD_MAX) as u16
    }

    /// How many characters are in the message.
    fn chars(&self) -> usize {
        self.input.chars().count()
    }

    /// Where the caret sits, as a byte offset.
    fn caret_byte(&self) -> usize {
        self.input
            .char_indices()
            .nth(self.caret)
            .map(|(at, _)| at)
            .unwrap_or(self.input.len())
    }

    /// Where the caret's own line starts, in characters.
    fn line_start(&self) -> usize {
        let cs: Vec<char> = self.input.chars().collect();
        let mut at = self.caret.min(cs.len());
        while at > 0 && cs[at - 1] != '\n' {
            at -= 1;
        }
        at
    }

    /// Where it ends, in characters.
    fn line_end(&self) -> usize {
        let cs: Vec<char> = self.input.chars().collect();
        let mut at = self.caret.min(cs.len());
        while at < cs.len() && cs[at] != '\n' {
            at += 1;
        }
        at
    }

    /// A picture onto the message, as a chip at the caret.
    ///
    /// The characters the chip covers are ordinary text -- `at` and `len` are
    /// where they are in the message -- and a space goes on either side so the
    /// next word is a word of its own: `abc [image 1] and` rather than
    /// `abc[image 1]and`. The whole insertion is one step of the caret.
    fn attach(&mut self, image: attach::Image) {
        let label = format!(" [image {}] ", self.shots.len() + 1);
        let at = self.caret.min(self.chars());
        let cs: Vec<char> = self.input.chars().collect();
        let head: String = cs[..at].iter().collect();
        let tail: String = cs[at..].iter().collect();
        self.input = format!("{head}{label}{tail}");
        self.shots.push(Shot { image, at, len: label.chars().count() });
        let last = self.shots.len() - 1;
        let width = label.chars().count();
        for shot in &mut self.shots[..last] {
            if shot.at >= at {
                shot.at += width;
            }
        }
        self.caret = at + width;
    }

    /// Take one picture off the message, chip and bytes together.
    ///
    /// The message loses the characters the chip covered and every shot after it
    /// moves back by that many, which is what keeps the chips and the text in
    /// step. The bytes go with the shot: the message never held more than the
    /// characters, so removing the chip is what removes the picture.
    fn detach(&mut self, n: usize) {
        let Some((at, len)) = self.shots.get(n).map(|s| (s.at, s.len)) else { return };
        // A chip goes off by a key or by a click, and both are a deletion someone
        // can want back, so the snapshot is taken here rather than at the key.
        self.hold();
        let cs: Vec<char> = self.input.chars().collect();
        let head: String = cs[..at.min(cs.len())].iter().collect();
        let tail: String = cs[(at + len).min(cs.len())..].iter().collect();
        self.input = format!("{head}{tail}");
        self.shots.remove(n);
        self.shift_shots_from(at, -(len as i32));
        self.caret = self.caret.min(at).min(self.chars());
        self.relabel();
    }

    /// Every chip's own text rewritten as its place in the message.
    ///
    /// The message carries the label, and the label carries the number, so taking
    /// a middle picture off leaves the ones after it wearing their old numbers:
    /// the text would say `[image 3]` while the model was handed two pictures.
    /// The number is the one thing this cannot get wrong, so the text is rebuilt
    /// from the shots rather than patched.
    fn relabel(&mut self) {
        let cs: Vec<char> = self.input.chars().collect();
        let mut order: Vec<usize> = (0..self.shots.len()).collect();
        order.sort_by_key(|i| self.shots[*i].at);
        let mut out = String::new();
        let mut shots = Vec::with_capacity(order.len());
        let mut at = 0usize;
        let mut caret = None;
        for (position, i) in order.iter().enumerate() {
            let shot = self.shots[*i].clone();
            let start = shot.at.min(cs.len());
            let end = (shot.at + shot.len).min(cs.len());
            let before: String = cs[at..start].iter().collect();
            // Where the caret was, counted into the message being rebuilt.
            if caret.is_none() && self.caret <= start {
                caret = Some(out.chars().count() + before.chars().count());
            }
            out.push_str(&before);
            let label = format!(" [image {}] ", position + 1);
            let label_at = out.chars().count();
            if caret.is_none() && self.caret < end {
                caret = Some(label_at + (self.caret - start));
            }
            out.push_str(&label);
            shots.push(Shot { image: shot.image, at: label_at, len: label.chars().count() });
            at = end;
        }
        let tail: String = cs[at.min(cs.len())..].iter().collect();
        if caret.is_none() {
            caret = Some(out.chars().count() + (self.caret.saturating_sub(at)));
        }
        out.push_str(&tail);
        self.input = out;
        self.shots = shots;
        // A caret that no longer exists in a shorter message goes to its end.
        self.caret = caret.unwrap_or(self.caret).min(self.chars());
    }

    /// Every shot at or after `from` moves by `by` characters.
    fn shift_shots_from(&mut self, from: usize, by: i32) {
        for shot in &mut self.shots {
            if shot.at >= from {
                shot.at = (shot.at as i32 + by).max(0) as usize;
            }
        }
    }

    /// The caret never rests strictly inside a chip: it belongs to one of its
    /// edges. Backwards lands on the left one, forwards on the right.
    fn snap_caret_out(&mut self, backwards: bool) {
        if let Some(shot) = self.shots.iter().find(|s| self.caret > s.at && self.caret < s.at + s.len) {
            self.caret = if backwards { shot.at } else { shot.at + shot.len };
        }
    }

    /// The shot that ends exactly where the caret is, which is what a backspace
    /// finds just behind it.
    fn shot_ending_at(&self, at: usize) -> Option<usize> {
        self.shots.iter().position(|s| s.at + s.len == at)
    }

    /// Cut the message between two characters, taking any chip that fell inside
    /// the cut and moving the ones after it back.
    fn cut(&mut self, start: usize, end: usize) {
        let cs: Vec<char> = self.input.chars().collect();
        let (start, end) = (start.min(cs.len()), end.min(cs.len()));
        if start >= end {
            return;
        }
        self.hold();
        let head: String = cs[..start].iter().collect();
        let tail: String = cs[end..].iter().collect();
        self.input = format!("{head}{tail}");
        self.shots.retain(|s| s.at >= end || s.at + s.len <= start);
        self.shift_shots_from(end, -((end - start) as i32));
        self.caret = start;
        self.relabel();
    }

    /// The start of the word behind the caret, in the same sense the word jumps
    /// mean: over the spaces, then over the word.
    fn word_start(&self) -> usize {
        let cs: Vec<char> = self.input.chars().collect();
        let mut at = self.caret.min(cs.len());
        while at > 0 && cs[at - 1].is_whitespace() {
            at -= 1;
        }
        while at > 0 && !cs[at - 1].is_whitespace() {
            at -= 1;
        }
        at
    }

    /// The far side of the next word boundary: over the rest of this word, then
    /// over the spaces that follow. The same distance the word jumps travel, so a
    /// word delete and a word jump agree about where a word ends.
    fn word_end(&self) -> usize {
        let cs: Vec<char> = self.input.chars().collect();
        let mut at = self.caret.min(cs.len());
        while at < cs.len() && !cs[at].is_whitespace() {
            at += 1;
        }
        while at < cs.len() && cs[at].is_whitespace() {
            at += 1;
        }
        at
    }

    fn insert(&mut self, c: char) {
        // Typing ends a run of deletions: what is deleted from here is a new
        // accident, and ctrl-y should reach only as far back as it.
        self.cutting = false;
        self.snap_caret_out(true);
        let at = self.caret.min(self.chars());
        // A chip starting at the caret is pushed back by the character; one the
        // caret is standing on has already been stepped out of.
        for shot in &mut self.shots {
            if shot.at >= at {
                shot.at += 1;
            }
        }
        let byte = self.caret_byte();
        self.input.insert(byte, c);
        self.caret += 1;
    }

    /// One character as the field takes it, with any terminal sequence removed.
    ///
    /// The key handler inserts through here rather than through `insert` directly,
    /// because a character arriving as a `KeyCode::Char` is not proof that a key
    /// made it: with the console's input mode drifted, a pointer moving over the
    /// window arrives as the characters of a mouse report. `cuacode::console::Typing`
    /// is the one place that decides what those are; this is the field's door.
    fn type_char(&mut self, c: char) {
        if let Some(c) = self.typing.keep(c) {
            self.insert(c);
        }
    }

    fn backspace(&mut self) {
        if self.caret == 0 {
            return;
        }
        self.hold();
        // A chip behind the caret goes whole, which is the one press that undoes
        // the one gesture that put it there.
        if let Some(n) = self.shot_ending_at(self.caret) {
            self.detach(n);
            return;
        }
        let end = self.caret_byte();
        let start = self.input[..end].char_indices().last().map(|(at, _)| at).unwrap_or(0);
        self.input.replace_range(start..end, "");
        self.caret -= 1;
        self.shift_shots_from(self.caret + 1, -1);
    }

    /// ctrl+backspace, and its mac spelling, which is the same cut.
    fn word_backspace(&mut self) {
        let start = self.word_start();
        self.cut(start, self.caret);
    }

    fn delete(&mut self) {
        // The same as backspace, looking forward.
        if let Some(n) = shot_at(&self.shots, self.caret) {
            self.detach(n);
            return;
        }
        let at = self.caret_byte();
        let Some(c) = self.input[at..].chars().next() else { return };
        self.hold();
        self.input.replace_range(at..at + c.len_utf8(), "");
        self.shift_shots_from(self.caret + 1, -1);
    }

    fn word_delete(&mut self) {
        let end = self.word_end();
        self.cut(self.caret, end);
    }

    /// From the caret back to the start of its line: cmd+backspace on a mac,
    /// which is ctrl-u in the terminal that reports it.
    fn kill_to_line_start(&mut self) {
        let start = self.line_start();
        self.cut(start, self.caret);
    }

    /// From the caret to the end of its line: cmd+delete on a mac, ctrl+k in
    /// readline, and the same cut wherever a terminal sends it.
    fn kill_to_line_end(&mut self) {
        let end = self.line_end();
        self.cut(self.caret, end);
    }

    /// Keep the field as it stands, so a deletion can be taken back.
    ///
    /// The field whole rather than the characters the cut took, because a chip is a
    /// picture and its number has to match the pictures the model is handed: a label
    /// yanked back with no shot behind it is a lie about what is being sent, and
    /// putting a shot back into the middle of a rebuilt label is arithmetic that can
    /// be wrong. A snapshot cannot be wrong about either.
    fn hold(&mut self) {
        if self.cutting || (self.input.is_empty() && self.shots.is_empty()) {
            return;
        }
        let now = self.held();
        self.stash(now);
        self.cutting = true;
    }

    /// The field as it stands, for the stack to carry.
    fn held(&self) -> Held {
        Held { input: self.input.clone(), shots: self.shots.clone(), caret: self.caret }
    }

    /// Onto the stack, oldest dropped past twenty.
    // ponytail: twenty entries and no ring. Deeper than that is a history a composer
    // does not need, and the ring is not worth the code here.
    fn stash(&mut self, held: Held) {
        self.undo.push(held);
        if self.undo.len() > 20 {
            self.undo.remove(0);
        }
    }

    /// ctrl-y: the field back as it stood before the last run of deletions, chips
    /// and caret where they were. readline's yank, and the one key between a slipped
    /// cmd-backspace and a lost paragraph.
    ///
    /// What is on the field now goes back on the stack rather than under it: putting
    /// back a deleted paragraph must not be a way to lose the one typed since, so a
    /// second ctrl-y has that one again.
    fn put_back(&mut self) {
        let Some(held) = self.undo.pop() else { return };
        if !self.input.is_empty() || !self.shots.is_empty() {
            let now = self.held();
            self.stash(now);
        }
        self.input = held.input;
        self.shots = held.shots;
        self.caret = held.caret;
        self.cutting = false;
    }

    fn left(&mut self) {
        self.caret = self.caret.saturating_sub(1);
        self.snap_caret_out(true);
    }

    fn right(&mut self) {
        self.caret = (self.caret + 1).min(self.chars());
        self.snap_caret_out(false);
    }

    /// A word at a time, in the sense every terminal means it: over the spaces,
    /// then over the word.
    fn word_left(&mut self) {
        self.caret = self.word_start();
        self.snap_caret_out(true);
    }

    fn word_right(&mut self) {
        self.caret = self.word_end();
        self.snap_caret_out(false);
    }

    /// A line up or down, keeping the column where it can: a shorter line takes
    /// the caret to its end rather than wrapping it somewhere it did not ask for.
    fn up_line(&mut self) {
        let column = self.caret - self.line_start();
        if self.line_start() == 0 {
            return;
        }
        self.caret = self.line_start() - 1;
        self.caret = (self.line_start() + column).min(self.line_end());
        self.snap_caret_out(true);
    }

    fn down_line(&mut self) {
        let column = self.caret - self.line_start();
        if self.line_end() == self.chars() {
            return;
        }
        self.caret = self.line_end() + 1;
        self.caret = (self.line_start() + column).min(self.line_end());
        self.snap_caret_out(false);
    }

    fn line_home(&mut self) {
        self.caret = self.line_start();
        self.snap_caret_out(false);
    }

    fn to_line_end(&mut self) {
        self.caret = self.line_end();
        self.snap_caret_out(true);
    }

    /// Send what has been typed.
    fn send(&mut self) {
        let text = self.input.trim().to_string();
        // A message of nothing but a picture is a message: the worker asks the
        // model what it makes of it, which is the whole reason to paste one.
        if text.is_empty() && self.shots.is_empty() {
            return;
        }
        // The message is sent: there is nothing left to take back, and a ctrl-y
        // after the send raising the text that went would be a second message that
        // looks sent and is not.
        self.undo.clear();
        self.cutting = false;
        if self.shots.is_empty() {
            // The two verbs this frontend answers itself, read before anything
            // goes down the pipe. The worker has a command for neither, so sent
            // as prose they would reach the model: `/help` a question about keys,
            // and `/permissions` a request in a conversation that has no such
            // thing. Both are read only when nothing is attached: `/help` with a
            // picture on it is a message, not a request for this screen's own
            // panel.
            if text == "/help" {
                self.input.clear();
                self.caret = 0;
                self.opened = Some(view::keys(&self.keys));
                return;
            }
            if text == "/permissions" || text.starts_with("/permissions ") {
                let arg = text["/permissions".len()..].trim().to_lowercase();
                self.input.clear();
                self.caret = 0;
                match arg.as_str() {
                    // On its own it is the question, and the list is the answer.
                    "" => self.pick(Kind::Permissions),
                    mode @ ("ask" | "auto" | "bypass" | "off") => {
                        self.act("permission.mode", serde_json::json!({ "mode": mode }));
                    }
                    other => self.say(format!("permissions: no mode called {other} (ask, auto, bypass, off)")),
                }
                return;
            }
        }
        let images: Vec<attach::Image> = self.shots.iter().map(|s| s.image.clone()).collect();
        // Drawn here and not sent to the worker: a live turn is never echoed
        // back, so this is the only chance to show what was asked. The row says
        // the pictures by name, the way a replayed one does.
        self.log.say(&text, images.iter().map(|i| i.name.clone()).collect());
        match &mut self.sess {
            Some(sess) => {
                if let Err(e) = sess.chat(&text, &images) {
                    self.say(e);
                }
            }
            // A replay has no worker. Saying so beats a field that looks live.
            None => self.say("this is a replay: there is nothing to send to".into()),
        }
        self.input.clear();
        self.shots.clear();
        self.caret = 0;
        self.follow = true;
    }

    /// Fold or unfold one step, on the reader's own authority rather than the
    /// global. The key is the step's head row: the thought when it has one, the
    /// first call when it does not.
    fn toggle_step(&mut self, head: usize) {
        let row = self.log.rows.get(head).cloned();
        let Some(row) = row else { return };
        if !view::foldable(&row) {
            return;
        }
        let open = self.shows(head, &row);
        self.overrides.insert(head, !open);
    }

    /// Whether a thought shows its text: the reader's own choice if they made one,
    /// the screen's own if they did not.
    fn shows(&self, i: usize, _row: &Row) -> bool {
        self.overrides.get(&i).copied().unwrap_or(self.open)
    }

    /// Whether one thought inside an opened step shows its text. Its own fold, and
    /// nothing global: ctrl-t is what opens a step's calls, and a step's thinking
    /// is opened one thought at a time.
    fn thought_shows(&self, i: usize) -> bool {
        self.thoughts.get(&i).copied().unwrap_or(false)
    }

    fn toggle_thought(&mut self, i: usize) {
        if !matches!(self.log.rows.get(i), Some(Row::Thinking { .. })) {
            return;
        }
        let open = self.thought_shows(i);
        self.thoughts.insert(i, !open);
    }

    fn scroll_by(&mut self, by: i64) {
        self.follow = false;
        self.scroll = (self.scroll as i64 + by).max(0) as u16;
    }

    /// Where a call can be opened: the row under the pointer, if it is a call.
    fn open(&mut self, i: usize) {
        // The row is what the panel is built from, so it is taken out of the log
        // first: the log cannot be borrowed while the screen is being written to.
        let row = match self.log.rows.get(i) {
            Some(row @ Row::Tool { .. }) => row.clone(),
            _ => return,
        };
        let Row::Tool { name, args, call, result, index } = &row else { return };
        // The arguments first, at once: the wire carried them whole, so the panel
        // is never empty while the worker is asked for its own record of the call.
        self.opened = Some(view::call_from_row(name, args, call, result.as_deref(), self.detail_room()));
        // A call with no index has no record to ask for -- it is still running, or
        // a replay, which has no worker at all. The row's own copy is the answer.
        let Some(index) = *index else { return };
        if let Some(sess) = &mut self.sess {
            if let Err(e) = sess.command("tool.detail", serde_json::json!({ "index": index })) {
                self.say(e);
            }
        }
    }

    /// Open the calls menu: every call this conversation has made, newest first,
    /// so the one just watched go past is already under the cursor and an older
    /// one is a line away. This is what a keyboard has instead of a mouse to
    /// double click a row with, and it is the only way to reach a call that is
    /// not the newest.
    fn open_calls(&mut self) {
        let items = menu::calls(&self.log.rows);
        if items.is_empty() {
            self.say("no call in this conversation to open".into());
            return;
        }
        let mut open = Menu::open_apart(Kind::Calls);
        open.fill(items);
        self.menu = Some(open);
    }

    /// A bracketed paste, which is how a terminal hands over what was dropped on
    /// it or pasted in from elsewhere.
    ///
    /// All or nothing: a paste that is mostly prose with a filename in it is
    /// prose, and pulling one word out of it would be the wrong reading of an
    /// ordinary paste. Every token naming an image is the file-manager drop of
    /// one or several pictures, which is a gesture and not text.
    ///
    /// ponytail: a path *typed* by hand is not absorbed, only a pasted one. That
    /// would mean a stat on every keystroke, against a gesture nobody uses: the
    /// drop is what writes a path into a terminal, and the clipboard is the other
    /// door.
    fn paste(&mut self, text: String) {
        // A terminal may hand a paste over with CRLF or bare CR line endings,
        // depending on where the text came from. The field keeps lines, so both
        // spellings collapse to the newline the keyboard inserts.
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        if let Some(images) = paths_as_images(&text) {
            for image in images {
                self.attach(image);
            }
            return;
        }
        for c in text.chars() {
            self.type_char(c);
        }
    }

    /// Something said in the conversation's own voice. The transcript's notice row
    /// is the vocabulary it already has, so nothing new is invented on screen.
    fn say(&mut self, note: String) {
        self.log.rows.push(Row::Notice { head: note, hidden: 0 });
    }

    /// The columns a panel's own code blocks have to fit in: the conversation
    /// pane less the margin and frame the panel is drawn with. It is the width the
    /// last frame drew at, which is the width the next one will, so a panel opened
    /// by a keystroke is sized right.
    ///
    /// ponytail: a resize between that frame and the panel being built leaves the
    /// panel a frame stale. Closing and reopening it is the fix; sizing at draw
    /// time would mean the panel carrying its data instead of its lines.
    fn detail_room(&self) -> usize {
        self.body_width.saturating_sub(4).max(8) as usize
    }

    /// The piece a screen line belongs to, for a click: the row it was drawn from
    /// and the row its step folds against.
    fn anchor_at(&self, y: u16) -> Option<(usize, usize)> {
        let line = self.scroll.checked_add(y.checked_sub(self.body_top)?)? as usize;
        let at = self.spans.iter().position(|(start, end)| line >= *start && line < *end)?;
        self.anchors.get(at).copied()
    }

    fn mouse(&mut self, m: MouseEvent) {
        // The pointer's own place in the field, for a chip that lights up under
        // it. Set before anything is decided, because the light is a fact about
        // the pointer and not about what the click does.
        self.hover = (m.kind != MouseEventKind::Up(MouseButton::Left)).then_some((m.column, m.row));
        // An open call has the wheel, the way it has the keyboard.
        if let Some(open) = &mut self.opened {
            match m.kind {
                MouseEventKind::ScrollUp => open.scroll = open.scroll.saturating_sub(3),
                MouseEventKind::ScrollDown => open.scroll = open.scroll.saturating_add(3),
                _ => {}
            }
            return;
        }
        match m.kind {
            MouseEventKind::ScrollUp => self.scroll_by(-3),
            MouseEventKind::ScrollDown => self.scroll_by(3),
            MouseEventKind::Down(MouseButton::Left) => {
                // A chip under the pointer goes off, which is the "remove it in a
                // second" the whole thing is for. Read off `field_rows`, the
                // frame's own numbers, so the column a click lands in is the
                // column that was drawn.
                if let Some(n) = self.chip_at(m.column, m.row) {
                    self.detach(n);
                    return;
                }
                let now = Instant::now();
                let twice = matches!(self.clicked, Some((x, y, at)) if x == m.column && y == m.row && now.duration_since(at) < DOUBLE_CLICK);
                self.clicked = Some((m.column, m.row, now));
                if twice {
                    if let Some((row, head)) = self.anchor_at(m.row) {
                        // A call opens, a thought unfolds, a step folds: the click
                        // does the useful thing for what it landed on. The step's own
                        // first row is the step -- that is the line that folds it --
                        // and a thought under it is the thought.
                        if matches!(self.log.rows.get(row), Some(Row::Tool { .. })) {
                            self.open(row);
                        } else if row != head {
                            self.toggle_thought(row);
                        } else {
                            self.toggle_step(head);
                        }
                    }
                }
            }
            _ => {}
        }
    }

    /// The shot under a screen position, if the pointer is on a chip.
    ///
    /// The field's own geometry as the last frame drew it: which screen row each
    /// of its lines landed on, where its first column is, and which chip each drawn
    /// column belongs to. A column `LEAD` in from the field's own first column is the
    /// prompt, so the tag comes off that much.
    fn chip_at(&self, column: u16, row: u16) -> Option<usize> {
        let (_, _, styles) = self
            .field_rows
            .iter()
            .find(|(r, _, _)| self.field_top + r == row)?;
        let at = column.saturating_sub(self.field_left + LEAD as u16);
        let tag = *styles.get(at as usize)?;
        (tag != 0).then(|| tag as usize - 1)
    }

    /// The keys a panel answers to, whether it is a call that was opened or a
    /// question the worker is blocked on.
    fn scroll_opened(&mut self, code: KeyCode) {
        let Some(open) = &mut self.opened else { return };
        match code {
            KeyCode::Up => open.scroll = open.scroll.saturating_sub(1),
            KeyCode::Down => open.scroll = open.scroll.saturating_add(1),
            KeyCode::PageUp => open.scroll = open.scroll.saturating_sub(10),
            KeyCode::PageDown => open.scroll = open.scroll.saturating_add(10),
            KeyCode::Home => open.scroll = 0,
            _ => {}
        }
    }

    /// Stop the round in flight. The worker ends the run and says so; nothing here
    /// pretends it stopped by itself.
    fn cancel(&mut self) {
        if let Some(sess) = &mut self.sess {
            if let Err(e) = sess.cancel() {
                self.say(e);
            }
        }
    }

    /// One key. Returns false to leave.
    fn key(&mut self, k: KeyEvent) -> bool {
        // AltGr arrives as ctrl+alt, and the character the layout made is in
        // the event: `AltGr+Q` on a German keyboard is a `@` that reads as both
        // modifiers. Both are spent on it here, or every ctrl and alt binding
        // below eats a character that was typed.
        let gr = altgr(k);
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL) && !gr;
        let alt = k.modifiers.contains(KeyModifiers::ALT) && !gr;
        let shift = k.modifiers.contains(KeyModifiers::SHIFT);
        let cmd = k.modifiers.contains(KeyModifiers::SUPER);
        // Any key at all is someone at the keyboard, and the clock a held prompt
        // waits on is this one: 3.5 seconds after the last of them, not before.
        self.since_key = Instant::now();

        // What this key is, if it is one of the screen's own. A character AltGr
        // made is never a chord: the ctrl+alt that reached it is how that character
        // was typed, not a request for a binding.
        let action = if gr { None } else { self.keys.action(k.code, k.modifiers) };

        // Leaving is available whatever else has the keyboard, so it is asked here
        // rather than below, with the rest of the bindings.
        if action == Some(Action::Quit) {
            return false;
        }

        // A question the worker is blocked on is answered before anything else: it
        // is the thing that cannot wait, and the panel beside it is only what the
        // answer needs.
        if let Some(mut asked) = self.prompt.clone() {
            // A question with options gets a panel and the keyboard: arrows move,
            // enter takes what is under them, a key that is not navigation starts
            // typing a custom answer instead. Picking from a list is the shortcut;
            // nothing about it should stop you from saying what you actually mean.
            //
            // A permission keeps the y/n it always had. It is binary, it is urgent,
            // and one keystroke is the right amount of ceremony for it.
            let reply = if asked.is_question() {
                match k.code {
                    // Arrows only move among real choices. A question with no
                    // options has no list, and swallowing up/down there just
                    // removes keys from a text field for no reason.
                    KeyCode::Up if asked.has_choices() && asked.typed.is_empty() => {
                        asked.picked = asked.picked.saturating_sub(1);
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Down if asked.has_choices() && asked.typed.is_empty() => {
                        asked.picked = (asked.picked + 1).min(asked.rows() - 1);
                        self.prompt = Some(asked);
                        return true;
                    }
                    // A new line in the answer, in the spellings a terminal
                    // actually sends: shift-enter where it reports a shifted key,
                    // alt-enter where it maps the key to `1b 0d`, and ctrl-j
                    // everywhere, since raw mode makes 0x0A a key of its own.
                    // Enter is the answer, so a break is only ever a modified
                    // form -- and it is what makes a two-paragraph answer one.
                    KeyCode::Enter if shift || alt => {
                        if !asked.on_custom() {
                            asked.picked = asked.options.len();
                        }
                        asked.typed.push('\n');
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Char('j') if ctrl => {
                        if !asked.on_custom() {
                            asked.picked = asked.options.len();
                        }
                        asked.typed.push('\n');
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Enter => {
                        if asked.on_custom() {
                            // The typed answer, or nothing worth sending.
                            let said = asked.typed.trim().to_string();
                            if said.is_empty() { return true; }
                            Some(serde_json::json!({ "answer": said }))
                        } else {
                            Some(serde_json::json!({ "choice": asked.options[asked.picked] }))
                        }
                    }
                    KeyCode::Esc if !asked.typed.is_empty() => {
                        // Escape backs out of what was typed before it backs out of
                        // the question: the first thing escape means is "undo that",
                        // the second is "never mind".
                        asked.typed.clear();
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Esc => Some(serde_json::json!({ "answer": "" })),
                    KeyCode::Backspace => {
                        asked.typed.pop();
                        self.prompt = Some(asked);
                        return true;
                    }
                    // A digit picks that option straight away, but only while the
                    // list is what has the cursor: once someone is typing an answer
                    // that happens to contain a number, the number is text.
                    KeyCode::Char(c) if c.is_ascii_digit() && asked.typed.is_empty()
                        && asked.has_choices() && !asked.on_custom() =>
                    {
                        let i = c.to_digit(10).unwrap_or(0) as usize;
                        if i >= 1 && i <= asked.options.len() {
                            self.prompt = Some(Ask { picked: i - 1, ..asked });
                            return true;
                        }
                        return true;
                    }
                    KeyCode::Char(c) if !ctrl && !alt && !cmd => {
                        // Any other character is the first character of a custom
                        // answer, and from then on this is a text field.
                        if !asked.on_custom() {
                            asked.picked = asked.options.len();
                        }
                        asked.typed.push(c);
                        self.prompt = Some(asked);
                        return true;
                    }
                    code => {
                        self.scroll_opened(code);
                        return true;
                    }
                }
            } else {
                // The same list a question gets, with two answers and no free text:
                // arrows move, a digit picks, enter takes what is under them. y and n
                // stay because they are the muscle memory this had before it was a
                // panel, and nothing about them is wrong.
                match k.code {
                    KeyCode::Up => {
                        asked.picked = 0;
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Down => {
                        asked.picked = 1;
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Char(c @ ('1' | '2')) if !ctrl && !alt => {
                        asked.picked = usize::from(c == '2');
                        self.prompt = Some(asked);
                        return true;
                    }
                    // The document behind the rows, the same keys a call's panel takes:
                    // a diff cut off at the pane is the one thing a person deciding on a
                    // write has to be able to reach.
                    KeyCode::PageUp => {
                        asked.scroll = asked.scroll.saturating_sub(10);
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::PageDown => {
                        asked.scroll = asked.scroll.saturating_add(10);
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Home => {
                        asked.scroll = 0;
                        self.prompt = Some(asked);
                        return true;
                    }
                    KeyCode::Enter => Some(serde_json::json!({ "allow": asked.picked == 0 })),
                    KeyCode::Char('y') => Some(serde_json::json!({ "allow": true })),
                    KeyCode::Char('n') | KeyCode::Esc => Some(serde_json::json!({ "allow": false })),
                    code => {
                        self.scroll_opened(code);
                        return true;
                    }
                }
            };
            if let (Some(sess), Some(body)) = (&mut self.sess, reply) {
                let _ = sess.reply(&asked.id, "permission", body);
            }
            // A question is said in the conversation's own voice, because it is gone
            // from the screen the moment this returns and something has to remain. A
            // permission is not: the call's own row is already on the screen and the
            // result lands against it, so `· allowed file` under it was the same fact
            // twice, and in the one row of this transcript that has no mark to carry.
            if asked.is_question() {
                let picked = if asked.on_custom() {
                    asked.typed.trim().to_string()
                } else {
                    asked.options.get(asked.picked).cloned().unwrap_or_default()
                };
                self.say(format!("{} {}", asked.question, picked).trim().to_string());
            }
            // Both answers land here, which is why the mark comes down here too:
            // every way out of the prompt takes it down.
            self.mark = Some(answered());
            self.prompt = None;
            self.opened = None;
            return true;
        }

        // An open panel takes what is left: it is what the eye is on, and escape is
        // what closes it.
        if self.opened.is_some() {
            match k.code {
                KeyCode::Esc | KeyCode::Char('q') => self.opened = None,
                code => self.scroll_opened(code),
            }
            return true;
        }

        // A menu owns the keyboard while it is up: what is typed filters it and
        // escape takes back what it wrote.
        if self.menu.is_some() {
            return self.menu_key(k);
        }

        // Everything below is the keymap's, and everything above is not: an open
        // panel, a picker and the `/` menu answer their own keys, and no file can
        // take the key that answers a permission prompt away from the panel that
        // asks one. This is the boundary, and it is the only one.
        //
        // Two things below stay compiled in because they are not bindings: a
        // newline inside a paste on Windows, and a character no chord names.
        #[cfg(target_os = "windows")]
        if self.paste_burst && k.code == KeyCode::Enter {
            self.insert('\n');
            return true;
        }
        match action {
            Some(action) => self.perform(action, k.code),
            // A character nobody bound is a character. Nothing else gets this far:
            // every other key is a binding above, or means nothing at all.
            None => {
                if let KeyCode::Char(c) = k.code {
                    if !ctrl {
                        self.insert(c);
                    }
                }
            }
        }
        true
    }

    /// Run one of the keymap's actions.
    ///
    /// Every body here is the arm it was when the keys were matched by hand, with
    /// the comment that explains it: what changed is who decides that a key means
    /// them. `code` is there for the one action whose character is part of it.
    fn perform(&mut self, action: Action, code: KeyCode) {
        match action {
            // The stop. Escape is what anyone reaches for, so escape is the stop,
            // and with nothing to stop it does the other thing escape means: it
            // empties what has been typed. ctrl-k is not a second spelling of it:
            // that is readline's kill-to-end, and it is bound as one below.
            Action::Stop => {
                if self.writing().is_some() {
                    self.cancel();
                } else {
                    self.hold();
                    self.input.clear();
                    self.shots.clear();
                    self.caret = 0;
                }
            }
            // Every step at once, which is the whole point of the key: one press
            // and the calls are all there. Any step the reader unfolded or folded
            // by hand is forgotten, and so is every thought they opened, so this
            // really does mean all of them. A call is not folded -- what it shows,
            // it always shows -- so this key leaves a column of calls exactly as it
            // found it.
            Action::Fold => {
                self.open = !self.open;
                self.overrides.clear();
                self.thoughts.clear();
            }
            // The calls, listed: a keyboard's answer to a mouse's double click,
            // and a way to reach one that is not the newest.
            Action::Calls => self.open_calls(),
            // The machine this conversation runs on. The only key that starts a
            // different conversation rather than talking to this one.
            Action::Host => self.pick_hosts(),
            // Let go of the box: the worker there lives in a pane of its own, so this
            // ends the view and leaves the work running. Nothing is sent to it.
            Action::Detach => self.detach_box(),
            // The worker already knows how to push a call into the background,
            // and discards this when nothing is in flight.
            Action::Background => {
                if let Some(sess) = &mut self.sess {
                    if let Err(e) = sess.background() {
                        self.say(e);
                    }
                }
            }
            // The clipboard. Not through the terminal: OSC 52 carries text and
            // most terminals will not answer a read of it at all, and the
            // terminal's own paste is *its* idea of the clipboard, which for an
            // image is nothing. The clipboard is the OS's, so the OS is asked.
            Action::Paste => match attach::clipboard() {
                Ok(image) => self.attach(image),
                // Most of the time the clipboard holds something that is not a
                // picture, which is not a failure and does not want a row
                // saying so on every ctrl-v of a word.
                Err(_) => {}
            },
            // A new line in the message. Which spelling arrives is the terminal's
            // business rather than the key's: one that reports shifted keys sends
            // shift-enter as itself, one that has the key mapped to a sequence sends
            // the alt-enter bytes for it, and raw mode makes 0x0A a key of its own,
            // so ctrl-j is a new line wherever it is pressed. Three spellings, one
            // action, and the map is where they are listed together.
            Action::Newline => self.insert('\n'),
            Action::Send => self.send(),
            Action::Backspace => self.backspace(),
            Action::WordBack => self.word_backspace(),
            // Delete deletes forward everywhere, which is the same in every
            // terminal. The two modified forms are the word and the line: on
            // Windows and Linux ctrl is the word, and on a mac cmd is the line
            // and the option key is the word. Both spellings are taken on every
            // platform, so a terminal that reports only one of them works, and
            // nothing here reads the OS.
            Action::Delete => self.delete(),
            Action::WordDelete => self.word_delete(),
            Action::KillStart => self.kill_to_line_start(),
            Action::LineStart => self.line_home(),
            Action::KillEnd => self.kill_to_line_end(),
            Action::LineEnd => self.to_line_end(),
            // Put back what the last deletion took: readline's yank, and the key
            // that answers a cmd-backspace or an escape pressed by accident.
            Action::Undo => self.put_back(),
            // The moves a mac terminal does not send as keys at all.
            //
            // Ghostty (which cmux is) ships "natural text editing" on by
            // default, and its own source says why: it "forces these keys to go
            // back to legacy encoding". So `cmd+←` arrives as ctrl-A, `cmd+→` as
            // ctrl-E, `cmd+backspace` as ctrl-U, `alt+←` as alt-B and `alt+→` as
            // alt-F, before any program sees them. Nothing binds `cmd+delete`,
            // which is why that one arrives as itself. A terminal that does send
            // the key sends SUPER instead -- crossterm reads that out of xterm's
            // `CSI 1;9D` and kitty's `CSI 1;9u` alike -- and a terminal that sends
            // neither has ctrl and alt with the arrows. Every spelling is bound,
            // and none of it asks what OS this is.
            Action::WordLeft => self.word_left(),
            Action::WordRight => self.word_right(),
            Action::Left => self.left(),
            Action::Right => self.right(),
            // Home and end are the ends of the line being typed whenever there
            // is a line to end, and the ends of the conversation when the box is
            // empty. Not "when the message is more than one line": typing a
            // single line is the common case, and these are the line's ends on
            // every platform, where the command key is only the mac spelling of
            // the same thing.
            Action::Home => {
                if !self.input.is_empty() {
                    self.line_home();
                } else {
                    self.follow = false;
                    self.scroll = 0;
                }
            }
            Action::End => {
                if !self.input.is_empty() {
                    self.to_line_end();
                } else {
                    self.follow = true;
                }
            }
            // Up and down are the message's lines while there is more than one
            // of them, and the conversation's when there is not.
            Action::Up => {
                if self.multiline() {
                    self.up_line();
                } else {
                    self.scroll_by(-1);
                }
            }
            Action::Down => {
                if self.multiline() {
                    self.down_line();
                } else {
                    self.scroll_by(1);
                }
            }
            Action::PageUp => self.scroll_by(-10),
            Action::PageDown => self.scroll_by(10),
            // A trigger at the start of a word opens its menu; anywhere else it
            // is a character, so `src/main.rs` and `user@example.com` are text.
            // A replay has no worker to ask, so it has no menus either. The
            // character is the whole of the trigger, so a rebind moves the key and
            // the menu is still the one that character means.
            Action::Trigger => {
                if let KeyCode::Char(c) = code {
                    let opens = self.sess.is_some() && self.at_word_start();
                    self.insert(c);
                    if let (true, Some(kind)) = (opens, Kind::of(c)) {
                        self.open_menu(kind);
                    }
                }
            }
            // Asked for before the panels, in `key`: leaving is available whatever
            // else has the keyboard, which is why it is not decided here.
            Action::Quit => {}
        }
    }

    /// Whether the open menu is a view of the line rather than a question beside
    /// it: true for a trigger's menu, false for a picker.
    fn on_line(&self) -> bool {
        self.menu.as_ref().is_some_and(|open| open.on_line())
    }

    /// Whether the caret sits at the start of a word, which is the only place a
    /// trigger opens a menu. Only at the end of the message, too: a menu filters
    /// on the tail of the line, and there is no tail to filter on from inside it.
    fn at_word_start(&self) -> bool {
        self.caret == self.chars()
            && self.input.chars().last().map_or(true, char::is_whitespace)
    }

    /// Put a menu up for the trigger that was just typed, and ask for the rows
    /// it needs unless they are already in hand.
    fn open_menu(&mut self, kind: Kind) {
        // The trigger is already on the line, and it is the last character.
        let anchor = self.caret.saturating_sub(1);
        let mut open = Menu::open(kind, anchor);
        let held = match kind {
            Kind::Commands => {
                open.fill(menu::with_local(self.commands.clone()));
                !self.commands.is_empty()
            }
            Kind::Skills => {
                open.fill(self.skills.clone());
                !self.skills.is_empty()
            }
            // A directory is a walk and never held: the tree moves under it.
            // A picker is opened fresh every time for the same reason: what it
            // marks as acting now is what changed. The calls are the frontend's
            // own rows, so they are filled as they are read.
            Kind::Calls => {
                open.fill(menu::calls(&self.log.rows));
                true
            }
            _ => false,
        };
        self.menu = Some(open);
        if !held {
            self.ask_for_rows();
        }
    }

    /// Ask the worker for what the open menu needs, when it does not have it.
    ///
    /// A file filter is a path, so a filter that walks into a subdirectory is a
    /// different question and only a changed directory is asked again: typing
    /// `src/` costs one request rather than one per keystroke.
    fn ask_for_rows(&mut self) {
        let (Some(sess), Some(open)) = (&mut self.sess, &mut self.menu) else { return };
        let ask = match open.kind {
            Kind::Commands => ("command.list", serde_json::json!({})),
            Kind::Skills => ("skill.list", serde_json::json!({})),
            // Asked once per opening and never again while the menu is up: a
            // list of providers is a network question, and a keystroke is no
            // reason to ask it twice. Held between openings either, because
            // what these two mark is what changed.
            Kind::Providers | Kind::Models => {
                if !open.is_empty() {
                    return;
                }
                if open.kind == Kind::Providers {
                    ("provider.list", serde_json::json!({}))
                } else {
                    // No name: the worker lists for the provider it is on and
                    // says which one that was in the reply.
                    ("model.list", serde_json::json!({}))
                }
            }
            // Asked for by the launch, once, before any of this, and asked by
            // name when a list is not in hand yet.
            Kind::Sessions => {
                if !open.is_empty() {
                    return;
                }
                ("session.list", serde_json::json!({}))
            }
            // Handed to the menu when it opens, not asked for: the four modes
            // are this frontend's own list, the calls are read off the
            // transcript it already holds, and the machines are a file it read at
            // launch. A worker cannot speak for a machine it has never met.
            Kind::Permissions | Kind::Calls | Kind::Hosts => return,
            Kind::Files | Kind::Dirs => {
                let (dir, _) = open.walked();
                let dir = dir.to_string();
                if open.dir.as_deref() == Some(dir.as_str()) {
                    return;
                }
                open.dir = Some(dir.clone());
                // The rows on hand are the last directory's, and a name that
                // happens to match in both would be the wrong file.
                open.fill(Vec::new());
                if open.kind == Kind::Dirs {
                    // Anywhere on this machine, and only directories: the
                    // answer to `/cwd` is a directory, never a file.
                    ("dir.list", serde_json::json!({ "dir": dir }))
                } else {
                    ("fs.list", serde_json::json!({ "dir": dir }))
                }
            }
        };
        let _ = sess.command(ask.0, ask.1);
    }

    /// One key while a menu is up.
    fn menu_key(&mut self, k: KeyEvent) -> bool {
        // AltGr reads as ctrl+alt on Windows and the character it made is in the
        // event, so a filter is filtered with it rather than losing it.
        let gr = altgr(k);
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL) && !gr;
        match k.code {
            // Escape takes back the trigger and the filter both. A menu that
            // left its own characters behind would have edited a message the
            // user never asked it to touch.
            KeyCode::Esc => {
                if let Some(open) = &self.menu {
                    // A trigger's own characters come back off the line. A
                    // picker never put any there, so escape at one is only the
                    // question closing -- and the new session stays.
                    if open.on_line() {
                        self.input = self.input.chars().take(open.anchor).collect();
                        self.caret = open.anchor;
                    }
                }
                self.menu = None;
            }
            KeyCode::Up => {
                if let Some(open) = &mut self.menu {
                    open.move_sel(-1);
                }
            }
            KeyCode::Down => {
                if let Some(open) = &mut self.menu {
                    open.move_sel(1);
                }
            }
            KeyCode::Enter | KeyCode::Tab => self.choose(),
            // The filter is the tail of the line, so one character comes off
            // both. Taking the trigger itself off is what closes the menu.
            KeyCode::Backspace => {
                let (line, empty) = match &self.menu {
                    Some(open) => (open.on_line(), open.filter.is_empty()),
                    None => return true,
                };
                if empty {
                    // Nothing left to take back. On the line that character is
                    // the trigger, so the menu goes with it; at a picker there
                    // was no trigger and the question stays.
                    if line {
                        self.backspace();
                        self.menu = None;
                    }
                    return true;
                }
                // The filter is the tail of the line, so one character comes off
                // both of them.
                if line {
                    self.backspace();
                }
                if let Some(open) = &mut self.menu {
                    open.filter.pop();
                    open.refilter();
                }
                self.ask_for_rows();
            }
            KeyCode::Char(c) if c.is_whitespace() && self.on_line() => {
                // A token ends at a space, and a menu is about one token. So the
                // space is not a filter that matches nothing: it ends the word
                // the trigger opened, the menu goes, and the space is typed. That
                // is what makes "something / something else" a sentence -- and
                // it is the same reading the worker's parser takes of it.
                //
                // The one space that opens a menu instead: `/cwd ` is followed
                // by a path, and a path is a place on a disk. The list starts at
                // the directory the conversation stands in, so the answer to
                // "where am I" and the answer to "where instead" are the same
                // menu.
                let wants_dirs = self.input.trim_end() == "/cwd";
                self.menu = None;
                self.insert(c);
                if wants_dirs {
                    self.open_cwd_picker();
                }
            }
            KeyCode::Char(c) if !ctrl => {
                // Typed after a trigger, the character is on the line as well as
                // in the filter: they are the same text. A picker's filter is its
                // own and the line is not touched.
                if self.menu.as_ref().is_some_and(|open| open.on_line()) {
                    let at = self.caret_byte();
                    self.input.insert(at, c);
                    self.caret += 1;
                }
                if let Some(open) = &mut self.menu {
                    open.filter.push(c);
                    open.refilter();
                }
                self.ask_for_rows();
            }
            _ => {}
        }
        true
    }

    /// Put the chosen row on the line or send it, whichever the row is for.
    ///
    /// The trigger and the filter go either way, so what is left is the thing
    /// itself -- and for a picker, nothing at all.
    fn choose(&mut self) {
        let Some((kind, anchor, item)) = self.menu.as_ref().and_then(|open| {
            open.chosen().map(|item| (open.kind, open.anchor, item.clone()))
        }) else {
            // Nothing under the cursor. At a trigger's menu that is a word the
            // list does not know, which the worker reads as prose and sends -- so
            // sending it is the only thing enter can mean. A picker opened by a
            // command has no line of its own, and enter there stays what it was.
            if self.on_line() {
                self.menu = None;
                self.send();
            }
            return;
        };
        let value = item.value.clone();
        match kind {
            // A picker is a switch rather than a word: nothing about it goes on
            // the line, and the line is left as it was.
            Kind::Providers => {
                self.menu = None;
                self.act("provider.use", serde_json::json!({ "name": value }));
            }
            Kind::Models => {
                let provider =
                    self.menu.as_ref().map(|open| open.provider.clone()).unwrap_or_default();
                self.menu = None;
                self.act("provider.set", serde_json::json!({ "name": provider, "model": value }));
            }
            // Carrying on with an older conversation. The worker replays it and
            // the screen fills in from that, so nothing goes on the line.
            Kind::Sessions => {
                // A path typed into the picker is the directory the new conversation
                // stands in, so it is read before the menu goes: it is the row's own
                // value as far as the person is concerned, not a session id.
                let typed = self
                    .menu
                    .as_ref()
                    .map(|open| open.filter.trim().to_string())
                    .unwrap_or_default();
                self.menu = None;
                if value == menu::NEW_IN {
                    self.act("session.cwd", serde_json::json!({ "path": typed }));
                    self.start_new_session();
                } else if value.is_empty() {
                    self.start_new_session();
                } else {
                    self.switch(&value);
                }
            }
            // A picker is a switch, so nothing about it goes on the line.
            Kind::Permissions => {
                self.menu = None;
                self.act("permission.mode", serde_json::json!({ "mode": value }));
            }
            // The one picker that replaces the worker instead of talking to it.
            // An empty value is this machine, which is where a screen starts.
            Kind::Hosts => {
                self.menu = None;
                let here = self.host.as_ref().map(|h| h.name.as_str());
                if here == Some(value.as_str()) || (value.is_empty() && here.is_none()) {
                    return;
                }
                let next = self.hosts.iter().find(|h| h.name == value).cloned();
                self.switch_host(next);
            }
            // A call is opened rather than written: the row's index is what the
            // worker's own record of it is asked for by, and what the panel is
            // built from until that lands.
            Kind::Calls => {
                self.menu = None;
                if let Ok(i) = value.parse::<usize>() {
                    // A call opens its panel. A thought opens the step it belongs to
                    // first -- the line it folds to is only drawn once the step is
                    // open -- and then the thought's own text.
                    match self.log.rows.get(i) {
                        Some(Row::Thinking { .. }) => {
                            let start = view::step_start(&self.log.rows, i);
                            if !self.shows(start, &self.log.rows[start]) {
                                self.overrides.insert(start, true);
                            }
                            self.toggle_thought(i);
                        }
                        _ => self.open(i),
                    }
                }
            }
            // A path is written, not switched to: the worker reads `/cwd <path>`
            // off the finished line, so choosing a directory only writes it. One
            // with a slash on the end is a directory to walk into, and the list
            // stays up at it -- the row that finishes is the one without.
            Kind::Dirs => {
                let keep: String = self.input.chars().take(anchor).collect();
                // A path with a space in it is one word only when it is quoted, and the
                // worker's parser is what has to read it back. The row that walks in keeps
                // its slash and no quotes: everything after the space is then the whole
                // path, which is what the list is asked for.
                let written = if value.ends_with('/') || !value.contains(char::is_whitespace) {
                    value.to_string()
                } else {
                    format!("\"{value}\"")
                };
                self.input = format!("{keep}{written}");
                self.caret = self.chars();
                self.menu = None;
                if written.ends_with('/') {
                    self.open_cwd_picker();
                }
            }
            _ => {
                let keep: String = self.input.chars().take(anchor).collect();
                self.input = format!("{keep}{value} ");
                self.caret = self.chars();
                self.menu = None;
            }
        }
    }

    /// Open one of the lists a command reaches rather than a trigger. It is a
    /// picker, so nothing is written to the line until a row is chosen.
    ///
    /// The permission modes are the one list filled here instead of asked for: the
    /// worker holds the mode in use, not a table of them, so a list of the four is
    /// this frontend's own and there is nothing to send for it.
    fn pick(&mut self, kind: Kind) {
        if kind == Kind::Permissions {
            let now = self.sess.as_ref().map(|s| s.snap.mode.clone()).unwrap_or_default();
            let mut open = Menu::open_apart(kind);
            open.fill(menu::modes(&now));
            self.menu = Some(open);
            return;
        }
        self.menu = Some(Menu::open_apart(kind));
        self.ask_for_rows();
    }

    /// The machines a conversation can run on, listed rather than typed. Filled
    /// here because the list is a file rather than the worker's business: a worker
    /// on this machine cannot speak for the ones it has never met.
    fn pick_hosts(&mut self) {
        let now = self.host.as_ref().map(|h| h.name.clone());
        let mut open = Menu::open_apart(Kind::Hosts);
        open.fill(menu::hosts(&self.hosts, now.as_deref()));
        self.menu = Some(open);
    }

    /// The list `/cwd`'s argument is picked from.
    ///
    /// A trigger menu rather than a picker: it is a view of the path typed after
    /// the word, so choosing a row writes onto the line and enter still sends it.
    /// The anchor is the character after the space, which makes the filter the
    /// path itself. Starts at the directory the conversation stands in -- the
    /// worker answers an empty filter with its own -- so the list a person opens
    /// to ask where they are is the list they would open to move.
    fn open_cwd_picker(&mut self) {
        let seen: Vec<char> = self.input.chars().take(self.caret).collect();
        let Some(space) = seen.iter().rposition(|c| c.is_whitespace()) else { return };
        let anchor = space + 1;
        let mut open = Menu::open(Kind::Dirs, anchor);
        open.filter = seen[anchor..].iter().collect();
        self.menu = Some(open);
        self.ask_for_rows();
    }

    /// The directory a new conversation would stand in, in words a row can carry.
    ///
    /// The machine's own entry when the worker is on a box, because that is where the
    /// worker was started and nothing here can change it; the launch's directory on
    /// this machine, which is the same fact the terminal envelope carries.
    fn new_session_where(&self) -> String {
        match &self.host {
            Some(h) => h.cwd().to_string(),
            None => session::working_dir(),
        }
    }

    /// A conversation that is not on that disk yet.
    ///
    /// The worker creates its folder with the first record, so the screen behind the
    /// picker is empty and stays empty until something is said, and nothing is written
    /// for a session opened and left alone.
    fn start_new_session(&mut self) {
        self.enter();
        self.act("session.new", serde_json::json!({}));
    }

    /// Move this screen onto another conversation, leaving the one it was showing the
    /// way the worker leaves it.
    ///
    /// Three cases, and the screen must not blur them:
    ///
    /// - **a round in flight is refused**, in one row saying why. A local worker is
    ///   this process's child, so taking the conversation out from under a run leaves
    ///   it writing into something nobody is looking at; a box's round is not this
    ///   screen's to take away either, since the work there never depended on it;
    /// - **a local worker, idle**: `session.load` commits the conversation it held,
    ///   drops it if nothing was ever said in it, and opens the other one. The child
    ///   stays -- there is nothing to detach from this process, and nothing to kill;
    /// - **a hosted worker on a box**: the same command typed into the pane that owns
    ///   it. The worker over there holds one conversation at a time, so this is the
    ///   whole of the move, and nothing on that box is stopped.
    ///
    /// The screen becomes the other conversation when the worker says it has it
    /// (`feed`'s `status {state:"session"}`), not when the row is chosen. A load that
    /// failed answers `error` instead, and a screen that had already emptied itself
    /// would have lost the conversation it was showing for nothing.
    fn switch(&mut self, id: &str) {
        let Some(sess) = &self.sess else { return };
        // The conversation already on screen is not a move to make: reopening it would
        // replay it over itself.
        if sess.snap.session_id == id {
            return;
        }
        if let Some(why) = switch_refusal(Some(&sess.snap)) {
            self.say(why.to_string());
            return;
        }
        self.act("session.load", serde_json::json!({ "id": id }));
    }

    /// The screen as a view of one conversation rather than of words in general: what
    /// is left of it when the conversation behind it is a different one.
    ///
    /// The draft, its pictures and the fold preference stay. A half-typed message is
    /// not part of the conversation being left, and whether the steps are open is how
    /// the reader reads rather than what they were reading.
    fn enter(&mut self) {
        self.log = Transcript::default();
        self.overrides.clear();
        self.thoughts.clear();
        self.figures.clear();
        self.spans.clear();
        self.anchors.clear();
        self.scroll = 0;
        self.follow = true;
        self.turn = None;
        self.opened = None;
        self.prompt = None;
        self.pending = None;
        self.menu = None;
        if let Some(sess) = &mut self.sess {
            session::rehomed(&mut sess.snap);
        }
    }

    /// Stop watching the box, and leave it working. `ctrl-d`.
    ///
    /// The worker over there is a child of its own tmux pane, so letting go of this
    /// session ends the view and nothing else: a turn in flight keeps running, the laptop
    /// can be shut, and ctrl-g brings the conversation back to where it got to. That is
    /// the whole of what detach means here - it is not a run sent away, it is this screen
    /// no longer being attached to work that never depended on it.
    fn detach_box(&mut self) {
        let Some(host) = self.host.clone() else {
            self.say(
                "detach is for a box: this conversation's worker lives in this terminal, so \
                 closing it ends the work"
                    .to_string(),
            );
            return;
        };
        // Dropping the session is the whole of it: `Worker::close` on a hosted worker kills
        // the tail and never sends `stop`, so nothing on that box hears about this.
        //
        // The machine goes with it. `host` is what the picker reads as "already on this
        // one" and what the next command is addressed to, and a screen attached to nothing
        // is on nothing: left set, ctrl-g could not pick the box it had just left, which is
        // the one thing the line below promises.
        self.sess = None;
        self.host = None;
        self.log = Transcript::default();
        self.menu = None;
        self.follow = true;
        self.scroll = 0;
        self.say(format!(
            "detached: {0} carries on, this screen is not attached to it - ctrl-g to come back",
            host.name
        ));
    }

    /// Move this screen onto another machine.
    ///
    /// The conversation is replaced rather than carried over: a session belongs to
    /// the worker that wrote it, so this starts a new one over there and asks that
    /// worker for its own list, which is the list the picker then opens. Nothing
    /// about the old screen survives except the state root and the hosts, which are
    /// the launch's.
    ///
    /// ponytail: a host that is down is not detected here. `ssh` is started, and a
    /// box that never answers costs `ConnectTimeout=10` and then the worker's own
    /// exit row with ssh's message on it -- the same path as any worker that dies,
    /// with no retry and no falling back to this machine, because silently running
    /// somewhere else is worse than saying nothing is running.
    ///
    /// Nothing is said on success either: spawning `ssh` is not the worker being
    /// there, and a row saying it had moved sat above ssh's own refusal when the
    /// box was down, which is two facts and one of them false. The picker marks the
    /// row the screen is on, and the header names the directory the worker reports,
    /// so where it is is drawn by the things that know.
    fn switch_host(&mut self, host: Option<Host>) {
        let home = self.home.clone();
        match App::live_on(host, home.as_deref(), Start::Ask, None) {
            Ok(next) => *self = next,
            Err(e) => self.say(format!("could not start a worker there: {e}")),
        }
    }

    /// Send one command to the worker, saying so on the screen when it will not
    /// go rather than leaving a choice that looks taken.
    fn act(&mut self, action: &str, fields: serde_json::Value) {
        if let Some(sess) = &mut self.sess {
            if let Err(e) = sess.command(action, fields) {
                self.say(e);
            }
        }
    }
}

/// How far the highlight travelling over a row has got, or None when nothing on the
/// screen is moving.
///
/// Two reasons for it to be on, and one clock for both. While the worker is working --
/// or while a peer's message is sitting in its mailbox waiting for the round to end,
/// which is a wait the round has not reached yet -- the sweep runs for as long as that
/// lasts, which is the whole of a round rather than the second after a row landed. The
/// arrival window is on top of that, so a peer message that lands on an idle screen
/// still gets its own second's glance, which is what catches the eye when nothing else
/// is moving.
///
/// Spaced off the screen's own clock rather than off a frame count: any event makes the
/// loop come round early, and a highlight that moved once per frame would race as soon
/// as the mouse moved. A free function rather than a method so the two rules can be
/// checked without a worker to start.
fn shimmer_phase(snap: Option<&Snapshot>, peer_age: Option<Duration>, ms: u128) -> Option<usize> {
    let alive = snap.is_some_and(|s| s.state.busy() || s.peer_waiting.is_some());
    if alive {
        return Some(ms as usize / view::SHIMMER_STEP_MS as usize);
    }
    let age = peer_age?.as_millis();
    if age > view::SHIMMER_MS {
        return None;
    }
    Some((age / view::SHIMMER_STEP_MS) as usize)
}

/// What the status line says about a peer whose message is waiting: the other agent's
/// own session name, as the worker put it on the knock. A knock the worker named nobody
/// on still says a peer is waiting, rather than inventing a name for one.
fn waiting_words(name: &str) -> String {
    match name.trim() {
        "" => "waiting on a peer".to_string(),
        who => format!("waiting on {who}"),
    }
}

/// How many cells the context gauge is. Six: a figure wide enough to be read as a
/// gauge at a glance, narrow enough that the row still holds the state and the
/// counts beside it.
const GAUGE_CELLS: usize = 6;
/// The gauge's two cells: filled and empty. Two shapes of the same width, so a
/// gauge that fills never moves the row around it.
const GAUGE_FULL: &str = "\u{2588}";
const GAUGE_EMPTY: &str = "\u{2591}";

/// Why a fold is on: the one pause where the worker is blocked on a model the
/// person cannot see, so the words say the word, the size, and what it frees.
fn compacting_words(snap: &Snapshot) -> String {
    let freed = snap.compact_freed;
    match (freed, snap.compact_folded) {
        (f, n) if f > 0 && n > 0 =>
            format!("\u{25c6} compacting: {n} records into a summary, {}k chars freed", f / 1000),
        (f, _) if f > 0 => format!("\u{25c6} compacting: {}k chars freed", f / 1000),
        _ => "\u{25c6} compacting the conversation into a summary".to_string(),
    }
}

/// Two spaces between two facts on the status row: enough that a count and a mode
/// are two things rather than one word.
fn spaced(zone: &mut Vec<Span<'static>>) {
    if !zone.is_empty() {
        zone.push(Span::raw("  "));
    }
}

/// The width of a zone in cells, which is what the right one is pushed against.
fn zone_width(zone: &[Span<'static>]) -> usize {
    Line::from(zone.to_vec()).width()
}

/// Why a conversation cannot be left for another one, or `None` when it can.
///
/// A round in flight is the one thing that stops it. It is read off the screen's own
/// snapshot, so the rule can be checked without a worker behind it -- which is the
/// whole reason this is not a method on `App`.
fn switch_refusal(snap: Option<&Snapshot>) -> Option<&'static str> {
    match snap {
        Some(s) if s.state.busy() => Some("a round is still running, escape stops it"),
        _ => None,
    }
}


/// The context gauge: how much of the window the conversation fills, as a bar and a
/// percentage. Both numbers are the wire's; with no window reported there is no
/// percentage to take, so it says what it does know instead of a fraction of a number
/// nobody sent.
fn context_gauge(snap: &Snapshot) -> Vec<Span<'static>> {
    let mut out: Vec<Span<'static>> = Vec::new();
    if snap.context_max > 0 {
        let percent = (snap.context_used * 100 / snap.context_max).clamp(0, 100) as usize;
        let filled = (percent * GAUGE_CELLS / 100).min(GAUGE_CELLS);
        // A gauge filling up is a state, not decoration: quiet, then warning, then
        // the tone this screen fails in.
        let tone = if percent >= 90 {
            ink::failed()
        } else if percent >= 70 {
            ink::warn()
        } else {
            ink::faint()
        };
        out.push(Span::styled(GAUGE_FULL.repeat(filled), Style::new().fg(tone)));
        // The empty rungs are the hairline this screen draws an edge in, so a gauge at
        // a fifth reads as one lit cell and five cells of track rather than as six of
        // the same thing.
        out.push(Span::styled(
            GAUGE_EMPTY.repeat(GAUGE_CELLS - filled),
            Style::new().fg(ink::rule()),
        ));
        out.push(Span::styled(format!(" {percent}%"), Style::new().fg(ink::faint())));
    } else if snap.context_used > 0 {
        out.push(Span::styled(
            format!("ctx {} (window unknown)", plenty(snap.context_used)),
            Style::new().fg(ink::faint()),
        ));
    }
    out
}

/// The rate the worker reported, when it reported one: a round that has said nothing
/// about how fast it is going says nothing about it.
fn token_rate(snap: &Snapshot) -> Vec<Span<'static>> {
    let mut out: Vec<Span<'static>> = Vec::new();
    if snap.tps > 0.0 {
        let est = if snap.tps_est { " est" } else { "" };
        out.push(Span::styled(format!("{:.0} tok/s{est}", snap.tps), Style::new().fg(ink::quiet())));
    }
    out
}

/// A hint as this screen writes one: the key in the tone you read by and its words
/// after it, quieter. A key map is read as pairs -- the key is what you are looking
/// for and the words are what you check it against -- so the two are told apart
/// rather than set as one grey line.
fn hint(key: &str, words: &str) -> Vec<Span<'static>> {
    vec![
        Span::styled(key.to_string(), Style::new().fg(ink::text())),
        Span::styled(format!(" {words}"), Style::new().fg(ink::quiet())),
    ]
}

/// Two hints, two cells of air between them: opencode's own spacing for a pair of
/// keys at the end of a bar.
fn hint_pair(first: (&str, &str), second: (&str, &str)) -> Vec<Span<'static>> {
    let mut spans = hint(first.0, first.1);
    spans.push(Span::raw("  "));
    spans.extend(hint(second.0, second.1));
    spans
}

/// The row under the card, as three zones: the dots and what is happening at its left,
/// the conversation's figures in the middle, and the counts, the permission mode and
/// the keys at its right, against the pane's own edge.
///
/// Every figure is one the worker sent, or the one clock this frontend owns -- the
/// round's elapsed seconds, which `view::turn` also draws and which is why it is
/// written to the same tenth. A zone with nothing to say says nothing: an absence is
/// not a zero, and the space it would have taken stays empty.
///
/// A free function over the snapshot, rather than only a method on `App`, so the row
/// can be read in a test without a worker behind it.
fn status_line(snap: &Snapshot, width: usize, ms: u128) -> Line<'static> {
    // The dots and the key that is live beside them are never given up: they are what
    // says a round is running, and how to get out of it.
    let mut lead: Vec<Span<'static>> = scan::dots(&snap.state, ms);
    lead.push(Span::raw(" "));
    let mut head: Vec<Span<'static>> = Vec::new();
    if snap.state.busy() {
        head.extend(hint("esc", "interrupt"));
    }

    // Another agent's message is in the mailbox and the round in flight has not carried
    // it into the model yet. It is not the message and nothing about the conversation
    // changes here; the row says the wait, with the name the worker put on the knock,
    // and a knock from nobody in particular still says a peer is waiting rather than
    // inventing a name for it.
    //
    // A fold is the one pause where the worker is blocked on a model the person cannot
    // see, so the row says what it is doing rather than sitting still for a minute.
    let mut note: Vec<Span<'static>> = Vec::new();
    if let Some(name) = &snap.peer_waiting {
        note.push(Span::styled(waiting_words(name), Style::new().fg(ink::quiet())));
    }
    if !snap.compacting.is_empty() && snap.compacting != "done" {
        if !note.is_empty() {
            spaced(&mut note);
        }
        note.push(Span::styled(compacting_words(snap), Style::new().fg(ink::warn())));
    }
    let mut perms: Vec<Span<'static>> = Vec::new();
    if !snap.mode.is_empty() {
        perms.push(Span::styled("perms ", Style::new().fg(ink::faint())));
        perms.push(Span::styled(snap.mode.clone(), Style::new().fg(ink::text())));
    }
    // The two keys that are true either way, at the end of the line: a round in flight
    // has no enter to send, so its pair is the calls a run can be watched through, and
    // a row of twenty bindings is a key map on the screen rather than an answer to
    // "what do I press now". `/help` is the door to the rest.
    let keys = hint_pair(
        match snap.state.busy() {
            true => ("ctrl+o", "calls"),
            false => ("enter", "send"),
        },
        ("/help", "keys"),
    );

    // A pane too narrow for the whole row gives up the asides before it gives up the
    // keys: the rate, then the gauge, then the note, then the permission mode. The
    // dots, the key beside them and the two at the right end stay, because what to
    // press is what this row is for.
    let mut keep = [true; 2];
    loop {
        let left = joined(&lead, &[head.as_slice()]);
        let right = joined(&[], &[
            if keep[0] { note.as_slice() } else { &[] },
            if keep[1] { perms.as_slice() } else { &[] },
            keys.as_slice(),
        ]);
        if zone_width(&left) + zone_width(&right) + 1 <= width || !keep.iter().any(|k| *k) {
            return row(left, right, width);
        }
        match keep.iter().position(|k| *k) {
            Some(at) => keep[at] = false,
            None => return row(left, right, width),
        }
    }
}

/// A zone out of its parts, two cells of air between them and the empty ones skipped:
/// a run of facts about one line, in the order they are read in.
fn joined(lead: &[Span<'static>], parts: &[&[Span<'static>]]) -> Vec<Span<'static>> {
    let mut out: Vec<Span<'static>> = lead.to_vec();
    for part in parts {
        if part.is_empty() {
            continue;
        }
        spaced(&mut out);
        out.extend(part.iter().cloned());
    }
    out
}

/// The two zones of one row: the left against the pane's left edge and the right against
/// its right, with at least one cell between them however narrow the pane gets. A row
/// that ran the two together read as one broken figure (`8 tok/sperms auto`).
fn row(left: Vec<Span<'static>>, right: Vec<Span<'static>>, width: usize) -> Line<'static> {
    let mut spans = left;
    let gap = width.saturating_sub(zone_width(&spans) + zone_width(&right)).max(1);
    spans.push(Span::raw(" ".repeat(gap)));
    spans.extend(right);
    Line::from(spans)
}

/// One frame while text is arriving: sixty a second, which is where a stream stops
/// reading as a teletype.
///
/// The wait below is not an idle timer. Nothing wakes the loop when the worker
/// speaks -- `event::poll` watches the terminal, and an answer lands in a channel
/// the poll cannot see -- so this number *is* the frame rate of a live answer. It
/// was 60ms for both cases, which is sixteen frames a second, and a provider
/// streams a delta per token: four or five of them arrived per frame and the text
/// came down in lumps 60ms apart.
const STREAM_MS: u64 = 16;
/// How long a last word keeps the fast clock up. An answer pauses between chunks
/// and again between a thought and the call it decided on, and dropping to the
/// slow clock across every gap is what makes one answer look like it keeps
/// restarting.
const STREAM_HOLD: Duration = Duration::from_millis(250);
/// A round in flight with nothing arriving: the scanner and the sweep. The scanner
/// steps every `scan::DOT_STEP_MS`, and a frame that arrives late drops a step, so
/// this is that number: one frame of this screen, one cell of the wire.
const MOVING_MS: u64 = scan::DOT_STEP_MS as u64;
/// A still screen still has to notice a session being written to.
const STILL_MS: u64 = 500;

/// How long the loop waits before coming round again: `idle` is how long since the
/// worker last said anything, `moving` is whether anything is animating on its own.
///
/// Three speeds, and only the first is new. A free function so the rule can be
/// checked without a worker to start, the same way `shimmer_phase` can.
fn frame_wait(idle: Duration, moving: bool) -> u64 {
    if idle < STREAM_HOLD {
        STREAM_MS
    } else if moving {
        MOVING_MS
    } else {
        STILL_MS
    }
}

/// Whether this key is AltGr's character rather than a ctrl or alt binding.
///
/// Windows reports AltGr as ctrl+alt and puts the character the layout made in
/// the same event: `AltGr+Q` is `@` on a German keyboard. So those two
/// modifiers are not asking for a binding there, they are how that character
/// was reached, and spending them on one is how a typed character goes missing.
///
/// The character is what tells the two apart. AltGr's level carries symbols and
/// accented letters -- `@ { [ ] } \ ~` and the currency and letter forms beside
/// them -- and never a plain ASCII letter or digit, which is what a ctrl or alt
/// binding is spelled with. So ctrl+alt+backspace and ctrl+alt+the arrows stay
/// bindings, and `\` typed on a German keyboard lands in the message.
///
/// ponytail: a layout whose AltGr level makes an ASCII letter or digit would be
/// read as a binding instead. None of the ones I know do, and the alternative is
/// asking Windows which layout is on the keyboard on every keystroke.
fn altgr(k: KeyEvent) -> bool {
    k.modifiers.contains(KeyModifiers::CONTROL | KeyModifiers::ALT)
        && matches!(k.code, KeyCode::Char(c) if !c.is_ascii_alphanumeric())
}

fn run(mut app: App) -> io::Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    // The enhanced keyboard protocol is what makes a shifted enter reportable
    // as itself: without it most terminals send the same byte plain enter
    // sends, so shift-enter would send a half-written message. A terminal that
    // does not know the sequence ignores it, and ctrl-j still gets a newline
    // in everywhere. Crossterm's legacy Windows console backend does not
    // implement the sequence, so Windows keeps its native key handling.
    #[cfg(not(target_os = "windows"))]
    execute!(
        stdout,
        event::PushKeyboardEnhancementFlags(event::KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
    )?;
    execute!(
        stdout,
        // Rows fold on a double click, which a terminal only reports if it is
        // asked to. The cost is the terminal's own drag-to-select.
        EnableMouseCapture,
        // Bracketed paste, which is what makes a dropped or pasted picture
        // arrive as one `Paste` event rather than as a burst of keystrokes. A
        // file dragged onto the window is the terminal typing its path, and
        // without this there is no way to tell that from a person typing one:
        // the path would land in the message as text and the picture it names
        // would never be attached.
        EnableBracketedPaste,
        SetBackgroundColor(TermColor::Rgb { r: view::PAGE.0, g: view::PAGE.1, b: view::PAGE.2 })
    )?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    // The console's input mode, taken now and held for as long as this screen is
    // drawn. Anything else on this console can change it -- and with VT input on,
    // the pointer's moves arrive as characters and are typed into the message. See
    // `cuacode::console` for what was measured and why the workers also spawn with
    // no console of this one's; this is the half that does not depend on that
    // having worked everywhere.
    let mut console = cuacode::console::Claim::take();

    // When the worker last put a word on the screen. This is the clock a live answer
    // is drawn against, and it has to be kept here rather than in the screen's state:
    // it is the loop's own pace, not something either frontend draws.
    let mut said: Option<Instant> = None;

    let result = loop {
        // Asked every pass, and it costs one console call: a mode found changed is
        // put back and the queue it already polluted is emptied, before any of it is
        // read as a key. Cheap enough for thirty a second and the only place the
        // drift is actually caught -- nothing tells this process that another one
        // moved the mode.
        console.reassert();
        let arrived = app.pump();
        if arrived {
            said = Some(Instant::now());
        }
        // The permission mark, written straight to the tty through the terminal's
        // own writer: between two frames, never inside one, so it cannot land in a
        // cell ratatui then believes it has already drawn. A terminal that does
        // not know `OSC 9` drops it on the floor.
        if let Some(mark) = app.mark.take() {
            execute!(term.backend_mut(), Print(mark))?;
        }
        // A still screen is redrawn only when something has changed it. A round in
        // flight turns the spinner and the mark, so it is drawn on a clock; nothing
        // in flight is drawn when the worker speaks or a key is pressed, which is
        // the whole of what can change it then.
        let moving = app.animating();
        if arrived || moving {
            term.draw(|f| draw(f, &mut app))?;
        }

        let tick = frame_wait(said.map_or(Duration::MAX, |at| at.elapsed()), moving);
        if event::poll(Duration::from_millis(tick))? {
            match event::read() {
                Ok(CEvent::Key(k)) if k.kind != crossterm::event::KeyEventKind::Release => {
                    // A key with console records behind it is the middle of a
                    // paste, not the end of one. The flag is set before the key
                    // is handled and again after it, so an enter is a newline
                    // when either the enter has more of the paste behind it, or
                    // the key before it did. That second case is the one that
                    // matters: a pasted block ending in a newline has nothing
                    // behind that newline, so without the carry-over the paste
                    // would send itself. Once the queue is empty for two keys in
                    // a row, the next enter sends the message.
                    #[cfg(target_os = "windows")]
                    if winpaste::pending_key_presses() > 0 {
                        app.paste_burst = true;
                    }
                    if !app.key(k) {
                        break Ok(());
                    }
                    #[cfg(target_os = "windows")]
                    {
                        app.paste_burst = winpaste::pending_key_presses() > 0;
                    }
                }
                Ok(CEvent::Mouse(m)) => app.mouse(m),
                // A paste is the second door a picture gets in through: a file
                // dropped on the window arrives as its path, and a paste of
                // several is the same gesture with more than one file. A paste
                // of words is the words.
                Ok(CEvent::Paste(text)) => app.paste(text),
                Ok(CEvent::Resize(_, _)) => {}
                Ok(_) => {}
                // A sequence this terminal sends and crossterm does not know is
                // dropped rather than taken as a reason to stop. xterm's older
                // modifyOtherKeys is one of those, and dying on a key press is
                // the worst thing a key handler can do.
                Err(_) => {}
            }
            // Anything that got here came from the keyboard, the mouse or the
            // terminal's size: each of those can change the screen. A frame is drawn
            // for it unless the loop is going to draw one on the way round anyway,
            // which is what keeps a round in flight from paying for two frames an
            // event -- a stream is a thousand events, and the top of the loop has
            // already taken in what just arrived.
            if !app.animating() {
                term.draw(|f| draw(f, &mut app))?;
            }
        }
    };

    if let Some(sess) = &mut app.sess {
        sess.close();
    }
    disable_raw_mode()?;
    #[cfg(not(target_os = "windows"))]
    execute!(term.backend_mut(), event::PopKeyboardEnhancementFlags)?;
    execute!(
        term.backend_mut(),
        DisableMouseCapture,
        DisableBracketedPaste,
        LeaveAlternateScreen,
        SetBackgroundColor(TermColor::Reset)
    )?;
    term.show_cursor()?;
    result
}

/// How many rows of a typed answer the panel shows: the tail, because typing
/// appends at the end, and a cap is what keeps a pasted paragraph from pushing
/// the question off the top of its own box.
const ANSWER_ROWS: usize = 4;

/// The hint under a typed answer. Here rather than inline in `ask_panel` because the
/// panel is sized to its content, and this is content: a hint wrapped to three rows
/// in a box drawn for a two-word question is the box being the wrong size.
const ANSWER_HINT: &str = "enter sends it \u{b7} shift-enter a new line \u{b7} esc clears";

/// `text` wrapped to `inner` columns and cut to `room` rows, the last one
/// ellipsized when there was more of it than the room. A box that just stopped
/// mid sentence read as the text being broken rather than as the panel being
/// short.
fn head_lines(text: &str, inner: usize, room: usize, style: Style) -> Vec<Line<'static>> {
    let wrapped = wrap_text(text, inner);
    let cut = wrapped.len() > room;
    wrapped
        .into_iter()
        .take(room)
        .enumerate()
        .map(|(i, line)| {
            let line = if cut && i + 1 == room {
                let mut short: String = line.chars().take(inner.saturating_sub(1)).collect();
                while short.ends_with(' ') {
                    short.pop();
                }
                short.push('\u{2026}');
                short
            } else {
                line
            };
            Line::from(Span::styled(line, style))
        })
        .collect()
}

/// The question's panel, as rows wrapped into `inner` columns and fitted to the
/// `room` a pane can give it.
///
/// Measured after the wrapping rather than guessed from the options: the height
/// used to be "options plus five", so a question that wrapped to four lines --
/// and every answer typed past one line -- pushed the answer row out of the
/// bottom of a box whose height had been worked out without them. That is the
/// "some of it is hidden" it looked like. What cannot be missing is built
/// first: the answer row and its hint, then whatever the question and its
/// detail have left over.
///
/// ponytail: a question taller than the whole pane loses its tail rather than
/// growing a scrollbar. It is a question, not a document; give the panel an
/// offset of its own if one ever needs more room than the pane has.
fn ask_panel(asked: &Ask, inner: usize, room: usize) -> Vec<Line<'static>> {
    let bright = Style::new().fg(ink::code());
    let dim = Style::new().fg(ink::quiet());
    let on_mark = bright.add_modifier(Modifier::BOLD);
    let off_mark = Style::new().fg(ink::rule());
    let indent = inner.saturating_sub(5).max(1);

    let typing = asked.on_custom() && !asked.typed.is_empty();
    // The answer and its hint sit two columns in, so two columns less is the room
    // they have -- the same measure the row is drawn on, not the options'.
    let field = inner.saturating_sub(2).max(1);
    let mut fixed: Vec<Line> = vec![Line::from("")];
    for (i, option) in asked.options.iter().enumerate() {
        let on = i == asked.picked;
        let style = if on { on_mark } else { off_mark };
        // A permission's rows carry their own keys -- `enter, y` beside allow and `n`
        // beside refuse. A hint under the pair is a row nobody needs and makes the
        // reader match two keys to two answers themselves; this way the key is on the
        // line it sends.
        let option = match (asked.is_question(), i) {
            (false, 0) => format!("{option} \u{b7} enter, y"),
            (false, _) => format!("{option} \u{b7} n, esc"),
            _ => option.clone(),
        };
        // Wrapped, with the lead repeating on every row the option needs: a
        // choice is often a whole sentence, and one that ran off the border read
        // as the list cutting it off rather than as a long choice.
        for (n, text) in wrap_text(&option, indent).into_iter().enumerate() {
            fixed.push(Line::from(vec![
                Span::styled(
                    if n == 0 {
                        format!("{} {}. ", if on { "\u{276f}" } else { " " }, i + 1)
                    } else {
                        " ".repeat(5)
                    },
                    if n == 0 { style } else { dim },
                ),
                Span::styled(text, style),
            ]));
        }
    }
    // A question has an answer row; a permission does not. Its two rows are the whole
    // of what can be said to it, and every key it takes is named on the row it sends,
    // so a row of keys under them would be the same thing said twice.
    if asked.is_question() {
        // The answer row. Three states, each visibly different or the box reads as
        // broken: the cursor is here and nothing is typed (a block caret), the cursor
        // is here and something is typed (the text, wrapped, with the caret at its
        // end), or the cursor is up in the list (a quiet row saying this one exists).
        let answer = if typing {
            wrap_text(&format!("{}\u{2588}", asked.typed), field)
        } else if asked.on_custom() {
            vec!["\u{2588}".to_string()]
        } else if asked.has_choices() {
            vec!["or type your own\u{2026}".to_string()]
        } else {
            vec![String::new()]
        };
        let from = answer.len().saturating_sub(ANSWER_ROWS);
        for (n, text) in answer[from..].iter().enumerate() {
            fixed.push(Line::from(vec![
                Span::styled(
                    if n == 0 && asked.on_custom() { "\u{276f} " } else { "  " },
                    Style::new().fg(if asked.on_custom() { ink::code() } else { ink::rule() }),
                ),
                Span::styled(
                    text.clone(),
                    Style::new().fg(if asked.on_custom() { ink::code() } else { ink::quiet() }),
                ),
            ]));
        }
        if typing {
            // Enter is what sends it, and the one thing a typed answer needs said:
            // the answer takes new lines, so the key that sends has to be told apart
            // from the one that breaks the line.
            for text in wrap_text(ANSWER_HINT, field) {
                fixed.push(Line::from(Span::styled(format!("  {text}"), dim)));
            }
        }
    }

    // What is left of the pane is the room the head has: the question for a question,
    // the document for a permission. The rows of the answer are never the part that
    // gets cut.
    let head_room = room.saturating_sub(fixed.len());
    let mut lines = if asked.is_question() {
        let mut head = head_lines(
            &asked.question,
            inner,
            head_room.max(usize::from(room > 0)),
            bright.add_modifier(Modifier::BOLD),
        );
        let left = room.saturating_sub(head.len());
        if !asked.detail.is_empty() && left > 1 {
            head.push(Line::from(""));
            head.extend(head_lines(&asked.detail, inner, left - 1, dim));
        }
        head
    } else {
        // The permission's document, in the tones it was built with: why the gate
        // stopped the call, the call itself, what it would do, and the change. Cut at
        // the pane, with the last row saying there is more rather than stopping the
        // text mid-sentence -- the decision under it is what has to be whole.
        //
        // ponytail: the question's own head (question and detail) still loses its tail
        // when it is taller than the pane, where a permission's document scrolls. A
        // question that long is a paragraph, and the answer is what has to fit.
        let mut doc: Vec<Line> = view::wrap_lines(&asked.body, inner);
        // The document's own trailing blank is not a gap: the rows under it already
        // stand apart from it, and two blank lines made the decision look detached
        // from what it was deciding.
        while doc.last().is_some_and(|line| line.to_string().trim().is_empty()) {
            doc.pop();
        }
        // The document is what scrolls, and the rows under it never do. Scrolled to the
        // end it stays there: the last page is where the last of the change is.
        let most = doc.len().saturating_sub(head_room);
        let from = asked.scroll.min(most);
        let end = (from + head_room).min(doc.len());
        let cut = end < doc.len();
        let mut head: Vec<Line> = doc.drain(from..end).collect();
        if cut && !head.is_empty() {
            head.pop();
            head.push(Line::from(Span::styled("\u{2026}", dim)));
        }
        head
    };
    lines.extend(fixed);
    // A pane with less room than the question and its list need: the tail goes,
    // which is the hint and then the last of the options. Nothing is ever drawn
    // past its own box, where the row after it would cut it in half.
    lines.truncate(room.max(1));
    lines
}

fn draw(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let column = column_of(area);
    // Nothing said yet: the word, what the screen knows about this session, and the
    // box. An opening screen is not a conversation with no rows in it, it is a door.
    if app.blank() {
        draw_home(f, app, column);
        return;
    }
    let head = app.header();
    let status = app.status_text(column.width as usize);
    let meta = composer_meta(app, column.width as usize, true);
    let news = app.news.as_ref().and_then(|n| n.behind());
    // Read once and used twice: the row's highlight and the status line's are the same
    // sweep off the same clock, which is what keeps the two moving at one speed.
    let shimmer = app.shimmer();

    // The frame is the header, the update notice, the conversation, the box -- a rule,
    // the message, its own row and a rule -- and the status row under it. A pane that
    // cannot hold all of it gives up the status row first, then the notice, then the
    // header, then the box's own row, then the box's rules: the conversation and one row
    // of the message are never given up.
    let mut head_on = true;
    let mut notice_on = news.is_some();
    let mut meta_on = meta.is_some();
    let mut status_on = status.is_some();
    let need = |with_head: bool, notice: bool, meta: bool, status: bool| {
        (if with_head { head.len() } else { 0 })
            + usize::from(notice)
            + usize::from(meta)
            + usize::from(status)
            + 4
    };
    while need(head_on, notice_on, meta_on, status_on) > usize::from(area.height) {
        if status_on {
            status_on = false;
        } else if notice_on {
            notice_on = false;
        } else if head_on {
            head_on = false;
        } else if meta_on {
            meta_on = false;
        } else {
            break;
        }
    }
    let framed = usize::from(area.height) >= 4;
    let chunks = if framed {
        let chrome = 3 + usize::from(meta_on);
        let spare = usize::from(area.height)
            - (if head_on { head.len() } else { 0 })
            - usize::from(notice_on)
            - chrome
            - usize::from(status_on);
        let field_h = (app.field_height(column.width as usize) as usize)
            .min(spare.max(1))
            .max(1) as u16;
        let mut rows = Vec::with_capacity(5 + head.len() + 2);
        if head_on {
            rows.push(Constraint::Length(head.len() as u16));
        }
        if notice_on {
            rows.push(Constraint::Length(1));
        }
        rows.extend([
            Constraint::Min(1),
            Constraint::Length(1),
            Constraint::Length(field_h),
        ]);
        if meta_on {
            rows.push(Constraint::Length(1));
        }
        rows.extend([
            Constraint::Length(1),
            Constraint::Length(u16::from(status_on)),
        ]);
        Layout::vertical(rows).split(column)
    } else {
        Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(column)
    };
    let (head_at, notice_at, body, rules, field_at, meta_at, status_at) = if framed {
        let head_slot = usize::from(head_on);
        let meta_slot = usize::from(meta_on);
        let at = head_slot + usize::from(notice_on);
        (
            head_on.then(|| chunks[0]),
            notice_on.then(|| chunks[head_slot]),
            chunks[at],
            Some((chunks[at + 1], chunks[at + 3 + meta_slot])),
            chunks[at + 2],
            meta_on.then(|| chunks[at + 3]),
            status_on.then(|| chunks[at + 4 + meta_slot]),
        )
    } else {
        (None, None, chunks[0], None, chunks[1], None, None)
    };
    if let Some(at) = head_at {
        f.render_widget(Paragraph::new(head), at);
    }
    if let (Some(at), Some(behind)) = (notice_at, news) {
        f.render_widget(Paragraph::new(notice_line(behind)), at);
    }

    let pieces = view::pieces(
        &app.log.rows,
        &|i, row| app.shows(i, row),
        &|i| app.thought_shows(i),
        &|i| app.figures(i),
        app.writing(),
        app.flash(),
        body.width.max(1) as usize,
        shimmer,
    );
    let Drawn { lines, spans, anchors, links } = view::draw(&pieces, body.width.max(1) as usize);
    app.spans = spans;
    app.anchors = anchors;
    app.body_top = body.y;
    app.body_width = body.width;

    let total = lines.len() as u16;
    let height = body.height.max(1);
    let end = total.saturating_sub(height);
    if app.follow {
        app.scroll = end;
    } else if app.scroll >= end {
        // Scrolled back to the end by hand: the newest row is what it shows, so
        // it follows again.
        app.scroll = end;
        app.follow = true;
    }
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)), body);
    // The paragraph has laid the rows out; now the links on them become links. It
    // has to be after the draw, because the column a label starts at is not known
    // until the line has been wrapped and scrolled into the pane.
    view::hyperlinks(f.buffer_mut(), body, app.scroll as usize, &links);

    // The box: a rule over the message, the message on its own line, the box's own
    // row under that, and a rule closing it. Three marks and the rail: the foot and the
    // half-block pedestal that used to fence the same input were two more.
    if let Some((top, bottom)) = rules {
        let rule = Line::from(Span::styled(
            "\u{2500}".repeat(column.width as usize),
            Style::new().fg(ink::rule()),
        ));
        f.render_widget(Paragraph::new(rule.clone()), top);
        f.render_widget(Paragraph::new(rule), bottom);
    }
    let field = app.field_lines(
        field_at.width.max(1) as usize,
        field_at.height.max(1) as usize,
        field_at.y,
    );
    // What the field turned out to be, for a click on a chip: the same numbers the draw
    // just used, rather than a second derivation of them.
    app.field_rows = field_windows(app, field_at.width.max(1) as usize);
    app.field_top = field_at.y;
    app.field_left = field_at.x;
    f.render_widget(Paragraph::new(field), field_at);
    // The box's own row, under the message: what would answer it, and the round's clock
    // pushed against the right end. Inside the box, between the message and the rule
    // under it, which is where it is read without leaving the line being typed.
    if let (Some(at), Some(line)) = (meta_at, meta) {
        f.render_widget(Paragraph::new(line), at);
    }

    if let (Some(at), Some(line)) = (status_at, status) {
        f.render_widget(Paragraph::new(line), at);
    }

    // A menu, over the tail of the conversation and just above the field: it is
    // what the line being typed is for, and covering the answer it is about to
    // become part of would be the wrong thing to hide.
    draw_menu(f, app, body);

    // A prompt is its own panel, above the field and below the conversation: it is a
    // decision, so it gets the room a decision needs rather than one line in a box
    // built for typing. Drawn here rather than through `opened` because the keyboard
    // is answering it while it is up, and a panel that shared the scroll keys with
    // the answer would be fighting itself.
    if let Some(asked) = app.prompt.as_ref() {
        // As wide as what is in it, up to a cap: a two-line decision in a box that
        // spans the pane reads as a wall with a sentence in the corner of it. The
        // widest thing it holds is measured before the wrap, so the wrap inside can
        // only make a row shorter and never push the border out. Never wider than the
        // pane it sits in, or the panel runs off the edge of the screen.
        let widest = |text: &str| text.lines().map(|line| line.chars().count()).max().unwrap_or(0);
        let mut want = 0;
        for line in &asked.body {
            want = want.max(line.to_string().chars().count());
        }
        for option in &asked.options {
            // The lead a row draws before the text: `\u{276f} 1. `.
            want = want.max(option.chars().count() + 5);
        }
        want = want.max(widest(&asked.question)).max(widest(&asked.detail));
        // The hint under a typed answer is drawn only while there is one, and it is the
        // widest thing in the box when it is: three wrapped rows of keys under a
        // two-word question reads as a layout bug rather than as a long hint.
        if asked.is_question() && asked.on_custom() && !asked.typed.is_empty() {
            want = want.max(ANSWER_HINT.chars().count());
        }
        let width = (want.min(72) as u16 + 4)
            .clamp(24, 72)
            .min(body.width.saturating_sub(4).max(1))
            .max(1);
        let inner = width.saturating_sub(2).max(1) as usize;
        // The height is the rows the wrapping actually produced, not an estimate
        // off the option count: that estimate is what let a question of four
        // wrapped lines push the answer row out of the bottom of its own box.
        let lines = ask_panel(asked, inner, body.height.saturating_sub(2) as usize);
        let height = (lines.len() as u16 + 2).min(body.height).max(2);
        let popup = Rect {
            x: body.x + (body.width.saturating_sub(width)) / 2,
            y: body.y + body.height.saturating_sub(height),
            width,
            height,
        };
        f.render_widget(Clear, popup);
        f.render_widget(
            Paragraph::new(lines).block(
                Block::bordered()
                    .border_type(BorderType::Rounded)
                    .title(asked.title())
                    .border_style(Style::new().fg(ink::code())),
            ),
            popup,
        );
    }

    // An open panel, over the conversation and not over the field: a question is
    // answered on that line, and a panel that covered the answer would be asking for
    // one it hid.
    if let Some(open) = &mut app.opened {
        let popup = centred(body, body.width.saturating_sub(2), body.height);
        // Clear the whole pane, not just the box: a box inset inside it would
        // leave a column of the conversation showing down its edge, which reads
        // as a broken frame rather than as a margin.
        f.render_widget(Clear, body);
        // Wrapped here, not by the widget, so the scroll offset counts what is on
        // the screen: a panel of long lines would otherwise scroll to the wrong
        // place, and nothing would stop it scrolling past the end.
        let inner = popup.width.saturating_sub(2).max(1) as usize;
        let wrapped = view::wrap_lines(&open.lines, inner);
        let most = (wrapped.len() as u16).saturating_sub(popup.height.saturating_sub(2));
        open.scroll = open.scroll.min(most);
        // A list taller than the pane has to say that it is: a page that stops at
        // ctrl-b reads as a page whose list ends there, and the keys that scroll it
        // are the ones nobody guesses. The range is counted in the lines drawn,
        // because that is what the scroll is counted in.
        let shown = popup.height.saturating_sub(2);
        let title = if most == 0 {
            format!(" {} ", open.title)
        } else {
            format!(
                " {} \u{b7} {}-{} of {} ",
                open.title,
                open.scroll + 1,
                (open.scroll + shown).min(wrapped.len() as u16),
                wrapped.len()
            )
        };
        f.render_widget(
            Paragraph::new(wrapped)
                .block(
                    Block::bordered()
                        .border_type(BorderType::Rounded)
                        .title(title)
                        .border_style(Style::new().fg(ink::rule())),
                )
                .scroll((open.scroll, 0)),
            popup,
        );
    }
}

/// The field as it stood before a deletion, for ctrl-y to come back to.
struct Held {
    input: String,
    shots: Vec<Shot>,
    caret: usize,
}

/// A picture waiting to be sent: its bytes, its own name, and where its chip sits
/// in the message.
///
/// The label is derived rather than stored -- `[image 2]` is `at + 2` -- because
/// the number is what ties the chip to the bytes, and a stored label could drift
/// from the ordinal the worker is handed.
#[derive(Clone)]
struct Shot {
    image: attach::Image,
    at: usize,
    len: usize,
}

/// One span of the message as it is drawn: text the user typed, or a chip
/// standing in for a picture. The message itself never holds the label -- `at`
/// and `len` are the characters of the message the chip covers -- so the text
/// that is sent and the text that is drawn cannot disagree.
///
/// The label is derived rather than stored -- `[image 2]` is the shot's own
/// place in `shots` plus one -- because the number is what ties the chip to the
/// bytes, and a stored label could drift from the ordinal the worker is handed.
/// The shot covering character `at`, if one is there. What makes a chip one
/// thing to the caret: a character inside one is the whole of it.
fn shot_at(shots: &[Shot], at: usize) -> Option<usize> {
    shots.iter().position(|s| at >= s.at && at < s.at + s.len)
}

/// The picture paths in a pasted block, if every token in it is one.
///
/// All or nothing on purpose: a paste that is mostly prose with a filename in it
/// is prose. Backslash-escaped spaces and quoted strings are the two ways a
/// terminal hands a path over, and both are read the way a shell reads them.
fn paths_as_images(text: &str) -> Option<Vec<attach::Image>> {
    let mut paths = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    let mut chars = trimmed.chars().peekable();
    while let Some(c) = chars.next() {
        match (quote, c) {
            (Some(q), _) if c == q => quote = None,
            (Some(_), '\\') => {
                // On Windows a backslash is a separator, not an escape: a
                // quoted `C:\shots\one.png` has to keep every one of them.
                if cfg!(windows) {
                    current.push('\\');
                } else if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (Some(_), _) => current.push(c),
            (None, '\'' | '"') => quote = Some(c),
            (None, '\\') => {
                // The same on an unquoted path: `C:\shots\one.png` is one
                // token, and eating each separator leaves a name no disk has.
                if cfg!(windows) {
                    current.push('\\');
                } else if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (None, c) if c.is_whitespace() => {
                if !current.is_empty() {
                    paths.push(std::mem::take(&mut current));
                }
            }
            (None, _) => current.push(c),
        }
    }
    if !current.is_empty() {
        paths.push(current);
    }
    if paths.is_empty() || !paths.iter().all(|p| attach::looks_like_image(p)) {
        return None;
    }
    let mut out = Vec::new();
    for path in paths {
        let path = expand_tilde(&path);
        match attach::read_file(&path) {
            Ok(image) => out.push(image),
            // Every path named a picture and one of them could not be read: the
            // paste is that file's failure rather than the user's, so nothing is
            // attached and the text stands as it was typed.
            Err(_) => return None,
        }
    }
    Some(out)
}

/// `~/shot.png`, which is how a path is usually written and never how the disk
/// spells it.
fn expand_tilde(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = cuacode::home_dir() {
            // Joined, not formatted: a Windows home already ends in a drive and
            // joins with the separator the rest of the path is checked against.
            return home.join(rest).display().to_string();
        }
    }
    path.to_string()
}

/// The style a column of the field's line is drawn in: the body tone for the words
/// the person typed, the amber the design gives anything you can act on for a chip,
/// and a reversal while the pointer is on that chip, which is how it says that a click
/// would take it off.
///
/// The background is not decided here: the card's own surface is under every cell of
/// this row, and a span that set one would be a second material for "your words".
fn chip_style(tag: u8, under_pointer: Option<u8>) -> Style {
    if tag == 0 {
        return Style::new().fg(ink::text());
    }
    let style = Style::new().fg(ink::code());
    if under_pointer == Some(tag) {
        style.add_modifier(Modifier::REVERSED)
    } else {
        style
    }
}

/// A string broken to lines that fit a width, on word boundaries where there are
/// any. The question panel draws what the worker sent, and that is prose of no
/// particular length.
fn wrap_text(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut out = Vec::new();
    for para in text.split('\n') {
        let mut line = String::new();
        for word in para.split_whitespace() {
            if !line.is_empty() && line.chars().count() + 1 + word.chars().count() > width {
                out.push(std::mem::take(&mut line));
            }
            if !line.is_empty() { line.push(' '); }
            line.push_str(word);
        }
        out.push(line);
    }
    out
}

/// What each row of the field is, and which chip each of its columns belongs to.
///
/// The draw needs exactly this and a click needs exactly this, so it is derived
/// once per frame rather than twice. `field_lines` is what paints it; this is
/// what a click reads. A row is now the whole width of the box, so there is no
/// window to remember and no offset to add back: the column a click lands in is
/// the column of the row.
fn field_windows(app: &App, width: usize) -> Vec<(u16, usize, Vec<u8>)> {
    let (cols, rows, _, _) = app.drawn(width.saturating_sub(LEAD).max(1));
    let mut out = Vec::new();
    for (k, (start, end)) in rows.iter().enumerate() {
        let styles: Vec<u8> = cols[*start..*end].iter().map(|(_, t)| *t).collect();
        out.push((k as u16, 0, styles));
    }
    out
}

/// The column of a pane the text is laid out in: the pane itself. opencode's session
/// content is `width="100%"`, and a page with a margin down each side is not a page it
/// draws -- the space beside the text was an invention of the first pass at this screen,
/// and it read as the window being the wrong size.
fn column_of(area: Rect) -> Rect {
    area
}

/// The one line that may sit above the conversation, and only on the day origin has a
/// commit this tree does not: what is missing, and the two commands that answer it. A
/// check that could not be made says nothing rather than guessing.
fn notice_line(behind: usize) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            format!(" you are behind {behind} {}", if behind == 1 { "update" } else { "updates" }),
            Style::new().fg(ink::warn()),
        ),
        Span::styled(
            "  \u{b7}  cuacode doctor, or cuacode doctor --upgrade",
            Style::new().fg(ink::dim()),
        ),
    ])
}

/// The mark, in opencode's own font as `pantheon-org/opencode-font` defines it: a glyph
/// is a seven-row grid of four columns, `#` is an ink cell and a space is not, and the
/// font is cut in two tones by row: the top band bright, the rows under it a step down.
/// Those are the letter shapes the font published, not ones drawn to match them.
const MARK_GLYPHS: [(char, [&str; 7]); 6] = [
    ('c', ["    ", "####", "#   ", "#   ", "#   ", "####", "    "]),
    ('u', ["    ", "#  #", "#  #", "#  #", "#  #", "####", "    "]),
    ('a', ["    ", "####", "   #", "####", "#  #", "####", "    "]),
    ('o', ["    ", "####", "#  #", "#  #", "#  #", "####", "    "]),
    ('d', ["   #", "####", "#  #", "#  #", "#  #", "####", "    "]),
    ('e', ["    ", "####", "#  #", "####", "#   ", "####", "    "]),
];
/// The word the mark spells, and the column where its accent half ends: `cua` is the
/// muted half and `code` the bright one, the way opencode's own lockup is split.
const MARK_WORD: &str = "cuacode";
/// The cells between two letters: one, which is what the font itself draws between two
/// glyphs, and what the mark was when it was last looked at and liked.
const MARK_GAP: usize = 1;
/// Where the accent half ends: three letters and the gaps after them.
const MARK_SPLIT: usize = 3 * 4 + 2 * MARK_GAP;
/// The rows the font colours its top band over: above this the mark takes the half's
/// bright tone, under it the same half one rung down.
const MARK_BAND: usize = 2;

/// One row of the mark as spans: the muted half and the bright half, each cut in the
/// font's two tones, with the gaps between letters left as page.
fn mark_line(cells: &[char], row: usize) -> Line<'static> {
    let bright = row <= MARK_BAND;
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut tone: Option<Color> = None;
    for (n, cell) in cells.iter().enumerate() {
        let ink = match cell {
            '#' if n < MARK_SPLIT => Some(if bright { ink::dim() } else { ink::quiet() }),
            '#' => Some(if bright { ink::text() } else { ink::dim() }),
            _ => None,
        };
        if ink != tone {
            if !run.is_empty() {
                spans.push(Span::styled(
                    std::mem::take(&mut run),
                    Style::new().fg(tone.unwrap_or(ink::text())),
                ));
            }
            tone = ink;
        }
        // A filled cell is a block on the screen; the grid's own `#` is how the font
        // publishes the shape and never something this screen prints.
        run.push(if *cell == '#' { '\u{2588}' } else { ' ' });
    }
    if !run.is_empty() {
        spans.push(Span::styled(run, Style::new().fg(tone.unwrap_or(ink::text()))));
    }
    Line::from(spans)
}

/// The narrowest pane that still carries the mark: under it the plain word is drawn
/// instead, because a wordmark wider than the screen is a wordmark cut in half.
const MARK_MIN: usize = 40;

/// The screen with nothing said in it yet: the mark, who would answer, the box, and the
/// two facts that belong on its own floor. A message typed here is the first row of a
/// conversation, and the moment one exists the session layout takes over.
///
/// What the title bar used to hold is here, and spread out: the model above the box
/// because it is who the sent message goes to, and the directory with the permission
/// mode and the build along the bottom edge, where a fact about the screen rather than
/// about the round belongs.
fn draw_home(f: &mut Frame, app: &mut App, column: Rect) {
    let width = (column.width as usize).min(HOME_WIDTH).max(12) as u16;
    let x = column.x + column.width.saturating_sub(width) / 2;
    let meta = composer_meta(app, width as usize, false);
    let name = who(app);
    let message_rows = app.field_height(width as usize).max(1) as u16;
    // The mark, who would answer, then the box: a blank between each of them. The box
    // is a rule, the message, its own row (the figures) and a rule -- the name is what
    // this screen says at the top, and the figures are what belongs beside the typing.
    let mut block = if column.width as usize >= MARK_MIN { 7 } else { 1 };
    if !name.is_empty() {
        block += 2;
    }
    block += 1 + 1 + message_rows + u16::from(meta.is_some()) + 1;
    let top = column.y + column.height.saturating_sub(block) / 2;
    let rule = || {
        Line::from(Span::styled(
            "\u{2500}".repeat(width as usize),
            Style::new().fg(ink::rule()),
        ))
    };

    // opencode's lockup: the muted half, a cell, the bright half, and each of them two
    // tones deep, the top of every letter brighter than the rows under it.
    let art = if column.width as usize >= MARK_MIN { 7 } else { 1 };
    if art == 1 {
        f.render_widget(
            Paragraph::new(centre_in(
                Line::from(Span::styled(
                    "cuacode",
                    Style::new().fg(ink::text()).add_modifier(Modifier::BOLD),
                )),
                width as usize,
            )),
            Rect { x, y: top, width, height: 1 },
        );
    } else {
        for row in 0..7u16 {
            let mut cells: Vec<char> = Vec::new();
            for (n, ch) in MARK_WORD.chars().enumerate() {
                if n > 0 {
                    cells.extend(std::iter::repeat_n(' ', MARK_GAP));
                }
                let glyph = MARK_GLYPHS.iter().find(|(c, _)| *c == ch).map(|(_, g)| g);
                cells.extend(glyph.map(|g| g[row as usize]).unwrap_or("    ").chars());
            }
            f.render_widget(
                Paragraph::new(centre_in(mark_line(&cells, row as usize), width as usize)),
                Rect { x, y: top + row, width, height: 1 },
            );
        }
    }
    let mut y = top + art;
    if !name.is_empty() {
        y += 2;
        f.render_widget(
            Paragraph::new(centre_in(Line::from(name), width as usize)),
            Rect { x, y, width, height: 1 },
        );
        y += 1;
    }
    y += 1;
    let box_top = y;
    f.render_widget(Paragraph::new(rule()), Rect { x, y, width, height: 1 });
    y += 1;
    let field = app.field_lines(width as usize, message_rows as usize, y);
    app.field_rows = field_windows(app, width as usize);
    app.field_top = y;
    app.field_left = x;
    f.render_widget(Paragraph::new(field), Rect { x, y, width, height: message_rows });
    y += message_rows;
    // The box's own row: what would answer the message, and the round's clock, inside
    // the two rules. The opening screen has one whenever there is anything to say --
    // a session has a model from its first status, so it is there from the start.
    if let Some(line) = meta {
        f.render_widget(Paragraph::new(line), Rect { x, y, width, height: 1 });
        y += 1;
    }
    f.render_widget(Paragraph::new(rule()), Rect { x, y, width, height: 1 });

    // The floor: where a message typed into this box would run, and what it would run
    // under. Pinned to the bottom edge, because it is a fact about the screen and not
    // the next thing to read.
    if column.height > block + 1 {
        f.render_widget(
            Paragraph::new(home_floor(app, column.width as usize)),
            Rect { y: column.y + column.height - 1, height: 1, ..column },
        );
    }

    // The `/`, `$` and `@` menus belong to the box, not to a conversation, so the one
    // typed here opens over the page above it, as wide as the box.
    draw_menu(f, app, Rect { x, y: 0, width, height: box_top.saturating_sub(column.y) });
}

/// Who would answer a message: the model, the provider that serves it, and the
/// effort it thinks at, as the wire last said them. The left half of the box's own
/// row, on either screen.
fn who(app: &App) -> Vec<Span<'static>> {
    let Some(snap) = app.sess.as_ref().map(|sess| &sess.snap) else { return Vec::new() };
    let sep = Span::styled("  \u{b7}  ", Style::new().fg(ink::faint()));
    let mut spans: Vec<Span<'static>> = Vec::new();
    if !snap.model.is_empty() {
        spans.push(Span::styled(snap.model.clone(), Style::new().fg(ink::text())));
    }
    if !snap.provider.is_empty() {
        if !spans.is_empty() {
            spans.push(sep.clone());
        }
        spans.push(Span::styled(snap.provider.clone(), Style::new().fg(ink::faint())));
    }
    if !snap.effort.is_empty() {
        if !spans.is_empty() {
            spans.push(sep);
        }
        spans.push(Span::styled(
            capitalize(&snap.effort),
            Style::new().fg(ink::warn()).add_modifier(Modifier::BOLD),
        ));
    }
    spans
}

/// The box's own row, under the message: who would answer it on the left, where a
/// conversation has nothing else saying it, and the conversation's figures pushed
/// against the right end -- the context gauge, the rate, and the round's own clock.
///
/// `None` when there is nothing to say: a replay has no session and a round that has
/// not run has no clock, and a blank row inside the box is a blank row.
fn composer_meta(app: &App, width: usize, with_who: bool) -> Option<Line<'static>> {
    let snap = app.sess.as_ref().map(|sess| &sess.snap);
    let name = if with_who { who(app) } else { Vec::new() };
    let gauge = snap.map(context_gauge).unwrap_or_default();
    let rate = snap.map(token_rate).unwrap_or_default();
    let clock = app.turn.as_ref().map(|t| view::turn(t, app.ms()));
    if name.is_empty() && gauge.is_empty() && rate.is_empty() && clock.is_none() {
        return None;
    }
    // A pane too narrow for all of it gives up the clock first, then the rate, then the
    // gauge, then the name: the name is what this row exists to say, and the figures are
    // on the opening screen's floor as well.
    let mut keep = [true; 4];
    loop {
        let left = joined(&[Span::styled(RAIL, Style::new().fg(ink::call()))], &[
            if keep[3] { name.as_slice() } else { &[] },
            if keep[2] { gauge.as_slice() } else { &[] },
            if keep[1] { rate.as_slice() } else { &[] },
        ]);
        let right = match (keep[0], clock.as_ref()) {
            (true, Some(clock)) => clock.spans.clone(),
            _ => Vec::new(),
        };
        if zone_width(&left) + zone_width(&right) + 1 <= width || !keep.iter().any(|k| *k) {
            return Some(row(left, right, width));
        }
        match keep.iter().position(|k| *k) {
            Some(at) => keep[at] = false,
            None => return Some(row(left, right, width)),
        }
    }
}

/// The opening screen's floor: the directory a message would run in, and against the
/// right edge the permission mode it would run under and the build it is running.
fn home_floor(app: &App, width: usize) -> Line<'static> {
    let dir = app.working_dir();
    let mut left = vec![Span::raw(" ")];
    if !dir.is_empty() {
        left.push(Span::styled(dir, Style::new().fg(ink::faint())));
    }
    let sep = Span::styled(" \u{b7} ", Style::new().fg(ink::rule()));
    let mut right: Vec<Span<'static>> = Vec::new();
    if let Some(mode) = app
        .sess
        .as_ref()
        .map(|s| s.snap.mode.as_str())
        .filter(|m| !m.is_empty())
    {
        right.push(Span::styled("perms ", Style::new().fg(ink::faint())));
        right.push(Span::styled(mode.to_string(), Style::new().fg(ink::quiet())));
        right.push(sep);
    }
    right.push(Span::styled(
        format!("quik v{}", version::ID),
        Style::new().fg(ink::faint()),
    ));
    let used = zone_width(&left) + zone_width(&right);
    left.push(Span::raw(" ".repeat(width.saturating_sub(used).saturating_sub(1))));
    left.extend(right);
    left.push(Span::raw(" "));
    Line::from(left)
}

/// The menu the line being typed opened, over the tail of `area` and just above the
/// composer: it is what the line is for, and covering what it is about to become the
/// start of would be the wrong thing to hide.
fn draw_menu(f: &mut Frame, app: &App, area: Rect) {
    let Some(menu) = &app.menu else { return };
    if area.height < 3 {
        return;
    }
    // As wide as the widest row it holds, not as wide as the pane: a menu stretched to the
    // column's full width is mostly empty and reads as a panel with a list lost in it, and
    // in a wide pane it puts a row's hint half a screen from the word it explains.
    let most = (area.width.saturating_sub(4) as usize).min(MENU_W).max(8);
    let widest = menu
        .lines(most)
        .iter()
        .map(Line::width)
        .max()
        .unwrap_or(8)
        .min(most);
    // The rows and nothing else: no border, no title, no footer. A rounded box with a
    // label on its edge and a row of keys under the list is three edges of chrome around
    // a handful of words, and the conversation behind it is what the box is covering.
    let width = widest.min(MENU_W).max(8) as u16 + 2;
    let lines = menu.lines(width.saturating_sub(2) as usize);
    let height = (lines.len() as u16).min(area.height).min(14);
    let popup = Rect {
        x: area.x,
        y: area.y + area.height - height,
        width: width.min(area.width),
        height,
    };
    f.render_widget(Clear, popup);
    f.render_widget(Paragraph::new(lines), popup);
}

/// The widest a menu gets, whatever the pane: wide enough for a command and the longest
/// hint the worker sends, and no wider.
const MENU_W: usize = 68;

/// A line of the mark, centred in the width it is drawn in.
fn centre_in(line: Line<'static>, width: usize) -> Line<'static> {
    let left = width.saturating_sub(line.width()) / 2;
    let mut spans = vec![Span::raw(" ".repeat(left))];
    spans.extend(line.spans);
    Line::from(spans)
}

/// A box centred in `area`, at most this wide and this tall.
fn centred(area: Rect, width: u16, height: u16) -> Rect {
    let w = width.min(area.width);
    let h = height.min(area.height);
    Rect {
        x: area.x + (area.width - w) / 2,
        y: area.y + (area.height - h) / 2,
        width: w,
        height: h,
    }
}

/// The effort ladder's own word, the way the design writes it: High, not high.
fn capitalize(word: &str) -> String {
    let mut chars = word.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

/// A round number of tokens, the way a context window is quoted: 1m, 200k.
fn plenty(n: i64) -> String {
    if n >= 1_000_000 {
        format!("{}m", n / 1_000_000)
    } else if n >= 1_000 {
        format!("{}k", n / 1_000)
    } else {
        n.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    /// The loop's pace, which is the one thing that decides whether a streamed answer
    /// looks continuous. Written down because it is invisible in a screenshot and easy
    /// to put back to the one slow number it used to be.
    #[test]
    fn text_arriving_is_drawn_at_a_frame_rate() {
        // A delta just landed: a frame every 16ms, sixty a second.
        assert_eq!(frame_wait(Duration::from_millis(0), true), STREAM_MS);
        // A pause between chunks keeps it: one answer, not a burst per chunk.
        assert_eq!(frame_wait(STREAM_HOLD - Duration::from_millis(1), true), STREAM_MS);
        // Quiet, but a round in flight: the spinner's pace, as it was.
        assert_eq!(frame_wait(STREAM_HOLD, true), MOVING_MS);
        // Nothing arriving and nothing moving.
        assert_eq!(frame_wait(Duration::MAX, false), STILL_MS);
    }

    /// Let the pause a prompt is held for go by: the screen is what it is 3.5 seconds
    /// after the last key, without the test sleeping for it.
    fn settle(app: &mut App) {
        app.since_key = Instant::now() - ASK_AFTER;
        app.raise_ask();
    }

    /// A drawn row of the composer as the test reads it. The box has no rail and no
    /// gutter any more, so a row is the prompt glyph and the message and nothing else:
    /// the helper stays because two dozen tests name it.
    fn said(line: &str, _width: usize) -> String {
        line.to_string()
    }

    /// One frame drawn into a test buffer and read back as text: the layout and
    /// the characters, without a terminal.
    fn frame_of(app: &mut App, w: u16, h: u16) -> String {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, app)).unwrap();
        let buf = term.backend().buffer();
        (0..h)
            .map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn seed(app: &mut App) {
        for line in [
            r#"{"type":"token","id":"a","data":{"state":"user","token":"what time is it?"}}"#,
            r#"{"type":"token","id":"a","data":{"state":"thinking","token":"They want the time. Run date."}}"#,
            r#"{"type":"token","id":"a","data":{"state":"tool_calls","token":[{"name":"Bash","args":{"command":"date"}}]}}"#,
            r#"{"type":"token","id":"a","data":{"state":"tool_output","token":"Bash","result":{"result":{"exit_code":0,"stdout":"Sat Oct  3 04:56:38 IDT 2026\n","duration":0.3}}}}"#,
            r#"{"type":"token","id":"a","data":{"state":"content","token":"It's 04:56 IDT on Saturday, October 3, 2026."}}"#,
        ] {
            app.feed(wire::parse(line).unwrap());
        }
    }

    /// The permission mark, byte for byte. This is the contract with
    /// `fleet/src/term.rs`: it reads these two strings back off the pane's stream
    /// and the whole point of a control plane is that a pane which stopped to ask
    /// is not a pane that looks idle. If this assertion changes, the other side
    /// changes with it or the mark silently stops being seen.
    #[test]
    fn the_permission_mark_is_the_bytes_fleet_reads() {
        assert_eq!(needs_you().as_bytes(), b"\x1b]9;cua-code-needs-you\x07");
        assert_eq!(answered().as_bytes(), b"\x1b]9;cua-code-needs-you-answer\x07");
        // OSC 9 to its `BEL`: a terminal that has never heard of us does nothing
        // with it rather than printing it, which is the whole reason for the
        // sequence and not `ESC [ ...`.
        assert!(needs_you().starts_with("\u{1b}]9;") && needs_you().ends_with('\u{7}'));
        // The answer's own name has the question's in it, so the two must not be
        // the same string: a prefix match on the raise would fire on the lower.
        assert_ne!(needs_you(), answered());
    }

    /// The host picker answers ctrl-g, and it lists the machines the picker will
    /// draw: this machine first, then whatever `hosts.json` held. Drawn into a
    /// buffer rather than asserted on a struct, because "the menu opened" is not
    /// the thing that matters -- the rows on the screen are.
    #[test]
    fn ctrl_g_lists_the_machines() {
        let mut app = App::new(None);
        app.hosts = vec![Host {
            name: "box".into(),
            ssh: "me@box".into(),
            dir: "/srv".into(),
            ..Host::default()
        }];
        app.key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::CONTROL));
        let kind = app.menu.as_ref().map(|m| m.kind);
        assert_eq!(kind, Some(Kind::Hosts), "ctrl-g opens the machine list");

        let screen = frame_of(&mut app, 100, 30);
        assert!(screen.contains("this machine"), "the one it is on is a row: {screen}");
        assert!(screen.contains("box"), "and the file's machines follow it");
        assert!(screen.contains("me@box"), "the ssh destination is the help beside it");
    }

    /// The conversation picker leads with the row that is not a conversation: a new
    /// one, standing where this screen's worker would put it. That is the row a person
    /// who has just moved to a box wants, and the one this used to make them find by
    /// pressing escape at a list of things they did not want.
    #[test]
    fn the_session_picker_offers_a_new_session_first() {
        let mut app = App::new(None);
        app.host = Some(Host {
            name: "box".into(),
            ssh: "me@box".into(),
            dir: "/srv/cuacode".into(),
            cwd: "/home/me/work".into(),
            ..Host::default()
        });
        app.resume = true;
        let list = r#"{"type":"sessions","id":"s1","data":{"sessions":[{"id":"2026-old","title":"an older one","updated":"2026-10-01T10:00:00+00:00","cwd":"/home/me/work"}],"active":"2026-old"}}"#;
        app.feed(wire::parse(list).unwrap());

        let open = app.menu.as_ref().expect("the picker goes up as the list lands");
        let first = open.chosen().expect("the picker highlights a row");
        assert_eq!(first.label, "new session");
        assert_eq!(first.hint, "/home/me/work", "the row says where the new one would stand");
        assert!(first.value.is_empty(), "empty is the new row: no session id can be");

        let screen = frame_of(&mut app, 100, 30);
        assert!(screen.contains("new session"), "{screen}");
        assert!(screen.contains("an older one"), "the box's own list is under it: {screen}");
    }

    /// A directory typed into the conversation picker is the row that starts a
    /// conversation there: no second command afterwards, and no session to pick first.
    #[test]
    fn a_path_typed_into_the_picker_is_a_new_session_row() {
        let mut app = App::new(None);
        app.resume = true;
        let list = r#"{"type":"sessions","id":"s2","data":{"sessions":[{"id":"2026-old","title":"an older one","updated":"2026-10-01T10:00:00+00:00","cwd":"/home/me/work"}],"active":"2026-old"}}"#;
        app.feed(wire::parse(list).unwrap());
        for c in "/home/me/deep".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        let open = app.menu.as_ref().expect("the picker is still up");
        let row = open.chosen().expect("the path row is what enter takes");
        assert_eq!(row.value, menu::NEW_IN, "a typed path is not a session id");
        assert_eq!(row.label, "new session in /home/me/deep");

        let screen = frame_of(&mut app, 100, 30);
        assert!(screen.contains("new session in /home/me/deep"), "{screen}");
    }

    /// Letting go of a box is not ending its work, and on this machine it is nothing at
    /// all: the worker here is this terminal's child, so the key says so rather than
    /// pretending something was detached.
    #[test]
    fn ctrl_d_on_this_machine_says_what_detach_is_for() {
        let mut app = App::new(None);
        app.key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL));
        let said: Vec<String> = app
            .log
            .rows
            .iter()
            .filter_map(|r| match r {
                Row::Notice { head, .. } => Some(head.clone()),
                _ => None,
            })
            .collect();
        assert!(
            said.iter().any(|s| s.contains("detach is for a box")),
            "said {said:?}"
        );
    }

    /// Letting go of a box has to leave the screen able to come back: the picker refuses
    /// the machine it thinks it is already on, so detaching has to clear that as well as
    /// the session, or the `ctrl-g` the line promises cannot reach the box it just left.
    #[test]
    fn detaching_a_box_leaves_the_screen_on_no_machine() {
        let mut app = App::new(None);
        app.host = Some(Host {
            name: "box".into(),
            ssh: "me@box".into(),
            dir: "/srv/cuacode".into(),
            ..Host::default()
        });
        app.hosts = app.host.clone().into_iter().collect();
        assert!(app.sess.is_none(), "the test starts with no worker attached");

        app.detach_box();

        assert!(
            app.host.is_none(),
            "nothing is attached, so the screen is on nothing and the picker can offer the box again"
        );
    }

    /// The two things this screen says on its own account: which build it is, on the
    /// opening screen, and what origin has that the tree does not, on the one line above
    /// the conversation.
    #[test]
    fn the_notice_names_what_is_behind_and_home_names_the_build() {
        let flat = |lines: Vec<Line<'static>>| -> String {
            lines
                .iter()
                .flat_map(|l| l.spans.iter().map(|s| s.content.to_string()))
                .collect()
        };
        let app = App::new(None);
        let floor = home_floor(&app, 80);
        let said: String = floor.spans.iter().map(|s| s.content.to_string()).collect();
        assert!(said.contains(version::ID), "the floor was {said:?}");
        assert!(!said.contains("behind"), "no news is no line: {said:?}");

        // One update is a word, three are a number with an s on it: a screen that
        // says "1 updates" is the tell that nobody read it.
        let one = flat(vec![notice_line(1)]);
        assert!(one.contains("behind 1 update"), "notice was {one:?}");
        assert!(!one.contains("1 updates"), "notice was {one:?}");
        let loud = flat(vec![notice_line(3)]);
        assert!(loud.contains("you are behind 3 updates"), "notice was {loud:?}");
        assert!(loud.contains("cuacode doctor --upgrade"), "notice was {loud:?}");

        // A check that could not be made says nothing rather than guessing.
        let mut app = App::new(None);
        app.news = Some(version::News::Unknown("no network".into()));
        assert!(app.news.as_ref().and_then(|n| n.behind()).is_none());
    }

    /// The mark goes up where the question does and comes down on every way out
    /// of it, which is the only thing about it the screen can get wrong.
    #[test]
    fn a_question_raises_the_mark_and_an_answer_lowers_it() {
        let ask = r#"{"type":"permission","id":"p1","data":{"name":"file","args":{"action":"write","path":"a.py","content":"x"}}}"#;
        for (exit, allow) in [(KeyCode::Char('y'), true), (KeyCode::Char('n'), false)] {
            let mut app = App::new(None);
            assert_eq!(app.mark, None, "nothing is marked on a screen with no question");
            app.feed(wire::parse(ask).unwrap());
            // A permission that lands mid-sentence marks nothing: the panel, and the
            // mark with it, wait for the keyboard to go still.
            assert_eq!(app.mark, None, "the mark went up over a sentence");
            settle(&mut app);
            assert_eq!(app.mark, Some(needs_you()), "the question raises the mark");
            app.key(KeyEvent::new(exit, KeyModifiers::NONE));
            assert_eq!(
                app.mark,
                Some(answered()),
                "{} lowers it, and a refusal is an exit like any other",
                if allow { "a yes" } else { "a no" }
            );
            assert!(app.prompt.is_none());
        }
        // Escape answers too, and is the third way out.
        let mut app = App::new(None);
        app.feed(wire::parse(ask).unwrap());
        settle(&mut app);
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.mark, Some(answered()));
    }

    /// The document behind a decision scrolls, and the rows that answer never move: a
    /// diff cut off at the pane is exactly what a person deciding on a write needs and
    /// cannot reach.
    #[test]
    fn a_permissions_document_scrolls_under_its_rows() {
        let diff: Vec<String> = (1..=40).map(|i| format!("+L{i:02}")).collect();
        let said = serde_json::json!({
            "type": "permission",
            "id": "p9",
            "data": {
                "name": "file",
                "kind": "permission",
                "args": {"action": "write", "path": "a.py", "content": "x"},
                "note": "it writes a file",
                "preview": {"summary": "write 9 bytes to a.py", "diff": diff.join("\n")}
            }
        });
        let mut app = App::new(None);
        app.feed(wire::parse(&said.to_string()).unwrap());
        settle(&mut app);

        let top = frame_of(&mut app, 80, 24);
        assert!(top.contains("+L01"), "the change is not on the panel:\n{top}");
        assert!(!top.contains("+L40"), "the whole diff fits, nothing to scroll");
        assert!(top.contains("1. allow"), "the rows are not there:\n{top}");

        for _ in 0..2 {
            app.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        }
        let down = frame_of(&mut app, 80, 24);
        assert!(!down.contains("+L01"), "the document did not move:\n{down}");
        assert!(down.contains("1. allow"), "the rows scrolled with it:\n{down}");

        // The end clamps instead of running off, and home comes back.
        for _ in 0..10 {
            app.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        }
        let end = frame_of(&mut app, 80, 24);
        assert!(end.contains("+L40"), "the end of the change is unreachable:\n{end}");
        assert!(end.contains("1. allow"), "the rows scrolled away:\n{end}");
        app.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        let home = frame_of(&mut app, 80, 24);
        assert!(home.contains("+L01"), "{home}");
    }

    /// A prompt that arrives while someone is typing waits for them to stop, and
    /// then waits a little longer: nothing of it is drawn, no mark goes up and the
    /// keys keep going where they were going, until the keyboard has been still.
    #[test]
    fn a_prompt_waits_for_the_typing_to_stop() {
        let mut app = App::new(None);
        typed(&mut app, "half a sentence");
        app.feed(
            wire::parse(
                r#"{"type":"permission","id":"p1","data":{"name":"shell","args":{"command":"rm x"}}}"#,
            )
            .unwrap(),
        );
        // Held: no panel, no mark, and the message still where it was.
        assert!(app.prompt.is_none(), "the panel came up over a sentence");
        assert!(app.pending.is_some(), "the permission was dropped");
        assert!(app.mark.is_none(), "the mark went up over a sentence");
        assert_eq!(app.input, "half a sentence");
        let shown = frame_of(&mut app, 80, 24);
        assert!(!shown.contains("allow shell?"), "{shown}");
        // 3.5 seconds after the last key, not before.
        app.since_key = Instant::now() - (ASK_AFTER - Duration::from_millis(100));
        assert!(!app.raise_ask(), "it came up early");
        assert!(app.prompt.is_none());
        // And then it does, with the mark, and the message is still there to send.
        settle(&mut app);
        assert!(app.prompt.is_some(), "the permission never came up");
        assert_eq!(app.mark, Some(needs_you()));
        assert_eq!(app.input, "half a sentence");
        let shown = frame_of(&mut app, 80, 24);
        assert!(shown.contains("allow shell?"), "{shown}");
    }

    /// A question the model asked is the thing the person is waiting for, not an
    /// interruption to be waited out: it goes up at once, over a half-typed message.
    #[test]
    fn a_question_goes_up_over_the_typing() {
        let mut app = App::new(None);
        typed(&mut app, "half a sentence");
        app.feed(
            wire::parse(
                r#"{"type":"permission","id":"q1","data":{"name":"ask_user","kind":"question","question":"which one?","options":["a","b"]}}"#,
            )
            .unwrap(),
        );
        assert!(app.raise_ask(), "the question waited for the keyboard to stop");
        assert!(app.prompt.is_some(), "the question was held");
        assert_eq!(app.mark, Some(needs_you()));
        assert_eq!(app.input, "half a sentence", "the message was disturbed");
        let shown = frame_of(&mut app, 100, 30);
        assert!(shown.contains("which one?"), "{shown}");
    }

    /// The field holding this text, with the caret at its end. What most of these
    /// tests mean by "a message".
    fn typed(app: &mut App, text: &str) {
        app.input = text.to_string();
        app.caret = app.input.chars().count();
    }

    /// More text at the caret, through the same insertion the keyboard uses, so a
    /// chip's place in the message moves exactly as it does for a person typing.
    fn more(app: &mut App, text: &str) {
        for c in text.chars() {
            app.insert(c);
        }
    }

    /// A picture onto the app, standing in for one read off disk: the bytes are
    /// nothing, and nothing here reads them.
    fn shot(app: &mut App, name: &str) {
        app.attach(attach::Image { name: name.into(), b64: "AAAA".into() });
    }

    /// The field's rows as a person sees them, and which chip each column of the
    /// first row belongs to.
    fn field_text(app: &App) -> (String, Vec<u8>) {
        let lines = app.field_lines(80, 8, 0);
        let text: String = lines.iter().map(Line::to_string).collect::<Vec<_>>().join("\n");
        let (cols, rows, _, _) = app.drawn(80);
        let tags: Vec<u8> = cols[rows[0].0..rows[0].1].iter().map(|(_, t)| *t).collect();
        (text, tags)
    }

    /// The whole point of the feature: a chip in the message, the picture on the
    /// wire, and a number that ties the two together.
    #[test]
    fn a_picture_goes_in_as_an_atomic_chip_and_out_as_numbered_bytes() {
        let mut app = App::new(None);
        typed(&mut app, "look at");
        shot(&mut app, "shot.png");
        more(&mut app, " and this");
        shot(&mut app, "other.png");

        // What the frame actually paints: the chips have to be on the screen, not
        // only in the model.
        let mut term = Terminal::new(TestBackend::new(60, 10)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let screen: Vec<String> =
            (0..10).map(|y| (0..60).map(|x| buf[(x, y)].symbol()).collect()).collect();
        let shown = screen.join("\n");
        assert!(shown.contains("[image 1]") && shown.contains("[image 2]"), "{shown}");
        assert!(shown.contains("look at") && shown.contains("and this"), "{shown}");

        // The message holds the label text, which is what gets sent; the drawing
        // shows the same label to the same width, so what is read is what is sent.
        assert_eq!(app.input, "look at [image 1]  and this [image 2] ");
        let (drawn, styles) = field_text(&app);
        assert!(drawn.contains("[image 1]") && drawn.contains("[image 2]"), "{drawn}");
        assert!(styles.iter().any(|t| *t == 1) && styles.iter().any(|t| *t == 2));

        // What the model is handed: the ordinal in the name matches the ordinal
        // in the message, which is the one thing this cannot get wrong.
        let json = app.shots[1].image.json(2);
        assert_eq!(json["name"], "image 2: other.png");
        assert_eq!(app.shots[0].image.json(1)["name"], "image 1: shot.png");
    }

    /// A chip is one thing: the caret steps over it, and one press takes it and
    /// its bytes off the message.
    #[test]
    fn a_chip_is_one_thing_to_the_caret_and_to_backspace() {
        let mut app = App::new(None);
        shot(&mut app, "a.png");
        assert_eq!(app.caret, app.input.chars().count(), "the caret lands past the chip");
        let end = app.caret;
        let start = end - app.shots[0].len;

        // Left from past the chip lands before it, never inside it.
        app.left();
        assert_eq!(app.caret, start);
        app.right();
        assert_eq!(app.caret, end);
        // Home is the line's start, which is before it; end is past it.
        app.line_home();
        assert_eq!(app.caret, 0);
        app.to_line_end();
        assert_eq!(app.caret, end);

        // And the one press: chip and bytes both gone.
        app.backspace();
        assert!(app.input.is_empty(), "{:?}", app.input);
        assert!(app.shots.is_empty());
        assert_eq!(app.caret, 0);

        // Delete does the same looking forward: with the caret before the chip.
        let mut app = App::new(None);
        shot(&mut app, "a.png");
        app.caret = 0;
        app.delete();
        assert!(app.input.is_empty() && app.shots.is_empty(), "{:?}", app.input);
    }

    /// Text typed around a chip, and deleted around it, moves the chip's place in
    /// the message without losing the picture.
    #[test]
    fn the_chips_stay_in_step_with_the_text_around_them() {
        let mut app = App::new(None);
        typed(&mut app, "one two");
        shot(&mut app, "a.png");
        more(&mut app, "three");
        // The caret lands past the chip, so the word typed next follows it: what
        // a person paste-then-keep-typing does.
        assert_eq!(app.input, "one two [image 1] three");
        assert_eq!(app.shots[0].at, 7);

        // A word delete that stops at the chip: the chip is not a word, so the cut
        // ends where the chip begins and it keeps its bytes.
        app.word_backspace();
        assert_eq!(app.shots.len(), 1, "the word delete ate the picture");
        assert_eq!(app.shots[0].image.name, "a.png");
        assert_eq!(app.input, "one two [image 1] ");

        // Killing to the end of the line after a chip leaves the chip alone, and
        // one with the caret before it takes it and the words after it.
        let mut app = App::new(None);
        typed(&mut app, "before");
        shot(&mut app, "a.png");
        app.kill_to_line_end();
        assert_eq!(app.input, "before [image 1] ");
        assert_eq!(app.shots.len(), 1);
        app.caret = 0;
        app.kill_to_line_end();
        assert!(app.input.is_empty() && app.shots.is_empty(), "{:?}", app.input);

        // And in the middle: a chip stays where it is while text is typed around
        // it, and its number still matches the bytes it carries.
        let mut app = App::new(None);
        typed(&mut app, "start");
        shot(&mut app, "a.png");
        more(&mut app, "end");
        app.left();
        app.left();
        more(&mut app, "X");
        assert_eq!(app.input, "start [image 1] eXnd");
        assert_eq!(app.shots[0].at, 5);
        assert_eq!(app.shots[0].image.json(1)["name"], "image 1: a.png");
    }

    /// Removing one picture renumbers the ones after it, which is what keeps the
    /// chips and the bytes handed over in the same order.
    #[test]
    fn removing_a_picture_renumbers_the_ones_after_it() {
        let mut app = App::new(None);
        shot(&mut app, "a.png");
        shot(&mut app, "b.png");
        shot(&mut app, "c.png");
        assert_eq!(app.shots[1].image.name, "b.png");
        // The chip's own place in `shots` is its number, so taking the middle one
        // out is all that renumbering takes.
        app.detach(1);
        assert_eq!(app.shots.iter().map(|s| s.image.name.as_str()).collect::<Vec<_>>(), vec!["a.png", "c.png"]);
        assert_eq!(app.shots[1].image.json(2)["name"], "image 2: c.png");
        assert!(!app.input.contains("[image 3]"), "{:?}", app.input);
    }

    /// A click on a chip takes that one off, and a click anywhere else does not.
    #[test]
    fn a_click_on_a_chip_removes_that_picture() {
        let mut app = App::new(None);
        typed(&mut app, "hi");
        shot(&mut app, "a.png");
        // Paint once so `field_rows` carries what a click reads: the draw is what
        // puts the geometry there.
        let mut term = Terminal::new(TestBackend::new(60, 12)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let (row, first, styles) = app.field_rows.first().cloned().expect("the field drew");
        let col = styles.iter().position(|t| *t == 1).expect("a chip column") as u16;
        // The field's own column, which is where the rail and the inset left the text.
        let click = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: app.field_left + 2 + col + first as u16,
            row: app.field_top + row,
            modifiers: KeyModifiers::NONE,
        };
        app.mouse(click);
        assert!(app.shots.is_empty(), "the click missed the chip");
        assert_eq!(app.input, "hi");

        // A click on the words does nothing but remember where it was.
        let mut app = App::new(None);
        typed(&mut app, "hi");
        shot(&mut app, "a.png");
        term.draw(|f| draw(f, &mut app)).unwrap();
        let row = app.field_rows[0].0;
        app.mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 3,
            row: app.field_top + row,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(app.shots.len(), 1);
    }

    /// The pointer on a chip lights it, and off it does not.
    #[test]
    fn a_chip_lights_up_under_the_pointer() {
        let mut app = App::new(None);
        shot(&mut app, "a.png");
        let mut term = Terminal::new(TestBackend::new(60, 12)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let (row, first, _) = app.field_rows.first().cloned().expect("the field drew");
        let at = MouseEvent {
            kind: MouseEventKind::Moved,
            column: 2 + first as u16 + 1,
            row: app.field_top + row,
            modifiers: KeyModifiers::NONE,
        };
        app.mouse(at);
        assert!(app.chip_at(at.column, at.row).is_some(), "the chip was not found");
        // Somewhere else on the line is not the chip.
        assert!(app.chip_at(40, at.row).is_none());
    }

    /// A paste is the other door in: a path that names a picture is that picture,
    /// and a paste of words is words.
    #[test]
    fn a_pasted_path_is_a_picture_and_a_pasted_sentence_is_a_sentence() {
        // A real png on disk, in the notebook rather than anywhere of the user's, and
        // under this process's own id: four agents run this suite at once and a shared
        // path is a file one of them truncates while another reads it.
        let dir = std::env::temp_dir().join(format!("quik-paste-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let png = dir.join("shot.png");
        let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
        bytes.extend_from_slice(b"payload");
        std::fs::write(&png, &bytes).unwrap();

        let mut app = App::new(None);
        app.paste(png.display().to_string());
        assert_eq!(app.shots.len(), 1, "the path was not read as a picture");
        assert_eq!(app.input, " [image 1] ");

        // A sentence with a filename in it is a sentence.
        let mut app = App::new(None);
        app.paste(format!("look at {} please", png.display()));
        assert!(app.shots.is_empty(), "a sentence was read as a drop");
        assert!(app.input.contains("look at"), "{:?}", app.input);

        // And a path that is not a picture at all is text.
        let mut app = App::new(None);
        app.paste("/tmp/notes.txt".to_string());
        assert!(app.shots.is_empty());
        assert_eq!(app.input, "/tmp/notes.txt");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Windows hands a paste over as keys, so the line endings it carries have
    /// to become the field's own newline rather than characters nobody can see.
    #[test]
    fn a_pasted_block_keeps_windows_line_endings_as_lines() {
        let mut app = App::new(None);
        app.paste("first\r\nsecond\rthird\n".to_string());
        assert_eq!(app.input, "first\nsecond\nthird\n");
    }

    /// An enter with more of a paste behind it is a line in the message, not
    /// the send key. The queue check itself needs a real console; this is the
    /// decision it feeds.
    #[cfg(target_os = "windows")]
    #[test]
    fn an_enter_inside_a_windows_paste_is_a_newline() {
        let mut app = App::new(None);
        typed(&mut app, "first");
        app.paste_burst = true;
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.input, "first\n");
    }

    /// cmd+delete and ctrl+delete, which are the line and the word, and which
    /// both have to work on a terminal that reports only one of the modifiers.
    #[test]
    fn the_line_and_word_deletes_are_bound_where_each_platform_puts_them() {
        let mut app = App::new(None);
        typed(&mut app, "one two three");
        app.caret = 4;
        // cmd+delete takes the rest of the line, which is the whole message here.
        app.key(KeyEvent::new(KeyCode::Delete, KeyModifiers::SUPER));
        assert_eq!(app.input, "one ");
        // ctrl+delete and alt+delete take the next word instead.
        typed(&mut app, "one two three");
        app.caret = 0;
        app.key(KeyEvent::new(KeyCode::Delete, KeyModifiers::CONTROL));
        assert_eq!(app.input, "two three");
        typed(&mut app, "one two three");
        app.caret = 0;
        app.key(KeyEvent::new(KeyCode::Delete, KeyModifiers::ALT));
        assert_eq!(app.input, "two three");
        // And the backspace half: ctrl+backspace is the word behind the caret.
        typed(&mut app, "one two");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::CONTROL));
        assert_eq!(app.input, "one ");
    }

    /// cmd+backspace, which on a mac terminal is not a key at all: Ghostty turns
    /// it into ctrl-u before the program sees it. Both spellings have to do the
    /// same thing, whichever one the terminal chose to send.
    #[test]
    fn cmd_backspace_clears_the_line_however_the_terminal_spells_it() {
        // The key itself, from a terminal that reports it: the whole line goes.
        let mut app = App::new(None);
        typed(&mut app, "delete all of this");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::SUPER));
        assert_eq!(app.input, "");

        // And the bytes Ghostty actually sends for it, ctrl-u: the same answer.
        let mut app = App::new(None);
        typed(&mut app, "delete all of this");
        app.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "");

        // It is the *line*, not the message: the line above it stays.
        let mut app = App::new(None);
        typed(&mut app, "keep me\ndrop this");
        app.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "keep me\n");
        assert_eq!(app.caret, 8);

        // And only what is before the caret on that line: the word after it, and
        // the space between them, are still there.
        let mut app = App::new(None);
        typed(&mut app, "head tail");
        app.caret = 4;
        app.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(app.input, " tail");
        assert_eq!(app.caret, 0, "the caret goes with what it cut");
    }

    /// ctrl-y puts back what a deletion took, in the one press the accident needs: a
    /// cmd-backspace over a whole paragraph, a backspace held down, and an escape
    /// with nothing running are all the same lost message.
    #[test]
    fn ctrl_y_puts_back_what_a_delete_took() {
        // The line, whole, with the caret where it was.
        let mut app = App::new(None);
        typed(&mut app, "a paragraph that was not meant to go");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::SUPER));
        assert!(app.input.is_empty());
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "a paragraph that was not meant to go");
        assert_eq!(app.caret, app.input.chars().count());

        // A key held down is one accident, not one per character: the first press
        // opened the run and the rest joined it, so one ctrl-y has all of it.
        let mut app = App::new(None);
        typed(&mut app, "gone in nineteen presses");
        for _ in 0.."gone in nineteen presses".len() {
            app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        }
        assert!(app.input.is_empty());
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "gone in nineteen presses");

        // And putting it back is not a way to lose what was typed since: the text
        // standing on the field goes onto the stack, so a second ctrl-y has it.
        let mut app = App::new(None);
        typed(&mut app, "first");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::SUPER));
        typed(&mut app, "second");
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "first");
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "second", "the text typed since went missing");

        // Escape with nothing running empties the field, so ctrl-y has to have it:
        // that press is the other half of the stop key.
        let mut app = App::new(None);
        typed(&mut app, "half a sentence");
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.input.is_empty());
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "half a sentence");
        // And with nothing left to put back it leaves the field alone rather than
        // emptying what is on it.
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "half a sentence");
    }

    /// A picture comes back with the text it was cut out of, and is still the
    /// picture the label's number promises: the label and the bytes stay one thing.
    #[test]
    fn ctrl_y_brings_a_chip_back_with_its_text() {
        let mut app = App::new(None);
        typed(&mut app, "look at this");
        shot(&mut app, "a.png");
        more(&mut app, " now");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::SUPER));
        assert!(app.input.is_empty() && app.shots.is_empty(), "{:?}", app.input);
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "look at this [image 1]  now");
        assert_eq!(app.shots.len(), 1, "the label came back without its picture");
        assert_eq!(app.shots[0].image.json(1)["name"], "image 1: a.png");
    }

    /// ctrl-a, ctrl-e, alt-b and alt-f: the bytes `cmd+←`, `cmd+→`, `alt+←` and
    /// `alt+→` become on a mac terminal, and readline's own moves everywhere else.
    #[test]
    fn the_line_and_word_moves_answer_to_the_bytes_a_terminal_sends_for_them() {
        let mut app = App::new(None);
        typed(&mut app, "one two three");
        // ctrl-a / ctrl-e are cmd+left and cmd+right where those are re-encoded.
        app.key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL));
        assert_eq!(app.caret, 0, "cmd+left arrives as ctrl-a");
        app.key(KeyEvent::new(KeyCode::Char('e'), KeyModifiers::CONTROL));
        assert_eq!(app.caret, 13, "cmd+right arrives as ctrl-e");

        // alt-b and alt-f are alt+left and alt+right, and a word per press.
        app.key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::ALT));
        assert_eq!(app.caret, 8);
        app.key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::ALT));
        assert_eq!(app.caret, 4);
        app.key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::ALT));
        assert_eq!(app.caret, 8);

        // ctrl-b is not one of them: this screen gives it to the background push.
        let mut app = App::new(None);
        typed(&mut app, "one two");
        app.key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::CONTROL));
        assert_eq!(app.caret, 7, "ctrl-b moved the caret, so it is bound to two things");
    }

    /// The line ends are still the line's ends when the message has more than one,
    /// and the ctrl spelling moves the same way the command key does.
    #[test]
    fn the_line_ends_are_the_line_ends_on_every_spelling() {
        for spelling in [KeyCode::Char('a'), KeyCode::Left] {
            let mut app = App::new(None);
            typed(&mut app, "one\ntwo");
            let modifiers = if spelling == KeyCode::Char('a') {
                KeyModifiers::CONTROL
            } else {
                KeyModifiers::SUPER
            };
            app.key(KeyEvent::new(spelling, modifiers));
            assert_eq!(app.caret, 4, "{spelling:?} went to the start of the second line");
        }
        for spelling in [KeyCode::Char('e'), KeyCode::Right] {
            let mut app = App::new(None);
            typed(&mut app, "one\ntwo");
            app.caret = 4;
            let modifiers = if spelling == KeyCode::Char('e') {
                KeyModifiers::CONTROL
            } else {
                KeyModifiers::SUPER
            };
            app.key(KeyEvent::new(spelling, modifiers));
            assert_eq!(app.caret, 7, "{spelling:?} went to the end of the second line");
        }
    }

    /// A pane this small has no room for the header and the rules; it must still
    /// draw, because a split that small is where this frontend is meant to live.
    #[test]
    fn a_pane_too_small_for_the_frame_still_draws() {
        for (w, h) in [(20u16, 5u16), (8, 4), (2, 2)] {
            let mut app = App::new(None);
            seed(&mut app);
            typed(&mut app, "hi");
            let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
            term.draw(|f| draw(f, &mut app)).unwrap();
        }
    }

    /// A menu draws over the conversation without taking the field with it:
    /// what is being chosen has to stay visible on the line it is being typed
    /// into, and a pane too small for the frame must still draw.
    #[test]
    fn a_menu_draws_over_the_conversation_and_leaves_the_field() {
        let mut app = App::new(None);
        seed(&mut app);
        typed(&mut app, "$un");
        let mut menu = Menu::open(Kind::Skills, 1);
        menu.fill(vec![
            Item::new("unslop", "Cut AI tells", "$unslop"),
            Item::new("review", "A verdict", "$review"),
        ]);
        app.menu = Some(menu);

        let (w, h) = (60u16, 14u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> =
            (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        // The header is above the conversation, so the body starts under it; the menu is
        // plain rows, and the box keeps its line wherever the menu draws over it.
        let body = lines[..h as usize - 2].join("\n");
        assert!(body.contains("unslop"), "{body}");
        assert!(body.contains("review"), "{body}");
        assert!(
            said(&lines[h as usize - 2], w as usize).starts_with('\u{2502}'),
            "{:?}",
            lines[h as usize - 2]
        );

        // And it draws in a pane with no room for it, rather than panicking.
        for (w, h) in [(30u16, 6u16), (12, 5), (4, 3)] {
            let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
            term.draw(|f| draw(f, &mut app)).unwrap();
        }
    }

    /// A picker's filter is its own. Typing at the session question must not put
    /// characters on the message, and escape there takes nothing back off it
    /// because nothing was ever put on. A trigger's menu is the exact opposite,
    /// and both halves are the same key handler.
    #[test]
    fn typing_at_a_picker_leaves_the_message_alone() {
        let mut app = App::new(None);
        typed(&mut app, "hello/");
        app.menu = Some(Menu::open_apart(Kind::Sessions));
        app.key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert_eq!(app.input, "hello/");
        assert_eq!(app.menu.as_ref().unwrap().filter, "x");
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.input, "hello/");
        assert!(app.menu.is_none());

        // The trigger at index 5 opened this one, so what is typed after it is
        // on the line, and escape takes the trigger and the filter back off.
        app.menu = Some(Menu::open(Kind::Skills, 5));
        app.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE));
        assert_eq!(app.input, "hello/y");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.input, "hello/");
        assert!(app.menu.is_some(), "a filter character comes off and the menu stays");
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.input, "hello");
        assert!(app.menu.is_none(), "taking the trigger itself off closes the menu");
    }

    /// A slash with a space on either side is a slash in a sentence. The message
    /// is sendable, and the menu that the slash opened is gone by the time enter
    /// comes: it was a token that ended at the space.
    #[test]
    fn a_slash_in_a_sentence_is_prose_and_sends() {
        let mut app = App::new(None);
        // The caret is at the end throughout, so the trigger rule is satisfied on
        // the `/` and this is the real path a keyboard takes.
        for c in "something ".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        // A replay has no worker, so the menu the trigger opens is the only thing
        // that has to survive here: it is opened by the app itself.
        app.menu = Some(Menu::open(Kind::Commands, app.caret.saturating_sub(1)));
        app.key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
        app.menu = Some(Menu::open(Kind::Commands, app.caret.saturating_sub(1)));
        assert!(app.menu.is_some(), "the trigger opened nothing");

        // The space ends the word, so the menu goes and the space is typed.
        app.key(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
        assert!(app.menu.is_none(), "a space is not a filter");
        assert_eq!(app.input, "something / ");

        more(&mut app, "something else");
        assert_eq!(app.input, "something / something else");
    }

    /// `/cwd ` opens the directories rather than a filter of commands. The list
    /// is a view of the path typed after the word, so the anchor is the space and
    /// what is typed after it is the path; choosing a row writes it and nothing is
    /// sent, because the worker parses the finished line.
    #[test]
    fn the_space_after_cwd_opens_the_directories() {
        let mut app = App::new(None);
        typed(&mut app, "/cwd");
        app.menu = Some(Menu::open(Kind::Commands, 0));
        app.key(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
        let open = app.menu.as_ref().expect("no list after /cwd ");
        assert_eq!(open.kind, Kind::Dirs);
        assert_eq!(open.anchor, 5, "the path starts after the space");
        assert_eq!(open.filter, "");

        // A directory to walk into writes its path with the slash that means
        // "keep going", and the list stays up at it.
        let mut walk = Menu::open(Kind::Dirs, 5);
        walk.fill(vec![menu::Item::new("work", "/home/me/work", "/home/me/work/")]);
        app.menu = Some(walk);
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.input, "/cwd /home/me/work/");
        assert_eq!(app.menu.as_ref().map(|m| m.kind), Some(Kind::Dirs),
                   "walking in keeps the list");

        // The directory itself is the answer: that row writes it, the list goes,
        // and enter after it is what sends the command.
        let mut done = Menu::open(Kind::Dirs, 5);
        done.fill(vec![menu::Item::new("use this directory", "/home/me/work", "/home/me/work")]);
        app.menu = Some(done);
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.input, "/cwd /home/me/work");
        assert!(app.menu.is_none());

        // And a path with a space in it goes on the line quoted, or the worker reads
        // it back as two words and sets nothing.
        let mut spaced = Menu::open(Kind::Dirs, 5);
        spaced.fill(vec![menu::Item::new(
            "use this directory",
            "/home/my work",
            "/home/my work",
        )]);
        app.menu = Some(spaced);
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.input, "/cwd \"/home/my work\"");
    }

    /// Enter at a menu whose filter matches nothing sends the message instead of
    /// swallowing the key. The word was never a command, which is the worker's own
    /// reading of it too.
    #[test]
    fn enter_on_a_menu_with_no_row_sends_what_was_typed() {
        let mut app = App::new(None);
        typed(&mut app, "a /notacommand");
        // A trigger's menu, with the row list it would have been given.
        let mut open = Menu::open(Kind::Commands, 2);
        open.fill(menu::with_local(vec![menu::Item::new("model", "which model", "/model")]));
        app.menu = Some(open);
        for c in "notacommand".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        assert!(app.menu.as_ref().is_some_and(|open| open.chosen().is_none()));
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        // It was sent, so the field is empty -- and there is no worker here to
        // take it, which is what the notice is, not a failure of the send.
        assert!(app.input.is_empty(), "{:?}", app.input);
        assert!(app.menu.is_none());
    }

    /// AltGr is ctrl+alt on Windows and the character it made is in the event.
    /// Every one of them lands in the message instead of being read as a binding.
    #[test]
    fn an_altgr_character_is_typed_rather_than_read_as_a_binding() {
        let mut app = App::new(None);
        let gr = KeyModifiers::CONTROL | KeyModifiers::ALT;
        // What a German layout puts behind AltGr: `@` on Q, `{` on 7, `]` on 9,
        // and the backslash on the key beside the left shift. The `@` is the one
        // that used to open the file menu, and a backslash used to vanish.
        for c in ['@', '{', ']', '\\'] {
            app.key(KeyEvent::new(KeyCode::Char(c), gr));
        }
        assert_eq!(app.input, "@{]\\");
        assert!(app.menu.is_none(), "AltGr+Q opened the file menu");

        // ctrl and alt are still bindings when the character came from a key
        // rather than from AltGr: ctrl+alt+backspace is the word delete.
        let mut app = App::new(None);
        typed(&mut app, "one two");
        app.key(KeyEvent::new(KeyCode::Backspace, gr));
        assert_eq!(app.input, "one ", "ctrl+alt+backspace is the word delete");
    }

    /// What the command line means, including the id that skips the question.
    #[test]
    fn the_resume_flag_reads_as_deck_reads_it() {
        let args = |s: &str| s.split_whitespace().map(String::from).collect::<Vec<_>>();
        assert_eq!(start_of(&args("quik")), Start::Fresh);
        assert_eq!(start_of(&args("quik --resume")), Start::Ask);
        assert_eq!(start_of(&args("quik -c")), Start::Ask);
        assert_eq!(start_of(&args("quik --continue")), Start::Ask);
        assert_eq!(start_of(&args("quik --resume abc")), Start::This("abc".into()));
        assert_eq!(start_of(&args("quik --resume=abc")), Start::This("abc".into()));
        // A following flag is not an id.
        assert_eq!(start_of(&args("quik --resume --fixture f.jsonl")), Start::Ask);
        assert_eq!(start_of(&args("quik -r abc --print")), Start::This("abc".into()));
    }

    /// A launch can carry the message to open with. A lone word that names a
    /// file is that file's contents; anything else is words, because every word
    /// after the first is part of the message.
    #[test]
    fn the_message_is_the_first_thing_that_is_not_a_flag() {
        // The binary's own name is skipped before the args get here, as it is in
        // main.
        let args = |s: &str| {
            s.split_whitespace().skip(1).map(String::from).collect::<Vec<_>>()
        };
        let dir = std::env::temp_dir().join(format!("quik-args-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("prompt.txt");
        std::fs::write(&file, "read this file, then do what it says\n").unwrap();
        let blank = dir.join("blank.txt");
        std::fs::write(&blank, "  \n\n").unwrap();
        let path = file.to_str().unwrap();

        assert_eq!(opening_message(&args("quik")), None);
        assert_eq!(opening_message(&args("quik --resume")), None);
        // A flag's own value is not the message, and skipping it must not eat a
        // word that follows a flag that takes none.
        assert_eq!(opening_message(&args("quik --home /tmp --resume")), None);
        assert_eq!(opening_message(&args("quik --refresh")), None);
        assert_eq!(opening_message(&args(&format!("quik {path}"))), Some("read this file, then do what it says\n".into()));
        // A blank file is no message: there would be nothing to send.
        assert_eq!(opening_message(&args(&format!("quik {}", blank.display()))), None);
        assert_eq!(
            opening_message(&args("quik --resume abc fix the parser")),
            Some("fix the parser".into())
        );
        assert_eq!(
            opening_message(&args("quik why is this failing")),
            Some("why is this failing".into()),
            "a word that names no file is a word"
        );
        assert_eq!(
            opening_message(&args("quik -- --not-a-flag just words")),
            Some("--not-a-flag just words".into())
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A message that starts no loop must leave no clock. Sending starts one
    /// optimistically, because the frontend cannot know what the text is yet;
    /// the worker's `idle` is what takes it back. A round that ran keeps its
    /// readout, so the two endings are told apart rather than both clearing.
    #[test]
    fn a_round_that_never_ran_leaves_no_clock() {
        let mut app = App::new(None);
        app.track(&State::Running, None, false);
        assert!(app.turn.is_some(), "sending puts the clock up");
        app.track(&State::Idle, None, false);
        assert!(app.turn.is_none(), "idle takes it back down");

        app.track(&State::Running, Some(120), false);
        app.track(&State::Tools, Some(120), false);
        app.track(&State::Done, Some(120), false);
        let kept = app.turn.as_ref().expect("a round that ran keeps its readout");
        assert_eq!(kept.tokens, Some(120));
        assert!(kept.ended.is_some());
    }

    /// The status row in each state, and the colour of its dots: the row is drawn in
    /// a live pane only -- it needs a worker's snapshot -- so this is how it is read
    /// without one.
    ///
    ///   cargo test -p cuacode-quik -- --ignored --nocapture show_the_status_row
    #[test]
    #[ignore]
    fn show_the_status_row() {
        let mut snap = Snapshot::default();
        snap.mode = "auto".into();
        snap.context_used = 8_400;
        snap.context_max = 20_000;
        snap.tps = 128.0;
        for (state, ms) in [
            (State::Idle, 0u128),
            (State::Running, 360),
            (State::Tools, 600),
            (State::Error, 0),
        ] {
            snap.state = state.clone();
            for width in [100usize, 76] {
                let line = status_line(&snap, width, ms);
                let said: String = line.spans.iter().map(|s| s.content.to_string()).collect();
                let dots: Vec<String> = line
                    .spans
                    .iter()
                    .take(scan::DOT_CELLS)
                    .map(|s| format!("{:?} {:?}", s.style.fg, s.content))
                    .collect();
                println!("{state:?} @{width}  {said}");
                println!("  {}", dots.join(" "));
            }
        }
    }

    /// The round's readout has a row of its own: the box's own row, under the message
    /// and inside the two rules, where the three facts about who is answering live. The
    /// message stops owing it cells, so it wraps at the box's whole width now.
    #[test]
    fn the_readout_rides_the_boxs_own_row() {
        let mut app = App::new(None);
        typed(&mut app, &"x".repeat(30));
        app.turn = Some(Turn {
            started: Instant::now(),
            ended: None,
            tokens: Some(12345),
            estimated: true,
        });
        let (w, h) = (60u16, 12u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> =
            (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        let field = lines.iter().position(|l| l.contains("xxx")).expect("no message row");
        let at = lines
            .iter()
            .position(|l| l.contains("~12.3k tok"))
            .expect("the readout is not on screen");
        // It is the row under the message, carrying the box's own line, and the message
        // above it is not drawn under it: the clock has a row of its own now.
        assert_eq!(at, field + 1, "the box's own row is the row under the message");
        assert!(lines[at].starts_with('\u{2502}'), "the rail runs through it: {:?}", lines[at]);
        assert!(lines[at].trim_end().ends_with('s'), "{:?}", lines[at]);
        assert!(!lines[field].contains("~12.3k tok"), "the clock is back on the message: {:?}", lines[field]);
    }

    /// The field is an editor, not a line that only grows at its end: the caret
    /// moves, a word at a time with the modifiers every terminal sends, and a
    /// character comes out from under the caret rather than off the end.
    #[test]
    fn the_caret_moves_and_edits_where_it_stands() {
        let mut app = App::new(None);
        typed(&mut app, "one two three");
        assert_eq!(app.caret, 13);
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        assert_eq!(app.caret, 11);
        // A new line lands where the caret is, not at the end of the message.
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        assert_eq!(app.input, "one two thr\nee");
        assert_eq!(app.lines(), vec!["one two thr", "ee"]);
        // The same key in the other encoding a terminal puts it in: VS Code's
        // terminal sends shift-enter as ESC CR, which is alt-enter here, and
        // both means the same thing to this field.
        typed(&mut app, "abc");
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT));
        assert_eq!(app.input, "abc\n");

        // A word at a time, both ways: ctrl and alt are the same gesture.
        typed(&mut app, "one two three");
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL));
        assert_eq!(app.caret, 8);
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL));
        assert_eq!(app.caret, 4);
        app.key(KeyEvent::new(KeyCode::Right, KeyModifiers::ALT));
        assert_eq!(app.caret, 8);
        // The ends of the line, which is the whole message while it is one.
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::SUPER));
        assert_eq!(app.caret, 0);
        app.key(KeyEvent::new(KeyCode::Right, KeyModifiers::SUPER));
        assert_eq!(app.caret, 13);

        // Backspace takes what is behind the caret and delete what is on it.
        typed(&mut app, "abc");
        app.key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.input, "ac");
        assert_eq!(app.caret, 1);
        app.key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE));
        assert_eq!(app.input, "a");

        // Home and end are the line's ends while there is a message, and the
        // conversation's when the box is empty. The command key does the same
        // thing wherever the terminal reports it, which is the mac spelling.
        typed(&mut app, "abc");
        app.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        assert_eq!(app.caret, 0);
        assert!(app.follow, "the field had the key, not the conversation");
        app.key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert_eq!(app.caret, 3);
        app.input.clear();
        app.caret = 0;
        app.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        assert!(!app.follow, "an empty box leaves the conversation the key");

        // The backup spelling, and the only one a terminal reporting neither
        // encoding can send: raw mode leaves 0x0A unambiguous.
        typed(&mut app, "abc");
        app.key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL));
        assert_eq!(app.input, "abc\n");
    }

    /// A message of more than one line: the box grows a line at a time, the
    /// prompt is only on the first, and past its ceiling the window follows the
    /// caret rather than the end of the message.
    #[test]
    fn a_multi_line_message_grows_the_box_and_scrolls_to_the_caret() {
        let mut app = App::new(None);
        typed(&mut app, "first\nsecond");
        assert_eq!(app.field_height(40), 2);
        assert!(app.multiline());

        let (w, h) = (40u16, 12u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> = (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        let first = lines.iter().position(|l| l.contains('\u{2502}')).expect("no field line");
        assert!(said(&lines[first], w as usize).starts_with('\u{2502}'), "{:?}", lines[first]);
        assert!(said(&lines[first + 1], w as usize).starts_with("\u{2502} second"), "{:?}", lines[first + 1]);

        // Ten lines is more than the box will show, so the window moves to the
        // line the caret is on and the first line goes off the top of it.
        typed(&mut app, &(1..=10).map(|n| format!("row {n}")).collect::<Vec<_>>().join("\n"));
        assert_eq!(app.field_height(40), FIELD_MAX as u16, "the box stops growing");
        let (w, h) = (40u16, 20u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> = (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        assert!(lines.iter().any(|l| l.contains("row 10")), "{}", lines.join("\n"));
        assert!(!lines.iter().any(|l| l.contains("row 1 ")), "{}", lines.join("\n"));
    }

    /// A single long line wraps instead of running off the edge: the box grows a
    /// row, the continuation starts under the text rather than under the prompt,
    /// and no row is wider than the box.
    #[test]
    fn a_long_line_wraps_and_the_box_grows_a_row() {
        let mut app = App::new(None);
        let words = "alpha beta gamma delta epsilon zeta eta theta iota kappa";
        typed(&mut app, words);
        assert!(!app.multiline(), "one line, however many rows it takes");
        // A 30-wide box gives the message 28 columns, and these ten words want
        // three rows of them.
        assert_eq!(app.field_height(30), 3, "the box grew a row per wrapped line");
        let (w, h) = (30u16, 16u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> =
            (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        let first = lines.iter().position(|l| l.contains('\u{2502}')).expect("no field line");
        let field: Vec<&String> = lines[first..first + 3].iter().collect();
        // Every row of the box carries its line, and each one starts with a word rather
        // than with half of one.
        assert!(said(field[0], 30).starts_with('\u{2502}'), "{:?}", field[0]);
        assert!(said(field[1], 30).starts_with("\u{2502} "), "{:?}", field[1]);
        assert!(said(field[2], 30).starts_with("\u{2502} "), "{:?}", field[2]);
        // Everything typed is on the screen, word for word: wrapping is not
        // clipping, and it is not cutting a word at the edge either.
        let shown = field.iter().map(|l| l.trim_end()).collect::<Vec<_>>().join(" ");
        for word in words.split(' ') {
            assert!(shown.contains(word), "{word} is not whole on the screen: {shown:?}");
        }
        // And not one row is wider than the box, which is the thing that was
        // wrong before: the text ran on past the frame forever.
        for l in &lines {
            assert!(l.chars().count() <= w as usize, "a row ran off the box: {l:?}");
        }
    }

    /// A chip is moved whole to the next row rather than being cut at the edge of
    /// the box: half of `[image 1]` on one row and half on the next would be a
    /// label that is not what gets sent.
    #[test]
    fn a_wrap_never_cuts_a_chip_in_half() {
        let mut app = App::new(None);
        // A word that fills most of the row, so the chip has nowhere to sit but
        // the next one, then enough after it to push it over again.
        typed(&mut app, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        shot(&mut app, "a.png");
        let (w, h) = (30u16, 14u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> =
            (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        let rows: Vec<&String> = lines
            .iter()
            .filter(|l| {
                let l = l.trim_start_matches('\u{2503}');
                l.starts_with("\u{2502} ") || l.starts_with('\u{276f}')
            })
            .collect();
        // The label is on one row and undivided, wherever the wrap fell.
        assert_eq!(
            rows.iter().filter(|l| l.contains("[image 1]")).count(),
            1,
            "the chip is not whole on one row: {rows:?}"
        );
        assert!(!rows.iter().any(|l| l.contains("[image") && !l.contains("[image 1]")), "{rows:?}");
    }

    /// The row the caret is on is the row it was drawn in, so a caret at the end
    /// of a line that wrapped puts the box's window where the typing is.
    #[test]
    fn the_box_follows_the_caret_through_a_wrap() {
        let mut app = App::new(None);
        // Eight rows' worth of a 20-wide box, so the ceiling is passed and the
        // window has to move, with the caret at the very end.
        typed(&mut app, &"word ".repeat(60));
        assert_eq!(app.field_height(20), FIELD_MAX as u16);
        let (w, h) = (20u16, 20u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let lines: Vec<String> =
            (0..h).map(|y| (0..w).map(|x| buf[(x, y)].symbol()).collect()).collect();
        // The last row on screen is the one the caret is on, and the caret's own
        // bar is at its end.
        // The caret is drawn, it is inside the composer, and it is on the last of the
        // message's rows: the window followed it and the foot is the row under it.
        let caret = lines.iter().position(|l| l.contains('\u{258f}')).expect("no caret row");
        // Inside the box, which is two rules: the row under the caret closes it.
        assert!(
            lines[caret + 1].starts_with('\u{2500}'),
            "the caret is not on the box's last row: {:?}",
            lines[caret + 1]
        );
        assert!(
            lines[caret].starts_with("\u{2502} "),
            "a continuation row carries the box's own line: {:?}",
            lines[caret]
        );
    }

    /// `/help` is the door to the keys, and it opens a panel like any other: the
    /// worker has no help command, so a line that only said `/help` would go to
    /// the model as prose unless the frontend reads it first.
    #[test]
    fn help_opens_the_keys_instead_of_going_to_the_model() {
        let mut app = App::new(None);
        typed(&mut app, "/help");
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let open = app.opened.as_ref().expect("help did not open");
        assert_eq!(open.title, "keys");
        let text: String = open.lines.iter().map(Line::to_string).collect::<Vec<_>>().join("\n");
        assert!(text.contains("ctrl+o") && text.contains("ctrl+b"), "{text}");
        // A key list is keys. The commands are the `/` menu's own list, and
        // naming one here would be the only command on a screen with room for
        // none of the others.
        assert!(!text.contains("/permissions"), "a command on the keys: {text}");
        assert!(!text.contains("/help"), "the panel naming itself: {text}");
        // It is a panel, so escape is what closes it, and nothing was said to
        // the conversation on the way in.
        assert!(app.log.rows.is_empty(), "help went into the transcript");
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.opened.is_none());
    }

    /// The dispatch reads the map. A key rebound in `keys.json` is that action, its
    /// old key is nothing, and quit moves with it even though quit is asked before
    /// the panels: what the file says is what the screen does, which is the whole
    /// point of the table.
    #[test]
    fn a_rebound_key_runs_its_action_and_the_old_key_stops() {
        let path = std::env::temp_dir().join(format!("quik-keys-dispatch-{}.json", std::process::id()));
        std::fs::write(&path, r#"{ "fold": "f2" }"#).unwrap();
        let mut app = App::new(None);
        app.keys = keys::load(&path).0;
        app.key(KeyEvent::new(KeyCode::F(2), KeyModifiers::NONE));
        assert!(app.open, "f2 did not fold");
        app.key(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL));
        assert!(app.open, "ctrl+t folded, and the file moved it");
        // And leaving, which is asked before the panels rather than with the rest.
        std::fs::write(&path, r#"{ "quit": "f9" }"#).unwrap();
        app.keys = keys::load(&path).0;
        assert!(!app.key(KeyEvent::new(KeyCode::F(9), KeyModifiers::NONE)), "f9 did not quit");
        std::fs::remove_file(&path).ok();
    }

    /// `/permissions` is a command rather than a key: on its own it opens the
    /// four modes with the one in use marked, and a mode after it sets the mode
    /// directly. Neither way says anything to the conversation.
    #[test]
    fn permissions_is_a_command_with_the_four_modes_behind_it() {
        let mut app = App::new(None);
        typed(&mut app, "/permissions");
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.menu.as_ref().expect("no modes").kind, Kind::Permissions);
        let modes = menu::modes("ask");
        let rows: Vec<&str> = modes.iter().map(|i| i.label.as_str()).collect();
        assert_eq!(rows, ["ask", "auto", "bypass", "off"]);
        assert!(modes[0].hint.contains("acting now"), "the mode in use is marked");
        assert!(modes[1].hint.is_empty(), "the others are not");
        assert!(app.log.rows.is_empty(), "the command was said to the conversation");
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.menu.is_none());

        // Spelled out it is the command, not the list, and a mode it does not
        // have is answered rather than swallowed.
        typed(&mut app, "/permissions off");
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.menu.is_none());
        assert!(app.log.rows.is_empty());
        typed(&mut app, "/permissions sideways");
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let said = app.log.rows.last().cloned().expect("nothing was said");
        assert!(matches!(&said, Row::Notice { head, .. } if head.contains("no mode called")), "{said:?}");
    }

    /// The `/` menu as it is drawn: the worker's own list, with this screen's two
    /// verbs in front of it.
    ///
    ///   cargo test -p cuacode-quik -- --ignored --nocapture show_the_command_menu
    #[test]
    #[ignore]
    fn show_the_command_menu() {
        let mut app = App::new(None);
        seed(&mut app);
        app.commands = vec![
            Item::new("help", "the keys", "/help"),
            Item::new("permissions", "ask, auto, bypass, or off", "/permissions"),
            Item::new("provider", "which provider answers, from here on", "/provider"),
            Item::new("model", "which model that provider runs", "/model"),
            Item::new("vision", "which provider looks at pictures", "/vision"),
            Item::new("effort", "how hard it thinks: off low medium high max", "/effort"),
            Item::new("cwd", "where the next conversation stands", "/cwd"),
            Item::new("compact", "fold the conversation now", "/compact"),
            Item::new("context", "how much of the window the conversation fills", "/context"),
            Item::new("usage", "what this and every other session has cost", "/usage"),
            Item::new("version", "which build of cuacode this worker runs", "/version"),
        ];
        app.open_menu(Kind::Commands);
        frame_of(&mut app, 100, 30);
        print!("{}", frame_of(&mut app, 100, 30));
    }

    /// The `/` menu is filled twice: from the rows in hand when it opens, and
    /// again from the list that lands under it. The second fill must keep this
    /// frontend's own verb, or the row being filtered for goes out from under
    /// the keystrokes -- which is what `/help` did the first time. A command that
    /// opens a picker is a row like any other: the list is the worker's own.
    #[test]
    fn the_workers_command_list_keeps_the_frontends_own_verb() {
        let mut app = App::new(None);
        app.open_menu(Kind::Commands);
        app.feed(
            wire::parse(
                r#"{"type":"commands","id":"a","data":{"commands":[{"name":"model","description":"which model"},{"name":"usage","description":"what it cost"}]}}"#,
            )
            .unwrap(),
        );
        let open = app.menu.as_ref().expect("the menu closed");
        let rows: String = open.lines(60).iter().map(Line::to_string).collect::<Vec<_>>().join("\n");
        assert!(rows.contains("help"), "{rows}");
        assert!(rows.contains("permissions"), "this screen's own verb: {rows}");
        assert!(rows.contains("model"), "the worker's list is the menu: {rows}");
        assert!(rows.contains("usage"), "{rows}");
    }

    /// ctrl-o opens the calls: the conversation's own rows, newest first, so a
    /// call that is not the newest is still reachable. Choosing one opens it from
    /// the row's own copy -- a replay has no worker to ask for the rest of it.
    #[test]
    fn ctrl_o_lists_the_calls_and_opens_the_one_chosen() {
        let mut app = App::new(None);
        seed(&mut app);
        // A second call, so "newest" is a choice rather than the only row there is.
        app.feed(wire::parse(r#"{"type":"token","id":"a","data":{"state":"tool_calls","token":[{"name":"file","args":{"action":"write","path":"a.py","content":"one\ntwo"}}]}}"#).unwrap());
        app.feed(wire::parse(r#"{"type":"token","id":"a","data":{"state":"tool_output","token":"file","result":{"result":{"created":true}}}}"#).unwrap());

        // A frame first, the way the loop runs: a panel is sized from the width
        // the screen last drew at, and with no frame there is no width to size to.
        let mut term = Terminal::new(TestBackend::new(60, 20)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();

        app.key(KeyEvent::new(KeyCode::Char('o'), KeyModifiers::CONTROL));
        let open = app.menu.as_ref().expect("ctrl-o opened no menu");
        assert_eq!(open.kind, Kind::Calls);
        let rows: Vec<String> = open.lines(60).iter().map(Line::to_string).collect();
        assert!(rows[0].contains("write(a.py)"), "newest first: {:?}", rows[0]);
        assert!(rows.iter().any(|r| r.contains("Bash(date)")), "{rows:?}");
        assert!(app.input.is_empty(), "a picker writes nothing on the line");

        // Choosing the row under the cursor opens that call in full.
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.menu.is_none());
        let panel = app.opened.as_ref().expect("nothing opened");
        assert_eq!(panel.title, "write(a.py)");
        let text: String = panel.lines.iter().map(Line::to_string).collect::<Vec<_>>().join("\n");
        assert!(text.contains("\u{256d} content \u{b7} 2 lines "), "{text}");
        assert!(text.contains("\u{2502} one") && text.contains("\u{2502} two"), "{text}");

        // Escape is what closes a panel, however it was opened.
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.opened.is_none());
    }

    /// Two thoughts and the call between them: one step, opened on ctrl-t, with
    /// its thinking folded to a line each.
    fn a_step_with_two_thoughts(app: &mut App) {
        for line in [
            r#"{"type":"token","id":"a","data":{"state":"thinking","token":"Look at the loader first. Then read the file."}}"#,
            r#"{"type":"token","id":"a","data":{"state":"tool_calls","token":[{"name":"Bash","args":{"command":"ls"}}]}}"#,
            r#"{"type":"token","id":"a","data":{"state":"tool_output","token":"Bash","result":{"result":{"exit_code":0,"stdout":"a\n"}}}}"#,
            r#"{"type":"token","id":"a","data":{"state":"thinking","token":"Now the loader is clear. Write the patch."}}"#,
        ] {
            app.feed(wire::parse(line).unwrap());
        }
    }

    /// The screen row a transcript row was drawn on, off the frame's own numbers.
    fn line_of(app: &App, row: usize) -> u16 {
        let at = app
            .anchors
            .iter()
            .position(|&(r, _)| r == row)
            .expect("the row drew");
        app.body_top + app.spans[at].0 as u16 - app.scroll
    }

    /// A double click on a thought's line opens that thought and leaves the step
    /// open under it; the step's own first line is what folds the step. Without the
    /// distinction the head's own thinking could not be opened at all, and a line
    /// of reasoning would be the only way to shut the column it sits in.
    #[test]
    fn a_double_click_opens_a_thought_and_the_head_still_folds_the_step() {
        let mut app = App::new(None);
        a_step_with_two_thoughts(&mut app);
        let mut term = Terminal::new(TestBackend::new(70, 20)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        app.key(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL));
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(app.shows(0, &app.log.rows[0]), "ctrl-t did not open the step");
        assert!(!app.thought_shows(2), "the thinking opened with it");

        let at = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 8,
            row: line_of(&app, 2),
            modifiers: KeyModifiers::NONE,
        };
        app.mouse(at);
        app.mouse(at);
        assert!(app.thought_shows(2), "the click missed the thought");
        assert!(app.shows(0, &app.log.rows[0]), "a thought folded its step");

        // The head line is the step, so pressing it twice shuts the whole column.
        let head = MouseEvent {
            row: line_of(&app, 0),
            ..at
        };
        app.mouse(head);
        app.mouse(head);
        assert!(!app.shows(0, &app.log.rows[0]), "the head did not fold the step");

        // ctrl-t is all of them at once, and it remembers none of them: the step
        // the head folded, and the thought opened by hand, are both back the way the
        // column opens.
        app.key(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL));
        assert!(!app.shows(0, &app.log.rows[0]), "ctrl-t kept a step open");
        assert!(!app.thought_shows(2), "a thought outlived ctrl-t");
        app.key(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL));
        assert!(app.shows(0, &app.log.rows[0]), "ctrl-t kept the head's fold");
    }

    /// A thought is in the calls list, and choosing one opens the step it is in
    /// first: the line it folds to is only drawn once the step is open, so an
    /// entry that only unfolded the thought would open nothing on screen.
    #[test]
    fn choosing_a_thought_opens_the_step_before_the_thought() {
        let mut app = App::new(None);
        a_step_with_two_thoughts(&mut app);
        let mut term = Terminal::new(TestBackend::new(70, 20)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(!app.shows(0, &app.log.rows[0]), "the screen opens folded");

        app.key(KeyEvent::new(KeyCode::Char('o'), KeyModifiers::CONTROL));
        // Newest first, so the thought the step is named for is the top entry.
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.menu.is_none(), "the picker stayed up");
        assert!(app.shows(0, &app.log.rows[0]), "the step did not open");
        assert!(app.thought_shows(2), "the thought did not open");
    }

    /// A panel's scroll is clamped to what it holds. The offset counts the lines
    /// on the screen rather than the ones the panel was built from, because a
    /// long line is several of them -- and a call that fits must not scroll off
    /// into the void.
    #[test]
    fn a_panel_scrolls_no_further_than_it_has_to() {
        let mut app = App::new(None);
        let long = (0..200)
            .map(|n| format!("line {n}, padded out so it wraps in a narrow box"))
            .collect::<Vec<_>>()
            .join("\n");
        let call = serde_json::json!({"action": "write", "path": "a.py", "content": long});
        app.opened = Some(view::call_from_row("file", "path=a.py", &call, Some("created"), 40));
        app.opened.as_mut().unwrap().scroll = u16::MAX;
        let (w, h) = (40u16, 12u16);
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let max = app.opened.as_ref().unwrap().scroll;
        assert!(max > 0, "a panel taller than the box has somewhere to scroll to");
        // It stays where it was put rather than creeping on every draw.
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert_eq!(app.opened.as_ref().unwrap().scroll, max);

        // A call with less in it than the box has room for is pinned at the top.
        let short = serde_json::json!({"action": "read", "path": "a.py"});
        app.opened = Some(view::call_from_row("file", "path=a.py", &short, None, 40));
        app.opened.as_mut().unwrap().scroll = u16::MAX;
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert_eq!(app.opened.as_ref().unwrap().scroll, 0);
    }
    /// A question the model asked draws its own panel, and the screen says so.
    ///
    /// The bug this pins: `is_question` was decided by "did any options arrive",
    /// so a question with no options fell through to the permission line and its
    /// yes/no keys -- the one case that most needs a text box was the one that did
    /// not get one. The envelope now carries `kind`, and this holds the drawing to
    /// it: a question shows its own text and an empty answer row, a permission
    /// still shows `allow ... ? y / n`.
    #[test]
    fn a_question_draws_a_question_and_a_permission_still_draws_permission() {
        let frame = frame_of;

        // 1. A question with no options: a text box, and nothing about allowing.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q1","data":{"name":"ask_user","kind":"question","question":"what should this be called?","options":[]}}"#).unwrap());
        settle(&mut app);
        let shown = frame(&mut app, 80, 24);
        assert!(shown.contains("what should this be called?"), "{shown}");
        assert!(!shown.contains("allow ask_user?"), "question drew the permission line:\n{shown}");
        // The caret is in the answer row, because there is no list to pick from.
        assert!(app.prompt.as_ref().is_some_and(|a| a.is_question() && a.on_custom()), "{shown}");

        // 2. A question with options: numbered rows, plus the custom row.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q2","data":{"name":"ask_user","kind":"question","question":"which file?","options":["main.rs","lib.rs"]}}"#).unwrap());
        settle(&mut app);
        let shown = frame(&mut app, 80, 24);
        if std::env::var("SHOW_QUESTION_PANEL").is_ok() { println!("{shown}"); }
        assert!(shown.contains("which file?") && shown.contains("main.rs") && shown.contains("lib.rs"), "{shown}");
        assert!(!shown.contains("allow ask_user?"), "question drew the permission line:\n{shown}");

        // 2b. The panel is called a question, not by the tool's name, and says
        //     which one of how many.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q2b","data":{"name":"ask_user","kind":"question","question":"which file?","options":["a","b"],"n":2,"of":3}}"#).unwrap());
        settle(&mut app);
        let shown = frame(&mut app, 80, 24);
        assert!(shown.contains("question 2 of 3"), "no count in the title:\n{shown}");
        assert!(!shown.contains("ask_user"), "the tool name is still on the panel:\n{shown}");
        // With the cursor up in the list, the answer row says it is there.
        assert!(shown.contains("or type your own"), "{shown}");

        // 2c. Typing moves the cursor down and the row becomes the field, with a
        //     caret and the hint that enter sends it.
        for c in "notes.txt".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        let shown = frame(&mut app, 80, 24);
        if std::env::var("SHOW_QUESTION_PANEL").is_ok() { println!("{shown}"); }
        assert!(shown.contains("notes.txt"), "typed text not drawn:\n{shown}");
        assert!(shown.contains("enter sends it"), "no hint while typing:\n{shown}");
        assert!(!shown.contains("or type your own"), "still prompting for input it has:\n{shown}");

        // 2d. A digit picks an option while the list has the cursor, and is text
        //     once something is being typed.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q2d","data":{"name":"ask_user","kind":"question","question":"pick","options":["one","two"]}}"#).unwrap());
        settle(&mut app);
        app.key(KeyEvent::new(KeyCode::Char('2'), KeyModifiers::NONE));
        assert_eq!(app.prompt.as_ref().unwrap().picked, 1, "digit did not pick");
        app.key(KeyEvent::new(KeyCode::Char('1'), KeyModifiers::NONE));
        assert_eq!(app.prompt.as_ref().unwrap().picked, 0, "digit did not move the list");

        // 3. A permission is a decision, not a line: the same two rows a question
        //    gets, with the reason and the call it is about behind them.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"p1","data":{"name":"shell","kind":"permission","args":{"command":"rm x"},"note":"the command is irreversible"}}"#).unwrap());
        settle(&mut app);
        let shown = frame(&mut app, 80, 24);
        if std::env::var("SHOW_QUESTION_PANEL").is_ok() { println!("{shown}"); }
        assert!(shown.contains("allow shell?"), "permission lost its title:\n{shown}");
        assert!(
            shown.contains("the command is irreversible"),
            "the reason is not on the panel:\n{shown}"
        );
        assert!(
            shown.contains("shell(rm x)"),
            "the call is not readable from it:\n{shown}"
        );
        // Each row names the keys that send it, so the pair needs no hint under them.
        assert!(
            shown.contains("1. allow \u{b7} enter, y") && shown.contains("2. refuse \u{b7} n, esc"),
            "the answers are not rows with their keys:\n{shown}"
        );
        assert!(!shown.contains("y / n") && !shown.contains("allows \u{b7} n refuses"), "{shown}");
        assert!(app.prompt.as_ref().is_some_and(|a| !a.is_question()));
        // The arrows move between them, and enter takes the row under the cursor.
        app.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(app.prompt.as_ref().unwrap().picked, 1, "the arrows do nothing");
        app.key(KeyEvent::new(KeyCode::Char('2'), KeyModifiers::NONE));
        assert_eq!(app.prompt.as_ref().unwrap().picked, 1, "a digit does not pick");
        app.key(KeyEvent::new(KeyCode::Char('1'), KeyModifiers::NONE));
        assert_eq!(app.prompt.as_ref().unwrap().picked, 0, "a digit does not pick");

        // 4. An envelope from a worker that predates `kind` still lands right: the
        //    question mark is the field only a question carries.
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q3","data":{"name":"ask_user","args":{"question":"still a question?"},"question":"still a question?"}}"#).unwrap());
        settle(&mut app);
        assert!(app.prompt.as_ref().is_some_and(|a| a.is_question()), "old envelope misread");
    }

    /// A question too long for one row, a choice too long for one line, and an
    /// answer typed across several: all of it inside the box, and the box as tall
    /// as what it holds.
    ///
    /// The bug this pins: the height was "options plus five", worked out before
    /// anything was wrapped, so the wrapped question's own rows pushed the answer
    /// row and the hint out of the bottom of the box -- and a choice longer than
    /// the border was cut off at it, mid word.
    #[test]
    fn a_long_question_and_a_long_answer_stay_inside_their_box() {
        let long = "should the panel keep the whole question when the wrapping needs more rows than the option count ever suggested";
        let choice = "keep the question whole and let the box grow to the rows it needs";
        let mut app = App::new(None);
        let said = serde_json::json!({
            "type": "permission",
            "id": "q",
            "data": {
                "name": "ask_user",
                "kind": "question",
                "question": long,
                "options": [choice, "cut it off and say nothing"],
            },
        });
        app.feed(wire::parse(&said.to_string()).unwrap());
        settle(&mut app);
        let shown = frame_of(&mut app, 80, 24);
        if std::env::var("SHOW_QUESTION_PANEL").is_ok() {
            println!("{shown}");
        }
        for word in long.split(' ').chain(choice.split(' ')) {
            assert!(shown.contains(word), "a word of it was cut off: {word}\n{shown}");
        }

        // Typed across two lines through both newline keys, since a terminal that
        // reports a shifted enter and one that maps the key to `1b 0d` send a
        // different event for it, and ctrl-j is the one that arrives everywhere.
        for c in "one line".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
        app.key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL));
        for c in "and a second".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        assert_eq!(
            app.prompt.as_ref().unwrap().typed,
            "one line\n\nand a second",
            "a newline key did not break the line"
        );

        let shown = frame_of(&mut app, 80, 24);
        if std::env::var("SHOW_QUESTION_PANEL").is_ok() {
            println!("{shown}");
        }
        assert!(shown.contains("and a second"), "the answer's last line is not drawn:\n{shown}");
        assert!(shown.contains('\u{2588}'), "the caret left the box:\n{shown}");
        assert!(
            shown.contains("esc clears"),
            "the hint is the box's last row and it was clipped:\n{shown}"
        );

        // And the fitting itself, measured rather than read off a frame: never
        // more rows than the room it was given, never a row wider than the box.
        let asked = app.prompt.clone().unwrap();
        for (w, h) in [(66usize, 20usize), (40, 12), (34, 5)] {
            let lines = ask_panel(&asked, w, h);
            assert!(lines.len() <= h, "{w}x{h}: {} rows in {h}", lines.len());
            for line in &lines {
                assert!(
                    line.width() <= w,
                    "{w}x{h}: {:?} is {} wide",
                    line.to_string(),
                    line.width()
                );
            }
        }

        // A long answer shows its tail, which is where the caret is: the head of
        // it going off the top is the field scrolling, not the box losing rows.
        let mut many = asked.clone();
        many.typed = "one\ntwo\nthree\nfour\nfive\nsix".to_string();
        let lines: Vec<String> = ask_panel(&many, 40, 20).iter().map(Line::to_string).collect();
        assert!(!lines.iter().any(|l| l.contains("one")), "the head of a long answer is drawn: {lines:?}");
        assert!(lines.iter().any(|l| l.contains("six")), "the caret's own row is not drawn: {lines:?}");
    }

    /// A character typed at a question goes into the answer, not into a dead arm.
    #[test]
    fn typing_at_a_question_builds_the_answer() {
        let mut app = App::new(None);
        app.feed(wire::parse(r#"{"type":"permission","id":"q","data":{"name":"ask_user","kind":"question","question":"name it","options":[]}}"#).unwrap());
        settle(&mut app);
        for c in "widget".chars() {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        // Backspace takes one back, as a text field does.
        app.key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        let asked = app.prompt.clone().expect("still up");
        assert_eq!(asked.typed, "widge", "typed answer: {:?}", asked.typed);
        assert!(asked.on_custom(), "an empty list means the answer row is the only row");
    }

    /// The fold line says something, and stops saying it.
    ///
    /// The complaint this pins: `/compact` runs on the main loop, outside
    /// generate(), so there was no outbox for its events and the screen never
    /// learned a fold was happening. The status text is what the frame draws off
    /// the snapshot, so this asserts the snapshot produces the line -- the frame
    /// itself only reserves a status row when there is one.
    #[test]
    fn a_fold_puts_a_line_on_the_status_row_and_takes_it_away() {
        use cuacode::session::{compact_state, Snapshot};

        // What the worker's start event does to the snapshot. Without the state
        // moving to Running the frame reserves no status row, which is the bug:
        // the worker sat in a model call with a screen that looked hung.
        let mut snap = Snapshot::default();
        assert_eq!(snap.state, State::Idle, "nothing is folding yet");

        compact_state(&mut snap, "start", 24, 18_000);
        assert_eq!(snap.state, State::Running, "a fold at the prompt must show as busy");
        assert_eq!(snap.compacting, "start");

        compact_state(&mut snap, "done", 0, 0);
        assert_eq!(snap.state, State::Idle, "a finished fold did not finish a turn");
        assert_eq!(snap.compacting, "done");
    }

    /// A round in flight is the one thing that stops a switch, and the row says why
    /// rather than the key going quietly dead. The rule is the snapshot's, so it is
    /// checked there: a `Session` cannot be built without a worker.
    #[test]
    fn switching_waits_for_the_round_in_flight() {
        let at = |state: &State| Snapshot { state: state.clone(), ..Snapshot::default() };
        for state in [State::Running, State::Tools] {
            assert_eq!(
                switch_refusal(Some(&at(&state))),
                Some("a round is still running, escape stops it"),
                "{state:?} is a round in flight"
            );
        }
        for state in [State::Idle, State::Done, State::Cancelled, State::Error] {
            assert_eq!(switch_refusal(Some(&at(&state))), None, "{state:?} is at rest");
        }
        assert_eq!(switch_refusal(None), None, "a replay has no conversation to leave");
    }

    /// The conversation left behind is not carried into the next one. The worker's own
    /// `status {state:"session"}` is what empties the screen -- so a load that failed,
    /// which answers `error`, leaves the conversation on screen alone -- and the draft
    /// in the composer is not part of a conversation.
    #[test]
    fn a_swap_clears_the_conversation_it_left() {
        let mut app = App::new(None);
        seed(&mut app);
        typed(&mut app, "half a thought");
        app.figures.insert(0, view::Round { tokens: 120, tps: 40.0, estimated: false });
        app.overrides.insert(0, true);
        app.thoughts.insert(1, true);
        app.open = true;
        app.follow = false;
        app.scroll = 7;
        app.turn = Some(Turn::starting());
        assert!(!app.log.rows.is_empty(), "nothing to clear");

        app.feed(
            wire::parse(
                r#"{"type":"status","id":"c2","data":{"state":"session","session_id":"other","provider":"fake","model":"m","msg_count":2}}"#,
            )
            .unwrap(),
        );

        assert!(app.log.rows.is_empty(), "the rows of the last one are gone");
        assert!(app.figures.is_empty() && app.overrides.is_empty() && app.thoughts.is_empty());
        assert!(app.turn.is_none(), "the composer's clock is the new conversation's");
        assert_eq!(app.scroll, 0);
        assert!(app.follow, "a conversation opens at its end");
        assert_eq!(app.input, "half a thought", "the draft is not the conversation's");
        assert!(app.open, "how the reader reads is not what they were reading");
    }


    fn show_frame(app: &mut App, w: u16, h: u16, title: &str) {
        println!("\n===== {title} ({w}x{h}) =====");
        for line in frame_of(app, w, h).lines() {
            println!("|{line}|");
        }
    }

    #[test]
    #[ignore]
    fn show_the_chrome() {
        let (w, h) = (90, 24);
        let mut app = App::new(None);
        show_frame(&mut app, w, h, "home, nothing typed");
        let mut app = App::new(None);
        seed(&mut app);
        show_frame(&mut app, w, h, "conversation");
        let mut app = App::new(None);
        seed(&mut app);
        typed(&mut app, "/");
        let mut menu = Menu::open(Kind::Commands, 0);
        menu.fill(menu::with_local(Vec::new()));
        app.menu = Some(menu);
        show_frame(&mut app, w, h, "the / menu");
        let mut app = App::new(None);
        seed(&mut app);
        typed(&mut app, "a long question about the wrap and the caret, typed into the field");
        show_frame(&mut app, w, h, "something typed");

        // The opening screen with its menu open: the one frame a first key opens.
        let mut app = App::new(None);
        typed(&mut app, "/");
        let mut menu = Menu::open(Kind::Commands, 0);
        menu.fill(menu::with_local(Vec::new()));
        app.menu = Some(menu);
        show_frame(&mut app, w, h, "home, the / menu");
    }


}
