//! Thin wrapper over the `tmux` CLI, plus a trait so tests can fake it (behavioural reference:
//! force/tmux.py). Every invocation is `tmux -S <socket> args...`, stdin null, capturing stdout.
//! Anything that can fail returns a `Result` so the UI never panics on tmux errors.

use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// A client attached to the server (`tmux list-clients`).
#[derive(Clone, Debug, PartialEq)]
pub struct Client {
    pub name: String,
    pub session: String,
    pub activity: i64,
}

/// The tmux interface the app talks to. Tests implement this to record argv and return canned data.
pub trait Tmux: Send {
    /// Run `tmux -S <socket> <args>` and return stdout (trimmed) or a stderr error string.
    fn run(&self, args: &[String]) -> Result<String, String>;
}

/// Real implementation running the `tmux` binary on a socket.
pub struct RealTmux {
    socket: String,
}

impl RealTmux {
    pub fn new(socket: String) -> Self {
        Self { socket }
    }
}

impl Tmux for RealTmux {
    fn run(&self, args: &[String]) -> Result<String, String> {
        let mut cmd = Command::new("tmux");
        cmd.arg("-S").arg(&self.socket);
        cmd.args(args);
        cmd.stdin(Stdio::null());
        let out = cmd.output().map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
        }
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    }
}

/// How long a host tmux command may run before it is killed and reported as unresponsive. A hung link (e.g. an
/// ssh connection that never answers) must not block the sidebar/header threads for the 45 s+ ssh would take to give
/// up, so every host command is bounded by this.
pub const HOST_TMUX_TIMEOUT: Duration = Duration::from_secs(8);

/// The HOST tmux handle in split (ui) mode: every command runs `<@force_host_tmux words> <args…>`
/// instead of `tmux -S <socket>`. `@force_host_tmux` is a whitespace-separated argv prefix, e.g.
/// `tmux -L force` (later `ssh -S /path host tmux -L force`); empty falls back to `tmux -L force`.
pub struct HostTmux {
    prefix: Vec<String>,
    timeout: Duration,
}

impl HostTmux {
    /// Build from the raw `@force_host_tmux` window-option value (empty -> `tmux -L force`).
    pub fn from_option(v: &str) -> Self {
        let words: Vec<String> = v.split_whitespace().map(str::to_string).collect();
        let prefix = if words.is_empty() { vec!["tmux".to_string(), "-L".to_string(), "force".to_string()] } else { words };
        HostTmux { prefix, timeout: HOST_TMUX_TIMEOUT }
    }

    /// Override the timeout (used by tests).
    pub fn with_timeout(mut self, t: Duration) -> Self {
        self.timeout = t;
        self
    }

    /// The full argv (prefix + `args`). Through ssh the remote shell re-splits the command line (and a
    /// `#{…}` word would start a comment there), so the tmux arguments are shell-quoted then.
    pub fn full_argv(&self, args: &[String]) -> Vec<String> {
        let mut v = self.prefix.clone();
        v.extend(remote_args(&self.prefix, args));
        v
    }
}

/// `args` as they must follow `prefix`: shell-quoted when the prefix goes through ssh (SPEC §40).
pub fn remote_args(prefix: &[String], args: &[String]) -> Vec<String> {
    let via_ssh = prefix
        .first()
        .and_then(|p| std::path::Path::new(p).file_name())
        .map_or(false, |n| n == "ssh");
    if via_ssh {
        args.iter().map(|a| shell_quote(a)).collect()
    } else {
        args.to_vec()
    }
}

impl Tmux for HostTmux {
    fn run(&self, args: &[String]) -> Result<String, String> {
        let mut cmd = Command::new(&self.prefix[0]);
        cmd.args(&self.prefix[1..]);
        cmd.args(remote_args(&self.prefix, args));
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());
        let mut child = cmd.spawn().map_err(|e| e.to_string())?;

        // Read stdout and stderr on two threads so a big output can never block the child on a full pipe while we
        // are polling for its exit.
        let mut stdout = child.stdout.take().expect("stdout piped");
        let mut stderr = child.stderr.take().expect("stderr piped");
        let out_thread = std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = stdout.read_to_end(&mut buf);
            buf
        });
        let err_thread = std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = stderr.read_to_end(&mut buf);
            buf
        });

        let deadline = Instant::now() + self.timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Some(status),
                Ok(None) => {}
                Err(e) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = out_thread.join();
                    let _ = err_thread.join();
                    return Err(e.to_string());
                }
            }
            if Instant::now() >= deadline {
                // give up on the host: kill the command and report it rather than blocking the caller forever
                let _ = child.kill();
                let _ = child.wait();
                let _ = out_thread.join();
                let _ = err_thread.join();
                return Err(format!("no answer from the host in {} s", self.timeout.as_secs().max(1)));
            }
            std::thread::sleep(Duration::from_millis(10));
        };

        let out = out_thread.join().unwrap_or_default();
        let err = err_thread.join().unwrap_or_default();
        match status {
            Some(status) if status.success() => Ok(String::from_utf8_lossy(&out).trim().to_string()),
            _ => Err(String::from_utf8_lossy(&err).trim().to_string()),
        }
    }
}

// --- high-level helpers (all built on `run`, so the fake records exact argv) --------------------

/// `tmux display -p -t <pane> '#{session_name}'` -> session name or None.
pub fn session_name(t: &dyn Tmux, pane: &str) -> Option<String> {
    let args: Vec<String> = ["display", "-p", "-t", pane, "#{session_name}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).ok().filter(|s| !s.is_empty())
}

/// `tmux display -p -t <pane> '#{@force_glyphs}'` -> the own session's glyph tier option or None.
pub fn glyphs_option(t: &dyn Tmux, pane: &str) -> Option<String> {
    pane_option(t, pane, "#{@force_glyphs}")
}

/// `tmux display -p -t <pane> '<format>'` -> the value of a pane/window option (window options are
/// readable from any pane of the window) or None. Used for the split-mode window options `@force_nested` and
/// `@force_host_tmux`.
pub fn pane_option(t: &dyn Tmux, pane: &str, format: &str) -> Option<String> {
    let args: Vec<String> = ["display", "-p", "-t", pane, format]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).ok().filter(|s| !s.is_empty())
}

/// `tmux set-option -w -t <pane> <name> <value>`, the window-option publish to the ui server.
pub fn set_window_option(t: &dyn Tmux, pane: &str, name: &str, value: &str) -> Result<(), String> {
    let args: Vec<String> = ["set-option", "-w", "-t", pane, name, value]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).map(|_| ())
}

/// `tmux list-panes -t <window> -F '#{@force_role}'` contains `role` (a sidebar pane present?).
pub fn window_has_pane_role(t: &dyn Tmux, win: &str, role: &str) -> bool {
    let args: Vec<String> = ["list-panes", "-t", win, "-F", "#{@force_role}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    match t.run(&args) {
        Ok(out) => out.lines().any(|l| l == role),
        Err(_) => false,
    }
}

/// `tmux display -p -t <pane> '#{pane_active}'` == "1".
/// Pids of the panes in `pane`'s window with `@force_role` == `role` whose process is a Force program (`force-*`): one
/// still starting (the `force` launcher before it execs) would be killed by SIGUSR1, and reads everything at start.
pub fn window_role_pids(t: &dyn Tmux, pane: &str, role: &str) -> Vec<i32> {
    let args = ["list-panes", "-t", pane, "-F", "#{@force_role}\t#{pane_current_command}\t#{pane_pid}"];
    let out = t.run(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap_or_default();
    out.lines()
        .filter_map(|l| {
            let mut f = l.split('\t');
            let (r, cmd, pid) = (f.next()?, f.next()?, f.next()?);
            (r == role && cmd.starts_with("force-")).then(|| pid.parse().ok()).flatten()
        })
        .collect()
}

pub fn pane_active(t: &dyn Tmux, pane: &str) -> bool {
    let args: Vec<String> = ["display", "-p", "-t", pane, "#{pane_active}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).map(|s| s == "1").unwrap_or(false)
}

/// `tmux list-clients -F ...` -> clients with activity (empty on failure).
pub fn clients(t: &dyn Tmux) -> Vec<Client> {
    let fmt = "#{client_name}\t#{client_session}\t#{client_activity}";
    let args: Vec<String> = ["list-clients", "-F", fmt].iter().map(|s| s.to_string()).collect();
    match t.run(&args) {
        Ok(out) => out
            .lines()
            .filter_map(|l| {
                let mut it = l.split('\t');
                let name = it.next()?.to_string();
                let session = it.next()?.to_string();
                let activity = it.next().unwrap_or("0").parse::<i64>().unwrap_or(0);
                Some(Client { name, session, activity })
            })
            .collect(),
        Err(_) => Vec::new(),
    }
}

/// A clickable cell rectangle (pane-relative, 0-based, end-exclusive) that a pane publishes so a tmux
/// `MouseDown1Pane` binding can open the menu itself. `kind` names which menu (`controls` | `hosts`).
#[derive(Clone, Debug, PartialEq)]
pub struct MenuHit {
    pub kind: &'static str,
    pub x0: usize,
    pub x1: usize,
    pub y0: usize,
    pub y1: usize,
}

/// Publish the menu hit as the five pane options `@force_menu_kind`, `@force_menu_x0`, `@force_menu_x1`,
/// `@force_menu_y0`, `@force_menu_y1` in ONE `set-option -p -t <pane> <name> <value>` invocation, the
/// commands separated by a lone `";"` argument (tmux's argv command separator) so the binding reads a consistent
/// rectangle. `None` sets `kind` and all four numbers to empty strings (no menu).
pub fn set_menu_hit(t: &dyn Tmux, pane: &str, hit: Option<&MenuHit>) -> Result<(), String> {
    let (kind, x0, x1, y0, y1) = match hit {
        Some(h) => (h.kind.to_string(), h.x0.to_string(), h.x1.to_string(), h.y0.to_string(), h.y1.to_string()),
        None => (String::new(), String::new(), String::new(), String::new(), String::new()),
    };
    let args = vec![
        "set-option".to_string(), "-p".to_string(), "-t".to_string(), pane.to_string(), "@force_menu_kind".to_string(), kind,
        ";".to_string(),
        "set-option".to_string(), "-p".to_string(), "-t".to_string(), pane.to_string(), "@force_menu_x0".to_string(), x0,
        ";".to_string(),
        "set-option".to_string(), "-p".to_string(), "-t".to_string(), pane.to_string(), "@force_menu_x1".to_string(), x1,
        ";".to_string(),
        "set-option".to_string(), "-p".to_string(), "-t".to_string(), pane.to_string(), "@force_menu_y0".to_string(), y0,
        ";".to_string(),
        "set-option".to_string(), "-p".to_string(), "-t".to_string(), pane.to_string(), "@force_menu_y1".to_string(), y1,
    ];
    t.run(&args).map(|_| ())
}

/// `tmux switch-client -c <client> -t =<target>`.
pub fn switch_client(t: &dyn Tmux, client: &str, target: &str) -> Result<(), String> {
    let args: Vec<String> = ["switch-client", "-c", client, "-t", &format!("={}", target)]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).map(|_| ())
}

/// The pre-rebrand `@force_role` value of the main pane, still carried by panes of live tmux servers.
const LEGACY_AGENT_ROLE: &str = "claude";

/// `tmux list-panes -s -t =<session> -F '#{@force_role}\t#{pane_id}'` -> id of the agent pane.
pub fn agent_pane(t: &dyn Tmux, session: &str) -> Option<String> {
    let fmt = "#{@force_role}\t#{pane_id}";
    let args: Vec<String> = ["list-panes", "-s", "-t", &format!("={}", session), "-F", fmt]
        .iter()
        .map(|s| s.to_string())
        .collect();
    match t.run(&args) {
        Ok(out) => out
            .lines()
            .find(|l| {
                let role = l.split('\t').next().unwrap_or("");
                role == "agent" || role == LEGACY_AGENT_ROLE
            })
            .and_then(|l| l.split('\t').nth(1).map(str::to_string)),
        Err(_) => None,
    }
}

/// `tmux select-pane -t <pane>`.
pub fn select_pane(t: &dyn Tmux, pane: &str) -> Result<(), String> {
    let args: Vec<String> = ["select-pane", "-t", pane].iter().map(|s| s.to_string()).collect();
    t.run(&args).map(|_| ())
}

/// `tmux display -p '#{socket_path}'` -> socket path or None.
pub fn socket_path(t: &dyn Tmux) -> Option<String> {
    let args: Vec<String> = ["display", "-p", "#{socket_path}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).ok().filter(|s| !s.is_empty())
}

/// `tmux display -p -t <pane> '#{window_id}'` -> the pane's window id or None.
pub fn window_id(t: &dyn Tmux, pane: &str) -> Option<String> {
    let args: Vec<String> = ["display", "-p", "-t", pane, "#{window_id}"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).ok().filter(|s| !s.is_empty())
}

/// Open the Force menu for `client`: `send-keys -K -c <client> C-Space`, as if the terminal typed the menu key, so
/// the menu comes from the same tmux config as the keyboard's (SPEC §44).
pub fn open_menu(t: &dyn Tmux, client: &str) -> Result<(), String> {
    let args: Vec<String> = ["send-keys", "-K", "-c", client, "C-Space"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).map(|_| ())
}

/// `tmux detach-client -t <client>`.
pub fn detach_client(t: &dyn Tmux, client: &str) -> Result<(), String> {
    let args: Vec<String> = ["detach-client", "-t", client]
        .iter()
        .map(|s| s.to_string())
        .collect();
    t.run(&args).map(|_| ())
}

/// `tmux display-popup -E -w W -h H [-c client] [-e K=V]… <command>`. In split (ui) mode `env`
/// carries the FORCE_CLIENT / FORCE_SESSION / FORCE_GLYPHS pairs; classic mode passes an empty slice so the
/// argv (and the existing argv tests) stay byte-identical.
pub fn popup(t: &dyn Tmux, client: Option<&str>, env: &[(String, String)], w: u32, h: u32, command: &str) -> Result<(), String> {
    let mut args: Vec<String> = vec!["display-popup".into(), "-E".into(), "-w".into(), format!("{}", w), "-h".into(), format!("{}", h)];
    if let Some(c) = client {
        args.push("-c".into());
        args.push(c.into());
    }
    for (k, v) in env {
        args.push("-e".into());
        args.push(format!("{}={}", k, v));
    }
    args.push(command.into());
    t.run(&args).map(|_| ())
}

/// Chuck's card popup (SPEC §50): the bars' surface (#212121) and a rounded light grey border, as the menu's
/// (force/cards.py POPUP_STYLE).
pub const CHUCK_POPUP_STYLE: [&str; 6] = ["-b", "rounded", "-S", "fg=#4e4e4e,bg=#212121", "-s", "bg=#212121"];

/// `popup` with extra display-popup options before the command (e.g. CHUCK_POPUP_STYLE).
pub fn popup_with(t: &dyn Tmux, client: Option<&str>, env: &[(String, String)], w: u32, h: u32, extra: &[&str],
                  command: &str) -> Result<(), String> {
    let mut args: Vec<String> = vec!["display-popup".into(), "-E".into(), "-w".into(), format!("{}", w), "-h".into(), format!("{}", h)];
    if let Some(c) = client {
        args.push("-c".into());
        args.push(c.into());
    }
    for (k, v) in env {
        args.push("-e".into());
        args.push(format!("{}={}", k, v));
    }
    args.extend(extra.iter().map(|s| s.to_string()));
    args.push(command.into());
    t.run(&args).map(|_| ())
}

/// Single-quote `s` the way a POSIX shell does (for the popup command argument).
pub fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_args_quoted_through_ssh() {
        let h = HostTmux::from_option("ssh -o X=1 host tmux -L force");
        let argv = h.full_argv(&["list-clients".to_string(), "-F".to_string(), "#{client_name}\t#{client_session}".to_string()]);
        assert_eq!(argv[..6], ["ssh", "-o", "X=1", "host", "tmux", "-L"]);
        assert_eq!(argv.last().unwrap(), "'#{client_name}\t#{client_session}'");
        let local = HostTmux::from_option("tmux -L force");
        assert_eq!(local.full_argv(&["-F".to_string(), "#{x}".to_string()]), ["tmux", "-L", "force", "-F", "#{x}"]);
    }

    #[test]
    fn shell_quotes() {
        assert_eq!(shell_quote("abc"), "'abc'");
        assert_eq!(shell_quote("a'b"), "'a'\\''b'");
        assert_eq!(shell_quote("/bin/true"), "'/bin/true'");
    }

    #[test]
    fn host_tmux_prefix_from_option() {
        // empty option falls back to `tmux -L force`
        let h = HostTmux::from_option("");
        assert_eq!(h.full_argv(&["switch-client".into(), "-c".into(), "/dev/pts/42".into()]),
            ["tmux", "-L", "force", "switch-client", "-c", "/dev/pts/42"]);
        // a multi-word ssh prefix is preserved word-by-word
        let h = HostTmux::from_option("ssh -S /tmp/x h tmux -L force");
        assert_eq!(h.full_argv(&["switch-client".into(), "-c".into(), "c".into(), "-t".into(), "=s1".into()]),
            ["ssh", "-S", "/tmp/x", "h", "tmux", "-L", "force", "'switch-client'", "'-c'", "'c'", "'-t'", "'=s1'"]);
        // simple explicit prefix
        let h = HostTmux::from_option("tmux -L abc");
        assert_eq!(h.full_argv(&["list-clients".into(), "-F".into(), "x".into()]),
            ["tmux", "-L", "abc", "list-clients", "-F", "x"]);
    }

    #[test]
    fn host_tmux_echo_and_false() {
        let h = HostTmux::from_option("echo");
        assert_eq!(h.run(&["hi".to_string()]), Ok("hi".to_string()));
        let f = HostTmux::from_option("false");
        assert!(f.run(&[]).is_err(), "non-zero exit is an Err");
    }

    #[test]
    fn host_tmux_times_out_and_kills() {
        // `sleep 5` would block for 5 s; with a 200 ms timeout it is killed and reported quickly.
        let h = HostTmux::from_option("sleep").with_timeout(Duration::from_millis(200));
        let start = Instant::now();
        let r = h.run(&["5".to_string()]);
        let err = r.expect_err("expected a timeout error");
        assert!(err.contains("no answer"), "error message: {:?}", err);
        assert!(start.elapsed() < Duration::from_secs(2), "must return well before `sleep 5` finishes");
    }

    #[test]
    fn host_tmux_large_output_not_truncated() {
        // 300000 zero bytes from /dev/zero are not whitespace, so trimming leaves them all in place.
        let h = HostTmux::from_option("head").with_timeout(HOST_TMUX_TIMEOUT);
        let out = h.run(&["-c".to_string(), "300000".to_string(), "/dev/zero".to_string()]).expect("head succeeds");
        assert_eq!(out.len(), 300000);
    }

    /// A recorder implementing `Tmux` so argv can be asserted (like header/app's FakeTmux).
    struct FakeTmux {
        log: std::sync::Arc<std::sync::Mutex<Vec<Vec<String>>>>,
    }
    impl Default for FakeTmux {
        fn default() -> Self {
            FakeTmux { log: std::sync::Arc::new(std::sync::Mutex::new(Vec::new())) }
        }
    }
    impl Tmux for FakeTmux {
        fn run(&self, args: &[String]) -> Result<String, String> {
            self.log.lock().unwrap().push(args.to_vec());
            Ok(String::new())
        }
    }

    #[test]
    fn set_menu_hit_argv_some() {
        let t = FakeTmux::default();
        let hit = MenuHit { kind: "controls", x0: 12, x1: 23, y0: 0, y1: 2 };
        set_menu_hit(&t, "%10", Some(&hit)).unwrap();
        let log = t.log.lock().unwrap();
        assert_eq!(log[0], [
            "set-option", "-p", "-t", "%10", "@force_menu_kind", "controls",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x0", "12",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x1", "23",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y0", "0",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y1", "2",
        ]);
    }

    struct Panes(&'static str);
    impl Tmux for Panes {
        fn run(&self, _args: &[String]) -> Result<String, String> {
            Ok(self.0.to_string())
        }
    }

    #[test]
    fn window_role_pids_only_running_force_programs() {
        let t = Panes("agent\tclaude\t10\nheader\tforce-header\t11\nheader\tforce\t12\nsidebar\tforce-sidebar\t13\nheader\tforce-header\tx");
        assert_eq!(window_role_pids(&t, "%1", "header"), vec![11]);
        assert!(window_role_pids(&Panes(""), "%1", "header").is_empty());
    }

    #[test]
    fn set_menu_hit_argv_none() {
        let t = FakeTmux::default();
        set_menu_hit(&t, "%10", None).unwrap();
        let log = t.log.lock().unwrap();
        assert_eq!(log[0], [
            "set-option", "-p", "-t", "%10", "@force_menu_kind", "",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x0", "",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_x1", "",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y0", "",
            ";",
            "set-option", "-p", "-t", "%10", "@force_menu_y1", "",
        ]);
    }
}
