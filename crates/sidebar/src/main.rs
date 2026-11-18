//! force-sidebar binary: args, terminal setup, panic/signal cleanup, and the event loop. It is a pure
//! reader of a JSON snapshot written by the force daemon and drives tmux only through the `tmux` CLI.

use std::io;
use std::path::PathBuf;

use crossterm::event::Event;
use ratatui::style::{Modifier, Style};
use ratatui::Terminal;

use force_sidebar::app::App;
use force_sidebar::fx::AnimMode;
use force_sidebar::glyphs::{self, GlyphArg};
use force_sidebar::term::Signal;
use force_sidebar::{model, term, tmux};

const USAGE: &str = "usage: force-sidebar --snapshot <path> --force-bin <path> [--socket <tmux socket>] [--animations full|subtle|off] [--glyphs auto|rich|plain]";

struct Args {
    snapshot: PathBuf,
    cockpit_bin: String,
    socket: String,
    anims: AnimMode,
    glyphs: GlyphArg,
}

fn parse_args() -> Result<Args, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut snapshot: Option<String> = None;
    let mut cockpit_bin: Option<String> = None;
    let mut socket: Option<String> = None;
    let mut anims: AnimMode = AnimMode::Full;
    let mut glyphs: GlyphArg = GlyphArg::Auto;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].clone();
        match a.as_str() {
            "--snapshot" => {
                i += 1;
                snapshot = args.get(i).cloned();
            }
            "--force-bin" => {
                i += 1;
                cockpit_bin = args.get(i).cloned();
            }
            "--socket" => {
                i += 1;
                socket = args.get(i).cloned();
            }
            "--animations" => {
                i += 1;
                let v = args.get(i).map(|s| s.as_str()).unwrap_or("");
                anims = AnimMode::parse(v); // unknown values fall back to full, never exit
            }
            "--glyphs" => {
                i += 1;
                let v = args.get(i).map(|s| s.as_str()).unwrap_or("");
                glyphs = glyphs::parse_flag(v)?; // invalid value -> usage error
            }
            other => return Err(format!("unknown argument: {}", other)),
        }
        i += 1;
    }
    let snapshot = snapshot.ok_or_else(|| "missing --snapshot".to_string())?;
    let cockpit_bin = cockpit_bin.ok_or_else(|| "missing --force-bin".to_string())?;
    let socket = match socket {
        Some(s) => s,
        None => std::env::var("TMUX")
            .ok()
            .and_then(|t| t.split(',').next().map(|s| s.to_string()))
            .unwrap_or_default(),
    };
    Ok(Args { snapshot: PathBuf::from(snapshot), cockpit_bin, socket, anims, glyphs })
}

fn paint_grid(buf: &mut ratatui::buffer::Buffer, grid: &force_sidebar::app::Grid) {
    for y in 0..grid.h {
        for x in 0..grid.w {
            let c = grid.row(y).get(x).copied().unwrap_or(force_sidebar::app::RowCell { ch: ' ', fg: model::BG, bg: model::BG, bold: false });
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

/// The sidebar event loop. Blocks on `recv_timeout(next_deadline)` rather than polling: any input, an
/// inotify `SnapshotChanged`, or a signal (incl. SIGUSR1 for the arrival fade) wakes it immediately;
/// with nothing to do it sleeps up to 5 s (the staleness re-check poll handles that), then just wakes for
/// animation frames / the spinner while visible.
fn run_loop(
    app: &mut App,
    terminal: &mut Terminal<term::PaneBackend<io::Stdout>>,
    rx: std::sync::mpsc::Receiver<term::Event>,
) -> io::Result<()> {
    let mut changed = true; // draw the first frame
    // Repaint every cell on the next draw, not only the ones that changed: after a resize (tmux may have
    // dropped content even when the size ends up where it was, so ratatui's own size check misses it) and
    // on SIGUSR1 (switches, tier changes). Otherwise lost cells stay blank, e.g. half-drawn header buttons.
    let mut repaint = false;
    let mut last_draw = term::now_ms();
    let mut last_effect = term::now_ms();
    loop {
        // Pending change -> don't block at all (first frame / redraw immediately). Otherwise wake for the
        // earliest of the next animation frame and the footer error expiry, capped at 5 s (the staleness check).
        let timeout = term::loop_timeout(changed, app.next_frame_ms(), app.error_expiry(), term::now_ms());

        match rx.recv_timeout(timeout) {
            Ok(term::Event::Input(ev)) => {
                let now = term::now_ms();
                match ev {
                    Event::Key(k) => {
                        app.set_now(now);
                        let _ = app.on_key(k.code);
                    }
                    Event::Mouse(m) => {
                        app.set_now(now);
                        let _ = app.on_mouse(m.kind, m.column, m.row);
                    }
                    Event::FocusGained => app.on_focus_gained(),
                    Event::FocusLost => app.on_focus_lost(),
                    Event::Resize(width, height) => {
                        app.on_resize(width as usize, height as usize);
                        repaint = true;
                    }
                    Event::Paste(_) => {}
                }
                changed = true;
            }
            Ok(term::Event::SnapshotChanged) => changed = true, // `poll` re-loads on the new mtime
            Ok(term::Event::Signal(sig)) => match sig {
                Signal::Term | Signal::Hup => break,
                Signal::Usr1 => {
                    app.sigusr1(term::now_ms());
                    repaint = true;
                    changed = true;
                }
            },
            Ok(term::Event::Exit) => break, // input reader hit EOF/error: tty died
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }

        let now2 = term::now_ms();
        if app.poll(now2) {
            changed = true;
        }
        let spinner_due = app.viewed() && app.any_working() && now2 - last_draw >= 100;
        let should_draw = changed
            || app.fx.running()
            || app.event_anim_running()
            || app.continuous_running()
            || spinner_due;
        if should_draw {
            let dt = (now2 - last_effect) as u32;
            last_effect = now2;
            last_draw = now2;
            let grid = app.build_grid();
            let mut stdout = io::stdout();
            let full = std::mem::take(&mut repaint);
            term::with_sync(&mut stdout, || {
                if full {
                    // forget the previous frame so this draw writes every cell; NOT terminal.clear(): it
                    // asks the terminal for the cursor position, whose reply our input thread swallows,
                    // and the timeout killed the process (every sidebar and header, 2026-10-19)
                    terminal.swap_buffers();
                }
                terminal
                    .draw(|f| {
                        paint_grid(f.buffer_mut(), &grid);
                        if app.fx.running() {
                            let area = f.area();
                            app.fx.process(dt, f.buffer_mut(), area);
                        }
                    })
                    .map(|_| ())
            })?;
            changed = false;
        }
        // a click's host switch goes out only now, after the frame that shows its result
        if app.run_pending() {
            changed = true;
        }
    }
    Ok(())
}

fn run() -> io::Result<()> {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{}", e);
            eprintln!("{}", USAGE);
            std::process::exit(2);
        }
    };

    term::install_panic_hook();
    term::enter()?;

    // restore on normal exit (also covers our own break from the loop)
    let restore = || term::leave();

    let (w, h) = match crossterm::terminal::size() {
        Ok((w, h)) => ((w as usize).max(3), (h as usize).max(1)),
        Err(_) => (29, 24),
    };

    let tmux: Box<dyn tmux::Tmux> = if args.socket.is_empty() {
        Box::new(NoopTmux)
    } else {
        Box::new(tmux::RealTmux::new(args.socket.clone()))
    };

    let tmux_pane = std::env::var("TMUX_PANE").ok();
    let tz = model::read_tz_offset();

    let mut app = App::new(tmux, args.snapshot.clone(), args.cockpit_bin, tmux_pane, args.glyphs, tz, w, h);
    app.set_anims(args.anims);

    let mut terminal = Terminal::new(term::PaneBackend::new(io::stdout()))?;
    // a plain clear-screen: terminal.clear() asks for the cursor position, which times out (and exits)
    // when tmux is busy resizing the pane, e.g. while a client attaches
    crossterm::execute!(io::stdout(), crossterm::terminal::Clear(crossterm::terminal::ClearType::All))?;

    let rx = term::spawn_loop(args.snapshot.clone());

    let result = run_loop(&mut app, &mut terminal, rx);

    restore();
    result
}

fn main() {
    if let Err(e) = run() {
        eprintln!("force-sidebar: {}", e);
    }
}

/// A tmux client used outside a tmux session where nothing should be executed.
struct NoopTmux;
impl tmux::Tmux for NoopTmux {
    fn run(&self, _args: &[String]) -> Result<String, String> {
        Err("not in tmux".to_string())
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use force_sidebar::app::App;

    fn bare_app() -> App {
        App::new(Box::new(crate::NoopTmux), PathBuf::from("/nonexistent.json"), "/bin/true".into(), None, force_sidebar::glyphs::GlyphArg::Fixed(force_sidebar::glyphs::Tier::Plain), 0, 40, 28)
    }

    /// The next event-loop deadline in ms from `now`: the sidebar's own frame deadline (already capped at 5 s
    /// for the staleness re-check), else exactly 5 s when nothing is active.
    fn next_timeout_ms(app: &App, now: i64) -> u64 {
        app.next_frame_ms()
            .map(|ms| (ms - now).max(1) as u64)
            .map(|d| d.min(5000))
            .unwrap_or(5000)
    }

    #[test]
    fn idle_deadline_caps_at_five_seconds() {
        let mut a = bare_app();
        a.set_now(1_000);
        assert_eq!(next_timeout_ms(&a, 1_000), 5000, "nothing active -> 5 s idle cap");
    }
    // The spinner (~100 ms), animation (~33 ms) and hidden (None) cases live in app::tests, which has the
    // snapshot/state helpers; this wrapper's only job is the 5 s cap above.
}
