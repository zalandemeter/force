//! The 2-row header pane. Row 0 is blank; row 1 is a rule ` ── … ── ` over the agent pane with its labels set into
//! it: ` ≡ ` (the sidebar toggle), then the session on screen with its agent (✻ claude / ▣ opencode), model, effort
//! and permission mode; right-aligned the attention items for other live sessions (NEEDS_YOU then YOUR_TURN, in
//! index order; host markers first), `+N more`, the working count, ` ? ` (controls) and ` × ` (quit), each
//! separated by `─`. Snapshot polling, staleness, own-session lookup and `viewed` mirror the sidebar. Rendering is
//! a pure data structure (cells + click ranges), so it is unit-testable.

use std::collections::HashMap;
use std::io::Write;
use std::path::PathBuf;
use std::time::Instant;

use ratatui::style::Color;
use unicode_width::UnicodeWidthStr;

use crate::fx::AnimMode;
use crate::glyphs::{self, GlyphArg, Tier};
use crate::hosts;
use crate::model::{self, SPINNER};
use crate::snapshot::{self, Session, Snapshot, State};
use crate::tmux::{self, Tmux};

// --- palette / layout constants ----------------------------------------------------
pub const BG: Color = Color::Rgb(0x21, 0x21, 0x21); // #212121: the shell colour around the agent pane
pub const BUTTON_FG: Color = Color::Rgb(0xc6, 0xc6, 0xc6); // #c6c6c6
pub const BUTTON_BG: Color = Color::Rgb(0x2c, 0x2c, 0x2c); // #2c2c2c
pub const PILL_NEEDS_BG: Color = Color::Rgb(0x4a, 0x2f, 0x2a); // #4a2f2a
pub const PILL_NEEDS_FG0: Color = model::C_NEEDS_YOU;
pub const PILL_NEEDS_FG1: Color = Color::Rgb(0xc9, 0x8a, 0x7d); // #c98a7d row-1
pub const PILL_TURN_BG: Color = Color::Rgb(0x2f, 0x3d, 0x31); // #2f3d31
pub const PILL_TURN_FG0: Color = Color::Rgb(0xa7, 0xd4, 0xad); // #a7d4ad row-0 bold
pub const PILL_TURN_FG1: Color = Color::Rgb(0x87, 0xc0, 0x8f); // #87c08f row-1
pub const WORKING_FG: Color = Color::Rgb(0xd7, 0xaf, 0x5f); // #d7af5f
pub const MORE_FG: Color = Color::Rgb(0x8a, 0x8a, 0x8a); // #8a8a8a, the contrast floor for text
pub const DAEMON_FG: Color = Color::Rgb(0x6c, 0x6c, 0x6c); // #6c6c6c
pub const RULE: Color = Color::Rgb(0x3c, 0x3c, 0x3c); // #3c3c3c the panel rules
pub const TITLE_FG: Color = Color::Rgb(0xf2, 0xf2, 0xf2);
pub const QUIT_BG: Color = Color::Rgb(0x2c, 0x2c, 0x2c); // #2c2c2c

// The rule on row 1: a blank column and a `─` before the first label, a `─` and a blank column after the last.
const LEFT_LEAD: usize = 2; // ` ─`
const RIGHT_TRAIL: usize = 2; // `─ `

const FADE_MS: f64 = 300.0;
const OWN_CACHE_SECS: f64 = 2.0;
const MAX_RENDER_GAP_MS: f64 = 5000.0;

/// A single rendered cell of either row.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Cell {
    pub ch: char,
    pub fg: Color,
    pub bg: Color,
    pub bold: bool,
}

fn blank(bg: Color) -> Cell {
    Cell { ch: ' ', fg: bg, bg, bold: false }
}

/// A clickable column range on a row and the action it triggers.
#[derive(Clone, PartialEq, Debug)]
pub struct Click {
    pub start: usize,
    pub end: usize, // exclusive
    pub action: Action,
}

/// Actions a mouse click can trigger (mirrors headerbar.py).
#[derive(Clone, PartialEq, Debug)]
pub enum Action {
    Sidebar,
    Switch(String),
    Host(String),
    Controls,
    Quit,
}

/// The pure result of laying out the header: both rows and the clickable ranges.
pub struct Layout {
    pub row0: Vec<Cell>,
    pub row1: Vec<Cell>,
    pub clicks: Vec<Click>,
}

/// A detached process spawner (so tests can inject a recorder instead of running scripts).
pub trait Spawner: Send {
    /// Spawn `path args...` detached (new process/session), stdio null, do not wait.
    fn spawn(&self, path: &str, args: &[String]) -> Result<(), String>;
    /// Downcast hook so tests reading back a recorded spawner can inspect it.
    fn as_any(&self) -> &dyn std::any::Any;
}

/// Production spawner: new session (setsid, detached), stdio null, no wait.
pub struct RealSpawner;

extern "C" {
    /// POSIX: create a new session and detach from the controlling terminal.
    fn setsid() -> i32;
}

impl Spawner for RealSpawner {
    fn spawn(&self, path: &str, args: &[String]) -> Result<(), String> {
        use std::os::unix::process::CommandExt;
        let mut cmd = std::process::Command::new(path);
        cmd.args(args)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        // SAFETY: `pre_exec` runs in the child after fork but before exec. Calling `setsid()`
        // there detaches the spawned process into a brand-new session (new controlling terminal)
        // with no libc crate required. `setsid` takes no arguments and only returns an error
        // code, so passing a unit closure is sound; if it fails the child is still exec'd.
        unsafe {
            cmd.pre_exec(|| {
                let _ = setsid();
                Ok(())
            });
        }
        cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// One pill: a session attention pill or a host marker. `host` is Some(name) for a host marker, None for a
/// session pill.
#[derive(Clone, PartialEq, Debug)]
struct Pill {
    name: String,
    state: State,
    label: String,
    host: Option<String>,
    /// A count item (the sidebar is shown, so it names the sessions): how many sessions are in `state`;
    /// `name` is the first of them, the one a click switches to.
    count: Option<usize>,
}

fn pill_from(s: &Session) -> Pill {
    let label = match s.state {
        State::NeedsYou => match s.activity.as_deref() {
            Some("asks you a question") => "question",
            Some("plan to review") => "plan",
            _ => "permission",
        },
        _ => "finished",
    };
    Pill { name: s.name.clone(), state: s.state, label: label.to_string(), host: None, count: None }
}

/// A host marker pill: row 1 is `{needs} need you` when needs > 0 else `{turn} finished`; the pill
/// state is NeedsYou when needs > 0 else YourTurn.
/// The `force` launcher to run: FORCE_BIN (a development instance's launcher, SPEC §2.1) when set, else the
/// live install's ~/.local/bin/force.
fn force_bin(force_bin: Option<String>, home: Option<String>) -> String {
    match force_bin {
        Some(b) if !b.is_empty() => b,
        _ => format!("{}/.local/bin/force", home.unwrap_or_default()),
    }
}

fn host_marker(h: &hosts::OtherHost) -> Pill {
    let state = if h.needs > 0 { State::NeedsYou } else { State::YourTurn };
    let label = if h.needs > 0 {
        format!("{} need you", h.needs)
    } else {
        format!("{} finished", h.turn)
    };
    Pill { name: h.label.clone(), state, label, host: Some(h.name.clone()), count: None }
}

/// Attention pills: NEEDS_YOU first then YOUR_TURN, each group in `index` order, excluding the own
/// session and archived ones.
fn attention_pills(snap: &Snapshot, own: Option<&str>) -> Vec<Pill> {
    let own = own.unwrap_or("");
    let mut live: Vec<&Session> = snap
        .sessions
        .iter()
        .filter(|s| !s.archived && s.name != own && matches!(s.state, State::NeedsYou | State::YourTurn))
        .collect();
    live.sort_by_key(|s| s.index.unwrap_or(0));
    let mut out = Vec::new();
    for s in live.iter().filter(|s| s.state == State::NeedsYou) {
        out.push(pill_from(s));
    }
    for s in live.iter().filter(|s| s.state == State::YourTurn) {
        out.push(pill_from(s));
    }
    out
}

/// The attention items as counts, one per state (needs you, then your turn): `🔔 2`, `✓ 1`. Used while the
/// sidebar is shown, which already names those sessions; a click goes to the first of them.
fn count_pills(pills: Vec<Pill>) -> Vec<Pill> {
    let mut out: Vec<Pill> = Vec::new();
    for p in pills {
        match out.iter_mut().find(|q| q.host.is_none() && q.state == p.state) {
            Some(q) => *q.count.get_or_insert(1) += 1,
            None => out.push(Pill { count: Some(1), ..p }),
        }
    }
    out
}

fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

fn truncate_name(name: &str, limit: Option<usize>) -> String {
    match limit {
        None => name.to_string(),
        Some(l) => model::truncate(name, l),
    }
}

fn working_count(snap: &Snapshot) -> usize {
    snap.sessions.iter().filter(|s| !s.archived && s.state == State::Working).count()
}

fn spinner(now_ms: i64) -> char {
    let i = (((now_ms / 100) % 10).rem_euclid(10)) as usize;
    SPINNER.chars().nth(i).unwrap()
}

/// The working icon alone: the spinner (PLAIN) or the sidebar's filling pie (RICH).
fn working_icon(tier: Tier, now_ms: i64) -> String {
    match tier {
        Tier::Plain => spinner(now_ms).to_string(),
        Tier::Rich => crate::model::glyph(tier, State::Working, now_ms).to_string(),
    }
}

fn working_text(tier: Tier, now_ms: i64, n: usize) -> String {
    match tier {
        Tier::Plain => format!("{} {} working", spinner(now_ms), n),
        // RICH: the sidebar's filling pie (2-cell icon slot)
        Tier::Rich => format!("{}{}{} working", crate::model::glyph(tier, State::Working, now_ms), glyphs::gap(tier), n),
    }
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

fn ease_out_cubic(t: f64) -> f64 {
    let t = t.clamp(0.0, 1.0);
    1.0 - (1.0 - t).powi(3)
}

/// Blend a cell's foreground and background towards `BG` by `k` (k=1 keeps the cell, k=0 fades out).
fn fade_toward_bg(c: Cell, k: f64) -> Cell {
    if k >= 1.0 {
        return c;
    }
    Cell { ch: c.ch, fg: lerp(BG, c.fg, k), bg: lerp(BG, c.bg, k), bold: c.bold }
}

/// The stateful header app: snapshot/own polling, fade bookkeeping and layout.
pub struct Header {
    pub tmux: Box<dyn Tmux>,
    pub spawner: Box<dyn Spawner>,
    pub snapshot_path: PathBuf,
    share_dir: PathBuf,
    log_path: PathBuf,
    tmux_pane: Option<String>,
    glyphs_arg: GlyphArg,
    tier: Tier,
    now_ms: i64,
    w: usize,

    snapshot: Option<Snapshot>,
    last_mtime: Option<std::time::SystemTime>,
    last_read_ms: f64,

    own: Option<String>,
    own_cached_at: Option<Instant>,
    last_own: Option<String>,
    first_render: bool,

    /// Split (ui) mode state (SPEC §39): the header is a pane on the ui server; `host_own` is the HOST
    /// session the nested client (name == `nested`) is attached to, used for the pills/current exclude.
    ui_mode: bool,
    nested: Option<String>,
    host_own: Option<String>,
    host_tmux: Option<Box<dyn Tmux>>,

    prev_pills: Vec<String>,
    fade_start: HashMap<String, f64>,
    anims: AnimMode,
    cascade_start: Option<f64>,

    /// Remote-host presence (SPEC §40): the <state> dir, the remote we show (FORCE_HOST), this machine's
    /// hostname, the other hosts with something for us, and the shown host's connection status.
    state_dir: Option<PathBuf>,
    shown_host: String,
    local_label: String,
    others: Vec<hosts::OtherHost>,
    others_read_ms: f64,
    host_status: Option<hosts::HostStatus>,

    /// Last menu hit published (`None` = never published), for the publish-on-change rule. RefCell because
    /// `build_grid` takes `&self` yet must remember what went out.
    last_menu_hit: std::cell::RefCell<Option<Option<crate::tmux::MenuHit>>>,

    /// Whether this window shows the sidebar: then the attention items are counts (the sidebar names them);
    /// without it they name the sessions. Re-read on a resize (a toggle resizes the header) and with each
    /// snapshot read.
    pub sidebar_shown: bool,
}

impl Header {
    pub fn new(
        tmux: Box<dyn Tmux>,
        spawner: Box<dyn Spawner>,
        snapshot_path: PathBuf,
        share_dir: PathBuf,
        tmux_pane: Option<String>,
        glyphs_arg: GlyphArg,
        w: usize,
    ) -> Self {
        let log_path = snapshot_path
            .parent()
            .map(|p| p.join("header.log"))
            .unwrap_or_else(|| PathBuf::from("header.log"));
        let tier = match glyphs_arg.fixed_tier() {
            Some(t) => t,
            None => tmux_pane
                .as_ref()
                .and_then(|p| tmux::glyphs_option(tmux.as_ref(), p))
                .map(|v| glyphs::from_option(&v))
                .unwrap_or(Tier::Plain),
        };
        // Remote-host state (SPEC §40): FORCE_HOST is the remote shown; <state> comes from the ui server's
        // @force_state global option. Tests leave everything empty.
        let (shown_host, local_label, state_dir) = if cfg!(test) {
            (String::new(), String::new(), None)
        } else {
            let shown_host = std::env::var("FORCE_HOST").unwrap_or_default();
            let local_label = std::fs::read_to_string("/proc/sys/kernel/hostname")
                .map(|h| h.trim().to_string())
                .unwrap_or_default();
            let state_dir = tmux_pane
                .as_ref()
                .and_then(|p| tmux::pane_option(tmux.as_ref(), p, "#{@force_state}"))
                .filter(|s| !s.is_empty())
                .map(PathBuf::from);
            (shown_host, local_label, state_dir)
        };
        Header {
            tmux,
            spawner,
            snapshot_path,
            share_dir,
            log_path,
            tmux_pane,
            glyphs_arg,
            tier,
            now_ms: 0,
            w,
            snapshot: None,
            last_mtime: None,
            last_read_ms: f64::NEG_INFINITY,
            own: None,
            own_cached_at: None,
            last_own: None,
            first_render: true,
            ui_mode: false,
            nested: None,
            host_own: None,
            host_tmux: None,
            prev_pills: Vec::new(),
            fade_start: HashMap::new(),
            anims: AnimMode::Full,
            cascade_start: None,
            state_dir,
            shown_host,
            local_label,
            others: Vec::new(),
            others_read_ms: f64::NEG_INFINITY,
            host_status: None,
            last_menu_hit: std::cell::RefCell::new(None),
            sidebar_shown: false,
        }
    }

    /// Set the remote-host state_dir/shown host/local label (used by tests in place of env+tmux reads) and
    /// force a re-read of other hosts on the next poll.
    pub fn set_hosts(&mut self, state_dir: Option<PathBuf>, shown: &str, local_label: &str) {
        self.state_dir = state_dir;
        self.shown_host = shown.to_string();
        self.local_label = local_label.to_string();
        self.others_read_ms = f64::NEG_INFINITY;
    }

    pub fn tier(&self) -> Tier {
        self.tier
    }

    /// Force a fixed glyph tier (used by tests; production sets it at construction / via auto re-read).
    pub fn set_tier(&mut self, tier: Tier) {
        self.tier = tier;
        self.glyphs_arg = GlyphArg::Fixed(tier);
    }

    /// In auto mode, re-read the own session's `@force_glyphs` tmux option. Returns true when the tier
    /// changed (the caller should force a full redraw). In fixed mode this never changes anything.
    fn refresh_auto_tier(&mut self) -> bool {
        if !self.glyphs_arg.is_auto() {
            return false;
        }
        let opt = self
            .tmux_pane
            .as_ref()
            .and_then(|p| tmux::glyphs_option(self.tmux.as_ref(), p));
        let new = opt.map(|v| glyphs::from_option(&v)).unwrap_or(Tier::Plain);
        if new != self.tier {
            self.tier = new;
            return true;
        }
        false
    }

    /// React to SIGUSR1: the header has no arrival fade, but re-reads the auto glyph tier, re-detects ui mode
    /// (host-own change may need a @force_session republish), and redraws.
    pub fn sigusr1(&mut self) -> bool {
        self.refresh_ui_mode();
        self.refresh_auto_tier()
    }

    pub fn set_anims(&mut self, mode: AnimMode) {
        self.anims = mode;
        if mode.is_off() {
            self.fade_start.clear();
            self.cascade_start = None;
        }
    }

    pub fn set_now(&mut self, now_ms: i64) {
        self.now_ms = now_ms;
    }

    pub fn on_resize(&mut self, w: usize) {
        self.w = w.max(1);
        self.sidebar_shown = self.window_has_sidebar();
    }

    pub fn daemon(&self) -> bool {
        match &self.snapshot {
            Some(s) => snapshot::is_stale(s, self.now_ms),
            None => true,
        }
    }

    pub fn valid_snapshot(&self) -> bool {
        !self.daemon()
    }

    pub fn viewed(&self) -> bool {
        match self.display_own() {
            None => true,
            Some(own) => match &self.snapshot {
                None => true,
                Some(snap) => snap
                    .sessions
                    .iter()
                    .find(|s| s.name == own)
                    .map(|s| s.viewed)
                    .unwrap_or(true),
            },
        }
    }

    pub fn any_working(&self) -> bool {
        match &self.snapshot {
            Some(snap) => snap.sessions.iter().any(|s| !s.archived && s.state == State::Working),
            None => false,
        }
    }

    /// Stat the snapshot every ~100 ms, re-read on change and at least every 5 s; refresh the own
    /// session every 2 s; schedule fades for newly-appeared pills (only while viewed). Returns whether
    /// anything changed (so the caller can skip redraws).
    pub fn poll(&mut self, now_ms: i64) -> bool {
        self.set_now(now_ms);
        let mut changed = false;

        let mtime = std::fs::metadata(&self.snapshot_path).and_then(|m| m.modified()).ok();
        let file_changed = mtime != self.last_mtime;
        let gap = now_ms as f64 - self.last_read_ms;
        if file_changed || gap >= MAX_RENDER_GAP_MS {
            self.last_mtime = mtime;
            self.last_read_ms = now_ms as f64;
            let new_snap = snapshot::load(&self.snapshot_path);
            self.update_from_snapshot(new_snap, now_ms);
            // The 5 s idle re-check also re-reads the auto glyph tier (one tmux call per 5 s is fine);
            // a snapshot re-read always forces a redraw regardless of any tier change.
            self.refresh_auto_tier();
            self.sidebar_shown = self.window_has_sidebar();
            changed = true;
        }

        if self.update_own() || self.first_render {
            if self.first_render {
                self.first_render = false;
                // startup cascade: the whole header fades in together over 200 ms (only while viewed)
                if !self.anims.is_off() && self.viewed() {
                    self.cascade_start = Some(now_ms as f64);
                }
            }
            if self.own != self.last_own {
                self.last_own = self.own.clone();
                changed = true;
            }
        }

        // Remote hosts + shown-host status, re-read at ~1 Hz while a state dir is configured.
        if let Some(sd) = &self.state_dir {
            if now_ms as f64 - self.others_read_ms >= 1000.0 {
                self.others_read_ms = now_ms as f64;
                let new_others = hosts::other_hosts(sd, &self.shown_host, &self.local_label, now_ms);
                if new_others != self.others {
                    self.others = new_others;
                    changed = true;
                }
                if !self.shown_host.is_empty() {
                    let new_status = hosts::read_status(sd, &self.shown_host);
                    if new_status != self.host_status {
                        self.host_status = new_status;
                        changed = true;
                    }
                }
            }
        }
        changed
    }

    fn update_from_snapshot(&mut self, new: Option<Snapshot>, now_ms: i64) {
        let old_pills = std::mem::take(&mut self.prev_pills);
        // current attention pill names (only meaningful when the snapshot is valid)
        let new_pills: Vec<String> = match &new {
            Some(s) if !snapshot::is_stale(s, now_ms) => {
                attention_pills(s, self.display_own()).into_iter().map(|p| p.name).collect()
            }
            _ => Vec::new(),
        };
        self.prev_pills = new_pills.clone();
        self.snapshot = new;

        if self.viewed() && !self.anims.is_off() {
            for name in &new_pills {
                if !old_pills.contains(name) {
                    self.fade_start.insert(name.clone(), now_ms as f64);
                }
            }
        }
        // prune fades for pills that are no longer present
        self.fade_start.retain(|name, _| new_pills.iter().any(|n| n == name));
    }

    fn update_own(&mut self) -> bool {
        let now = Instant::now();
        let due = self
            .own_cached_at
            .map(|t| now.duration_since(t).as_secs_f64() >= OWN_CACHE_SECS)
            .unwrap_or(true);
        if !due {
            return false;
        }
        self.own_cached_at = Some(now);
        self.own = self
            .tmux_pane
            .as_ref()
            .and_then(|p| tmux::session_name(self.tmux.as_ref(), p));
        self.refresh_ui_mode();
        true
    }

    /// The session to exclude from attention pills / current: the HOST session in ui mode, else the ui session.
    fn display_own(&self) -> Option<&str> {
        if self.ui_mode {
            self.host_own.as_deref()
        } else {
            self.own.as_deref()
        }
    }

    /// Re-read split-mode window options and the host-own session (start / SIGUSR1 / every 2 s own re-read).
    fn refresh_ui_mode(&mut self) {
        let nested = self
            .tmux_pane
            .as_ref()
            .and_then(|p| tmux::pane_option(self.tmux.as_ref(), p, "#{@force_nested}"));
        let prev = self.host_own.clone();
        if nested.as_deref().map_or(false, |s| !s.is_empty()) {
            self.ui_mode = true;
            self.nested = nested;
            if self.host_tmux.is_none() {
                let prefix = self
                    .tmux_pane
                    .as_ref()
                    .and_then(|p| tmux::pane_option(self.tmux.as_ref(), p, "#{@force_host_tmux}"));
                self.host_tmux = Some(Box::new(tmux::HostTmux::from_option(prefix.as_deref().unwrap_or(""))));
            }
            if let (Some(h), Some(nest)) = (&self.host_tmux, &self.nested) {
                if let Some(c) = tmux::clients(h.as_ref()).into_iter().find(|c| &c.name == nest) {
                    self.host_own = Some(c.session);
                }
            }
        } else {
            self.ui_mode = false;
            self.nested = None;
            self.host_tmux = None;
            self.host_own = self.own.clone();
        }
        if self.host_own != prev {
            self.publish_force_session();
        }
    }

    /// Publish `@force_session` on the ui server ONLY when this header's window has no sidebar pane (the sidebar
    /// publishes it otherwise — the header is the fallback for e.g. a `@force_sidebar off` layout). The check
    /// mirrors lifecycle.py, which tags the pane started by `force-sidebar` as `@force_role=sidebar`.
    fn publish_force_session(&mut self) {
        if !self.ui_mode {
            return;
        }
        if self.window_has_sidebar() {
            return;
        }
        if let (Some(pane), Some(host_own)) = (&self.tmux_pane, &self.host_own) {
            let _ = tmux::set_window_option(self.tmux.as_ref(), pane, "@force_session", host_own);
        }
    }

    /// Whether the header's window contains a sidebar pane (`@force_role=sidebar`).
    fn window_has_sidebar(&self) -> bool {
        let win = match self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
            Some(w) => w,
            None => return false,
        };
        tmux::window_has_pane_role(self.tmux.as_ref(), &win, "sidebar")
    }

    /// True while any pill is still fading in (caller should redraw at a fast rate).
    pub fn fading(&self) -> bool {
        if self.anims.is_off() {
            return false;
        }
        self.fade_start.values().any(|start| (self.now_ms as f64 - start) < FADE_MS)
    }

    /// True during the startup cascade (~30 fps frames).
    pub fn cascade_running(&self) -> bool {
        self.cascade_start
            .map(|s| (self.now_ms as f64 - s) < 200.0)
            .unwrap_or(false)
    }

    pub fn has_snapshot(&self) -> bool {
        self.snapshot.is_some()
    }

    /// The header has no footer error span to expire, so this is always `None`.
    pub fn error_expiry(&self) -> Option<i64> {
        None
    }

    /// Deadline for the next frame: 30 fps while a pill is fading in or the cascade runs, 10 fps while
    /// the working spinner is live; otherwise no animation deadline (the loop falls back to the 5 s cap).
    pub fn next_frame_ms(&self) -> Option<i64> {
        if !self.viewed() {
            return None;
        }
        if self.fading() || self.cascade_running() {
            Some(self.now_ms + 33)
        } else if self.any_working() {
            Some(self.now_ms + 100)
        } else {
            None
        }
    }

    // --- click handling ---------------------------------------------------------
    pub fn hit_test(&self, col: usize) -> Option<Action> {
        self.build_layout().clicks.iter().find(|c| c.start <= col && col < c.end).map(|c| c.action.clone())
    }

    pub fn on_click(&mut self, col: usize) -> bool {
        if let Some(a) = self.hit_test(col) {
            self.run_action(&a);
            true
        } else {
            false
        }
    }

    /// The most recently active client attached to the own session (as in the sidebar).
    fn own_client(&self) -> Option<String> {
        let own = self.own.as_ref()?;
        let clients = tmux::clients(self.tmux.as_ref());
        clients
            .iter()
            .filter(|c| &c.session == own)
            .max_by_key(|c| c.activity)
            .map(|c| c.name.clone())
    }

    fn log_error(&self, action: &str, err: &str) {
        let time = time_string();
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&self.log_path) {
            let _ = writeln!(f, "{} {}: {}", time, action, err);
        }
    }

    pub fn run_action(&mut self, action: &Action) {
        match action {
            Action::Sidebar => self.do_sidebar(),
            Action::Switch(n) => self.do_switch(n),
            Action::Host(n) => self.do_host(n),
            Action::Controls => self.do_controls(),
            Action::Quit => self.do_quit(),
        }
    }

    /// Clicking an other-host marker rebuilds this ui window for that host (`force ui-host NAME --window <id> --attention`: on the session that needs you).
    fn do_host(&mut self, name: &str) {
        let win = match self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
            Some(w) => w,
            None => {
                self.log_error("host", "cannot resolve window_id");
                return;
            }
        };
        let bin = force_bin(std::env::var("FORCE_BIN").ok(), std::env::var("HOME").ok());
        let args = ["ui-host", name, "--window", &win, "--attention"]
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>();
        if let Err(e) = self.spawner.spawn(&bin, &args) {
            self.log_error("host", &e);
        }
    }

    fn do_sidebar(&mut self) {
        let sock = match tmux::socket_path(self.tmux.as_ref()) {
            Some(s) => s,
            None => {
                self.log_error("sidebar", "cannot resolve socket_path");
                return;
            }
        };
        let win = match self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
            Some(w) => w,
            None => {
                self.log_error("sidebar", "cannot resolve window_id");
                return;
            }
        };
        let script = self.share_dir.join("sidebar-toggle.sh").to_string_lossy().to_string();
        if let Err(e) = self.spawner.spawn(&script, &[sock, win]) {
            self.log_error("sidebar", &e);
        }
    }

    /// The controls button when tmux did not catch the click itself (the MouseDown1Pane binding opens the menu
    /// with the click): press the menu key for this terminal.
    fn do_controls(&mut self) {
        let client = match self.own_client() {
            Some(c) => c,
            None => {
                self.log_error("controls", "no client for own session");
                return;
            }
        };
        if let Err(e) = tmux::open_menu(self.tmux.as_ref(), &client) {
            self.log_error("controls", &e);
        }
    }

    fn do_quit(&mut self) {
        let client = match self.own_client() {
            Some(c) => c,
            None => {
                self.log_error("quit", "no client for own session");
                return;
            }
        };
        if let Err(e) = tmux::detach_client(self.tmux.as_ref(), &client) {
            self.log_error("quit", &e);
        }
    }

    fn do_switch(&mut self, name: &str) {
        if self.ui_mode {
            // split mode: switch the HOST nested client, then focus the main pane of my ui window, and optimistically
            // adopt `name` as host own + publish it (the arrival effect, if any, is the SIGUSR1 tier re-read).
            let client = match &self.nested {
                Some(c) => c.clone(),
                None => {
                    self.log_error(&format!("switch:{}", name), "no nested client");
                    return;
                }
            };
            if let Some(host) = &self.host_tmux {
                if let Err(e) = tmux::switch_client(host.as_ref(), &client, name) {
                    self.log_error(&format!("switch:{}", name), &e);
                }
            }
            self.ui_select_main();
            if self.host_own.as_deref() != Some(name) {
                self.host_own = Some(name.to_string());
                self.publish_force_session();
            }
            self.refresh_auto_tier(); // arrival effect: as a SIGUSR1 would re-read the tier
            return;
        }
        let client = match self.own_client() {
            Some(c) => c,
            None => {
                self.log_error(&format!("switch:{}", name), "no client for own session");
                return;
            }
        };
        if let Err(e) = tmux::switch_client(self.tmux.as_ref(), &client, name) {
            self.log_error(&format!("switch:{}", name), &e);
        }
        if let Some(own) = &self.own {
            if let Some(pane) = tmux::agent_pane(self.tmux.as_ref(), own) {
                if let Err(e) = tmux::select_pane(self.tmux.as_ref(), &pane) {
                    self.log_error(&format!("switch:{}", name), &e);
                }
            }
        }
    }

    /// Focus the main (agent) pane of this header's ui window.
    fn ui_select_main(&mut self) {
        let Some(own) = &self.own else { return };
        if let Some(pane) = tmux::agent_pane(self.tmux.as_ref(), own) {
            if let Err(e) = tmux::select_pane(self.tmux.as_ref(), &pane) {
                self.log_error("switch", &e);
            }
        }
    }

    // --- layout / rendering -------------------------------------------------------
    /// Build the full header (both rows and click ranges) from the current state.
    /// The "panels" header: row 0 is blank, row 1 is a rule ` ── … ── ` over the agent pane with the
    /// labels set into it: ` ≡ ` (sidebar), the session on screen and its model on the left; the attention items,
    /// the working count, ` ? ` (controls) and ` × ` (quit) on the right, separated by `─`.
    pub fn build_layout(&self) -> Layout {
        let w = self.w;
        let row0 = vec![blank(BG); w];
        let mut row1 = vec![Cell { ch: '─', fg: RULE, bg: BG, bold: false }; w];
        if w >= 2 {
            // a plain rule with a blank column at each end: rounded corners here would hang over the pane's edge
            row1[0] = blank(BG);
            row1[w - 1] = blank(BG);
        }
        let mut clicks = Vec::new();
        let tier = self.tier;
        let sb = format!(" {}{}", glyphs::state(tier, '≡', '\u{EBF3}'), glyphs::gap(tier));
        let mut x = LEFT_LEAD;
        x = self.inlay(&mut row1, x, &sb, BUTTON_FG, false, Some(Action::Sidebar), &mut clicks) + 1;
        let left_end = x;

        if !self.valid_snapshot() {
            let text = if self.shown_host.is_empty() {
                " daemon not running ".to_string()
            } else {
                match &self.host_status {
                    Some(s) if s.state == "version" => format!(" {}: other Force version · force remote upgrade {} ", self.shown_host, self.shown_host),
                    _ => format!(" {} offline · reconnecting ", self.shown_host),
                }
            };
            let room = w.saturating_sub(left_end + RIGHT_TRAIL);
            let shown: String = text.chars().take(room).collect();
            self.inlay(&mut row1, left_end, &shown, DAEMON_FG, false, None, &mut clicks);
            let mut rows = [row0, row1];
            {
                let [a, b] = &mut rows;
                self.cascade_blend(&mut [a, b]);
            }
            let [row0, row1] = rows;
            return Layout { row0, row1, clicks };
        }

        let snap = self.snapshot.as_ref().expect("valid snapshot");
        // right block, laid out right to left: ×, ?, working count
        let quit = format!(" {}{}", glyphs::state(tier, '×', '\u{F011}'), glyphs::gap(tier));
        let ctrl = format!(" {}{}", glyphs::state(tier, '?', '\u{F11C}'), glyphs::gap(tier));
        let n = working_count(snap);
        let working = if n == 0 {
            String::new()
        } else if self.sidebar_shown {
            // a count like the attention items beside it: ` ◐  1 `
            format!(" {}{}{} ", working_icon(tier, self.now_ms), glyphs::gap(tier), n)
        } else {
            format!(" {} ", working_text(tier, self.now_ms, n))
        };
        let mut right: Vec<(String, Color, Option<Action>)> = vec![(quit, BUTTON_FG, Some(Action::Quit)), (ctrl, BUTTON_FG, Some(Action::Controls))];
        if !working.is_empty() {
            right.push((working, WORKING_FG, None));
        }
        // drop parts that do not fit: working first, then quit, then controls
        let rw = |r: &Vec<(String, Color, Option<Action>)>| r.iter().map(|p| width(&p.0) + 1).sum::<usize>() + RIGHT_TRAIL - 1;
        while !right.is_empty() && left_end + rw(&right) > w {
            let drop = if right.len() == 3 { 2 } else { 0 };
            right.remove(drop.min(right.len() - 1));
        }
        let right_w = if right.is_empty() { RIGHT_TRAIL } else { rw(&right) };
        let mut rx = w.saturating_sub(right_w);
        let right_start = rx;
        for (text, fg, action) in right.iter().rev() {
            rx = self.inlay(&mut row1, rx, text, *fg, false, action.clone(), &mut clicks) + 1;
        }

        // attention items, right-aligned against the right block
        let mut pills: Vec<Pill> = self.others.iter().map(host_marker).collect();
        let own_pills = attention_pills(snap, self.display_own());
        pills.extend(if self.sidebar_shown { count_pills(own_pills) } else { own_pills });
        let pills_end = right_start; // each item already brings its `─` separator
        let min_title = 12usize;
        let avail = pills_end.saturating_sub(left_end + min_title);
        let (limit, kept) = fit_pills(&pills, avail, glyphs::extra(tier));
        let texts: Vec<(String, &Pill)> = pills
            .iter()
            .take(kept)
            .map(|p| {
                let lead = if p.host.is_some() {
                    glyphs::state(tier, '↔', '\u{EB3A}')
                } else if p.state == State::NeedsYou {
                    glyphs::state(tier, '!', '\u{F0F3}')
                } else {
                    glyphs::state(tier, '●', '\u{F058}')
                };
                // a host marker says how many sessions there want you: ` ↔ buildbox 2 need you `
                let label = if p.host.is_some() { format!(" {}", p.label) } else { String::new() };
                let body = match p.count {
                    Some(n) => n.to_string(),
                    None => truncate_name(&p.name, limit),
                };
                (format!(" {}{}{}{} ", lead, glyphs::gap(tier), body, label), p)
            })
            .collect();
        let more = if kept < pills.len() { format!(" +{} more ", pills.len() - kept) } else { String::new() };
        let total: usize = texts.iter().map(|t| width(&t.0) + 1).sum::<usize>() + if more.is_empty() { 0 } else { width(&more) + 1 };
        // what does not fit between the left side and the right block is not drawn at all (never over the toggle)
        let fits = pills_end >= left_end + total;
        let (texts, more) = if fits { (texts, more) } else { (Vec::new(), String::new()) };
        let total = if fits { total } else { 0 };
        let mut px = pills_end.saturating_sub(total);
        let pills_start = px;
        for (text, p) in &texts {
            let fg = if p.state == State::NeedsYou { PILL_NEEDS_FG0 } else { PILL_TURN_FG0 };
            let k = match self.fade_start.get(&p.name) {
                Some(start) if !self.anims.is_off() => ((self.now_ms as f64 - start) / FADE_MS).clamp(0.0, 1.0),
                _ => 1.0,
            };
            let action = match &p.host {
                Some(name) => Action::Host(name.clone()),
                None => Action::Switch(p.name.clone()),
            };
            px = self.inlay(&mut row1, px, text, lerp(RULE, fg, k), true, Some(action), &mut clicks) + 1;
        }
        if !more.is_empty() {
            self.inlay(&mut row1, px, &more, MORE_FG, false, None, &mut clicks);
        }

        // the session on screen and its model, in what is left between the sidebar button and the attention items
        if let Some(own) = self.display_own() {
            let room = pills_start.saturating_sub(left_end + 1);
            let sess = snap.sessions.iter().find(|s| s.name == own);
            let name = format!(" {} ", own);
            if width(&name) <= room {
                let mut after = self.inlay(&mut row1, left_end, &name, TITLE_FG, true, None, &mut clicks);
                // which agent runs it: its mark in its colour, then its name
                let kind = match sess.map(|s| s.kind.as_str()) {
                    Some("claude") => Some((model::KIND_CLAUDE, "claude")),
                    Some("opencode") => Some((model::KIND_OPENCODE, "opencode")),
                    _ => None,
                };
                let kinded = kind.is_some();
                if let Some(((mark, color), word)) = kind {
                    let k = format!("{mark} {word} ");
                    if after + width(&k) <= left_end + room {
                        self.inlay(&mut row1, after, &mark.to_string(), color, true, None, &mut clicks);
                        after = self.inlay(&mut row1, after + 1, &format!(" {word} "), BUTTON_FG, false, None, &mut clicks);
                    }
                }
                let meta: Vec<String> = sess
                    .map(|s| [s.model.clone(), s.effort.clone()].into_iter().flatten().collect())
                    .unwrap_or_default();
                if !meta.is_empty() {
                    let m = format!("{}{} ", if kinded { "· " } else { "" }, meta.join(" · "));
                    if after + width(&m) <= left_end + room {
                        after = self.inlay(&mut row1, after, &m, MORE_FG, false, None, &mut clicks);
                    }
                }
                // the permission mode, in its badge colour (the same colours as the card's mode letter)
                let mode = sess.and_then(|s| s.mode.as_deref()).and_then(|md| {
                    let word = match md {
                        "auto" => "auto",
                        "edits" => "accept edits",
                        "plan" | "oc-plan" => "plan",
                        "manual" => "manual",
                        "bypass" => "bypass",
                        "oc-build" => "build",
                        _ => return None,
                    };
                    model::MODE_BADGES.iter().find(|(k, _, _)| *k == md).map(|(_, _, c)| (word, *c))
                });
                if let Some((word, color)) = mode {
                    let sep = if meta.is_empty() && !kinded { "" } else { "· " };
                    if after + width(sep) + width(word) + 1 <= left_end + room {
                        after = self.inlay(&mut row1, after, sep, MORE_FG, false, None, &mut clicks);
                        self.inlay(&mut row1, after, &format!("{word} "), color, false, None, &mut clicks);
                    }
                }
            }
        }
        let mut rows = [row0, row1];
        {
            let [a, b] = &mut rows;
            self.cascade_blend(&mut [a, b]);
        }
        let [row0, row1] = rows;
        Layout { row0, row1, clicks }
    }

    /// Write `text` into the rule at `x` (on the header colour) and return the column after it; with an action,
    /// record its click range.
    #[allow(clippy::too_many_arguments)]
    fn inlay(&self, row: &mut [Cell], x: usize, text: &str, fg: Color, bold: bool, action: Option<Action>, clicks: &mut Vec<Click>) -> usize {
        let w = row.len();
        let mut xi = x;
        for ch in text.chars() {
            if xi + 1 >= w {
                break;
            }
            row[xi] = Cell { ch, fg, bg: BG, bold: bold && ch != ' ' };
            xi += 1;
        }
        if let Some(a) = action {
            if xi > x {
                clicks.push(Click { start: x, end: xi, action: a });
            }
        }
        xi
    }

    /// Startup cascade: a single fade of the whole header from BG over 200 ms (viewed only).
    fn cascade_alpha(&self) -> Option<f64> {
        let s = self.cascade_start?;
        if self.anims.is_off() || !self.viewed() {
            return None;
        }
        let t = ease_out_cubic((self.now_ms as f64 - s) / 200.0);
        if t >= 1.0 {
            None
        } else {
            Some(t)
        }
    }

    fn cascade_blend(&self, rows: &mut [&mut Vec<Cell>]) {
        if let Some(a) = self.cascade_alpha() {
            for row in rows.iter_mut() {
                for cell in row.iter_mut() {
                    *cell = fade_toward_bg(*cell, a);
                }
            }
        }
    }

    /// Convenience: build the 2-row cell grid (for rendering).
    pub fn build_grid(&self) -> Grid {
        let layout = self.build_layout();
        self.publish_menu_hit(&layout);
        Grid { w: self.w, rows: vec![layout.row0, layout.row1] }
    }

    /// Publish where the `? controls` button sits so tmux's own `MouseDown1Pane` binding can open the controls
    /// menu. The button spans both rows (y0=0, y1=2); when it is dropped (narrow header) or there is no
    /// valid snapshot, publish `None`. Publishes on the header's OWN pane's server/pane and only when the value
    /// changed since the last frame; a tmux error is logged and not retried until the value changes.
    fn publish_menu_hit(&self, layout: &Layout) {
        let Some(pane) = &self.tmux_pane else { return };
        let hit = layout
            .clicks
            .iter()
            .find(|c| c.action == Action::Controls)
            .map(|c| crate::tmux::MenuHit { kind: "controls", x0: c.start, x1: c.end, y0: 0, y1: 2 });
        let mut cache = self.last_menu_hit.borrow_mut();
        if matches!(&*cache, Some(prev) if prev == &hit) {
            return;
        }
        if let Err(e) = tmux::set_menu_hit(self.tmux.as_ref(), pane, hit.as_ref()) {
            self.log_error("menu_hit", &e);
        }
        *cache = Some(hit);
    }
}

/// 2-row header grid (pure data, paintable).
pub struct Grid {
    pub w: usize,
    rows: Vec<Vec<Cell>>,
}

impl Grid {
    pub fn row(&self, y: usize) -> &[Cell] {
        &self.rows[y.min(1)]
    }
    pub fn height(&self) -> usize {
        2
    }
}

/// Decide how many columns a kept set of pills occupies at a given name length. Each pill spans
/// 2 caps + max(row0 body = ` ● <name> `, row1 body = `   <label> `) plus one BG gap column.
fn pills_width(pills: &[Pill], limit: Option<usize>, kept: usize, extra: usize) -> usize {
    let mut sum = 0usize;
    for p in pills.iter().take(kept) {
        if let Some(n) = p.count {
            sum += width(&n.to_string()) + 4 + extra + 1; // ` ● <n> ` + one `─`
            continue;
        }
        let label = if p.host.is_some() { width(&p.label) + 1 } else { 0 }; // a host marker's ` 2 need you`
        sum += width(&truncate_name(&p.name, limit)) + label + 4 + extra + 1; // ` ● <name> ` + one `─`
    }
    if kept < pills.len() {
        sum += width(&format!(" +{} more ", pills.len() - kept)) + 1;
    }
    sum
}

/// The fitting sequence: unlimited, then names 14 then 10, then drop pills from the end showing
/// `+N more`; if even that does not fit, the caller crops from the right of the pills area.
fn fit_pills(pills: &[Pill], avail: usize, extra: usize) -> (Option<usize>, usize) {
    let total = pills.len();
    if pills_width(pills, None, total, extra) <= avail {
        return (None, total);
    }
    if pills_width(pills, Some(14), total, extra) <= avail {
        return (Some(14), total);
    }
    if pills_width(pills, Some(10), total, extra) <= avail {
        return (Some(10), total);
    }
    let mut chosen = (Some(10), 0usize);
    for kept in (0..=total).rev() {
        if pills_width(pills, Some(10), kept, extra) <= avail {
            chosen = (Some(10), kept);
            break;
        }
        chosen = (Some(10), kept);
    }
    chosen
}

fn time_string() -> String {
    let out = std::process::Command::new("date")
        .arg("+%F %T")
        .stdin(std::process::Stdio::null())
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok());
    out.map(|s| s.trim().to_string()).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::{Session, Snapshot, State};
    use crate::tmux::Client;
    use std::sync::{Arc, Mutex};

    const NOW: i64 = 1_790_000_000_000;

    type Log = Arc<Mutex<Vec<Vec<String>>>>;
    type SpawnLog = Arc<Mutex<Vec<(String, Vec<String>)>>>;

    struct FakeTmux {
        log: Log,
        session: Option<String>,
        glyph: Arc<Mutex<Option<String>>>,
        clients_: Vec<Client>,
        agent: Option<String>,
        socket: Option<String>,
        win: Option<String>,
        nested: Option<String>,
        host_opt: Option<String>,
        prefix: Vec<String>,
    }

    impl FakeTmux {
        fn new() -> (Box<Self>, Log) {
            let log = Arc::new(Mutex::new(Vec::new()));
            let glyph = Arc::new(Mutex::new(None));
            (Box::new(FakeTmux { log: log.clone(), glyph: glyph.clone(), session: None, clients_: Vec::new(), agent: None, socket: None, win: None, nested: None, host_opt: None, prefix: Vec::new() }), log)
        }

        fn new_glyph() -> (Box<Self>, Arc<Mutex<Option<String>>>) {
            let log = Arc::new(Mutex::new(Vec::new()));
            let glyph = Arc::new(Mutex::new(None));
            (Box::new(FakeTmux { log, glyph: glyph.clone(), session: None, clients_: Vec::new(), agent: None, socket: None, win: None, nested: None, host_opt: None, prefix: Vec::new() }), glyph)
        }

        /// A session that has no attached clients at all.
        fn new_with_session_no_clients() -> (Box<Self>, Log) {
            let (mut t, log) = FakeTmux::new();
            t.session = Some("own".into());
            (t, log)
        }
    }

    impl Tmux for FakeTmux {
        fn run(&self, args: &[String]) -> Result<String, String> {
            let mut entry = self.prefix.clone();
            entry.extend(args.iter().cloned());
            self.log.lock().unwrap().push(entry);
            match args.first().map(|s| s.as_str()) {
                Some("display") => {
                    if args.iter().any(|a| a == "#{@force_glyphs}") {
                        Ok(self.glyph.lock().unwrap().clone().unwrap_or_default())
                    } else if args.iter().any(|a| a == "#{socket_path}") {
                        Ok(self.socket.clone().unwrap_or_default())
                    } else if args.iter().any(|a| a == "#{window_id}") {
                        Ok(self.win.clone().unwrap_or_default())
                    } else if args.iter().any(|a| a == "#{@force_nested}") {
                        Ok(self.nested.clone().unwrap_or_default())
                    } else if args.iter().any(|a| a == "#{@force_host_tmux}") {
                        Ok(self.host_opt.clone().unwrap_or_default())
                    } else {
                        Ok(self.session.clone().unwrap_or_default())
                    }
                }
                Some("list-clients") => Ok(self
                    .clients_
                    .iter()
                    .map(|c| format!("{}\t{}\t{}", c.name, c.session, c.activity))
                    .collect::<Vec<_>>()
                    .join("\n")),
                Some("list-panes") => Ok(self.agent.as_ref().map(|p| format!("agent\t{}", p)).unwrap_or_default()),
                _ => Ok(String::new()),
            }
        }
    }

    #[derive(Default, Clone)]
    struct FakeSpawner {
        log: SpawnLog,
    }

    impl Spawner for FakeSpawner {
        fn spawn(&self, path: &str, args: &[String]) -> Result<(), String> {
            self.log.lock().unwrap().push((path.to_string(), args.to_vec()));
            Ok(())
        }
        fn as_any(&self) -> &dyn std::any::Any {
            self
        }
    }

    fn sess(name: &str, index: i32, state: State, viewed: bool, activity: Option<&str>, archived: bool) -> Session {
        Session {
            name: name.into(),
            index: Some(index),
            state,
            since_ms: Some(NOW - 60_000),
            version: None,
            outdated: false,
            archived,
            order: index as i64,
            cwd: format!("/home/alice/{}", name),
            session_id: format!("s{}", index),
            pid: None,
            waiting_for: None,
            viewed,
            activity: activity.map(str::to_string),
            title: None,
            mode: None,
            model: None,
            effort: None,
            shared: None,
            note: None,
            kind: "claude".into(),
            support: false,
        }
    }

    fn header_with(snap: Option<Snapshot>, own: Option<&str>, w: usize) -> Header {
        let (tmux, _) = FakeTmux::new();
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(
            tmux,
            spawner,
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            w,
        );
        h.now_ms = NOW;
        h.own = own.map(str::to_string);
        h.snapshot = snap;
        h
    }

    fn header_with_tier(snap: Option<Snapshot>, own: Option<&str>, tier: Tier, w: usize) -> Header {
        let (tmux, _) = FakeTmux::new();
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(
            tmux,
            spawner,
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(tier),
            w,
        );
        h.now_ms = NOW;
        h.own = own.map(str::to_string);
        h.snapshot = snap;
        h
    }

    fn snap(sessions: Vec<Session>) -> Snapshot {
        Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions, usage: None, limits: vec![] }
    }

    /// The text of the rule row (row 1), where every label sits.
    fn rule_text(h: &Header) -> String {
        h.build_layout().row1.iter().map(|c| c.ch).collect()
    }

    fn find_sub(row: &[Cell], needle: &str) -> Option<usize> {
        let chars: Vec<char> = row.iter().map(|c| c.ch).collect();
        let n: Vec<char> = needle.chars().collect();
        chars.windows(n.len()).position(|w| w == n.as_slice())
    }

    fn find_text(row: &[Cell], needle: &str) -> usize {
        find_sub(row, needle).unwrap_or_else(|| panic!("{:?} not in {:?}", needle, row.iter().map(|c| c.ch).collect::<String>()))
    }

    #[test]
    fn rule_row_shape() {
        // row 0 blank; row 1 a `─` rule on BG with a blank column at each end, every label on BG
        let snap = snap(vec![sess("me", 0, State::Working, true, None, false), sess("w2", 1, State::Working, true, None, false)]);
        let layout = header_with(Some(snap), Some("me"), 100).build_layout();
        assert!(layout.row0.iter().all(|c| *c == blank(BG)), "row 0 blank");
        assert_eq!(layout.row1[0], blank(BG));
        assert_eq!(layout.row1[99], blank(BG));
        assert_eq!(layout.row1[1], Cell { ch: '─', fg: RULE, bg: BG, bold: false });
        assert_eq!(layout.row1[98], Cell { ch: '─', fg: RULE, bg: BG, bold: false });
        assert!(layout.row1.iter().all(|c| c.bg == BG), "no boxes: everything on the header colour");
        assert_eq!(find_text(&layout.row1, " ≡ "), 2, "sidebar toggle at col 2");
    }

    #[test]
    fn attention_items_order_colours_and_own_excluded() {
        let snap = snap(vec![
            sess("me", 1, State::NeedsYou, true, Some("asks you a question"), false), // own, excluded
            sess("b", 2, State::NeedsYou, true, Some("asks you a question"), false),
            sess("a", 0, State::NeedsYou, true, Some("plan to review"), false),
            sess("other", 5, State::YourTurn, true, None, false),
            sess("work", 3, State::Working, true, None, false),
        ]);
        let h = header_with(Some(snap), Some("me"), 150);
        let r = h.build_layout().row1;
        let (ia, ib, io) = (find_text(&r, " ! a "), find_text(&r, " ! b "), find_text(&r, " ● other "));
        assert!(ia < ib && ib < io, "NEEDS_YOU by index, then YOUR_TURN: a={} b={} other={}", ia, ib, io);
        assert!(find_sub(&r, "! me ").is_none(), "the own session has no item");
        assert_eq!((r[ia + 3].fg, r[ia + 3].bold), (PILL_NEEDS_FG0, true));
        assert_eq!((r[io + 3].fg, r[io + 3].bold), (PILL_TURN_FG0, true));
        assert_eq!(r[ia + 4].ch, ' ');
        assert_eq!(r[ia + 5], Cell { ch: '─', fg: RULE, bg: BG, bold: false }, "one `─` after each item");
    }

    #[test]
    fn attention_items_are_counts_while_the_sidebar_is_shown() {
        let snap = snap(vec![
            sess("me", 1, State::Idle, true, None, false),
            sess("b", 2, State::NeedsYou, true, Some("asks you a question"), false),
            sess("a", 0, State::NeedsYou, true, Some("plan to review"), false),
            sess("other", 5, State::YourTurn, true, None, false),
            sess("work", 3, State::Working, true, None, false),
        ]);
        let mut h = header_with(Some(snap.clone()), Some("me"), 150);
        h.sidebar_shown = true;
        let layout = h.build_layout();
        let r = &layout.row1;
        let (ineeds, iturn) = (find_text(r, " ! 2 "), find_text(r, " ● 1 "));
        assert!(ineeds < iturn, "needs-you count first, then your-turn");
        assert!(find_sub(r, " a ").is_none() && find_sub(r, "other").is_none(), "no session names");
        assert_eq!((r[ineeds + 3].fg, r[ineeds + 3].bold), (PILL_NEEDS_FG0, true));
        assert!(find_sub(r, " 1 working ").is_none() && find_sub(r, " 1 ").is_some(), "the working item is a count too");
        // a count goes to the first of its sessions (by index): "a"
        let click = layout.clicks.iter().find(|c| c.start == ineeds).expect("count click");
        assert_eq!(click.action, Action::Switch("a".into()));
        let pills = count_pills(attention_pills(&snap, Some("me")));
        assert_eq!(pills.len(), 2);
        assert_eq!(click.end - click.start + 1, pills_width(&pills[..1], None, 1, 0), "drawn width + its `─` = the estimate");
        // without the sidebar the items name the sessions again
        h.sidebar_shown = false;
        let r = h.build_layout().row1;
        assert!(find_sub(&r, " ! a ").is_some() && find_sub(&r, " 1 working ").is_some());
    }

    #[test]
    fn items_width_matches_fit() {
        let snap = snap(vec![sess("me", 1, State::Working, true, None, false), sess("portal", 2, State::YourTurn, false, None, false)]);
        let h = header_with(Some(snap.clone()), Some("me"), 150);
        let layout = h.build_layout();
        let click = layout.clicks.iter().find(|c| c.action == Action::Switch("portal".into())).expect("item click");
        let pills = attention_pills(&snap, Some("me"));
        assert_eq!(click.end - click.start + 1, pills_width(&pills, None, 1, 0), "drawn width + its `─` = the estimate");
    }

    #[test]
    fn title_with_agent_model_effort_and_mode() {
        let mut me = sess("me", 0, State::Idle, true, None, false);
        me.model = Some("opus 5.5".into());
        me.effort = Some("high".into());
        me.mode = Some("bypass".into());
        let h = header_with(Some(snap(vec![me.clone()])), Some("me"), 150);
        let r = h.build_layout().row1;
        let t = find_text(&r, " me ✻ claude · opus 5.5 · high · bypass ");
        assert_eq!(t, 6, "right after the toggle and its `─`");
        assert!(r[t + 1].bold && r[t + 1].fg == TITLE_FG, "session name bold, bright");
        assert_eq!(r[t + 4].fg, crate::model::KIND_CLAUDE.1, "the agent mark in its colour");
        let mode_at = find_text(&r, "bypass");
        assert_eq!(r[mode_at].fg, crate::model::BADGE_B, "mode word in its badge colour");
        for (mode, word) in [("auto", "auto"), ("edits", "accept edits"), ("plan", "plan"), ("manual", "manual"), ("oc-build", "build"), ("oc-plan", "plan")] {
            let mut s = me.clone();
            s.mode = Some(mode.into());
            let row = header_with(Some(snap(vec![s])), Some("me"), 150).build_layout().row1;
            assert!(find_sub(&row, &format!("· {word} ")).is_some(), "{mode} -> {word}");
        }
        // opencode and shell sessions
        let mut oc = me.clone();
        oc.kind = "opencode".into();
        oc.mode = Some("oc-build".into());
        let row = header_with(Some(snap(vec![oc])), Some("me"), 150).build_layout().row1;
        assert!(find_sub(&row, " me ▣ opencode · opus 5.5 · high · build ").is_some());
        let mut sh = sess("me", 0, State::Shell, true, None, false);
        sh.kind = "shell".into();
        let t = rule_text(&header_with(Some(snap(vec![sh])), Some("me"), 150));
        assert!(t.contains(" me ─") && !t.contains("claude"), "a shell shows just its name: {:?}", t);
    }

    #[test]
    fn title_gives_way_to_attention_items() {
        let mut me = sess("me", 0, State::Idle, true, None, false);
        me.model = Some("opus 5.5".into());
        me.effort = Some("high".into());
        me.mode = Some("auto".into());
        let s = snap(vec![me, sess("needsyou", 1, State::NeedsYou, true, None, false)]);
        let t = rule_text(&header_with(Some(s), Some("me"), 60));
        assert!(t.contains(" ! needsyou "), "the item stays: {:?}", t);
        assert!(!t.contains("opus 5.5 · high · auto"), "the title's details make room: {:?}", t);
    }

    #[test]
    fn rich_icons() {
        let snap = snap(vec![
            sess("me", 0, State::Working, true, None, false),
            sess("a", 1, State::NeedsYou, true, None, false),
            sess("b", 2, State::YourTurn, true, None, false),
        ]);
        let t = rule_text(&header_with_tier(Some(snap), Some("me"), Tier::Rich, 150));
        for (icon, what) in [('\u{F0F3}', "needs-you"), ('\u{F058}', "your-turn"), ('\u{EBF3}', "sidebar"), ('\u{F11C}', "controls"), ('\u{F011}', "quit")] {
            assert!(t.contains(icon), "{what} icon: {:?}", t);
        }
        assert!(!t.contains('!') && !t.contains('●') && !t.contains('≡') && !t.contains('×'), "no plain glyphs: {:?}", t);
    }

    #[test]
    fn fitting_truncates_to_14_then_10_then_more() {
        let long = snap(vec![sess("abcdefghijklmnopqrst", 0, State::NeedsYou, true, None, false)]);
        let widest = |w: usize| rule_text(&header_with(Some(long.clone()), Some("me"), w));
        let t = widest(150);
        assert!(t.contains("abcdefghijklmnopqrst"), "room: whole name: {:?}", t);
        let w14 = (20..150).find(|&w| widest(w).contains("abcdefghijklm…")).expect("a width with the 14-column name");
        assert!(!widest(w14).contains("nop"));
        let w10 = (20..w14).find(|&w| widest(w).contains("abcdefghi…")).expect("a narrower width with the 10-column name");
        assert!(w10 < w14);
        let three = snap(vec![
            sess("aaaa", 0, State::NeedsYou, true, None, false),
            sess("bbbb", 1, State::NeedsYou, true, None, false),
            sess("force", 2, State::NeedsYou, true, None, false),
        ]);
        let w = (20..150).find(|&w| rule_text(&header_with(Some(three.clone()), Some("me"), w)).contains("+2 more")).expect("+2 more");
        let t = rule_text(&header_with(Some(three), Some("me"), w));
        assert!(t.contains(" ! aaaa ") && !t.contains("bbbb") && !t.contains("force"), "first kept, the rest folded: {:?}", t);
    }

    #[test]
    fn right_block_alignment_and_working_count() {
        for w in [80usize, 150] {
            let snap = snap(vec![sess("me", 0, State::Working, true, None, false), sess("w2", 1, State::Working, true, None, false)]);
            let layout = header_with(Some(snap), Some("me"), w).build_layout();
            let r = &layout.row1;
            let quit = find_text(r, " × ");
            assert_eq!(quit, w - 5, "` × ` then `─` and the blank end column");
            let ctl = find_text(r, " ? ");
            assert_eq!(ctl, quit - 4, "` ? ` one `─` before quit");
            let wk = find_text(r, "working");
            assert_eq!(r[wk].fg, WORKING_FG);
            assert!(wk < ctl);
            let click = |a: Action| layout.clicks.iter().find(|c| c.action == a).cloned().expect("click");
            assert_eq!((click(Action::Quit).start, click(Action::Quit).end), (quit, quit + 3));
            assert_eq!((click(Action::Controls).start, click(Action::Controls).end), (ctl, ctl + 3));
        }
        let t = rule_text(&header_with(Some(snap(vec![sess("me", 0, State::Idle, true, None, false)])), Some("me"), 80));
        assert!(!t.contains("working"), "no working count at 0: {:?}", t);
        assert!(t.contains(" ? ") && t.contains(" × "));
    }

    #[test]
    fn daemon_not_running_rule() {
        let h = header_with(None, Some("me"), 80);
        let layout = h.build_layout();
        let t = rule_text(&h);
        assert!(t.contains(" daemon not running "), "{:?}", t);
        assert_eq!(layout.row1[find_text(&layout.row1, "daemon")].fg, DAEMON_FG);
        assert_eq!(layout.clicks.len(), 1, "only the sidebar toggle is clickable when down");
        assert_eq!(layout.clicks[0].action, Action::Sidebar);
        let mut s = snap(vec![sess("me", 0, State::Working, true, None, false)]);
        s.generated_ms = NOW - 10_000;
        assert!(rule_text(&header_with(Some(s), Some("me"), 80)).contains("daemon not running"), "stale = down");
    }

    #[test]
    fn hit_testing_each_segment() {
        let snap = snap(vec![sess("me", 0, State::Working, true, None, false), sess("aaa", 1, State::NeedsYou, true, None, false)]);
        let h = header_with(Some(snap), Some("me"), 150);
        let layout = h.build_layout();
        assert_eq!(h.hit_test(0), None);
        assert_eq!(h.hit_test(1), None, "the rule is not clickable");
        assert_eq!(h.hit_test(3), Some(Action::Sidebar));
        let pill = layout.clicks.iter().find(|c| matches!(&c.action, Action::Switch(_))).expect("item click");
        assert_eq!(h.hit_test(pill.start), Some(pill.action.clone()));
        assert_eq!(h.hit_test(pill.end - 1), Some(pill.action.clone()));
        assert_eq!(h.hit_test(pill.end), None, "the `─` after it is not");
        let ctl = layout.clicks.iter().find(|c| c.action == Action::Controls).unwrap();
        let qui = layout.clicks.iter().find(|c| c.action == Action::Quit).unwrap();
        assert_eq!(h.hit_test(ctl.start + 1), Some(Action::Controls));
        assert_eq!(h.hit_test(qui.start + 1), Some(Action::Quit));
    }

    #[test]
    fn header_auto_tier_reads_option_and_sigusr1() {
        let (mut tmux, glyph) = FakeTmux::new_glyph();
        tmux.session = Some("own".into());
        *glyph.lock().unwrap() = Some("rich".into());
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(tmux, spawner, PathBuf::from("/nonexistent/snapshot.json"), PathBuf::from("/share"), Some("%10".into()), GlyphArg::Auto, 80);
        h.own = Some("own".into());
        assert_eq!(h.tier(), Tier::Rich, "auto reads rich at startup");
        *glyph.lock().unwrap() = Some("plain".into());
        assert!(h.sigusr1(), "tier change forces a redraw");
        assert_eq!(h.tier(), Tier::Plain);
        *glyph.lock().unwrap() = None;
        assert!(!h.sigusr1(), "Plain -> absent option is not a change");
        assert_eq!(h.tier(), Tier::Plain);
        *glyph.lock().unwrap() = Some("rich".into());
        assert!(h.sigusr1());
        assert_eq!(h.tier(), Tier::Rich);
    }

    #[test]
    fn fake_tmux_quit_argv() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("own".into());
        tmux.clients_ = vec![Client { name: "c0".into(), session: "own".into(), activity: 9 }];
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(tmux, spawner, PathBuf::from("/nonexistent/snapshot.json"), PathBuf::from("/share"), Some("%10".into()), GlyphArg::Fixed(Tier::Plain), 80);
        h.own = Some("own".into());
        h.run_action(&Action::Quit);
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("detach-client")).expect("detach-client");
        assert_eq!(*e, ["detach-client", "-t", "c0"]);
    }

    #[test]
    fn fake_tmux_switch_argv_with_select_pane() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("own".into());
        tmux.clients_ = vec![Client { name: "c0".into(), session: "own".into(), activity: 11 }];
        tmux.agent = Some("%12".into());
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(tmux, spawner, PathBuf::from("/nonexistent/snapshot.json"), PathBuf::from("/share"), Some("%10".into()), GlyphArg::Fixed(Tier::Plain), 80);
        h.own = Some("own".into());
        h.run_action(&Action::Switch("demo".into()));
        let log = log.lock().unwrap();
        let sw = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("switch-client")).expect("switch-client");
        assert_eq!(*sw, ["switch-client", "-c", "c0", "-t", "=demo"]);
        let sel = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("select-pane")).expect("select-pane");
        assert_eq!(*sel, ["select-pane", "-t", "%12"]);
    }

    #[test]
    fn fake_tmux_switch_argv_ui_mode_host_and_ui_select() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("ui-sess".into());
        tmux.agent = Some("%12".into());
        let (mut host, hlog) = FakeTmux::new();
        host.clients_ = vec![Client { name: "/dev/pts/42".into(), session: "s0".into(), activity: 11 }];
        host.prefix = ["ssh", "-S", "/tmp/x", "h", "tmux", "-L", "force"].iter().map(|s| s.to_string()).collect();
        let spawner: Box<dyn Spawner> = Box::new(FakeSpawner::default());
        let mut h = Header::new(tmux, spawner, PathBuf::from("/nonexistent/snapshot.json"), PathBuf::from("/share"), Some("%10".into()), GlyphArg::Fixed(Tier::Plain), 80);
        h.own = Some("ui-sess".into());
        h.ui_mode = true;
        h.nested = Some("/dev/pts/42".into());
        h.host_own = Some("s0".into());
        h.host_tmux = Some(host);
        h.run_action(&Action::Switch("demo".into()));
        // host switch runs with the @force_host_tmux prefix + the nested client
        assert!(hlog.lock().unwrap().iter().any(|e| *e == ["ssh", "-S", "/tmp/x", "h", "tmux", "-L", "force", "switch-client", "-c", "/dev/pts/42", "-t", "=demo"]), "host switch argv");
        // ui select-pane of the main pane, and optimistic host own published when no sidebar is present
        assert!(log.lock().unwrap().iter().any(|e| *e == ["select-pane", "-t", "%12"]), "ui select-pane");
        assert_eq!(h.host_own.as_deref(), Some("demo"));
    }

    #[test]
    fn spawned_sidebar_and_controls_paths() {
        // sidebar
        let (mut tmux, _) = FakeTmux::new();
        tmux.socket = Some("/tmp/cockpit.sock".into());
        tmux.win = Some("@7".into());
        let spawner = FakeSpawner::default();
        let mut h = Header::new(
            tmux,
            Box::new(spawner),
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            80,
        );
        h.run_action(&Action::Sidebar);
        let sp = h.spawner;
        let calls = sp_log(sp.as_ref());
        assert_eq!(calls[0].0, "/share/sidebar-toggle.sh");
        assert_eq!(calls[0].1, ["/tmp/cockpit.sock", "@7"]);

        // controls needs a client for the own session
        let (mut tmux2, log2) = FakeTmux::new();
        tmux2.session = Some("own".into());
        tmux2.socket = Some("/tmp/cockpit.sock".into());
        tmux2.clients_ = vec![Client { name: "c0".into(), session: "own".into(), activity: 5 }];
        let spawner2 = FakeSpawner::default();
        let mut h2 = Header::new(
            tmux2,
            Box::new(spawner2),
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            80,
        );
        h2.own = Some("own".into());
        h2.run_action(&Action::Controls);
        assert!(sp_log(h2.spawner.as_ref()).is_empty(), "no script: the menu comes from the tmux config");
        let menu: Vec<String> = ["send-keys", "-K", "-c", "c0", "C-Space"].iter().map(|s| s.to_string()).collect();
        assert!(log2.lock().unwrap().iter().any(|c| *c == menu), "the menu key pressed for the own client");
    }

    fn sp_log(s: &dyn Spawner) -> Vec<(String, Vec<String>)> {
        let f = s.as_any().downcast_ref::<FakeSpawner>().expect("FakeSpawner");
        f.log.lock().unwrap().clone()
    }

    #[test]
    fn lerp_colors_all_channels() {
        // fade a NEEDS pill (coral-ish #e8806f on #4a2f2a) from BG at t=0, 0.5, 1
        let a = Color::Rgb(0xe8, 0x80, 0x6f);
        assert_eq!(lerp(a, BG, 0.0), a, "t=0 is the start colour");
        assert_eq!(lerp(a, BG, 1.0), BG, "t=1 is the end colour");
        // midpoint towards #212121: r=232-99.5->132, g=128-47.5->80, b=111-39->72
        assert_eq!(lerp(a, BG, 0.5), Color::Rgb(132, 80, 72));
        // clamping beyond [0,1]
        assert_eq!(lerp(a, BG, 2.0), BG);
        assert_eq!(lerp(a, BG, -0.5), a);
    }

    #[test]
    fn no_client_for_own_session_logs_and_does_nothing() {
        use std::fs;
        let dir = std::env::temp_dir().join(format!("cockpit-hdr-noclient-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        let (tmux, log) = FakeTmux::new_with_session_no_clients();
        let spawner = FakeSpawner::default();
        let mut h = Header::new(
            tmux,
            Box::new(spawner),
            dir.join("snapshot.json"),
            dir.clone(),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            80,
        );
        h.own = Some("own".into());

        h.run_action(&Action::Controls);
        h.run_action(&Action::Quit);
        h.run_action(&Action::Switch("demo".into()));

        // only list-clients probes happened (querying for the own session's client), no mutations
        for e in log.lock().unwrap().iter() {
            assert_eq!(e.first().map(|s| s.as_str()), Some("list-clients"), "unexpected tmux call: {:?}", e);
        }
        // nothing was spawned
        let sp = h.spawner;
        assert_eq!(sp_log(sp.as_ref()).len(), 0, "no spawn expected when no client");

        // every action is recorded in header.log
        let content = fs::read_to_string(dir.join("header.log")).expect("header.log written");
        let _ = fs::remove_dir_all(&dir);
        for needle in [
            "controls: no client for own session",
            "quit: no client for own session",
            "switch:demo: no client for own session",
        ] {
            assert!(content.contains(needle), "missing {:?} in log:\n{}", needle, content);
        }
    }

    // --- event-loop frame deadlines ---------------------------------------------------
    #[test]
    fn header_deadlines_idle_working_fading_hidden() {
        // idle: no animation deadline at all -> the loop falls back to the 5 s cap.
        let mut h = header_with(Some(snap(vec![sess("a", 0, State::Idle, true, None, false)])), None, 80);
        h.set_now(NOW);
        assert_eq!(h.next_frame_ms(), None, "idle header -> no frame deadline");

        // a viewed working session -> ~100 ms spinner.
        h.snapshot = Some(snap(vec![sess("a", 0, State::Working, true, None, false)]));
        h.set_now(NOW);
        assert_eq!(h.next_frame_ms(), Some(NOW + 100), "working + viewed -> spinner frame");

        // a pill fading in (viewed) -> ~33 ms.
        h.snapshot = Some(snap(vec![sess("a", 0, State::NeedsYou, true, None, false)]));
        h.set_now(NOW);
        h.fade_start.insert("a".into(), NOW as f64);
        assert_eq!(h.next_frame_ms(), Some(NOW + 33), "fading pill -> 30 fps");

        // startup cascade -> ~33 ms.
        h.fade_start.clear();
        h.cascade_start = Some(NOW as f64);
        assert_eq!(h.next_frame_ms(), Some(NOW + 33), "cascade -> 30 fps");

        // not viewed -> no deadlines even while working.
        h.cascade_start = None;
        h.snapshot = Some(snap(vec![sess("own", 0, State::Working, false, None, false)]));
        h.own = Some("own".into());
        h.set_now(NOW);
        assert_eq!(h.next_frame_ms(), None, "hidden -> no frame deadline");
    }

    // --- remote host markers / status (SPEC §40) ------------------------------------
    fn temp_state(tag: &str) -> std::path::PathBuf {
        use std::fs;
        let p = std::env::temp_dir().join(format!("cockpit-hdr-{}-{}", tag, std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn write_snapshot(dir: &std::path::Path, rel: &str, now_ms: i64, sessions: serde_json::Value) {
        use std::fs;
        let path = dir.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, serde_json::to_string(&serde_json::json!({"generated_ms": now_ms, "sessions": sessions})).unwrap()).unwrap();
    }

    fn write_status(dir: &std::path::Path, host: &str, state: &str) {
        use std::fs;
        let path = dir.join("remotes").join(host).join("status.json");
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, format!(r#"{{"state":"{}","detail":"","at":1.0}}"#, state)).unwrap();
    }

    fn sess_json(name: &str, state: &str) -> serde_json::Value {
        serde_json::json!({"name": name, "state": state})
    }

    fn host_header(snapshot_path: std::path::PathBuf, win: Option<&str>) -> (Header, FakeSpawner) {
        let (mut tmux, _) = FakeTmux::new();
        tmux.win = win.map(str::to_string);
        let spawner = FakeSpawner::default();
        let mut h = Header::new(
            tmux,
            Box::new(spawner.clone()),
            snapshot_path,
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            150,
        );
        h.now_ms = NOW;
        (h, spawner)
    }

    #[test]
    fn host_marker_drawn_before_session_pills() {
        use std::fs;
        let dir = temp_state("marker");
        write_snapshot(&dir, "snapshot.json", NOW, serde_json::json!([sess_json("sess1", "NEEDS_YOU")]));
        write_snapshot(&dir, "remotes/buildbox/snapshot.json", NOW, serde_json::json!([
            sess_json("a", "NEEDS_YOU"),
            sess_json("b", "NEEDS_YOU"),
        ]));
        write_status(&dir, "buildbox", "connected");
        let (mut h, _) = host_header(dir.join("snapshot.json"), None);
        h.set_hosts(Some(dir.clone()), "", "");
        h.poll(NOW);
        let layout = h.build_layout();
        let r1: String = layout.row1.iter().map(|c| c.ch).collect();
        // the marker names the host and how many sessions there want you (needs=2)
        assert!(r1.contains(" ↔ buildbox 2 need you "), "marker on the rule: {:?}", r1);
        // marker click switches host, and it comes before the session's item
        let host_click = layout.clicks.iter().find(|c| c.action == Action::Host("buildbox".into())).expect("host click");
        let sess_click = layout.clicks.iter().find(|c| c.action == Action::Switch("sess1".into())).expect("session click");
        assert!(host_click.end <= sess_click.start, "marker before session item");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn force_bin_prefers_the_instance_launcher() {
        assert_eq!(force_bin(Some("/x/force-dev".into()), Some("/home/u".into())), "/x/force-dev");
        assert_eq!(force_bin(Some(String::new()), Some("/home/u".into())), "/home/u/.local/bin/force");
        assert_eq!(force_bin(None, Some("/home/u".into())), "/home/u/.local/bin/force");
    }

    #[test]
        fn host_marker_click_runs_spawner() {
        let dir = temp_state("click");
        write_snapshot(&dir, "snapshot.json", NOW, serde_json::json!([sess_json("me", "IDLE")]));
        write_snapshot(&dir, "remotes/buildbox/snapshot.json", NOW, serde_json::json!([sess_json("a", "NEEDS_YOU")]));
        write_status(&dir, "buildbox", "connected");
        let (mut h, _spawner) = host_header(dir.join("snapshot.json"), Some("@7"));
        h.set_hosts(Some(dir.clone()), "", "");
        h.poll(NOW);
        let layout = h.build_layout();
        let click = layout.clicks.iter().find(|c| c.action == Action::Host("buildbox".into())).cloned().unwrap();
        h.run_action(&click.action);
        let calls = sp_log(h.spawner.as_ref());
        assert_eq!(calls[0].0, force_bin(std::env::var("FORCE_BIN").ok(), std::env::var("HOME").ok()));
        assert_eq!(calls[0].1, ["ui-host", "buildbox", "--window", "@7", "--attention"]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn host_marker_one_finished_variant() {
        let dir = temp_state("finished");
        write_snapshot(&dir, "snapshot.json", NOW, serde_json::json!([sess_json("me", "IDLE")]));
        write_snapshot(&dir, "remotes/bob/snapshot.json", NOW, serde_json::json!([sess_json("a", "YOUR_TURN")]));
        write_status(&dir, "bob", "connected");
        let (mut h, _) = host_header(dir.join("snapshot.json"), None);
        h.set_hosts(Some(dir.clone()), "", "");
        h.poll(NOW);
        let r1: String = h.build_layout().row1.iter().map(|c| c.ch).collect();
        assert!(r1.contains(" ↔ bob 1 finished "), "row 1: {:?}", r1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn no_markers_when_none_have_counts() {
        let dir = temp_state("nocounts");
        write_snapshot(&dir, "remotes/buildbox/snapshot.json", NOW, serde_json::json!([sess_json("a", "IDLE")]));
        write_status(&dir, "buildbox", "connected");
        let (mut h, _) = host_header(dir.join("snapshot.json"), None);
        h.set_hosts(Some(dir.clone()), "", "");
        h.poll(NOW);
        let layout = h.build_layout();
        assert!(!layout.clicks.iter().any(|c| matches!(&c.action, Action::Host(_))), "no host markers when no counts");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn remote_offline_and_version_text_when_snapshot_missing() {
        let dir = temp_state("status");
        write_status(&dir, "buildbox", "offline");
        let (mut h, _) = host_header(dir.join("snapshot.json"), None);
        h.set_hosts(Some(dir.clone()), "buildbox", "");
        h.poll(NOW);
        {
            let t = rule_text(&h);
            assert!(t.contains("buildbox offline · reconnecting"), "offline text: {:?}", t);
        }
        write_status(&dir, "buildbox", "version");
        h.set_hosts(Some(dir.clone()), "buildbox", "");
        h.poll(NOW);
        {
            let t = rule_text(&h);
            assert!(t.contains("buildbox: other Force version · force remote upgrade buildbox"), "version text: {:?}", t);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn daemon_not_running_unchanged_when_shown_host_empty() {
        let dir = temp_state("deamon-empty");
        let (mut h, _) = host_header(dir.join("snapshot.json"), None);
        h.set_hosts(Some(dir.clone()), "", "");
        h.poll(NOW);
        assert!(rule_text(&h).contains("daemon not running"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    // --- narrow widths: right block drops parts, status never over the sidebar button ----------------------

    /// Assert the sidebar button (cols 0..pills_start) in `layout` is byte-identical to the one drawn at w=128
    /// (where nothing is ever dropped or overlapped), plus that no click range ends past `w`.
    fn assert_narrow_layout(layout: &Layout, big: &Layout, pills_start: usize, w: usize, label: &str) {
        let mut cs = layout.clicks.clone();
        cs.sort_by_key(|c| c.start);
        for win in cs.windows(2) {
            assert!(win[0].end <= win[1].start, "{}: overlapping clicks at w={}: {:?} vs {:?}", label, w, win[0], win[1]);
        }
        for c in &layout.clicks {
            assert!(c.end <= w, "{}: click end {} past w={}: {:?}", label, c.end, w, c);
        }
        if w >= pills_start {
            for col in 0..pills_start {
                assert_eq!(layout.row0[col], big.row0[col], "{}: row0 col {} at w={}", label, col, w);
                assert_eq!(layout.row1[col], big.row1[col], "{}: row1 col {} at w={}", label, col, w);
            }
        }
    }

    #[test]
    fn narrow_widths_with_pills_right_block_drops_parts() {
        for tier in [Tier::Plain, Tier::Rich] {
            let snap = snap(vec![
                sess("w1", 0, State::Working, true, None, false),
                sess("w2", 1, State::Working, true, None, false),
                sess("n1", 2, State::NeedsYou, true, None, false),
            ]);
            let big = header_with_tier(Some(snap.clone()), Some("me"), tier, 128).build_layout();
            let pills_start = LEFT_LEAD + 3 + glyphs::extra(tier) + 1; // ` ≡ ` and its `─`: fixed at every width
            for w in 10..=70 {
                let h = header_with_tier(Some(snap.clone()), Some("me"), tier, w);
                let layout = h.build_layout();
                assert_narrow_layout(&layout, &big, pills_start, w, &format!("tier={:?} snap", tier));
            }
        }
    }

    #[test]
    fn narrow_widths_long_remote_status_never_over_button() {
        for tier in [Tier::Plain, Tier::Rich] {
            let host = "a".repeat(30); // long enough that the status text overflows even at w=70
            let mut big = header_with_tier(None, Some("me"), tier, 128);
            big.shown_host = host.clone();
            big.host_status = Some(hosts::HostStatus { state: "version".into(), detail: String::new() });
            let big = big.build_layout();
            let pills_start = LEFT_LEAD + 3 + glyphs::extra(tier) + 1; // ` ≡ ` and its `─`: fixed at every width
            for w in 10..=70 {
                let mut h = header_with_tier(None, Some("me"), tier, w);
                h.shown_host = host.clone();
                h.host_status = Some(hosts::HostStatus { state: "version".into(), detail: String::new() });
                let layout = h.build_layout();
                // only the sidebar button is clickable when there is no valid snapshot
                assert!(layout.clicks.iter().all(|c| c.action == Action::Sidebar), "only sidebar click: {:?}", layout.clicks);
                assert_narrow_layout(&layout, &big, pills_start, w, &format!("tier={:?} status", tier));
            }
        }
    }

    #[test]
    fn narrow_width_only_controls_fits() {
        // narrowing drops the working count first, then quit, then controls: some width keeps only controls
        for tier in [Tier::Plain, Tier::Rich] {
            let snap = snap(vec![
                sess("w1", 0, State::Working, true, None, false),
                sess("w2", 1, State::Working, true, None, false),
                sess("n1", 2, State::NeedsYou, true, None, false),
            ]);
            let has = |l: &Layout, a: Action| l.clicks.iter().any(|c| c.action == a);
            let w = (0..=128)
                .find(|&w| {
                    let l = header_with_tier(Some(snap.clone()), Some("me"), tier, w).build_layout();
                    has(&l, Action::Controls) && !has(&l, Action::Quit)
                })
                .expect("a width exists where only controls fits");
            let h = header_with_tier(Some(snap), Some("me"), tier, w);
            let layout = h.build_layout();
            let text: String = layout.row1.iter().map(|c| c.ch).collect();
            assert!(!text.contains("working"), "working count dropped at w={} tier={:?}: {:?}", w, tier, text);
            assert!(has(&layout, Action::Controls), "controls clickable at w={}", w);
            assert!(!has(&layout, Action::Quit), "quit not clickable at w={}", w);
            // a little wider: quit comes back before the working count does
            let back = (w + 1..=w + 10)
                .map(|w2| header_with_tier(Some(h.snapshot.clone().unwrap()), Some("me"), tier, w2).build_layout())
                .find(|l| has(l, Action::Quit))
                .expect("quit comes back a few columns wider");
            let text: String = back.row1.iter().map(|c| c.ch).collect();
            assert!(!text.contains("working"), "the working count is still dropped then: {:?}", text);
        }
    }

    #[test]
    fn publishes_controls_menu_hit_on_first_frame_only() {
        let (tmux, log) = FakeTmux::new();
        let mut h = Header::new(
            tmux,
            Box::new(FakeSpawner::default()),
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            128,
        );
        h.now_ms = NOW;
        h.own = Some("me".into());
        h.snapshot = Some(snap(vec![sess("me", 0, State::Working, true, None, false)]));
        let ctl = h
            .build_layout()
            .clicks
            .iter()
            .find(|c| c.action == Action::Controls)
            .cloned()
            .expect("controls click at w=128");
        let _ = h.build_grid(); // first frame publishes
        let _ = h.build_grid(); // identical second frame must publish nothing
        let log = log.lock().unwrap();
        let menus: Vec<Vec<String>> = log
            .iter()
            .filter(|e| e.first().map(|s| s.as_str()) == Some("set-option"))
            .cloned()
            .collect();
        assert_eq!(menus.len(), 1, "exactly one publish for two identical frames");
        assert_eq!(menus[0], [
            "set-option", "-p", "-t", "%10", "@force_menu_kind", "controls",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x0", &format!("{}", ctl.start),
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x1", &format!("{}", ctl.end),
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y0", "0",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y1", "2",
        ]);
    }

    #[test]
    fn publishes_none_when_controls_dropped() {
        // At w=9 the whole right block (working count, controls, quit) is dropped: only the sidebar toggle remains.
        let (tmux, log) = FakeTmux::new();
        let mut h = Header::new(
            tmux,
            Box::new(FakeSpawner::default()),
            PathBuf::from("/nonexistent/snapshot.json"),
            PathBuf::from("/share"),
            Some("%10".into()),
            GlyphArg::Fixed(Tier::Plain),
            9,
        );
        h.now_ms = NOW;
        h.own = Some("me".into());
        h.snapshot = Some(snap(vec![sess("me", 0, State::Idle, true, None, false)]));
        assert!(
            !h.build_layout().clicks.iter().any(|c| c.action == Action::Controls),
            "controls must be dropped at a width this narrow"
        );
        let _ = h.build_grid();
        let log = log.lock().unwrap();
        let m = log
            .iter()
            .find(|e| e.first().map(|s| s.as_str()) == Some("set-option"))
            .expect("a publish happens");
        assert_eq!(m[5], "", "kind is empty when controls is dropped");
        assert_eq!(m[12], "", "x0 empty");
        assert_eq!(m[19], "", "x1 empty");
        assert_eq!(m[26], "", "y0 empty");
        assert_eq!(m[33], "", "y1 empty");
    }
}
