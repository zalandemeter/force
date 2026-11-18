//! force-header binary: the 2-row header pane above the agent's pane. Shares the terminal/signal/watchdog
//! setup with the sidebar (force_sidebar::term) and the snapshot/tmux/own-session logic with the
//! sidebar (force_sidebar::header, force_sidebar::snapshot, force_sidebar::tmux).

use std::io;
use std::path::PathBuf;

use crossterm::event::Event;
use ratatui::style::{Modifier, Style};
use ratatui::Terminal;

use force_sidebar::header::{Header, RealSpawner};
use force_sidebar::fx::AnimMode;
use force_sidebar::glyphs::{self, GlyphArg};
use force_sidebar::term::{self, Signal};
use force_sidebar::tmux::{self, RealTmux};

const USAGE: &str = "usage: force-header --snapshot <path> --share-dir <dir> [--socket <tmux socket>] [--animations full|subtle|off] [--glyphs auto|rich|plain]";

struct Args {
    snapshot: PathBuf,
    share_dir: PathBuf,
    socket: String,
    anims: AnimMode,
    glyphs: GlyphArg,
}

fn parse_args() -> Result<Args, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut snapshot: Option<String> = None;
    let mut share_dir: Option<String> = None;
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
            "--share-dir" => {
                i += 1;
                share_dir = args.get(i).cloned();
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
    let share_dir = share_dir.ok_or_else(|| "missing --share-dir".to_string())?;
    let socket = match socket {
        Some(s) => s,
        None => std::env::var("TMUX")
            .ok()
            .and_then(|t| t.split(',').next().map(|s| s.to_string()))
            .unwrap_or_default(),
    };
    Ok(Args {
        snapshot: PathBuf::from(snapshot),
        share_dir: PathBuf::from(share_dir),
        socket,
        anims,
        glyphs,
    })
}

fn paint_grid(buf: &mut ratatui::buffer::Buffer, grid: &force_sidebar::header::Grid) {
    for y in 0..grid.height() {
        for x in 0..grid.w {
            let c = grid.row(y).get(x).copied().unwrap_or(force_sidebar::header::Cell {
                ch: ' ',
                fg: force_sidebar::header::BG,
                bg: force_sidebar::header::BG,
                bold: false,
            });
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

    let w = match crossterm::terminal::size() {
        Ok((w, _)) => (w as usize).max(1),
        Err(_) => 80,
    };

    let tmux: Box<dyn tmux::Tmux> = if args.socket.is_empty() {
        Box::new(NoopTmux)
    } else {
        Box::new(RealTmux::new(args.socket.clone()))
    };
    let spawner: Box<dyn force_sidebar::header::Spawner> = Box::new(RealSpawner);

    let tmux_pane = std::env::var("TMUX_PANE").ok();

    let mut header = Header::new(tmux, spawner, args.snapshot.clone(), args.share_dir, tmux_pane, args.glyphs, w);
    header.set_anims(args.anims);

    let mut terminal = Terminal::new(term::PaneBackend::new(io::stdout()))?;
    // a plain clear-screen: terminal.clear() asks for the cursor position, which times out (and exits)
    // when tmux is busy resizing the pane, e.g. while a client attaches
    crossterm::execute!(io::stdout(), crossterm::terminal::Clear(crossterm::terminal::ClearType::All))?;

    let rx = term::spawn_loop(args.snapshot.clone());
    let result = run_loop(&mut header, &mut terminal, rx);
    restore();
    result
}

/// Event-driven main loop: block on `recv_timeout(next_deadline)` where the deadline is the earliest of
/// the next animation frame (pill reveal / cascade / working spinner) and the 5 s staleness re-check,
/// so idle wakeups stay at ~1 per 5 s. Every draw is wrapped in DEC mode 2026 synchronized update.
fn run_loop(
    header: &mut Header,
    terminal: &mut Terminal<term::PaneBackend<io::Stdout>>,
    rx: std::sync::mpsc::Receiver<term::Event>,
) -> io::Result<()> {
    let mut changed = true;
    // Repaint every cell after a resize and on SIGUSR1, not only changed ones: tmux may have dropped part
    // of the 2-row pane (buttons with one row of background) even when the size ends up unchanged.
    let mut repaint = false;
    let mut last_draw = term::now_ms();
    loop {
        // Pending change -> draw immediately (no 5 s wait for the first frame); otherwise wake for the next
        // animation frame, capped at 5 s (the staleness re-check).
        let timeout = term::loop_timeout(changed, header.next_frame_ms(), None, term::now_ms());
        match rx.recv_timeout(timeout) {
            Ok(term::Event::Input(ev)) => match ev {
                Event::Key(_) => {}
                Event::Mouse(m) => {
                    use crossterm::event::MouseButton;
                    use crossterm::event::MouseEventKind as M;
                    if let M::Down(MouseButton::Left) = m.kind {
                        header.set_now(term::now_ms());
                        header.on_click(m.column as usize);
                    }
                }
                Event::FocusGained | Event::FocusLost => {}
                Event::Resize(width, _) => {
                    header.on_resize(width as usize);
                    repaint = true;
                    changed = true;
                }
                Event::Paste(_) => {}
            },
            Ok(term::Event::SnapshotChanged) => {}
            Ok(term::Event::Signal(sig)) => match sig {
                Signal::Term | Signal::Hup => break,
                // The header has no switch arrival fade, but SIGUSR1 re-reads the auto glyph tier.
                Signal::Usr1 => {
                    header.sigusr1();
                    repaint = true;
                    changed = true;
                }
            },
            Ok(term::Event::Exit) => break,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }

        let now2 = term::now_ms();
        if header.poll(now2) {
            changed = true;
        }
        let spinner_due = header.viewed() && header.any_working() && now2 - last_draw >= 100;
        let should_draw = changed || header.fading() || header.cascade_running() || spinner_due;
        if should_draw {
            let grid = header.build_grid();
            let mut stdout = io::stdout();
            let full = std::mem::take(&mut repaint);
            term::with_sync(&mut stdout, || {
                if full {
                    // forget the previous frame so this draw writes every cell; NOT terminal.clear(): it
                    // asks the terminal for the cursor position, whose reply our input thread swallows,
                    // and the timeout killed the process (every sidebar and header, 2026-10-19)
                    terminal.swap_buffers();
                }
                terminal.draw(|f| paint_grid(f.buffer_mut(), &grid)).map(|_| ())
            })?;
            last_draw = now2;
            changed = false;
        }
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("force-header: {}", e);
    }
}

/// A tmux client used outside a tmux session where nothing should be executed.
struct NoopTmux;
impl tmux::Tmux for NoopTmux {
    fn run(&self, _args: &[String]) -> Result<String, String> {
        Err("not in tmux".to_string())
    }
}
