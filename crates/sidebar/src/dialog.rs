//! The `force-dialog` binary's library: popup dialogs (new / rename / move / archive / delete / archived / jump)
//! rendered into a ratatui `Buffer`. Behavioural reference: force/dialogs.py. All state mutation goes through
//! the `force` CLI (`<force-bin> <args…>`, no shell) so force owns locking; tmux calls use
//! `tmux -L <socket> …`. The dialogs are fully testable against a fake `Runner` that records argv.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use ratatui::buffer::Buffer;
use ratatui::style::{Color, Modifier, Style};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::glyphs::Tier;
use crate::snapshot::{self, State};

// --------------------------------------------------------------------------- palette ----------
pub const BG: Color = Color::Rgb(0x21, 0x21, 0x21); // #212121 the shell colour (popups stand off the agent panes)
pub const BG_RAISED: Color = Color::Rgb(0x2a, 0x2a, 0x2a); // #2a2a2a
pub const BG_SELECTED: Color = Color::Rgb(0x36, 0x36, 0x36); // #363636
pub const FG: Color = Color::Rgb(0xc6, 0xc6, 0xc6); // #c6c6c6
pub const FG_DIM: Color = Color::Rgb(0x80, 0x80, 0x80); // #808080
pub const FG_FAINT: Color = Color::Rgb(0x5f, 0x5f, 0x5f); // #5f5f5f
pub const ACCENT: Color = Color::Rgb(0xd9, 0x77, 0x57); // #d97757 coral
pub const ERROR: Color = Color::Rgb(0xd7, 0x5f, 0x5f);
pub const SUCCESS: Color = Color::Rgb(0x87, 0xaf, 0x87);
pub const WARNING: Color = Color::Rgb(0xd7, 0xaf, 0x5f);
pub const WHITE: Color = Color::Rgb(0xff, 0xff, 0xff);
pub const RULE: Color = Color::Rgb(0x3c, 0x3c, 0x3c); // #3c3c3c the cards' rule under the title
pub const BG_INK: Color = Color::Rgb(0x26, 0x26, 0x26); // #262626 text on the coral button
pub const C_NEEDS_YOU: Color = Color::Rgb(0xe8, 0x80, 0x6f);
pub const C_YOUR_TURN: Color = Color::Rgb(0x87, 0xc0, 0x8f);
pub const C_DOWN: Color = Color::Rgb(0xd7, 0x5f, 0x5f);

const CONTENT_LEFT: usize = 2;
/// Row layout below the title (row 1): a blank row, then the content.
const ARCH_LIST_TOP: usize = 3;
const JUMP_FILTER_Y: usize = 3;
const JUMP_LIST_TOP: usize = 5;
const SPACER: usize = 3;
const NAME_RE_MAX: usize = 31;

// --------------------------------------------------------------------------- registry --------

/// One read-only registry entry (force/registry.py). The dialog never writes the registry itself.
#[derive(Clone, Debug, PartialEq)]
pub struct RegistryEntry {
    pub name: String,
    pub cwd: String,
    pub archived: bool,
    pub archived_at: Option<String>,
    pub pending_name: Option<String>,
    pub kind: String,
}

/// Parse the registry JSON `{"sessions":[{name, cwd, archived, archived_at, pending_name, …}]}`.
/// Missing/bad file or entries -> empty list; the dialog reads it read-only.
pub fn load_registry(path: &Path) -> Vec<RegistryEntry> {
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let value: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let sessions = match value.get("sessions").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for s in sessions {
        let obj = match s.as_object() {
            Some(o) => o,
            None => continue,
        };
        let name = match obj.get("name").and_then(|v| v.as_str()) {
            Some(n) => n,
            None => continue,
        };
        out.push(RegistryEntry {
            name: name.to_string(),
            cwd: obj.get("cwd").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            archived: obj.get("archived").and_then(|v| v.as_bool()).unwrap_or(false),
            archived_at: obj.get("archived_at").and_then(|v| v.as_str()).map(str::to_string),
            pending_name: obj.get("pending_name").and_then(|v| v.as_str()).map(str::to_string),
            kind: obj.get("kind").and_then(|v| v.as_str()).unwrap_or("claude").to_string(),
        });
    }
    out
}

/// `^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$` and at most 31 chars (force/registry.py `valid_name`).
pub fn valid_name(name: &str) -> bool {
    if name.is_empty() || name.chars().count() > NAME_RE_MAX {
        return false;
    }
    let bytes = name.as_bytes();
    if !(bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit()) {
        return false;
    }
    if !(bytes[bytes.len() - 1].is_ascii_lowercase() || bytes[bytes.len() - 1].is_ascii_digit()) {
        return false;
    }
    bytes
        .iter()
        .all(|&b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// True if any entry (or its pending rename) has this name (force/registry.py `name_taken`).
pub fn name_taken(entries: &[RegistryEntry], name: &str) -> bool {
    entries
        .iter()
        .any(|e| e.name == name || e.pending_name.as_deref() == Some(name))
}

/// Fold `~` / `~/…` to the user's home directory (expands both forms).
pub fn expand_user(path: &str, home: &str) -> String {
    if path == "~" {
        return home.to_string();
    }
    if let Some(rest) = path.strip_prefix("~/") {
        return format!("{}/{}", home, rest);
    }
    path.to_string()
}

/// Shorten a cwd for display: home -> `~`, `~/x` -> `~/x`, else as-is.
pub fn display_cwd(cwd: &str, home: &str) -> String {
    if cwd == home {
        return "~".to_string();
    }
    if let Some(rest) = cwd.strip_prefix(&format!("{}/", home)) {
        return format!("~/{}", rest);
    }
    cwd.to_string()
}

/// Archived entries, newest `archived_at` first (force/registry.py `archived`).
pub fn archived_sorted(entries: &[RegistryEntry]) -> Vec<RegistryEntry> {
    let mut v: Vec<RegistryEntry> = entries.iter().filter(|e| e.archived).cloned().collect();
    v.sort_by(|a, b| {
        b.archived_at
            .as_deref()
            .unwrap_or("")
            .cmp(a.archived_at.as_deref().unwrap_or(""))
    });
    v
}

// --------------------------------------------------------------------------- runner ----------

/// Runs the force CLI and tmux. The real one shells out to `<force-bin>` / `tmux -L <socket>`; tests fake
/// it to record argv and return canned output. `force` returns the stdout or the *last line* of stderr on error.
pub trait Runner {
    /// Run `<force-bin> <args…>`; Err carries the last stderr line (or a fallback) on non-zero exit.
    fn cockpit(&self, args: &[String]) -> Result<String, String>;
    /// Run `tmux -L <socket> <args…>`; Err carries stderr on non-zero exit.
    fn tmux(&self, args: &[String]) -> Result<String, String>;
    /// Run `tmux -L <socket> <args…>` and return whether it succeeded (used for `has-session`).
    fn tmux_ok(&self, args: &[String]) -> bool;
}

/// Real runner shelling out to the force binary and the tmux CLI on a named socket.
pub struct RealRunner {
    cockpit_bin: String,
    socket: String,
    /// a remote host's tmux (SPEC §40): `ssh … HOST tmux -L force` words instead of `tmux -L <socket>`
    host: Option<Vec<String>>,
}

impl RealRunner {
    pub fn new(cockpit_bin: String, socket: String) -> Self {
        Self { cockpit_bin, socket, host: None }
    }
    /// Run tmux through this whitespace-separated prefix (FORCE_HOST_TMUX) when it is non-empty.
    pub fn with_host_prefix(mut self, prefix: Option<&str>) -> Self {
        let words: Vec<String> = prefix.unwrap_or("").split_whitespace().map(str::to_string).collect();
        self.host = if words.is_empty() { None } else { Some(words) };
        self
    }
    fn tmux_argv(&self, args: &[String]) -> (String, Vec<String>) {
        match &self.host {
            Some(p) => {
                let mut argv: Vec<String> = p[1..].to_vec();
                argv.extend(crate::tmux::remote_args(p, args));
                (p[0].clone(), argv)
            }
            None => {
                let mut argv = vec!["-L".to_string(), self.socket.clone()];
                argv.extend(args.iter().cloned());
                ("tmux".to_string(), argv)
            }
        }
    }
    fn exec(bin: &str, args: &[String]) -> Result<String, String> {
        let out = Command::new(bin)
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            let last = String::from_utf8_lossy(&out.stderr)
                .lines()
                .rev()
                .find(|l| !l.trim().is_empty())
                .unwrap_or("")
                .trim()
                .to_string();
            return Err(if last.is_empty() { "command failed".to_string() } else { last });
        }
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }
}

impl Runner for RealRunner {
    fn cockpit(&self, args: &[String]) -> Result<String, String> {
        Self::exec(&self.cockpit_bin, args)
    }
    fn tmux(&self, args: &[String]) -> Result<String, String> {
        let (bin, argv) = self.tmux_argv(args);
        Self::exec(&bin, &argv)
    }
    fn tmux_ok(&self, args: &[String]) -> bool {
        let (bin, argv) = self.tmux_argv(args);
        match Command::new(&bin).args(&argv).stdin(Stdio::null()).output() {
            Ok(o) => o.status.success(),
            Err(_) => false,
        }
    }
}

// --------------------------------------------------------------------------- status ----------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum StatusKind {
    Neutral,
    Error,
    Ok,
    Warning,
}

// --------------------------------------------------------------------------- dialogs --------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    New,
    Rename,
    Move,
    Archive,
    Delete,
    Archived,
    Jump,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum NewFocus {
    Kind,
    Name,
    Folder,
    /// The Remote Control checkbox (Claude Code only, SPEC §53).
    Remote,
}

/// The New dialog's Remote Control checkbox row (SPEC §53).
const NEW_REMOTE_Y: usize = 12;

/// A jump session: state from the snapshot (or IDLE from the registry fallback) plus its index for ordering.
#[derive(Clone, Debug)]
pub struct JumpSession {
    pub name: String,
    pub state: State,
    pub index: Option<i32>,
}

pub struct Dialog {
    pub kind: Kind,
    pub name: Option<String>,
    registry_path: PathBuf,
    snapshot_path: PathBuf,
    runner: Box<dyn Runner>,
    pub w: usize,
    pub h: usize,
    pub now_ms: i64,
    home: String,

    pub status_kind: StatusKind,
    pub status_text: String,
    pub done: bool,
    pub result: i32,
    pub exit_after_ms: Option<i64>,

    // registry (reloaded for the archived list after a forget)
    pub entries: Vec<RegistryEntry>,

    // new
    /// Session kinds offered on the New dialog (`<force-bin> kinds --json`: claude / opencode only when
    /// installed, always shell; SPEC §36) and the selected one.
    pub new_kinds: Vec<String>,
    pub new_kind: usize,
    pub name_input: String,
    pub name_cursor: usize,
    pub cwd_input: String,
    pub cwd_cursor: usize,
    pub new_focus: NewFocus,
    /// Start a Claude Code session with Remote Control (the checkbox, checked by default; SPEC §53).
    pub new_remote: bool,
    /// This machine's `remoteControl` setting (SPEC §53): the box's start value.
    pub new_remote_default: bool,

    // rename
    pub rename_input: String,
    pub rename_cursor: usize,

    // move
    pub move_input: String,
    pub move_cursor: usize,

    // archive / delete
    pub forced: bool,
    pub busy: bool,
    pub warn_kind: StatusKind,
    pub warn_text: String,

    // archived
    pub arch_cursor: usize,
    pub pending_forget: Option<String>,

    // jump
    pub jump_filter: String,
    pub jump_cursor: usize,
    pub jump_sel: usize,
    pub jump_ordered: Vec<JumpSession>,
    pub jump_shown: Vec<JumpSession>,
    pub own_session: Option<String>,
    pub glyphs: bool,
    pub tier: Tier,

    // split (ui) mode env injected from the display-popup's -e pairs (empty in classic mode).
    force_client: Option<String>,
    remote_host: bool,
    force_session: Option<String>,
}

/// The split-mode env injected into the dialog through a seam (classic mode == `NO_ENV`): `client` is the
/// host nested client (every `switch-client` gets `-c`), `session` is the host-own session (the jump dialog's
/// own session, instead of `display -p` / `list-clients`), `glyphs` overrides the auto tier read.
#[derive(Clone, Copy, Debug, Default)]
pub struct ForceEnv<'a> {
    pub client: Option<&'a str>,
    pub session: Option<&'a str>,
    pub glyphs: Option<&'a str>,
    /// the remote host shown (FORCE_HOST, SPEC §40): its folders are not on this machine, so they are not
    /// checked here (the remote refuses a missing one)
    pub host: Option<&'a str>,
}

fn home_dir() -> String {
    std::env::var("HOME").unwrap_or_else(|_| "/".to_string())
}

impl Dialog {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        kind: Kind,
        name: Option<String>,
        registry: &Path,
        snapshot: &Path,
        runner: Box<dyn Runner>,
        tier: Tier,
        w: usize,
        h: usize,
    ) -> Dialog {
        Self::new_with_env(kind, name, registry, snapshot, runner, &ForceEnv::default(), tier, w, h)
    }

    /// Constructor with split-mode force-env (client / session / glyphs). `new` is classic mode (no env).
    pub fn new_with_env(
        kind: Kind,
        name: Option<String>,
        registry: &Path,
        snapshot: &Path,
        runner: Box<dyn Runner>,
        force: &ForceEnv<'_>,
        tier: Tier,
        w: usize,
        h: usize,
    ) -> Dialog {
        let home = home_dir();
        let default_cwd = expand_user("~", &home);
        let entries = load_registry(registry);
        let mut d = Dialog {
            kind,
            name,
            registry_path: registry.to_path_buf(),
            snapshot_path: snapshot.to_path_buf(),
            runner,
            w,
            h,
            now_ms: 0,
            home,
            status_kind: StatusKind::Neutral,
            status_text: String::new(),
            done: false,
            result: 0,
            exit_after_ms: None,
            entries,
            new_kinds: Vec::new(),
            new_kind: 0,
            name_input: String::new(),
            name_cursor: 0,
            cwd_cursor: default_cwd.chars().count(),
            cwd_input: default_cwd,
            new_focus: NewFocus::Name,
            new_remote: true,
            new_remote_default: true,
            rename_input: String::new(),
            rename_cursor: 0,
            move_input: String::new(),
            move_cursor: 0,
            forced: false,
            busy: false,
            warn_kind: StatusKind::Neutral,
            warn_text: String::new(),
            arch_cursor: 0,
            pending_forget: None,
            jump_filter: String::new(),
            jump_cursor: 0,
            jump_sel: 0,
            jump_ordered: Vec::new(),
            jump_shown: Vec::new(),
            own_session: None,
            glyphs: true,
            tier,
            force_client: force.client.map(str::to_string),
            remote_host: force.host.is_some_and(|h| !h.is_empty()),
            force_session: force.session.map(str::to_string),
        };
        match kind {
            Kind::New => {
                d.new_kinds = parse_kinds(d.runner.cockpit(&["kinds".to_string(), "--json".to_string()]).ok());
                d.new_remote_default = rc_default(d.runner.cockpit(&["settings".to_string(), "--json".to_string()]).ok());
                d.new_remote = d.new_remote_default;
                d.new_focus = NewFocus::Name; // type the name right away (as the Python dialog)
                d.validate_new();
            }
            Kind::Rename => {
                let old = d.name.clone().unwrap_or_default();
                d.rename_input.clone_from(&old);
                d.rename_cursor = old.chars().count();
                d.validate_rename();
            }
            Kind::Move => {
                let cwd = d.current_cwd();
                let disp = if cwd.is_empty() { String::new() } else { display_cwd(&cwd, &d.home) };
                d.move_input = disp;
                d.move_cursor = d.move_input.chars().count();
                d.validate_move();
            }
            Kind::Archive | Kind::Delete => d.busy = d.target_busy(),
            Kind::Archived => {}
            Kind::Jump => {
                d.detect_own_session();
                d.load_sessions();
                d.rebuild_jump();
            }
        }
        d
    }

    fn set_status(&mut self, kind: StatusKind, text: impl Into<String>) {
        self.status_kind = kind;
        self.status_text = text.into();
    }

    /// Whether NAME is busy (WORKING or NEEDS_YOU) in the snapshot.
    fn target_busy(&self) -> bool {
        let snap = snapshot::load(&self.snapshot_path);
        let snap = match snap {
            Some(s) => s,
            None => return false,
        };
        let name = self.name.clone().unwrap_or_default();
        snap.sessions
            .iter()
            .find(|s| s.name == name)
            .map(|s| s.state == State::Working || s.state == State::NeedsYou)
            .unwrap_or(false)
    }

    // ---------------------------------------------------------------- validation --------

    /// New-dialog validation message for the name field (None == valid).
    pub fn name_message(&self, name: &str) -> Option<String> {
        if !valid_name(name) {
            return Some("use lowercase letters, digits and dashes (max 31)".to_string());
        }
        if name_taken(&self.entries, name) {
            return Some(format!("'{}' is already taken", name));
        }
        None
    }

    fn new_folder(&self) -> String {
        if self.cwd_input.trim().is_empty() {
            expand_user("~", &self.home)
        } else {
            expand_user(self.cwd_input.trim(), &self.home)
        }
    }

    /// The kind the New dialog has selected ("claude" when none is listed).
    fn new_kind_name(&self) -> &str {
        self.new_kinds.get(self.new_kind).map(String::as_str).unwrap_or("claude")
    }

    /// The Remote Control checkbox is offered (only for a Claude Code session).
    fn remote_offered(&self) -> bool {
        self.new_kind_name() == "claude"
    }

    pub fn validate_new(&mut self) {
        let name = self.name_input.trim().to_string();
        if name.is_empty() {
            // no name: Force names it (SPEC §49.6)
            if !self.remote_host && !std::path::Path::new(&self.new_folder()).is_dir() {
                self.set_status(StatusKind::Error, "folder does not exist");
                return;
            }
            let how = if self.new_kind_name() == "shell" { "ready: named after its folder" } else { "ready: named after your first message" };
            self.set_status(StatusKind::Ok, how);
            return;
        }
        if let Some(msg) = self.name_message(&name) {
            self.set_status(StatusKind::Error, msg);
            return;
        }
        if !self.remote_host && !std::path::Path::new(&self.new_folder()).is_dir() {
            self.set_status(StatusKind::Error, "folder does not exist");
            return;
        }
        self.set_status(StatusKind::Ok, "ready");
    }

    /// Rename validation: None == valid; Some("same name") → the neutral "type a new name".
    pub fn validation_message(&self) -> Option<String> {
        let new = self.rename_input.trim();
        let old = self.name.clone().unwrap_or_default();
        if new == old {
            return Some("same name".to_string());
        }
        if !valid_name(new) {
            return Some("use lowercase letters, digits and dashes (max 31)".to_string());
        }
        let taken = self.entries.iter().any(|e| {
            (e.name != old) && (e.name == new || e.pending_name.as_deref() == Some(new))
        });
        if taken {
            return Some(format!("'{}' is already taken", new));
        }
        None
    }

    fn validate_rename(&mut self) {
        match self.validation_message().as_deref() {
            Some("same name") => self.set_status(StatusKind::Neutral, "type a new name"),
            Some(msg) => self.set_status(StatusKind::Error, msg),
            None => self.set_status(StatusKind::Ok, "ready"),
        }
    }

    // ---------------------------------------------------------------- move --------

    /// The session's current working directory, expanded to its absolute path ("" when the session is unknown).
    fn current_cwd(&self) -> String {
        let name = self.name.clone().unwrap_or_default();
        self.entries
            .iter()
            .find(|e| e.name == name)
            .map(|e| expand_user(&e.cwd, &self.home))
            .unwrap_or_default()
    }

    /// The session's registry kind ("" when the session is unknown).
    fn entry_kind(&self) -> String {
        let name = self.name.clone().unwrap_or_default();
        self.entries
            .iter()
            .find(|e| e.name == name)
            .map(|e| e.kind.clone())
            .unwrap_or_default()
    }

    /// Move validation: None == ready to move; Some carries the (status kind, message) to show.
    fn move_message(&self) -> Option<(StatusKind, &'static str)> {
        let input = self.move_input.trim();
        if input.is_empty() {
            return Some((StatusKind::Neutral, "type a folder"));
        }
        let expanded = self.move_expanded(input);
        let cur = self.current_cwd();
        if strip_trailing_slash(&expanded) == strip_trailing_slash(&cur) {
            return Some((StatusKind::Neutral, "type a new folder"));
        }
        if !self.remote_host && !std::path::Path::new(&expanded).is_dir() {
            return Some((StatusKind::Error, "no such folder"));
        }
        None
    }

    fn validate_move(&mut self) {
        match self.move_message() {
            Some((kind, msg)) => self.set_status(kind, msg),
            None => self.set_status(StatusKind::Neutral, ""), // no status when valid
        }
    }

    /// The expanded absolute move target (no trailing '/'; "" when the field is empty).
    fn move_target(&self) -> String {
        let input = self.move_input.trim();
        if input.is_empty() {
            String::new()
        } else {
            strip_trailing_slash(&self.move_expanded(input)).to_string()
        }
    }

    /// The typed folder with `~` expanded; a relative path is taken from the session's current folder.
    fn move_expanded(&self, input: &str) -> String {
        let e = expand_user(input, &self.home);
        if e.starts_with('/') {
            return e;
        }
        let cur = self.current_cwd();
        format!("{}/{}", strip_trailing_slash(&cur).trim_end_matches('/'), e)
    }

    fn action_submit_move(&mut self) {
        if self.done {
            return;
        }
        if self.move_message().is_some() {
            self.validate_move();
            return;
        }
        let name = self.name.clone().unwrap_or_default();
        match self.runner.cockpit(&["move".to_string(), name, self.move_target()]) {
            Err(e) => self.set_status(StatusKind::Error, e),
            Ok(_) => self.finish(0),
        }
    }

    fn key_move(&mut self, code: crossterm::event::KeyCode, ctrl: bool) {
        if code == crossterm::event::KeyCode::Esc {
            self.finish(0);
            return;
        }
        if code == crossterm::event::KeyCode::Enter {
            self.action_submit_move();
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('u') {
            self.move_input.clear();
            self.move_cursor = 0;
            self.validate_move();
            return;
        }
        if ctrl {
            return;
        }
        if code == crossterm::event::KeyCode::Tab {
            self.complete_move();
            return;
        }
        edit_text(&mut self.move_input, &mut self.move_cursor, code);
        self.validate_move();
    }

    fn complete_move(&mut self) -> bool {
        complete_folder_path(&mut self.move_input, &mut self.move_cursor, &self.home, self.remote_host,
                           &*self.runner)
    }

    // ---------------------------------------------------------------- footer --------
    fn footer_sections(&self) -> Vec<(&'static str, &'static str)> {
        match self.kind {
            Kind::New => {
                if self.new_focus == NewFocus::Remote {
                    vec![("enter", "create"), ("space", "toggle"), ("esc", "cancel")]
                } else if self.new_focus == NewFocus::Folder {
                    vec![("enter", "create"), ("esc", "cancel")]
                } else {
                    vec![("enter", "next"), ("ctrl+s", "create"), ("esc", "cancel")]
                }
            }
            Kind::Rename => vec![("enter", "rename"), ("esc", "cancel")],
            Kind::Move => vec![("enter", "move"), ("tab", "complete"), ("esc", "cancel")],
            Kind::Archive => {
                if self.forced {
                    vec![("y", "archive anyway"), ("n", "cancel")]
                } else if self.busy {
                    vec![("f", "force"), ("n", "cancel")]
                } else {
                    vec![("y", "archive"), ("n", "cancel")]
                }
            }
            Kind::Delete => {
                if self.forced {
                    vec![("y", "delete anyway"), ("n", "cancel")]
                } else if self.busy {
                    vec![("f", "force"), ("n", "cancel")]
                } else {
                    vec![("y", "delete"), ("n", "cancel")]
                }
            }
            Kind::Archived => vec![("enter", "restore"), ("d", "delete"), ("esc", "close")],
            Kind::Jump => vec![("enter", "jump"), ("↑↓", "select"), ("esc", "close")],
        }
    }

    // ---------------------------------------------------------------- drawing --------
    pub fn draw(&mut self, buf: &mut Buffer) {
        let area = *buf.area();
        for y in 0..area.height {
            for x in 0..area.width {
                if let Some(cell) = buf.cell_mut((x, y)) {
                    cell.set_symbol(" ");
                    cell.set_style(Style::new().bg(BG));
                }
            }
        }
        match self.kind {
            Kind::New => self.draw_new(buf),
            Kind::Rename => self.draw_rename(buf),
            Kind::Move => self.draw_move(buf),
            Kind::Archive | Kind::Delete => self.draw_confirm(buf),
            Kind::Archived => self.draw_archived(buf),
            Kind::Jump => self.draw_jump(buf),
        }
        self.draw_footer(buf);
    }

    fn content_w(&self) -> usize {
        self.w.saturating_sub(CONTENT_LEFT * 2)
    }

    pub fn cursor_pos(&self) -> Option<(u16, u16)> {
        match self.kind {
            Kind::New => match self.new_focus {
                NewFocus::Name => self.input_cursor(7, &self.name_input, self.name_cursor),
                NewFocus::Folder => self.input_cursor(10, &self.cwd_input, self.cwd_cursor),
                NewFocus::Kind | NewFocus::Remote => None,
            },
            Kind::Rename => self.input_cursor(4, &self.rename_input, self.rename_cursor),
            Kind::Move => self.input_cursor(4, &self.move_input, self.move_cursor),
            Kind::Jump => self.input_cursor(JUMP_FILTER_Y, &self.jump_filter, self.jump_cursor),
            _ => None,
        }
    }

    fn input_cursor(&self, y: usize, text: &str, cursor: usize) -> Option<(u16, u16)> {
        let inner = self.content_w().saturating_sub(1);
        let (_, vis_cursor) = show_slice(text, cursor, inner);
        let x = CONTENT_LEFT + 1 + prefix_width(text, vis_cursor);
        if x >= self.w {
            return None;
        }
        Some((x as u16, y as u16))
    }

    /// The keys, as on Force's cards (force/cards.py): right-aligned on the row above the last, the first (the
    /// main action) as a coral button ` Enter  Create `, the others in grey (`Esc  Cancel`).
    fn draw_footer(&self, buf: &mut Buffer) {
        let y = self.h.saturating_sub(2);
        let parts: Vec<(String, bool)> = self
            .footer_sections()
            .iter()
            .enumerate()
            .map(|(i, (key, word))| {
                let text = format!("{}  {}", cap_key(key), cap_first(word));
                if i == 0 { (format!(" {} ", text), true) } else { (text, false) }
            })
            .collect();
        let width: usize = parts.iter().map(|(t, _)| t.chars().map(chw).sum::<usize>()).sum::<usize>()
            + SPACER * parts.len().saturating_sub(1);
        let right = self.w.saturating_sub(CONTENT_LEFT);
        let mut x = right.saturating_sub(width).max(CONTENT_LEFT);
        for (i, (text, button)) in parts.iter().enumerate() {
            if i > 0 {
                x += SPACER;
            }
            if *button {
                for ch in text.chars() {
                    x = set_cell(buf, x, y, ch, BG_INK, ACCENT, true);
                }
            } else {
                x = put(buf, x, y, text, FG_DIM);
            }
        }
    }

    /// The title row and its rule, as on Force's cards: the icon in coral (rich tier), the title in white bold.
    fn draw_title(&self, buf: &mut Buffer, icon: char, text: &str) {
        let mut x = CONTENT_LEFT;
        if self.tier == Tier::Rich {
            x = set_cell(buf, x, 1, icon, ACCENT, BG, false);
            x += crate::glyphs::gap(self.tier).chars().count();
        }
        for ch in text.chars() {
            if x >= self.w.saturating_sub(CONTENT_LEFT) {
                break;
            }
            x = set_cell(buf, x, 1, ch, WHITE, BG, true);
        }
        let mut rx = CONTENT_LEFT;
        while rx < self.w.saturating_sub(CONTENT_LEFT) {
            rx = set_cell(buf, rx, 2, '─', RULE, BG, false);
        }
    }

    fn draw_input(&self, buf: &mut Buffer, y: usize, text: &str, cursor: usize, placeholder: &str, focused: bool) {
        let width = self.content_w();
        fill(buf, CONTENT_LEFT, y, width, BG_RAISED);
        set_cell(buf, CONTENT_LEFT, y, '▍', if focused { ACCENT } else { Color::Rgb(0x3a, 0x3a, 0x3a) }, BG_RAISED, false);
        let inner = width.saturating_sub(1);
        if inner == 0 {
            return;
        }
        let (visible, vis_cursor) = show_slice(text, cursor, inner);
        if visible.is_empty() && !placeholder.is_empty() {
            let _ = put_trunc_on(buf, CONTENT_LEFT + 1, y, placeholder, FG_FAINT, BG_RAISED, inner);
            return;
        }
        let mut x = CONTENT_LEFT + 1;
        for (i, ch) in visible.chars().enumerate() {
            if i == vis_cursor && focused {
                set_cell(buf, x, y, ch, BG, BG_SELECTED, true);
            } else {
                set_cell(buf, x, y, ch, FG, BG_RAISED, false);
            }
            x += chw(ch);
        }
    }

    fn draw_new(&mut self, buf: &mut Buffer) {
        // rows: 1 title, 3 `Kind`, 4 chips, 6 `Name`, 7 name input, 9 `Folder`, 10 folder input,
        //       12 Remote Control chip (Claude Code only), 14 status, last row footer.
        self.draw_title(buf, '\u{F067}', "New session");
        put(buf, CONTENT_LEFT, 3, "Kind", FG_DIM);
        // the selected chip is raised; coral while the kind row has focus
        let sel_fg = if self.new_focus == NewFocus::Kind { ACCENT } else { WHITE };
        for (i, (x, label)) in kind_chips(&self.new_kinds).into_iter().enumerate() {
            let on = i == self.new_kind;
            let (bg, fg) = if on { (BG_SELECTED, sel_fg) } else { (BG_RAISED, FG_DIM) };
            toggle(buf, x, 4, label, bg, fg, on);
        }
        put(buf, CONTENT_LEFT, 6, "Name", FG_DIM);
        self.draw_input(buf, 7, &self.name_input, self.name_cursor, "empty: named for you", self.new_focus == NewFocus::Name);
        put(buf, CONTENT_LEFT, 9, "Folder", FG_DIM);
        self.draw_input(buf, 10, &self.cwd_input, self.cwd_cursor, "", self.new_focus == NewFocus::Folder);
        if self.remote_offered() {
            // a chip like the kind chips: raised and lit when on, flat and dim when off; coral while focused
            let focused = self.new_focus == NewFocus::Remote;
            let mark = if self.new_remote {
                crate::glyphs::state(self.tier, '✓', '\u{F14A}')      // check-square
            } else {
                crate::glyphs::state(self.tier, ' ', '\u{F096}')      // square
            };
            let label = format!("{}{} Remote Control", mark, " ".repeat(crate::glyphs::extra(self.tier)));
            let (bg, fg) = match (self.new_remote, focused) {
                (_, true) => (BG_SELECTED, ACCENT),
                (true, false) => (BG_SELECTED, WHITE),
                (false, false) => (BG_RAISED, FG_DIM),
            };
            let x = toggle(buf, CONTENT_LEFT, NEW_REMOTE_Y, &label, bg, fg, self.new_remote);
            put(buf, x + 2, NEW_REMOTE_Y, "in the Claude app and on claude.ai", FG_DIM);
        }
        self.status_text_y(buf, NEW_REMOTE_Y + 2);
    }

    fn draw_rename(&mut self, buf: &mut Buffer) {
        self.draw_title(buf, '\u{F040}', &format!("Rename {}", self.name.clone().unwrap_or_default()));
        put(buf, CONTENT_LEFT, 3, "New name", FG_DIM);
        self.draw_input(buf, 4, &self.rename_input, self.rename_cursor, "", true);
        self.status_text_y(buf, 6);
    }

    fn draw_move(&mut self, buf: &mut Buffer) {
        self.draw_title(buf, '\u{F07B}', &format!("Move {}", self.name.clone().unwrap_or_default()));
        put(buf, CONTENT_LEFT, 3, "Folder", FG_DIM);
        self.draw_input(buf, 4, &self.move_input, self.move_cursor, "", true);
        // a shell session restarts in the new folder: one dim warning row under the field
        if self.entry_kind() == "shell" {
            self.draw_colored(buf, 6, "the shell restarts in the new folder", StatusKind::Warning);
        }
        self.status_text_y(buf, 7);
    }

    fn draw_confirm(&mut self, buf: &mut Buffer) {
        let (icon, raw) = match (self.kind, self.forced) {
            (Kind::Archive, false) => ('\u{F187}', format!("Archive {}?", self.name.clone().unwrap_or_default())),
            (Kind::Archive, true) => ('\u{F187}', format!("Force-archive {}?", self.name.clone().unwrap_or_default())),
            (_, false) => ('\u{F1F8}', format!("Delete {}?", self.name.clone().unwrap_or_default())),
            (_, true) => ('\u{F1F8}', format!("Force-delete {}?", self.name.clone().unwrap_or_default())),
        };
        self.draw_title(buf, icon, &raw);
        let body = match self.kind {
            Kind::Archive => "Stops the session and keeps its conversation. Restore it any time from the archived list (Ctrl+Space A).",
            _ => "Stops it and removes it from Force for good; it cannot be restored. (Archive keeps it instead.)",
        };
        let mut y = 3;
        for l in wrap(body, self.content_w()) {
            put(buf, CONTENT_LEFT, y, &l, FG_DIM);
            y += 1;
        }
        self.warn_y(buf, y + 1);
    }

    fn warn_y(&mut self, buf: &mut Buffer, y: usize) {
        let mut text = self.warn_text.clone();
        let mut kind = self.warn_kind;
        if text.is_empty() && !self.forced && self.busy {
            kind = StatusKind::Warning;
            text = format!("{}{}{} is working right now", crate::glyphs::state(self.tier, '!', '\u{F071}'), crate::glyphs::gap(self.tier), self.name.clone().unwrap_or_default());
        }
        if text.is_empty() {
            return;
        }
        self.draw_colored(buf, y, &text, kind);
    }

    fn status_text_y(&mut self, buf: &mut Buffer, y: usize) {
        let text = self.status_text.clone();
        let kind = self.status_kind;
        self.draw_colored(buf, y, &text, kind);
    }

    fn draw_colored(&mut self, buf: &mut Buffer, y: usize, text: &str, kind: StatusKind) {
        let (glyph, color) = status_parts(self.tier, kind);
        let mut x = CONTENT_LEFT;
        if let Some(g) = glyph {
            x = set_cell(buf, x, y, g, color, BG, false);
            x += 1 + crate::glyphs::extra(self.tier);
        }
        let _ = put_trunc(buf, x, y, text, color, self.w - CONTENT_LEFT - 2);
    }

    fn draw_archived(&mut self, buf: &mut Buffer) {
        let entries = archived_sorted(&self.entries);
        // the count in the title: the list can hold more than fits (11 archived, 7 shown)
        let title = if entries.is_empty() { "Archived sessions".to_string() }
                    else { format!("Archived sessions ({})", entries.len()) };
        self.draw_title(buf, '\u{F187}', &title);
        let list_top = ARCH_LIST_TOP;
        let bottom = self.h.saturating_sub(3);
        let list_h = bottom.saturating_sub(list_top);
        if entries.is_empty() {
            let lines = ["Nothing archived yet.", "Archive a session with Ctrl+Space a."];
            let start = list_top + (list_h / 2).saturating_sub(1);
            for (i, l) in lines.iter().enumerate() {
                let x = CONTENT_LEFT + self.content_w().saturating_sub(l.width()) / 2;
                let _ = put(buf, x, start + i, l, FG_DIM);
            }
            self.status_text_y(buf, bottom);
            return;
        }
        let (top, visible, up, down) = self.arch_layout(entries.len());
        let more = |n: usize, arrow: char| format!("{arrow} {n} more");
        if up {
            let _ = put(buf, CONTENT_LEFT, list_top, &more(top, '↑'), FG_DIM);
        }
        let mut row = list_top + up as usize;
        for (idx, e) in entries.iter().enumerate().skip(top).take(visible) {
            if row + 1 >= bottom {
                break;
            }
            let selected = idx == self.arch_cursor;
            let (bg, name_color) = if selected { (BG_SELECTED, ACCENT) } else { (BG, FG) };
            if selected {
                // the highlight covers both rows of the entry, one column wider than the text on each side
                fill(buf, CONTENT_LEFT - 1, row, self.content_w() + 2, BG_SELECTED);
                fill(buf, CONTENT_LEFT - 1, row + 1, self.content_w() + 2, BG_SELECTED);
            }
            let _ = put_trunc_on(buf, CONTENT_LEFT, row, &e.name, name_color, bg, self.content_w());
            let date: String = e.archived_at.as_deref().unwrap_or("").chars().take(10).collect();
            let meta = format!("archived {} · {}", date, display_cwd(&e.cwd, &self.home));
            let _ = put_trunc_on(buf, CONTENT_LEFT, row + 1, &meta, FG_DIM, bg, self.content_w());
            row += 2;
        }
        if down {
            let below = entries.len().saturating_sub(top + visible);
            let _ = put(buf, CONTENT_LEFT, list_top + up as usize + 2 * visible, &more(below, '↓'), FG_DIM);
        }
        self.status_text_y(buf, bottom);
    }

    /// The archived list's layout: (first entry shown, entries that fit, a `↑ N more` row above them, a `↓ N more`
    /// row below them). Each entry takes two rows and each `more` row one, so the two depend on each other: settle
    /// them in a few passes. Drawing and clicks both use it.
    fn arch_layout(&self, n: usize) -> (usize, usize, bool, bool) {
        let rows = self.h.saturating_sub(3).saturating_sub(ARCH_LIST_TOP);
        let (mut up, mut down) = (false, false);
        let mut out = (0, 0, false, false);
        for _ in 0..4 {
            let visible = (rows.saturating_sub(up as usize + down as usize) / 2).max(1);
            let top = scroll_top(self.arch_cursor, visible, n);
            out = (top, visible, up, down);
            let (u, d) = (top > 0, top + visible < n);
            if (u, d) == (up, down) {
                break;
            }
            (up, down) = (u, d);
        }
        out
    }

    fn draw_jump(&mut self, buf: &mut Buffer) {
        self.draw_title(buf, '\u{F002}', "Jump to session");
        self.draw_input(buf, JUMP_FILTER_Y, &self.jump_filter, self.jump_cursor, "type to filter", true);
        let list_top = JUMP_LIST_TOP;
        let bottom = self.h.saturating_sub(3);
        let list_h = bottom.saturating_sub(list_top);
        if self.jump_shown.is_empty() {
            let msg = "no session matches";
            let x = CONTENT_LEFT + self.content_w().saturating_sub(msg.width()) / 2;
            let _ = put(buf, x, list_top, msg, FG_DIM);
            self.set_status(StatusKind::Neutral, "no session matches");
            self.status_text_y(buf, bottom);
            return;
        }
        let visible = list_h.max(1);
        let top = scroll_top(self.jump_sel, visible, self.jump_shown.len());
        for (row, (idx, s)) in (list_top..).zip(self.jump_shown.iter().enumerate().skip(top)) {
            if row >= bottom {
                break;
            }
            let selected = idx == self.jump_sel;
            let (bg, color) = if selected { (BG_SELECTED, ACCENT) } else { (BG, FG) };
            if selected {
                fill(buf, CONTENT_LEFT - 1, row, self.content_w() + 2, BG_SELECTED);
            }
            let mut x = CONTENT_LEFT;
            if self.glyphs {
                x = set_cell(buf, x, row, glyph_char(self.tier, s.state, self.now_ms), glyph_color(s.state), bg, matches!(s.state, State::NeedsYou | State::YourTurn));
                x += 1 + crate::glyphs::extra(self.tier);
            }
            let room = self.content_w().saturating_sub(x - CONTENT_LEFT);
            let _ = put_trunc_on(buf, x, row, &s.name, color, bg, room);
        }
        self.status_text_y(buf, bottom);
    }

    // ---------------------------------------------------------------- jump session loading
    /// The `switch-client` argv for `name`: in split mode (FORCE_CLIENT set) it carries `-c <nested>` so the
    /// host switch targets the right nested client; classic mode is `switch-client -t =name`.
    fn switch_argv(&self, name: &str) -> Vec<String> {
        let mut argv = vec!["switch-client".to_string()];
        if let Some(c) = self.force_client.as_deref().filter(|c| !c.is_empty()) {
            argv.push("-c".to_string());
            argv.push(c.to_string());
        }
        argv.push("-t".to_string());
        argv.push(format!("={}", name));
        argv
    }

    fn detect_own_session(&mut self) {
        if let Some(s) = self.force_session.as_deref().filter(|s| !s.is_empty()) {
            self.own_session = Some(s.to_string());
            return;
        }
        let out = self.runner.tmux(&["display".to_string(), "-p".to_string(), "#{session_name}".to_string()]);
        if let Ok(name) = out {
            if !name.is_empty() {
                self.own_session = Some(name);
                return;
            }
        }
        // fall back to the first client's session
        if let Ok(c) = self.runner.tmux(&["list-clients".to_string(), "-F".to_string(), "#{client_session}".to_string()]) {
            if let Some(first) = c.lines().next() {
                if !first.is_empty() {
                    self.own_session = Some(first.to_string());
                }
            }
        }
    }

    fn load_sessions(&mut self) {
        let snap = snapshot::load(&self.snapshot_path);
        self.glyphs = snap.is_some();
        let sessions: Vec<JumpSession> = match &snap {
            Some(s) => s
                .sessions
                .iter()
                .filter(|s| !s.archived && !s.support)       // Chuck opens from his own row (SPEC §50)
                .map(|s| JumpSession {
                    name: s.name.clone(),
                    state: s.state,
                    index: s.index,
                })
                .collect(),
            None => self
                .entries
                .iter()
                .filter(|e| !e.archived)
                .map(|e| JumpSession {
                    name: e.name.clone(),
                    state: State::Idle,
                    index: None,
                })
                .collect(),
        };
        let group = |s: &JumpSession| match s.state {
            State::NeedsYou => 0u8,
            State::YourTurn => 1u8,
            _ => 2u8,
        };
        let mut order = sessions;
        order.sort_by_key(|s| (group(s), s.index.unwrap_or(0)));
        if let Some(own) = self.own_session.clone() {
            let own_s = order.iter().find(|s| s.name == own).cloned();
            order.retain(|s| s.name != own);
            if let Some(o) = own_s {
                order.push(o);
            }
        }
        self.jump_ordered = order;
    }

    fn rebuild_jump(&mut self) {
        self.jump_shown = rank_sessions(&self.jump_ordered, &self.jump_filter);
    }

    // ---------------------------------------------------------------- key handling
    pub fn handle_key(&mut self, key: crossterm::event::KeyEvent, now_ms: i64) {
        self.now_ms = now_ms;
        if self.done {
            return;
        }
        let code = key.code;
        let shift = key.modifiers.contains(crossterm::event::KeyModifiers::SHIFT);
        let ctrl = key.modifiers.contains(crossterm::event::KeyModifiers::CONTROL);
        match self.kind {
            Kind::New => self.key_new(code, shift, ctrl),
            Kind::Rename => self.key_rename(code, ctrl),
            Kind::Move => self.key_move(code, ctrl),
            Kind::Archive | Kind::Delete => self.key_confirm(code),
            Kind::Archived => self.key_archived(code),
            Kind::Jump => self.key_jump(code, ctrl),
        }
    }

    /// Handle a mouse action: click a list row selects+activates; a click on a kind toggle selects it.
    pub fn handle_mouse(&mut self, m: &crossterm::event::MouseEvent) {
        use crossterm::event::{MouseButton, MouseEventKind};
        if self.done {
            return;
        }
        // the wheel moves the selection one entry per notch, which scrolls the list as the arrow keys do
        if let MouseEventKind::ScrollUp | MouseEventKind::ScrollDown = m.kind {
            let down = matches!(m.kind, MouseEventKind::ScrollDown);
            let step = |cur: usize, n: usize| if down { (cur + 1).min(n.saturating_sub(1)) } else { cur.saturating_sub(1) };
            match self.kind {
                Kind::Archived => {
                    let n = archived_sorted(&self.entries).len();
                    self.arch_cursor = step(self.arch_cursor, n);
                }
                Kind::Jump => self.jump_sel = step(self.jump_sel, self.jump_shown.len()),
                _ => {}
            }
            return;
        }
        if !matches!(m.kind, MouseEventKind::Down(MouseButton::Left)) {
            return;
        }
        let col = m.column as usize;
        let row = m.row as usize;
        match self.kind {
            Kind::New => {
                if row == 4 && col >= CONTENT_LEFT {
                    // the chip under the click (or the last one left of it)
                    let chips = kind_chips(&self.new_kinds);
                    self.new_kind = chips.iter().rposition(|(x, _)| col >= *x).unwrap_or(0);
                    self.new_focus = NewFocus::Kind;
                } else if row == 7 {
                    self.new_focus = NewFocus::Name;
                } else if row == 10 {
                    self.new_focus = NewFocus::Folder;
                } else if row == NEW_REMOTE_Y && self.remote_offered() {
                    self.new_focus = NewFocus::Remote;
                    self.new_remote = !self.new_remote;
                }
            }
            Kind::Archived => {
                let entries = archived_sorted(&self.entries);
                if entries.is_empty() {
                    return;
                }
                let (top, visible, up, down) = self.arch_layout(entries.len());
                let first = ARCH_LIST_TOP + up as usize;
                if up && row == ARCH_LIST_TOP {
                    self.arch_cursor = top.saturating_sub(visible);                 // `↑ N more`: a page up
                } else if down && row == first + 2 * visible {
                    self.arch_cursor = (top + 2 * visible - 1).min(entries.len() - 1);   // `↓ N more`: a page down
                } else if row >= first && row < first + 2 * visible {
                    let idx = top + (row - first) / 2;
                    if idx < entries.len() {
                        self.arch_cursor = idx;
                        self.action_arch_choose();
                    }
                }
            }
            Kind::Jump => {
                if self.jump_shown.is_empty() {
                    return;
                }
                let list_top = JUMP_LIST_TOP;
                let bottom = self.h.saturating_sub(3);
                let visible = (bottom - list_top).max(1);
                let top = scroll_top(self.jump_sel, visible, self.jump_shown.len());
                if row < list_top {
                    return;
                }
                let idx = top + (row - list_top);
                if idx < self.jump_shown.len() {
                    self.jump_sel = idx;
                    self.action_jump_choose();
                }
            }
            _ => {}
        }
    }

    /// Restore-or-jump on the currently selected jump row.
    fn action_jump_choose(&mut self) {
        let name = match self.jump_shown.get(self.jump_sel) {
            Some(s) => s.name.clone(),
            None => {
                self.set_status(StatusKind::Neutral, "no session matches");
                return;
            }
        };
        match self.runner.tmux(&self.switch_argv(&name)) {
            Err(e) => self.set_status(StatusKind::Error, format!("switch: {}", e)),
            Ok(_) => self.finish(0),
        }
    }

    /// Move the New dialog's kind selection by `d` chips, wrapping.
    fn step_kind(&mut self, d: i64) {
        let n = self.new_kinds.len().max(1) as i64;
        self.new_kind = ((self.new_kind as i64 + d).rem_euclid(n)) as usize;
        if self.new_focus == NewFocus::Remote && !self.remote_offered() {
            self.new_focus = NewFocus::Folder;
        }
    }

    fn key_new(&mut self, code: crossterm::event::KeyCode, shift: bool, ctrl: bool) {
        if code == crossterm::event::KeyCode::Esc {
            self.finish(0);
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('t') {
            self.step_kind(1);
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('s') {
            self.action_submit_new();
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('r') {
            if self.remote_offered() {
                self.new_remote = !self.new_remote;
            }
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('u') {
            match self.new_focus {
                NewFocus::Name => {
                    self.name_input.clear();
                    self.name_cursor = 0;
                }
                NewFocus::Folder => {
                    self.cwd_input.clear();
                    self.cwd_cursor = 0;
                }
                NewFocus::Kind | NewFocus::Remote => {}
            }
            self.validate_new();
            return;
        }
        // Shift+Tab: most terminals send it as BackTab, some as Tab with Shift
        if code == crossterm::event::KeyCode::BackTab || (code == crossterm::event::KeyCode::Tab && shift) {
            self.cycle_new_focus(-1);
            return;
        }
        if code == crossterm::event::KeyCode::Tab {
            if self.new_focus == NewFocus::Folder && self.complete_folder() {
                // Tab completed a path in the Folder field; focus stays in Folder.
                return;
            }
            self.cycle_new_focus(1);
            return;
        }
        if self.new_focus == NewFocus::Kind {
            match code {
                crossterm::event::KeyCode::Left => self.step_kind(-1),
                crossterm::event::KeyCode::Right => self.step_kind(1),
                _ => {}
            }
            return;
        }
        if ctrl {
            return;
        }
        match self.new_focus {
            NewFocus::Kind => {}
            NewFocus::Remote => match code {
                crossterm::event::KeyCode::Enter => self.action_submit_new(),
                crossterm::event::KeyCode::Char(' ') => self.new_remote = !self.new_remote,
                _ => {}
            },
            NewFocus::Name => {
                if code == crossterm::event::KeyCode::Enter {
                    // an empty name is fine (the session is named for you): Enter moves on to Folder either way,
                    // unless the typed name is refused (its message is shown)
                    let name = self.name_input.trim().to_string();
                    if name.is_empty() || self.name_message(&name).is_none() {
                        self.new_focus = NewFocus::Folder;
                        self.cwd_cursor = self.cwd_input.chars().count();
                    }
                    return;
                }
                edit_text(&mut self.name_input, &mut self.name_cursor, code);
                self.validate_new();
            }
            NewFocus::Folder => {
                if code == crossterm::event::KeyCode::Enter {
                    self.action_submit_new();
                    return;
                }
                edit_text(&mut self.cwd_input, &mut self.cwd_cursor, code);
                self.validate_new();
            }
        }
    }

    fn cycle_new_focus(&mut self, step: i32) {
        let mut order = vec![NewFocus::Kind, NewFocus::Name, NewFocus::Folder];
        if self.remote_offered() {
            order.push(NewFocus::Remote);
        }
        let cur = order.iter().position(|f| *f == self.new_focus).unwrap_or(0);
        let next = (cur as i32 + step).rem_euclid(order.len() as i32) as usize;
        self.new_focus = order[next];
        match self.new_focus {
            NewFocus::Folder => self.cwd_cursor = self.cwd_input.chars().count(),
            NewFocus::Name => self.name_cursor = self.name_input.chars().count(),
            NewFocus::Kind | NewFocus::Remote => {}
        }
    }

    /// Attempt Tab-completion of the Folder path. Returns `true` if the text
    /// was changed (focus stays in Folder), `false` if nothing completed
    /// (caller should cycle focus on).
    fn complete_folder(&mut self) -> bool {
        complete_folder_path(&mut self.cwd_input, &mut self.cwd_cursor, &self.home, self.remote_host,
                           &*self.runner)
    }

    fn action_submit_new(&mut self) {
        if self.done {
            return;
        }
        let name = self.name_input.trim().to_string();
        let auto = name.is_empty();     // Force names it and prints the name (SPEC §49.6)
        if (!auto && self.name_message(&name).is_some()) || (!self.remote_host && !std::path::Path::new(&self.new_folder()).is_dir()) {
            self.validate_new();
            return;
        }
        let mut argv = vec!["new".to_string()];
        if !auto {
            argv.push(name.clone());
        }
        argv.extend(["--cwd".to_string(), self.new_folder(), "--no-switch".to_string()]);
        match self.new_kinds.get(self.new_kind).map(String::as_str) {
            Some("shell") => argv.push("--shell".to_string()),
            Some("opencode") => argv.extend(["--kind".to_string(), "opencode".to_string()]),
            _ => {}
        }
        // SPEC §53: unchecked -> --no-remote-control; checked -> --remote-control only against an "off" default, so an
        // older Force (a remote host) never sees a flag it does not know unless the box was turned on against it
        if self.remote_offered() {
            if !self.new_remote {
                argv.push("--no-remote-control".to_string());
            } else if !self.new_remote_default {
                argv.push("--remote-control".to_string());
            }
        }
        match self.runner.cockpit(&argv) {
            Err(e) => self.set_status(StatusKind::Error, e),
            Ok(out) => {
                let name = if auto { out.lines().last().unwrap_or("").trim().to_string() } else { name };
                if name.is_empty() {
                    self.set_status(StatusKind::Error, "created, but no name came back");
                    return;
                }
                match self.runner.tmux(&self.switch_argv(&name)) {
                    Err(e) => self.set_status(StatusKind::Error, format!("switch: {}", e)),
                    Ok(_) => self.finish(0),
                }
            }
        }
    }

    fn key_rename(&mut self, code: crossterm::event::KeyCode, ctrl: bool) {
        if code == crossterm::event::KeyCode::Esc {
            self.finish(0);
            return;
        }
        if code == crossterm::event::KeyCode::Enter || (ctrl && code == crossterm::event::KeyCode::Char('s')) {
            self.action_submit_rename();
            return;
        }
        if ctrl && code == crossterm::event::KeyCode::Char('u') {
            self.rename_input.clear();
            self.rename_cursor = 0;
            self.validate_rename();
            return;
        }
        if ctrl {
            return;
        }
        edit_text(&mut self.rename_input, &mut self.rename_cursor, code);
        self.validate_rename();
    }

    fn action_submit_rename(&mut self) {
        if self.done {
            return;
        }
        if self.validation_message().is_some() {
            self.validate_rename();
            return;
        }
        let old = self.name.clone().unwrap_or_default();
        let new = self.rename_input.trim().to_string();
        match self.runner.cockpit(&["rename".to_string(), old.clone(), new.clone()]) {
            Err(e) => self.set_status(StatusKind::Error, e),
            Ok(_) => {
                if self.runner.tmux_ok(&["has-session".to_string(), format!("={}", old)]) {
                    self.set_status(StatusKind::Ok, "renamed — applies when the session is idle");
                    self.exit_after_ms = Some(self.now_ms + 1500);
                } else {
                    self.finish(0);
                }
            }
        }
    }

    fn key_confirm(&mut self, code: crossterm::event::KeyCode) {
        match code {
            crossterm::event::KeyCode::Esc | crossterm::event::KeyCode::Char('n') => self.finish(0),
            crossterm::event::KeyCode::Char('f') => {
                self.forced = true;
                self.warn_text.clear();
            }
            crossterm::event::KeyCode::Enter | crossterm::event::KeyCode::Char('y') => self.action_yes(),
            _ => {}
        }
    }

    fn action_yes(&mut self) {
        if self.done {
            return;
        }
        if !self.forced && self.busy {
            self.warn_kind = StatusKind::Warning;
            self.warn_text = format!("{}{}busy — press f to force", crate::glyphs::state(self.tier, '!', '\u{F071}'), crate::glyphs::gap(self.tier));
            return;
        }
        let name = self.name.clone().unwrap_or_default();
        let mut argv = match self.kind {
            Kind::Archive => vec!["archive".to_string(), name.clone()],
            _ => vec!["delete".to_string(), name.clone(), "--yes".to_string()],
        };
        if self.forced {
            argv.push("--force".to_string());
        }
        match self.runner.cockpit(&argv) {
            Err(e) => {
                self.warn_kind = StatusKind::Warning;
                self.warn_text = format!("{} {}", crate::glyphs::state(self.tier, '!', '⚠'), e);
            }
            Ok(_) => self.finish(0),
        }
    }

    fn key_archived(&mut self, code: crossterm::event::KeyCode) {
        match code {
            crossterm::event::KeyCode::Esc | crossterm::event::KeyCode::Char('q') => self.finish(0),
            crossterm::event::KeyCode::Char('d') | crossterm::event::KeyCode::Char('f') => self.action_forget(),
            crossterm::event::KeyCode::Up => {
                let n = archived_sorted(&self.entries).len();
                if n > 0 {
                    self.arch_cursor = (self.arch_cursor + n - 1) % n;
                }
            }
            crossterm::event::KeyCode::Down => {
                let n = archived_sorted(&self.entries).len();
                if n > 0 {
                    self.arch_cursor = (self.arch_cursor + 1) % n;
                }
            }
            crossterm::event::KeyCode::Enter => self.action_arch_choose(),
            _ => {}
        }
    }

    fn selected_archived_name(&self) -> Option<String> {
        archived_sorted(&self.entries)
            .get(self.arch_cursor)
            .map(|e| e.name.clone())
    }

    fn forget_current(&mut self) {
        let name = match self.selected_archived_name() {
            Some(n) => n,
            None => return,
        };
        match self.runner.cockpit(&["forget".to_string(), name.clone()]) {
            Err(e) => {
                self.set_status(StatusKind::Error, e);
                self.pending_forget = None;
            }
            Ok(_) => {
                // The registry is read-only from the dialog's view: re-read the file for anything the daemon
                // wrote, and drop the forgotten row from the in-memory list too (the real force removes it).
                self.entries = load_registry(&self.registry_path);
                self.entries.retain(|e| e.name != name);
                self.arch_cursor = 0;
                self.pending_forget = None;
                self.set_status(StatusKind::Neutral, "");
            }
        }
    }

    fn action_forget(&mut self) {
        let name = match self.selected_archived_name() {
            Some(n) => n,
            None => return,
        };
        if self.pending_forget.as_deref() == Some(&name) {
            self.forget_current();
            return;
        }
        self.pending_forget = Some(name.clone());
        self.set_status(
            StatusKind::Warning,
            format!("delete {} for good? enter confirms, it cannot be restored", name),
        );
    }

    fn action_arch_choose(&mut self) {
        if self.pending_forget.is_some() {
            self.forget_current();
            return;
        }
        let name = match self.selected_archived_name() {
            Some(n) => n,
            None => return,
        };
        match self.runner.cockpit(&["unarchive".to_string(), name.clone()]) {
            Err(e) => self.set_status(StatusKind::Error, e),
            Ok(_) => match self.runner.tmux(&self.switch_argv(&name)) {
                Err(e) => self.set_status(StatusKind::Error, format!("switch: {}", e)),
                Ok(_) => self.finish(0),
            },
        }
    }

    fn key_jump(&mut self, code: crossterm::event::KeyCode, ctrl: bool) {
        if ctrl && code == crossterm::event::KeyCode::Char('u') {
            self.jump_filter.clear();
            self.jump_cursor = 0;
            self.rebuild_jump();
            self.jump_sel = 0;
            return;
        }
        match code {
            crossterm::event::KeyCode::Esc => self.finish(0),
            crossterm::event::KeyCode::Up => {
                if !self.jump_shown.is_empty() {
                    self.jump_sel = (self.jump_sel + self.jump_shown.len() - 1) % self.jump_shown.len();
                }
            }
            crossterm::event::KeyCode::Down => {
                if !self.jump_shown.is_empty() {
                    self.jump_sel = (self.jump_sel + 1) % self.jump_shown.len();
                }
            }
            crossterm::event::KeyCode::Enter => self.action_jump_choose(),
            _ => {
                edit_text(&mut self.jump_filter, &mut self.jump_cursor, code);
                self.rebuild_jump();
                self.jump_sel = 0;
            }
        }
    }

    fn finish(&mut self, result: i32) {
        self.result = result;
        self.done = true;
    }

    /// Returns true when it is time for the loop to exit.
    pub fn tick(&mut self, now_ms: i64) -> bool {
        self.now_ms = now_ms;
        if self.done {
            return true;
        }
        if let Some(dead) = self.exit_after_ms {
            if now_ms >= dead {
                self.result = 0;
                self.done = true;
                return true;
            }
        }
        false
    }
}

// --------------------------------------------------------------------------- pure helpers ----

fn chw(ch: char) -> usize {
    ch.width().unwrap_or(1)
}

/// A key as the cards write it: `enter` -> `Enter`, `ctrl+s` -> `Ctrl+S`, `y` -> `Y`.
fn cap_key(key: &str) -> String {
    key.split('+')
        .map(|p| {
            let mut c = p.chars();
            match c.next() {
                Some(f) if p.chars().count() == 1 => f.to_uppercase().collect::<String>(),
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join("+")
}

fn cap_first(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// Strip a trailing `/` for path comparison (a lone `/` stays as-is).
fn strip_trailing_slash(s: &str) -> &str {
    let t = s.trim_end_matches('/');
    if t.is_empty() { "/" } else { t }
}

/// Shared Tab-completion of a folder path shared by the New and Move dialogs. Reads what the user typed in
/// `text`, expands `~` for matching but keeps what was typed, and on a match replaces `text` (single match ->
/// full path + '/', multiple -> longest common prefix) and moves `cursor` to the end. Returns `true` when the
/// text changed.
fn complete_folder_path(text: &mut String, cursor: &mut usize, home: &str, remote: bool,
                      runner: &dyn Runner) -> bool {
    let old = text.clone();
    let cur = old.clone();
    // Empty text has no partial name to extend.
    if cur.is_empty() {
        return false;
    }
    let (dir, _) = split_dir_prefix(&cur);
    let prefix = Path::new(&cur)
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    // No partial name typed (path ends with '/') -> nothing to extend.
    if prefix.is_empty() {
        return false;
    }
    // On a remote host the folders live there: ask it (`force dirs <dir as typed> <prefix>`). Locally, read
    // the file system, expanding ~ for matching but keeping what the user typed in the text.
    let base = expand_user(&dir, home);
    let mut matches: Vec<String> = if remote {
        match runner.cockpit(&["dirs".to_string(), dir.clone(), prefix.clone()]) {
            Ok(out) => out.lines().filter(|l| !l.is_empty()).map(str::to_string).collect(),
            Err(_) => return false,
        }
    } else {
        match std::fs::read_dir(&base) {
            Ok(rd) => rd
                .filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .filter_map(|e| e.file_name().to_str().map(|s| s.to_string()))
                .filter(|s| s.starts_with(&prefix))
                .collect(),
            Err(_) => Vec::new(),
        }
    };
    if matches.is_empty() {
        return false;
    }
    matches.sort();
    // A single match completes fully and appends '/'. Multiple matches
    // extend to the longest common prefix.
    let ext = if matches.len() == 1 {
        format!("{}/", matches[0])
    } else {
        let refs: Vec<&str> = matches.iter().map(|s| s.as_str()).collect();
        longest_common_prefix(&refs)
    };
    let base_disp = if dir.starts_with("~") { dir } else { base.clone() };
    *text = if base_disp.ends_with('/') {
        format!("{}{}", base_disp, ext)
    } else {
        format!("{}/{}", base_disp, ext)
    };
    *cursor = text.chars().count();
    *text != old
}

fn edit_text(text: &mut String, cursor: &mut usize, code: crossterm::event::KeyCode) {
    let len = text.chars().count();
    match code {
        crossterm::event::KeyCode::Char(c) => {
            text.insert(*cursor, c);
            *cursor += 1;
        }
        crossterm::event::KeyCode::Backspace => {
            if *cursor > 0 {
                text.remove(*cursor - 1);
                *cursor -= 1;
            }
        }
        crossterm::event::KeyCode::Delete => {
            if *cursor < len {
                text.remove(*cursor);
            }
        }
        crossterm::event::KeyCode::Left => {
            if *cursor > 0 {
                *cursor -= 1;
            }
        }
        crossterm::event::KeyCode::Right => {
            if *cursor < len {
                *cursor += 1;
            }
        }
        crossterm::event::KeyCode::Home => *cursor = 0,
        crossterm::event::KeyCode::End => *cursor = len,
        _ => {}
    }
}

fn set_cell(buf: &mut Buffer, x: usize, y: usize, ch: char, fg: Color, bg: Color, bold: bool) -> usize {
    if let Some(cell) = buf.cell_mut((x as u16, y as u16)) {
        cell.set_char(ch);
        let mut st = Style::new().fg(fg).bg(bg);
        if bold {
            st = st.add_modifier(Modifier::BOLD);
        }
        cell.set_style(st);
    }
    x + chw(ch)
}

fn put(buf: &mut Buffer, x: usize, y: usize, s: &str, fg: Color) -> usize {
    let mut xx = x;
    for ch in s.chars() {
        xx = set_cell(buf, xx, y, ch, fg, BG, false);
    }
    xx
}

fn put_trunc(buf: &mut Buffer, x: usize, y: usize, s: &str, fg: Color, max: usize) -> usize {
    put_trunc_on(buf, x, y, s, fg, BG, max)
}

/// `put_trunc` on a given background (inputs, toggles: text must keep the field colour).
fn put_trunc_on(buf: &mut Buffer, x: usize, y: usize, s: &str, fg: Color, bg: Color, max: usize) -> usize {
    let mut xx = x;
    for ch in s.chars().take(max) {
        if xx >= max + x {
            break;
        }
        xx = set_cell(buf, xx, y, ch, fg, bg, false);
    }
    xx
}

fn fill(buf: &mut Buffer, x: usize, y: usize, w: usize, bg: Color) {
    for i in 0..w {
        set_cell(buf, x + i, y, ' ', FG, bg, false);
    }
}

/// A ` label ` chip; returns the column after it.
fn toggle(buf: &mut Buffer, x: usize, y: usize, label: &str, bg: Color, fg: Color, bold: bool) -> usize {
    let width = label.width() + 2;
    fill(buf, x, y, width, bg);
    let mut xx = x + 1;
    for ch in label.chars() {
        xx = set_cell(buf, xx, y, ch, fg, bg, bold);
    }
    x + width
}

/// The `remoteControl` setting from `force settings --json` (SPEC §53): its boolean, else on (no answer, an older
/// Force without the key, anything unexpected).
fn rc_default(out: Option<String>) -> bool {
    out.and_then(|o| serde_json::from_str::<serde_json::Value>(&o).ok())
        .and_then(|v| v.get("remoteControl").and_then(|b| b.as_bool()))
        .unwrap_or(true)
}

/// The kinds `<force-bin> kinds --json` reports, in the dialog's order; claude + shell when it cannot be
/// asked (an older force) or answers nothing usable.
fn parse_kinds(out: Option<String>) -> Vec<String> {
    let known = ["claude", "opencode", "shell"];
    let got: Vec<String> = out
        .and_then(|o| serde_json::from_str::<Vec<String>>(&o).ok())
        .unwrap_or_default()
        .into_iter()
        .filter(|k| known.contains(&k.as_str()))
        .collect();
    let mut kinds: Vec<String> = known.iter().filter(|k| got.iter().any(|g| g == *k)).map(|k| k.to_string()).collect();
    if kinds.is_empty() {
        kinds = vec!["claude".to_string(), "shell".to_string()];
    }
    if !kinds.iter().any(|k| k == "shell") {
        kinds.push("shell".to_string());
    }
    kinds
}

/// Kind chips on the New dialog: (start column, label), one column apart.
fn kind_chips(kinds: &[String]) -> Vec<(usize, &'static str)> {
    let mut x = CONTENT_LEFT;
    kinds
        .iter()
        .map(|k| {
            let label = match k.as_str() {
                "opencode" => "OpenCode",
                "shell" => "Shell",
                _ => "Claude Code",
            };
            let at = x;
            x += label.len() + 2 + 1;
            (at, label)
        })
        .collect()
}

fn show_slice(text: &str, cursor: usize, maxw: usize) -> (String, usize) {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= maxw || maxw == 0 {
        return (text.to_string(), cursor.min(chars.len()));
    }
    let start = if cursor < maxw - 1 {
        0
    } else {
        (cursor - (maxw - 1)).min(chars.len().saturating_sub(maxw))
    };
    let end = (start + maxw).min(chars.len());
    let visible: String = chars[start..end].iter().collect();
    (visible, cursor.saturating_sub(start))
}

fn prefix_width(text: &str, n: usize) -> usize {
    text.chars().take(n).map(chw).sum()
}

fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut line = String::new();
    for word in text.split(' ') {
        if line.is_empty() {
            line.push_str(word);
        } else if line.width() + 1 + word.width() <= width {
            line.push(' ');
            line.push_str(word);
        } else {
            out.push(line.clone());
            line = word.to_string();
        }
    }
    if !line.is_empty() || out.is_empty() {
        out.push(line);
    }
    out
}

fn scroll_top(cursor: usize, visible: usize, len: usize) -> usize {
    if visible == 0 || len == 0 {
        return 0;
    }
    let max_top = len.saturating_sub(visible);
    let top = if cursor >= visible { cursor - visible + 1 } else { 0 };
    top.min(max_top)
}

fn status_parts(tier: Tier, kind: StatusKind) -> (Option<char>, Color) {
    match kind {
        StatusKind::Error => (Some(crate::glyphs::state(tier, '×', '\u{F057}')), ERROR),
        StatusKind::Ok => (Some(crate::glyphs::state(tier, '✓', '\u{F058}')), SUCCESS),
        StatusKind::Warning => (None, WARNING),
        StatusKind::Neutral => (None, FG_DIM),
    }
}

fn glyph_char(tier: Tier, s: State, now_ms: i64) -> char {
    crate::model::glyph(tier, s, now_ms)
}

fn glyph_color(s: State) -> Color {
    match s {
        State::NeedsYou => C_NEEDS_YOU,
        State::Working => WARNING,
        State::YourTurn => C_YOUR_TURN,
        State::Down => C_DOWN,
        _ => FG_DIM,
    }
}

fn split_dir_prefix(path: &str) -> (String, String) {
    match std::path::Path::new(path).parent() {
        Some(p) => (p.to_string_lossy().to_string(), path.to_string()),
        None => ("/".to_string(), path.to_string()),
    }
}

fn longest_common_prefix(v: &[&str]) -> String {
    if v.is_empty() {
        return String::new();
    }
    let mut p: Vec<char> = v[0].chars().collect();
    for s in &v[1..] {
        let chars: Vec<char> = s.chars().collect();
        let mut n = 0;
        while n < p.len() && n < chars.len() && p[n] == chars[n] {
            n += 1;
        }
        p.truncate(n);
    }
    p.into_iter().collect()
}

// --------------------------------------------------------------------------- ranking --------

fn is_subsequence(needle: &str, haystack: &str) -> bool {
    let mut it = haystack.chars();
    needle.chars().all(|c| it.any(|h| h == c))
}

/// Rank `ordered` against a filter: prefix 0, substring 1, subsequence 2, else hidden; stable by position.
pub fn rank_sessions(ordered: &[JumpSession], filter: &str) -> Vec<JumpSession> {
    if filter.trim().is_empty() {
        return ordered.to_vec();
    }
    let low = filter.trim().to_lowercase();
    let mut ranked: Vec<(u8, usize, &JumpSession)> = Vec::new();
    for (pos, s) in ordered.iter().enumerate() {
        let name = s.name.to_lowercase();
        let rank = if name.starts_with(&low) {
            0
        } else if name.contains(&low) {
            1
        } else if is_subsequence(&low, &name) {
            2
        } else {
            continue;
        };
        ranked.push((rank, pos, s));
    }
    ranked.sort_by_key(|(r, p, _)| (*r, *p));
    ranked.into_iter().map(|(_, _, s)| s.clone()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::rc::Rc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    static COUNTER: AtomicUsize = AtomicUsize::new(0);

    pub fn tmp_file(content: &str) -> PathBuf {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let mut p = std::env::temp_dir();
        p.push(format!("cockpit_dialog_t{}_{}.json", std::process::id(), n));
        std::fs::write(&p, content).unwrap();
        p
    }

    #[derive(Default)]
    pub struct Logs {
        pub cockpit: RefCell<Vec<Vec<String>>>,
        pub cockpit_resp: RefCell<VecDeque<Result<String, String>>>,
        pub tmux: RefCell<Vec<Vec<String>>>,
        pub tmux_resp: RefCell<VecDeque<Result<String, String>>>,
        pub has: RefCell<VecDeque<bool>>,
    }

    #[derive(Clone)]
    pub struct FakeRunner {
        pub logs: Rc<Logs>,
    }

    impl FakeRunner {
        pub fn new() -> Self {
            FakeRunner { logs: Rc::new(Logs::default()) }
        }
    }

    impl Runner for FakeRunner {
        fn cockpit(&self, _args: &[String]) -> Result<String, String> {
            self.logs.cockpit.borrow_mut().push(_args.to_vec());
            match self.logs.cockpit_resp.borrow_mut().pop_front() {
                Some(r) => r,
                None => Ok(String::new()),
            }
        }
        fn tmux(&self, args: &[String]) -> Result<String, String> {
            match self.logs.tmux_resp.borrow_mut().pop_front() {
                Some(r) => {
                    self.logs.tmux.borrow_mut().push(args.to_vec());
                    r
                }
                None => {
                    self.logs.tmux.borrow_mut().push(args.to_vec());
                    Ok(String::new())
                }
            }
        }
        fn tmux_ok(&self, _args: &[String]) -> bool {
            self.logs.has.borrow_mut().pop_front().unwrap_or(false)
        }
    }

    pub fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    // ---- validation: names, taken, folders ----------------------------------------------

    #[test]
    fn valid_names() {
        for ok in ["a", "portal", "portal-gen", "a1b-2c", &"x".repeat(31)] {
            assert!(valid_name(ok), "should be valid: {}", ok);
        }
        for bad in ["", "Portal", "portal_", "-portal", "portal-", "a b", "ű", &"x".repeat(32)] {
            assert!(!valid_name(bad), "should be invalid: {}", bad);
        }
    }

    #[test]
    fn taken_picks_name_and_pending() {
        let entries = vec![RegistryEntry {
            name: "x".into(),
            cwd: "/home".into(),
            archived: false,
            archived_at: None,
            pending_name: Some("y".into()),
            kind: "claude".into(),
        }];
        assert!(name_taken(&entries, "x"));
        assert!(name_taken(&entries, "y"));
        assert!(!name_taken(&entries, "z"));
    }

    #[test]
    fn new_folder_must_exist() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        d.name_input = "portal".into();
        d.cwd_input = "/nonexistent/definitely/missing".into();
        d.validate_new();
        assert_eq!(d.status_kind, StatusKind::Error);
        assert_eq!(d.status_text, "folder does not exist");
        // valid folder -> ready
        d.cwd_input = std::env::temp_dir().to_string_lossy().to_string();
        d.validate_new();
        assert_eq!(d.status_kind, StatusKind::Ok);
        assert_eq!(d.status_text, "ready");
    }

    #[test]
    fn rich_dialog_title_icon_and_status_glyphs() {
        // the cards' title: the icon in coral (RICH), the title in white bold, a rule under it; PLAIN: no icon
        let reg = std::path::PathBuf::from("/nonexistent/registry.json");
        let snap = std::path::PathBuf::from("/nonexistent/snapshot.json");
        let mut d = Dialog::new(Kind::Rename, Some("x".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Rich, 64, 11);
        let mut buf = Buffer::empty(ratatui::layout::Rect::new(0, 0, 64, 11));
        d.draw(&mut buf);
        assert_eq!(buf[(2u16, 1u16)].symbol(), "\u{F040}");
        assert_eq!(buf[(2u16, 1u16)].fg, ACCENT);
        assert_eq!(buf[(5u16, 1u16)].symbol(), "R");
        assert_eq!(buf[(5u16, 1u16)].fg, WHITE);
        assert_eq!(buf[(2u16, 2u16)].symbol(), "─");
        assert_eq!(buf[(2u16, 2u16)].fg, RULE);
        // the main action is a coral button on the bottom right, a blank row under it
        let row: String = (0..64u16).map(|x| buf[(x, 9u16)].symbol().to_string()).collect();
        assert!(row.trim_end().ends_with("Enter  Rename    Esc  Cancel") || row.contains(" Enter  Rename "), "{row:?}");
        let ex = row.find("Enter").unwrap() as u16;
        assert_eq!(buf[(ex, 9u16)].bg, ACCENT);
        let last: String = (0..64u16).map(|x| buf[(x, 10u16)].symbol().to_string()).collect();
        assert!(last.trim().is_empty());
        // status ✓/× swap to font icons in RICH
        assert_eq!(status_parts(Tier::Plain, StatusKind::Ok).0, Some('✓'));
        assert_eq!(status_parts(Tier::Rich, StatusKind::Ok).0, Some('\u{F058}'));
        assert_eq!(status_parts(Tier::Rich, StatusKind::Error).0, Some('\u{F057}'));
        // jump list rows use the state glyphs
        assert_eq!(glyph_char(Tier::Rich, State::NeedsYou, 0), '\u{F0F3}');
        assert_eq!(glyph_char(Tier::Plain, State::NeedsYou, 0), '!');
    }

    #[test]
    fn new_taken_and_invalid_messages() {
        let reg = tmp_file(r#"{"sessions":[{"name":"taken","cwd":"/","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        d.name_input = "taken".into();
        d.validate_new();
        assert_eq!(d.status_kind, StatusKind::Error);
        assert!(d.status_text.contains("already taken"));
        d.name_input = "UPPER".into();
        d.validate_new();
        assert_eq!(d.status_kind, StatusKind::Error);
    }

    #[test]
    fn rename_same_name_is_neutral() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Rename, Some("portal".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        d.validate_rename();
        assert_eq!(d.status_text, "type a new name");
        d.rename_input = "portal2".into();
        d.validate_rename();
        assert_eq!(d.status_kind, StatusKind::Ok);
    }

    // ---- jump ranking/ordering ---------------------------------------------------------
    fn jsess(name: &str, state: State, index: Option<i32>) -> JumpSession {
        JumpSession { name: name.into(), state, index }
    }

    #[test]
    fn jump_rank_prefix_substring_subsequence() {
        let ordered = vec![
            jsess("portal", State::Idle, Some(0)),
            jsess("lap", State::Idle, Some(1)),
            jsess("mail", State::Idle, Some(2)),
            jsess("together", State::Idle, Some(3)),
            jsess("alpha", State::Idle, Some(4)),
        ];
        // "al": prefix alpha; substring portal, mail; subsequence lap
        let r: Vec<String> = rank_sessions(&ordered, "al").iter().map(|s| s.name.clone()).collect();
        assert_eq!(r, vec!["alpha", "portal", "mail"]);
        // empty filter returns all in order
        let r = rank_sessions(&ordered, "");
        assert_eq!(r.len(), 5);
    }

    #[test]
    fn jump_ordering_group_and_own_last() {
        let snap_json = r#"{"generated_ms":1,"sessions":[
            {"name":"z","state":"IDLE","index":3,"cwd":"/","archived":false},
            {"name":"a","state":"NEEDS_YOU","index":1,"cwd":"/","archived":false},
            {"name":"b","state":"YOUR_TURN","index":2,"cwd":"/","archived":false},
            {"name":"me","state":"WORKING","index":4,"cwd":"/","archived":false},
            {"name":"gone","state":"WORKING","index":5,"cwd":"/","archived":true}
        ]}"#;
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(snap_json);
        let runner = FakeRunner::new();
        runner.logs.has.borrow_mut().push_back(false);
        let mut d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(runner), Tier::Plain, 64, 14);
        d.own_session = Some("me".into());
        d.load_sessions();
        assert!(d.glyphs);
        let names: Vec<String> = { let _ = &d.jump_ordered; d.jump_ordered.iter().map(|s| s.name.clone()).collect() };
        // NEEDS_YOU first, then YOUR_TURN, then the rest by index, own last
        assert_eq!(names, vec!["a", "b", "z", "me"]);
    }

    #[test]
    fn jump_registry_fallback_no_glyphs() {
        let reg = tmp_file(r#"{"sessions":[{"name":"q","cwd":"/","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"not json"#); // unparseable -> None -> registry fallback
        let runner = FakeRunner::new();
        let d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(runner), Tier::Plain, 64, 14);
        assert!(!d.glyphs);
        assert_eq!(d.jump_ordered.len(), 1);
        assert_eq!(d.jump_ordered[0].name, "q");
    }

    // ---- archived ordering + meta text ---------------------------------------------------
    #[test]
    fn archived_newest_first_and_home_meta() {
        let reg = tmp_file(r#"{"sessions":[
            {"name":"old","cwd":"/home/u/z","archived":true,"archived_at":"2024-01-01T00:00:00Z","created_at":"x","order":0},
            {"name":"new","cwd":"/home/u","archived":true,"archived_at":"2024-03-01T00:00:00Z","created_at":"x","order":0},
            {"name":"live","cwd":"/home/u/y","archived":false,"created_at":"x","order":0}
        ]}"#);
        let _snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let entries = load_registry(&reg);
        let a = archived_sorted(&entries);
        assert_eq!(a.len(), 2);
        assert_eq!(a[0].name, "new");
        assert_eq!(a[1].name, "old");
        // home -> ~
        assert_eq!(display_cwd("/home/u", "/home/u"), "~");
        assert_eq!(display_cwd("/home/u/z", "/home/u"), "~/z");
        assert_eq!(display_cwd("/opt/x", "/home/u"), "/opt/x");
    }

    #[test]
    fn archived_meta_text() {
        assert_eq!(display_cwd("/home/u", "/home/u"), "~");
        assert_eq!(display_cwd("/home/u/proj", "/home/u"), "~/proj");
    }

    // ---- exact argv per action ----------------------------------------------------------
    #[test]
    fn new_action_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.name_input = "portal".into();
        d.cwd_input = "/tmp".into();
        d.new_kind = 0; // claude
        d.action_submit_new();
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["kinds", "--json"], "asks which kinds exist");
        assert_eq!(c[2], vec!["new", "portal", "--cwd", "/tmp", "--no-switch"]);
        let t = runner.logs.tmux.borrow();
        assert_eq!(t[0], vec!["switch-client", "-t", "=portal"]);
    }

    #[test]
    fn new_without_name_is_named_for_you() {
        // SPEC §49.6: an empty name is valid; `force new` without one prints the name it picked
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok(r#"["claude","opencode","shell"]"#.to_string()));
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok("{}".to_string()));   // settings --json (SPEC §53)
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok("new-2".to_string()));
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.name_input = "".into();
        d.cwd_input = "/tmp".into();
        d.new_kind = 0;
        d.validate_new();
        assert_eq!(d.status_kind, StatusKind::Ok);
        assert_eq!(d.status_text, "ready: named after your first message");
        d.action_submit_new();
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[2], vec!["new", "--cwd", "/tmp", "--no-switch"]);
        assert_eq!(runner.logs.tmux.borrow()[0], vec!["switch-client", "-t", "=new-2"]);
    }

    // ---- Remote Control checkbox (SPEC §53) ---------------------------------------------
    fn rc_key(code: crossterm::event::KeyCode, mods: crossterm::event::KeyModifiers) -> crossterm::event::KeyEvent {
        crossterm::event::KeyEvent::new(code, mods)
    }

    fn rc_row(buf: &Buffer) -> String {
        (0..buf.area().width).map(|x| buf.cell((x, NEW_REMOTE_Y as u16)).map(|c| c.symbol().to_string()).unwrap_or_default()).collect()
    }

    #[test]
    fn new_remote_control_checked_by_default_sends_nothing() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        assert!(d.new_remote, "checked by default");
        let mut buf = Buffer::empty(ratatui::layout::Rect::new(0, 0, 64, 17));
        d.draw(&mut buf);
        assert!(rc_row(&buf).contains("✓ Remote Control"), "{:?}", rc_row(&buf));
        let below = (0..buf.area().width).map(|x| buf.cell((x, NEW_REMOTE_Y as u16 + 1)).map(|c| c.symbol().to_string())
            .unwrap_or_default()).collect::<String>();
        assert!(below.trim().is_empty(), "a blank row between the chip and the status line: {below:?}");
        d.name_input = "portal".into();
        d.cwd_input = "/tmp".into();
        d.action_submit_new();
        assert_eq!(runner.logs.cockpit.borrow()[2], vec!["new", "portal", "--cwd", "/tmp", "--no-switch"]);
    }

    #[test]
    fn new_remote_control_unchecked_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.name_input = "portal".into();
        d.cwd_input = "/tmp/".into();     // ends in "/": Tab completes nothing, focus moves on
        // Tab from Name -> Folder -> Remote Control; Space unchecks it; Enter creates
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::NONE), 0);
        assert_eq!(d.new_focus, NewFocus::Folder);
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::NONE), 0);
        assert_eq!(d.new_focus, NewFocus::Remote);
        assert_eq!(d.footer_sections()[1], ("space", "toggle"));
        d.handle_key(rc_key(KeyCode::Char(' '), KeyModifiers::NONE), 0);
        assert!(!d.new_remote);
        let mut buf = Buffer::empty(ratatui::layout::Rect::new(0, 0, 64, 17));
        d.draw(&mut buf);
        assert!(rc_row(&buf).contains("Remote Control") && !rc_row(&buf).contains('✓'), "{:?}", rc_row(&buf));
        d.handle_key(rc_key(KeyCode::Enter, KeyModifiers::NONE), 0);
        assert!(d.done);
        assert_eq!(runner.logs.cockpit.borrow()[2],
                   vec!["new", "portal", "--cwd", "/tmp/", "--no-switch", "--no-remote-control"]);
    }

    #[test]
    fn new_remote_control_ctrl_r_and_click_toggle() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        d.handle_key(rc_key(KeyCode::Char('r'), KeyModifiers::CONTROL), 0);
        assert!(!d.new_remote, "Ctrl+R from the Name field");
        assert_eq!(d.new_focus, NewFocus::Name);
        assert_eq!(d.name_input, "", "Ctrl+R types nothing");
        d.handle_mouse(&crossterm::event::MouseEvent {
            kind: crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left),
            column: 4, row: NEW_REMOTE_Y as u16, modifiers: KeyModifiers::NONE });
        assert!(d.new_remote, "a click on the row toggles it back");
        assert_eq!(d.new_focus, NewFocus::Remote);
    }

    #[test]
    fn new_remote_control_default_from_the_setting() {
        // SPEC §53: the box starts from remoteControl; checked against an "off" default sends --remote-control
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok(r#"["claude","shell"]"#.to_string()));
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok(r#"{"remoteControl": false, "intro": true}"#.to_string()));
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        assert!(!d.new_remote_default && !d.new_remote, "off by the setting: unchecked");
        assert_eq!(runner.logs.cockpit.borrow()[1], vec!["settings", "--json"]);
        d.name_input = "a".into();
        d.cwd_input = "/tmp".into();
        d.action_submit_new();
        assert_eq!(runner.logs.cockpit.borrow()[2], vec!["new", "a", "--cwd", "/tmp", "--no-switch", "--no-remote-control"]);

        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok(r#"["claude","shell"]"#.to_string()));
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok(r#"{"remoteControl": false}"#.to_string()));
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.new_remote = true;                       // turned on against the default
        d.name_input = "b".into();
        d.cwd_input = "/tmp".into();
        d.action_submit_new();
        assert_eq!(runner.logs.cockpit.borrow()[2], vec!["new", "b", "--cwd", "/tmp", "--no-switch", "--remote-control"]);
    }

    #[test]
    fn new_remote_control_default_is_on_without_an_answer() {
        assert!(rc_default(None));
        assert!(rc_default(Some("not json".into())));
        assert!(rc_default(Some(r#"{"intro": true}"#.into())), "an older Force without the key");
        assert!(rc_default(Some(r#"{"remoteControl": "no"}"#.into())), "not a boolean");
        assert!(!rc_default(Some(r#"{"remoteControl": false}"#.into())));
    }

    #[test]
    fn new_shift_tab_goes_back_and_enter_on_an_empty_name_moves_on() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        assert!(d.new_focus == NewFocus::Name);
        d.handle_key(rc_key(KeyCode::BackTab, KeyModifiers::SHIFT), 0);     // how most terminals send Shift+Tab
        assert!(d.new_focus == NewFocus::Kind, "Shift+Tab: back to Kind");
        d.handle_key(rc_key(KeyCode::BackTab, KeyModifiers::SHIFT), 0);
        assert!(d.new_focus == NewFocus::Remote, "and round to the last field");
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::NONE), 0);
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::NONE), 0);
        assert!(d.new_focus == NewFocus::Name);
        d.handle_key(rc_key(KeyCode::Enter, KeyModifiers::NONE), 0);
        assert!(d.new_focus == NewFocus::Folder, "Enter on an empty name moves on (it is named for you)");
        assert!(!d.done, "and creates nothing yet");
    }

    #[test]
    fn new_remote_control_only_for_claude() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.new_remote = false;
        d.new_focus = NewFocus::Remote;
        // Ctrl+T to Shell: the checkbox goes away, focus falls back to Folder, nothing is sent for it
        d.handle_key(rc_key(KeyCode::Char('t'), KeyModifiers::CONTROL), 0);
        assert_eq!(d.new_kind_name(), "shell");
        assert_eq!(d.new_focus, NewFocus::Folder);
        let mut buf = Buffer::empty(ratatui::layout::Rect::new(0, 0, 64, 17));
        d.draw(&mut buf);
        assert!(!rc_row(&buf).contains("Remote Control"));
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::SHIFT), 0);
        d.handle_key(rc_key(KeyCode::Tab, KeyModifiers::NONE), 0);
        assert_ne!(d.new_focus, NewFocus::Remote, "Tab skips it");
        d.name_input = "sh".into();
        d.cwd_input = "/tmp".into();
        d.action_submit_new();
        assert_eq!(runner.logs.cockpit.borrow()[2], vec!["new", "sh", "--cwd", "/tmp", "--no-switch", "--shell"]);
    }

    #[test]
    fn new_shell_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.name_input = "shell1".into();
        d.cwd_input = "/tmp".into();
        d.new_kind = d.new_kinds.iter().position(|k| k == "shell").unwrap();
        d.action_submit_new();
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[2], vec!["new", "shell1", "--cwd", "/tmp", "--no-switch", "--shell"]);
    }

    #[test]
    fn kinds_offered_only_when_installed() {
        assert_eq!(parse_kinds(Some(r#"["claude","opencode","shell"]"#.into())), vec!["claude", "opencode", "shell"]);
        assert_eq!(parse_kinds(Some(r#"["opencode","shell"]"#.into())), vec!["opencode", "shell"]);
        assert_eq!(parse_kinds(Some(r#"["claude"]"#.into())), vec!["claude", "shell"], "shell always");
        assert_eq!(parse_kinds(None), vec!["claude", "shell"], "older force without `kinds`");
        assert_eq!(parse_kinds(Some("garbage".into())), vec!["claude", "shell"]);
        let chips = kind_chips(&parse_kinds(Some(r#"["claude","opencode","shell"]"#.into())));
        assert_eq!(chips.iter().map(|c| c.1).collect::<Vec<_>>(), vec!["Claude Code", "OpenCode", "Shell"]);
        assert!(chips[0].0 < chips[1].0 && chips[1].0 < chips[2].0);
    }

    #[test]
    fn new_opencode_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.new_kinds = vec!["claude".into(), "opencode".into(), "shell".into()];
        d.name_input = "oc1".into();
        d.cwd_input = "/tmp".into();
        d.step_kind(1);
        d.action_submit_new();
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[2], vec!["new", "oc1", "--cwd", "/tmp", "--no-switch", "--kind", "opencode"]);
    }

    #[test]
    fn rename_pending_message() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Rename, Some("old".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 12);
        d.rename_input = "new".into();
        d.validate_rename();
        // tmux has-session returns true -> pending + timer, not done yet
        runner.logs.has.borrow_mut().push_back(true);
        d.action_submit_rename();
        assert!(!d.done);
        assert_eq!(d.status_kind, StatusKind::Ok);
        assert_eq!(d.status_text, "renamed — applies when the session is idle");
        assert!(d.exit_after_ms.is_some());
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["rename", "old", "new"]);
    }

    #[test]
    fn rename_immediate_when_session_gone() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Rename, Some("old".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 12);
        d.rename_input = "new".into();
        d.validate_rename();
        runner.logs.has.borrow_mut().push_back(false);
        d.action_submit_rename();
        assert!(d.done);
        let t = runner.logs.tmux.borrow();
        assert!(t.is_empty()); // tmux_ok doesn't log to tmux log
    }

    #[test]
    fn registry_kind_parses_defaults_to_claude() {
        let reg = tmp_file(r#"{"sessions":[
            {"name":"sh","cwd":"/","kind":"shell","created_at":"x","order":0},
            {"name":"cl","cwd":"/","created_at":"x","order":0},
            {"name":"oc","cwd":"/","kind":"opencode","created_at":"x","order":0}
        ]}"#);
        let entries = load_registry(&reg);
        let kind = |n: &str| entries.iter().find(|e| e.name == n).map(|e| e.kind.clone()).unwrap();
        assert_eq!(kind("sh"), "shell");
        assert_eq!(kind("cl"), "claude", "missing kind defaults to claude");
        assert_eq!(kind("oc"), "opencode");
    }

    #[test]
    fn move_prefilled_with_display_cwd() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/outside/x","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        assert_eq!(d.move_input, display_cwd("/outside/x", &d.home), "prefilled with the session cwd");
        assert_eq!(d.move_cursor, d.move_input.chars().count(), "cursor at the end");
    }

    #[test]
    fn move_unknown_name_empty_field() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let d = Dialog::new(Kind::Move, Some("ghost".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        assert_eq!(d.move_input, "", "unlisted name -> empty field");
        assert_eq!(d.status_text, "type a folder", "open validates the empty field");
    }

    #[test]
    fn move_validation_statuses() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        // empty -> neutral
        d.move_input = "".into();
        d.validate_move();
        assert_eq!((d.status_kind, d.status_text.as_str()), (StatusKind::Neutral, "type a folder"));
        // same folder (with and without trailing '/') -> neutral
        d.move_input = "/home/u/p".into();
        d.validate_move();
        assert_eq!((d.status_kind, d.status_text.as_str()), (StatusKind::Neutral, "type a new folder"));
        d.move_input = "/home/u/p/".into();
        d.validate_move();
        assert_eq!((d.status_kind, d.status_text.as_str()), (StatusKind::Neutral, "type a new folder"));
        // missing folder -> error
        d.move_input = "/nonexistent/definitely/missing".into();
        d.validate_move();
        assert_eq!((d.status_kind, d.status_text.as_str()), (StatusKind::Error, "no such folder"));
        // valid folder -> no status
        d.move_input = std::env::temp_dir().to_string_lossy().to_string();
        d.validate_move();
        assert_eq!(d.status_kind, StatusKind::Neutral);
        assert_eq!(d.status_text, "");
    }

    #[test]
    fn move_enter_valid_runs_and_closes() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 12);
        let target = std::env::temp_dir().join("move_target_dir").to_string_lossy().to_string();
        std::fs::create_dir_all(&target).ok();
        d.move_input = target.clone();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(d.done, "closes on success");
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["move", "p", target.as_str()], "expanded absolute path, no trailing slash");
        let _ = std::fs::remove_dir_all(&target);
    }

    #[test]
    fn move_relative_path_is_taken_from_the_session_folder() {
        let base = std::env::temp_dir().join(format!("move_rel_{}", std::process::id()));
        std::fs::create_dir_all(base.join("sub")).unwrap();
        let basestr = base.to_string_lossy().to_string();
        let reg = tmp_file(&format!(r#"{{"sessions":[{{"name":"p","cwd":"{basestr}","created_at":"x","order":0}}]}}"#));
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 12);
        d.move_input = "sub/".into();
        d.move_cursor = 4;
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(d.done);
        assert_eq!(runner.logs.cockpit.borrow()[0], vec!["move".to_string(), "p".to_string(), format!("{basestr}/sub")]);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn move_enter_invalid_does_not_run() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 12);
        // invalid: same folder
        d.move_input = "/home/u/p".into();
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(!d.done);
        assert!(runner.logs.cockpit.borrow().is_empty(), "invalid Enter must not call the runner");
        // invalid: missing folder
        d.move_input = "/nonexistent/x".into();
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(runner.logs.cockpit.borrow().is_empty(), "missing folder must not call the runner");
    }

    #[test]
    fn move_runner_failure_shows_message() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, dummy_err_runner(), Tier::Plain, 64, 12);
        d.move_input = std::env::temp_dir().to_string_lossy().to_string();
        d.action_submit_move();
        assert!(!d.done, "stays open on error");
        assert_eq!(d.status_kind, StatusKind::Error);
        assert!(d.status_text.contains("boom"));
    }

    #[test]
    fn move_tab_completes_unique_subfolder() {
        let base = std::env::temp_dir().join(format!(
            "cockpit_move_comp_{}_{}",
            std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().subsec_nanos()
        ));
        std::fs::create_dir_all(base.join("unique_target")).unwrap();
        let base_str = base.to_string_lossy().to_string();
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        d.move_input = format!("{}/uniq", base_str);
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.move_input, format!("{}/unique_target/", base_str), "unique subfolder completes with slash");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn move_ctrl_u_clears_field() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        d.move_input = "/other/x".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL), 0);
        assert_eq!(d.move_input, "");
        assert_eq!(d.status_text, "type a folder");
    }

    #[test]
    fn move_esc_cancels() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        d.handle_key(key(KeyCode::Esc), 0);
        assert!(d.done);
        assert_eq!(d.result, 0);
    }

    #[test]
    fn archive_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Archive, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 11);
        d.busy = false;
        d.action_yes();
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["archive", "p"]);
    }

    #[test]
    fn delete_argv_force_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Delete, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 11);
        d.busy = false;
        d.action_yes();
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["delete", "p", "--yes"]);
    }

    #[test]
    fn busy_then_force_flow() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[{"name":"p","state":"WORKING","archived":false}]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Archive, Some("p".into()), &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 11);
        assert!(d.busy);
        // pressing y while busy does NOT run cockpit, shows a force hint, stays open
        d.handle_key(key(KeyCode::Char('y')), 0);
        assert!(!d.done);
        assert!(d.warn_text.contains("busy"));
        assert!(runner.logs.cockpit.borrow().is_empty());
        // f -> forced, then y archives with --force
        d.handle_key(key(KeyCode::Char('f')), 0);
        assert!(d.forced);
        d.handle_key(key(KeyCode::Char('y')), 0);
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["archive", "p", "--force"]);
    }

    #[test]
    fn stderr_error_shown_and_stays_open() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Archive, Some("p".into()), &reg, &snap, dummy_err_runner(), Tier::Plain, 64, 11);
        d.busy = false;
        d.action_yes();
        assert!(!d.done, "stays open on error");
        assert_eq!(d.warn_kind, StatusKind::Warning);
        assert!(d.warn_text.contains("boom"));
    }

    fn dummy_err_runner() -> Box<dyn Runner> {
        struct R;
        impl Runner for R {
            fn cockpit(&self, _a: &[String]) -> Result<String, String> { Err("cockpit: boom".to_string()) }
            fn tmux(&self, _a: &[String]) -> Result<String, String> { Ok(String::new()) }
            fn tmux_ok(&self, _a: &[String]) -> bool { false }
        }
        Box::new(R)
    }

    fn archived_reg(n: usize) -> std::path::PathBuf {
        let rows: Vec<String> = (0..n).map(|i| format!(
            r#"{{"name":"s{i:02}","cwd":"/tmp","archived":true,"archived_at":"2026-11-{:02}T00:00:00Z","created_at":"x","order":0}}"#,
            28 - i)).collect();
        tmp_file(&format!(r#"{{"sessions":[{}]}}"#, rows.join(",")))
    }

    fn screen(d: &mut Dialog) -> Vec<String> {
        let mut buf = Buffer::empty(ratatui::layout::Rect::new(0, 0, d.w as u16, d.h as u16));
        d.draw(&mut buf);
        (0..d.h as u16).map(|y| (0..d.w as u16).map(|x| buf.cell((x, y)).map(|c| c.symbol().to_string())
            .unwrap_or_default()).collect::<String>()).collect()
    }

    #[test]
    fn archived_shows_the_count_and_what_is_out_of_view() {
        // 11 archived in a 72x19 popup: the title counts them, a `↓ N more` row says some are below
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Archived, None, &archived_reg(11), &snap, Box::new(FakeRunner::new()), Tier::Plain, 72, 19);
        let rows = screen(&mut d);
        assert!(rows[1].contains("Archived sessions (11)"), "{:?}", rows[1]);
        let (top, visible, up, down) = d.arch_layout(11);
        assert!((top, up, down) == (0, false, true), "{top} {visible} {up} {down}");
        assert!(rows.iter().any(|r| r.contains(&format!("↓ {} more", 11 - visible))), "{rows:#?}");
        assert!(!rows.iter().any(|r| r.contains("↑")));
        // the wheel moves the selection, and the list follows it
        for _ in 0..10 {
            d.handle_mouse(&wheel(true));
        }
        assert_eq!(d.arch_cursor, 10, "ten notches down: the last entry");
        d.handle_mouse(&wheel(true));
        assert_eq!(d.arch_cursor, 10, "the wheel stops at the end (it does not wrap like the arrow keys)");
        let rows = screen(&mut d);
        let (top, _, up, down) = d.arch_layout(11);
        assert!(up && !down);
        assert!(rows.iter().any(|r| r.contains(&format!("↑ {top} more"))), "{rows:#?}");
        d.handle_mouse(&wheel(false));
        assert_eq!(d.arch_cursor, 9);
    }

    #[test]
    fn archived_more_rows_page_on_click_and_entries_still_hit() {
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Archived, None, &archived_reg(11), &snap, Box::new(FakeRunner::new()), Tier::Plain, 72, 19);
        let (_, visible, _, _) = d.arch_layout(11);
        let down_row = (ARCH_LIST_TOP + 2 * visible) as u16;
        d.handle_mouse(&click(10, down_row));
        assert_eq!(d.arch_cursor, (2 * visible - 1).min(10), "`↓ N more`: a page down (at most the last), nothing restored");
        assert!(!d.done);
        let (top, visible, up, _) = d.arch_layout(11);
        assert!(up);
        d.handle_mouse(&click(10, ARCH_LIST_TOP as u16));
        assert_eq!(d.arch_cursor, top.saturating_sub(visible), "`↑ N more`: a page up");
    }

    #[test]
    fn jump_scrolls_with_the_wheel() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[
            {"name":"a","state":"IDLE","index":1,"cwd":"/"},{"name":"b","state":"IDLE","index":2,"cwd":"/"},
            {"name":"c","state":"IDLE","index":3,"cwd":"/"}]}"#);
        let mut d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let start = d.jump_sel;
        d.handle_mouse(&wheel(true));
        assert_eq!(d.jump_sel, (start + 1).min(d.jump_shown.len() - 1));
        d.handle_mouse(&wheel(false));
        assert_eq!(d.jump_sel, start);
    }

    fn wheel(down: bool) -> crossterm::event::MouseEvent {
        crossterm::event::MouseEvent {
            kind: if down { crossterm::event::MouseEventKind::ScrollDown } else { crossterm::event::MouseEventKind::ScrollUp },
            column: 10, row: 8, modifiers: crossterm::event::KeyModifiers::NONE,
        }
    }

    fn click(col: u16, row: u16) -> crossterm::event::MouseEvent {
        crossterm::event::MouseEvent {
            kind: crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left),
            column: col, row, modifiers: crossterm::event::KeyModifiers::NONE,
        }
    }

    #[test]
    fn archived_restore_and_forget_argv() {
        let reg = tmp_file(r#"{"sessions":[{"name":"old","cwd":"/home/u","archived":true,"archived_at":"2024-01-01T00:00:00Z","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Archived, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 72, 20);
        assert_eq!(d.selected_archived_name().as_deref(), Some("old"));
        d.action_arch_choose();
        assert!(d.done);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["unarchive", "old"]);
        let t = runner.logs.tmux.borrow();
        assert_eq!(t[0], vec!["switch-client", "-t", "=old"]);
    }

    #[test]
    fn archived_forget_then_rebuild() {
        let reg = tmp_file(r#"{"sessions":[{"name":"old","cwd":"/home/u","archived":true,"archived_at":"2024-01-01T00:00:00Z","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Archived, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 72, 20);
        // d -> sets pending confirm, then enter -> forget
        d.handle_key(key(KeyCode::Char('d')), 0);
        assert!(d.pending_forget.is_some());
        assert!(d.status_text.contains("cannot be restored"));
        d.handle_key(key(KeyCode::Enter), 0);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["forget", "old"]);
        assert!(d.pending_forget.is_none());
    }

    #[test]
    fn jump_action_switch_argv() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[{"name":"q","state":"IDLE","index":0,"cwd":"/","archived":false}]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 14);
        d.rebuild_jump();
        assert_eq!(d.jump_shown.len(), 1);
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(d.done);
        let t = runner.logs.tmux.borrow();
        assert_eq!(t[0], vec!["display", "-p", "#{session_name}"]);
        assert_eq!(t[1], vec!["list-clients", "-F", "#{client_session}"]);
        assert_eq!(t[2], vec!["switch-client", "-t", "=q"]);
    }

    #[test]
    fn force_env_client_and_session_in_jump() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[{"name":"q","state":"IDLE","index":0,"cwd":"/","archived":false}]}"#);
        let runner = FakeRunner::new();
        let force = ForceEnv { client: Some("/dev/pts/42"), session: Some("my-sess"), glyphs: Some("plain"), host: None };
        let mut d = Dialog::new_with_env(Kind::Jump, None, &reg, &snap, Box::new(runner.clone()), &force, Tier::Plain, 64, 14);
        // jump's own session comes from FORCE_SESSION, so no `display -p`/`list-clients` own lookup
        assert_eq!(d.own_session.as_deref(), Some("my-sess"));
        d.rebuild_jump();
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(d.done);
        let t = runner.logs.tmux.borrow();
        assert_eq!(t[0], vec!["switch-client", "-c", "/dev/pts/42", "-t", "=q"], "switch-client gets -c from FORCE_CLIENT");
    }
}

#[cfg(test)]
mod layout_tests {
    use super::*;
    use crossterm::event::KeyCode;
    use crossterm::event::KeyEvent;
    use crossterm::event::KeyModifiers;
    use crate::dialog::tests::{FakeRunner, key, tmp_file};

    fn render(d: &mut Dialog, w: u16, h: u16) -> Buffer {
        use ratatui::layout::Rect;
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        d.draw(&mut buf);
        buf
    }

    fn row_text(buf: &Buffer, y: u16) -> String {
        let mut s = String::new();
        for x in 0..buf.area().width {
            s.push_str(buf[(x, y)].symbol());
        }
        s
    }

    fn contains(row: &str, needle: &str) -> bool {
        row.contains(needle)
    }

    #[test]
    fn jump_selected_row_text_on_highlight() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[{"name":"qq","state":"IDLE","index":0,"cwd":"/","archived":false}]}"#);
        let mut d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Rich, 64, 14);
        d.rebuild_jump();
        let buf = render(&mut d, 64, 14);
        let y = (0..14u16).find(|&y| row_text(&buf, y).contains("qq")).expect("row");
        let x = (0..64u16).find(|&x| buf[(x, y)].symbol() == "q").expect("name");
        assert_eq!(buf[(x, y)].bg, BG_SELECTED, "name drawn on the highlight");
        assert_eq!(buf[(CONTENT_LEFT as u16 - 1, y)].bg, BG_SELECTED, "highlight padded one column left");
        // RICH: icon, then a 2-cell slot gap before the name
        assert_eq!(x as usize, CONTENT_LEFT + 3);
    }

    #[test]
    fn new_layout_rows() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 1), "New session"), "title on row 1: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "Kind"), "Kind label on row 3: {:?}", row_text(&buf, 3));
        assert!(contains(&row_text(&buf, 4), "Claude"), "toggles on row 4: {:?}", row_text(&buf, 4));
        assert!(contains(&row_text(&buf, 4), "Shell"), "toggles on row 4: {:?}", row_text(&buf, 4));
        assert!(contains(&row_text(&buf, 6), "Name"), "Name label on row 6: {:?}", row_text(&buf, 6));
        assert!(contains(&row_text(&buf, 7), "▍"), "name input on row 7: {:?}", row_text(&buf, 7));
        assert!(contains(&row_text(&buf, 9), "Folder"), "Folder label on row 9: {:?}", row_text(&buf, 9));
        assert!(contains(&row_text(&buf, 10), "▍"), "folder input on row 10: {:?}", row_text(&buf, 10));
        // the placeholder keeps the input field's background (no seam), the selected chip is raised
        let ph = (0..64u16).find(|&x| buf[(x, 7u16)].symbol() == "e").expect("placeholder");
        assert_eq!(buf[(ph, 7u16)].bg, BG_RAISED);
        let c = (0..64u16).find(|&x| buf[(x, 4u16)].symbol() == "C").expect("Claude chip");
        assert_eq!(buf[(c, 4u16)].bg, BG_SELECTED, "selected chip text on the chip background");
        assert!(contains(&row_text(&buf, 15), "Enter"), "footer above the last row: {:?}", row_text(&buf, 15));
    }

    #[test]
    fn new_initial_focus_name_and_taken_error() {
        let reg = tmp_file(r#"{"sessions":[{"name":"taken","cwd":"/","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        assert_eq!(d.new_focus, NewFocus::Name);
        for c in "taken".chars() {
            d.handle_key(key(KeyCode::Char(c)), 0);
        }
        assert_eq!(d.name_input, "taken");
        assert_eq!(d.status_kind, StatusKind::Error);
        assert!(d.status_text.contains("taken"), "taken error: {:?}", d.status_text);
        // ctrl+u clears the name field
        d.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL), 0);
        assert_eq!(d.name_input, "");
    }

    #[test]
    fn folder_tab_completion() {
        let base = std::env::temp_dir().join(format!(
            "cockpit_comp_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .subsec_nanos()
        ));
        // <base>/d1/{sub1,sub2}  and  <base>/d2/{target}
        let d1 = base.join("d1");
        std::fs::create_dir_all(d1.join("sub1")).unwrap();
        std::fs::create_dir_all(d1.join("sub2")).unwrap();
        let d2 = base.join("d2");
        std::fs::create_dir_all(d2.join("target")).unwrap();
        let base_str = base.to_string_lossy().to_string();

        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::New, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        d.new_focus = NewFocus::Folder;

        // Multiple matches -> extend to longest common prefix; focus stays in Folder.
        d.cwd_input = format!("{}/d1/su", base_str);
        d.cwd_cursor = d.cwd_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.cwd_input, format!("{}/d1/sub", base_str), "lcp extend");
        assert_eq!(d.new_focus, NewFocus::Folder, "focus stays in Folder after completion");

        // Single match -> completes fully and appends '/'.
        d.cwd_input = format!("{}/d2/t", base_str);
        d.cwd_cursor = d.cwd_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.cwd_input, format!("{}/d2/target/", base_str), "single match + slash");
        assert_eq!(d.new_focus, NewFocus::Folder, "focus stays in Folder");

        // No matches -> nothing completes -> focus cycles on.
        d.cwd_input = format!("{}/d1/zz", base_str);
        d.cwd_cursor = d.cwd_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.cwd_input, format!("{}/d1/zz", base_str), "no match leaves text");
        assert_eq!(d.new_focus, NewFocus::Remote, "no match cycles focus (on to Remote Control, SPEC §53)");

        // Path ending in '/' -> nothing completes -> focus cycles on.
        d.new_focus = NewFocus::Folder;
        d.cwd_input = format!("{}/d1/", base_str);
        d.cwd_cursor = d.cwd_input.chars().count();
        let before = d.cwd_input.clone();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.cwd_input, before, "trailing slash leaves text");
        assert_eq!(d.new_focus, NewFocus::Remote, "trailing slash cycles focus");

        // Empty text -> nothing completes -> focus cycles on.
        d.new_focus = NewFocus::Folder;
        d.cwd_input = String::new();
        d.cwd_cursor = 0;
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.new_focus, NewFocus::Remote, "empty text cycles focus");

        // ~ expansion matches, but what the user typed (~) is kept in the text.
        d.home = base_str.clone();
        d.new_focus = NewFocus::Folder;
        d.cwd_input = "~/d1/su".to_string();
        d.cwd_cursor = d.cwd_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.cwd_input, "~/d1/sub", "~ expansion matches, typed text kept");
        assert_eq!(d.new_focus, NewFocus::Folder, "focus stays in Folder after ~ completion");

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn rename_layout_rows() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Rename, Some("x".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 1), "Rename x"), "title on row 1: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "New name"), "label on row 3: {:?}", row_text(&buf, 3));
        assert!(contains(&row_text(&buf, 4), "▍"), "input on row 4: {:?}", row_text(&buf, 4));
        assert!(contains(&row_text(&buf, 15), "Enter  Rename"), "footer: {:?}", row_text(&buf, 15));
    }

    #[test]
    fn move_layout_rows_and_no_warning_for_claude() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        d.move_input = "/other/x".into();
        let buf = render(&mut d, 64, 12);
        assert!(contains(&row_text(&buf, 1), "Move p"), "title on row 1: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "Folder"), "label on row 3: {:?}", row_text(&buf, 3));
        assert!(contains(&row_text(&buf, 4), "▍"), "input on row 4: {:?}", row_text(&buf, 4));
        assert!(contains(&row_text(&buf, 10), "Enter  Move"), "footer: {:?}", row_text(&buf, 10));
        assert!(contains(&row_text(&buf, 10), "Tab  Complete"), "footer: {:?}", row_text(&buf, 10));
        assert!(contains(&row_text(&buf, 10), "Esc  Cancel"), "footer: {:?}", row_text(&buf, 10));
        assert!(!contains(&row_text(&buf, 6), "shell restarts"), "claude: no warning: {:?}", row_text(&buf, 6));
    }

    #[test]
    fn move_shell_warning_fits_and_keeps_footer() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/home/u/p","kind":"shell","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Move, Some("p".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 12);
        let buf = render(&mut d, 64, 12);
        assert!(contains(&row_text(&buf, 6), "the shell restarts in the new folder"), "shell warning: {:?}", row_text(&buf, 6));
        assert!(contains(&row_text(&buf, 10), "Enter  Move"), "footer preserved: {:?}", row_text(&buf, 10));
        assert!(!contains(&row_text(&buf, 10), "shell restarts"), "warning must not overlap the footer");
        // status lands below the warning
        d.move_input = "/nonexistent/x".into();
        d.validate_move();
        let buf = render(&mut d, 64, 12);
        assert!(contains(&row_text(&buf, 7), "no such folder"), "status on row 7 below warning: {:?}", row_text(&buf, 7));
    }

    #[test]
    fn archive_busy_layout_rows() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[{"name":"x","state":"WORKING","archived":false}]}"#);
        let mut d = Dialog::new(Kind::Archive, Some("x".into()), &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 1), "Archive x?"), "title: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "Stops the session"), "body on row 3: {:?}", row_text(&buf, 3));
        assert!(d.busy);
        assert!(contains(&row_text(&buf, 15), "F  Force"), "busy footer: {:?}", row_text(&buf, 15));
        // body must not reach the footer row
        assert!(!contains(&row_text(&buf, 15), "Stops the session"), "body overlapping footer: {:?}", row_text(&buf, 15));
    }

    #[test]
    fn archived_layout_and_meta() {
        let reg = tmp_file(r#"{"sessions":[{"name":"old","cwd":"/home/u/proj","archived":true,"archived_at":"2025-10-09T12:00:00+00:00","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Archived, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 1), "Archived sessions"), "title: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "old"), "name on row 3: {:?}", row_text(&buf, 3));
        assert!(contains(&row_text(&buf, 4), "archived 2025-10-09 · "), "meta: {:?}", row_text(&buf, 4));
        assert!(contains(&row_text(&buf, 15), "Enter  Restore"), "footer: {:?}", row_text(&buf, 15));
    }

    #[test]
    fn archived_integer_archived_at_no_panic() {
        let reg = tmp_file(r#"{"sessions":[{"name":"old","cwd":"/home/u","archived":true,"archived_at":12345,"created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let mut d = Dialog::new(Kind::Archived, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 4), "archived  · "), "integer archived_at -> empty date: {:?}", row_text(&buf, 4));
    }

    #[test]
    fn archived_forget_removes_row() {
        let reg = tmp_file(r#"{"sessions":[{"name":"old","cwd":"/home/u","archived":true,"archived_at":"2025-10-09T12:00:00+00:00","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new(Kind::Archived, None, &reg, &snap, Box::new(runner.clone()), Tier::Plain, 64, 17);
        d.handle_key(key(KeyCode::Char('d')), 0);
        d.handle_key(key(KeyCode::Enter), 0);
        let c = runner.logs.cockpit.borrow();
        assert_eq!(c[0], vec!["forget", "old"]);
        assert!(d.pending_forget.is_none());
        assert!(archived_sorted(&d.entries).is_empty(), "row removed even though the fake force left the file intact");
        let _ = render(&mut d, 64, 17); // rebuild draws empty state without panic
    }

    #[test]
    fn jump_layout_rows() {
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":1,"sessions":[{"name":"q","state":"IDLE","index":0,"cwd":"/","archived":false}]}"#);
        let mut d = Dialog::new(Kind::Jump, None, &reg, &snap, Box::new(FakeRunner::new()), Tier::Plain, 64, 17);
        let buf = render(&mut d, 64, 17);
        assert!(contains(&row_text(&buf, 1), "Jump to session"), "title: {:?}", row_text(&buf, 1));
        assert!(contains(&row_text(&buf, 3), "▍"), "filter input on row 3: {:?}", row_text(&buf, 3));
        assert!(contains(&row_text(&buf, 15), "Enter  Jump"), "footer: {:?}", row_text(&buf, 15));
        assert!(contains(&row_text(&buf, 15), "Esc  Close"), "footer: {:?}", row_text(&buf, 15));
    }

    #[test]
    fn real_runner_host_prefix() {
        let r = RealRunner::new("force".into(), "force".into()).with_host_prefix(Some("ssh -S /tmp/c h tmux -L force"));
        // through ssh the arguments are quoted for the remote shell
        let (bin, argv) = r.tmux_argv(&["switch-client".to_string(), "-t".to_string(), "=s1".to_string()]);
        assert_eq!(bin, "ssh");
        assert_eq!(argv, ["-S", "/tmp/c", "h", "tmux", "-L", "force", "'switch-client'", "'-t'", "'=s1'"]);
        let r = RealRunner::new("force".into(), "force".into()).with_host_prefix(Some(""));
        let (bin, argv) = r.tmux_argv(&["ls".to_string()]);
        assert_eq!((bin.as_str(), argv), ("tmux", vec!["-L".to_string(), "force".to_string(), "ls".to_string()]));
    }

    #[test]
    fn remote_host_skips_local_folder_checks() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/remote/only/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let force = ForceEnv { client: None, session: None, glyphs: None, host: Some("devbox") };
        let mut d = Dialog::new_with_env(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()), &force, Tier::Plain, 64, 12);
        d.move_input = "/remote/only/other".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Enter), 0);
        assert!(d.done, "a folder that exists only on the remote is accepted");
        assert_eq!(runner.logs.cockpit.borrow()[0], vec!["move", "p", "/remote/only/other"]);
    }

    // -- remote Tab-completion via `force dirs` -------------------------------------------

    fn remote_move_dialog(runner: &FakeRunner) -> Dialog {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/remote/p","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let force = ForceEnv { client: None, session: None, glyphs: None, host: Some("devbox") };
        Dialog::new_with_env(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()), &force, Tier::Plain, 64, 12)
    }

    #[test]
    fn remote_move_single_match_completes_to_slash() {
        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok("proj\n".to_string()));
        let mut d = remote_move_dialog(&runner);
        d.move_input = "~/src/pr".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.move_input, "~/src/proj/");
        assert_eq!(runner.logs.cockpit.borrow()[0], vec!["dirs", "~/src", "pr"]);
    }

    #[test]
    fn remote_move_multiple_matches_extend_to_lcp() {
        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Ok("proj-a\nproj-b\n".to_string()));
        let mut d = remote_move_dialog(&runner);
        d.move_input = "~/src/pr".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.move_input, "~/src/proj-");
        assert_eq!(runner.logs.cockpit.borrow()[0], vec!["dirs", "~/src", "pr"]);
    }

    #[test]
    fn remote_completion_error_leaves_text_and_new_cycles_focus() {
        // Move: a failed `dirs` leaves the text untouched (return false, no completion).
        let runner = FakeRunner::new();
        runner.logs.cockpit_resp.borrow_mut().push_back(Err("cockpit: boom".to_string()));
        let mut d = remote_move_dialog(&runner);
        d.move_input = "~/src/pr".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(d.move_input, "~/src/pr", "unchanged text on error");

        // New: a failed `dirs` behaves like nothing completing -> focus moves on (to Remote Control).
        let reg = tmp_file(r#"{"sessions":[]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let force = ForceEnv { client: None, session: None, glyphs: None, host: Some("devbox") };
        let runner2 = FakeRunner::new();
        runner2.logs.cockpit_resp.borrow_mut().push_back(Err("cockpit: boom".to_string()));
        let mut nd = Dialog::new_with_env(Kind::New, None, &reg, &snap, Box::new(runner2.clone()), &force, Tier::Plain, 64, 17);
        nd.new_focus = NewFocus::Folder;
        nd.cwd_input = "~/src/pr".into();
        nd.cwd_cursor = nd.cwd_input.chars().count();
        nd.handle_key(key(KeyCode::Tab), 0);
        assert_eq!(nd.cwd_input, "~/src/pr", "unchanged text on error");
        assert_eq!(nd.new_focus, NewFocus::Remote, "focus cycles on as when nothing completes");
    }

    #[test]
    fn local_dialog_never_calls_dirs() {
        let reg = tmp_file(r#"{"sessions":[{"name":"p","cwd":"/tmp","created_at":"x","order":0}]}"#);
        let snap = tmp_file(r#"{"generated_ms":0,"sessions":[]}"#);
        let runner = FakeRunner::new();
        let mut d = Dialog::new_with_env(Kind::Move, Some("p".into()), &reg, &snap, Box::new(runner.clone()),
                                        &ForceEnv::default(), Tier::Plain, 64, 12);
        d.move_input = "/tmp/ex".into();
        d.move_cursor = d.move_input.chars().count();
        d.handle_key(key(KeyCode::Tab), 0);
        assert!(runner.logs.cockpit.borrow().iter().all(|a| a.first().map(String::as_str) != Some("dirs")),
                "a local dialog must complete from the file system, never `force dirs`: {:?}", runner.logs.cockpit.borrow());
    }
}
