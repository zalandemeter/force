//! The `force-link exec` side of a direct channel (SPEC §56.2). It asks the local daemon over `<state>/link.sock`
//! with plain std I/O (no async runtime, no iroh): `direct` means the daemon carries the channel and this process
//! proxies its stdin, stdout, stderr, size and exit code; anything else means ssh, as in part A.

use crate::frame::{self, Header};
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// The daemon's answer must come quickly: it never dials for a request, it only looks.
const ANSWER_TIMEOUT: Duration = Duration::from_secs(3);

/// Ask the daemon. Some(stream) when it answered `direct` (the channel is open), None for ssh: no socket, no
/// daemon, `ssh`, or any error on the way.
pub fn ask(state: &str, header: &Header) -> Option<UnixStream> {
    let sock = Path::new(state).join("link.sock");
    let mut s = UnixStream::connect(&sock).ok()?;
    s.set_read_timeout(Some(ANSWER_TIMEOUT)).ok()?;
    s.set_write_timeout(Some(ANSWER_TIMEOUT)).ok()?;
    s.write_all(&header.to_line()).ok()?;
    let answer = frame::read_line_unbuffered(&mut s, 64).ok()?;
    if answer != b"direct" {
        return None;
    }
    s.set_read_timeout(None).ok()?;
    s.set_write_timeout(None).ok()?;
    Some(s)
}

/// One request to the daemon for its status; None when it is not running.
pub fn status(state: &str) -> Option<serde_json::Value> {
    let mut s = UnixStream::connect(Path::new(state).join("link.sock")).ok()?;
    s.set_read_timeout(Some(ANSWER_TIMEOUT)).ok()?;
    s.write_all(b"{\"v\":1,\"op\":\"status\"}\n").ok()?;
    let line = frame::read_line_unbuffered(&mut s, 1 << 20).ok()?;
    serde_json::from_slice(&line).ok()
}

// ------------------------------------------------------------------------------------------------ terminal --
fn isatty(fd: i32) -> bool {
    unsafe { libc::isatty(fd) == 1 }
}

/// (cols, rows) of the terminal on stdin, else stdout.
pub fn term_size() -> Option<(u16, u16)> {
    for fd in [0, 1] {
        let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
        if unsafe { libc::ioctl(fd, libc::TIOCGWINSZ as _, &mut ws) } == 0 && ws.ws_col > 0 && ws.ws_row > 0 {
            return Some((ws.ws_col, ws.ws_row));
        }
    }
    None
}

/// The saved terminal mode, restored on every way out (normal exit, error, a terminating signal).
static mut SAVED: std::mem::MaybeUninit<libc::termios> = std::mem::MaybeUninit::uninit();
static RAW: AtomicBool = AtomicBool::new(false);
static WINCH_FD: AtomicI32 = AtomicI32::new(-1);

fn restore_terminal() {
    if RAW.swap(false, Ordering::SeqCst) {
        unsafe { libc::tcsetattr(0, libc::TCSANOW, std::ptr::addr_of!(SAVED) as *const libc::termios) };
    }
}

extern "C" fn on_fatal_signal(sig: libc::c_int) {
    // async-signal-safe: tcsetattr and _exit only
    if RAW.load(Ordering::SeqCst) {
        unsafe { libc::tcsetattr(0, libc::TCSANOW, std::ptr::addr_of!(SAVED) as *const libc::termios) };
    }
    unsafe { libc::_exit(128 + sig) };
}

extern "C" fn on_winch(_sig: libc::c_int) {
    let fd = WINCH_FD.load(Ordering::SeqCst);
    if fd >= 0 {
        let b = [1u8];
        unsafe { libc::write(fd, b.as_ptr() as *const _, 1) };
    }
}

fn set_handler(sig: libc::c_int, f: extern "C" fn(libc::c_int)) {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = f as usize;
        libc::sigemptyset(&mut sa.sa_mask);
        sa.sa_flags = libc::SA_RESTART;
        libc::sigaction(sig, &sa, std::ptr::null_mut());
    }
}

/// Raw mode on stdin, like `ssh -t`; the old mode comes back on exit.
fn enter_raw() -> io::Result<()> {
    unsafe {
        let mut t: libc::termios = std::mem::zeroed();
        if libc::tcgetattr(0, &mut t) != 0 {
            return Err(io::Error::last_os_error());
        }
        std::ptr::addr_of_mut!(SAVED).write(std::mem::MaybeUninit::new(t));
        for sig in [libc::SIGTERM, libc::SIGHUP, libc::SIGINT, libc::SIGQUIT] {
            set_handler(sig, on_fatal_signal);
        }
        RAW.store(true, Ordering::SeqCst);
        let mut raw = t;
        libc::cfmakeraw(&mut raw);
        if libc::tcsetattr(0, libc::TCSANOW, &raw) != 0 {
            RAW.store(false, Ordering::SeqCst);
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn finish(code: i32) -> ! {
    let _ = io::stdout().flush();
    restore_terminal();
    std::process::exit(code);
}

// ---------------------------------------------------------------------------------------------------- proxy --
fn send(w: &Mutex<UnixStream>, kind: u8, payload: &[u8]) -> io::Result<()> {
    let mut g = w.lock().unwrap_or_else(|e| e.into_inner());
    frame::write_frame(&mut *g, kind, payload)
}

/// The header `force-link exec` sends. `--tty` only takes effect when stdin is a terminal (as `ssh -t`).
pub fn header_for(host: &str, argv: &[String], want_tty: bool) -> Header {
    let tty = want_tty && isatty(0);
    let (cols, rows) = if tty { term_size().unwrap_or((80, 24)) } else { (0, 0) };
    let term = if tty {
        std::env::var("TERM").ok().filter(|t| !t.is_empty() && t.len() <= 64
            && t.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'+' | b'-')))
            .unwrap_or_else(|| "xterm-256color".into())
    } else {
        String::new()
    };
    Header { v: 1, host: Some(host.to_string()), argv: argv.to_vec(), tty, cols, rows, term }
}

/// Proxy an open channel until the remote program's exit code arrives; never returns.
pub fn proxy(stream: UnixStream, host: &str, tty: bool) -> ! {
    let writer = Arc::new(Mutex::new(match stream.try_clone() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("force-link: {host}: {e}");
            finish(255);
        }
    }));
    if tty {
        if let Err(e) = enter_raw() {
            eprintln!("force-link: {host}: raw mode: {e}");
        }
        // SIGWINCH -> a byte on a pipe -> a resize frame (a signal handler cannot write frames itself)
        let mut fds = [0i32; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } == 0 {
            for fd in fds {
                unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
            }
            WINCH_FD.store(fds[1], Ordering::SeqCst);
            set_handler(libc::SIGWINCH, on_winch);
            let w = writer.clone();
            let rfd = fds[0];
            std::thread::spawn(move || {
                let mut b = [0u8; 64];
                loop {
                    let n = unsafe { libc::read(rfd, b.as_mut_ptr() as *mut _, b.len()) };
                    if n <= 0 {
                        if n < 0 && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
                            continue;
                        }
                        return;
                    }
                    if let Some((c, r)) = term_size() {
                        if send(&w, frame::RESIZE, &frame::resize_payload(c, r)).is_err() {
                            return;
                        }
                    }
                }
            });
        }
    }
    // stdin -> frames; its end -> stdin EOF
    let w = writer.clone();
    std::thread::spawn(move || {
        let mut buf = vec![0u8; 32 * 1024];
        let mut stdin = io::stdin().lock();
        loop {
            match stdin.read(&mut buf) {
                Ok(0) | Err(_) => {
                    let _ = send(&w, frame::STDIN_EOF, b"");
                    return;
                }
                Ok(n) => {
                    if send(&w, frame::STDIN, &buf[..n]).is_err() {
                        return;
                    }
                }
            }
        }
    });
    // frames -> stdout, stderr, the exit code
    let mut r = io::BufReader::with_capacity(64 * 1024, stream);
    let mut out = io::stdout().lock();
    loop {
        match frame::read_frame(&mut r) {
            Ok(Some(f)) => match f.kind {
                frame::STDOUT => {
                    if out.write_all(&f.payload).and_then(|_| out.flush()).is_err() {
                        finish(255);
                    }
                }
                frame::STDERR => {
                    let mut e = io::stderr().lock();
                    let _ = e.write_all(&f.payload).and_then(|_| e.flush());
                }
                frame::EXIT => {
                    let code = frame::parse_exit(&f.payload).unwrap_or(255);
                    finish(code);
                }
                frame::ERROR => {
                    let (code, msg) = frame::parse_error(&f.payload);
                    restore_terminal();
                    eprintln!("force-link: {host}: {code}: {msg}");
                    finish(255);
                }
                k => {
                    restore_terminal();
                    eprintln!("force-link: {host}: unexpected frame type {k}");
                    finish(255);
                }
            },
            Ok(None) => {
                restore_terminal();
                eprintln!("force-link: {host}: the direct link closed");
                finish(255);
            }
            Err(e) => {
                restore_terminal();
                eprintln!("force-link: {host}: {e}");
                finish(255);
            }
        }
    }
}
