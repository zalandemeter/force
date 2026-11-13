//! force-dialog binary: the force popup dialogs (reference force/dialogs.py). Runs inside a tmux
//! display-popup (a full small terminal) and reuses the crate's terminal setup/restore + signal code.
//! It draws its first frame immediately, then blocks on input; all mutations go through the force CLI.

use std::io;
use std::path::PathBuf;

use crossterm::event::Event;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use force_sidebar::dialog::{Dialog, ForceEnv, Kind, RealRunner, Runner};
use force_sidebar::glyphs::{self, GlyphArg, Tier};

const USAGE: &str = "usage: force-dialog <new|rename|move|archive|delete|archived|jump> [NAME] --registry <path> --snapshot <path> --force-bin <path> --tmux-socket <name> [--glyphs auto|rich|plain]";

struct Args {
    kind: Kind,
    name: Option<String>,
    registry: PathBuf,
    snapshot: PathBuf,
    cockpit_bin: String,
    socket: String,
    glyphs: GlyphArg,
}

fn parse_args() -> Result<Args, String> {
    parse_args_from(std::env::args().skip(1).collect())
}

fn parse_args_from(args: Vec<String>) -> Result<Args, String> {
    let mut kind: Option<Kind> = None;
    let mut name: Option<String> = None;
    let mut registry: Option<String> = None;
    let mut snapshot: Option<String> = None;
    let mut cockpit_bin: Option<String> = None;
    let mut socket: Option<String> = None;
    let mut glyphs: GlyphArg = GlyphArg::Auto;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        match a {
            // the first word is the dialog's kind, a second one the session's name: a session may well be called
            // `new` (an automatic name) or `jump`, and `delete new` must open the delete dialog for it
            _ if kind.is_none() && !a.starts_with("--") => {
                kind = Some(parse_kind(a)?);
            }
            "--registry" => {
                i += 1;
                registry = args.get(i).cloned();
            }
            "--snapshot" => {
                i += 1;
                snapshot = args.get(i).cloned();
            }
            "--force-bin" => {
                i += 1;
                cockpit_bin = args.get(i).cloned();
            }
            "--tmux-socket" => {
                i += 1;
                socket = args.get(i).cloned();
            }
            "--glyphs" => {
                i += 1;
                let v = args.get(i).map(|s| s.as_str()).unwrap_or("");
                glyphs = glyphs::parse_flag(v)?; // invalid value -> usage error
            }
            other => {
                if other.starts_with("--") {
                    return Err(format!("unknown argument: {}", other));
                }
                if name.is_some() {
                    return Err(format!("unexpected argument: {}", other));
                }
                name = Some(other.to_string()); // NAME, for rename/move/archive/delete
            }
        }
        i += 1;
    }
    let kind = kind.ok_or_else(|| "missing dialog kind".to_string())?;
    let registry = registry.ok_or_else(|| "missing --registry".to_string())?;
    let snapshot = snapshot.ok_or_else(|| "missing --snapshot".to_string())?;
    let cockpit_bin = cockpit_bin.ok_or_else(|| "missing --force-bin".to_string())?;
    let socket = socket.ok_or_else(|| "missing --tmux-socket".to_string())?;
    Ok(Args {
        kind,
        name,
        registry: PathBuf::from(registry),
        snapshot: PathBuf::from(snapshot),
        cockpit_bin,
        socket,
        glyphs,
    })
}

/// Resolve the dialog's glyph tier: `rich`/`plain` are fixed; `auto` reads `@force_glyphs` from the
/// popup's own session via `tmux -L <socket> display -p '#{@force_glyphs}'`.
fn resolve_tier(args: &GlyphArg, runner: &dyn Runner) -> Tier {
    match args {
        GlyphArg::Fixed(t) => *t,
        GlyphArg::Auto => {
            let opt = runner
                .tmux(&["display".to_string(), "-p".to_string(), "#{@force_glyphs}".to_string()])
                .ok();
            glyphs::from_option(opt.as_deref().unwrap_or(""))
        }
    }
}

fn parse_kind(s: &str) -> Result<Kind, String> {
    Ok(match s {
        "new" => Kind::New,
        "rename" => Kind::Rename,
        "move" => Kind::Move,
        "archive" => Kind::Archive,
        "delete" => Kind::Delete,
        "archived" => Kind::Archived,
        "jump" => Kind::Jump,
        other => return Err(format!("unknown dialog kind: {}", other)),
    })
}

fn draw(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>, dialog: &mut Dialog) -> io::Result<()> {
    terminal.draw(|f| {
        let cursor = dialog.cursor_pos();
        dialog.draw(f.buffer_mut());
        if let Some((x, y)) = cursor {
            f.set_cursor_position((x, y));
        }
    })?;
    Ok(())
}

fn run() -> io::Result<i32> {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{}", e);
            eprintln!("{}", USAGE);
            std::process::exit(2);
        }
    };
    let (w, h) = match crossterm::terminal::size() {
        Ok((w, h)) => ((w as usize).max(8), (h as usize).max(6)),
        Err(_) => (64, 14),
    };

    // Split (ui) mode injects FORCE_CLIENT / FORCE_SESSION / FORCE_GLYPHS via the display-popup -e pairs.
    let fc = std::env::var("FORCE_CLIENT").ok().filter(|s| !s.is_empty());
    let fs = std::env::var("FORCE_SESSION").ok().filter(|s| !s.is_empty());
    let fg = std::env::var("FORCE_GLYPHS").ok().filter(|s| !s.is_empty());
    let fh = std::env::var("FORCE_HOST").ok().filter(|s| !s.is_empty());
    let force = ForceEnv { client: fc.as_deref(), session: fs.as_deref(), glyphs: fg.as_deref(), host: fh.as_deref() };
    let host_tmux = std::env::var("FORCE_HOST_TMUX").ok();

    let runner: Box<dyn Runner> =
        Box::new(RealRunner::new(args.cockpit_bin, args.socket).with_host_prefix(host_tmux.as_deref()));
    // FORCE_GLYPHS overrides the auto tier read; otherwise `auto` reads `@force_glyphs`.
    let tier = match force.glyphs {
        Some(g) => glyphs::from_option(g),
        None => resolve_tier(&args.glyphs, runner.as_ref()),
    };
    let mut dialog = Dialog::new_with_env(args.kind, args.name, &args.registry, &args.snapshot, runner, &force, tier, w, h);

    force_sidebar::term::install_panic_hook();
    force_sidebar::term::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    // a plain clear-screen: terminal.clear() asks for the cursor position, which can time out and exit
    crossterm::execute!(io::stdout(), crossterm::terminal::Clear(crossterm::terminal::ClearType::All))?;

    dialog.now_ms = force_sidebar::term::now_ms();
    draw(&mut terminal, &mut dialog)?; // first frame immediately

    loop {
        if crossterm::event::poll(std::time::Duration::from_millis(50))? {
            match crossterm::event::read()? {
                Event::Key(k) => {
                    dialog.handle_key(k, force_sidebar::term::now_ms());
                }
                Event::Mouse(m) => {
                    dialog.handle_mouse(&m);
                }
                Event::Resize(w2, h2) => {
                    dialog.w = (w2 as usize).max(8);
                    dialog.h = (h2 as usize).max(6);
                }
                _ => {}
            }
        }
        let now = force_sidebar::term::now_ms();
        let exit_now = dialog.tick(now);
        draw(&mut terminal, &mut dialog)?;
        if exit_now {
            break;
        }
    }
    force_sidebar::term::leave();
    Ok(dialog.result)
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            force_sidebar::term::leave();
            eprintln!("force-dialog: {}", e);
            std::process::exit(1);
        }
    }
}



#[cfg(test)]
mod tests {
    use super::*;

    fn parse(words: &[&str]) -> Result<Args, String> {
        let mut v: Vec<String> = words.iter().map(|w| w.to_string()).collect();
        v.extend(["--registry", "r", "--snapshot", "s", "--force-bin", "f", "--tmux-socket", "t"].map(String::from));
        parse_args_from(v)
    }

    #[test]
    fn a_session_named_like_a_dialog_is_its_name() {
        // `force dialog delete new`: delete the session called `new` (an automatic name), not open the New dialog
        for k in ["new", "jump", "archived", "rename", "move", "archive", "delete"] {
            let a = parse(&["delete", k]).unwrap();
            assert!(matches!(a.kind, Kind::Delete), "delete {k}");
            assert_eq!(a.name.as_deref(), Some(k));
        }
        let a = parse(&["rename", "jump"]).unwrap();
        assert!(matches!(a.kind, Kind::Rename) && a.name.as_deref() == Some("jump"));
        let a = parse(&["new"]).unwrap();
        assert!(matches!(a.kind, Kind::New) && a.name.is_none());
    }

    #[test]
    fn bad_arguments_are_refused() {
        assert!(parse(&["nope"]).is_err(), "an unknown kind");
        assert!(parse(&["delete", "a", "b"]).is_err(), "two names");
        assert!(parse(&[]).is_err(), "no kind");
    }
}
