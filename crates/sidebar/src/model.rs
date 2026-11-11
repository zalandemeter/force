//! Pure presentation logic: colours, glyphs, ages, line building into ratatui styles, usage bars and
//! reset times. Everything here is deterministic given its inputs, so it is unit-testable. (Colours
//! reference force/theme.py; behavioural reference is force/sidebar.py + force/usage.py.)

use ratatui::style::Color;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::glyphs::{self, Tier};
use crate::snapshot::{Session, State, Usage, Window};

/// One rendered cell of a content line (app paints it into the buffer adding the marker column etc).
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Glyph {
    pub ch: char,
    pub color: Color,
    pub bold: bool,
}

impl Glyph {
    pub fn plain(ch: char) -> Glyph {
        Glyph { ch, color: BG, bold: false }
    }
}

// --- palette -----------------------------------------------------------------
pub const BG: Color = Color::Rgb(0x21, 0x21, 0x21); // #212121: the shell colour around the agent pane
pub const ACCENT: Color = Color::Rgb(0xd9, 0x77, 0x57); // #d97757 coral

// card backgrounds (session items)
pub const CARD_CURRENT: Color = Color::Rgb(0x2c, 0x2c, 0x2c); // #2c2c2c
pub const CARD_NEEDS: Color = BG; // needs-you shows in its glyph and text colour
pub const PANEL_RULE: Color = Color::Rgb(0x3c, 0x3c, 0x3c); // #3c3c3c
pub const PANEL_TITLE: Color = Color::Rgb(0xf6, 0xc2, 0xa4); // #f6c2a4 peach
pub const CARD_IDLE: Color = BG; // no surface: only the current, needs-you and flashing cards get one
pub const CURSOR_CARD: Color = Color::Rgb(0x2e, 0x2e, 0x2e); // #2e2e2e
pub const CARD_ATTN_HI: Color = Color::Rgb(0x4d, 0x31, 0x2c); // #4d312c
// card edge colours
pub const EDGE_IDLE: Color = Color::Rgb(0x4e, 0x4e, 0x4e); // #4e4e4e
// brand rows
pub const BRAND_UNDERLINE: Color = Color::Rgb(0x33, 0x33, 0x33); // #333333
pub const IDX: Color = Color::Rgb(0x5f, 0x5f, 0x5f); // #5f5f5f
pub const NAME: Color = Color::Rgb(0xc6, 0xc6, 0xc6); // #c6c6c6
pub const NAME_CURRENT: Color = Color::Rgb(0xff, 0xff, 0xff);
pub const AGE: Color = Color::Rgb(0x8a, 0x8a, 0x8a); // #8a8a8a: the floor for text that carries information
pub const OUTDATED: Color = Color::Rgb(0x5f, 0xaf, 0xaf); // #5fafaf
pub const GREY: Color = Color::Rgb(0x8a, 0x8a, 0x8a); // #8a8a8a

pub const C_NEEDS_YOU: Color = Color::Rgb(0xf0, 0x64, 0x5e); // #f0645e: a clear red, apart from the coral of the own card
pub const C_WORKING: Color = Color::Rgb(0xd7, 0xaf, 0x5f);
pub const C_YOUR_TURN: Color = Color::Rgb(0x87, 0xc0, 0x8f);
pub const C_DOWN: Color = Color::Rgb(0xd7, 0x5f, 0x5f);
pub const C_YOUR_TURN_DIM: Color = Color::Rgb(0x79, 0xa5, 0x80); // #79a580
pub const C_WORKING_DIM: Color = Color::Rgb(0xa6, 0x89, 0x4d); // #d7af5f at ~60% brightness
/// A working session whose agent wrote a finding you have not seen (SPEC §5, `unread`): its icon and edge ...
pub const C_UNREAD: Color = Color::Rgb(0x5c, 0x9c, 0xf5); // #5c9cf5
/// ... and its line-2 text: about 4.6:1 on the card, as readable as the working text.
pub const C_UNREAD_TEXT: Color = Color::Rgb(0x5b, 0x8f, 0xd6); // #5b8fd6

// Mode colours as the agents themselves show them (dark themes): Claude Code's autoAccept, planMode, warning (auto),
// inactive (manual) and error (bypass); OpenCode's build and plan agents take its theme's secondary and accent.
pub const BADGE_A: Color = Color::Rgb(0xff, 0xc1, 0x07); // auto: warning rgb(255,193,7)
pub const BADGE_E: Color = Color::Rgb(0xaf, 0x87, 0xff); // accept edits: autoAccept rgb(175,135,255)
pub const BADGE_P: Color = Color::Rgb(0x48, 0x96, 0x8c); // plan: planMode rgb(72,150,140)
pub const BADGE_M: Color = Color::Rgb(0x99, 0x99, 0x99); // manual: inactive rgb(153,153,153)
pub const BADGE_B: Color = Color::Rgb(0xff, 0x6b, 0x80); // bypass: error rgb(255,107,128)
pub const BADGE_OC_BUILD: Color = Color::Rgb(0x5c, 0x9c, 0xf5); // OpenCode build: secondary #5c9cf5
pub const BADGE_OC_PLAN: Color = Color::Rgb(0x9d, 0x7c, 0xd8); // OpenCode plan: accent #9d7cd8
pub const SHARED_RW: Color = Color::Rgb(0xd7, 0x5f, 0x5f);
pub const SHARED_RO: Color = Color::Rgb(0xd7, 0xaf, 0x5f);

pub const USAGE_LABEL: Color = Color::Rgb(0x8a, 0x8a, 0x8a);
pub const USAGE_PCT: Color = Color::Rgb(0xc6, 0xc6, 0xc6);
pub const USAGE_RESET: Color = Color::Rgb(0x8a, 0x8a, 0x8a);
pub const EMPTY_TRACK: Color = Color::Rgb(0x3a, 0x3a, 0x3a);
pub const FILL_START: Color = Color::Rgb(0x5f, 0x5f, 0x5f);
pub const THRESH_LO: Color = Color::Rgb(0xb0, 0xb0, 0xb0);
pub const THRESH_MID: Color = Color::Rgb(0xd7, 0xaf, 0x5f);
pub const THRESH_HI: Color = Color::Rgb(0xe8, 0x80, 0x6f);

pub const MODE_BADGES: [(&str, char, Color); 7] = [
    ("auto", 'A', BADGE_A),
    ("edits", 'E', BADGE_E),
    ("plan", 'P', BADGE_P),
    ("manual", 'M', BADGE_M),
    ("bypass", 'B', BADGE_B),
    // OpenCode's agents in the same slot (SPEC §36): build edits and runs commands, plan only reads
    ("oc-build", 'B', BADGE_OC_BUILD),
    ("oc-plan", 'P', BADGE_OC_PLAN),
];

/// The kind mark in front of the model on a card's line 3, under the state icon (SPEC §36): Claude
/// Code's ✻ and OpenCode's ▣, so mixed sessions are told apart. Plain Unicode, in every font.
pub const KIND_CLAUDE: (char, Color) = ('✻', ACCENT);
pub const KIND_OPENCODE: (char, Color) = ('▣', Color::Rgb(0xc8, 0xc8, 0xc8));

pub const SPINNER: &str = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
const WEEKDAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// --- state helpers ------------------------------------------------------------
/// The headline glyph colour per state.
pub fn glyph_color(s: State) -> Color {
    match s {
        State::NeedsYou => C_NEEDS_YOU,
        State::Working => C_WORKING,
        State::YourTurn => C_YOUR_TURN,
        State::Down => C_DOWN,
        State::Shell | State::Idle => GREY,
        State::Stale | State::Starting | State::Unknown | State::Archived => IDX,
    }
}

/// A working session with a finding you have not seen (SPEC §5): its whole card is blue.
pub fn shows_unread(state: State, unread: bool) -> bool {
    unread && state == State::Working
}

pub fn is_unread(s: &Session) -> bool {
    shows_unread(s.state, s.unread)
}

/// A card's icon colour: the state's, blue for an unread finding (SPEC §5).
pub fn session_glyph_color(s: &Session) -> Color {
    if is_unread(s) {
        C_UNREAD
    } else {
        glyph_color(s.state)
    }
}

/// A card's edge colour: the state's, blue for an unread finding (SPEC §5).
pub fn card_edge_color(state: State, unread: bool) -> Color {
    if shows_unread(state, unread) {
        C_UNREAD
    } else {
        edge_color(state)
    }
}

/// Dimmed colour used for state labels (the "dimmed state colour" on line 2).
pub fn label_color(s: State) -> Color {
    match s {
        State::Working => C_WORKING_DIM,
        // dim(C_YOUR_TURN) (#517355) was too dark to read on #212121
        State::YourTurn => C_YOUR_TURN_DIM,
        // the grey states dimmed to 60 % were nearly invisible on the card background
        State::Idle | State::Stale | State::Starting | State::Unknown | State::Archived => LABEL_GREY,
        _ => dim(glyph_color(s)),
    }
}

/// Readable dim grey for the state word of quiet states (idle, stale, …).
pub const LABEL_GREY: Color = Color::Rgb(0x8a, 0x8a, 0x8a);

/// Card text indent (lines 2 and 3 start under the session name): 5 columns, 6 in RICH (2-cell icon slot).
fn indent(tier: Tier) -> String {
    " ".repeat(4 + glyphs::extra(tier))
}

pub fn is_bold_glyph(s: State) -> bool {
    matches!(s, State::NeedsYou | State::YourTurn)
}

/// The card EDGE colour per state. The current (own) session always uses coral `ACCENT` (handled by the app).
pub fn edge_color(s: State) -> Color {
    match s {
        State::NeedsYou => C_NEEDS_YOU,
        State::Working => C_WORKING,
        State::YourTurn => C_YOUR_TURN,
        State::Down => C_DOWN,
        _ => EDGE_IDLE,
    }
}

/// The host's name, set into the rule above the limits like the header's labels.
pub const HOST_LABEL: Color = Color::Rgb(0xc6, 0xc6, 0xc6); // #c6c6c6, the header's label colour

/// One cell of the brand block: an upper half block `▀` whose fg is the top pixel and bg the bottom pixel
/// (two pixels per cell), or the underline glyph on BG.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LogoCell {
    pub ch: char,
    pub fg: Color,
    pub bg: Color,
    pub bold: bool,
}

/// Rows of the brand block: a blank row, the brand rule, and a blank row that keeps the sessions apart from it.
pub const BRAND_ROWS: usize = 3;
/// The brand's letters, spaced like the header's title.
const BRAND_WORD: &str = "F O R C E";
const BRAND_LETTERS: Color = Color::Rgb(0xa0, 0xa0, 0xa0); // quieter than the session names
const BRAND_SHINE: Color = Color::Rgb(0xf6, 0xc2, 0xa4); // peach

/// Labelled rules run over columns `1..w` (`w - 1` cells): the free column on the left mirrors the invisible
/// pane border on the right, so a rule has the same margin on both sides of the visible sidebar.
pub fn label_rule_cells(w: usize) -> usize {
    w.saturating_sub(1)
}

/// The gap between an icon and its text in a label of `fixed` other cells: 2 cells (the icon slot used
/// everywhere in RICH) when that leaves the same number of `─` on both sides of the label, else 1.
pub fn icon_gap(w: usize, fixed: usize) -> usize {
    if label_rule_cells(w).saturating_sub(fixed + 2) % 2 == 0 { 2 } else { 1 }
}

/// The first column of a label `label_w` wide centred on its rule (`label_rule_cells`).
pub fn label_x0(w: usize, label_w: usize) -> usize {
    1 + label_rule_cells(w).saturating_sub(label_w) / 2
}

/// The brand label's cells: ` <bolt>  F O R C E ` (RICH: the Nerd Font bolt and its gap, see `icon_gap`), or
/// ` F O R C E ` in PLAIN, which has no icon font.
fn brand_label(tier: crate::glyphs::Tier, w: usize) -> Vec<(char, Color, bool)> {
    let mut out = vec![(' ', BG, false)];
    if tier == crate::glyphs::Tier::Rich {
        out.push(('\u{F0E7}', ACCENT, false));
        for _ in 0..icon_gap(w, 3 + BRAND_WORD.chars().count()) {
            out.push((' ', BG, false));
        }
    }
    out.extend(BRAND_WORD.chars().map(|ch| (ch, BRAND_LETTERS, false)));
    out.push((' ', BG, false));
    out
}

/// The brand block, each row exactly `w` wide (owner 2026-11-05: FORCE only, in the header's rule style):
/// a blank row, a `─` rule with ` <bolt>  F O R C E ` centred in it (the sessions panel's top rule), and a
/// blank row. `shine` is an optional light sweep centred on that column (the arrival animation): it warms the
/// letters to peach as it passes.
pub fn brand_logo(w: usize, shine: Option<f64>, tier: crate::glyphs::Tier) -> Vec<Vec<LogoCell>> {
    let blank = LogoCell { ch: ' ', fg: BG, bg: BG, bold: false };
    let label = brand_label(tier, w);
    let (cells, x0) = (label_rule_cells(w), label_x0(w, label.len()));
    let mut rule: Vec<LogoCell> = (0..w)
        .map(|x| if x == 0 || x > cells { blank } else { LogoCell { ch: '─', fg: PANEL_RULE, bg: BG, bold: false } })
        .collect();
    if w >= label.len() + 4 {
        for (i, &(ch, fg, bold)) in label.iter().enumerate() {
            let x = x0 + i;
            let fg = match shine {
                Some(s) if ch != ' ' && fg == BRAND_LETTERS => lerp(fg, BRAND_SHINE, (1.0 - (x as f64 - s).abs() / 2.5).max(0.0)),
                _ => fg,
            };
            rule[x] = LogoCell { ch, fg, bg: BG, bold };
        }
    }
    vec![vec![blank; w], rule, vec![blank; w]]
}

/// Columns the arrival light sweep travels over (start, end) for a pane `w` wide: across the letters.
pub fn logo_shine_span(w: usize) -> (f64, f64) {
    let n = brand_label(crate::glyphs::Tier::Rich, w).len();
    let x0 = label_x0(w, n) as f64;
    (x0 - 2.0, x0 + n as f64 + 2.0)
}

pub fn state_label(s: State) -> &'static str {
    match s {
        State::NeedsYou => "needs you",
        State::Working => "working",
        State::YourTurn => "your turn",
        State::Idle => "idle",
        State::Stale => "stale",
        State::Down => "down",
        State::Starting => "starting",
        State::Unknown => "unknown",
        State::Archived => "archived",
        State::Shell => "shell",
    }
}

fn dim(c: Color) -> Color {
    match c {
        Color::Rgb(r, g, b) => Color::Rgb(
            (r as u16 * 6 / 10) as u8,
            (g as u16 * 6 / 10) as u8,
            (b as u16 * 6 / 10) as u8,
        ),
        c => c,
    }
}

// --- glyph / age / truncate ---------------------------------------------------
pub fn glyph(tier: Tier, s: State, now_ms: i64) -> char {
    match s {
        State::Archived => ' ',
        State::Working => match tier {
            Tier::Plain => {
                let i = (((now_ms / 100) % 10).rem_euclid(10)) as usize;
                SPINNER.chars().nth(i).unwrap()
            }
            // RICH: a text-size pie that fills in 8 steps (md circle-slice 1..8), same slot as the icons
            Tier::Rich => {
                let i = ((now_ms / 125).rem_euclid(8)) as u32;
                char::from_u32(0xF0A9E + i).unwrap_or('\u{F0A9E}')
            }
        },
        State::Down => glyphs::state(tier, '×', '\u{F057}'),
        State::Shell => glyphs::state(tier, '$', '\u{EA85}'),
        State::Starting => glyphs::state(tier, '…', '\u{F110}'),
        State::NeedsYou => glyphs::state(tier, '!', '\u{F0F3}'),
        State::YourTurn => glyphs::state(tier, '●', '\u{F058}'),
        State::Stale => glyphs::state(tier, '◌', '\u{F017}'),
        State::Idle => glyphs::state(tier, '○', '\u{F10C}'),
        State::Unknown => glyphs::state(tier, '?', '\u{F059}'),
    }
}

pub fn age(since_ms: Option<i64>, now_ms: i64) -> String {
    let s = match since_ms {
        None => return String::new(),
        Some(s) => s,
    };
    let diff = now_ms - s;
    if diff < 0 {
        return "now".to_string();
    }
    let sec = diff / 1000;
    if sec < 10 {
        "now".to_string()
    } else if sec < 60 {
        format!("{}s", sec)
    } else if sec < 60 * 60 {
        format!("{}m", sec / 60)
    } else if sec < 48 * 60 * 60 {
        format!("{}h", sec / 3600)
    } else {
        format!("{}d", sec / 86400)
    }
}

/// Truncate to `limit` display columns, appending a single ellipsis (`…`) when it overflows.
pub fn truncate(s: &str, limit: usize) -> String {
    if limit == 0 {
        return String::new();
    }
    if UnicodeWidthStr::width(s) <= limit {
        return s.to_string();
    }
    if limit == 1 {
        return "…".to_string();
    }
    let mut out = String::new();
    let mut w = 0usize;
    for ch in s.chars() {
        let cw = UnicodeWidthChar::width(ch).unwrap_or(0).max(1);
        if w + cw > limit - 1 {
            break;
        }
        out.push(ch);
        w += cw;
    }
    out.push('…');
    out
}

pub fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

fn space(color: Color) -> Glyph {
    Glyph { ch: ' ', color, bold: false }
}

fn push_str(out: &mut Vec<Glyph>, s: &str, color: Color, bold: bool) {
    for ch in s.chars() {
        out.push(Glyph { ch, color, bold });
    }
}

fn pad_to(out: &mut Vec<Glyph>, c: usize) {
    while out.len() < c {
        out.push(space(BG));
    }
}

/// Line 1: `  {glyph} {name}{pad}{age}` + optional ` ↑` (outdated), exactly `c` columns. The first column is the
/// card's edge slot (the app draws the `▍` there); no index number.
pub fn line1(s: &Session, now_ms: i64, c: usize, name_color: Color, tier: Tier) -> Vec<Glyph> {
    // Chuck (SPEC §50) shows his face while there is nothing to tell; working, needs you and your turn as usual
    let face = s.support && matches!(s.state, State::Idle | State::Stale);
    let g = if face { glyphs::state(tier, '?', crate::app::CHUCK_RICH) } else { glyph(tier, s.state, now_ms) };
    let gap = glyphs::gap(tier);
    let prefix = format!("  {}{}", g, gap); // width 4 (5 in RICH: 2-cell icon slot)
    let age = age(s.since_ms, now_ms);
    // the right-edge marker stays a 1-cell `↑` in both tiers (an icon in the last column has no 2-cell
    // slot); line 2 carries the RICH icon with the version.
    let right = if s.outdated { format!("{} ↑", age) } else { age.clone() };
    let rw = width(&right);
    let pw = width(&prefix);
    let limit = (c as i64 - pw as i64 - rw as i64 - 1).max(1) as usize;
    let name = truncate(&s.name, limit);
    let nw = width(&name);
    let pad = (c as i64 - pw as i64 - nw as i64 - rw as i64).max(0) as usize;

    let gcol = if face { ACCENT } else { session_glyph_color(s) };
    let gbold = if face { false } else { is_bold_glyph(s.state) };
    let mut out = Vec::with_capacity(c);
    out.push(space(BG));
    out.push(space(BG));
    out.push(Glyph { ch: g, color: gcol, bold: gbold });
    for _ in 0..gap.len() {
        out.push(space(gcol));
    }
    push_str(&mut out, &name, name_color, false);
    for _ in 0..pad {
        out.push(space(BG));
    }
    push_str(&mut out, &age, AGE, false);
    if s.outdated {
        push_str(&mut out, " ↑", OUTDATED, false);
    }
    out
}

fn outdated_tail(s: &Session, tier: Tier) -> String {
    if !s.outdated {
        return String::new();
    }
    let arrow = glyphs::state(tier, '↑', glyphs::OUTDATED_RICH);
    match &s.version {
        Some(v) if !v.is_empty() => format!(" · {}{}{}", arrow, glyphs::gap(tier), v),
        _ => format!(" · {}", arrow),
    }
}

/// Line 2 for a SHELL row: `shell <cwd>` (home -> ~, long path cut left with `…`). Width `c`.
fn line2_shell(s: &Session, c: usize, tier: Tier) -> Vec<Glyph> {
    let home = std::env::var("HOME").unwrap_or_default();
    let cwd = s.cwd.as_str();
    let shown = if !home.is_empty() && cwd == home {
        "~".to_string()
    } else if !home.is_empty() {
        if let Some(rest) = cwd.strip_prefix(&home) {
            let rest = rest.trim_start_matches('/');
            format!("~{}", rest)
        } else {
            cwd.to_string()
        }
    } else {
        cwd.to_string()
    };
    let indent = indent(tier);
    let avail = (c as i64 - indent.len() as i64).max(1) as usize;
    if let Some(title) = s.title.as_deref().filter(|t| !t.is_empty()) {
        let mut out = Vec::with_capacity(c);
        push_str(&mut out, &indent, GREY, false);
        push_str(&mut out, &truncate(title, avail), GREY, false);
        pad_to(&mut out, c);
        return out;
    }
    let body = format!("shell {}", shown);
    let body = if width(&body) > avail {
        // keep the word "shell …", cut the path's left end
        let head = "shell …";
        let avail_path = (avail as i64 - head.len() as i64).max(1) as usize;
        format!("{}{}", head, trim_right(&shown, avail_path))
    } else {
        body
    };
    let body = truncate(&body, avail);
    let mut out = Vec::with_capacity(c);
    push_str(&mut out, &indent, GREY, false);
    push_str(&mut out, &body, GREY, false);
    pad_to(&mut out, c);
    out
}

/// Keep the RIGHT `limit` columns of `s`.
fn trim_right(s: &str, limit: usize) -> String {
    if width(s) <= limit {
        return s.to_string();
    }
    let mut collected: Vec<char> = Vec::new();
    let mut w = 0usize;
    for ch in s.chars().rev() {
        let cw = UnicodeWidthChar::width(ch).unwrap_or(0).max(1);
        if w + cw > limit {
            break;
        }
        collected.push(ch);
        w += cw;
    }
    collected.iter().rev().collect()
}

/// Line 2: indented activity / state-label line (behavioural reference `_line2`). Width `c`.
pub fn line2(s: &Session, c: usize, tier: Tier) -> Vec<Glyph> {
    if s.state == State::Shell {
        return line2_shell(s, c, tier);
    }
    let indent = indent(tier);
    let avail = (c as i64 - indent.len() as i64).max(1) as usize;

    // a session reported from outside (kind "shell", state not SHELL): the summary shows as it is (SPEC §43.2)
    let reported: Option<String> = if s.kind == "shell" {
        s.activity.clone().filter(|a| !a.is_empty())
    } else {
        None
    };
    let activity: Option<String> = match s.state {
        State::NeedsYou if reported.is_some() => reported.clone(),
        State::YourTurn if reported.is_some() => reported.clone(),
        State::NeedsYou => {
            let a = s.activity.as_deref();
            if matches!(a, Some("asks you a question") | Some("plan to review") | Some("trust this folder?")) {
                a.map(str::to_string)
            } else {
                Some("needs permission".to_string())
            }
        }
        State::Working => s.activity.clone().filter(|a| !a.is_empty()),
        _ => None,
    };

    let tail = outdated_tail(s, tier);
    let mut out = Vec::with_capacity(c);
    push_str(&mut out, &indent, GREY, false);

    match activity {
        Some(act) => {
            let act = truncate(&act, (avail as i64 - width(&tail) as i64).max(1) as usize);
            let (color, bold) = match s.state {
                State::NeedsYou => (C_NEEDS_YOU, true),
                State::Working if is_unread(s) => (C_UNREAD_TEXT, false),
                State::Working => (C_WORKING_DIM, false),
                State::YourTurn => (label_color(State::YourTurn), false),
                _ => (GREY, false),
            };
            push_str(&mut out, &act, color, bold);
            if !tail.is_empty() {
                push_str(&mut out, &tail, OUTDATED, false);
            }
        }
        None => {
            // a restart's feedback replaces the label for a few seconds (SPEC §7.3)
            let (label, color) = match s.note.as_deref() {
                Some("restarted") => ("restarted", C_YOUR_TURN),
                Some("restarting") => ("restarting", LABEL_GREY),
                Some("archiving") => ("archiving", LABEL_GREY),
                Some("moving") => ("moving", LABEL_GREY),
                Some("moved") => ("moved", C_YOUR_TURN),
                _ if is_unread(s) => (state_label(s.state), C_UNREAD_TEXT),
                _ => (state_label(s.state), label_color(s.state)),
            };
            let shown_note = matches!(
                s.note.as_deref(),
                Some("restarted") | Some("restarting") | Some("archiving") | Some("moving") | Some("moved")
            );
            let title = s
                .title
                .as_deref()
                .filter(|t| !t.is_empty() && !shown_note && matches!(s.state, State::Idle | State::Stale));
            let label = match title {
                Some(t) => truncate(t, (avail as i64 - width(&tail) as i64).max(1) as usize),
                None => label.to_string(),
            };
            push_str(&mut out, &label, color, false);
            if !tail.is_empty() {
                push_str(&mut out, &tail, OUTDATED, false);
            }
        }
    }
    // content must not exceed avail columns between the indent and the right edge
    if out.len() > c {
        out.truncate(c);
    } else {
        pad_to(&mut out, c);
    }
    out
}

/// Line 3: 5-space indent + dim grey `model · effort`, then badge / shared right-aligned. Width `c`.
pub fn line3(s: &Session, c: usize, tier: Tier) -> Vec<Glyph> {
    let badge = MODE_BADGES
        .iter()
        .find(|(m, _, _)| Some(*m) == s.mode.as_deref())
        .map(|(_, ch, col)| (*ch, *col));
    let shared = s.shared.as_ref().filter(|m| !m.is_empty());
    let deco: usize = if badge.is_some() && shared.is_some() {
        3
    } else if badge.is_some() || shared.is_some() {
        2
    } else {
        0
    };
    let body_w = (c as i64 - deco as i64).max(1) as usize;

    let text = match (&s.model, &s.effort) {
        (Some(m), Some(e)) => format!("{} · {}", m, e),
        (Some(m), None) => m.clone(),
        (None, Some(e)) => e.clone(),
        _ => String::new(),
    };
    let indent = indent(tier);
    let plain = {
        let limit = (body_w as i64 - indent.len() as i64).max(1) as usize;
        format!("{}{}", indent, truncate(&text, limit))
    };
    let mut out = Vec::with_capacity(c);
    push_str(&mut out, &plain, GREY, false);
    // the kind mark in the state-icon column (3), only where there is a model to label
    let mark = match s.kind.as_str() {
        "opencode" => Some(KIND_OPENCODE),
        "claude" if !text.is_empty() => Some(KIND_CLAUDE),
        _ => None,
    };
    if let (Some((ch, color)), Some(cell)) = (mark, out.get_mut(3)) {
        *cell = Glyph { ch, color, bold: false };
    }
    let pad = (body_w as i64 - width(&plain) as i64).max(0) as usize;
    for _ in 0..pad {
        out.push(space(GREY));
    }
    if let Some(obj) = shared {
        let rw = obj.iter().any(|(_, v)| v == "w");
        out.push(Glyph { ch: '⇄', color: if rw { SHARED_RW } else { SHARED_RO }, bold: rw });
        if let Some((bc, bcol)) = badge {
            out.push(space(GREY));
            out.push(Glyph { ch: bc, color: bcol, bold: true });
        } else {
            out.push(space(GREY));
        }
    } else if let Some((bc, bcol)) = badge {
        out.push(space(GREY));
        out.push(Glyph { ch: bc, color: bcol, bold: true });
    }
    out
}

// --- usage --------------------------------------------------------------------
pub fn threshold_color(pct: f64) -> Color {
    if pct >= 90.0 {
        THRESH_HI
    } else if pct >= 70.0 {
        THRESH_MID
    } else {
        THRESH_LO
    }
}


/// The smooth usage bar: `barw` cells, filled with eighth-block precision in the threshold colour.
/// RICH draws a slim heavy-line bar on a dim track.
pub fn usage_bar(barw: usize, pct: f64, tier: Tier) -> Vec<Glyph> {
    match tier {
        // the same slim line in both tiers: box drawing needs no Nerd Font (every font and terminal has it)
        Tier::Plain | Tier::Rich => usage_bar_capsule(barw, pct),
    }
}

/// Usage bar (both tiers): a slim line of heavy box-drawing strokes, like Python Rich's progress bars: `━` per
/// filled cell in the solid threshold colour, `╸` (left half) for a half cell, `━` in the dim track colour
/// for the rest; at least half a cell once anything is used. Box drawing is drawn by the terminal itself
/// in kitty, VTE (Tilix, GNOME Terminal), WezTerm, Ghostty, Alacritty, Windows Terminal..., so the bar
/// joins without seams everywhere with no font to install. (Until 2026-10-19 Nerd Font capsule glyphs
/// U+EE00–EE05: only kitty draws those itself; elsewhere they came from a font, dashed or with seams.)
fn usage_bar_capsule(barw: usize, pct: f64) -> Vec<Glyph> {
    let pct = if pct.is_finite() { pct.clamp(0.0, 100.0) } else { 0.0 };
    let thresh = threshold_color(pct);
    let mut halves = (pct / 100.0 * barw as f64 * 2.0).round() as usize;
    if pct > 0.0 && halves == 0 {
        halves = 1;
    }
    let (full, half) = (halves / 2, halves % 2 == 1);
    (0..barw)
        .map(|j| {
            if j < full {
                Glyph { ch: '━', color: thresh, bold: false }
            } else if j == full && half {
                Glyph { ch: '╸', color: thresh, bold: false }
            } else {
                Glyph { ch: '━', color: EMPTY_TRACK, bold: false }
            }
        })
        .collect()
}

fn lerp(a: Color, b: Color, t: f64) -> Color {
    let t = t.clamp(0.0, 1.0);
    match (a, b) {
        (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) => Color::Rgb(
            (r1 as f64 + (r2 as f64 - r1 as f64) * t) as u8,
            (g1 as f64 + (g2 as f64 - g1 as f64) * t) as u8,
            (b1 as f64 + (b2 as f64 - b1 as f64) * t) as u8,
        ),
        _ => b,
    }
}

/// `16:30` (same local day) / `Thu` (other day) / `new` (past).
pub fn reset_short(resets_at: i64, now: i64, tz_offset: i32) -> String {
    if resets_at <= now {
        return "new".to_string();
    }
    let (y1, m1, d1, weekday) = local_civil(resets_at, tz_offset);
    let (y2, m2, d2, _) = local_civil(now, tz_offset);
    let (h, mm) = local_hms(resets_at, tz_offset);
    if (y1, m1, d1) == (y2, m2, d2) {
        format!("{:02}:{:02}", h, mm)
    } else {
        WEEKDAYS[weekday].to_string()
    }
}

/// Slim usage panel lines: one blank row, one line per present window (five_hour then seven_day),
/// then one blank row. 3 rows with one window, 4 with two; empty when no windows are present.
/// Width `w` each (full pane width). Times are unix SECONDS.
pub fn usage_lines(usage: &Usage, now: i64, w: usize, tz_offset: i32, tier: Tier) -> Vec<Vec<Glyph>> {
    limits_lines(&[(None, usage.clone())], now, w, tz_offset, tier)
}

/// A limits block's label (SPEC §49.2): the provider's kind mark and name, e.g. ('✻', coral, "Claude").
pub type LimitsLabel = (char, Color, String);

/// The kind mark of a provider's limits: the cards' kind mark (✻ Claude, ▣ OpenCode), else a dot.
pub fn limits_mark(kind: &str) -> (char, Color) {
    match kind {
        "claude" => KIND_CLAUDE,
        "opencode" => KIND_OPENCODE,
        _ => ('·', USAGE_LABEL),
    }
}

/// The dock's limits (SPEC §49.2): per block a label row (the kind mark in the cards' icon column, the name where
/// the cards' names start) or, unlabelled, a blank row, then its windows; one blank row after the last block.
/// Blocks without a window are skipped; none at all -> no lines.
pub fn limits_lines(blocks: &[(Option<LimitsLabel>, Usage)], now: i64, w: usize, tz_offset: i32, tier: Tier) -> Vec<Vec<Glyph>> {
    let mut lines: Vec<Vec<Glyph>> = Vec::new();
    for (label, usage) in blocks {
        if usage.five_hour.is_none() && usage.seven_day.is_none() {
            continue;
        }
        lines.push(match label {
            Some((mark, color, text)) => limits_label_line(*mark, *color, text, w),
            None => blank_line(w),
        });
        for (win, label) in [(usage.five_hour, "5h"), (usage.seven_day, "wk")] {
            if let Some(win) = win {
                lines.push(usage_line(label, win, w, now, tz_offset, tier));
            }
        }
    }
    if !lines.is_empty() {
        lines.push(blank_line(w));
    }
    lines
}

fn limits_label_line(mark: char, color: Color, text: &str, w: usize) -> Vec<Glyph> {
    let mut out = Vec::with_capacity(w);
    out.push(space(BG));
    out.push(space(BG));
    out.push(space(BG));
    out.push(Glyph { ch: mark, color, bold: false });
    out.push(space(BG));
    push_str(&mut out, text, USAGE_LABEL, false);
    pad_to(&mut out, w);
    out.truncate(w);
    out
}

fn blank_line(w: usize) -> Vec<Glyph> {
    vec![space(BG); w]
}

fn usage_line(label: &str, win: Window, w: usize, now: i64, tz_offset: i32, tier: Tier) -> Vec<Glyph> {
    // `   5h ` (the label in the cards' icon column), the bar, ` 100% 16:30` (the reset right-aligned) and 2
    // free columns: with the invisible pane border beside them, the same 3-column margin as at the start
    let barw = (w as i64 - 19).max(4) as usize;
    let expired = win.resets_at <= now;
    let pct = if expired { 0.0 } else { win.used_percentage };
    let token = if expired { "–".to_string() } else { format!("{}%", pct.round() as i64) };
    let pctcol = if pct >= 70.0 { threshold_color(pct) } else { USAGE_PCT };
    let mut out = Vec::with_capacity(w);
    out.push(space(BG));
    out.push(space(BG));
    out.push(space(BG));
    push_str(&mut out, label, USAGE_LABEL, false);
    out.push(space(BG));
    out.extend(usage_bar(barw, pct, tier));
    out.push(space(BG));
    push_str(&mut out, &format!("{:>4}", token), pctcol, false);
    out.push(space(BG));
    let reset = reset_short(win.resets_at, now, tz_offset);
    push_str(&mut out, &format!("{:>5}", reset), USAGE_RESET, false);
    pad_to(&mut out, w);
    if out.len() > w {
        out.truncate(w);
    }
    out
}

// --- local civil time (tz-offset aware, no chrono) ------------------------------
pub fn read_tz_offset() -> i32 {
    let out = std::process::Command::new("date")
        .arg("+%z")
        .stdin(std::process::Stdio::null())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok());
    match out {
        Some(s) => parse_tz_offset(s.trim()),
        None => 0,
    }
}

pub fn parse_tz_offset(s: &str) -> i32 {
    let b = s.as_bytes();
    if b.len() != 5 {
        return 0;
    }
    let sign = match b[0] {
        b'+' => 1i64,
        b'-' => -1i64,
        _ => return 0,
    };
    let h = match std::str::from_utf8(&b[1..3]).ok().and_then(|x| x.parse::<i64>().ok()) {
        Some(v) => v,
        None => return 0,
    };
    let m = match std::str::from_utf8(&b[3..5]).ok().and_then(|x| x.parse::<i64>().ok()) {
        Some(v) => v,
        None => return 0,
    };
    (sign * (h * 3600 + m * 60)) as i32
}

fn local_civil(t: i64, tz_offset: i32) -> (i64, i64, i64, usize) {
    let local = t + tz_offset as i64;
    let days = floor_div(local, 86400);
    let (y, m, d) = civil_from_days(days);
    // 1970-01-01 was a Thursday; weekday index over ["Mon".."Sun"].
    let weekday = ((days + 3).rem_euclid(7)) as usize;
    (y, m, d, weekday)
}

fn local_hms(t: i64, tz_offset: i32) -> (i64, i64) {
    let local = t + tz_offset as i64;
    let rem = local.rem_euclid(86400);
    (rem / 3600, (rem % 3600) / 60)
}

fn floor_div(a: i64, b: i64) -> i64 {
    let q = a / b;
    let r = a % b;
    if r != 0 && (r < 0) != (b < 0) {
        q - 1
    } else {
        q
    }
}

/// Howard Hinnant's civil_from_days.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

#[cfg(test)]
pub fn glyphs_to_string(g: &[Glyph]) -> String {
    g.iter().map(|x| x.ch).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chuck_shows_his_face_while_idle() {
        let mut s = sess(State::Idle);
        s.support = true;
        let l = line1(&s, 0, 29, NAME, Tier::Rich);
        assert_eq!(l[2].ch, crate::app::CHUCK_RICH);
        assert_eq!(l[2].color, ACCENT);
        assert_eq!(line1(&s, 0, 29, NAME, Tier::Plain)[2].ch, '?');
        // anything to tell shows as usual
        s.state = State::NeedsYou;
        assert_eq!(line1(&s, 0, 29, NAME, Tier::Rich)[2].ch, glyph(Tier::Rich, State::NeedsYou, 0));
    }

    fn sess(state: State) -> Session {
        Session {
            name: "portal".into(),
            index: Some(2),
            state,
            since_ms: Some(1791699990000),
            version: None,
            outdated: false,
            archived: false,
            order: 1,
            cwd: "/home/alice/portal".into(),
            session_id: "s".into(),
            pid: None,
            waiting_for: None,
            viewed: true,
            activity: None,
            title: None,
            mode: None,
            model: None,
            effort: None,
            shared: None,
            note: None,
            kind: "claude".into(),
            support: false,
            unread: false,
        }
    }

    #[test]
    fn age_table() {
        let now = 1_000_000i64;
        assert_eq!(age(None, now), "");
        assert_eq!(age(Some(now + 50), now), "now");
        assert_eq!(age(Some(now - 5), now), "now");
        assert_eq!(age(Some(now - 45_000), now), "45s");
        assert_eq!(age(Some(now - 12 * 60_000), now), "12m");
        assert_eq!(age(Some(now - 3 * 3_600_000), now), "3h");
        assert_eq!(age(Some(now - 49 * 3_600_000), now), "2d");
        assert_eq!(age(Some(now - 5 * 86_400_000), now), "5d");
    }

    #[test]
    fn glyph_per_state() {
        for (st, expected) in [
            (State::Down, '×'),
            (State::Shell, '$'),
            (State::Starting, '…'),
            (State::NeedsYou, '!'),
            (State::YourTurn, '●'),
            (State::Stale, '◌'),
            (State::Idle, '○'),
            (State::Unknown, '?'),
            (State::Archived, ' '),
        ] {
            assert_eq!(glyph(Tier::Plain, st, 0), expected);
        }
        assert_eq!(glyph(Tier::Plain, State::Working, 0), SPINNER.chars().next().unwrap());
        assert_eq!(glyph(Tier::Plain, State::Working, 900), SPINNER.chars().nth(9).unwrap());
        assert_eq!(glyph(Tier::Plain, State::Working, 1000), SPINNER.chars().next().unwrap());
    }

    #[test]
    fn rich_state_glyphs() {
        for (st, rich) in [
            (State::NeedsYou, '\u{F0F3}'),
            (State::YourTurn, '\u{F058}'),
            (State::Idle, '\u{F10C}'),
            (State::Stale, '\u{F017}'),
            (State::Down, '\u{F057}'),
            (State::Shell, '\u{EA85}'),
            (State::Starting, '\u{F110}'),
            (State::Unknown, '\u{F059}'),
        ] {
            assert_eq!(glyph(Tier::Rich, st, 0), rich, "state {:?}", st);
        }
        assert_eq!(glyph(Tier::Rich, State::Archived, 0), ' ');
        // PLAIN keeps the braille spinner; RICH fills a pie in 8 steps of 125 ms
        assert_eq!(glyph(Tier::Plain, State::Working, 0), SPINNER.chars().next().unwrap());
        assert_eq!(glyph(Tier::Rich, State::Working, 0), '\u{F0A9E}');
        assert_eq!(glyph(Tier::Rich, State::Working, 7 * 125), '\u{F0AA5}');
        assert_eq!(glyph(Tier::Rich, State::Working, 8 * 125), '\u{F0A9E}');
        for i in 0..8u32 {
            let c = char::from_u32(0xF0A9E + i).unwrap();
            assert_eq!(unicode_width::UnicodeWidthChar::width(c), Some(1));
        }
    }

    #[test]
    fn rich_outdated_marker_uses_f0aa() {
        let now = 1_790_000_000_000i64;
        let mut s1 = sess(State::NeedsYou);
        s1.since_ms = Some(now - 15 * 60 * 1000);
        s1.outdated = true;
        s1.version = Some("2.1.295".into());
        let text = glyphs_to_string(&line1(&s1, now, 30, NAME, Tier::Rich));
        // line 1 keeps the 1-cell `↑` at the right edge in both tiers
        assert!(text.contains('↑') && !text.contains('\u{F0AA}'), "RICH line1 outdated: {:?}", text);
        // the · ↑ <ver> tail on line 2
        let text2 = glyphs_to_string(&line2(&s1, 30, Tier::Rich));
        assert!(text2.contains("\u{F0AA}  2.1.295"), "RICH line2 tail with a 2-cell slot: {:?}", text2);
        assert_eq!(width(&text), 30);
        assert_eq!(width(&text2), 30);
        // PLAIN stays `↑`
        let plain = glyphs_to_string(&line1(&s1, now, 30, NAME, Tier::Plain));
        assert!(plain.contains('↑'));
    }

    #[test]
    fn rich_capsule_bar() {
        use crate::glyphs::Tier as T;
        let used = |b: &[Glyph]| b.iter().filter(|g| g.color != EMPTY_TRACK).map(|g| g.ch).collect::<String>();
        for pct in [0.0, f64::NAN, -5.0] {
            let b = usage_bar(12, pct, T::Rich);
            assert_eq!(b.len(), 12);
            assert!(b.iter().all(|g| g.ch == '━' && g.color == EMPTY_TRACK));
        }
        assert_eq!(used(&usage_bar(12, 1.0, T::Rich)), "╸", "half a cell once anything is used");
        assert_eq!(used(&usage_bar(12, 50.0, T::Rich)), "━━━━━━");
        assert_eq!(used(&usage_bar(12, 45.0, T::Rich)), "━━━━━╸");
        let b = usage_bar(12, 45.0, T::Rich);
        assert!(b[..6].iter().all(|g| g.color == threshold_color(45.0)), "solid fill colour");
        assert!(b[6..].iter().all(|g| g.ch == '━' && g.color == EMPTY_TRACK));
        assert_eq!(used(&usage_bar(12, 100.0, T::Rich)), "━".repeat(12));
        assert_eq!(used(&usage_bar(12, 250.0, T::Rich)), "━".repeat(12));
    }

    #[test]
    fn truncation_ellipsis() {
        assert_eq!(truncate("hello", 10), "hello");
        assert_eq!(truncate("hello world", 5), "hell…");
        assert_eq!(truncate("hello world", 1), "…");
        assert_eq!(truncate("ab", 0), "");
        assert_eq!(width(&truncate("hello world", 6)), 6);
    }

    #[test]
    fn line1_exact_text() {
        let now = 1_790_000_000_000i64;
        let mut s = sess(State::NeedsYou);
        s.index = Some(3);
        s.since_ms = Some(now - 15 * 60 * 1000); // age "15m"
        let cur = line1(&s, now, 26, NAME_CURRENT, Tier::Plain);
        let notcur = line1(&s, now, 26, NAME, Tier::Plain);
        assert_eq!(glyphs_to_string(&cur), glyphs_to_string(&notcur));
        let text = glyphs_to_string(&cur);
        assert_eq!(text, format!("  ! portal{0}15m", " ".repeat(13)), "no index number: the first column is the edge slot");
        assert_eq!(cur.iter().find(|g| g.ch == 'p').unwrap().color, NAME_CURRENT);
        assert_eq!(notcur.iter().find(|g| g.ch == 'p').unwrap().color, NAME);
    }

    #[test]
    fn line1_with_outdated() {
        let now = 1_790_000_000_000i64;
        let mut s = sess(State::NeedsYou);
        s.outdated = true;
        s.version = Some("2.1.295".into());
        s.since_ms = Some(now - 15 * 60 * 1000);
        let text = glyphs_to_string(&line1(&s, now, 26, NAME, Tier::Plain));
        assert_eq!(width(&text), 26);
        assert!(text.contains('↑'));
    }

    #[test]
    fn line2_shell_home_and_long_path() {
        // This test needs a known $HOME to exercise the `~` substitution.
        std::env::set_var("HOME", "/home/alice");
        let home = "/home/alice";
        let mut s = sess(State::Shell);
        s.cwd = home.to_string();
        let t = glyphs_to_string(&line2(&s, 40, Tier::Plain));
        assert!(t.contains("shell ~"), "got {:?}", t);

        let long = format!("{}/very/long/path/that/keeps/going/deep/down", home);
        s.cwd = long;
        let t = glyphs_to_string(&line2(&s, 30, Tier::Plain));
        assert_eq!(width(&t), 30);
        assert!(t.starts_with("    shell …"), "got {:?}", t);
    }

    #[test]
    fn line2_needs_you_variants() {
        let mut s = sess(State::NeedsYou);
        s.activity = Some("asks you a question".into());
        assert!(glyphs_to_string(&line2(&s, 26, Tier::Plain)).contains("asks you a question"));

        s.activity = Some("random thing".into());
        assert!(glyphs_to_string(&line2(&s, 26, Tier::Plain)).contains("needs permission"));

        s.activity = Some("plan to review".into());
        assert!(glyphs_to_string(&line2(&s, 26, Tier::Plain)).contains("plan to review"));

        // bold needs-you colour
        let g = line2(&s, 26, Tier::Plain);
        assert!(g.iter().any(|x| x.ch == 'p' && x.bold && x.color == C_NEEDS_YOU));
    }

    #[test]
    fn line2_working_activity_vs_label() {
        let mut s = sess(State::Working);
        s.activity = Some("editing app.py".into());
        let t = glyphs_to_string(&line2(&s, 26, Tier::Plain));
        assert!(t.contains("editing app.py"));
        let g = line2(&s, 26, Tier::Plain);
        assert!(g.iter().any(|x| x.ch == 'e' && x.color == C_WORKING_DIM));

        s.activity = None;
        assert!(glyphs_to_string(&line2(&s, 26, Tier::Plain)).contains("working"));
    }

    fn l2(s: &Session, c: usize) -> (String, Vec<Glyph>) {
        let g = line2(s, c, Tier::Plain);
        (glyphs_to_string(&g), g)
    }

    #[test]
    fn line2_shell_title() {
        let mut s = sess(State::Shell);
        s.kind = "shell".into();
        s.title = Some("htop".into());
        let (t, g) = l2(&s, 30);
        assert!(t.contains("htop") && !t.contains("shell"), "{:?}", t);
        assert!(g.iter().any(|x| x.ch == 'h' && x.color == GREY));
        s.title = Some("x".repeat(60));
        let (t, g) = l2(&s, 20);
        assert_eq!(g.len(), 20);
        assert!(t.contains('…'));
        s.title = None;
        assert!(l2(&s, 40).0.contains("shell ~") || l2(&s, 40).0.contains("shell /"));
    }

    #[test]
    fn reported_shell_cards() {
        for st in [State::Working, State::NeedsYou, State::YourTurn] {
            let mut s = sess(st);
            s.kind = "shell".into();
            // without activity: the state label
            let (t, _) = l2(&s, 30);
            let want = if st == State::NeedsYou { "needs permission" } else { state_label(st) };
            assert!(t.contains(want), "{:?} {:?}", st, t);
            // with activity: shown as is
            s.activity = Some("building".into());
            let (t, g) = l2(&s, 30);
            assert!(t.contains("building") && !t.contains("needs permission") && !t.contains("your turn"), "{:?}", t);
            let col = match st {
                State::NeedsYou => C_NEEDS_YOU,
                State::YourTurn => label_color(State::YourTurn),
                _ => C_WORKING_DIM,
            };
            assert!(g.iter().any(|x| x.ch == 'b' && x.color == col), "{:?}", st);
            // empty activity = none
            s.activity = Some(String::new());
            assert!(!l2(&s, 30).0.contains("building"));
        }
    }

    #[test]
    fn reported_shell_glyph_and_edge_follow_state() {
        let mut s = sess(State::Working);
        s.kind = "shell".into();
        let g = line1(&s, 1791700000000, 30, GREY, Tier::Plain);
        assert!(g.iter().any(|x| x.ch == '●' || SPINNER.contains(x.ch)));
        assert_ne!(glyph(Tier::Plain, State::Working, 0), '$');
        assert_ne!(glyph_color(State::Working), glyph_color(State::Shell));
    }

    #[test]
    fn line2_title_replaces_idle_stale_label() {
        for (st, word) in [(State::Idle, "idle"), (State::Stale, "stale")] {
            let mut s = sess(st);
            s.title = Some("refactor parser".into());
            let (t, g) = l2(&s, 30);
            assert!(t.contains("refactor parser") && !t.contains(word), "{:?}", t);
            assert!(g.iter().any(|x| x.ch == 'r' && x.color == label_color(st)));
        }
        // your-turn / working / needs-you unchanged
        let mut s = sess(State::YourTurn);
        s.title = Some("refactor parser".into());
        let (t, _) = l2(&s, 30);
        assert!(t.contains("your turn") && !t.contains("refactor"));
        // a note wins
        let mut s = sess(State::Idle);
        s.title = Some("refactor parser".into());
        s.note = Some("restarting".into());
        let (t, _) = l2(&s, 30);
        assert!(t.contains("restarting") && !t.contains("refactor"));
        // outdated tail kept, title truncated to fit
        let mut s = sess(State::Idle);
        s.title = Some("a very long pane title that cannot fit".into());
        s.outdated = true;
        s.version = Some("2.1.295".into());
        let (t, g) = l2(&s, 26);
        assert_eq!(g.len(), 26);
        assert!(t.contains('…') && t.contains("2.1.295"), "{:?}", t);
        // long title without tail
        s.outdated = false;
        let (t, g) = l2(&s, 20);
        assert_eq!(g.len(), 20);
        assert!(t.trim_end().chars().count() <= 20 && t.contains('…'));
    }

    #[test]
    fn line2_outdated_suffix() {
        let mut s = sess(State::Working);
        s.activity = Some("editing".into());
        s.outdated = true;
        s.version = Some("2.1.295".into());
        let t = glyphs_to_string(&line2(&s, 26, Tier::Plain));
        assert!(t.contains('↑') && t.contains("2.1.295"));
    }

    #[test]
    fn line3_model_effort_badge() {
        let mut s = sess(State::Working);
        s.model = Some("opus 5.5".into());
        s.effort = Some("high".into());
        s.mode = Some("auto".into());
        let g = line3(&s, 26, Tier::Plain);
        let t = glyphs_to_string(&g);
        assert!(t.contains("opus 5.5 · high"));
        assert_eq!(width(&t), 26);
        assert!(g.iter().any(|x| x.ch == 'A' && x.bold));
    }

    #[test]
    fn line3_shared_ro_rw() {
        let mut s = sess(State::Working);
        s.model = Some("m".into());
        let mut m = serde_json::Map::new();
        m.insert("alice".into(), serde_json::json!("r"));
        s.shared = Some(m);
        let g = line3(&s, 26, Tier::Plain);
        assert!(glyphs_to_string(&g).contains('⇄'));
        assert_eq!(g.iter().find(|x| x.ch == '⇄').unwrap().color, SHARED_RO);

        let mut m = serde_json::Map::new();
        m.insert("bob".into(), serde_json::json!("w"));
        s.shared = Some(m);
        let g = line3(&s, 26, Tier::Plain);
        assert_eq!(g.iter().find(|x| x.ch == '⇄').unwrap().color, SHARED_RW);

        s.mode = Some("edits".into());
        let g = line3(&s, 26, Tier::Plain);
        let t = glyphs_to_string(&g);
        assert!(t.contains('⇄') && t.contains('E'));
        assert_eq!(width(&t), 26);
    }

    #[test]
    fn line3_no_badge_no_shared() {
        let mut s = sess(State::Working);
        s.model = None;
        s.effort = None;
        let g = line3(&s, 26, Tier::Plain);
        let t = glyphs_to_string(&g);
        assert_eq!(t.len(), 26);
        assert!(t.chars().all(|c| c == ' '));
    }

    #[test]
    fn usage_bar_len_and_rounding() {
        for pct in [0.0, 1.0, 50.0, 99.6, 100.0] {
            assert_eq!(usage_bar(13, pct, Tier::Plain).len(), 13);
            assert_eq!(usage_bar(13, pct, Tier::Plain), usage_bar(13, pct, Tier::Rich), "same bar in both tiers");
        }
        assert!(usage_bar(10, 0.0, Tier::Plain).iter().all(|g| g.ch == '━' && g.color == EMPTY_TRACK));
        assert!(usage_bar(10, 100.0, Tier::Plain).iter().all(|g| g.ch == '━' && g.color != EMPTY_TRACK));
    }

    #[test]
    fn threshold_colors() {
        assert_eq!(threshold_color(69.0), THRESH_LO);
        assert_eq!(threshold_color(70.0), THRESH_MID);
        assert_eq!(threshold_color(89.99), THRESH_MID);
        assert_eq!(threshold_color(90.0), THRESH_HI);
    }

    #[test]
    fn line3_kind_mark_and_opencode_badge() {
        let mut s = sess(State::Idle);
        s.model = Some("opus 5.5".into());
        s.effort = Some("high".into());
        s.mode = Some("auto".into());
        let g = line3(&s, 26, Tier::Plain);
        assert_eq!(g[3].ch, '✻', "the claude kind's mark under the state icon");
        assert!(glyphs_to_string(&g).contains("opus 5.5 · high"));
        s.kind = "opencode".into();
        s.model = Some("deepseek v4 flash".into());
        s.effort = None;
        s.mode = Some("oc-build".into());
        let g = line3(&s, 26, Tier::Plain);
        assert_eq!(g[3].ch, '▣');
        let t = glyphs_to_string(&g);
        assert!(t.contains("deepseek v4 flash"), "{t:?}");
        assert_eq!(g.last().unwrap().ch, 'B', "the agent in the mode badge slot");
        assert_eq!(g.len(), 26, "the sidebar's card width at 28 columns");
        s.kind = "shell".into();
        s.model = None;
        s.mode = None;
        assert!(glyphs_to_string(&line3(&s, 26, Tier::Plain)).trim().is_empty(), "shells: no mark");
    }

    #[test]
    fn line2_restart_note_replaces_the_label() {
        let mut s = sess(State::Idle);
        s.note = Some("restarted".into());
        let g = line2(&s, 24, Tier::Plain);
        assert!(glyphs_to_string(&g).contains("restarted"));
        assert!(g.iter().any(|x| x.color == C_YOUR_TURN));
        s.note = Some("restarting".into());
        s.state = State::Starting;
        assert!(glyphs_to_string(&line2(&s, 24, Tier::Plain)).contains("restarting"));
        s.note = Some("archiving".into());
        s.state = State::Idle;
        let g = line2(&s, 24, Tier::Plain);
        assert!(glyphs_to_string(&g).contains("archiving") && !glyphs_to_string(&g).contains("idle"));
        assert!(g.iter().any(|x| x.color == LABEL_GREY));
        s.note = Some("moving".into());
        s.state = State::Idle;
        let g = line2(&s, 24, Tier::Plain);
        assert!(glyphs_to_string(&g).contains("moving") && !glyphs_to_string(&g).contains("idle"));
        assert!(g.iter().any(|x| x.color == LABEL_GREY));
        s.note = Some("moved".into());
        s.state = State::Idle;
        let g = line2(&s, 24, Tier::Plain);
        assert!(glyphs_to_string(&g).contains("moved") && !glyphs_to_string(&g).contains("idle"));
        assert!(g.iter().any(|x| x.color == C_YOUR_TURN));
        s.note = None;
        s.state = State::Starting;
        assert!(glyphs_to_string(&line2(&s, 24, Tier::Plain)).contains("starting"));
    }

    #[test]
    fn unread_working_icon_is_blue() {
        // SPEC §5: a working session with an unread finding has a blue card: icon and edge #5c9cf5, the line-2 text
        // (activity, or the "working" label) #5b8fd6. The spinner keeps turning, the name and age stay grey, and
        // the flag means nothing in other states.
        let mut s = sess(State::Working);
        s.activity = Some("editing deploy.tf".into());
        assert_eq!(line1(&s, 0, 29, NAME, Tier::Plain)[2].color, C_WORKING);
        assert_eq!(card_edge_color(s.state, s.unread), C_WORKING);
        s.unread = true;
        let l = line1(&s, 0, 29, NAME, Tier::Plain);
        assert_eq!(l[2].color, C_UNREAD);
        assert_eq!(l[2].ch, glyph(Tier::Plain, State::Working, 0));
        assert!(l.iter().any(|g| g.ch == 'p' && g.color == NAME), "the name stays grey");
        assert!(l.iter().any(|g| g.ch != ' ' && g.color == AGE), "the age stays grey");
        assert_eq!(card_edge_color(s.state, s.unread), C_UNREAD);
        let l2 = line2(&s, 29, Tier::Plain);
        assert!(l2.iter().any(|g| g.ch == 'e' && g.color == C_UNREAD_TEXT), "the activity is blue");
        assert!(!l2.iter().any(|g| g.color == C_WORKING_DIM));
        s.activity = None;
        let l2 = line2(&s, 29, Tier::Plain);
        assert!(glyphs_to_string(&l2).contains("working"));
        assert!(l2.iter().any(|g| g.ch == 'k' && g.color == C_UNREAD_TEXT), "the label is blue");
        s.state = State::YourTurn;
        assert_eq!(session_glyph_color(&s), C_YOUR_TURN);
        assert_eq!(card_edge_color(s.state, s.unread), C_YOUR_TURN);
    }

    #[test]
    fn edge_colour_per_state() {
        assert_eq!(edge_color(State::NeedsYou), C_NEEDS_YOU);
        assert_eq!(edge_color(State::Working), C_WORKING);
        assert_eq!(edge_color(State::YourTurn), C_YOUR_TURN);
        assert_eq!(edge_color(State::Down), C_DOWN);
        assert_eq!(edge_color(State::Idle), EDGE_IDLE);
        assert_eq!(edge_color(State::Unknown), EDGE_IDLE);
    }

    #[test]
    fn limits_label_row() {
        let usage = Usage {
            five_hour: Some(Window { used_percentage: 28.0, resets_at: 51600 }),
            seven_day: Some(Window { used_percentage: 13.0, resets_at: 51720 }),
        };
        let (mark, color) = limits_mark("claude");
        let lines = limits_lines(&[(Some((mark, color, "Claude".into())), usage.clone())], 1000, 29, 0, Tier::Plain);
        assert_eq!(lines.len(), 4, "label, 5h, wk, blank: as high as the unlabelled panel");
        let t: Vec<String> = lines.iter().map(|l| glyphs_to_string(l)).collect();
        assert_eq!(t[0].trim_end(), "   ✻ Claude");
        assert_eq!(lines[0][3].color, ACCENT, "the Claude mark in its colour, in the cards' icon column");
        assert_eq!(lines[0][5].color, USAGE_LABEL);
        assert!(t[1].starts_with("   5h "));
        assert!(t[3].chars().all(|c| c == ' '));
        // two providers: each its label and windows, one blank row at the end
        let other = Usage { five_hour: Some(Window { used_percentage: 5.0, resets_at: 51600 }), seven_day: None };
        let lines = limits_lines(&[(Some(('✻', ACCENT, "Claude".into())), usage), (Some(('·', USAGE_LABEL, "Codex".into())), other)], 1000, 29, 0, Tier::Plain);
        assert_eq!(lines.len(), 6);
        assert_eq!(glyphs_to_string(&lines[3]).trim_end(), "   · Codex");
    }

    #[test]
    fn usage_lines_two_windows_layout() {
        let usage = Usage {
            five_hour: Some(Window { used_percentage: 28.0, resets_at: 51600 }),
            seven_day: Some(Window { used_percentage: 13.0, resets_at: 51720 }),
        };
        let lines = usage_lines(&usage, 1000, 29, 0, Tier::Plain);
        assert_eq!(lines.len(), 4);
        for l in &lines {
            assert_eq!(l.len(), 29);
        }
        let t: Vec<String> = lines.iter().map(|l| glyphs_to_string(l)).collect();
        assert!(t[0].chars().all(|c| c == ' '), "line 0 should be blank");
        assert!(t[3].chars().all(|c| c == ' '), "line 3 should be blank");
        // row 1: five_hour -> "   5h " + bar (10) + " 28% 14:20" + 2 free columns (+ the pane border = 3)
        assert_eq!(&t[1][0..6], "   5h ");
        assert_eq!(lines[1][3].ch, '5', "label in the cards' icon column (grid col 3)");
        assert_eq!(lines[1][4].ch, 'h');
        assert_eq!(lines[1][3].color, USAGE_LABEL);
        assert!(!lines[1][3].bold);
        // the bar runs from col 6 for 10 cells
        assert!(matches!(lines[1][6].ch, '━' | '╸'), "bar starts col 6");
        assert!(matches!(lines[1][15].ch, '━' | '╸') && lines[1][16].ch == ' ', "bar ends col 15");
        // pct occupies the 4 cells right after bar+space (cols 17..21)
        let pctstr: String = lines[1][17..21].iter().map(|g| g.ch).collect();
        assert_eq!(pctstr, " 28%");
        assert!(t[1].contains("14:20"), "reset text present");
        assert!(t[1].ends_with("14:20  "), "2 free columns after the reset time, 3 with the pane border: {:?}", t[1]);
        // row 2: seven_day uses "wk"
        assert_eq!(&t[2][0..6], "   wk ");
        let pctstr2: String = lines[2][17..21].iter().map(|g| g.ch).collect();
        assert_eq!(pctstr2, " 13%");
        assert!(t[2].contains("14:22"));
    }

    #[test]
    fn only_seven_day() {
        let usage = Usage {
            five_hour: None,
            seven_day: Some(Window { used_percentage: 13.0, resets_at: 51720 }),
        };
        let lines = usage_lines(&usage, 1000, 29, 0, Tier::Plain);
        assert_eq!(lines.len(), 3); // blank + line + blank
        assert!(glyphs_to_string(&lines[1]).starts_with("   wk "));
        for l in &lines {
            assert_eq!(l.len(), 29);
        }
    }

    #[test]
    fn no_windows_empty() {
        let usage = Usage { five_hour: None, seven_day: None };
        assert!(usage_lines(&usage, 1000, 29, 0, Tier::Plain).is_empty());
    }

    #[test]
    fn expired_window() {
        let usage = Usage {
            five_hour: Some(Window { used_percentage: 43.0, resets_at: 100 }),
            seven_day: None,
        };
        let lines = usage_lines(&usage, 200, 26, 0, Tier::Plain);
        assert_eq!(lines.len(), 3); // blank + line + blank
        let s = glyphs_to_string(&lines[1]);
        assert!(s.contains('–'), "expired shows en dash");
        assert!(s.contains("new"), "expired reset text is \"new\"");
        for l in &lines {
            assert_eq!(l.len(), 26);
        }
    }

    #[test]
    fn high_pct_threshold_colour() {
        let usage = Usage {
            five_hour: Some(Window { used_percentage: 75.0, resets_at: 51600 }),
            seven_day: None,
        };
        let lines = usage_lines(&usage, 1000, 29, 0, Tier::Plain);
        for g in &lines[1][17..21] {
            assert_eq!(g.color, threshold_color(75.0));
            assert!(!g.bold);
        }
    }

    #[test]
    fn narrow_width_clamps_bar() {
        let usage = Usage {
            five_hour: Some(Window { used_percentage: 28.0, resets_at: 51600 }),
            seven_day: None,
        };
        let lines = usage_lines(&usage, 1000, 20, 0, Tier::Plain);
        assert_eq!(lines.len(), 3);
        for l in &lines {
            assert_eq!(l.len(), 20);
        }
        // barw clamps to 4: bar occupies cols 6..10
        assert!(matches!(lines[1][6].ch, '━' | '╸'));
        assert!(matches!(lines[1][9].ch, '━' | '╸'));
    }

    #[test]
    fn reset_short_same_day_other_day() {
        let t = 1609498800; // 2021-01-01 11:00 UTC (Fri)
        // same local day -> HH:MM
        assert_eq!(reset_short(t + 3600, t, 0), "12:00");
        // other local day -> weekday
        assert_eq!(reset_short(t + 90000, t, 0), "Sat");
        // expired -> "new"
        assert_eq!(reset_short(t - 10, t, 0), "new");
        // tz_offset honoured: +3600 shifts the local hour
        assert_eq!(reset_short(t + 3600, t, 3600), "13:00");
    }

    #[test]
    fn parse_tz() {
        assert_eq!(parse_tz_offset("+0200"), 7200);
        assert_eq!(parse_tz_offset("-0500"), -18000);
        assert_eq!(parse_tz_offset("+0545"), 20700);
        assert_eq!(parse_tz_offset("garbage"), 0);
    }

}
