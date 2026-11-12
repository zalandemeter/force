//! Shared terminal setup shared by both the `force-sidebar` and `force-header` binaries: raw mode,
//! alternate screen, mouse/focus capture, synchronous-update draws, and the canonical Ratatui event loop:
//! a single `mpsc` channel fed by an input thread (blocking `crossterm::event::read`), a Linux
//! inotify watcher on the snapshot directory, and a signal self-pipe. The main loop blocks on
//! `recv_timeout(next_deadline)`, so idle wakeups drop to ~1 per 5 s instead of polling every 25 ms.

use std::io;
use std::os::unix::io::RawFd;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Set when a signal handler observes SIGTERM/SIGHUP (kind forwarded on the channel too).
pub static SIGNALLED: AtomicBool = AtomicBool::new(false);
/// Set when the sidebar receives SIGUSR1 (forwarded as `Event::Signal(Usr1)` too).
pub static USR1: AtomicBool = AtomicBool::new(false);
/// True while raw mode / alternate screen is active, so the panic path can restore.
pub static RAW_ENABLED: AtomicBool = AtomicBool::new(false);

/// Write end of the signal self-pipe (a raw fd, because signal handlers must be async-signal-safe).
static PIPE_W: AtomicI32 = AtomicI32::new(-1);

/// Signal kinds forwarded over the channel.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Signal {
    /// SIGUSR1 — the force hook signals a switch arrival fade (sidebar only; the header ignores it).
    Usr1,
    /// SIGTERM.
    Term,
    /// SIGHUP.
    Hup,
}

/// A single application event. `Input` comes from the blocking event reader; `SnapshotChanged` from inotify;
/// `Signal` from the self-pipe thread; `Exit` when the input reader hits EOF/error (tty death) so the
/// main loop stops cleanly without a polling watchdog.
#[derive(Debug)]
pub enum Event {
    Input(crossterm::event::Event),
    SnapshotChanged,
    Signal(Signal),
    Exit,
}

unsafe fn sig_write(byte: u8) {
    let w = PIPE_W.load(Ordering::SeqCst);
    if w >= 0 {
        libc::write(w, &byte as *const u8 as *const libc::c_void, 1);
    }
}
unsafe extern "C" fn handle_sigterm(_: i32) {
    SIGNALLED.store(true, Ordering::SeqCst);
    sig_write(3);
}
unsafe extern "C" fn handle_sighup(_: i32) {
    SIGNALLED.store(true, Ordering::SeqCst);
    sig_write(4);
}
unsafe extern "C" fn handle_sigusr1(_: i32) {
    USR1.store(true, Ordering::SeqCst);
    sig_write(1);
}

/// Wrap one `draw` in DEC mode 2026 (Begin/EndSynchronizedUpdate) so tmux / the terminal never shows a
/// half-drawn frame. Returns the same result as the inner `draw`.
pub fn with_sync<W: io::Write>(w: &mut W, draw: impl FnOnce() -> io::Result<()>) -> io::Result<()> {
    crossterm::execute!(w, crossterm::terminal::BeginSynchronizedUpdate)?;
    let r = draw();
    crossterm::execute!(w, crossterm::terminal::EndSynchronizedUpdate)?;
    r
}

/// Spawn the three event sources (input reader, signal self-pipe, inotify snapshot watcher) feeding one
/// `mpsc` channel and return its receiver. The main loop should `recv_timeout(next_deadline)` on it.
pub fn spawn_loop(snapshot: PathBuf) -> mpsc::Receiver<Event> {
    let (tx, rx) = mpsc::channel::<Event>();

    // Signal self-pipe: handlers write one byte; a small thread forwards it as `Event::Signal`.
    let mut fds = [0 as RawFd; 2];
    unsafe {
        if libc::pipe(fds.as_mut_ptr()) == 0 {
            PIPE_W.store(fds[1], Ordering::SeqCst);
            // `sighandler_t` is `size_t`; route through a pointer so the cast is explicit and the
            // ABI of our `extern "C"` handlers is preserved.
            libc::signal(libc::SIGTERM, handle_sigterm as *const () as usize);
            libc::signal(libc::SIGHUP, handle_sighup as *const () as usize);
            libc::signal(libc::SIGUSR1, handle_sigusr1 as *const () as usize);
            let tx = tx.clone();
            std::thread::spawn(move || {
                let mut buf = [0u8; 32];
                loop {
                    let n = libc::read(fds[0], buf.as_mut_ptr() as *mut libc::c_void, buf.len());
                    if n <= 0 {
                        break;
                    }
                    for &b in &buf[..n as usize] {
                        let sig = match b {
                            1 => Signal::Usr1,
                            3 => Signal::Term,
                            4 => Signal::Hup,
                            _ => continue,
                        };
                        if tx.send(Event::Signal(sig)).is_err() {
                            return;
                        }
                    }
                }
            });
        }
    }

    // Input thread: a blocking read that also reports tty death as `Event::Exit`.
    {
        let tx = tx.clone();
        std::thread::spawn(move || loop {
            match crossterm::event::read() {
                Ok(ev) => {
                    if tx.send(Event::Input(ev)).is_err() {
                        break;
                    }
                }
                Err(_) => {
                    let _ = tx.send(Event::Exit);
                    break;
                }
            }
        });
    }

    // inotify watcher on the snapshot DIRECTORY (the daemon replaces the file atomically).
    spawn_watch(snapshot, tx);

    rx
}

/// Watch `snapshot`'s directory with inotify for IN_MOVED_TO | IN_CLOSE_WRITE | IN_CREATE of that
/// file name and forward `Event::SnapshotChanged`. Uses the `libc` inotify API directly (no new deps).
#[cfg(target_os = "linux")]
fn spawn_watch(snapshot: PathBuf, tx: Sender<Event>) {
    let dir = snapshot.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from("."));
    let name = snapshot.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    std::thread::spawn(move || {
        let cdir = match std::ffi::CString::new(dir.to_string_lossy().as_bytes()) {
            Ok(c) => c,
            Err(_) => return,
        };
        let fd = unsafe { libc::inotify_init1(libc::IN_CLOEXEC) };
        if fd < 0 {
            return;
        }
        let wd = unsafe { libc::inotify_add_watch(fd, cdir.as_ptr(), libc::IN_MOVED_TO | libc::IN_CLOSE_WRITE | libc::IN_CREATE) };
        if wd < 0 {
            return;
        }
        let mut buf = [0u8; 4096];
        loop {
            // The fd is opened blocking (no IN_NONBLOCK) so read() sleeps in the kernel until an event
            // arrives; this thread never wakes periodically while idle.
            let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
            if n < 0 {
                let err = std::io::Error::last_os_error();
                if err.raw_os_error() == Some(libc::EINTR) {
                    continue;
                }
                return; // real error (e.g. watch removed)
            }
            if n == 0 {
                break;
            }
            let mut off = 0usize;
            while off + 16 <= n as usize {
                let mask = u32::from_ne_bytes([buf[off + 4], buf[off + 5], buf[off + 6], buf[off + 7]]);
                let len = u32::from_ne_bytes([buf[off + 12], buf[off + 13], buf[off + 14], buf[off + 15]]) as usize;
                let name_start = off + 16;
                let name_bytes = &buf[name_start..(name_start + len).min(n as usize)];
                let fname: String = name_bytes.split(|&b| b == 0).next().map(|s| String::from_utf8_lossy(s).into_owned()).unwrap_or_default();
                if mask & (libc::IN_MOVED_TO | libc::IN_CLOSE_WRITE | libc::IN_CREATE) != 0
                    && fname == name
                    && tx.send(Event::SnapshotChanged).is_err()
                {
                    return;
                }
                off += 16 + len;
            }
        }
    });
}

/// macOS (SPEC §51): kqueue EVFILT_VNODE / NOTE_WRITE on the snapshot's directory fires when an entry is
/// added or renamed in it (the daemon replaces the file atomically). The event does not name the entry, so
/// `SnapshotChanged` is sent only when the snapshot's identity (inode, size, mtime) changed since the last one.
#[cfg(target_os = "macos")]
fn spawn_watch(snapshot: PathBuf, tx: Sender<Event>) {
    use std::os::unix::fs::MetadataExt;
    let dir = snapshot.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from("."));
    std::thread::spawn(move || {
        let cdir = match std::ffi::CString::new(dir.to_string_lossy().as_bytes()) {
            Ok(c) => c,
            Err(_) => return,
        };
        let dfd = unsafe { libc::open(cdir.as_ptr(), libc::O_EVTONLY | libc::O_CLOEXEC) };
        if dfd < 0 {
            return;
        }
        let kq = unsafe { libc::kqueue() };
        if kq < 0 {
            unsafe { libc::close(dfd) };
            return;
        }
        let change = libc::kevent {
            ident: dfd as libc::uintptr_t,
            filter: libc::EVFILT_VNODE,
            flags: libc::EV_ADD | libc::EV_CLEAR,
            fflags: libc::NOTE_WRITE,
            data: 0,
            udata: std::ptr::null_mut(),
        };
        if unsafe { libc::kevent(kq, &change, 1, std::ptr::null_mut(), 0, std::ptr::null()) } < 0 {
            unsafe { libc::close(kq); libc::close(dfd) };
            return;
        }
        let ident = |p: &PathBuf| std::fs::metadata(p).ok().map(|m| (m.ino(), m.size(), m.mtime(), m.mtime_nsec()));
        let mut last = ident(&snapshot);
        loop {
            let mut ev = libc::kevent { ident: 0, filter: 0, flags: 0, fflags: 0, data: 0, udata: std::ptr::null_mut() };
            // blocks in the kernel until the directory changes: no periodic wake-ups while idle
            let n = unsafe { libc::kevent(kq, std::ptr::null(), 0, &mut ev, 1, std::ptr::null()) };
            if n < 0 {
                if std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                    continue;
                }
                break;
            }
            if n == 0 {
                continue;
            }
            let now = ident(&snapshot);
            if now.is_some() && now != last {
                last = now;
                if tx.send(Event::SnapshotChanged).is_err() {
                    break;
                }
            }
        }
        unsafe { libc::close(kq); libc::close(dfd) };
    });
}

/// Elsewhere: no watcher; the main loop's periodic re-read still picks the snapshot up.
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn spawn_watch(_snapshot: PathBuf, _tx: Sender<Event>) {}

/// This machine's short host name (`gethostname` up to the first dot, as the daemon's FORCE_HOST: macOS
/// answers "name.local"), "" when unknown.
pub fn host_name() -> String {
    let mut buf = [0u8; 256];
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) };
    if rc != 0 {
        return String::new();
    }
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    let full = String::from_utf8_lossy(&buf[..end]).trim().to_string();
    full.split('.').next().unwrap_or("").to_string()
}

/// Replace the default panic hook so the terminal is restored before the process dies.
pub fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if RAW_ENABLED.swap(false, Ordering::SeqCst) {
            let _ = restore_impl();
        }
        default_hook(info);
    }));
}

/// Enter raw mode and the alternate screen with mouse/focus capture.
pub fn enter() -> io::Result<()> {
    crossterm::terminal::enable_raw_mode()?;
    RAW_ENABLED.store(true, Ordering::SeqCst);
    let mut stdout = io::stdout();
    crossterm::execute!(
        stdout,
        crossterm::terminal::EnterAlternateScreen,
        crossterm::event::EnableMouseCapture,
        crossterm::event::EnableFocusChange
    )?;
    Ok(())
}

/// Leave the alternate screen / raw mode (ignores errors, e.g. a dead tty).
pub fn leave() {
    let _ = restore_impl();
}

fn restore_impl() -> io::Result<()> {
    let mut so = io::stdout();
    crossterm::execute!(
        so,
        crossterm::terminal::LeaveAlternateScreen,
        crossterm::event::DisableMouseCapture,
        crossterm::event::DisableFocusChange
    )?;
    crossterm::terminal::disable_raw_mode()?;
    RAW_ENABLED.store(false, Ordering::SeqCst);
    Ok(())
}

/// The event-loop blocking deadline as a `Duration` from `now_ms`. A pending draw (`changed`) must happen
/// immediately so the loop must not block at all (`Duration::ZERO`); otherwise the earliest of the next animation
/// frame and the footer-error expiry wins, capped at 5 s (the snapshot staleness re-check). With nothing active it
/// falls back to the 5 s cap.
pub fn loop_timeout(
    changed: bool,
    next_frame_ms: Option<i64>,
    error_expiry_ms: Option<i64>,
    now_ms: i64,
) -> Duration {
    if changed {
        return Duration::ZERO;
    }
    let mut ms = 5_000u64;
    if let Some(frame) = next_frame_ms {
        ms = ms.min((frame - now_ms).max(1) as u64);
    }
    if let Some(exp) = error_expiry_ms {
        ms = ms.min((exp - now_ms + 1).max(1) as u64);
    }
    Duration::from_millis(ms)
}

/// Monotonic wall-clock in milliseconds (for spinners, fades, staleness).
pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    /// The loop's blocking deadline: a pending draw must block 0 ms, an animation ~33 ms ahead wins, and the
    /// footer-error expiry beats the 5 s cap when it is earlier.
    #[test]
    fn loop_timeout_changed_immediate_otherwise_deadlines() {
        let now = 1_000i64;
        // pending change -> draw immediately, never block for it
        assert_eq!(loop_timeout(true, None, None, now), Duration::ZERO);
        assert_eq!(loop_timeout(true, Some(now + 100), Some(now + 33), now), Duration::ZERO);
        // nothing active -> 5 s idle cap
        assert_eq!(loop_timeout(false, None, None, now), Duration::from_secs(5));
        // animation deadline ~33 ms ahead -> ~33 ms
        assert_eq!(loop_timeout(false, Some(now + 33), None, now), Duration::from_millis(33));
        // error expiry earlier than both the animation and the 5 s cap wins (+1 ms because it fires at `exp`)
        assert_eq!(loop_timeout(false, Some(now + 33), Some(now + 10), now), Duration::from_millis(11));
        // frame deadline never exceeds the 5 s cap even when far in the future
        assert_eq!(loop_timeout(false, Some(now + 60_000), None, now), Duration::from_secs(5));
    }

    /// Every synchronized draw is framed by DEC mode 2026 (`\x1b[?2026h` ... `\x1b[?2026l`).
    #[test]
    fn sync_output_is_framed_by_mode_2026() {
        let mut buf: Vec<u8> = Vec::new();
        let mut body: Vec<u8> = Vec::new();
        with_sync(&mut buf, || {
            body.extend_from_slice(b"MIDDLE");
            Ok(())
        })
        .unwrap();
        let s = String::from_utf8(buf).unwrap();
        assert_eq!(String::from_utf8(body).unwrap(), "MIDDLE", "draw output delivered");
        assert!(s.starts_with("\x1b[?2026h"), "starts with BeginSynchronizedUpdate: {:?}", s);
        assert!(s.ends_with("\x1b[?2026l"), "ends with EndSynchronizedUpdate: {:?}", s);
    }

    /// An atomic rename of the snapshot (the daemon's write pattern) must surface as `SnapshotChanged`
    /// within 200 ms.
    #[test]
    fn atomic_rename_delivers_snapshot_changed() {
        let dir = std::env::temp_dir().join(format!("cockpit-watch-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let target = dir.join("snapshot.json");
        std::fs::write(&target, b"{}").unwrap();

        let rx = spawn_loop(target.clone());
        // give the watcher thread a moment to register on the directory
        std::thread::sleep(Duration::from_millis(50));

        // atomic replace: write a temp file, then rename() over the watched name
        let tmp = dir.join("snapshot.json.tmp");
        std::fs::write(&tmp, br#"{"sessions":[]}"#).unwrap();
        std::fs::rename(&tmp, &target).unwrap();

        let started = Instant::now();
        let got = loop {
            match rx.recv_timeout(Duration::from_millis(200)) {
                Ok(Event::SnapshotChanged) => break true,
                Ok(_) => continue,
                Err(_) => break false,
            }
        };
        assert!(got, "SnapshotChanged not received");
        assert!(
            started.elapsed() < Duration::from_millis(200),
            "delivered too slowly: {:?}",
            started.elapsed()
        );
        std::fs::remove_dir_all(&dir).ok();
    }
}
