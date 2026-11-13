//! Application state, layout, keyboard/mouse handling, and rendering into a cell grid. The grid is a
//! pure data structure so it is testable with ratatui's `TestBackend`; the terminal loop in main.rs
//! turns the grid into a `Buffer`.

use std::collections::HashMap;
use std::path::PathBuf;

use ratatui::style::Color;

use crate::fx::{AnimMode, Effects};
use crate::glyphs::{self, GlyphArg, Tier};
use crate::hosts;
use crate::model::{self as m, Glyph};
use crate::snapshot::{self, Session, Snapshot, State};
use crate::tmux::{self, Tmux};

pub const ARCHIVED_TARGET: &str = "__archived__";
pub const NEW_TARGET: &str = "__new__";
/// Chuck, Force's assistant (SPEC §50): the footer's `ask chuck` row and the rail's face.
pub const CHUCK_TARGET: &str = "__chuck__";
/// Chuck's face: Font Awesome's smile (Nerd Font) in the rich tier, `?` in the plain one.
pub const CHUCK_RICH: char = '\u{F118}';

#[derive(Clone, Copy, PartialEq, Debug)]
pub struct RowCell {
    pub ch: char,
    pub fg: Color,
    pub bg: Color,
    pub bold: bool,
}

pub struct Grid {
    pub w: usize,
    pub h: usize,
    rows: Vec<Vec<RowCell>>,
}

impl Grid {
    pub fn new(w: usize, h: usize) -> Self {
        let blank = RowCell { ch: ' ', fg: m::BG, bg: m::BG, bold: false };
        Grid { w, h, rows: vec![vec![blank; w]; h] }
    }
    pub fn row(&self, y: usize) -> &[RowCell] {
        &self.rows[y.min(self.h - 1)]
    }
    pub fn set(&mut self, y: usize, x: usize, cell: RowCell) {
        if y < self.h && x < self.w {
            self.rows[y][x] = cell;
        }
    }
    pub fn put_glyph(&mut self, y: usize, x: usize, g: Glyph, bg: Color, bold: bool) {
        self.set(y, x, RowCell { ch: g.ch, fg: g.color, bg, bold: bold || g.bold });
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    Session(String),
    Archived,
    New,
    Chuck,
    Daemon,
}

impl Target {
    pub fn str(&self) -> &str {
        match self {
            Target::Session(n) => n,
            Target::Archived => ARCHIVED_TARGET,
            Target::New => NEW_TARGET,
            Target::Chuck => CHUCK_TARGET,
            Target::Daemon => "",
        }
    }
}

struct Item {
    target: Target,
    top: usize,
    height: usize,
    /// the card's last visible row is a partial `▀` half-block (no text).
    partial: bool,
    lines: Vec<Vec<Glyph>>, // each width c (content)
    is_session: bool,
    current: bool,
    state: State,
    /// a working session with an unread finding: its edge is blue (SPEC §5).
    unread: bool,
    /// a working claude session waiting on a background command of its own: its edge is the dim working colour (SPEC §5).
    watching: bool,
    /// grid columns [from, to) this item owns when it shares its row with another (the one-row actions).
    cols: Option<(usize, usize)>,
}

pub struct App {
    pub tmux: Box<dyn Tmux>,
    pub snapshot_path: PathBuf,
    pub cockpit_bin: String,
    tmux_pane: Option<String>,
    /// The `--glyphs` selection (auto re-queries the tmux option).
    glyphs_arg: GlyphArg,
    /// The glyph tier currently in use; a tier change forces a full redraw.
    tier: Tier,
    /// host name shown under the wordmark
    host: String,
    /// Remote-host state (SPEC §40): the remote NAME we show (FORCE_HOST, empty for this machine), the
    /// <state> dir, its cached status, and the last time we read it.
    remote_host: String,
    state_dir: Option<PathBuf>,
    host_status: Option<hosts::HostStatus>,
    status_read_ms: f64,
    pub tz_offset: i32,
    pub now_ms: i64,
    pub w: usize,
    pub h: usize,
    pub c: usize,

    snapshot: Option<Snapshot>,
    last_mtime: Option<std::time::SystemTime>,
    last_read_ms: f64,

    own: Option<String>,
    own_cached_at: Option<std::time::Instant>,
    last_own: Option<String>,
    first_render: bool,

    /// Split (ui) mode state (SPEC §39): a separate ui-server pane (this sidebar), with a nested HOST client.
    /// Non-empty `@force_nested` on this pane's window <=> ui mode. `host_own` is the HOST session that
    /// nested client is attached to; the pane's own ui session (`own`) is kept for client lookups / glyphs / panes.
    ui_mode: bool,
    nested: Option<String>,
    host_own: Option<String>,
    host_tmux: Option<Box<dyn Tmux>>,
    /// the `@force_host_tmux` words `host_tmux` was built from (None: injected by a test, kept as it is)
    host_tmux_prefix: Option<String>,
    /// `@force_overlay` set: the main pane shows Chuck from this machine over a remote's list (SPEC §50)
    overlay: bool,

    items: Vec<Item>,
    offset: usize,
    list_h: usize,
    /// A list taller than its area pins the actions (archived, new session) to the bottom of it, and the
    /// session cards scroll in the viewport above them (rows view_top..view_top+view_h of the list area).
    pinned: bool,
    view_top: usize,
    view_h: usize,
    /// content height of the session cards; a blank row follows, then the actions at actions_top.
    sess_total: usize,
    actions_top: usize,
    /// 1 when new + archived share one row, 2 when the pane is too narrow for that.
    actions_rows: usize,
    /// hidden cards above / below the viewport, and whether one of them needs you (pinned only).
    more_up: (usize, bool),
    more_down: (usize, bool),
    usage_h: usize,
    footer_h: usize,
    /// rows of the bottom dock: a blank row, the host rule, then the limits (or one blank row); 0 = none
    dock_h: usize,
    /// rail (W < 16): absolute row -> clickable target for hit-testing (built each draw).
    rail_targets: Vec<(usize, Target)>,
    /// rail: whether the usage panel is shown at the bottom of this draw.
    rail_usage_show: bool,

    cursor: usize,
    /// keep the own card in view of a scrolled list (off after a mouse-wheel scroll until the next rest).
    follow_own: bool,

    /// Last menu hit published (`None` = never published), for the publish-on-change rule.
    last_menu_hit: Option<Option<crate::tmux::MenuHit>>,

    error: Option<(String, i64)>, // (message, expires_at_ms)

    prev_states: HashMap<String, State>,
    prev_usage: Option<(f64, f64)>,
    prev_live: std::collections::HashSet<String>,
    /// whether the sidebar has ever been rendered as viewed (drives the first-seen arrival fade).
    seen_viewed: bool,
    /// when the current-look fade-in started (`None` = instant current once viewed).
    current_fade_start: Option<f64>,
    /// when the logo's light sweep started (arrival / first view; `None` = no sweep).
    logo_shine_start: Option<f64>,

    /// Animation mode (from `--animations`).
    pub anims: AnimMode,
    /// WORKING -> YOUR_TURN finish flash, keyed by session name.
    finish_flash: HashMap<String, FinishFlash>,
    /// card grow (appearing) / shrink (disappearing) motions, keyed by name.
    card_motion: HashMap<String, CardMotion>,
    /// last-known sessions of outgoing (shrinking) cards, keyed by name.
    outgoing: HashMap<String, Session>,
    /// previous live list (for grow/shrink detection); None until the first live snapshot.
    had_live_list: bool,
    /// treat as viewed until this wall-clock ms (the SIGUSR1 grace window).
    viewed_until: Option<i64>,
    /// force an own-session repoll on the next update (SIGUSR1).
    force_own_once: bool,
    /// split mode: a host switch (nested client, target) clicked but not yet sent. The click shows the new current
    /// card at once and the main loop sends the switch after that frame (`run_pending`): on a ui on another
    /// computer the switch is an ssh round trip (~150-250 ms) the highlight must not wait for.
    pending_switch: Option<(String, String)>,
    /// (target, ms) of the last click switch: the switch hook's SIGUSR1 that follows it does not re-run the
    /// arrival fade on the card that is already shown as current.
    clicked: Option<(String, i64)>,

    pub fx: Effects,
}

const OWN_CACHE_SECS: f64 = 2.0;
/// How long a click on a card counts as "viewed" without the snapshot: the daemon's tick plus, on a ui on another
/// computer, the snapshot copy (the copy is ~0.3 s behind; generous so a slow tick never blinks the highlight off).
const CLICK_VIEWED_MS: i64 = 5000;
const MAX_RENDER_GAP_MS: f64 = 5000.0;
/// Number of brand rows pinned at the top of the pane (rows 0..BRAND_H); the list starts at BRAND_H.
const BRAND_H: usize = m::BRAND_ROWS; // the brand rule is the sessions panel's top rule, a blank row under it
/// The narrow rail keeps a 2-row brand (`c` + underline).
const RAIL_BRAND_H: usize = 2;
/// The usage panel is dropped only when it would leave the list fewer rows than this (about two cards).
const MIN_LIST_ROWS: usize = 8;
/// Below this many list rows the actions scroll with the cards (pinning would leave no room for them).
const PIN_MIN_LIST: usize = 7;
/// The own-card arrival fade is exactly 3 frames ~40 ms apart (blends 1/3, 2/3, 1).
const ARRIVE_STEP_MS: i64 = 40;
/// The logo's light sweep on arrival.
const LOGO_SHINE_MS: f64 = 700.0;
/// The finish-flash card background (#3f5a45) and shimmer highlight (#f2d49b).
const FINISH_DUR_MS: f64 = 600.0;
const FINISH_GLYPH_MS: f64 = 1200.0;
const CARD_DUR_MS: f64 = 160.0;
const SHIMMER_PERIOD_MS: f64 = 1600.0;
const FINISH_BG: Color = Color::Rgb(0x3f, 0x5a, 0x45);
const SHIMMER_FG: Color = Color::Rgb(0xf2, 0xd4, 0x9b);
const FINISH_CHECK: Color = Color::Rgb(0x87, 0xc0, 0x8f);

struct FinishFlash {
    start_ms: f64,
}

struct CardMotion {
    start_ms: f64,
    grow: bool,
}

impl App {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        tmux: Box<dyn Tmux>,
        snapshot_path: PathBuf,
        cockpit_bin: String,
        tmux_pane: Option<String>,
        glyphs_arg: GlyphArg,
        tz_offset: i32,
        w: usize,
        h: usize,
    ) -> Self {
        let c = w.saturating_sub(2);
        // Resolve the initial tier. `auto` queries the pane's own session `@force_glyphs` right now.
        let tier = match glyphs_arg.fixed_tier() {
            Some(t) => t,
            None => tmux_pane
                .as_ref()
                .and_then(|p| tmux::glyphs_option(tmux.as_ref(), p))
                .map(|v| glyphs::from_option(&v))
                .unwrap_or(Tier::Plain),
        };
        // Remote-host present-state (SPEC §40): FORCE_HOST = the remote shown; <state> from @force_state.
        let remote_host = if cfg!(test) {
            String::new()
        } else {
            std::env::var("FORCE_HOST").unwrap_or_default()
        };
        let state_dir = if cfg!(test) {
            None
        } else {
            tmux_pane
                .as_ref()
                .and_then(|p| tmux::pane_option(tmux.as_ref(), p, "#{@force_state}"))
                .filter(|s| !s.is_empty())
                .map(PathBuf::from)
        };
        App {
            tmux,
            snapshot_path,
            cockpit_bin,
            tmux_pane,
            glyphs_arg,
            tier,
            // the machine force runs on (tests start without one, so layouts do not depend on the test host)
            host: if cfg!(test) {
                String::new()
            } else {
                // the host this ui shows (a remote's name, SPEC §40), else this machine
                std::env::var("FORCE_HOST").ok().filter(|h| !h.is_empty()).unwrap_or_else(|| {
                    crate::term::host_name()
                })
            },
            remote_host,
            state_dir,
            host_status: None,
            status_read_ms: f64::NEG_INFINITY,
            tz_offset,
            now_ms: 0,
            w,
            h,
            c,
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
            host_tmux_prefix: None,
            overlay: false,
            items: Vec::new(),
            offset: 0,
            list_h: 0,
            pinned: false,
            view_top: 0,
            view_h: 0,
            sess_total: 0,
            actions_top: 0,
            actions_rows: 2,
            more_up: (0, false),
            more_down: (0, false),
            usage_h: 0,
            footer_h: 0,
            dock_h: 0,
            rail_targets: Vec::new(),
            rail_usage_show: false,
            cursor: 0,
            follow_own: true,
            error: None,
            prev_states: HashMap::new(),
            prev_usage: None,
            prev_live: std::collections::HashSet::new(),
            seen_viewed: false,
            current_fade_start: None,
            pending_switch: None,
            clicked: None,
            logo_shine_start: None,
            anims: AnimMode::Full,
            finish_flash: HashMap::new(),
            card_motion: HashMap::new(),
            outgoing: HashMap::new(),
            had_live_list: false,
            viewed_until: None,
            force_own_once: false,
            fx: Effects::new(),
            last_menu_hit: None,
        }
    }

    pub fn tier(&self) -> Tier {
        self.tier
    }

    /// Force a fixed glyph tier (used by tests; production sets it at construction / via auto re-read).
    pub fn set_tier(&mut self, tier: Tier) {
        self.tier = tier;
        self.glyphs_arg = GlyphArg::Fixed(tier);
    }

    /// Set the remote-host state_dir and the remote name shown (tests; production reads FORCE_HOST + @force_state).
    pub fn set_remote(&mut self, state_dir: Option<PathBuf>, remote_host: &str) {
        self.state_dir = state_dir;
        self.remote_host = remote_host.to_string();
        self.status_read_ms = f64::NEG_INFINITY;
    }

    /// True when we show a remote and it is not currently connected (missing status counts as down).
    fn remote_down(&self) -> bool {
        // no state dir (no @force_state): the status is unknown, not down
        !self.remote_host.is_empty()
            && self.state_dir.is_some()
            && self.host_status.as_ref().map(|s| s.state != "connected").unwrap_or(true)
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

    pub fn set_anims(&mut self, mode: AnimMode) {
        self.anims = mode;
        self.fx.set_mode(mode);
        if mode.is_off() {
            // `off` draws every frame in its final state: drop all own animations.
            self.finish_flash.clear();
            self.card_motion.clear();
            self.outgoing.clear();
        }
    }

    pub fn set_now(&mut self, now_ms: i64) {
        self.now_ms = now_ms;
    }

    /// Build a grid at a specific timestamp (for tests that need to advance the clock).
    #[cfg(test)]
    pub fn build_grid_at(&mut self, now_ms: i64) -> Grid {
        self.set_now(now_ms);
        self.build_grid()
    }

    pub fn daemon(&self) -> bool {
        match &self.snapshot {
            Some(s) => snapshot::is_stale(s, self.now_ms),
            None => true,
        }
    }

    pub fn poll(&mut self, now_ms: i64) -> bool {
        self.set_now(now_ms);
        let mut changed = false;

        let mtime = std::fs::metadata(&self.snapshot_path)
            .and_then(|m| m.modified())
            .ok();
        let file_changed = mtime != self.last_mtime;
        let gap = now_ms as f64 - self.last_read_ms;
        if file_changed || gap >= MAX_RENDER_GAP_MS {
            self.last_mtime = mtime;
            self.last_read_ms = now_ms as f64;
            // Also re-read when unchanged for MAX_RENDER_GAP_MS: a snapshot that stops changing means
            // the daemon may be dead, and only a fresh load applies the staleness check.
            let new_snap = snapshot::load(&self.snapshot_path);
            self.update_from_snapshot(new_snap, now_ms);
            // The 5 s idle re-check also re-reads the auto glyph tier (one tmux call per 5 s is fine).
            self.refresh_auto_tier();
            changed = true;
        }

        if self.update_own() || self.first_render {
            self.first_render = false;
            if self.own != self.last_own {
                self.last_own = self.own.clone();
                if self.viewed() {
                    self.fx.trigger_edge_sweep(now_ms as f64);
                }
                changed = true;
            }
        }

        if self.error_cleared(now_ms) {
            changed = true;
        }

        // Remote status, re-read at ~1 Hz while we show a remote and have a state dir.
        if !self.remote_host.is_empty() {
            if let Some(sd) = &self.state_dir {
                if now_ms as f64 - self.status_read_ms >= 1000.0 {
                    self.status_read_ms = now_ms as f64;
                    let new_status = hosts::read_status(sd, &self.remote_host);
                    if new_status != self.host_status {
                        self.host_status = new_status;
                        changed = true;
                    }
                }
            }
        }
        self.fx.tick(now_ms as f64);
        changed
    }

    fn update_from_snapshot(&mut self, new: Option<Snapshot>, now_ms: i64) {
        let old_states = self.prev_states.clone();
        let old_usage = self.prev_usage;
        let new_live: std::collections::HashSet<String> = Self::live_names(&new);
        let old_live: std::collections::HashSet<String> = std::mem::take(&mut self.prev_live);
        self.prev_live = new_live.clone();
        self.prev_states.clear();
        if let Some(ref snap) = new {
            for s in &snap.sessions {
                self.prev_states.insert(s.name.clone(), s.state);
            }
            // track the last-known Session so a disappearing card can shrink with its last look
            for s in &snap.sessions {
                self.outgoing.insert(s.name.clone(), s.clone());
            }
        }
        let was_viewed = self.viewed();
        self.snapshot = new;
        if was_viewed && !self.anims.is_off() {
            // WORKING -> YOUR_TURN finish flash
            for (name, st) in &self.prev_states {
                if old_states.get(name) == Some(&State::Working) && *st == State::YourTurn {
                    self.finish_flash.insert(name.clone(), FinishFlash { start_ms: now_ms as f64 });
                }
                if old_states.get(name) != Some(st) {
                    let current = self.display_own() == Some(name.as_str());
                    match st {
                        State::NeedsYou if !current => self.fx.attention(name.clone(), now_ms as f64),
                        State::YourTurn | State::Down => self.fx.pulse(name.clone(), m::glyph_color(*st), now_ms as f64),
                        _ => {}
                    }
                }
            }
            // grow / shrink detection (skip on the very first live list: no motion)
            if self.had_live_list {
                for name in &new_live {
                    if !old_live.contains(name) {
                        self.card_motion.insert(name.clone(), CardMotion { start_ms: now_ms as f64, grow: true });
                    }
                }
                for name in &old_live {
                    if !new_live.contains(name) {
                        self.card_motion.insert(name.clone(), CardMotion { start_ms: now_ms as f64, grow: false });
                    }
                }
            }
            self.had_live_list = true;
        }
        let nu = self.usage_pct();
        if was_viewed {
            if let Some(from) = old_usage {
                if nu != from {
                    self.fx.start_usage_tween([from.0, from.1], [nu.0, nu.1], now_ms as f64);
                }
            }
        }
        self.prev_usage = Some(nu);
    }

    /// Names of non-archived sessions in the current snapshot.
    fn live_names(snap: &Option<Snapshot>) -> std::collections::HashSet<String> {
        match snap {
            Some(s) => s.sessions.iter().filter(|s| !s.archived).map(|s| s.name.clone()).collect(),
            None => std::collections::HashSet::new(),
        }
    }

    fn usage_pct(&self) -> (f64, f64) {
        let u = self.snapshot.as_ref().and_then(|s| s.usage.as_ref());
        let f = |w: &Option<snapshot::Window>| w.map(|x| x.used_percentage).unwrap_or(0.0);
        match u {
            Some(u) => (f(&u.five_hour), f(&u.seven_day)),
            None => (0.0, 0.0),
        }
    }

    fn update_own(&mut self) -> bool {
        let now = std::time::Instant::now();
        let due = if self.force_own_once {
            self.force_own_once = false;
            true
        } else {
            self.own_cached_at
                .map(|t| now.duration_since(t).as_secs_f64() >= OWN_CACHE_SECS)
                .unwrap_or(true)
        };
        if !due {
            return false;
        }
        self.own_cached_at = Some(now);
        self.own = self
            .tmux_pane
            .as_ref()
            .and_then(|p| tmux::session_name(self.tmux.as_ref(), p));
        // ui-mode detection + host-own resolution (also publishes @force_session when host own changes).
        self.refresh_ui_mode();
        true
    }

    /// The session to use for display/logic (current-card highlight, attention suppression, viewed, env): the HOST
    /// session in ui mode, else the pane's own ui session. In classic mode both are the same session.
    /// Chuck's session (SPEC §50): whether this window shows it now, and its state.
    fn chuck_status(&self) -> (bool, Option<State>) {
        let chuck = self.snapshot.as_ref().and_then(|s| s.sessions.iter().find(|s| s.support && !s.archived));
        match chuck {
            Some(c) => (self.overlay || self.display_own() == Some(c.name.as_str()), Some(c.state)),
            // a remote's list has no Chuck of this machine: lit while he is shown over it
            None => (self.overlay, None),
        }
    }

    fn display_own(&self) -> Option<&str> {
        if self.ui_mode {
            self.host_own.as_deref()
        } else {
            self.own.as_deref()
        }
    }

    /// Re-read split-mode window options and the host-own session. Called at start, on SIGUSR1, and every own
    /// re-read (the 2 s cache). Non-empty `@force_nested` <=> ui mode; host own is the host `list-clients`
    /// line whose client_name == `@force_nested` (kept on previous value when that client is gone). Publishes
    /// `@force_session` on the ui server whenever the host own changes (the daemon uses it to update its state).
    fn refresh_ui_mode(&mut self) {
        let nested = self
            .tmux_pane
            .as_ref()
            .and_then(|p| tmux::pane_option(self.tmux.as_ref(), p, "#{@force_nested}"));
        let prev = self.host_own.clone();
        if nested.as_deref().map_or(false, |s| !s.is_empty()) {
            self.ui_mode = true;
            self.nested = nested;
            // the host handle from @force_host_tmux words, rebuilt when they change (Chuck's overlay points them at
            // this machine and back, SPEC §50); a handle a test injected is kept
            let prefix = self
                .tmux_pane
                .as_ref()
                .and_then(|p| tmux::pane_option(self.tmux.as_ref(), p, "#{@force_host_tmux}"));
            if self.host_tmux.is_none() || (self.host_tmux_prefix.is_some() && self.host_tmux_prefix != prefix) {
                self.host_tmux = Some(Box::new(tmux::HostTmux::from_option(prefix.as_deref().unwrap_or(""))));
                self.host_tmux_prefix = Some(prefix.unwrap_or_default());
            }
            self.overlay = self
                .tmux_pane
                .as_ref()
                .and_then(|p| tmux::pane_option(self.tmux.as_ref(), p, "#{@force_overlay}"))
                .map_or(false, |s| !s.is_empty());
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

    /// `set-option -w -t <$TMUX_PANE> @force_session <host_own>` on the ui server (the sidebar always
    /// publishes the current host own; the header only publishes when no sidebar pane is in its window).
    fn publish_force_session(&mut self) {
        if !self.ui_mode {
            return;
        }
        if let (Some(pane), Some(host_own)) = (&self.tmux_pane, &self.host_own) {
            let _ = tmux::set_window_option(self.tmux.as_ref(), pane, "@force_session", host_own);
        }
    }

    /// React to SIGUSR1: re-poll immediately, treat as viewed for 2 s (the SIGUSR1 grace window), and
    /// run the own-card arrival fade (the cards themselves appear instantly; owner removed the cascade).
    pub fn sigusr1(&mut self, now_ms: i64) -> bool {
        self.set_now(now_ms);
        self.view_for(2000);
        self.force_own_once = true;
        self.follow_own = true;
        self.seen_viewed = true;
        // SIGUSR1 also re-reads the auto glyph tier (and redraws if it changed).
        let mut changed = self.refresh_auto_tier();
        changed = self.poll(now_ms) || changed;
        let after_click = self.clicked.as_ref().is_some_and(|(t, at)| {
            self.host_own.as_deref() == Some(t.as_str()) && now_ms - at < 1000
        });
        if !self.anims.is_off() && !after_click {
            // always fade the own card to its current look on a SIGUSR1, and sweep light over the logo.
            self.current_fade_start = Some(now_ms as f64);
            self.logo_shine_start = Some(now_ms as f64);
        }
        self.fx.tick(now_ms as f64);
        changed
    }

    /// Send the host switch a click left for after its frame (split mode). A failed switch shows the error and
    /// re-reads the real host own on the next poll (undoing the optimistic highlight); a done one tells the
    /// header of this window, whose own re-read would otherwise lag up to 2 s on a ui on another computer.
    /// Returns true when a redraw is needed.
    pub fn run_pending(&mut self) -> bool {
        let Some((client, target)) = self.pending_switch.take() else {
            return false;
        };
        let Some(host) = &self.host_tmux else {
            return false;
        };
        if let Err(e) = tmux::switch_client(host.as_ref(), &client, &target) {
            self.show_error(format!("switch: {}", e));
            self.clicked = None;
            self.force_own_once = true;
            return true;
        }
        self.own_cached_at = Some(std::time::Instant::now());
        if let Some(pane) = &self.tmux_pane {
            for pid in tmux::window_role_pids(self.tmux.as_ref(), pane, "header") {
                // SAFETY: kill(2) with a pid read from tmux; window_role_pids only returns Force's own programs
                unsafe {
                    libc::kill(pid, libc::SIGUSR1);
                }
            }
        }
        false
    }

    /// True while an event animation (arrival fade, finish flash, card motion) needs frames.
    pub fn event_anim_running(&self) -> bool {
        if !self.viewed() {
            return false;
        }
        if self.anims.is_off() {
            return false;
        }
        self.arrival_active()
            || self.logo_shine_active()
            || self.finish_flash_active()
            || self.card_motion_active()
    }

    /// True while the continuous working shimmer would animate (full mode, viewed, something working).
    pub fn continuous_running(&self) -> bool {
        self.anims.shimmer() && self.viewed() && self.any_working()
    }

    /// True while the own-card arrival fade still needs frames (3 frames ~40 ms apart).
    fn arrival_active(&self) -> bool {
        match self.current_fade_start {
            Some(s) => (self.now_ms as f64 - s) < (2.0 * ARRIVE_STEP_MS as f64) + 1.0,
            None => false,
        }
    }
    fn logo_shine_active(&self) -> bool {
        match self.logo_shine_start {
            Some(s) => (self.now_ms as f64 - s) < LOGO_SHINE_MS,
            None => false,
        }
    }

    /// The light sweep's pixel column at `now_ms`, while it runs on a viewed pane (not in `off`).
    fn logo_shine_pos(&self, now_ms: i64) -> Option<f64> {
        let start = self.logo_shine_start?;
        if self.anims.is_off() || !self.viewed() {
            return None;
        }
        let t = (now_ms as f64 - start) / LOGO_SHINE_MS;
        if !(0.0..1.0).contains(&t) {
            return None;
        }
        let (a, b) = m::logo_shine_span(self.w);
        Some(a + (b - a) * Effects::ease(t))
    }

    fn finish_flash_active(&self) -> bool {
        self.finish_flash.values().any(|f| (self.now_ms as f64 - f.start_ms) < FINISH_GLYPH_MS)
    }
    fn card_motion_active(&self) -> bool {
        self.card_motion.values().any(|c| (self.now_ms as f64 - c.start_ms) < CARD_DUR_MS)
    }

    /// Current animated card height in half-rows (`full` = the whole card, 0 = fully collapsed).
    fn card_half(&self, name: &str, now: f64, full: i32) -> i32 {
        match self.card_motion.get(name) {
            Some(cm) => {
                let t = ease_in_out(((now - cm.start_ms) / CARD_DUR_MS).clamp(0.0, 1.0));
                let f = if cm.grow { t * full as f64 } else { (1.0 - t) * full as f64 };
                f.round() as i32
            }
            None => full,
        }
    }

    /// Rows of a session's card: 2 (name, activity), 3 with the model line on the own card (not for a shell).
    fn card_rows(&self, _s: &Session) -> usize {
        // two rows for every card: the own card's model / mode line moved to the header's title
        2
    }

    /// How "current" (highlighted) the own card should look right now as a 0..=1 blend from its
    /// non-current look (edge by state, idle/needs bg, normal name) to the current look (coral edge,
    /// #363636 bg, bright name). Hidden -> 0 (neutral); a current fade that has started -> 3 whole frames
    /// at 1/3, 2/3, 1 (each ~40 ms apart); otherwise -> 1.
    fn own_current_alpha(&self, now_ms: i64) -> f64 {
        if !self.viewed() {
            return 0.0;
        }
        if self.anims.is_off() {
            return 1.0; // `off`: instant current look once viewed
        }
        match self.current_fade_start {
            Some(s) => {
                let step = ((now_ms as f64 - s) / ARRIVE_STEP_MS as f64).floor() as i64;
                if step <= 0 {
                    1.0 / 3.0
                } else if step == 1 {
                    2.0 / 3.0
                } else {
                    1.0
                }
            }
            None => 1.0,
        }
    }

    /// The finish-flash timer for `name` (`Some(t)` with `t` = ms since flash start) while it is still
    /// within `FINISH_GLYPH_MS`; `None` once done.
    fn finish_flash_for(&self, name: &str, now_ms: i64) -> Option<f64> {
        let f = self.finish_flash.get(name)?;
        let t = now_ms as f64 - f.start_ms;
        if (0.0..FINISH_GLYPH_MS).contains(&t) {
            Some(t)
        } else {
            None
        }
    }

    /// Sweep a 4-cell light band left→right across `y` (a line-2 row). Blends each nearby glyph's fg
    /// toward `SHIMMER_FG` with `k = max(0, 1 - |x-center|/2.5)`.
    fn shimmer_row(&self, grid: &mut Grid, y: usize, now_ms: i64) {
        let progress = ((now_ms % SHIMMER_PERIOD_MS as i64) as f64) / SHIMMER_PERIOD_MS;
        let min_x = 1i64;
        let max_x = (self.w as i64).saturating_sub(2).max(min_x);
        let span = (max_x - min_x) as f64;
        let center = min_x as f64 + span * progress;
        for x in 1..self.w {
            let k = 1.0 - ((x as f64) - center).abs() / 2.5;
            if k > 0.0 {
                let cell = grid.row(y)[x];
                let new = lerp_bg(cell.fg, SHIMMER_FG, k.min(1.0));
                grid.set(y, x, RowCell { ch: cell.ch, fg: new, bg: cell.bg, bold: cell.bold });
            }
        }
    }

    fn error_cleared(&mut self, now_ms: i64) -> bool {
        if self.error.as_ref().map(|(_, t)| *t <= now_ms).unwrap_or(false) {
            self.error = None;
            let _ = now_ms;
            true
        } else {
            false
        }
    }

    /// Treat the own session as viewed for the next `ms` (never shortens a longer window already running).
    fn view_for(&mut self, ms: i64) {
        let until = self.now_ms + ms;
        self.viewed_until = Some(self.viewed_until.map_or(until, |t| t.max(until)));
    }

    pub fn viewed(&self) -> bool {
        // SIGUSR1 forces "viewed" for 2 s because the daemon's `viewed` flag lags the switch.
        if let Some(t) = self.viewed_until {
            if self.now_ms <= t {
                return true;
            }
        }
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
        // SPEC §5: a watching session has nothing turning, so it does not keep the spinner alive.
        match &self.snapshot {
            Some(snap) => snap.sessions.iter().any(|s| !s.archived && s.state == State::Working && !s.watching),
            None => false,
        }
    }

    /// True once a snapshot has been loaded (drives the 5 s staleness re-check deadline).
    pub fn has_snapshot(&self) -> bool {
        self.snapshot.is_some()
    }

    /// Absolute wall-clock ms of the footer-error expiry, if any (one of the loop's wake sources).
    pub fn error_expiry(&self) -> Option<i64> {
        self.error.as_ref().map(|(_, t)| *t)
    }

    /// Absolute wall-clock ms of the next frame the sidebar wants to draw, or None if nothing animates.
    /// Only returns a deadline while the pane is viewed; hidden sessions get no frame deadlines (they just wait
    /// on the 5 s staleness cap). ~33 ms for event animations / attention, ~100 ms for the working spinner.
    pub fn next_frame_ms(&self) -> Option<i64> {
        if !self.viewed() {
            return None;
        }
        if self.fx.snappy_running() || self.event_anim_running() || self.fx.attention_running() {
            Some(self.now_ms + 33)
        } else if self.anims.shimmer() && self.any_working() {
            Some(self.now_ms + 100)
        } else {
            None
        }
    }

    // --- focus / rest / errors -----------------------------------------------------
    pub fn on_focus_gained(&mut self) {
        // no-op: there is no keyboard mode to re-arm.
    }
    pub fn on_focus_lost(&mut self) {
        self.rest();
    }
    fn rest(&mut self) {
        self.follow_own = true;
        self.rest_cursor();
    }
    fn rest_cursor(&mut self) {
        if let Some(own) = self.display_own() {
            if let Some(i) = self.items.iter().position(|it| it.target.str() == own) {
                self.cursor = i;
                return;
            }
        }
        if let Some(i) = self.items.iter().position(|it| it.is_session) {
            self.cursor = i;
        } else {
            self.cursor = 0;
        }
    }

    fn show_error(&mut self, msg: String) {
        self.error = Some((msg, self.now_ms + 4000));
    }

    // --- actions ------------------------------------------------------------------
    fn activate(&mut self, target: &str) {
        if target == ARCHIVED_TARGET {
            self.popup("archived", None, 72, 21);
            return;
        }
        if target == NEW_TARGET {
            self.popup("new", None, 64, 18);
            return;
        }
        if target == CHUCK_TARGET {
            self.open_chuck();
            return;
        }
        let own = match &self.own {
            Some(o) => o.clone(),
            None => return,
        };
        // A click proves this window is looked at: the clicked card is current at once. The snapshot's `viewed`
        // follows only after the daemon's next tick (~1 s) and, on a ui on another computer, the snapshot copy.
        self.view_for(CLICK_VIEWED_MS);
        if self.ui_mode && self.overlay {
            // Chuck shown over a remote (SPEC §50): the window's host back in the main pane, on this card
            if let Some(w) = self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
                let args = vec!["ui-main".to_string(), "--window".to_string(), w, "--target".to_string(),
                                target.to_string()];
                if let Err(e) = crate::header::Spawner::spawn(&crate::header::RealSpawner, &self.cockpit_bin, &args) {
                    self.show_error(format!("switch: {}", e));
                }
            }
            return;
        }
        if self.ui_mode {
            // split mode: switch on the HOST (the nested client), then focus the main pane of my ui window.
            let client = match &self.nested {
                Some(c) => c.clone(),
                None => return,
            };
            if let Some(p) = tmux::agent_pane(self.tmux.as_ref(), &own) {
                if let Err(e) = tmux::select_pane(self.tmux.as_ref(), &p) {
                    self.show_error(format!("select: {}", e));
                }
            }
            self.rest();
            // optimistic host own + publish, shown in full at once (no fade: it is the answer to a click); the
            // host switch itself is sent after this frame (run_pending).
            if self.host_own.as_deref() != Some(target) {
                self.host_own = Some(target.to_string());
                self.publish_force_session();
            }
            self.current_fade_start = None;
            if !self.anims.is_off() {
                self.logo_shine_start = Some(self.now_ms as f64);
            }
            self.clicked = Some((target.to_string(), self.now_ms));
            // no host re-read before that frame either (it is another round trip)
            self.own_cached_at = Some(std::time::Instant::now());
            if self.host_tmux.is_some() {
                self.pending_switch = Some((client, target.to_string()));
            }
            return;
        }
        // classic: switch on the ui server with the most-recently active client of the own session.
        let client = self.own_client();
        if let Some(c) = client {
            if let Err(e) = tmux::switch_client(self.tmux.as_ref(), &c, target) {
                self.show_error(format!("switch: {}", e));
                return;
            }
        }
        self.rest();
        if let Some(p) = tmux::agent_pane(self.tmux.as_ref(), &own) {
            if let Err(e) = tmux::select_pane(self.tmux.as_ref(), &p) {
                self.show_error(format!("select: {}", e));
            }
        }
    }

    /// Chuck's card and session (`force support --ui`, SPEC §50) in a popup, with the dialogs' env plus this
    /// window (a window showing a remote host comes back to this machine for him).
    fn open_chuck(&mut self) {
        let client = self.own_client();
        let mut env = self.popup_env();
        if self.ui_mode {
            if let Some(w) = self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
                env.push(("FORCE_WINDOW".to_string(), w));
            }
        }
        let command = format!("{} support --ui", tmux::shell_quote(&self.cockpit_bin));
        if let Err(e) = tmux::popup_with(self.tmux.as_ref(), client.as_deref(), &env, 72, 17,
                                         &tmux::CHUCK_POPUP_STYLE, &command) {
            self.show_error(format!("chuck: {}", e));
        }
    }

    /// The update card (`force update --ui`, SPEC §3) in a popup, opened by a click on the update note. The note is
    /// the shown host's (a ui showing a remote has that remote's snapshot), and so is the card: popup_env passes
    /// FORCE_HOST, and the card then runs there.
    fn open_update(&mut self) {
        let client = self.own_client();
        let env = self.popup_env();
        let command = format!("{} update --ui", tmux::shell_quote(&self.cockpit_bin));
        if let Err(e) = tmux::popup_with(self.tmux.as_ref(), client.as_deref(), &env, 72, 20,
                                         &tmux::CHUCK_POPUP_STYLE, &command) {
            self.show_error(format!("update: {}", e));
        }
    }

    fn popup_env(&self) -> Vec<(String, String)> {
        let mut env: Vec<(String, String)> = Vec::new();
        if self.ui_mode {
            if let Some(n) = &self.nested {
                env.push(("FORCE_CLIENT".to_string(), n.clone()));
            }
            if let Some(ho) = &self.host_own {
                env.push(("FORCE_SESSION".to_string(), ho.clone()));
            }
            env.push(("FORCE_GLYPHS".to_string(), crate::glyphs::tier_str(self.tier).to_string()));
            if let Ok(h) = std::env::var("FORCE_HOST") {
                if !h.is_empty() {
                    env.push(("FORCE_HOST".to_string(), h));
                }
            }
        } else {
            env.push(("FORCE_GLYPHS".to_string(), crate::glyphs::tier_str(self.tier).to_string()));
        }
        env
    }

    fn popup(&mut self, kind: &str, name: Option<&str>, w: u32, h: u32) {
        let client = self.own_client();
        let mut env: Vec<(String, String)> = Vec::new();
        if self.ui_mode {
            // split mode: hand the host nested client / host-own session / tier to the dialog as env pairs.
            if let Some(n) = &self.nested {
                env.push(("FORCE_CLIENT".to_string(), n.clone()));
            }
            if let Some(ho) = &self.host_own {
                env.push(("FORCE_SESSION".to_string(), ho.clone()));
            }
            env.push(("FORCE_GLYPHS".to_string(), crate::glyphs::tier_str(self.tier).to_string()));
            // the host this ui window shows (SPEC §40): the dialog's commands and tmux calls go there
            if let Ok(h) = std::env::var("FORCE_HOST") {
                if !h.is_empty() {
                    env.push(("FORCE_HOST".to_string(), h));
                }
            }
        }
        let mut parts = vec![
            tmux::shell_quote(&self.cockpit_bin),
            tmux::shell_quote("dialog"),
            tmux::shell_quote(kind),
        ];
        if let Some(n) = name {
            parts.push(tmux::shell_quote(n));
        }
        let command = parts.join(" ");
        if let Err(e) = tmux::popup(self.tmux.as_ref(), client.as_deref(), &env, w, h, &command) {
            self.show_error(format!("dialog: {}", e));
        }
    }

    fn own_client(&self) -> Option<String> {
        let own = self.own.as_ref()?;
        let clients = tmux::clients(self.tmux.as_ref());
        clients
            .iter()
            .filter(|c| &c.session == own)
            .max_by_key(|c| c.activity)
            .map(|c| c.name.clone())
    }

    /// Open the host picker (the `hosts-menu` tmux popup menu) on the ui window's client. Only meaningful in
    /// split (ui) mode, where the bottom-left host chip is shown; bound to the chip click and the `h` key.
    fn open_hosts(&mut self) {
        if !self.ui_mode {
            return;
        }
        let client = match self.own_client() {
            Some(c) => c,
            None => {
                self.show_error("hosts: no client".into());
                return;
            }
        };
        let window = match self.tmux_pane.as_ref().and_then(|p| tmux::window_id(self.tmux.as_ref(), p)) {
            Some(w) => w,
            None => {
                self.show_error("hosts: no window".into());
                return;
            }
        };
        let cmd = format!(
            "{} hosts-menu --client {} --window {}",
            tmux::shell_quote(&self.cockpit_bin),
            tmux::shell_quote(&client),
            tmux::shell_quote(&window),
        );
        if let Err(e) = self.tmux.run(&["run-shell".to_string(), "-b".to_string(), cmd]) {
            self.show_error(format!("hosts: {}", e));
            return;
        }
        self.rest();
    }

    fn focus_agent(&mut self) {
        let own = match &self.own {
            Some(o) => o.clone(),
            None => return,
        };
        if let Some(p) = tmux::agent_pane(self.tmux.as_ref(), &own) {
            if let Err(e) = tmux::select_pane(self.tmux.as_ref(), &p) {
                self.show_error(format!("select: {}", e));
            }
        }
    }

    // --- keyboard ----------------------------------------------------------------
    pub fn on_key(&mut self, _key: crossterm::event::KeyCode) -> bool {
        self.focus_agent();
        true
    }

    // --- mouse -------------------------------------------------------------------
    pub fn on_mouse(&mut self, kind: crossterm::event::MouseEventKind, col: u16, row: u16) -> bool {
        use crossterm::event::MouseButton;
        use crossterm::event::MouseEventKind as M;
        match kind {
            M::Down(MouseButton::Left) => {
                if self.update_at(col as usize, row as usize) {
                    self.open_update();
                    return true;
                }
                if self.chip_at(col as usize, row as usize) {
                    self.open_hosts();
                    return true;
                }
                if let Some(up) = self.more_at(row as usize) {
                    self.scroll_by(up, self.view_h.saturating_sub(1).max(1));
                    return true;
                }
                let hit = self.hit_test(col as usize, row as usize).map(|t| t.str().to_string());
                self.rest();
                if let Some(t) = hit {
                    let t = t.clone();
                    self.activate(&t);
                }
            }
            M::ScrollUp => self.scroll(true),
            M::ScrollDown => self.scroll(false),
            _ => return false,
        }
        true
    }

    fn scroll(&mut self, up: bool) {
        self.scroll_by(up, 3);
    }

    fn scroll_by(&mut self, up: bool, delta: usize) {
        let (total, view) = if self.pinned {
            // any offset > 0 shows the "more above" row, so the bottom is reached with that smaller viewport
            (self.sess_total, self.list_h - self.actions_h() - 1)
        } else {
            (self.items.last().map(|i| i.top + i.height).unwrap_or(0), self.list_h)
        };
        if total <= view {
            return;
        }
        let max = total - view;
        self.follow_own = false;
        self.offset = if up {
            self.offset.saturating_sub(delta)
        } else {
            (self.offset + delta).min(max)
        };
    }

    pub fn on_resize(&mut self, w: usize, h: usize) {
        if w >= 3 {
            self.w = w;
            self.c = w - 2;
        }
        self.h = h.max(1);
    }

    // --- layout / items --------------------------------------------------------------
    fn usage_height(&self) -> usize {
        if self.daemon() {
            return 0;
        }
        // per block a label (or blank) row and one row per window, then a blank row (SPEC §49.2)
        let rows: usize = self
            .limits_blocks()
            .iter()
            .map(|(_, u)| [u.five_hour.is_some(), u.seven_day.is_some()].iter().filter(|b| **b).count())
            .filter(|n| *n > 0)
            .map(|n| n + 1)
            .sum();
        if rows > 0 { rows + 1 } else { 0 }
    }

    /// The dock's limits blocks (SPEC §49.2): each provider's label and windows. A snapshot without `limits`
    /// but with `usage` (built in a test) is one unlabelled block.
    fn limits_blocks(&self) -> Vec<(Option<m::LimitsLabel>, snapshot::Usage)> {
        let Some(snap) = self.snapshot.as_ref() else { return vec![] };
        if snap.limits.is_empty() {
            return snap.usage.iter().map(|u| (None, u.clone())).collect();
        }
        snap.limits
            .iter()
            .map(|l| {
                let (mark, color) = m::limits_mark(&l.kind);
                (Some((mark, color, l.label.clone())), l.usage.clone())
            })
            .collect()
    }

    fn footer_text(&self) -> Option<String> {
        if self.daemon() {
            if !self.remote_host.is_empty() {
                return Some(self.remote_empty_text());
            }
            return Some("daemon not running".to_string());
        }
        if let Some((m, _)) = self.error.as_ref() {
            return Some(m.clone());
        }
        // the last `force smoke` failed (SPEC §37), with its reason: an agent CLI logged out, or changed in a way
        // Force mishandles
        if let Some(reason) = self.snapshot.as_ref().and_then(|s| s.smoke_failed.as_deref()) {
            return Some(format!("{} {reason}", crate::glyphs::state(self.tier, '×', '✖')));   // × where ✖ would be an emoji
        }
        // a newer Force release is available (SPEC §3): a footer note, lower priority than any error/smoke
        if let Some(v) = self.snapshot.as_ref().and_then(|s| s.update.as_deref()) {
            return Some(format!("↑ Force {v} · force update"));
        }
        None
    }

    /// Whether the footer note is the update-available notice (SPEC §3): it is drawn in the coral accent,
    /// not the down/error colour.
    fn footer_is_update(&self) -> bool {
        self.error.is_none()
            && self.snapshot.as_ref().and_then(|s| s.smoke_failed.as_deref()).is_none()
            && self.snapshot.as_ref().and_then(|s| s.update.as_deref()).is_some()
    }

    /// The footer text as drawn: none when the host chip already says it (a remote that is down: the chip
    /// reads "host · offline" / "host · other version").
    fn footer_note(&self) -> Option<String> {
        if self.host_chip_on() && self.remote_down() {
            return None;
        }
        self.footer_text()
    }

    /// Text for a remote we show but have no usable snapshot for: `"{host} offline"` (version: `"{host}: other
    /// version"`). Used in both the footer and the placeholder list row; shown only when a remote is being shown.
    fn remote_empty_text(&self) -> String {
        match &self.host_status {
            Some(s) if s.state == "version" => format!("{}: other version", self.host),
            _ => format!("{} offline", self.host),
        }
    }

    fn live_sessions(snap: &Option<Snapshot>) -> Vec<&Session> {
        let mut v: Vec<&Session> = match snap {
            // Chuck (SPEC §50) is not a card: he opens from his own row, which shows his state
            Some(s) => s.sessions.iter().filter(|s| !s.archived && !s.support).collect(),
            None => Vec::new(),
        };
        v.sort_by_key(|s| s.index.unwrap_or(0));
        v
    }

    fn build_items(&mut self, c: usize, now_ms: i64) -> (Vec<Item>, usize) {
        let now_f = now_ms as f64;
        // prune finished card motions, their outgoing entries, and expired finish flashes
        let finished: Vec<String> = self
            .card_motion
            .iter()
            .filter(|(_, cm)| now_f - cm.start_ms >= CARD_DUR_MS)
            .map(|(n, _)| n.clone())
            .collect();
        for n in &finished {
            if let Some(cm) = self.card_motion.remove(n) {
                if !cm.grow {
                    self.outgoing.remove(n);
                }
            }
        }
        self.finish_flash.retain(|_, f| now_f - f.start_ms < FINISH_GLYPH_MS);

        let mut items = Vec::new();
        let mut running = 0usize;
        if self.daemon() {
            let text = if !self.remote_host.is_empty() {
                self.remote_empty_text()
            } else {
                "daemon not running".to_string()
            };
            items.push(Item {
                target: Target::Daemon,
                top: 0,
                height: 1,

                partial: false,
                lines: vec![dim_row(&text, c)],
                is_session: false,
                current: false,
                state: State::Idle,
                unread: false,
                watching: false,
                cols: None,
            });
            return (items, 1);
        }

        // display cards = live sessions + still-shrinking outgoing sessions, ordered, with animated heights
        struct Disp {
            name: String,
            sess: Option<Session>,
            idx: i64,
            h_half: i32,
        }
        let mut cards: Vec<Disp> = Vec::new();
        for s in Self::live_sessions(&self.snapshot) {
            let full = 2 * self.card_rows(s) as i32;
            cards.push(Disp {
                name: s.name.clone(),
                sess: Some(s.clone()),
                idx: s.order,
                h_half: self.card_half(&s.name, now_f, full),
            });
        }
        let shrink_names: Vec<String> = self
            .card_motion
            .iter()
            .filter(|(_, cm)| !cm.grow)
            .map(|(n, _)| n.clone())
            .collect();
        for name in shrink_names {
            if cards.iter().any(|d| d.name == name) {
                continue;
            }
            if let Some(s) = self.outgoing.get(&name) {
                let full = 2 * self.card_rows(s) as i32;
                cards.push(Disp {
                    name,
                    sess: Some(s.clone()),
                    idx: s.order,
                    h_half: self.card_half(&s.name, now_f, full),
                });
            }
        }
        cards.sort_by_key(|d| d.idx);

        for d in cards {
            let h_half = d.h_half;
            if h_half <= 0 {
                continue; // fully collapsed: not rendered yet
            }

            let s = match &d.sess {
                Some(s) => s,
                None => continue,
            };
            // lay out by whole rows; a trailing odd half-row becomes a partial `▀` row (no text)
            let full = (h_half / 2) as usize;
            let partial = h_half % 2 == 1;
            let height = full + partial as usize;
            let current = self.display_own() == Some(s.name.as_str());
            let name_color = m::NAME;
            let mut lines = vec![
                pad(m::line1(s, now_ms, c, name_color, self.tier), c),
                pad(m::line2(s, c, self.tier), c),
                pad(m::line3(s, c, self.tier), c),
            ];
            lines.truncate(full);
            if partial {
                lines.push(vec![Glyph::plain(' '); c]);
            }
            items.push(Item {
                target: Target::Session(s.name.clone()),
                top: running,
                height,

                partial,
                lines,
                is_session: true,
                current,
                state: s.state,
                unread: s.unread,
                watching: s.watching,
                cols: None,
            });
            running += height;
        }

        let narch = self.snapshot.as_ref().map(|s| s.sessions.iter().filter(|s| s.archived).count()).unwrap_or(0);
        // A blank row, then `+ new` and `archived` set into one rule, like the header's labels: ` ─ + new ───
        // archived 0 ─` (the `+` in the cards' icon column, the rule over columns 1..w like the brand's); a pane
        // too narrow for both stacks them. RICH puts the icons in a 2-cell slot.
        running += 1;
        self.actions_top = running;
        let (new_text, arch_text) = if self.tier == Tier::Rich {
            (format!("  {}  new", '\u{F067}'), format!("{}  archived {}", '\u{F187}', narch))
        } else {
            ("  + new".to_string(), format!("archived ({})", narch))
        };
        // the rule row: `─ ` + new + ` `, `─`s, ` ` + archived + ` ─`, over columns 1..w (w - 1 cells)
        let rule_n = self.w.saturating_sub(1);
        let left: Vec<Glyph> = std::iter::once(Glyph { ch: '─', color: m::PANEL_RULE, bold: false })
            .chain(new_text.chars().skip(1).map(|ch| Glyph { ch, color: m::GREY, bold: false }))
            .chain(std::iter::once(Glyph { ch: ' ', color: m::GREY, bold: false }))
            .collect();
        let right: Vec<Glyph> = format!(" {arch_text} ").chars().map(|ch| Glyph { ch, color: m::GREY, bold: false })
            .chain(std::iter::once(Glyph { ch: '─', color: m::PANEL_RULE, bold: false }))
            .collect();
        let w = self.w;
        let action = |target: Target, top: usize, line: Vec<Glyph>, cols: Option<(usize, usize)>| Item {
            target,
            top,
            height: 1,
            partial: false,
            lines: vec![line],
            is_session: false,
            current: false,
            state: State::Idle,
            unread: false,
            watching: false,
            cols,
        };
        if left.len() + 2 + right.len() <= rule_n {
            let mid = rule_n - left.len() - right.len();
            let mut line = left.clone();
            line.extend((0..mid).map(|_| Glyph { ch: '─', color: m::PANEL_RULE, bold: false }));
            line.extend(right.iter().copied());
            let split = 1 + left.len() + mid / 2; // grid column where the archived click area begins
            items.push(action(Target::New, running, line.clone(), Some((0, split))));
            items.push(action(Target::Archived, running, line, Some((split, w))));
            self.actions_rows = 1;
            running += 1;
        } else {
            let arch_text = if self.tier == Tier::Rich { format!("  {arch_text}") } else { format!("    {arch_text}") };
            items.push(action(Target::Archived, running, dim_row(&arch_text, c), None));
            items.push(action(Target::New, running + 1, dim_row(&new_text, c), None));
            self.actions_rows = 2;
            running += 2;
        }
        // Chuck, Force's assistant (SPEC §50): a blank row, then his label centred in a rule like the one above
        // (` ─── ☺  ask chuck ─── `, over columns 1..w), the face in coral
        running += 1;
        // His row shows his session's state: lit (coral rule, white label) while this window shows him, else the
        // label in the state's colour when there is something to tell (needs you, working, your turn).
        let (active, chuck_state) = self.chuck_status();
        let (text_color, bold, rule_color) = if active {
            (m::NAME_CURRENT, true, m::ACCENT)
        } else {
            match chuck_state {
                Some(State::NeedsYou) => (m::C_NEEDS_YOU, true, m::PANEL_RULE),
                Some(State::Working) => (m::C_WORKING, false, m::PANEL_RULE),
                Some(State::YourTurn) => (m::C_YOUR_TURN, false, m::PANEL_RULE),
                _ => (m::GREY, false, m::PANEL_RULE),
            }
        };
        let mut label: Vec<Glyph> = vec![Glyph { ch: ' ', color: text_color, bold: false }];
        label.push(Glyph { ch: glyphs::state(self.tier, '?', CHUCK_RICH), color: m::ACCENT, bold: false });
        label.extend(format!("{}ask chuck ", if self.tier == Tier::Rich { "  " } else { " " })
            .chars().map(|ch| Glyph { ch, color: text_color, bold }));
        let dashes = rule_n.saturating_sub(label.len());
        let rule = |n: usize| (0..n).map(|_| Glyph { ch: '─', color: rule_color, bold: false });
        let mut chuck: Vec<Glyph> = rule(dashes / 2).collect();
        chuck.extend(label);
        chuck.extend(rule(dashes - dashes / 2));
        items.push(action(Target::Chuck, running, chuck, None));
        self.actions_rows += 2;
        running += 1;
        (items, running)
    }

    /// Lay out the list area: pinned or not, the card viewport, the scroll offset and the hidden counts.
    fn layout_list(&mut self, total: usize) {
        self.sess_total = self.items.iter().filter(|i| i.is_session).map(|i| i.top + i.height).max().unwrap_or(0);
        self.pinned = total > self.list_h && self.list_h >= PIN_MIN_LIST && !self.daemon();
        self.view_top = 0;
        if !self.pinned {
            self.view_h = self.list_h;
            self.compute_offset(total);
            self.more_up = (0, false);
            self.more_down = (0, false);
            return;
        }
        self.view_h = self.list_h - self.actions_h();
        let wanted = self.offset;
        self.compute_offset(self.sess_total);
        if self.offset > 0 {
            // the "more above" row takes the top row of the viewport; lay out again from the wanted offset,
            // which the smaller viewport may allow (a smaller viewport only scrolls further)
            self.view_top = 1;
            self.view_h -= 1;
            self.offset = wanted;
            self.compute_offset(self.sess_total);
        }
        let (off, end) = (self.offset, self.offset + self.view_h);
        let count = |f: &dyn Fn(&Item) -> bool| {
            let hidden: Vec<&Item> = self.items.iter().filter(|i| i.is_session && f(i)).collect();
            (hidden.len(), hidden.iter().any(|i| i.state == State::NeedsYou))
        };
        self.more_up = count(&|i| i.top < off);
        self.more_down = count(&|i| i.top + i.height > end);
    }

    fn compute_offset(&mut self, total: usize) {
        if total <= self.view_h {
            self.offset = 0;
            return;
        }
        let max = total - self.view_h;
        self.offset = self.offset.min(max);
        if !self.follow_own {
            return;
        }
        if let Some(it) = self.items.get(self.cursor) {
            // pinned actions are always in view
            if self.pinned && !it.is_session {
                return;
            }
            if it.top < self.offset {
                self.offset = it.top;
            }
            if it.top + it.height > self.offset + self.view_h {
                self.offset = it.top + it.height - self.view_h;
            }
        }
        self.offset = self.offset.min(max);
    }

    /// Rows of the pinned actions block: the "more below" row (blank when nothing is hidden) + the actions.
    fn actions_h(&self) -> usize {
        1 + self.actions_rows
    }

    /// The list-area row of an item's line, None while it is scrolled out of view.
    fn list_row(&self, it: &Item, line_idx: usize) -> Option<usize> {
        if self.pinned && !it.is_session {
            return Some(self.list_h - self.actions_rows + (it.top - self.actions_top) + line_idx);
        }
        let sy = (it.top + line_idx) as i64 - self.offset as i64;
        (sy >= 0 && (sy as usize) < self.view_h).then(|| self.view_top + sy as usize)
    }

    /// The "more" row at pane row `y`: Some(true) above the cards, Some(false) below them.
    fn more_at(&self, y: usize) -> Option<bool> {
        if self.rail() || !self.pinned || y < BRAND_H {
            return None;
        }
        let y = y - BRAND_H;
        if y == 0 && self.view_top == 1 && self.more_up.0 > 0 {
            Some(true)
        } else if y == self.list_h - self.actions_h() && self.more_down.0 > 0 {
            Some(false)
        } else {
            None
        }
    }

    /// The item at pane column `x`, row `y`.
    pub fn hit_test(&self, x: usize, y: usize) -> Option<&Target> {
        if self.rail() {
            return self.rail_targets.iter().find(|(ty, _)| *ty == y).map(|(_, t)| t);
        }
        // rows 0..BRAND_H are the brand rows: not clickable, not part of the scrolled list
        if y < BRAND_H {
            return None;
        }
        let y = y - BRAND_H;
        if y >= self.list_h || self.more_at(y + BRAND_H).is_some() {
            return None;
        }
        self.items
            .iter()
            .find(|it| {
                it.cols.map_or(true, |(a, b)| (a..b).contains(&x)) && (0..it.height).any(|l| self.list_row(it, l) == Some(y))
            })
            .map(|it| &it.target)
    }

    // --- rendering ------------------------------------------------------------------
    /// The compact rail layout, used whenever the pane is narrower than 16 columns.
    fn rail(&self) -> bool {
        self.w < 16
    }

    pub fn build_grid(&mut self) -> Grid {
        if self.rail() {
            return self.build_rail();
        }
        let w = self.w;
        let h = self.h;
        let c = self.c;
        let now_ms = self.now_ms;
        let mut grid = Grid::new(w, h);

        // The bottom dock: a blank row, then a rule with the host set into it like the header's labels (a plain
        // rule without a host), then the limits (a blank row, one row per window, a blank row) or, without
        // them, one blank row. A footer note (an error, the self-test, daemon down) gets its own row above it.
        self.footer_h = self.footer_note().is_some() as usize;
        let (items, total) = self.build_items(c, now_ms);
        // The limits stay (owner: they must always be visible); a list that does not fit scrolls, with the own
        // card kept in view. Only a window so short that the list would get fewer than MIN_LIST_ROWS rows drops
        // them.
        let want_usage = if w >= 3 { self.usage_height() } else { 0 };
        let free = h.saturating_sub(BRAND_H + self.footer_h);
        self.usage_h = if want_usage > 0 && 2 + want_usage + total.min(MIN_LIST_ROWS) <= free { want_usage } else { 0 };
        self.dock_h = if self.usage_h > 0 {
            2 + self.usage_h
        } else if self.host_chip_on() {
            3
        } else {
            0
        };
        self.list_h = h.saturating_sub(BRAND_H + self.dock_h + self.footer_h).min(h);

        // Arrival fade the FIRST time the sidebar is actually seen (usually a fresh attach), not at start
        // while hidden.
        if self.viewed() && !self.seen_viewed {
            self.seen_viewed = true;
            if !self.anims.is_off() && self.current_fade_start.is_none() {
                self.current_fade_start = Some(now_ms as f64);
                self.logo_shine_start = Some(now_ms as f64);
            }
        }

        self.items = items;
        // The cursor rests on the own card, so a scrolled list shows it (unless the user scrolled the list
        // with the wheel).
        if self.follow_own {
            self.rest_cursor();
        }
        self.layout_list(total);

        let viewed = self.viewed();
        let sweep = self.fx.edge_sweep.as_ref().map(|s| {
            let t = (now_ms as f64 - s.start_ms) / s.dur_ms;
            Effects::ease(t) * 4.0
        });

        // brand rows pinned to the top of the pane (not part of the scrolled list)
        let shine = self.logo_shine_pos(now_ms);
        for (y, row) in m::brand_logo(w, shine, self.tier).iter().enumerate() {
            for (x, c) in row.iter().enumerate() {
                grid.set(y, x, RowCell { ch: c.ch, fg: c.fg, bg: c.bg, bold: c.bold });
            }
        }

        for it in &self.items {
            let is_card = it.is_session;
            let mb = 0.0; // amount blended toward BG (0 = full, 1 = pure BG); no cascade any more

            for line_idx in 0..it.height {
                let Some(ly) = self.list_row(it, line_idx) else {
                    continue;
                };
                let y = ly + BRAND_H;
                let line = &it.lines[line_idx];

                if is_card {
                    // The own card wears its CURRENT look only to the degree `own_current_alpha` allows:
                    // hidden -> neutral, first-seen / SIGUSR1 -> 3-frame fade in, otherwise -> current.
                    let own_alpha = if it.current { self.own_current_alpha(now_ms) } else { 1.0 };
                    let base_card = if it.current {
                        let neutral = if it.state == State::NeedsYou { m::CARD_NEEDS } else { m::CARD_IDLE };
                        lerp_bg(neutral, m::CARD_CURRENT, own_alpha)
                    } else if it.state == State::NeedsYou {
                        m::CARD_NEEDS
                    } else {
                        m::CARD_IDLE
                    };
                    let card = base_card;
                    let card = set_pulse(card, &self.fx, it, now_ms, viewed);
                    let card = attention_bg(&self.fx, it, now_ms, viewed, card);

                    // finish flash: bg #3f5a45 -> normal over 600ms
                    let flash = if let Target::Session(nm) = &it.target {
                        self.finish_flash_for(nm, now_ms)
                    } else {
                        None
                    };
                    let card_final = match flash.map(|t| (t / FINISH_DUR_MS).clamp(0.0, 1.0)) {
                        Some(t) => lerp_bg(FINISH_BG, card, t),
                        None => card,
                    };

                    let mix = lerp_bg(card_final, m::BG, mb);
                    // Only a card with its own colour (current, a flash or pulse) is drawn on a surface, inside the
                    // panel's side rules (columns 1..W-2); the own card has a coral bar in column 1.
                    let surface = mix != m::BG;
                    // A partially visible last row (grow/shrink) is a `▀` half-block in the card bg, no text.
                    if it.partial && line_idx + 1 == it.height {
                        if surface {
                            let block = RowCell { ch: '▀', fg: mix, bg: m::BG, bold: false };
                            for x in 1..w.saturating_sub(1) {
                                grid.set(y, x, block);
                            }
                        }
                        continue;
                    }
                    if surface {
                        let bg_cell = RowCell { ch: ' ', fg: m::BG, bg: mix, bold: false };
                        for x in 1..w.saturating_sub(1) {
                            grid.set(y, x, bg_cell);
                        }
                    }
                    // content in columns 1..=W-2 (glyphs also fade toward BG; own name blends to white)
                    for (x, g) in (1usize..w - 1).zip(line.iter()) {
                        let gc = if it.current && g.color == m::NAME {
                            lerp_bg(m::NAME, m::NAME_CURRENT, own_alpha)
                        } else {
                            g.color
                        };
                        grid.set(y, x, RowCell {
                            ch: g.ch,
                            fg: lerp_bg(gc, m::BG, mb),
                            bg: mix,
                            bold: g.bold,
                        });
                    }
                    // finish flash: show a bold ✓ (instead of ●) at the line-1 glyph (grid col 3) for 1.2s
                    if line_idx == 0 {
                        if let Some(t) = flash {
                            if t < FINISH_GLYPH_MS {
                                grid.set(y, 3, RowCell { ch: glyphs::state(self.tier, '✓', glyphs::CHECK_RICH), fg: FINISH_CHECK, bg: mix, bold: true });
                            }
                        }
                    }
                    // working shimmer across the line-2 activity text
                    if line_idx == 1 && it.state == State::Working && self.anims.shimmer() && viewed {
                        self.shimmer_row(&mut grid, y, now_ms);
                    }
                    // the edge `▍` in column 1: the state's colour, coral for the own card, swept top to bottom when the
                    // own session changes; drawn after the shimmer, which must not recolour it
                    let edge = if it.current {
                        lerp_bg(m::item_edge_color(it.state, it.unread, it.watching), m::ACCENT, own_alpha)
                    } else {
                        m::item_edge_color(it.state, it.unread, it.watching)
                    };
                    let edge_show = match sweep {
                        Some(st) if it.current && own_alpha > 0.0 && viewed => (line_idx as f64) < st,
                        _ => true,
                    };
                    if edge_show {
                        grid.set(y, 1, RowCell { ch: '▍', fg: edge, bg: mix, bold: false });
                    }
                } else {
                    // action / non-session row on BG (text fades toward BG with the last card); the actions' rule
                    // runs into the last column
                    for (x, g) in (1usize..w).zip(line.iter()) {
                        if it.cols.is_some_and(|(a, b)| !(a..b).contains(&x)) {
                            continue;
                        }
                        let fg = g.color;
                        grid.set(y, x, RowCell {
                            ch: g.ch,
                            fg: lerp_bg(fg, m::BG, mb),
                            bg: m::BG,
                            bold: g.bold,
                        });
                    }
                }
            }
        }

        // "↑ N more" / "↓ N more" where cards are hidden (in the needs-you colour if one of them needs you)
        if self.pinned {
            if self.view_top == 1 && self.more_up.0 > 0 {
                self.draw_more(&mut grid, BRAND_H, '↑', self.more_up, c);
            }
            if self.more_down.0 > 0 {
                self.draw_more(&mut grid, BRAND_H + self.list_h - self.actions_h(), '↓', self.more_down, c);
            }
        }
        if self.usage_h > 0 {
            self.draw_usage(&mut grid, c, now_ms, viewed, 0.0);
        }
        if let Some(y) = self.dock_rule_y() {
            if w >= 4 {
                rule(&mut grid, y);
            }
            if self.host_chip_on() {
                self.draw_host_chip(&mut grid, y, w);
            }
        }
        if let Some(text) = self.footer_note() {
            if let Some(y) = h.checked_sub(1 + self.dock_h) {
                let fg = if self.footer_is_update() { m::ACCENT } else { m::C_DOWN };
                for (x, ch) in text.chars().enumerate() {
                    if x < w {
                        grid.set(y, x, RowCell { ch, fg, bg: m::BG, bold: false });
                    }
                }
            }
        }
        self.publish_menu_hit();
        grid
    }

    /// A "more" row: the arrow in the state-glyph column, the text lined up with the session names.
    fn draw_more(&self, grid: &mut Grid, y: usize, arrow: char, (n, needs): (usize, bool), c: usize) {
        let gap = if self.tier == Tier::Rich { "  " } else { " " };
        let fg = if needs { m::C_NEEDS_YOU } else { m::GREY };
        let text = pad(format!("  {arrow}{gap}{n} more").chars().map(|ch| Glyph { ch, color: fg, bold: needs }).collect(), c);
        grid.set(y, 0, RowCell { ch: ' ', fg: m::BG, bg: m::BG, bold: false });
        for (x, g) in (1usize..self.w).zip(text.iter()) {
            grid.set(y, x, RowCell { ch: g.ch, fg: g.color, bg: m::BG, bold: g.bold });
        }
    }

    /// Whether the host label is shown in the dock's rule (a host name and room for it).
    fn host_chip_on(&self) -> bool {
        !self.host.is_empty() && self.w >= 10
    }

    /// The row of the dock's rule (the host label's row), if the dock is drawn.
    fn dock_rule_y(&self) -> Option<usize> {
        if self.dock_h < 2 {
            return None;
        }
        self.h.checked_sub(self.dock_h - 1)
    }

    /// The host chip's name text, truncated to fit the layout (shared by drawing and hit-testing so the
    /// clickable cells always match what is drawn).
    fn chip_name(&self) -> String {
        // a remote we show that is not connected wears the down look; its text says so.
        let label = if self.remote_down() {
            match &self.host_status {
                Some(s) if s.state == "version" => format!("{} · other version", self.host),
                _ => format!("{} · offline", self.host),
            }
        } else {
            self.host.clone()
        };
        // ` ─ ` + icon + gap (up to 2) + name + ` ` + at least one `─`
        let room = self.w.saturating_sub(3 + 1 + 2 + 1 + 1);
        m::truncate(&label, room)
    }

    /// Width of the drawn host label in cells: the leading space, the icon, the gap, the name and the trailing
    /// space. Used by `draw_host_chip` (which walks these cells) and `chip_at` (which hit-tests them) so the
    /// two can never drift.
    fn chip_width(&self) -> usize {
        1 + 1 + self.chip_gap() + self.chip_name().chars().count() + 1
    }

    /// The gap between the host icon and its name (`m::icon_gap`): the same `─` on both sides of the label.
    fn chip_gap(&self) -> usize {
        m::icon_gap(self.w, 3 + self.chip_name().chars().count())
    }

    /// The column where the host label starts: centred on its rule with the same `─` on both sides
    /// (`m::label_rule`).
    fn chip_x0(&self) -> usize {
        m::label_x0(self.w, self.chip_width())
    }

    /// Whether the given cell is on the clickable host label: not rail, split (ui) mode, the label is drawn
    /// (`host_chip_on()`), on the dock's rule, the label plus one `─` each side.
    fn chip_at(&self, x: usize, y: usize) -> bool {
        if self.rail() || !self.ui_mode || !self.host_chip_on() {
            return false;
        }
        if Some(y) != self.dock_rule_y() {
            return false;
        }
        let x0 = self.chip_x0();
        (x0 - 1..=x0 + self.chip_width()).contains(&x)
    }

    /// Whether the given cell is on the clickable update note in the footer (SPEC §3): the note drawn when a newer
    /// Force is available opens the update card. Not in rail mode (no footer there).
    fn update_at(&self, x: usize, y: usize) -> bool {
        if self.rail() || !self.footer_is_update() {
            return false;
        }
        let Some(note) = self.footer_note() else { return false };
        if Some(y) != self.h.checked_sub(1 + self.dock_h) {
            return false;
        }
        x < note.chars().count()
    }

    /// The host label set into the dock's rule, centred, like the header's labels: ` @ host ` (RICH: the
    /// codicon `remote` icon in a 2-cell slot), icon coral, name in the label colour; all red when the remote
    /// is down (its text then says offline / other version).
    fn draw_host_chip(&self, grid: &mut Grid, y: usize, w: usize) {
        let icon = crate::glyphs::state(self.tier, '@', '\u{EB3A}');
        let name = self.chip_name();
        let down = self.remote_down();
        let chip_fg = if down { m::C_DOWN } else { m::HOST_LABEL };
        let icon_fg = if down { m::C_DOWN } else { m::ACCENT };
        let mut x = self.chip_x0();
        let put = |grid: &mut Grid, x: &mut usize, ch: char, fg: ratatui::style::Color| {
            if *x < w {
                grid.set(y, *x, RowCell { ch, fg, bg: m::BG, bold: false });
            }
            *x += 1;
        };
        put(grid, &mut x, ' ', chip_fg);
        put(grid, &mut x, icon, icon_fg);
        for _ in 0..self.chip_gap() {
            put(grid, &mut x, ' ', chip_fg);
        }
        for ch in name.chars() {
            put(grid, &mut x, ch, chip_fg);
        }
        put(grid, &mut x, ' ', chip_fg);
    }

    /// Publish where the host chip sits so tmux's own `MouseDown1Pane` binding can open the host picker, using the
    /// exact same conditions and width as `chip_at`/`chip_width`: only in the full layout, in split (ui) mode, when
    /// the label is shown (`host_chip_on()`), on the dock's rule. Otherwise publish `None`. Publishes on
    /// the sidebar's OWN pane's server/pane and only when the value changed since the last frame; a tmux error is not
    /// retried until the value changes.
    fn publish_menu_hit(&mut self) {
        let Some(pane) = &self.tmux_pane else { return };
        let hit = if self.rail() || !self.ui_mode {
            None
        } else if !self.host_chip_on() {
            None
        } else {
            self.dock_rule_y()
                .map(|y| {
                    let x0 = self.chip_x0();
                    crate::tmux::MenuHit { kind: "hosts", x0: x0 - 1, x1: x0 + self.chip_width() + 1, y0: y, y1: y + 1 }
                })
        };
        if self.last_menu_hit.as_ref() == Some(&hit) {
            return;
        }
        // a failure only costs the click-opened menu (the chip still opens the keyboard one): no footer error
        let _ = tmux::set_menu_hit(self.tmux.as_ref(), pane, hit.as_ref());
        self.last_menu_hit = Some(hit);
    }

    /// The narrow-rail layout (W < 16): one row per live session plus `a<count>`/`+` actions and, when
    /// it fits, a two-row usage strip at the bottom. Effects (arrival fade, finish flash, needs-you pulse and
    /// apply to each single rail row; there is no shimmer or grow/shrink in the rail.
    fn build_rail(&mut self) -> Grid {
        let w = self.w;
        let h = self.h;
        let now_ms = self.now_ms;
        let mut grid = Grid::new(w, h);

        // rail has no footer rows; usage shown at the bottom only if it fits.
        self.footer_h = 0;
        self.usage_h = 0;
        self.dock_h = 0;
        self.list_h = h.saturating_sub(RAIL_BRAND_H);

        // Arrival fade the FIRST time the sidebar is actually seen (mirrors the full layout).
        if self.viewed() && !self.seen_viewed {
            self.seen_viewed = true;
            if !self.anims.is_off() && self.current_fade_start.is_none() {
                self.current_fade_start = Some(now_ms as f64);
                self.logo_shine_start = Some(now_ms as f64);
            }
        }
        let (items, _total) = self.build_items(w, now_ms);
        self.items = items;
        self.offset = 0;

        let viewed = self.viewed();
        let sessions = Self::live_sessions(&self.snapshot);

        // --- fixed rows: brand ---------------------------------------------------------
        // brand: Force's coral bolt (RICH), or a bold coral `F` where there is no icon font
        let mark = if self.tier == Tier::Rich { '\u{F0E7}' } else { 'F' };
        grid.set(0, 1, RowCell { ch: mark, fg: m::ACCENT, bg: m::BG, bold: mark == 'F' });
        for x in 0..w {
            grid.set(1, x, RowCell { ch: '▔', fg: m::BRAND_UNDERLINE, bg: m::BG, bold: false });
        }

        // --- one row per live session ---------------------------------------------------
        let mut rail_targets: Vec<(usize, Target)> = Vec::new();
        let mut y = RAIL_BRAND_H;
        for s in sessions.iter() {
            let current = self.display_own() == Some(s.name.as_str());
            let it = Item {
                target: Target::Session(s.name.clone()),
                top: 0,
                height: 1,
                partial: false,
                lines: Vec::new(),
                is_session: true,
                current,
                state: s.state,
                unread: s.unread,
                watching: s.watching,
                cols: None,
            };
            let mb = 0.0;

            let own_alpha = if current { self.own_current_alpha(now_ms) } else { 1.0 };
            let neutral = if s.state == State::NeedsYou { m::CARD_NEEDS } else { m::CARD_IDLE };
            let base = if current {
                lerp_bg(neutral, m::CARD_CURRENT, own_alpha)
            } else if s.state == State::NeedsYou {
                m::CARD_NEEDS
            } else {
                m::CARD_IDLE
            };
            let base = set_pulse(base, &self.fx, &it, now_ms, viewed);
            let bg = attention_bg(&self.fx, &it, now_ms, viewed, lerp_bg(base, m::BG, mb));
            let edge = if current {
                lerp_bg(m::card_edge_color(s), m::ACCENT, own_alpha)
            } else {
                m::card_edge_color(s)
            };

            // row background over the full width
            for x in 0..w {
                grid.set(y, x, RowCell { ch: ' ', fg: m::BG, bg, bold: false });
            }
            // edge column 0
            grid.set(y, 0, RowCell { ch: '▍', fg: lerp_bg(edge, m::BG, mb), bg, bold: false });

            // index right-aligned in columns 1..=2 (#ffffff bold on the current session)
            let idx = s.index.unwrap_or(0);
            let idx_str = idx.to_string();
            let idx_color = if current { m::NAME_CURRENT } else { m::IDX };
            let idx_bold = current;
            // right-aligned so the last digit sits in column 2 (index area is columns 1..=2)
            let start_col = 2usize.saturating_sub(idx_str.len().saturating_sub(1));
            for (k, ch) in idx_str.chars().enumerate() {
                let x = start_col + k;
                if x <= 2 {
                    grid.set(y, x, RowCell { ch, fg: lerp_bg(idx_color, m::BG, mb), bg, bold: idx_bold });
                }
            }
            // column 4: the state glyph (spinner / needs-you `!` etc.); finish flash shows a bold ✓
            if self.finish_flash_for(&s.name, now_ms).is_some() {
                grid.set(y, 4, RowCell { ch: glyphs::state(self.tier, '✓', glyphs::CHECK_RICH), fg: FINISH_CHECK, bg, bold: true });
            } else {
                let glyph = m::card_glyph(self.tier, s, now_ms);
                let gfg = lerp_bg(m::session_glyph_color(s), m::BG, mb);
                grid.set(y, 4, RowCell { ch: glyph, fg: gfg, bg, bold: m::is_bold_glyph(s.state) });
            }

            rail_targets.push((y, Target::Session(s.name.clone())));
            y += 1;
        }

        // --- actions: blank, `a<count>` (archived), ` +` (new) ---------------------
        y += 1; // blank spacer
        let narch = self.snapshot.as_ref().map(|s| s.sessions.iter().filter(|s| s.archived).count()).unwrap_or(0);
        rail_targets.push((y, Target::Archived));
        // RICH lays the action rows out like the session rows: the count where the index goes, the icon in
        // the glyph column (col 4, with its 2-cell slot).
        let arch_row = if self.tier == Tier::Rich {
            format!("{:>3} {}", narch, '\u{F187}')
        } else {
            format!(" a{}", narch)
        };
        set_rail_text(&mut grid, y, w, arch_row, m::GREY);
        y += 1;
        rail_targets.push((y, Target::New));
        set_rail_text(&mut grid, y, w, if self.tier == Tier::Rich { format!("    {}", '\u{F067}') } else { " +".to_string() }, m::GREY);
        y += 1;
        rail_targets.push((y, Target::Chuck));
        set_rail_text(&mut grid, y, w, if self.tier == Tier::Rich { format!("    {}", CHUCK_RICH) } else { " ?".to_string() }, m::ACCENT);
        y += 1;

        // --- usage (2 rows + 1 blank) at the bottom if it is present and fits ----------
        self.rail_usage_show = !self.daemon() && usage_present(&self.snapshot) && y + 3 <= h;
        if self.rail_usage_show {
            self.draw_rail_usage(&mut grid, now_ms, viewed);
        }

        self.rail_targets = rail_targets;
        grid
    }

    /// The rail usage strip at the bottom: two rows `5h<nn>%` / `wk<nn>%` (label #808080, percentage
    /// by the threshold colour, `–` when expired) plus one blank row beneath.
    fn draw_rail_usage(&mut self, grid: &mut Grid, now_ms: i64, viewed: bool) {
        let usage = match self.snapshot.as_ref().and_then(|s| s.usage.as_ref()) {
            Some(u) => u,
            None => return,
        };
        let (mut f5, mut f7) = self.usage_pct();
        if viewed {
            if let Some(tw) = &self.fx.usage_tween {
                let progress = Effects::ease((now_ms as f64 - tw.start_ms) / tw.dur_ms);
                f5 = lerp_f(tw.from[0], tw.to[0], progress);
                f7 = lerp_f(tw.from[1], tw.to[1], progress);
            }
        }
        let now_s = now_ms / 1000;
        let y0 = self.h.saturating_sub(3); // two usage rows plus one blank beneath
        let mut row = y0;
        if let Some(w5) = &usage.five_hour {
            set_rail_text(grid, row, self.w, "5h".to_string(), m::USAGE_LABEL);
            frag(grid, row, self.w, w5, f5, now_s);
            row += 1;
        }
        if let Some(w7) = &usage.seven_day {
            set_rail_text(grid, row, self.w, "wk".to_string(), m::USAGE_LABEL);
            frag(grid, row, self.w, w7, f7, now_s);
        }
    }

    fn draw_usage(&mut self, grid: &mut Grid, _c: usize, now_ms: i64, viewed: bool, mb: f64) {
        let usage = match self.snapshot.as_ref().and_then(|s| s.usage.as_ref()) {
            Some(u) => u,
            None => return,
        };
        let (mut f5, mut f7) = self.usage_pct();
        if viewed {
            if let Some(tw) = &self.fx.usage_tween {
                let progress = Effects::ease((now_ms as f64 - tw.start_ms) / tw.dur_ms);
                f5 = lerp_f(tw.from[0], tw.to[0], progress);
                f7 = lerp_f(tw.from[1], tw.to[1], progress);
            }
        }
        let win = |w: &Option<snapshot::Window>, pct: f64| {
            w.map(|x| snapshot::Window { used_percentage: pct, resets_at: x.resets_at })
        };
        let a = snapshot::Usage { five_hour: win(&usage.five_hour, f5), seven_day: win(&usage.seven_day, f7) };
        // the first block's bars move with the tween; the others are drawn as they are
        let mut blocks = self.limits_blocks();
        if let Some(first) = blocks.first_mut() {
            first.1 = a;
        }
        // `resets_at` in the snapshot is unix-seconds; usage_lines/reset_text expect seconds here.
        let now_s = now_ms / 1000;
        let lines = m::limits_lines(&blocks, now_s, self.w, self.tz_offset, self.tier);
        let Some(rule_y) = self.dock_rule_y() else { return };
        let start = rule_y + 1; // under the dock's rule
        for (i, line) in lines.iter().enumerate() {
            let y = start + i;
            if y >= self.h {
                break;
            }
            for (x, g) in line.iter().enumerate() {
                if x < self.w {
                    grid.set(y, x, RowCell { ch: g.ch, fg: lerp_bg(g.color, m::BG, mb), bg: m::BG, bold: g.bold });
                }
            }
        }
    }
}

/// One plain `─` rule on row `y` over columns `1..w` (`m::label_rule_cells`): the free column on the left
/// mirrors the pane border on the right.
fn rule(grid: &mut Grid, y: usize) {
    let w = grid.w;
    if y >= grid.h || w < 4 {
        return;
    }
    for x in 1..w {
        grid.set(y, x, RowCell { ch: '─', fg: m::PANEL_RULE, bg: m::BG, bold: false });
    }
}

fn pad(mut v: Vec<Glyph>, c: usize) -> Vec<Glyph> {
    while v.len() < c {
        v.push(Glyph::plain(' '));
    }
    while v.len() > c {
        v.pop();
    }
    v
}

fn dim_row(text: &str, c: usize) -> Vec<Glyph> {
    pad(
        text.chars().map(|ch| Glyph { ch, color: m::GREY, bold: false }).collect(),
        c,
    )
}

/// The NEEDS_YOU attention pulse: the card background oscillates sinusoidally between #3a2b28 and #4d312c
/// (1.6 s period) for the first ~10 s after entering the state, then settles back to #3a2b28. Skipped when
/// not viewed or when the session is the current one (never added in that case anyway).
fn attention_bg(fx: &Effects, it: &Item, now_ms: i64, viewed: bool, base: Color) -> Color {
    if !viewed || it.current {
        return base;
    }
    let Target::Session(name) = &it.target else {
        return base;
    };
    if let Some(a) = fx.attentions.iter().find(|a| &a.name == name) {
        let t = now_ms as f64 - a.start_ms;
        if t < a.dur_ms {
            let phase = std::f64::consts::PI * 2.0 * t / 1600.0;
            let k = 0.5 + 0.5 * phase.sin();
            return lerp_bg(m::CARD_NEEDS, m::CARD_ATTN_HI, k);
        }
    }
    base
}

fn set_pulse(bg: Color, fx: &Effects, it: &Item, now_ms: i64, viewed: bool) -> Color {
    if !viewed {
        return bg;
    }
    match &it.target {
        Target::Session(name) => {
            if let Some(p) = fx.pulses.iter().find(|p| &p.name == name) {
                let t = Effects::ease((now_ms as f64 - p.start_ms) / p.dur_ms);
                lerp_bg(bg, p.color, 1.0 - t)
            } else {
                bg
            }
        }
        _ => bg,
    }
}

fn lerp_bg(a: Color, b: Color, t: f64) -> Color {
    match (a, b) {
        (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) => Color::Rgb(
            (r1 as f64 + (r2 as f64 - r1 as f64) * t) as u8,
            (g1 as f64 + (g2 as f64 - g1 as f64) * t) as u8,
            (b1 as f64 + (b2 as f64 - b1 as f64) * t) as u8,
        ),
        _ => b,
    }
}

fn lerp_f(a: f64, b: f64, t: f64) -> f64 {
    a + (b - a) * t
}

/// Ease-in-out (smoothstep), clamped to [0,1].
fn ease_in_out(t: f64) -> f64 {
    let t = t.clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

/// Whether the snapshot carries any usage window (rail usage strip shown only then).
fn usage_present(snap: &Option<Snapshot>) -> bool {
    snap.as_ref()
        .and_then(|s| s.usage.as_ref())
        .map(|u| u.five_hour.is_some() || u.seven_day.is_some())
        .unwrap_or(false)
}

/// Write `text` (single colour) onto a rail row starting at column 0 on the default BG.
fn set_rail_text(grid: &mut Grid, y: usize, w: usize, text: String, color: Color) {
    for (x, ch) in text.chars().enumerate() {
        if x < w {
            grid.set(y, x, RowCell { ch, fg: color, bg: m::BG, bold: false });
        }
    }
}

/// Write the rail usage percentage fragment for one window: `NN%` coloured by the threshold, or `–` when expired.
fn frag(grid: &mut Grid, y: usize, w: usize, win: &snapshot::Window, pct: f64, now_s: i64) {
    let text = if win.resets_at <= now_s {
        "–".to_string()
    } else {
        format!("{}%", pct.round() as i64)
    };
    let color = if win.resets_at <= now_s { m::USAGE_RESET } else { m::threshold_color(pct) };
    // right-aligned, at least one column after the 2-char label (`5h  43%`, `5h 100%`)
    let start = w.saturating_sub(text.chars().count()).max(3);
    for (x, ch) in text.chars().enumerate() {
        let xx = start + x;
        if xx < w {
            grid.set(y, xx, RowCell { ch, fg: color, bg: m::BG, bold: false });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use crossterm::event::KeyCode;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use crate::snapshot::{State, Usage, Window};
    use crate::tmux::Client;

    const NOW: i64 = 1_790_000_000_000;

    struct FakeTmux {
        log: Arc<Mutex<Vec<Vec<String>>>>,
        session: Option<String>,
        glyph: Arc<Mutex<Option<String>>>,
        clients_: Vec<Client>,
        agent: Option<String>,
        /// value returned for `#{@force_nested}` (None/empty <=> classic mode).
        nested: Option<String>,
        /// value returned for `#{@force_host_tmux}`.
        host_opt: Option<String>,
        /// argv prefix emulating a HOST tmux handle (ui fakes leave empty so classic logs are unchanged).
        prefix: Vec<String>,
        /// make `switch-client` fail (a host that refuses the switch).
        fail_switch: bool,
    }

    type Log = Arc<Mutex<Vec<Vec<String>>>>;

    impl FakeTmux {
        fn new() -> (Box<Self>, Log) {
            let log: Log = Arc::new(Mutex::new(Vec::new()));
            let glyph: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
            (Box::new(FakeTmux { log: log.clone(), glyph: glyph.clone(), session: None, clients_: Vec::new(), agent: None, nested: None, host_opt: None, prefix: Vec::new(), fail_switch: false }), log)
        }
        fn new_glyph() -> (Box<Self>, Arc<Mutex<Option<String>>>) {
            let log: Log = Arc::new(Mutex::new(Vec::new()));
            let glyph: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
            (Box::new(FakeTmux { log, glyph: glyph.clone(), session: None, clients_: Vec::new(), agent: None, nested: None, host_opt: None, prefix: Vec::new(), fail_switch: false }), glyph)
        }
    }

    impl Tmux for FakeTmux {
        fn run(&self, args: &[String]) -> Result<String, String> {
            // a host handle records its prefix; the ui handle records just the args (keeps classic logs intact)
            let mut entry = self.prefix.clone();
            entry.extend(args.iter().cloned());
            self.log.lock().unwrap().push(entry);
            match args.first().map(|s| s.as_str()) {
                Some("display") => {
                    if args.iter().any(|a| a == "#{@force_glyphs}") {
                        Ok(self.glyph.lock().unwrap().clone().unwrap_or_default())
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
                Some("switch-client") if self.fail_switch => Err("can't find session".into()),
                Some("list-panes") => Ok(self.agent.as_ref().map(|p| format!("agent\t{}", p)).unwrap_or_default()),
                _ => Ok(String::new()),
            }
        }
    }

    fn sess(name: &str, index: i32, state: State, archived: bool, viewed: bool) -> Session {
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
            activity: None,
            title: None,
            mode: Some("auto".into()),
            model: Some("opus 5.5".into()),
            effort: Some("high".into()),
            shared: None,
            note: None,
            kind: "claude".into(),
            support: false,
            unread: false,
            watching: false,
        }
    }

    fn app_with(tmux: Box<dyn Tmux>) -> App {
        App::new(tmux, PathBuf::from("/nonexistent.json"), "/bin/true".into(), None, GlyphArg::Fixed(Tier::Plain), 0, 28, 40)
    }

    /// Paint an app Grid into a ratatui Buffer the same way main.rs does.
    fn paint(buf: &mut ratatui::buffer::Buffer, grid: &Grid) {
        use ratatui::style::{Modifier, Style};
        for y in 0..grid.h {
            for x in 0..grid.w {
                let c = grid
                    .row(y)
                    .get(x)
                    .copied()
                    .unwrap_or(RowCell { ch: ' ', fg: m::BG, bg: m::BG, bold: false });
                let mut style = Style::new().fg(c.fg).bg(c.bg);
                if c.bold {
                    style = style.add_modifier(Modifier::BOLD);
                }
                if let Some(cell) = buf.cell_mut((x as u16, y as u16)) {
                    cell.set_char(c.ch);
                    cell.set_style(style);
                }
            }
        }
    }

    fn buf_row(buf: &ratatui::buffer::Buffer, y: u16) -> String {
        let mut s = String::new();
        for x in 0..buf.area.width {
            if let Some(c) = buf.cell((x, y)) {
                s.push_str(c.symbol());
            } else {
                s.push(' ');
            }
        }
        s
    }

    // --- hit-testing: y row -> item, honouring scroll offset -------------------------
    #[test]
    fn hit_test_respects_scroll_offset() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![
                sess("s0", 0, State::Working, false, true),
                sess("s1", 1, State::Idle, false, true),
                sess("s2", 2, State::Idle, false, true),
            ],
            usage: None, limits: vec![],
        };
        let mut a = App::new(tmux, PathBuf::from("/nonexistent.json"), "/bin/true".into(), None, GlyphArg::Fixed(Tier::Plain), 0, 28, BRAND_H + 2);
        a.set_now(NOW);
        a.own = Some("s2".into());
        a.update_from_snapshot(Some(snap), NOW);
        a.build_grid(); // the list follows the own card: it scrolls to s2
        assert!(a.offset > 0);
        // with the list scrolled, the first list row maps to the own card, not the first one
        assert_eq!(a.hit_test(2, BRAND_H).map(|t| t.str()), Some("s2"));
    }

    #[test]
    fn hit_test_maps_row_to_item() {
        let (tmux, _log) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![
                sess("s0", 0, State::Working, false, true),
                sess("s1", 1, State::Idle, false, true),
                sess("s2", 2, State::Idle, false, true),
            ],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.build_grid(); // sets items + offset
        // rows 0..BRAND_H are the brand rows: not clickable
        for y in 0..BRAND_H {
            assert!(a.hit_test(2, y).is_none());
        }
        // the list starts at row BRAND_H; cards are back-to-back (2 rows each: no own card, one model line)
        assert_eq!(a.hit_test(2, BRAND_H).map(|t| t.str()), Some("s0"));
        assert_eq!(a.hit_test(2, BRAND_H + 2).map(|t| t.str()), Some("s1"));
        assert_eq!(a.hit_test(2, BRAND_H + 4).map(|t| t.str()), Some("s2"));
        assert!(a.hit_test(2, BRAND_H + 6).is_none(), "blank row above the actions");
        // one action row: new on the left, archived on the right
        assert_eq!(a.hit_test(4, BRAND_H + 7).map(|t| t.str()), Some("__new__"));
        assert_eq!(a.hit_test(a.w - 3, BRAND_H + 7).map(|t| t.str()), Some("__archived__"));
        // a blank row, then Chuck's row (SPEC §50), clickable over its whole width
        assert!(a.hit_test(4, BRAND_H + 8).is_none());
        assert_eq!(a.hit_test(2, BRAND_H + 9).map(|t| t.str()), Some("__chuck__"));
        assert_eq!(a.hit_test(a.w - 3, BRAND_H + 9).map(|t| t.str()), Some("__chuck__"));
        assert!(a.hit_test(2, BRAND_H + 10).is_none()); // past the list of items
    }

    #[test]
    fn host_label_truncates_and_keeps_its_row_under_notes() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.host = "a-very-long-hostname-for-a-cloud-vm".into();
        // no limits: the dock is a blank row, the host rule, a blank row
        let g = a.build_grid_at(NOW);
        let text = |g: &Grid, y: usize| g.row(y).iter().map(|c| c.ch).collect::<String>();
        let rule = text(&g, a.h - 2);
        assert!(rule.starts_with(" ─ @") && rule.contains("a-very") && rule.trim_end().ends_with("… ─"), "a full-width label keeps one rule cell each side: {rule:?}");
        assert_eq!(rule.chars().count(), a.w);
        assert!(rule.ends_with('─'), "the rule runs into the last column: {rule:?}");
        assert_eq!(text(&g, a.h - 1).trim(), "", "a blank row under the host rule");
        assert_eq!(text(&g, a.h - 3).trim(), "", "a blank row above it");
        assert_eq!(a.dock_rule_y(), Some(a.h - 2));
        // an error goes on its own row above the dock; the host rule keeps its place
        a.show_error("switch: boom".into());
        let g = a.build_grid_at(NOW);
        assert!(text(&g, a.h - 2).starts_with(" ─ @") && text(&g, a.h - 2).contains("a-very"));
        assert!(text(&g, a.h - 4).starts_with("switch: boom"), "{:?}", text(&g, a.h - 4));
        assert_eq!(a.footer_h, 1);
        // a failed self-test likewise
        a.error = None;
        let snap = Snapshot { update: None, smoke_failed: Some("self-test failed".into()), generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        let g = a.build_grid_at(NOW);
        assert!(text(&g, a.h - 4).contains("self-test failed"));
        assert!(text(&g, a.h - 2).starts_with(" ─ @") && text(&g, a.h - 2).contains("a-very"));
        // no host and no limits: no dock, the rows go back to the list
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        a.host.clear();
        a.build_grid_at(NOW);
        assert_eq!(a.footer_h, 0);
        assert_eq!(a.dock_h, 0);
        assert_eq!(a.dock_rule_y(), None);
    }

    #[test]
    fn host_chip_remote_offline_version_connected() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.host = "server".into();
        a.set_remote(Some(PathBuf::from("/nonexistent")), "server");
        // offline: icon and text in the down colour, on the shell background (no chip)
        a.host_status = Some(hosts::HostStatus { state: "offline".into(), detail: String::new() });
        {
            let g = a.build_grid_at(NOW);
            let (y, x0) = (a.dock_rule_y().unwrap(), a.chip_x0());
            let line: String = g.row(y).iter().map(|c| c.ch).collect();
            assert!(line.starts_with(" ─") && line.contains(" @") && line.contains("server · offline ─"), "{line:?}");
            assert!(g.row(y).iter().all(|c| c.bg == m::BG));
            assert_eq!(g.row(y)[x0 + 1].fg, m::C_DOWN);
            assert_eq!(g.row(y)[x0 + 3].fg, m::C_DOWN);
        }
        // version
        a.host_status = Some(hosts::HostStatus { state: "version".into(), detail: String::new() });
        {
            let g = a.build_grid_at(NOW);
            let line: String = g.row(a.dock_rule_y().unwrap()).iter().map(|c| c.ch).collect();
            assert!(line.contains("server · other vers…"), "truncated to keep a rule end: {line:?}");
        }
        // connected: coral icon, the name in the label colour, the rule around it
        a.host_status = Some(hosts::HostStatus { state: "connected".into(), detail: String::new() });
        {
            let g = a.build_grid_at(NOW);
            let (y, x0) = (a.dock_rule_y().unwrap(), a.chip_x0());
            let line: String = g.row(y).iter().map(|c| c.ch).collect();
            assert!(line.contains("─ @  server ─"), "{line:?}");
            // centred: the same rule on each side, running into the last column (beside the pane border)
            let left = line[..line.find(" @").unwrap()].chars().filter(|&c| c == '─').count();
            let right = line.chars().rev().take_while(|&c| c == '─').count();
            assert_eq!(left, right, "centred: {left} vs {right} in {line:?}");
            assert_eq!(g.row(y)[1].fg, m::PANEL_RULE);
            assert_eq!(g.row(y)[x0 + 1].fg, m::ACCENT);
            assert_eq!(g.row(y)[x0 + 3].fg, m::HOST_LABEL);
            assert!(g.row(y).iter().all(|c| c.bg == m::BG));
        }
    }

    #[test]
    fn update_footer_shows_only_without_error_and_in_accent() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        let snap = Snapshot { update: Some("2.2.0".into()), smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        // shown when there is no error
        assert_eq!(a.footer_text(), Some("↑ Force 2.2.0 · force update".to_string()));
        let g = a.build_grid_at(NOW);
        let row: String = g.row(a.h - 1).iter().map(|c| c.ch).collect();
        assert!(row.contains("↑ Force 2.2.0"), "{row:?}");
        assert_eq!(g.row(a.h - 1)[1].fg, m::ACCENT, "the update note uses the accent, not the error colour");
        assert_ne!(m::ACCENT, m::C_DOWN);
        // an error wins over the update note
        a.show_error("boom".into());
        assert_eq!(a.footer_text(), Some("boom".to_string()));
        let g = a.build_grid_at(NOW);
        let row: String = g.row(a.h - 1).iter().map(|c| c.ch).collect();
        assert!(row.contains("boom"), "{row:?}");
    }

    #[test]
    fn update_click_opens_the_card_in_a_popup() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.set_now(NOW);
        let snap = Snapshot { update: Some("2.2.0".into()), smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        let _ = a.build_grid_at(NOW);
        let y = a.h.checked_sub(1 + a.dock_h).unwrap();
        assert!(a.footer_is_update() && a.footer_note().is_some(), "the update note is the footer");
        assert!(a.update_at(0, y), "the note row is clickable");
        assert!(a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), 0, y as u16));
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("display-popup")).expect("a popup");
        assert_eq!(&e[1..6], &["-E", "-w", "72", "-h", "20"]);
        for w in crate::tmux::CHUCK_POPUP_STYLE {
            assert!(e.iter().any(|x| x == w), "styled like the card's: {w}");
        }
        assert_eq!(e.last().unwrap(), &format!("{} update --ui", crate::tmux::shell_quote("/bin/true")));
    }

    #[test]
    fn update_click_without_a_newer_release_opens_nothing() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.set_now(NOW);
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        let _ = a.build_grid_at(NOW);
        let y = a.h.checked_sub(1 + a.dock_h).unwrap();
        assert!(!a.footer_is_update(), "no update -> footer_is_update is false");
        assert!(!a.update_at(0, y), "not clickable without an update");
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), 0, y as u16);
        let log = log.lock().unwrap();
        assert!(!log.iter().any(|e| e.first().map(|s| s.as_str()) == Some("display-popup")), "no popup");
    }

    #[test]
    fn update_popup_passes_the_popup_env_in_ui_mode() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.ui_mode = true;
        a.set_now(NOW);
        let snap = Snapshot { update: Some("2.2.0".into()), smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        a.update_from_snapshot(Some(snap), NOW);
        let _ = a.build_grid_at(NOW);
        let y = a.h.checked_sub(1 + a.dock_h).unwrap();
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), 0, y as u16);
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("display-popup")).expect("a popup");
        assert!(e.iter().any(|x| x.starts_with("FORCE_GLYPHS=")), "popup_env (-e FORCE_GLYPHS) is passed");
    }

    #[test]
    fn remote_no_snapshot_placeholder_and_footer_text() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.host = "server".into();
        a.set_remote(Some(PathBuf::from("/nonexistent")), "server");
        a.host_status = None; // no status -> offline
        assert_eq!(a.footer_text(), Some("server offline".to_string()));
        let joined: String = (0..a.h).map(|y| a.build_grid_at(NOW).row(y).iter().map(|c| c.ch).collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(joined.contains("server offline"), "placeholder row: {joined:?}");
        // version variant
        a.host_status = Some(hosts::HostStatus { state: "version".into(), detail: String::new() });
        assert_eq!(a.footer_text(), Some("server: other version".to_string()));
        let joined: String = (0..a.h).map(|y| a.build_grid_at(NOW).row(y).iter().map(|c| c.ch).collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(joined.contains("server: other version"), "placeholder row: {joined:?}");
    }

    // --- full-sidebar render into a real buffer -------------------------------------
    #[test]
    fn renders_full_sidebar_cards_and_usage() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![
                sess("own", 0, State::Working, false, true),
                sess("other", 1, State::Idle, false, true),
                sess("done", 2, State::Idle, true, false),
            ],
            usage: Some(Usage {
                five_hour: Some(Window { used_percentage: 43.0, resets_at: NOW / 1000 + 3600 }),
                seven_day: Some(Window { used_percentage: 13.0, resets_at: NOW / 1000 + 86_400 }),
            }), limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.own = Some("own".into());
        let grid = a.build_grid();

        let mut term = Terminal::new(TestBackend::new(a.w as u16, a.h as u16)).unwrap();
        term.draw(|f| paint(f.buffer_mut(), &grid)).unwrap();
        let buf = term.backend().buffer();

        // current ("own") session is first: its 2 card rows carry the edge glyph in column 1 (inside the panel)
        for y in 0..2u16 {
            assert_eq!(buf_row(buf, y + BRAND_H as u16).chars().nth(1), Some('▍'), "row {}", y + BRAND_H as u16);
        }
        // the row right after the own card is the next card's first row, still with an edge glyph
        assert_eq!(buf_row(buf, BRAND_H as u16 + 2).chars().nth(1), Some('▍'), "other card's first row follows own");
        // the sessions panel: the brand rule above the list (a blank row between), a plain rule under the actions
        let srule = buf_row(buf, 1);
        assert!(srule.starts_with(" ──") && srule.contains("F O R C E"), "brand rule: {:?}", srule);
        assert_eq!(buf_row(buf, BRAND_H as u16 - 1).trim(), "", "a blank row under the brand rule");
        let joined: String = (0..buf.area.height).map(|y| buf_row(buf, y)).collect::<Vec<_>>().join("\n");
        assert!(joined.contains("archived (1)"), "missing archived count:\n{}", joined);
        assert!(joined.contains("+ new"), "missing new-session action:\n{}", joined);
        assert!(!joined.contains("PLAN USAGE"), "no usage header any more:\n{}", joined);
        assert!(joined.contains("   5h "), "missing 5h label:\n{}", joined);
        assert!(joined.contains("   wk "), "missing wk label:\n{}", joined);
        assert!(!(BRAND_H as u16..a.dock_rule_y().unwrap() as u16).any(|y| { let r = buf_row(buf, y); !r.trim().is_empty() && r.trim().chars().all(|c| c == '─') }),
            "no rule under the actions any more");
        // the dock: a blank row, the rule (plain without a host), a blank row, 5h, wk, a blank row, on BG
        let last = (a.h - 1) as u16;
        assert_eq!(buf_row(buf, last).trim(), "", "a blank last row");
        assert!(buf_row(buf, last - 2).starts_with("   5h ") && buf_row(buf, last - 1).starts_with("   wk "));
        assert_eq!(buf_row(buf, last - 3).trim(), "");
        assert_eq!(buf_row(buf, last - 4).trim(), "─".repeat(a.w - 1), "dock rule: {:?}", buf_row(buf, last - 4));
        assert_eq!(buf_row(buf, last - 5).trim(), "", "a blank row above the dock");
        assert!((0..a.w).all(|x| buf.cell((x as u16, last - 4)).unwrap().bg == m::BG), "rules sit on BG");
    }

    // --- brand rows -------------------------------------------------------------------
    #[test]
    fn brand_rows_text_and_colours() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::Idle, false, true)],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);

        // the brand: a blank row, ` F O R C E ` centred in a rule (PLAIN: no bolt icon), a blank row
        a.host = "devhost".into();
        let grid = a.build_grid();
        let row = |y: usize| -> String { grid.row(y).iter().map(|c| c.ch).collect() };
        assert_eq!(BRAND_H, 3, "blank, the brand rule, blank");
        assert_eq!(a.w, 28);
        assert_eq!(row(0).trim(), "", "top margin");
        // 28 columns: 26 rule cells would leave 15 `─` (7 + 8), so the rule runs into the last column (27 cells):
        // the 11-cell label starts at 1 + (27 - 11) / 2 = 9, with 8 `─` on each side
        assert_eq!(row(1), format!(" {} F O R C E {}", "─".repeat(8), "─".repeat(8)), "centred brand rule");
        assert_eq!(row(2).trim(), "", "a blank row before the sessions");
        assert!(row(3).starts_with(" ▍"), "the first card right after it: {:?}", row(3));
        assert_eq!(grid.row(1)[1].fg, m::PANEL_RULE);
        assert_eq!(grid.row(1)[10].ch, 'F');
        assert!(!grid.row(1)[10].bold, "quiet letters, not bold");
        assert_eq!(grid.row(1)[10].fg, ratatui::style::Color::Rgb(0xa0, 0xa0, 0xa0));
        assert!((0..a.w).all(|x| grid.row(1)[x].bg == m::BG));
        // RICH: the coral bolt leads the letters, one-cell gaps throughout, the same rule on both sides
        let sides = |r: &str| {
            let left = r.chars().skip(1).take_while(|&c| c == '─').count();
            let right = r.trim_end().chars().rev().take_while(|&c| c == '─').count();
            (left, right)
        };
        for w in [28, 29, 30, 31] {
            let rich = m::brand_logo(w, None, Tier::Rich);
            let r1: String = rich[1].iter().map(|c| c.ch).collect();
            assert!(r1.contains("─ \u{F0E7}") && r1.contains(" F O R C E ─"), "{r1:?}");
            assert!(r1.ends_with('─'), "the rule runs into the last column, beside the pane border: {r1:?}");
            let (l, r) = sides(&r1);
            assert_eq!(l, r, "w={w}: {r1:?}");
            let bolt = rich[1].iter().position(|c| c.ch == '\u{F0E7}').unwrap();
            assert_eq!(rich[1][bolt].fg, m::ACCENT);
        }
        // the arrival sweep warms the letter under it to peach
        let lit = m::brand_logo(28, Some(10.0), Tier::Plain);
        assert_eq!(lit[1][10].fg, ratatui::style::Color::Rgb(0xf6, 0xc2, 0xa4));
        assert_eq!(lit[1][18].fg, ratatui::style::Color::Rgb(0xa0, 0xa0, 0xa0), "letters away from it stay grey");
        // the host is set into the dock's rule: one-cell gaps, the same `─` on both sides
        let y = a.dock_rule_y().unwrap();
        let x0 = a.chip_x0();
        // ` @ devhost ` = 11 cells: like the brand, the rule runs into the last column, 8 `─` each side
        assert_eq!(row(y), format!(" {} @ devhost {}", "─".repeat(8), "─".repeat(8)), "host rule, even on both sides");
        assert_eq!(x0, 9);
        assert_eq!(grid.row(y)[x0 + 1].fg, m::ACCENT);
        assert_eq!(grid.row(y)[x0 + 3].fg, m::HOST_LABEL);
        assert_eq!(grid.row(y)[x0 + 3].bg, m::BG);
        // an even-length name keeps the free last column, still even on both sides
        a.host = "thinkpad".into();
        let grid = a.build_grid();
        let r: String = grid.row(a.dock_rule_y().unwrap()).iter().map(|c| c.ch).collect();
        assert_eq!(r, format!(" {} @  thinkpad {}", "─".repeat(7), "─".repeat(7)), "a 2-cell icon slot evens it out");
    }

    #[test]
    fn logo_shine_runs_once_on_arrival_and_not_in_off() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.seen_viewed = true;
        let still = a.build_grid_at(NOW);
        a.sigusr1(NOW);
        assert!(a.event_anim_running(), "the sweep asks for frames");
        let mid = a.build_grid_at(NOW + 350);
        let differs = (0..3).any(|y| (0..a.w).any(|x| mid.row(y)[x] != still.row(y)[x]));
        assert!(differs, "the sweep lightens part of the logo mid-way");
        let after = a.build_grid_at(NOW + 800);
        assert!((0..3).all(|y| after.row(y) == still.row(y)), "back to the still logo after 700 ms");
        a.set_anims(AnimMode::Off);
        a.sigusr1(NOW + 1000);
        let off = a.build_grid_at(NOW + 1350);
        assert!((0..3).all(|y| off.row(y) == still.row(y)), "no sweep with animations off");
    }

    // --- card rows: edge, card background, gap row ------------------------------------
    #[test]
    fn card_rows_edge_and_backgrounds() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![
                sess("own", 0, State::Working, false, true),
                sess("needs", 1, State::NeedsYou, false, true),
                sess("idle", 2, State::Idle, false, true),
            ],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.own = Some("own".into());
        // the needs-you session schedules an attention pulse; clear it so we assert resting colours
        a.fx.attentions.clear();
        a.seen_viewed = true;
        let grid = a.build_grid();

        // own, needs and idle cards from row BRAND_H, 2 rows each, back-to-back
        // edge `▍` in column 1 (inside the panel): current -> coral; needs-you -> #e8806f; idle -> #4e4e4e
        assert_eq!(grid.row(BRAND_H)[1].ch, '▍');
        assert_eq!(grid.row(BRAND_H)[1].fg, m::ACCENT);
        assert_eq!(grid.row(BRAND_H)[1].bg, m::CARD_CURRENT);
        assert_eq!(grid.row(BRAND_H + 2)[1].fg, m::C_NEEDS_YOU);
        assert_eq!(grid.row(BRAND_H + 2)[1].bg, m::CARD_NEEDS);
        assert_eq!(grid.row(BRAND_H + 4)[1].fg, m::EDGE_IDLE);
        assert_eq!(grid.row(BRAND_H + 4)[1].bg, m::CARD_IDLE);
        // only the own card has a surface: columns 1..=W-2; column 0 and W-1 stay on the panel's BG
        for x in 1..a.w - 1 {
            assert_eq!(grid.row(BRAND_H)[x].bg, m::CARD_CURRENT, "own card col {}", x);
            assert_eq!(grid.row(BRAND_H + 4)[x].bg, m::BG, "idle card col {}", x);
        }
        assert_eq!(grid.row(BRAND_H)[0].bg, m::BG);
        assert_eq!(grid.row(BRAND_H)[a.w - 1].bg, m::BG);
        // no index numbers: the state glyph sits in column 3, the name from column 5
        let line: String = grid.row(BRAND_H + 4).iter().map(|c| c.ch).collect();
        assert!(line.starts_with(" ▍ ○ idle"), "idle card's first line: {:?}", line);
        // the row right after a card is the next card's first row with its own edge colour
        assert_eq!(grid.row(BRAND_H + 1)[1].fg, m::ACCENT, "own card's second row keeps its edge");
        assert_eq!(grid.row(BRAND_H + 2)[1].fg, m::C_NEEDS_YOU, "needs card's first row follows own card");
    }

    // --- usage panel texts and bar widths -------------------------------------------------
    #[test]
    fn usage_panel_texts_and_thresholds() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::Idle, false, true)],
            usage: Some(Usage {
                five_hour: Some(Window { used_percentage: 43.0, resets_at: NOW / 1000 + 3600 }),
                seven_day: Some(Window { used_percentage: 95.0, resets_at: NOW / 1000 + 86_400 }),
            }), limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        let grid = a.build_grid();
        let w = a.w;

        let barw = (w as i64 - 19).max(4) as usize;
        // the dock under the list: a blank row, the rule, a blank row, 5h, wk, a blank row
        let top = BRAND_H + a.list_h + 1;
        assert_eq!(a.usage_h, 4);
        assert_eq!(a.dock_h, 6);
        assert_eq!(a.dock_rule_y(), Some(top));
        let rule: String = grid.row(top).iter().map(|c| c.ch).collect();
        assert_eq!(rule.trim(), "─".repeat(w - 1), "dock rule: {rule:?}");
        assert_eq!(grid.row(top + 1).iter().map(|c| c.ch).collect::<String>().trim(), "", "a blank row under the rule");
        let line_a5 = &grid.row(top + 2);
        assert!(line_a5.iter().any(|c| c.ch == '━' || c.ch == '╸')); // the slim bar
        assert!(line_a5.iter().take(3).all(|c| c.ch == ' '));
        let label: String = line_a5.iter().skip(3).take(3).map(|c| c.ch).collect();
        assert_eq!(label, "5h ");
        // 7-day window (95%) -> high threshold colour on the filled bar end + pct column
        let line_a7 = &grid.row(top + 3);
        let text7: String = line_a7.iter().map(|c| c.ch).collect();
        assert!(text7.starts_with("   wk ") && text7.contains("95%"), "{text7:?}");
        let nbar_filled = line_a7.iter().skip(6).take(barw)
            .filter(|c| (c.ch == '━' || c.ch == '╸') && c.fg != m::EMPTY_TRACK).count();
        assert!(nbar_filled >= barw / 2, "week bar should be mostly full, got {} filled", nbar_filled);
        assert_eq!(grid.row(top + 4).iter().map(|c| c.ch).collect::<String>().trim(), "", "a blank last row");
        assert_eq!(top + 4, a.h - 1);
        // threshold colour boundaries in model (bar end colour)
        assert_eq!(m::threshold_color(69.0), m::THRESH_LO);
        assert_eq!(m::threshold_color(70.0), m::THRESH_MID);
        assert_eq!(m::threshold_color(95.0), m::THRESH_HI);
    }

    // --- NEEDS_YOU attention pulse ------------------------------------------------------
    #[test]
    fn needs_you_attention_pulse_viewed() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::NeedsYou, false, true)],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        // viewed = true (no own set -> viewed()==true), not current
        a.update_from_snapshot(Some(snap.clone()), NOW); // enters NEEDS_YOU while viewed
        assert!(!a.fx.attentions.is_empty(), "attention should be scheduled");
        let t0 = a.fx.attentions[0].start_ms;
        a.seen_viewed = true;
        // at +1ms the card background oscillates away from the resting #3a2b28
        let g1 = a.build_grid_at(NOW + 1);
        let bg = g1.row(BRAND_H)[5].bg;
        assert_ne!(bg, m::CARD_NEEDS, "card should oscillate during the pulse");
        // after 10 s the pulse has ended and the resting #3a2b28 returns
        a.fx.tick(t0 + 10_001.0);
        let g2 = a.build_grid_at(NOW);
        assert_eq!(g2.row(BRAND_H)[5].bg, m::CARD_NEEDS, "card settles to #3a2b28 after");
    }

    #[test]
    fn needs_you_no_pulse_when_not_viewed_or_current() {
        // not viewed: viewed=false for the own session
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("own", 0, State::NeedsYou, false, false)],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.own = Some("own".into());
        a.update_from_snapshot(Some(snap), NOW);
        assert!(a.fx.attentions.is_empty(), "no attention when not viewed");

        // current session: pulse skipped
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("own", 0, State::NeedsYou, false, true)],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.own = Some("own".into());
        a.update_from_snapshot(Some(snap), NOW);
        assert!(a.fx.attentions.is_empty(), "no attention for the current session");
        a.seen_viewed = true;
        let g = a.build_grid();
        assert_eq!(g.row(BRAND_H)[5].bg, m::CARD_CURRENT, "current card rests on #363636");
    }

    // --- fake tmux records exact argv for activate ----------------------------------
    #[test]
    fn activate_switches_then_selects() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
        tmux.agent = Some("%12".into());
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.activate("own");
        let log = log.lock().unwrap();
        // own_client() calls list-clients first; find the switch/select entries.
        let idx = log.iter().position(|e| e.first().map(|s| s.as_str()) == Some("switch-client")).unwrap();
        assert_eq!(log[idx], ["switch-client", "-c", "c0", "-t", "=own"]);
        let sel = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("select-pane")).unwrap();
        assert_eq!(*sel, ["select-pane", "-t", "%12"]);
    }

    // --- fake tmux records exact argv for each dialog popup --------------------------
    #[test]
    fn chuck_opens_his_card_in_a_popup() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.activate(CHUCK_TARGET);
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("display-popup")).expect("a popup");
        assert_eq!(&e[1..6], &["-E", "-w", "72", "-h", "17"]);
        for w in crate::tmux::CHUCK_POPUP_STYLE {
            assert!(e.iter().any(|x| x == w), "styled like the menu's: {w}");
        }
        assert_eq!(e.last().unwrap(), &format!("{} support --ui", crate::tmux::shell_quote("/bin/true")));
    }

    #[test]
    fn popup_argv_for_each_kind() {
        for (kind, w, h, name) in [
            ("new", 64u32, 18u32, None),
            ("archived", 72u32, 21u32, None),
            ("rename", 64u32, 12u32, Some("a'b")),
            ("move", 64u32, 12u32, Some("proj 2")),
            ("archive", 64u32, 11u32, Some("proj 2")),
            ("delete", 64u32, 11u32, Some("proj 2")),
        ] {
            let (mut tmux, log) = FakeTmux::new();
            tmux.clients_.push(Client { name: "c0".into(), session: "own".into(), activity: 9 });
            let mut a = app_with(tmux);
            a.own = Some("own".into());
            a.popup(kind, name, w, h);
            let log = log.lock().unwrap();
            let e = log
                .iter()
                .find(|e| e.first().map(|s| s.as_str()) == Some("display-popup"))
                .expect("display-popup argv recorded");
            assert_eq!(e[0], "display-popup");
            assert_eq!(e[1], "-E");
            assert_eq!(e[2], "-w");
            assert_eq!(e[3], format!("{}", w));
            assert_eq!(e[4], "-h");
            assert_eq!(e[5], format!("{}", h));
            assert_eq!(e[6], "-c");
            assert_eq!(e[7], "c0");
            let mut expect = format!("{} {}", crate::tmux::shell_quote("/bin/true"), crate::tmux::shell_quote("dialog"));
            expect = format!("{} {}", expect, crate::tmux::shell_quote(kind));
            if let Some(n) = name {
                expect = format!("{} {}", expect, crate::tmux::shell_quote(n));
            }
            assert_eq!(e.get(8).map(|s| s.as_str()), Some(expect.as_str()), "kind={}", kind);
        }
    }

    // --- split (ui) mode: detection, publish, switching, popup env -------------------
    #[test]
    fn ui_mode_detected_and_host_own_published_on_change() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.nested = Some("/dev/pts/42".into()); // @force_nested set <=> ui mode
        tmux.session = Some("ui-sess".into());
        let (mut host, hlog) = FakeTmux::new();
        host.clients_ = vec![Client { name: "/dev/pts/42".into(), session: "s1".into(), activity: 5 }];
        let mut a = app_with(tmux);
        a.tmux_pane = Some("%10".into());
        a.host_tmux = Some(host);
        a.refresh_ui_mode();
        assert!(a.ui_mode, "non-empty @force_nested enables ui mode");
        assert_eq!(a.nested.as_deref(), Some("/dev/pts/42"));
        assert_eq!(a.host_own.as_deref(), Some("s1"), "host own resolved from host list-clients by nested name");
        // publishes @force_session once, on the change, to the ui server
        let setopts: Vec<Vec<String>> = log
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e.first().map(|s| s.as_str()) == Some("set-option"))
            .cloned()
            .collect();
        assert_eq!(setopts.len(), 1);
        assert_eq!(setopts[0], ["set-option", "-w", "-t", "%10", "@force_session", "s1"]);
        // unchanged read -> no new publish
        a.refresh_ui_mode();
        let n2 = log.lock().unwrap().iter().filter(|e| e.first().map(|s| s.as_str()) == Some("set-option")).count();
        assert_eq!(n2, 1);
        // the host handle was asked for its clients
        assert!(hlog.lock().unwrap().iter().any(|e| e.first().map(|s| s.as_str()) == Some("list-clients")), "host list-clients queried");
    }

    #[test]
    fn activate_ui_mode_host_switch_with_prefix_and_ui_select() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("ui-sess".into());
        tmux.agent = Some("%99".into());
        let (mut host, hlog) = FakeTmux::new();
        host.clients_ = vec![Client { name: "/dev/pts/42".into(), session: "s1".into(), activity: 5 }];
        host.prefix = ["ssh", "-S", "/tmp/x", "h", "tmux", "-L", "force"].iter().map(|s| s.to_string()).collect();
        let mut a = app_with(tmux);
        a.tmux_pane = Some("%10".into());
        a.ui_mode = true;
        a.nested = Some("/dev/pts/42".into());
        a.host_own = Some("s0".into());
        a.host_tmux = Some(host);
        a.own = Some("ui-sess".into());
        a.now_ms = NOW;
        a.activate("s1");
        // the click is answered first: nothing sent to the host until the frame showing it is drawn
        assert!(!hlog.lock().unwrap().iter().any(|e| e.contains(&"switch-client".to_string())), "switch waits for the frame");
        assert!(a.current_fade_start.is_none(), "a clicked card is current at once, no fade");
        assert!(!a.run_pending(), "a done switch needs no redraw");
        // host switch uses the @force_host_tmux prefix + the nested client as -c
        assert!(hlog.lock().unwrap().iter().any(|e| *e == ["ssh", "-S", "/tmp/x", "h", "tmux", "-L", "force", "switch-client", "-c", "/dev/pts/42", "-t", "=s1"]), "host switch argv prefix+args");
        // optimistic host own set + published + ui select-pane of the main pane
        assert_eq!(a.host_own.as_deref(), Some("s1"));
        assert!(log.lock().unwrap().iter().any(|e| *e == ["set-option", "-w", "-t", "%10", "@force_session", "s1"]), "published");
        assert!(log.lock().unwrap().iter().any(|e| *e == ["select-pane", "-t", "%99"]), "ui select-pane of main pane");
    }

    fn ui_app(host_fail: bool) -> (App, Log) {
        let (mut tmux, _log) = FakeTmux::new();
        tmux.session = Some("ui-sess".into());
        tmux.agent = Some("%99".into());
        tmux.nested = Some("/dev/pts/42".into());
        let (mut host, hlog) = FakeTmux::new();
        host.clients_ = vec![Client { name: "/dev/pts/42".into(), session: "s0".into(), activity: 5 }];
        host.fail_switch = host_fail;
        let mut a = app_with(tmux);
        a.tmux_pane = Some("%10".into());
        a.ui_mode = true;
        a.nested = Some("/dev/pts/42".into());
        a.host_own = Some("s0".into());
        a.host_tmux = Some(host);
        a.own = Some("ui-sess".into());
        a.now_ms = NOW;
        (a, hlog)
    }

    #[test]
    fn ui_click_switch_is_sent_once_after_the_frame() {
        let (mut a, hlog) = ui_app(false);
        a.activate("s1");
        assert_eq!(a.display_own(), Some("s1"), "highlight moves on the click");
        assert!(!a.run_pending());
        assert!(!a.run_pending(), "nothing left to send");
        let n = hlog.lock().unwrap().iter().filter(|e| e.first().map(|s| s.as_str()) == Some("switch-client")).count();
        assert_eq!(n, 1);
    }

    #[test]
    fn ui_click_card_is_current_before_the_snapshot_says_viewed() {
        let (mut a, _hlog) = ui_app(false);
        // the daemon still reports the old session as the viewed one
        a.snapshot = Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, usage: None, limits: vec![],
            sessions: vec![sess("s0", 0, State::Idle, false, true), sess("s1", 1, State::Idle, false, false)] });
        assert_eq!(a.own_current_alpha(NOW), 1.0);
        a.activate("s1");
        assert!(a.viewed());
        assert_eq!(a.own_current_alpha(NOW), 1.0, "clicked card fully current at once");
        a.sigusr1(NOW + 100);
        assert!(a.viewed_until.unwrap() >= NOW + CLICK_VIEWED_MS, "a following signal does not shorten it");
    }

    #[test]
    fn ui_click_switch_failure_shows_error_and_restores_the_real_current() {
        let (mut a, _hlog) = ui_app(true);
        a.activate("s1");
        assert_eq!(a.display_own(), Some("s1"));
        assert!(a.run_pending(), "a failed switch redraws");
        assert!(a.error.as_ref().is_some_and(|(m, _)| m.starts_with("switch:")), "error shown");
        a.poll(NOW + 10);
        assert_eq!(a.display_own(), Some("s0"), "back to what the host shows");
    }

    #[test]
    fn switch_hook_signal_after_a_click_does_not_refade_the_card() {
        let (mut a, _hlog) = ui_app(false);
        a.anims = AnimMode::Full;
        a.activate("s0");   // the host already shows s0: the hook's re-read agrees with the click
        a.run_pending();
        a.sigusr1(NOW + 50);
        assert!(a.current_fade_start.is_none(), "the clicked card stays fully current");
        a.sigusr1(NOW + 2000);
        assert!(a.current_fade_start.is_some(), "a later signal (an attach) fades as before");
    }

    #[test]
    fn popup_argv_ui_mode_has_three_env_pairs_before_command() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.clients_.push(Client { name: "c0".into(), session: "ui-sess".into(), activity: 9 });
        let mut a = app_with(tmux);
        a.own = Some("ui-sess".into());
        a.ui_mode = true;
        a.nested = Some("/dev/pts/42".into());
        a.host_own = Some("s1".into());
        a.set_tier(Tier::Plain);
        a.popup("new", None, 64, 18);
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("display-popup")).expect("display-popup recorded");
        assert_eq!(e[6], "-c");
        assert_eq!(e[7], "c0");
        assert_eq!(e[8], "-e");
        assert_eq!(e[9], "FORCE_CLIENT=/dev/pts/42");
        assert_eq!(e[10], "-e");
        assert_eq!(e[11], "FORCE_SESSION=s1");
        assert_eq!(e[12], "-e");
        assert_eq!(e[13], "FORCE_GLYPHS=plain");
        let expect = format!("{} {} {}", crate::tmux::shell_quote("/bin/true"), crate::tmux::shell_quote("dialog"), crate::tmux::shell_quote("new"));
        assert_eq!(e.get(14).map(|s| s.as_str()), Some(expect.as_str()), "command still last, after the env pairs");
    }

    // --- host chip click / h key open the host picker (ui mode only) ------------------
    fn hosts_ui_app() -> (App, Log) {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("@N".into()); // `display -p -t <pane> '#{window_id}'` resolves via the fake's default
        tmux.clients_.push(Client { name: "c0".into(), session: "ui-sess".into(), activity: 9 });
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.tmux_pane = Some("%10".into());
        a.ui_mode = true;
        a.own = Some("ui-sess".into());
        a.host = "srv".into();
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.w = 28;
        a.h = 30;
        a.c = 26;
        a.build_grid();
        (a, log)
    }

    fn run_shell_cmds(log: &Log) -> Vec<String> {
        log.lock()
            .unwrap()
            .iter()
            .filter(|e| e.first().map(|s| s.as_str()) == Some("run-shell"))
            .map(|e| e.get(2).cloned().unwrap_or_default())
            .collect()
    }

    #[test]
    fn host_chip_click_opens_hosts_menu_in_ui_mode() {
        let (mut a, log) = hosts_ui_app();
        assert!(a.chip_at(a.chip_x0(), 28), "cell on the chip is clickable");
        assert!(a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), a.chip_x0() as u16, 28));
        let log = log.lock().unwrap();
        let e = log.iter().find(|e| e.first().map(|s| s.as_str()) == Some("run-shell")).expect("run-shell recorded");
        assert_eq!(e[0], "run-shell");
        assert_eq!(e[1], "-b");
        let expect = format!("{} hosts-menu --client {} --window {}", crate::tmux::shell_quote("/bin/true"), crate::tmux::shell_quote("c0"), crate::tmux::shell_quote("@N"));
        assert_eq!(e.get(2).map(|s| s.as_str()), Some(expect.as_str()));
        assert!(e[2].contains("hosts-menu --client") && e[2].contains("--window"));
    }

    #[test]
    fn chip_click_past_chip_does_nothing() {
        let (mut a, log) = hosts_ui_app();
        let width = a.chip_width();
        assert!(a.chip_at(a.chip_x0(), 28));
        // a column past the chip (and not on any list row) opens nothing
        let x = (a.chip_x0() + width + 1).min(a.w - 1);
        assert!(!a.chip_at(x, 28), "x={} past chip width {}", x, width);
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), x as u16, 28);
        assert!(run_shell_cmds(&log).is_empty(), "no run-shell for a click past the chip");
    }

    #[test]
    fn chip_click_no_run_shell_in_classic_mode() {
        let (mut a, log) = hosts_ui_app();
        a.ui_mode = false;
        assert!(!a.chip_at(a.chip_x0(), 28), "no chip in classic mode");
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), a.chip_x0() as u16, 28);
        assert!(run_shell_cmds(&log).is_empty(), "classic mode records no run-shell");
    }

    #[test]
    fn chip_click_opens_hosts_menu_under_a_footer_error() {
        let (mut a, log) = hosts_ui_app();
        a.show_error("switch: boom".into());
        assert!(a.footer_text().is_some());
        assert!(a.chip_at(a.chip_x0(), 28), "the chip stays clickable under a footer note");
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), a.chip_x0() as u16, 28);
        assert_eq!(run_shell_cmds(&log).len(), 1, "the hosts menu opens");
    }

    // --- menu-hit publish: where the host chip sits (so tmux can open the picker itself) ------
    fn sidebar_menu_app(classic: bool, error: Option<&str>) -> (App, Log) {
        let (mut tmux, log) = FakeTmux::new();
        tmux.session = Some("@N".into());
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = App::new(tmux, PathBuf::from("/nonexistent.json"), "/bin/true".into(), Some("%10".into()), GlyphArg::Fixed(Tier::Plain), 0, 28, 30);
        a.ui_mode = !classic;
        a.own = Some("ui-sess".into());
        a.host = "srv".into();
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.c = 26;
        if let Some(m) = error {
            a.show_error(m.into());
        }
        (a, log)
    }

    #[test]
    fn sidebar_publishes_hosts_menu_hit() {
        let (mut a, log) = sidebar_menu_app(false, None);
        let _ = a.build_grid_at(NOW);
        // chip conditions and width match chip_at/chip_width at w=28,h=30
        assert!(a.host_chip_on() && a.footer_text().is_none(), "chip is shown");
        assert_eq!(a.dock_rule_y(), Some(28), "the host rule: a blank row under it");
        let (x0, w) = (a.chip_x0(), a.chip_width());
        assert_eq!(x0, m::label_x0(a.w, w), "the label is centred on the rule");
        assert!(a.chip_at(x0 - 1, 28) && !a.chip_at(x0 - 2, 28), "the hit area starts one rule cell before the label");
        assert!(a.chip_at(x0 + w, 28), "and ends one rule cell after it");
        assert!(!a.chip_at(x0 + w + 1, 28), "the rule further on is off it");
        assert!(!a.chip_at(x0, 29), "the blank row under it is off it");
        // first frame publishes hosts; a second identical frame republishes nothing
        let guard = log.lock().unwrap();
        let menus: Vec<Vec<String>> = guard.iter().filter(|e| e.first().map(|s| s.as_str()) == Some("set-option")).cloned().collect();
        assert_eq!(menus.len(), 1, "one publish");
        assert_eq!(menus[0], [
            "set-option", "-p", "-t", "%10", "@force_menu_kind", "hosts",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x0", &format!("{}", x0 - 1),
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x1", &format!("{}", x0 + w + 1),
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y0", "28",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y1", "29",
        ]);
        drop(guard);
        let _ = a.build_grid_at(NOW);
        assert_eq!(log.lock().unwrap().iter().filter(|e| e.first().map(|s| s.as_str()) == Some("set-option")).count(), 1, "no republish on an identical frame");
    }

    #[test]
    fn sidebar_publishes_none_in_classic_mode() {
        let (mut a, log) = sidebar_menu_app(true, None);
        let _ = a.build_grid_at(NOW);
        let log = log.lock().unwrap();
        let menus: Vec<Vec<String>> = log.iter().filter(|e| e.first().map(|s| s.as_str()) == Some("set-option")).cloned().collect();
        assert_eq!(menus.len(), 1);
        assert_eq!(menus[0][5], "", "classic mode publishes no kind");
    }

    #[test]
    fn sidebar_publishes_hosts_when_footer_error_shown() {
        let (mut a, log) = sidebar_menu_app(false, Some("switch: boom"));
        assert!(a.footer_text().is_some());
        let _ = a.build_grid_at(NOW);
        let log = log.lock().unwrap();
        let menus: Vec<Vec<String>> = log.iter().filter(|e| e.first().map(|s| s.as_str()) == Some("set-option")).cloned().collect();
        assert_eq!(menus.len(), 1);
        assert_eq!(menus[0][5], "hosts", "the chip under a footer note stays clickable");
    }

    #[test]
    fn key_focuses_agent_no_popup_or_spawn() {
        let (mut tmux, log) = FakeTmux::new();
        tmux.agent = Some("pane1".into());
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, false, true)], usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.own = Some("s0".into());
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        assert!(a.on_key(KeyCode::Char('n')), "any key returns true");
        let log = log.lock().unwrap();
        assert!(!log.iter().any(|e| e.first().map(|s| s.as_str()) == Some("display-popup")), "no popup opened");
        assert!(!log.iter().any(|e| e.first().map(|s| s.as_str()) == Some("run-shell")), "no run-shell spawned");
        let panes: Vec<&Vec<String>> = log.iter().filter(|e| e.first().map(|s| s.as_str()) == Some("select-pane")).collect();
        assert_eq!(panes.len(), 1, "one select-pane of the agent pane");
        assert_eq!(panes[0][1], "-t");
        assert_eq!(panes[0][2], "pane1");
    }

    #[test]
    fn key_with_no_own_does_not_panic() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        assert!(a.own.is_none());
        assert!(a.on_key(KeyCode::Char('n')), "key still returns true with no own session");
    }

    // --- animation utilities -------------------------------------------------------
    fn idle_snap(names: &[&str]) -> Snapshot {
        let sessions = names
            .iter()
            .enumerate()
            .map(|(i, n)| sess(n, i as i32, State::Idle, false, true))
            .collect();
        Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions, usage: None, limits: vec![] }
    }

    fn write_snap_file(names: &[&str]) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "cockpit_sidebar_test_{}.json",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        let sessions: Vec<serde_json::Value> = names
            .iter()
            .enumerate()
            .map(|(i, n)| serde_json::json!({
                "name": n, "index": i, "state": "IDLE", "order": i,
                "viewed": true, "archived": false, "since_ms": NOW - 60_000,
            }))
            .collect();
        let snap = serde_json::json!({ "generated_ms": NOW, "sessions": sessions });
        std::fs::write(&path, serde_json::to_vec(&snap).unwrap()).unwrap();
        path
    }



    // --- finish flash: colour 0/300/600 ms + ✓ until 1.2 s -------------------
    #[test]
    fn finish_flash_colour_and_check_glyph() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        let mut s = sess("s0", 0, State::YourTurn, false, true);
        s.order = 0;
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![s], usage: None, limits: vec![] }), NOW);
        a.seen_viewed = true;
        a.finish_flash.insert("s0".into(), FinishFlash { start_ms: NOW as f64 });

        // t=0: bg is the flash green start, ✓ shown
        let g0 = a.build_grid_at(NOW);
        assert_eq!(g0.row(BRAND_H)[5].bg, FINISH_BG);
        assert_eq!(g0.row(BRAND_H)[3].ch, '✓');
        // t=300: midway between flash green and normal
        let g300 = a.build_grid_at(NOW + 300);
        let b = g300.row(BRAND_H)[5].bg;
        assert_ne!(b, FINISH_BG);
        assert_ne!(b, m::CARD_IDLE);
        assert_eq!(g300.row(BRAND_H)[3].ch, '✓');
        // t=900: flash colour done (600ms) but ✓ still shown until 1.2s
        let g900 = a.build_grid_at(NOW + 900);
        assert_eq!(g900.row(BRAND_H)[5].bg, m::CARD_IDLE);
        assert_eq!(g900.row(BRAND_H)[3].ch, '✓');
        // t=1300: ✓ gone, back to the normal ● glyph
        let g1300 = a.build_grid_at(NOW + 1300);
        assert_eq!(g1300.row(BRAND_H)[5].bg, m::CARD_IDLE);
        assert_eq!(g1300.row(BRAND_H)[3].ch, '●');
    }

    // --- grow / shrink heights + hit-testing during --------------------------------
    #[test]
    fn grow_shrink_heights_and_hit_testing() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        // feed (first live list: no motion), then a new session appears (grow)
        a.update_from_snapshot(Some(idle_snap(&["s0"])), NOW);
        a.update_from_snapshot(Some(idle_snap(&["s0", "s1"])), NOW);
        a.seen_viewed = true;

        // mid-grow: s1 has a partial/short height (half-row timing, so sample mid-flight)
        a.build_grid_at(NOW + 100);
        let s1 = a.items.iter().find(|i| matches!(&i.target, Target::Session(n) if n == "s1")).unwrap();
        assert!(s1.height > 0 && s1.height < 3, "growing height {}", s1.height);
        // hit-testing uses the animated layout
        assert_eq!(a.hit_test(2, s1.top + BRAND_H).map(|t| t.str()), Some("s1"));

        // now it disappears (shrink) and leaves once fully collapsed
        a.update_from_snapshot(Some(idle_snap(&["s0"])), NOW);
        a.build_grid_at(NOW + 80);
        let s1 = a.items.iter().find(|i| matches!(&i.target, Target::Session(n) if n == "s1"));
        let s1 = s1.expect("s1 still shrinking");
        assert!(s1.height > 0 && s1.height < 3, "shrinking height {}", s1.height);
        // after the motion it is removed entirely
        a.build_grid_at(NOW + 400);
        let s1 = a.items.iter().find(|i| matches!(&i.target, Target::Session(n) if n == "s1"));
        assert!(s1.is_none(), "s1 fully collapsed and removed");
    }

    // --- shimmer: centre lit, far unchanged; subtle/off never shimmer -------------
    #[test]
    fn shimmer_lights_band_only_in_full() {
        let make = |tmux: Box<dyn Tmux>| {
            let mut a = app_with(tmux);
            let mut s = sess("s0", 0, State::Working, false, true);
            s.order = 0;
            s.activity = Some("editing".into());
            a.set_now(NOW);
            a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![s], usage: None, limits: vec![] }), NOW);
            a.seen_viewed = true;
            a
        };
        // full: a cell near the moving centre is lifted toward SHIMMER_FG (line-2 row = screen row 3)
        let (tmux, _) = FakeTmux::new();
        let mut a = make(tmux);
        let g = a.build_grid_at(NOW + 800); // centre ≈ mid-span
        let expect = lerp_bg(m::BG, SHIMMER_FG, 0.8); // centre 13.5 -> col 13 sits 0.5 away, k=0.8
        assert_eq!(g.row(BRAND_H + 1)[13].fg, expect, "band lit at its centre");
        assert_eq!(g.row(BRAND_H + 1)[2].fg, m::GREY, "far cell untouched");

        // subtle: no shimmer highlight anywhere
        let (tmux, _) = FakeTmux::new();
        let mut a = make(tmux);
        a.set_anims(AnimMode::Subtle);
        let g = a.build_grid_at(NOW + 800);
        assert_eq!(g.row(BRAND_H + 1)[13].fg, m::BG, "subtle never shimmers");
    }

    #[test]
    fn off_renders_final_states() {
        let (mut tmux, _) = FakeTmux::new();
        tmux.session = Some("s0".into());
        let mut a = app_with(tmux);
        let mut s = sess("s0", 0, State::Working, false, true);
        s.order = 0;
        s.activity = Some("editing".into());
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![s], usage: None, limits: vec![] }), NOW);
        a.own = Some("s0".into());
        a.set_anims(AnimMode::Off);
        let g = a.build_grid();
        // final state on the very first frame: full card bg + edge
        assert_eq!(g.row(BRAND_H)[5].bg, m::CARD_CURRENT, "card at current bg on the first frame");
        assert_eq!(g.row(BRAND_H)[1].fg, m::ACCENT, "edge current coral in off");
        assert_eq!(g.row(BRAND_H)[13].fg, m::BG, "no shimmer in off");
        let g2 = a.build_grid_at(NOW + 5);
        assert!(g2.row(1)[2].fg != m::BG, "brand still renders");
    }

    // --- sigusr1 re-polls, forces viewing, and arms the arrival fade ------------
    #[test]
    fn sigusr1_repolls_and_arms_arrival_fade() {
        let (mut tmux, _) = FakeTmux::new();
        tmux.session = Some("s1".into());
        let path = write_snap_file(&["s0", "s1"]);
        let mut a = App::new(tmux, path, "/bin/true".into(), Some("P".into()), GlyphArg::Fixed(Tier::Plain), 0, 28, 40);
        a.set_anims(AnimMode::Full);
        a.sigusr1(NOW);

        assert_eq!(a.own.as_deref(), Some("s1"), "own re-queried");
        assert!(a.viewed(), "sigusr1 forces viewed for 2s");
        // a live origin is a plain switch: arrival fade armed.
        assert!(a.current_fade_start.is_some(), "arrival fade armed on SIGUSR1");
        // repeated poll after >2s: the forced-viewed window has elapsed.
        a.poll(NOW + 2500);
        assert!(a.viewed_until.unwrap_or(0) <= NOW + 2000, "forced-viewed window ended");
        std::fs::remove_file(&a.snapshot_path).ok();
    }

    // --- auto glyph tier: read @force_glyphs at startup + on SIGUSR1 -----------------
    #[test]
    fn auto_tier_reads_option_at_startup_and_on_sigusr1() {
        let (mut tmux, glyph) = FakeTmux::new_glyph();
        tmux.session = Some("s1".into());
        *glyph.lock().unwrap() = Some("rich".into());
        let mut a = App::new(tmux, PathBuf::from("/nonexistent.json"), "/bin/true".into(), Some("P".into()), GlyphArg::Auto, 0, 28, 40);
        assert_eq!(a.tier(), Tier::Rich, "auto reads rich at startup");

        *glyph.lock().unwrap() = Some("plain".into());
        assert!(a.sigusr1(NOW), "tier change forces a redraw");
        assert_eq!(a.tier(), Tier::Plain, "sigusr1 re-reads option");

        // absent / empty option => Plain
        *glyph.lock().unwrap() = None;
        assert!(!a.sigusr1(NOW), "Plain->Plain is not a change"); // already plain
        assert!(!a.sigusr1(NOW) && a.tier() == Tier::Plain);

        *glyph.lock().unwrap() = Some("rich".into());
        assert!(a.sigusr1(NOW));
        assert_eq!(a.tier(), Tier::Rich);
        std::fs::remove_file(&a.snapshot_path).ok();
    }

    // --- RICH action rows / rail keep their widths ---------------------------------
    #[test]
    fn rich_action_and_rail_rows_keep_width() {
        let (mut tmux, _) = FakeTmux::new();
        tmux.session = Some("s1".into());
        let mut a = App::new(tmux, PathBuf::from("/nonexistent.json"), "/bin/true".into(), Some("P".into()), GlyphArg::Fixed(Tier::Rich), 0, 28, 40);
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, true, false), sess("s1", 1, State::Idle, false, true)], usage: None, limits: vec![] }), NOW);
        a.seen_viewed = true;
        a.own = Some("s1".into());
        // plain rows for comparison
        let mut p = App::new(FakeTmux::new().0, PathBuf::from("/nonexistent.json"), "/bin/true".into(), Some("P".into()), GlyphArg::Fixed(Tier::Plain), 0, 28, 40);
        p.set_now(NOW);
        p.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![sess("s0", 0, State::Idle, true, false), sess("s1", 1, State::Idle, false, true)], usage: None, limits: vec![] }), NOW);
        p.seen_viewed = true;
        p.own = Some("s1".into());

        let gr = a.build_grid_at(NOW);
        let gp = p.build_grid_at(NOW);
        // Every RICH glyph is exactly 1 column wide (glyphs.rs assert_width_1), and the grid
        // lays cells out one column per char, so RICH rows occupy exactly the same columns.
        assert_eq!((gr.w, gr.h), (gp.w, gp.h), "grid dimensions identical");
        // RICH archived action row uses U+F187; new-session row uses U+F067.
        let combined = (0..gr.h).flat_map(|y| gr.row(y)).map(|c| c.ch).collect::<String>();
        assert!(combined.contains('\u{F187}'), "RICH archived icon present");
        assert!(combined.contains('\u{F067}'), "RICH new-session icon present");
        // one action row: the new icon leads it, the archived icon sits right before its label
        let row = (0..gr.h).map(|y| gr.row(y).iter().map(|c| c.ch).collect::<String>()).find(|r| r.contains("archived")).unwrap();
        assert!(row.starts_with(" ─ \u{F067}  new ─"), "the new label leads the rule: {row:?}");
        assert!(row.ends_with(" ─"), "the rule runs into the last column: {row:?}");
        assert!(row.contains("\u{F187}  archived"), "{row:?}");
    }

    // --- an unread finding colours the card's edge (SPEC §5) ------------------------
    #[test]
    fn unread_card_edge_is_blue() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        let mut s0 = sess("s0", 0, State::Working, false, false);
        s0.unread = true;
        let s1 = sess("s1", 1, State::Working, false, false);
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![s0, s1], usage: None, limits: vec![] }), NOW);
        let g = a.build_grid_at(NOW);
        assert_eq!(g.row(BRAND_H)[1].fg, m::C_UNREAD, "unread card edge");
        assert_eq!(g.row(BRAND_H + 1)[1].fg, m::C_UNREAD, "the edge runs down the card");
        assert_eq!(g.row(BRAND_H + 3)[1].fg, m::C_WORKING, "a working card without a finding stays amber");
    }

    // --- hidden own card renders NON-current (neutral) ------------------------------
    #[test]
    fn hidden_own_card_is_neutral() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        let mut s = sess("s0", 0, State::Working, false, false); // NOT viewed
        s.order = 0;
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions: vec![s], usage: None, limits: vec![] }), NOW);
        a.own = Some("s0".into());
        a.seen_viewed = true;
        a.current_fade_start = None; // nothing should highlight a hidden card
        let g = a.build_grid_at(NOW);
        assert_ne!(g.row(BRAND_H)[5].bg, m::CARD_CURRENT, "hidden own card is not current-styled");
        assert_eq!(g.row(BRAND_H)[5].bg, m::CARD_IDLE, "hidden own card uses its neutral bg");
        assert_eq!(g.row(BRAND_H)[1].fg, m::edge_color(State::Working), "hidden own edge follows state");
    }

    // --- arrival fade: exactly 3 frames (1/3, 2/3, 1) on the own card only --
    #[test]
    fn arrival_fade_three_frames_own_rows_only() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(idle_snap(&["s0", "s1"])), NOW);
        a.own = Some("s1".into());
        a.seen_viewed = true;
        a.current_fade_start = Some(NOW as f64);
        // frame 0 @ 0ms: own card at 1/3 blend (bg, edge, name all blended together)
        let g0 = a.build_grid_at(NOW);
        assert_eq!(g0.row(BRAND_H + 3)[5].bg, lerp_bg(m::CARD_IDLE, m::CARD_CURRENT, 1.0 / 3.0), "frame0 bg 33%");
        assert_eq!(g0.row(BRAND_H + 3)[1].fg, lerp_bg(m::edge_color(State::Idle), m::ACCENT, 1.0 / 3.0), "frame0 edge 33%");
        assert_ne!(g0.row(BRAND_H)[5].bg, m::CARD_CURRENT, "non-own card unchanged");
        // frame 1 @ 40ms: own card at 2/3 blend; other cards still identical
        let g1 = a.build_grid_at(NOW + 40);
        assert_eq!(g1.row(BRAND_H + 3)[5].bg, lerp_bg(m::CARD_IDLE, m::CARD_CURRENT, 2.0 / 3.0), "frame1 bg 66%");
        assert_eq!(g1.row(BRAND_H)[5].bg, g0.row(BRAND_H)[5].bg, "s0 card identical between frames");
        // frame 2 @ 80ms: own card full current
        let g2 = a.build_grid_at(NOW + 80);
        assert_eq!(g2.row(BRAND_H + 3)[5].bg, m::CARD_CURRENT, "frame2 bg 100%");
        assert_eq!(g2.row(BRAND_H)[5].bg, g0.row(BRAND_H)[5].bg, "s0 card still identical");
        // only the own card's 2 rows differ across the whole fade
        let mut changed_rows = 0usize;
        for y in 0..a.h {
            for x in 0..a.w {
                if g0.row(y)[x] != g2.row(y)[x] {
                    changed_rows += 1;
                    break;
                }
            }
        }
        assert_eq!(changed_rows, 2, "only the own card's 2 rows changed across the arrival fade");
    }

    // --- grow/shrink: half-row height + `▀` partial last row ----------------------
    #[test]
    fn grow_partial_last_row_half_block() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(idle_snap(&["s0"])), NOW);
        a.update_from_snapshot(Some(idle_snap(&["s0", "s1"])), NOW);
        a.seen_viewed = true;
        // the first frame where s1 has grown to 1 half-row -> height 1, partial -> `▀` half-block over the
        // card bg, no text.
        let (g, s1) = (NOW..NOW + CARD_DUR_MS as i64)
            .step_by(4)
            .find_map(|t| {
                let g = a.build_grid_at(t);
                let s1 = a.items.iter().find(|i| matches!(&i.target, Target::Session(n) if n == "s1"))?;
                Some((g, (s1.top, s1.height, s1.partial)))
            })
            .unwrap();
        let (top, height, partial) = s1;
        assert_eq!(height, 1, "growing s1 height is a single partial row");
        assert!(partial, "s1 flagged partial");
        let y = top + BRAND_H;
        // an idle card has no surface (CARD_IDLE is BG), so its partial row shows nothing
        assert_eq!(m::CARD_IDLE, m::BG);
        assert!(g.row(y).iter().all(|c| c.ch == ' ' && c.bg == m::BG), "no half-block for a card without a surface");
    }


    // --- idle 25ms ticks request NO redraws (0 draws over a simulated idle second) --
    #[test]
    fn idle_ticks_request_no_draws() {
        let (tmux, _) = FakeTmux::new();
        let path = write_snap_file(&["s0"]);
        let mut a = App::new(tmux, path, "/bin/true".into(), None, GlyphArg::Fixed(Tier::Plain), 0, 28, 40);
        a.set_now(NOW);
        assert!(a.poll(NOW), "initial load requests a draw");
        let mut draws = 0;
        for i in 1..=40 { // 40 x 25ms ticks = one idle second
            if a.poll(NOW + 25 * i) {
                draws += 1;
            }
        }
        assert_eq!(draws, 0, "idle 25ms ticks must not request redraws");
        std::fs::remove_file(&a.snapshot_path).ok();
    }

    // --- event-loop next_frame_ms deadlines ---------------------------------------
    #[test]
    fn deadlines_idle_spinner_animation_hidden() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::Idle, false, false)], // archived=false, viewed=false
            usage: None, limits: vec![],
        }), NOW);
        a.own = Some("s0".into());
        // hidden (own session not viewed, nothing armed): no frame deadline.
        assert!(!a.viewed(), "s0 not viewed");
        assert_eq!(a.next_frame_ms(), None, "hidden -> no frame deadline");
        // spinner: a viewed working session -> ~100 ms.
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::Working, false, true)],
            usage: None, limits: vec![],
        }), NOW);
        a.own = Some("s0".into());
        a.set_now(NOW);
        assert!(a.viewed(), "s0 viewed");
        assert_eq!(a.next_frame_ms(), Some(NOW + 100), "working + viewed -> ~100 ms spinner");
        // event animation (e.g. a finish flash) -> ~33 ms.
        a.finish_flash.insert("s0".into(), FinishFlash { start_ms: NOW as f64 });
        a.set_now(NOW);
        assert_eq!(a.next_frame_ms(), Some(NOW + 33), "animation active -> ~33 ms");
    }

    // --- needs-you glyph is `!` -------------------------------------------------
    #[test]
    fn needs_you_glyph_is_bang() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.set_now(NOW);
        a.update_from_snapshot(Some(Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::NeedsYou, false, true)],
            usage: None, limits: vec![],
        }), NOW);
        a.seen_viewed = true;
        let g = a.build_grid();
        assert_eq!(g.row(BRAND_H)[3].ch, '!', "NEEDS_YOU glyph is `!`");
    }

    #[test]
    fn usage_kept_and_list_scrolls_when_many_sessions() {
        // 10 sessions in a 26-row pane: the limits stay visible and the list scrolls to keep the own card.
        let (tmux, _) = FakeTmux::new();
        let sessions: Vec<Session> = (0..10).map(|i| sess(&format!("s{i}"), i, State::Idle, false, true)).collect();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions,
            usage: Some(Usage {
                five_hour: Some(Window { used_percentage: 18.0, resets_at: NOW / 1000 + 3600 }),
                seven_day: Some(Window { used_percentage: 30.0, resets_at: NOW / 1000 + 86_400 }),
            }), limits: vec![],
        };
        let mut a = app_with(tmux);
        a.own = Some("s9".into());
        a.on_resize(28, 26);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.seen_viewed = true;
        let g = a.build_grid_at(NOW);
        assert_eq!(a.usage_h, 4, "usage panel kept");
        assert!(a.offset > 0, "list scrolled: offset={} list_h={} cursor={} items={}", a.offset, a.list_h, a.cursor, a.items.len());
        let text: String = (0..g.h).map(|y| g.row(y).iter().map(|c| c.ch).collect::<String>() + "\n").collect();
        assert!(text.contains("s9"), "own card in view:\n{text}");

        // a wheel scroll up moves away from the own card and stays there on the next frames
        let before = a.offset;
        a.scroll(true);
        a.build_grid_at(NOW);
        assert!(a.offset < before, "wheel scroll kept ({} -> {})", before, a.offset);
        // resting again (focus loss, a switch) follows the own card again
        a.rest();
        a.build_grid_at(NOW);
        assert_eq!(a.offset, before);
    }

    // --- short windows: list wins over the usage panel ------------------------------
    #[test]
    fn usage_hidden_when_window_too_short() {
        let (tmux, _) = FakeTmux::new();
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("s0", 0, State::Idle, false, true)],
            usage: Some(Usage {
                five_hour: Some(Window { used_percentage: 43.0, resets_at: NOW / 1000 + 3600 }),
                seven_day: Some(Window { used_percentage: 95.0, resets_at: NOW / 1000 + 86_400 }),
            }), limits: vec![],
        };
        let mut a = app_with(tmux); // h = 40: plenty of room -> usage shown
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap.clone()), NOW);
        a.seen_viewed = true;
        let roomy = a.build_grid_at(NOW);
        assert_eq!(a.usage_h, 4, "on a tall window the usage panel is shown");

        // shrink the window so list + usage no longer fit -> usage yields, list keeps its height.
        a.on_resize(a.w, BRAND_H + 4);
        let short = a.build_grid_at(NOW);
        assert_eq!(a.usage_h, 0, "usage hidden when the window is too short");
        assert_eq!(a.list_h, a.h - BRAND_H, "the freed rows go to the list");
        // the card still renders at the top of the list (row BRAND_H) after the panel is dropped
        let card_row: String = short.row(BRAND_H).iter().map(|c| c.ch).collect();
        assert!(card_row.contains('s'), "list card still drawn: {card_row:?}");
        let _ = roomy;
    }

    // --- narrow rail mode (W < 16) --------------------------------------------------

    fn rail_snap() -> Snapshot {
        Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![
                sess("mine", 0, State::Working, false, true),
                sess("quest", 1, State::NeedsYou, false, true),
                sess("turn", 2, State::YourTurn, false, true),
            ],
            usage: Some(Usage {
                five_hour: Some(Window { used_percentage: 43.0, resets_at: NOW / 1000 + 3600 }),
                seven_day: Some(Window { used_percentage: 95.0, resets_at: NOW / 1000 + 86_400 }),
            }), limits: vec![],
        }
    }

    fn rail_app(h: usize) -> App {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.on_resize(7, h);
        a.set_now(NOW);
        a.update_from_snapshot(Some(rail_snap()), NOW);
        a.seen_viewed = true;
        a.current_fade_start = Some((NOW - 5000) as f64);
        a.fx.attentions.clear(); // assert resting colours; the rail effects are covered separately
        a.fx.pulses.clear();
        a.finish_flash.clear();
        a
    }

    fn row_of(a: &mut App, y: usize) -> Vec<RowCell> {
        a.build_grid_at(NOW).row(y).to_vec()
    }

    /// Rail for W < 16, full layout for W >= 16.
    #[test]
    fn rail_vs_full_by_width_and_live_switch_on_resize() {
        let (tmux, _) = FakeTmux::new();
        let mut a = app_with(tmux);
        a.on_resize(7, 30);
        assert!(a.rail(), "7 wide -> rail");
        let rail = a.build_grid_at(NOW);
        let mark = if a.tier == Tier::Rich { '\u{F0E7}' } else { 'F' };
        assert_eq!(rail.row(0)[1].ch, mark, "rail brand: the bolt (or `F`) at col 1");
        assert_eq!(rail.row(1)[0].ch, '▔', "rail underline row");

        // wide again -> full layout
        a.on_resize(28, 30);
        assert!(!a.rail(), "28 wide -> full");
        let full = a.build_grid_at(NOW);
        assert_ne!(full.row(0)[1].ch, 'c', "full brand is not a lone `c`");
    }

    /// Rail row content / colours for working (spinner), needs-you (`!`) and the current session's white index.
    #[test]
    fn rail_rows_current_needs_working() {
        let mut a = rail_app(30);
        a.own = Some("mine".into());
        // rows: 0 brand, 1 underline, 2..=4 sessions
        let mine = row_of(&mut a, 2);
        assert_eq!(mine[0].ch, '▍', "edge");
        assert_eq!(mine[2].ch, '0' /* index 0 right-aligned in cols1..2 */, "index digit");
        assert_eq!(mine[2].fg, m::NAME_CURRENT, "current index is white");
        // working glyph at col 4
        let working = mine[4].ch;
        assert_ne!(working, ' ', "working has a glyph (spinner)");
        assert_eq!(mine[0].fg, m::ACCENT, "current edge is coral");
        assert_eq!(mine[1].bg, m::CARD_CURRENT, "current bg #363636");

        let quest = row_of(&mut a, 3);
        assert_eq!(quest[2].ch, '1', "needs-you index 1");
        assert_eq!(quest[1].bg, m::CARD_NEEDS, "needs-you bg #3a2b28");
        assert_eq!(quest[4].ch, '!', "needs-you glyph is `!`");
        assert_eq!(quest[4].fg, m::C_NEEDS_YOU, "needs-you glyph coral");
        assert_eq!(quest[0].fg, m::C_NEEDS_YOU, "needs-you edge coral");

        let turn = row_of(&mut a, 4);
        assert_eq!(turn[4].ch, '●', "your-turn glyph");
        assert_eq!(turn[1].bg, m::CARD_IDLE, "other bg #2c2c2c");
    }

    /// Hit-testing of rail session rows and the `a<count>` / `+` action rows.
    #[test]
    fn rail_hit_test_rows_and_actions() {
        let mut a = rail_app(30);
        a.set_now(NOW);
        a.build_grid_at(NOW);
        // rows: 2,3,4 = sessions mine/quest/turn; 5 blank; 6 = a0; 7 = +
        assert_eq!(a.hit_test(2, 2).map(|t| t.str()), Some("mine"));
        assert_eq!(a.hit_test(2, 4).map(|t| t.str()), Some("turn"));
        // no archived sessions -> a0
        assert_eq!(a.hit_test(2, 6).map(|t| t.str()), Some(ARCHIVED_TARGET));
        assert_eq!(a.hit_test(2, 7).map(|t| t.str()), Some(NEW_TARGET));
        // brand rows not clickable; blank spacer not clickable
        assert_eq!(a.hit_test(2, 0), None);
        assert_eq!(a.hit_test(2, 1), None);
        assert_eq!(a.hit_test(2, 5), None);
    }

    /// Rail usage strip: labels `5h`/`wk` and a percentage coloured by the threshold, plus `–` when expired.
    #[test]
    fn rail_usage_rows_text_and_colours() {
        let mut a = rail_app(30);
        a.build_grid_at(NOW);
        // h=30 -> usage at rows 27 (5h) and 28 (wk); 29 blank
        let row5 = a.build_grid_at(NOW).row(27).to_vec();
        let text5: String = row5.iter().map(|c| c.ch).take(7).collect();
        assert_eq!(text5, "5h  43%", "5h row: label + right-aligned pct");
        assert_eq!(row5[0].fg, m::USAGE_LABEL, "label #808080");
        assert_eq!(row5[4].fg, m::THRESH_LO, "43% -> low threshold");
        let row7 = a.build_grid_at(NOW).row(28).to_vec();
        let text7: String = row7.iter().map(|c| c.ch).take(7).collect();
        assert_eq!(text7, "wk  95%", "wk row");
        assert_eq!(row7[4].fg, m::THRESH_HI, "95% -> high threshold");
        assert!(a.rail_usage_show, "usage shown on a 30-row rail");
    }

    /// When the rail window is too short, usage is dropped (the fixed rows win).
    #[test]
    fn rail_usage_hidden_when_too_short() {
        let mut a = rail_app(9); // brand2 + 3 sessions + blank + a + + = 8, + usage3 = 11 > 9
        a.build_grid_at(NOW);
        assert!(!a.rail_usage_show, "usage dropped on a short rail");
    }

    // --- many sessions: the actions stay pinned, "N more" rows show what is hidden ------
    fn chuck_app(state: State, own: &str) -> App {
        let (tmux, _) = FakeTmux::new();
        let mut chuck = sess("chuck", 2, state, false, false);
        chuck.support = true;
        let sessions = vec![sess("s0", 0, State::Idle, false, true), sess("s1", 1, State::Idle, false, true), chuck];
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions, usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.on_resize(28, 40);
        a.set_now(NOW);
        a.own = Some(own.into());
        a.update_from_snapshot(Some(snap), NOW);
        a.seen_viewed = true;
        a
    }

    fn chuck_row(a: &App, g: &Grid) -> usize {
        (0..g.h).find(|&y| row_text(g, y).contains("ask chuck")).expect("Chuck's row")
    }

    #[test]
    fn chuck_is_not_a_card_but_his_row_shows_him() {
        let mut a = chuck_app(State::Working, "s0");
        let g = a.build_grid_at(NOW);
        assert!(!(0..g.h).any(|y| a.hit_test(4, y).map(|t| t.str() == "chuck").unwrap_or(false)), "no card for Chuck");
        assert!(!(0..g.h).any(|y| row_text(&g, y).contains(" chuck ") && !row_text(&g, y).contains("ask")));
        let y = chuck_row(&a, &g);
        let label = row_text(&g, y).find("ask").unwrap();
        let col = row_text(&g, y)[..label].chars().count();
        assert_eq!(g.row(y)[col].fg, m::C_WORKING, "working while you are elsewhere");
        assert_eq!(g.row(y)[1].fg, m::PANEL_RULE);
    }

    #[test]
    fn chuck_row_is_lit_while_you_are_with_him_and_no_card_is_current() {
        let mut a = chuck_app(State::Idle, "chuck");
        let g = a.build_grid_at(NOW);
        let y = chuck_row(&a, &g);
        let label = row_text(&g, y).find("ask").unwrap();
        let col = row_text(&g, y)[..label].chars().count();
        assert_eq!(g.row(y)[col].fg, m::NAME_CURRENT);
        assert!(g.row(y)[col].bold);
        assert_eq!(g.row(y)[1].fg, m::ACCENT, "the rule turns coral");
        assert!(!a.items.iter().any(|i| i.is_session && i.current), "no selected card");
        // elsewhere and idle: the plain grey label
        let mut b = chuck_app(State::Idle, "s1");
        let g = b.build_grid_at(NOW);
        let y = chuck_row(&b, &g);
        let label = row_text(&g, y).find("ask").unwrap();
        let col = row_text(&g, y)[..label].chars().count();
        assert_eq!(g.row(y)[col].fg, m::GREY);
        assert_eq!(g.row(y)[1].fg, m::PANEL_RULE);
    }

    #[test]
    fn chuck_over_a_remotes_list_lights_his_row() {
        // a remote's list has no Chuck; @force_overlay says he is in the main pane (SPEC §50)
        let (tmux, _) = FakeTmux::new();
        let sessions = vec![sess("r0", 0, State::Idle, false, true)];
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions, usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.on_resize(28, 40);
        a.set_now(NOW);
        a.own = Some("chuck".into());
        a.update_from_snapshot(Some(snap), NOW);
        a.overlay = true;
        let g = a.build_grid_at(NOW);
        let y = chuck_row(&a, &g);
        assert_eq!(g.row(y)[1].fg, m::ACCENT);
        assert!(!a.items.iter().any(|i| i.is_session && i.current));
        a.overlay = false;
        let g = a.build_grid_at(NOW);
        assert_eq!(g.row(chuck_row(&a, &g))[1].fg, m::PANEL_RULE);
    }

    #[test]
    fn chuck_needing_you_turns_his_row_red() {
        let mut a = chuck_app(State::NeedsYou, "s0");
        let g = a.build_grid_at(NOW);
        let y = chuck_row(&a, &g);
        let label = row_text(&g, y).find("ask").unwrap();
        let col = row_text(&g, y)[..label].chars().count();
        assert_eq!(g.row(y)[col].fg, m::C_NEEDS_YOU);
    }

    fn many_app(n: i32, h: usize, needs: Option<i32>) -> App {
        let (tmux, _) = FakeTmux::new();
        let sessions: Vec<Session> = (0..n)
            .map(|i| sess(&format!("s{i}"), i, if Some(i) == needs { State::NeedsYou } else { State::Idle }, false, true))
            .collect();
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: NOW, latest_version: None, sessions, usage: None, limits: vec![] };
        let mut a = app_with(tmux);
        a.on_resize(28, h);
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.seen_viewed = true;
        a
    }

    fn row_text(g: &Grid, y: usize) -> String {
        g.row(y).iter().map(|c| c.ch).collect()
    }

    #[test]
    fn actions_pinned_when_the_list_overflows() {
        // 10 two-row cards (20 rows) in a 15-row list: new + archived and Chuck's row stay at the bottom,
        // "↓ N more" above
        let mut a = many_app(10, BRAND_H + 15, None);
        let g = a.build_grid_at(NOW);
        assert_eq!(a.list_h, 15);
        assert!(a.pinned);
        assert_eq!(a.actions_rows, 3);
        let chuck = BRAND_H + a.list_h - 1;
        // his label centred in a rule like new/archived's, over columns 1..w
        let d = a.w - 1 - 13;
        assert_eq!(row_text(&g, chuck), format!(" {} ? ask chuck {}", "─".repeat(d / 2), "─".repeat(d - d / 2)));
        let face = 1 + d / 2 + 1;
        assert_eq!(g.row(chuck)[face].fg, m::ACCENT);
        assert_eq!(g.row(chuck)[1].fg, m::PANEL_RULE);
        assert_eq!(a.hit_test(4, chuck).map(|t| t.str()), Some("__chuck__"));
        assert!(row_text(&g, chuck - 1).trim().is_empty(), "a blank row above Chuck's");
        let last = chuck - 2;
        let t = row_text(&g, last);
        // new and archived set into one rule, like the header's labels, over columns 1..w
        assert_eq!(t, format!(" ─ + new {} archived (0) ─", "─".repeat(a.w - 1 - 8 - 15)), "{t:?}");
        assert_eq!(g.row(last)[1].fg, m::PANEL_RULE);
        assert_eq!(g.row(last)[4].fg, m::GREY);
        // viewport 11 rows: s0..s4 whole, s5 cut, s5..s9 hidden (5)
        assert!(row_text(&g, last - 1).contains("↓ 5 more"), "{:?}", row_text(&g, last - 1));
        assert!(!row_text(&g, BRAND_H).contains("more"), "nothing hidden above at the top");
        assert_eq!(a.hit_test(4, last).map(|t| t.str()), Some("__new__"));
        assert_eq!(a.hit_test(a.w - 3, last).map(|t| t.str()), Some("__archived__"));
        assert!(a.hit_test(2, last - 1).is_none(), "the more row is not an item");
        assert_eq!(a.hit_test(2, BRAND_H).map(|t| t.str()), Some("s0"));
    }

    #[test]
    fn list_that_fits_has_a_blank_row_then_the_actions() {
        let mut a = many_app(3, 40, None);
        let g = a.build_grid_at(NOW);
        assert!(!a.pinned);
        assert_eq!(a.offset, 0);
        assert!(row_text(&g, BRAND_H + 6).trim().is_empty(), "blank row above the actions");
        assert!(a.hit_test(2, BRAND_H + 6).is_none());
        let t = row_text(&g, BRAND_H + 7);
        assert!(t.contains("+ new") && t.contains("archived (0)"), "{t:?}");
        assert!(!(0..g.h).any(|y| row_text(&g, y).contains("more")));
    }

    #[test]
    fn scrolling_shows_more_above_and_reaches_the_last_card() {
        let mut a = many_app(10, BRAND_H + 15, None);
        a.build_grid_at(NOW);
        for _ in 0..20 {
            a.on_mouse(crossterm::event::MouseEventKind::ScrollDown, 2, 0);
            a.build_grid_at(NOW);
        }
        let g = a.build_grid_at(NOW);
        let last = BRAND_H + a.list_h - 3;     // the new/archived rule; a blank row and Chuck's under it
        assert!(row_text(&g, BRAND_H).contains("↑ 5 more"), "{:?} off={} view={} total={}", row_text(&g, BRAND_H), a.offset, a.view_h, a.sess_total);
        assert!(row_text(&g, last - 1).trim().is_empty(), "nothing below: {:?}", row_text(&g, last - 1));
        // the last card ends right above the more row
        assert_eq!(a.hit_test(2, last - 2).map(|t| t.str()), Some("s9"));
        assert_eq!(a.hit_test(2, BRAND_H + 1).map(|t| t.str()), Some("s5"));
        assert!(a.hit_test(2, BRAND_H).is_none(), "the more row is not an item");
        assert!(row_text(&g, last).contains("+ new"));
    }

    #[test]
    fn clicking_more_pages_the_cards() {
        let mut a = many_app(10, BRAND_H + 15, None);
        a.build_grid_at(NOW);
        let down = BRAND_H + a.list_h - a.actions_h();
        a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), 2, down as u16);
        a.build_grid_at(NOW);
        assert_eq!(a.offset, 10, "paged to the end (viewport 11 - 1 = 10, clamped)");
        assert!(!a.follow_own);
        // back up a page at a time: 10 -> 1 -> 0
        for want in [1, 0] {
            a.on_mouse(crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left), 2, BRAND_H as u16);
            a.build_grid_at(NOW);
            assert_eq!(a.offset, want);
        }
    }

    #[test]
    fn hidden_card_that_needs_you_colours_the_more_row() {
        let mut a = many_app(10, BRAND_H + 15, Some(8));
        let g = a.build_grid_at(NOW);
        let y = BRAND_H + a.list_h - a.actions_h();
        let x = row_text(&g, y).chars().position(|ch| ch == '↓').unwrap();
        assert_eq!(g.row(y)[x].fg, m::C_NEEDS_YOU);
        assert!(g.row(y)[x].bold);
        let mut b = many_app(10, BRAND_H + 15, None);
        let g = b.build_grid_at(NOW);
        assert_eq!(g.row(y)[x].fg, m::GREY);
    }

    #[test]
    fn own_card_kept_in_view_between_the_more_rows() {
        let mut a = many_app(10, BRAND_H + 15, None);
        a.own = Some("s8".into());
        let g = a.build_grid_at(NOW);
        assert!(a.offset > 0);
        let rows: Vec<String> = (BRAND_H + 1..BRAND_H + 1 + a.view_h).map(|y| row_text(&g, y)).collect();
        assert!(rows.iter().any(|r| r.contains("s8")), "{rows:?}");
        assert!(row_text(&g, BRAND_H).contains("more"));
    }

    #[test]
    fn card_rows_two_rows_for_every_card() {
        // the model / mode line moved to the header: every card is 2 rows, the own one too
        let (tmux, _) = FakeTmux::new();
        let mut odd = sess("odd", 2, State::Idle, false, true);
        odd.model = Some("sonnet 5.5".into());
        let snap = Snapshot { update: None, smoke_failed: None,
            generated_ms: NOW,
            latest_version: None,
            sessions: vec![sess("own", 0, State::Idle, false, true), sess("s1", 1, State::Idle, false, true), odd, sess("sh", 3, State::Shell, false, true)],
            usage: None, limits: vec![],
        };
        let mut a = app_with(tmux);
        a.own = Some("own".into());
        a.set_now(NOW);
        a.update_from_snapshot(Some(snap), NOW);
        a.build_grid_at(NOW);
        fn h(a: &App, n: &str) -> Option<usize> {
            a.items.iter().find(|i| i.target.str() == n).map(|i| i.height)
        }
        for n in ["own", "s1", "odd", "sh"] {
            assert_eq!(h(&a, n), Some(2), "{n}");
        }
    }

    #[test]
    fn narrow_pane_stacks_the_actions() {
        let mut a = many_app(2, 40, None);
        a.on_resize(18, 40);
        let g = a.build_grid_at(NOW);
        assert_eq!(a.actions_rows, 4);
        assert!(row_text(&g, BRAND_H + 5).contains("archived"));
        assert!(row_text(&g, BRAND_H + 6).contains("+ new"));
        assert!(row_text(&g, BRAND_H + 7).trim().is_empty());
        assert!(row_text(&g, BRAND_H + 8).contains("ask chuck"));
        assert_eq!(a.hit_test(2, BRAND_H + 5).map(|t| t.str()), Some("__archived__"));
        assert_eq!(a.hit_test(2, BRAND_H + 6).map(|t| t.str()), Some("__new__"));
        assert_eq!(a.hit_test(2, BRAND_H + 8).map(|t| t.str()), Some("__chuck__"));
    }

    #[test]
    fn tiny_list_keeps_the_actions_in_the_scroll() {
        let mut a = many_app(10, BRAND_H + PIN_MIN_LIST - 1, None);
        a.build_grid_at(NOW);
        assert!(!a.pinned);
        assert_eq!(a.view_h, a.list_h);
    }

    #[test]
    fn failed_self_test_shows_its_reason_in_the_footer() {
        let mut a = many_app(2, 40, None);
        let g = a.build_grid_at(NOW);
        let h = a.h;
        let bottom = |g: &Grid| (h - 2..h).map(|y| row_text(g, y)).collect::<Vec<_>>().join("\n");
        assert!(!bottom(&g).contains('×'));
        let mut snap = a.snapshot.clone().unwrap();
        snap.smoke_failed = Some("Claude Code logged out".into());
        a.update_from_snapshot(Some(snap.clone()), NOW);
        let g = a.build_grid_at(NOW);
        assert!(bottom(&g).contains("× Claude Code logged out"), "{:?}", bottom(&g));
        snap.smoke_failed = Some("self-test failed".into());
        a.update_from_snapshot(Some(snap), NOW);
        let g = a.build_grid_at(NOW);
        assert!(bottom(&g).contains("× self-test failed"), "{:?}", bottom(&g));
    }
}
