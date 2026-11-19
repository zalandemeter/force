//! force-link: Force's single entry point to a remote host (SPEC §56.1, part A — over ssh, unchanged
//! behaviour). `exec` builds exactly what `force/remote.py`'s `ssh_argv` builds and execs it; `version`
//! prints the program's version. In this part it only ever shells out to ssh.

mod hosts;
mod json;
mod quote;

use std::os::unix::process::CommandExt;
use std::path::Path;

const USAGE: &str = "usage: force-link exec --state DIR --hosts FILE HOST [--tty] [--print] -- ARGV...\n       force-link version";

struct ExecArgs {
    state: String,
    hosts_file: String,
    host: String,
    tty: bool,
    print: bool,
    argv: Vec<String>,
}

/// Parse the words after `exec`. The first `--` ends flag parsing; everything after it is ARGV verbatim.
fn parse_exec_args(args: impl IntoIterator<Item = String>) -> Result<ExecArgs, String> {
    let args: Vec<String> = args.into_iter().collect();
    let mut state: Option<String> = None;
    let mut hosts_file: Option<String> = None;
    let mut tty = false;
    let mut print = false;
    let mut host: Option<String> = None;
    let mut argv: Vec<String> = Vec::new();
    let mut saw_boundary = false;
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if saw_boundary {
            argv.push(a.clone());
            i += 1;
            continue;
        }
        match a.as_str() {
            "--" => {
                saw_boundary = true;
                i += 1;
            }
            "--tty" => {
                tty = true;
                i += 1;
            }
            "--print" => {
                print = true;
                i += 1;
            }
            "--state" => {
                i += 1;
                state = Some(args.get(i).cloned().ok_or("missing value for --state")?);
                i += 1;
            }
            "--hosts" => {
                i += 1;
                hosts_file = Some(args.get(i).cloned().ok_or("missing value for --hosts")?);
                i += 1;
            }
            other if other.starts_with('-') && other != "-" => {
                return Err(format!("unknown option: {}", other));
            }
            _ => {
                if host.is_some() {
                    return Err(format!("unexpected argument: {}", a));
                }
                host = Some(a.clone());
                i += 1;
            }
        }
    }
    let state = state.ok_or("missing --state")?;
    let hosts_file = hosts_file.ok_or("missing --hosts")?;
    let host = host.ok_or("missing HOST")?;
    if !saw_boundary {
        return Err("missing --".into());
    }
    Ok(ExecArgs { state, hosts_file, host, tty, print, argv })
}

/// Exactly `["ssh", *ssh_opts, *(["-t"] if tty else []), <dest>, <cmd>]` (force/remote.py ssh_argv).
fn build_argv(state: &str, dest: &str, tty: bool, argv: &[String]) -> Vec<String> {
    let mut out: Vec<String> = vec![
        "ssh".into(),
        "-o".into(),
        "ControlMaster=auto".into(),
        "-o".into(),
        format!("ControlPath={}/ssh/%C", state),
        "-o".into(),
        "ControlPersist=10m".into(),
        "-o".into(),
        "ServerAliveInterval=15".into(),
        "-o".into(),
        "ServerAliveCountMax=3".into(),
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "ConnectTimeout=10".into(),
    ];
    if tty {
        out.push("-t".into());
    }
    out.push(dest.to_string());
    out.push(quote::join_cmd(argv));
    out
}

/// `<state>/ssh` with mode 0700 (parents too), like ssh_opts (SPEC §40).
fn ensure_ssh_dir(state_dir: &str) -> std::io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    let mut b = std::fs::DirBuilder::new();
    b.recursive(true).mode(0o700);
    b.create(Path::new(state_dir).join("ssh"))
}

/// Print `s` as one JSON string: `"`, `\` and control chars escaped, non-ASCII raw UTF-8.
fn json_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn print_argv_json(argv: &[String]) {
    let body = argv
        .iter()
        .map(|w| json_quote(w))
        .collect::<Vec<_>>()
        .join(",");
    println!("[{}]", body);
}

fn no_host(host: &str) -> ! {
    eprintln!("force-link: no host {}", host);
    std::process::exit(255);
}

fn run_exec(args: impl IntoIterator<Item = String>) -> i32 {
    let p = match parse_exec_args(args) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("force-link: {}", e);
            eprintln!("{}", USAGE);
            return 2;
        }
    };
    if !hosts::valid_name(&p.host) {
        no_host(&p.host);
    }
    let contents = match std::fs::read_to_string(&p.hosts_file) {
        Ok(c) => c,
        Err(_) => no_host(&p.host),
    };
    let doc = match json::parse(&contents) {
        Ok(d) => d,
        Err(_) => no_host(&p.host),
    };
    let dest = match hosts::host_ssh(&doc, &p.host) {
        Some(d) => d,
        None => no_host(&p.host),
    };
    if let Err(e) = ensure_ssh_dir(&p.state) {
        eprintln!("force-link: {}: {}", p.state, e);
        return 255;
    }
    let argv = build_argv(&p.state, &dest, p.tty, &p.argv);
    if p.print {
        print_argv_json(&argv);
        return 0;
    }
    let err = std::process::Command::new(&argv[0]).args(&argv[1..]).exec();
    eprintln!("force-link: exec {}: {}", argv[0], err);
    255
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut it = args.into_iter();
    let cmd = it.next();
    match cmd.as_deref() {
        Some("version") => {
            println!("force-link {}", env!("CARGO_PKG_VERSION"));
        }
        Some("exec") => std::process::exit(run_exec(it)),
        _ => {
            eprintln!("{}", USAGE);
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(args: &[&str]) -> Result<ExecArgs, String> {
        parse_exec_args(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn argv_without_tty() {
        let a = build_argv("/state", "devbox", false, &["force".into(), "list".into()]);
        assert_eq!(a[0], "ssh");
        assert!(a.contains(&"-o".into()));
        assert!(a.iter().any(|w| w == "ControlMaster=auto"));
        assert!(a.iter().any(|w| w == "BatchMode=yes"));
        assert!(a.iter().any(|w| w == "ControlPath=/state/ssh/%C"));
        assert!(!a.contains(&"-t".into()));
        assert_eq!(a[a.len() - 2], "devbox");
        assert_eq!(a[a.len() - 1], "force list");
    }

    #[test]
    fn argv_with_tty_and_home_words() {
        let a = build_argv("/s", "h", true, &["tmux".into(), "~/.local/bin/force".into()]);
        assert!(a.contains(&"-t".into()));
        assert_eq!(a[a.len() - 1], "tmux ~/.local/bin/force");
        let b = build_argv("/s", "h", true, &["~/my dir".into()]);
        assert_eq!(b[b.len() - 1], "~/'my dir'");
        let c = build_argv("/s", "h", false, &["~/".into()]);
        assert_eq!(c[c.len() - 1], "~/");
    }

    #[test]
    fn parse_flags_any_order() {
        let a = p(&["--hosts", "h.json", "devbox", "--state", "/s", "--tty", "--print", "--", "x"]).unwrap();
        assert_eq!(a.state, "/s");
        assert_eq!(a.hosts_file, "h.json");
        assert_eq!(a.host, "devbox");
        assert!(a.tty && a.print);
        assert_eq!(a.argv, vec!["x".to_string()]);
    }

    #[test]
    fn parse_argv_verbatim_after_boundary() {
        let a = p(&["--state", "/s", "--hosts", "f", "h", "--", "--", "-x"]).unwrap();
        assert_eq!(a.argv, vec!["--".to_string(), "-x".to_string()]);
        // a flag-looking word after -- is not a flag
        let b = p(&["--state", "/s", "--hosts", "f", "h", "--", "--tty"]).unwrap();
        assert!(!b.tty);
        assert_eq!(b.argv, vec!["--tty".to_string()]);
    }

    #[test]
    fn usage_errors() {
        assert!(p(&["--state", "/s", "--hosts", "f", "h"]).is_err()); // no --
        assert!(p(&["--state", "/s", "--hosts", "f", "--", "x"]).is_err()); // no HOST
        assert!(p(&["--hosts", "f", "h", "--", "x"]).is_err()); // no --state
        assert!(p(&["--state", "/s", "h", "--", "x"]).is_err()); // no --hosts
        assert!(p(&["--state"]).is_err()); // missing value
        assert!(p(&["--bogus", "x"]).is_err()); // unknown flag
    }

    #[test]
    fn json_output_escaping() {
        assert_eq!(json_quote("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(json_quote("line\n"), "\"line\\n\"");
        assert_eq!(json_quote("\u{0007}"), "\"\\u0007\"");
        assert_eq!(json_quote("é"), "\"é\"");
    }
}
