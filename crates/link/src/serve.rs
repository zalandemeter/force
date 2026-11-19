//! The serving side of a channel (SPEC §56.2): a peer's QUIC stream runs one allowed program here, on pipes or
//! on a new pty, and its output, exit code or refusal goes back as frames.

use crate::allow::{child_env, Allow};
use crate::frame::{self, aio, Header};
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc;

/// Held while a pty is opened (until its descriptors are close-on-exec) and while a child is spawned, so no child
/// inherits another channel's pty.
static SPAWN_LOCK: Mutex<()> = Mutex::new(());

/// How long a child may outlive SIGHUP before SIGKILL.
const KILL_AFTER: Duration = Duration::from_secs(10);
/// How long output is still read after the child exited (a grandchild may hold the pipes or the pty).
const DRAIN_AFTER_EXIT: Duration = Duration::from_secs(2);

pub fn log(msg: &str) {
    eprintln!("force-link: {msg}");
}

/// Serve one stream: header, check, spawn, pump, exit frame. `send`/`recv` are the stream's halves.
pub async fn serve<S, R>(send: S, mut recv: R, allow: &Allow, peer: &str)
where
    S: AsyncWrite + Unpin + Send + 'static,
    R: AsyncRead + Unpin + Send + 'static,
{
    let (tx, rx) = mpsc::channel::<Vec<u8>>(16);
    let writer = tokio::spawn(write_all_then_finish(send, rx));
    let header = match tokio::time::timeout(Duration::from_secs(10), aio::read_line(&mut recv, frame::MAX_HEADER)).await {
        Ok(Ok(line)) => Header::parse(&line),
        Ok(Err(e)) => Err(format!("header: {e}")),
        Err(_) => Err("header: timed out".into()),
    };
    let header = header.and_then(|h| match h.host {
        None => Ok(h),
        Some(_) => Err("host is not sent over the link".to_string()),
    });
    match header {
        Err(e) => refuse(&tx, "bad_request", &e, peer),
        Ok(h) => match allow.check(&h.argv) {
            Ok(argv) => run(argv, &h, allow, recv, &tx, peer).await,
            Err(why) => refuse(&tx, "not_allowed", &why, peer),
        },
    }
    drop(tx);
    let _ = writer.await;
}

fn refuse(tx: &mpsc::Sender<Vec<u8>>, code: &str, why: &str, peer: &str) {
    log(&format!("refused a channel from {peer}: {code}: {why}"));
    let _ = tx.try_send(frame::encode(frame::ERROR, &frame::error_payload(code, why)));
}

async fn write_all_then_finish<S: AsyncWrite + Unpin>(mut send: S, mut rx: mpsc::Receiver<Vec<u8>>) {
    while let Some(b) = rx.recv().await {
        if send.write_all(&b).await.is_err() {
            return;
        }
    }
    let _ = send.shutdown().await; // finish: a clean end of the stream
}

fn exit_code(st: std::process::ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    st.code().unwrap_or_else(|| 128 + st.signal().unwrap_or(0))
}

/// A new pty (master, slave) with the given size, both close-on-exec. Caller holds SPAWN_LOCK.
fn open_pty(cols: u16, rows: u16) -> io::Result<(OwnedFd, OwnedFd)> {
    let mut m: libc::c_int = -1;
    let mut s: libc::c_int = -1;
    let ws = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
    let r = unsafe { libc::openpty(&mut m, &mut s, std::ptr::null_mut(), std::ptr::null_mut(), &ws as *const _ as *mut _) };
    if r != 0 {
        return Err(io::Error::last_os_error());
    }
    let (m, s) = unsafe { (OwnedFd::from_raw_fd(m), OwnedFd::from_raw_fd(s)) };
    for fd in [m.as_raw_fd(), s.as_raw_fd()] {
        if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok((m, s))
}

fn set_nonblocking(fd: &OwnedFd) -> io::Result<()> {
    let fl = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
    if fl < 0 || unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, fl | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn set_winsize(fd: &OwnedFd, cols: u16, rows: u16) {
    let ws = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 };
    unsafe { libc::ioctl(fd.as_raw_fd(), libc::TIOCSWINSZ as _, &ws) };
}

async fn fd_read(afd: &AsyncFd<OwnedFd>, buf: &mut [u8]) -> io::Result<usize> {
    loop {
        let mut g = afd.readable().await?;
        match g.try_io(|inner| {
            let n = unsafe { libc::read(inner.as_raw_fd(), buf.as_mut_ptr() as *mut _, buf.len()) };
            if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
        }) {
            Ok(Err(e)) if e.raw_os_error() == Some(libc::EIO) => return Ok(0), // the slave side is closed
            Ok(r) => return r,
            Err(_would_block) => continue,
        }
    }
}

async fn fd_write_all(afd: &AsyncFd<OwnedFd>, mut buf: &[u8]) -> io::Result<()> {
    while !buf.is_empty() {
        let mut g = afd.writable().await?;
        match g.try_io(|inner| {
            let n = unsafe { libc::write(inner.as_raw_fd(), buf.as_ptr() as *const _, buf.len()) };
            if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
        }) {
            Ok(Ok(n)) => buf = &buf[n..],
            Ok(Err(e)) => return Err(e),
            Err(_would_block) => continue,
        }
    }
    Ok(())
}

async fn pump<R: AsyncRead + Unpin>(mut r: R, kind: u8, tx: mpsc::Sender<Vec<u8>>) {
    let mut buf = vec![0u8; 32 * 1024];
    loop {
        match r.read(&mut buf).await {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                if tx.send(frame::encode(kind, &buf[..n])).await.is_err() {
                    return;
                }
            }
        }
    }
}

/// Where stdin frames go.
enum Input {
    Pipe(Option<tokio::process::ChildStdin>),
    Pty(std::sync::Arc<AsyncFd<OwnedFd>>),
}

/// Read the client's frames until the channel closes (Ok) or breaks the protocol (Err).
async fn input_loop<R: AsyncRead + Unpin>(mut recv: R, mut input: Input) -> Result<(), String> {
    loop {
        let f = match aio::read_frame(&mut recv).await {
            Ok(Some(f)) => f,
            Ok(None) => return Ok(()),
            Err(e) => return Err(e.to_string()),
        };
        match (f.kind, &mut input) {
            (frame::STDIN, Input::Pipe(stdin)) => {
                if let Some(s) = stdin {
                    if s.write_all(&f.payload).await.is_err() {
                        *stdin = None; // the program closed its stdin: drop what follows
                    }
                }
            }
            (frame::STDIN, Input::Pty(m)) => {
                let _ = fd_write_all(m, &f.payload).await;
            }
            (frame::STDIN_EOF, Input::Pipe(stdin)) => *stdin = None,
            (frame::STDIN_EOF, Input::Pty(_)) => {}
            (frame::RESIZE, Input::Pty(m)) => {
                let (c, r) = frame::parse_resize(&f.payload).ok_or("bad resize frame")?;
                if c > 0 && r > 0 {
                    set_winsize(m.get_ref(), c, r);
                }
            }
            (frame::RESIZE, Input::Pipe(_)) => {}
            (k, _) => return Err(format!("unexpected frame type {k} from the client")),
        }
    }
}

async fn run<R>(argv: Vec<String>, h: &Header, allow: &Allow, recv: R, tx: &mpsc::Sender<Vec<u8>>, peer: &str)
where
    R: AsyncRead + Unpin + Send + 'static,
{
    let env = child_env(std::env::vars_os(), &allow.home, if h.tty { Some(h.term.as_str()) } else { None });
    let mut cmd = tokio::process::Command::new(&argv[0]);
    cmd.args(&argv[1..]).env_clear().envs(env).current_dir(&allow.home).kill_on_drop(false);
    let tty = h.tty;
    let spawned = {
        let _g = SPAWN_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        (|| -> io::Result<(tokio::process::Child, Option<OwnedFd>)> {
            if tty {
                let (cols, rows) = if h.cols > 0 && h.rows > 0 { (h.cols, h.rows) } else { (80, 24) };
                let (master, slave) = open_pty(cols, rows)?;
                cmd.stdin(Stdio::from(slave.try_clone()?))
                    .stdout(Stdio::from(slave.try_clone()?))
                    .stderr(Stdio::from(slave));
                // a new session with the pty as its controlling terminal (std has set up fds 0-2 by now)
                unsafe {
                    cmd.pre_exec(|| {
                        if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                            return Err(io::Error::last_os_error());
                        }
                        Ok(())
                    });
                }
                let child = cmd.spawn()?;
                Ok((child, Some(master)))
            } else {
                cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
                // its own session and process group, so closing the channel can hang up all of it
                unsafe {
                    cmd.pre_exec(|| {
                        if libc::setsid() < 0 {
                            return Err(io::Error::last_os_error());
                        }
                        Ok(())
                    });
                }
                Ok((cmd.spawn()?, None))
            }
        })()
    };
    drop(cmd); // closes our copies of the pty slave
    let (mut child, master) = match spawned {
        Ok(x) => x,
        Err(e) => {
            log(&format!("spawn {} for {peer}: {e}", argv[0]));
            let _ = tx.send(frame::encode(frame::ERROR, &frame::error_payload("spawn_failed", &e.to_string()))).await;
            return;
        }
    };
    let pid = child.id().map(|p| p as libc::pid_t);
    let (input, outputs): (Input, Vec<tokio::task::JoinHandle<()>>) = match master {
        Some(m) => {
            if let Err(e) = set_nonblocking(&m) {
                log(&format!("pty: {e}"));
            }
            // SAFETY: an OwnedFd stays open and the same descriptor for the AsyncFd's whole life
            let m = match unsafe { AsyncFd::register(m) } {
                Ok(a) => std::sync::Arc::new(a),
                Err(e) => {
                    let e = e.to_string();
                    log(&format!("pty: {e}"));
                    hangup(pid);
                    let _ = child.wait().await;
                    return;
                }
            };
            let m2 = m.clone();
            let txo = tx.clone();
            let out = tokio::spawn(async move {
                let mut buf = vec![0u8; 32 * 1024];
                loop {
                    match fd_read(&m2, &mut buf).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => {
                            if txo.send(frame::encode(frame::STDOUT, &buf[..n])).await.is_err() {
                                return;
                            }
                        }
                    }
                }
            });
            (Input::Pty(m), vec![out])
        }
        None => {
            let mut outs = Vec::new();
            if let Some(o) = child.stdout.take() {
                outs.push(tokio::spawn(pump(o, frame::STDOUT, tx.clone())));
            }
            if let Some(e) = child.stderr.take() {
                outs.push(tokio::spawn(pump(e, frame::STDERR, tx.clone())));
            }
            (Input::Pipe(child.stdin.take()), outs)
        }
    };
    let mut input_task = tokio::spawn(input_loop(recv, input));
    let status = tokio::select! {
        st = child.wait() => st,
        r = &mut input_task => {
            // the channel closed (or broke the protocol) while the program runs: hang it up
            if let Ok(Err(e)) = &r {
                log(&format!("channel from {peer}: {e}"));
            }
            hangup(pid);
            match tokio::time::timeout(KILL_AFTER, child.wait()).await {
                Ok(st) => st,
                Err(_) => {
                    if let Some(p) = pid {
                        unsafe { libc::kill(-p, libc::SIGKILL) };
                    }
                    child.wait().await
                }
            }
        }
    };
    input_task.abort();
    // the rest of the output, but not forever: a grandchild may hold the pipes or the pty open
    let deadline = tokio::time::Instant::now() + DRAIN_AFTER_EXIT;
    for mut o in outputs {
        if tokio::time::timeout_at(deadline, &mut o).await.is_err() {
            o.abort();
        }
    }
    let code = status.map(exit_code).unwrap_or(255);
    let _ = tx.send(frame::encode(frame::EXIT, &code.to_be_bytes())).await;
}

/// SIGHUP to the program's process group (it leads its own session).
fn hangup(pid: Option<libc::pid_t>) {
    if let Some(p) = pid {
        unsafe { libc::kill(-p, libc::SIGHUP) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use tokio::io::{AsyncWriteExt, DuplexStream, ReadHalf, WriteHalf};

    /// A home with a fake `~/.local/bin/force` running `body` (sh).
    fn home_with_force(tag: &str, body: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("force-link-serve-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let bin = d.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        let f = bin.join("force");
        std::fs::write(&f, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
        d
    }

    struct Chan {
        r: ReadHalf<DuplexStream>,
        w: WriteHalf<DuplexStream>,
        task: tokio::task::JoinHandle<()>,
    }

    /// A channel to `serve`, its header line already sent (raw: `line`).
    async fn open_raw(home: &Path, line: &[u8]) -> Chan {
        let (client, server) = tokio::io::duplex(1 << 16);
        let (sr, sw) = tokio::io::split(server);
        let allow = Allow::new(&home.to_string_lossy(), None);
        let task = tokio::spawn(async move { serve(sw, sr, &allow, "test").await });
        let (r, mut w) = tokio::io::split(client);
        w.write_all(line).await.unwrap();
        Chan { r, w, task }
    }

    async fn open(home: &Path, h: &Header) -> Chan {
        open_raw(home, &h.to_line()).await
    }

    fn header(argv: &[&str], tty: bool) -> Header {
        Header { v: 1, host: None, argv: argv.iter().map(|s| s.to_string()).collect(), tty,
                 cols: if tty { 100 } else { 0 }, rows: if tty { 30 } else { 0 },
                 term: if tty { "xterm".into() } else { String::new() } }
    }

    type Collected = (Vec<u8>, Vec<u8>, Option<i32>, Option<(String, String)>);

    /// Every frame until the end of the stream: (stdout, stderr, exit, error).
    async fn collect(r: &mut ReadHalf<DuplexStream>) -> Collected {
        let (mut out, mut err, mut exit, mut error) = (Vec::new(), Vec::new(), None, None);
        let fut = async {
            while let Some(f) = aio::read_frame(r).await.unwrap() {
                match f.kind {
                    frame::STDOUT => out.extend(f.payload),
                    frame::STDERR => err.extend(f.payload),
                    frame::EXIT => exit = frame::parse_exit(&f.payload),
                    frame::ERROR => error = Some(frame::parse_error(&f.payload)),
                    k => panic!("frame {k}"),
                }
            }
        };
        tokio::time::timeout(Duration::from_secs(10), fut).await.expect("channel ended in time");
        (out, err, exit, error)
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn pipes_stdout_stderr_stdin_and_exit_code() {
        // TERM as exported to the program (printenv), not $TERM: bash as sh (macOS) sets an unexported TERM=dumb
        let home = home_with_force("pipes", "echo \"args:$*\"; echo oops >&2; cat; \
                                             echo \"home:$HOME path:$PATH term:$(printenv TERM || echo unset)\"; \
                                             exit 3");
        let mut c = open(&home, &header(&["~/.local/bin/force", "a b", "c"], false)).await;
        c.w.write_all(&frame::encode(frame::STDIN, b"from stdin\n")).await.unwrap();
        c.w.write_all(&frame::encode(frame::STDIN_EOF, b"")).await.unwrap();
        let (out, err, exit, error) = collect(&mut c.r).await;
        let out = String::from_utf8(out).unwrap();
        assert!(out.starts_with("args:a b c\nfrom stdin\n"), "{out}");
        assert!(out.contains(&format!("home:{} path:{}/.local/bin:/usr/local/bin:/usr/bin:/bin term:unset",
                                      home.display(), home.display())), "{out}");
        assert_eq!(err, b"oops\n");
        assert_eq!((exit, error), (Some(3), None));
        c.task.await.unwrap();
        std::fs::remove_dir_all(&home).ok();
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn not_allowed_spawns_nothing() {
        let home = home_with_force("deny", "exit 0");
        let marker = home.join("ran");
        let touch = format!("touch {}", marker.display());
        for argv in [vec!["sh", "-c", &touch], vec!["/bin/sh", "-c", &touch], vec![".local/bin/force"],
                     vec!["tmux", "-L", "force"]] {
            let mut c = open(&home, &header(&argv, false)).await;
            let (out, _, exit, error) = collect(&mut c.r).await;
            assert_eq!(error.unwrap().0, "not_allowed");
            assert!(out.is_empty() && exit.is_none());
            c.task.await.unwrap();
        }
        assert!(!marker.exists());
        std::fs::remove_dir_all(&home).ok();
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn bad_header_is_bad_request() {
        let home = home_with_force("bad", "exit 0");
        for line in [&b"{\"v\":1,\"host\":\"devbox\",\"argv\":[\"~/.local/bin/force\"]}\n"[..], b"not json\n"] {
            let mut c = open_raw(&home, line).await;
            let (_, _, exit, error) = collect(&mut c.r).await;
            assert_eq!((error.unwrap().0.as_str(), exit), ("bad_request", None));
            c.task.await.unwrap();
        }
        std::fs::remove_dir_all(&home).ok();
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn tty_gets_a_controlling_pty_of_the_size() {
        // a tty on stdin, the size, TERM, and the program is the foreground process group of its terminal
        let home = home_with_force("tty", "[ -t 0 ] && echo istty; stty size; echo \"term:$TERM\"; \
                                           [ \"$(ps -o tpgid= -p $$ | tr -d ' ')\" = \"$(ps -o pgid= -p $$ | tr -d ' ')\" ] \
                                           && echo fg");
        let mut c = open(&home, &header(&["~/.local/bin/force"], true)).await;
        let (out, _, exit, error) = collect(&mut c.r).await;
        let out = String::from_utf8(out).unwrap().replace('\r', "");
        assert!(out.contains("istty\n30 100\nterm:xterm\nfg\n"), "{out:?}");
        assert_eq!((exit, error), (Some(0), None));
        c.task.await.unwrap();
        std::fs::remove_dir_all(&home).ok();
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn closing_the_channel_hangs_up_the_program() {
        let home = home_with_force("hup", "trap 'echo hup > \"$HOME/hup\"; exit 1' HUP; echo ready; \
                                           while :; do sleep 0.1; done");
        let mut c = open(&home, &header(&["~/.local/bin/force"], false)).await;
        let f = tokio::time::timeout(Duration::from_secs(10), aio::read_frame(&mut c.r)).await
            .unwrap().unwrap().unwrap();
        assert_eq!((f.kind, f.payload.as_slice()), (frame::STDOUT, &b"ready\n"[..]));
        c.w.shutdown().await.unwrap(); // the channel closes
        tokio::time::timeout(Duration::from_secs(10), c.task).await.unwrap().unwrap();
        assert_eq!(std::fs::read_to_string(home.join("hup")).unwrap(), "hup\n");
        std::fs::remove_dir_all(&home).ok();
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_client_frame_of_the_wrong_kind_ends_the_channel() {
        let home = home_with_force("proto", "echo ready; while :; do sleep 0.1; done");
        let mut c = open(&home, &header(&["~/.local/bin/force"], false)).await;
        let _ = aio::read_frame(&mut c.r).await.unwrap();
        c.w.write_all(&frame::encode(frame::EXIT, &0i32.to_be_bytes())).await.unwrap();
        tokio::time::timeout(Duration::from_secs(15), c.task).await.unwrap().unwrap();
        std::fs::remove_dir_all(&home).ok();
    }
}
