//! What a peer may run here over the direct link (SPEC §56.2): Force itself or this host's tmux on its live
//! server, never a shell, with a fixed, small environment.

use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

/// The programs a peer may start, resolved once when the daemon starts.
#[derive(Debug, Clone)]
pub struct Allow {
    pub home: String,
    /// `$HOME/.local/bin/force`
    pub force: String,
    /// this host's tmux: Force's own when the release ships one, else `tmux` on PATH (remote.py's `_TMUX_PROBE`)
    pub tmux: Option<String>,
}

fn executable(p: &Path) -> bool {
    std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// `$HOME/.local/share/force/app/share/tmux-bin/tmux` when executable, else the first `tmux` on `path`.
pub fn resolve_tmux(home: &str, path: &str) -> Option<String> {
    let own = PathBuf::from(home).join(".local/share/force/app/share/tmux-bin/tmux");
    if executable(&own) {
        return Some(own.to_string_lossy().into_owned());
    }
    for dir in path.split(':').filter(|d| d.starts_with('/')) {
        let p = Path::new(dir).join("tmux");
        if executable(&p) {
            return Some(p.to_string_lossy().into_owned());
        }
    }
    None
}

impl Allow {
    pub fn new(home: &str, tmux: Option<String>) -> Allow {
        let home = home.trim_end_matches('/').to_string();
        Allow { force: format!("{home}/.local/bin/force"), home, tmux }
    }

    /// `~/x` -> `$HOME/x` (a leading `~/` only, as the remote shell expands it for the ssh path); other words as
    /// they are.
    pub fn expand(&self, w: &str) -> String {
        match w.strip_prefix("~/") {
            Some(rest) => format!("{}/{}", self.home, rest),
            None => w.to_string(),
        }
    }

    /// The argv to execute, or why not. argv[0] (after `~/` expansion) must be exactly `$HOME/.local/bin/force`,
    /// or this host's tmux (bare `tmux` means it) followed by exactly `-L force` and then no further option: a
    /// tmux command or nothing (`-c` would run a shell, `-S`/`-f` would change the server).
    pub fn check(&self, argv: &[String]) -> Result<Vec<String>, String> {
        let mut out: Vec<String> = argv.iter().map(|w| self.expand(w)).collect();
        let Some(first) = out.first().cloned() else {
            return Err("empty argv".into());
        };
        if first == self.force {
            return Ok(out);
        }
        if let Some(tmux) = &self.tmux {
            if first == "tmux" || first == *tmux {
                if out.len() < 3 || out[1] != "-L" || out[2] != "force" {
                    return Err("tmux only with -L force".into());
                }
                if out.get(3).is_some_and(|w| w.starts_with('-')) {
                    return Err("no further tmux options".into());
                }
                out[0] = tmux.clone();
                return Ok(out);
            }
        }
        Err(format!("{} is not allowed", first))
    }
}

/// The environment of a spawned program: HOME, USER, LOGNAME, LANG and LC_* from `from` (the daemon's own),
/// PATH = `$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin`, and TERM with a tty. Nothing else.
pub fn child_env(from: impl IntoIterator<Item = (OsString, OsString)>, home: &str, term: Option<&str>)
                 -> Vec<(OsString, OsString)> {
    let mut out: Vec<(OsString, OsString)> = Vec::new();
    for (k, v) in from {
        let ks = k.to_string_lossy();
        if matches!(ks.as_ref(), "USER" | "LOGNAME" | "LANG") || ks.starts_with("LC_") {
            out.push((k, v));
        }
    }
    out.push(("HOME".into(), home.into()));
    out.push(("PATH".into(), format!("{home}/.local/bin:/usr/local/bin:/usr/bin:/bin").into()));
    if let Some(t) = term {
        out.push(("TERM".into(), t.into()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn allow() -> Allow {
        Allow::new("/home/u", Some("/usr/bin/tmux".into()))
    }

    fn v(words: &[&str]) -> Vec<String> {
        words.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn force_is_allowed_with_tilde_expanded() {
        assert_eq!(allow().check(&v(&["~/.local/bin/force", "watch"])).unwrap(),
                   v(&["/home/u/.local/bin/force", "watch"]));
        assert_eq!(allow().check(&v(&["/home/u/.local/bin/force", "list", "~/x"])).unwrap(),
                   v(&["/home/u/.local/bin/force", "list", "/home/u/x"]));
    }

    #[test]
    fn tmux_only_with_dash_l_force() {
        assert_eq!(allow().check(&v(&["tmux", "-L", "force", "display", "-p", "x"])).unwrap(),
                   v(&["/usr/bin/tmux", "-L", "force", "display", "-p", "x"]));
        assert_eq!(allow().check(&v(&["/usr/bin/tmux", "-L", "force"])).unwrap(),
                   v(&["/usr/bin/tmux", "-L", "force"]));
        assert!(allow().check(&v(&["tmux", "ls"])).is_err());
        assert!(allow().check(&v(&["tmux", "-L", "other", "ls"])).is_err());
        assert!(allow().check(&v(&["tmux", "-Lforce", "ls"])).is_err());
        assert!(allow().check(&v(&["tmux", "-L", "force", "-c", "sh -c id"])).is_err());
        assert!(allow().check(&v(&["tmux", "-L", "force", "-S", "/tmp/x"])).is_err());
        assert!(allow().check(&v(&["/opt/tmux", "-L", "force", "ls"])).is_err()); // another tmux
    }

    #[test]
    fn everything_else_refused() {
        assert!(allow().check(&v(&["sh", "-c", "id"])).is_err());
        assert!(allow().check(&v(&["/bin/sh"])).is_err());
        assert!(allow().check(&v(&[".local/bin/force"])).is_err()); // relative
        assert!(allow().check(&v(&["force", "list"])).is_err()); // bare, not the absolute launcher
        assert!(allow().check(&v(&["~/.local/bin/../bin/force"])).is_err());
        assert!(allow().check(&v(&["~/.local/bin/force2"])).is_err());
        assert!(allow().check(&v(&[])).is_err());
        let no_tmux = Allow::new("/home/u", None);
        assert!(no_tmux.check(&v(&["tmux", "-L", "force"])).is_err());
    }

    #[test]
    fn env_is_the_short_list() {
        let from = vec![("USER", "u"), ("LANG", "C.UTF-8"), ("LC_ALL", "C"), ("SSH_AUTH_SOCK", "/x"),
                        ("PATH", "/evil"), ("LD_PRELOAD", "/x.so"), ("HOME", "/elsewhere")];
        let env = child_env(from.into_iter().map(|(k, v)| (k.into(), v.into())), "/home/u", Some("xterm"));
        let keys: Vec<String> = env.iter().map(|(k, _)| k.to_string_lossy().into_owned()).collect();
        assert_eq!(keys, vec!["USER", "LANG", "LC_ALL", "HOME", "PATH", "TERM"]);
        let get = |k: &str| env.iter().find(|(kk, _)| kk == k).map(|(_, v)| v.to_string_lossy().into_owned());
        assert_eq!(get("HOME").unwrap(), "/home/u");
        assert_eq!(get("PATH").unwrap(), "/home/u/.local/bin:/usr/local/bin:/usr/bin:/bin");
        assert!(child_env(Vec::<(OsString, OsString)>::new(), "/h", None).iter().all(|(k, _)| k != "TERM"));
    }

    #[test]
    fn tmux_resolution_prefers_forces_own() {
        let d = std::env::temp_dir().join(format!("force-link-allow-{}", std::process::id()));
        let own = d.join(".local/share/force/app/share/tmux-bin");
        let bin = d.join("bin");
        std::fs::create_dir_all(&own).unwrap();
        std::fs::create_dir_all(&bin).unwrap();
        let mk = |p: &Path| {
            std::fs::write(p, "#!/bin/sh\n").unwrap();
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
        };
        mk(&bin.join("tmux"));
        let home = d.to_string_lossy().into_owned();
        let path = format!("relative:{}", bin.display());
        assert_eq!(resolve_tmux(&home, &path).unwrap(), bin.join("tmux").to_string_lossy());
        mk(&own.join("tmux"));
        assert_eq!(resolve_tmux(&home, &path).unwrap(), own.join("tmux").to_string_lossy());
        assert_eq!(resolve_tmux("/nonexistent", "/nonexistent/bin"), None);
        std::fs::remove_dir_all(&d).ok();
    }
}
