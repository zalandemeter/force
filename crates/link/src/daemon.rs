//! `force-link daemon` (SPEC §56.2): this install's direct link. It listens on UDP (QUIC, iroh) for its paired
//! peers, dials the paired remotes, and serves `force-link exec` and `force-link status` on `<state>/link.sock`.
//!
//! The endpoint talks to nobody but the two hosts of a link: no relay, no address lookup (no n0 DNS or pkarr),
//! no port mapping, no net report probes. `check_isolated` refuses to run if that ever stops being true.

use crate::allow::{resolve_tmux, Allow};
use crate::frame::{self, aio, Header};
use crate::identity::{self, PeersFile};
use crate::serve::{self, log};
use iroh::endpoint::{
    presets, AfterHandshakeOutcome, Connection, EndpointHooks, IdleTimeout, IncomingAddr, PortmapperConfig,
    QuicTransportConfig, Side, VarInt, WeakConnectionHandle,
};
use iroh::{Endpoint, EndpointAddr, NetReportConfig, PublicKey, RelayMode, SecretKey, Watcher};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};
use tokio::io::AsyncWriteExt;
use tokio::net::{UnixListener, UnixStream};

pub const ALPN: &[u8] = b"force-link/1";
pub const DIAL_TIMEOUT: Duration = Duration::from_secs(2);
pub const REDIAL_EVERY: Duration = Duration::from_secs(60);
pub const KEEPALIVE: Duration = Duration::from_secs(5);
pub const IDLE_TIMEOUT: Duration = Duration::from_secs(15);
pub const PEERS_CHECK: Duration = Duration::from_secs(2);
/// Incoming handshakes per source address and minute.
pub const RATE_PER_MIN: u32 = 10;
/// Channels a peer may have open on one connection.
pub const MAX_STREAMS: u32 = 64;

#[derive(Debug, Clone)]
pub struct Opts {
    pub state: PathBuf,
    pub hosts: PathBuf,
    pub peers: PathBuf,
    pub port: u16,
}

pub fn parse_opts(args: impl IntoIterator<Item = String>) -> Result<Opts, String> {
    let mut state = None;
    let mut hosts = None;
    let mut peers = None;
    let mut port = None;
    let mut it = args.into_iter();
    while let Some(a) = it.next() {
        let mut val = || it.next().ok_or(format!("missing value for {a}"));
        match a.as_str() {
            "--state" => state = Some(PathBuf::from(val()?)),
            "--hosts" => hosts = Some(PathBuf::from(val()?)),
            "--peers" => peers = Some(PathBuf::from(val()?)),
            "--port" => {
                let v = val()?;
                port = Some(v.parse::<u16>().map_err(|_| format!("bad port {v}"))?);
            }
            _ => return Err(format!("unexpected argument: {a}")),
        }
    }
    let port = port.ok_or("missing --port")?;
    if port != 0 && port < 1024 {
        return Err("--port must be 0 or 1024..65535".into());
    }
    Ok(Opts {
        state: state.ok_or("missing --state")?,
        hosts: hosts.ok_or("missing --hosts")?,
        peers: peers.ok_or("missing --peers")?,
        port,
    })
}

/// Per source address: at most RATE_PER_MIN handshakes in a minute.
#[derive(Default)]
pub struct RateLimit {
    seen: HashMap<IpAddr, (Instant, u32)>,
}

impl RateLimit {
    pub fn allow(&mut self, ip: IpAddr, now: Instant) -> bool {
        if self.seen.len() > 4096 {
            self.seen.retain(|_, (t, _)| now.duration_since(*t) < Duration::from_secs(60));
        }
        let e = self.seen.entry(ip).or_insert((now, 0));
        if now.duration_since(e.0) >= Duration::from_secs(60) {
            *e = (now, 0);
        }
        if e.1 >= RATE_PER_MIN {
            return false;
        }
        e.1 += 1;
        true
    }
}

/// At most `per_min` log lines a minute for refusals (an address that floods us cannot flood the log).
struct LogBudget {
    start: Instant,
    used: u32,
    per_min: u32,
}

impl LogBudget {
    fn take(&mut self) -> bool {
        let now = Instant::now();
        if now.duration_since(self.start) >= Duration::from_secs(60) {
            self.start = now;
            self.used = 0;
        }
        self.used += 1;
        self.used <= self.per_min
    }
}

/// One remote this daemon dials.
#[derive(Default)]
struct Remote {
    card: Option<identity::RemoteCard>,
    conn: Option<Connection>,
    dialing: bool,
    last_dial: Option<Instant>,
    dialed_card: Option<identity::RemoteCard>,
    state: &'static str,
    reason: String,
    since_ms: u64,
}

impl Remote {
    fn set(&mut self, state: &'static str, reason: &str) {
        if self.state != state || self.reason != reason {
            self.state = state;
            self.reason = reason.to_string();
            self.since_ms = identity::now_ms();
        }
    }
}

struct Shared {
    opts: Opts,
    allow: Allow,
    peers: RwLock<HashMap<PublicKey, String>>,
    remotes: Mutex<HashMap<String, Remote>>,
    incoming: Mutex<Vec<(PublicKey, WeakConnectionHandle)>>,
    rate: Mutex<RateLimit>,
    refusals: Mutex<LogBudget>,
    addrs_changed: AtomicBool,
    port: Mutex<u16>,
    addrs: Mutex<Vec<String>>,
}

impl Shared {
    fn refusal(&self, msg: &str) {
        if self.refusals.lock().unwrap().take() {
            log(msg);
        }
    }
}

/// The handshake gate: an incoming connection is closed right after the handshake, before any stream is accepted,
/// unless its key is in link-peers.json; an outgoing one must speak our ALPN (its key is the one we dialed: TLS
/// verified it, and `dial` checks it again).
#[derive(Clone)]
struct Gate(Arc<Shared>);

impl std::fmt::Debug for Gate {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Gate")
    }
}

impl EndpointHooks for Gate {
    async fn after_handshake<'a>(&'a self, conn: &'a Connection) -> AfterHandshakeOutcome {
        if conn.alpn() != ALPN {
            return AfterHandshakeOutcome::Reject { error_code: VarInt::from_u32(400), reason: b"alpn".to_vec() };
        }
        if conn.side() == Side::Client {
            return AfterHandshakeOutcome::Accept;
        }
        let id = conn.remote_id();
        if self.0.peers.read().unwrap().contains_key(&id) {
            return AfterHandshakeOutcome::Accept;
        }
        self.0.refusal(&format!("refused unknown id {}", id.fmt_short()));
        AfterHandshakeOutcome::Reject { error_code: VarInt::from_u32(403), reason: b"unknown".to_vec() }
    }
}

/// Bind the endpoint: our key, our ALPN, UDP `port` on all IPv4 (required) and IPv6 (when the host has it)
/// addresses, nothing else.
pub async fn bind_endpoint(key: SecretKey, port: u16, hooks: impl EndpointHooks + 'static) -> Result<Endpoint, String> {
    let transport = QuicTransportConfig::builder()
        .keep_alive_interval(KEEPALIVE)
        .max_idle_timeout(Some(IdleTimeout::try_from(IDLE_TIMEOUT).expect("idle timeout")))
        .max_concurrent_bidi_streams(VarInt::from_u32(MAX_STREAMS))
        .max_concurrent_uni_streams(VarInt::from_u32(1)) // the peer's READY
        .build();
    let v6 = iroh::endpoint::BindOpts::default().set_is_required(false);
    let ep = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .portmapper_config(PortmapperConfig::Disabled)
        .net_report_config(NetReportConfig::minimal())
        .secret_key(key)
        .alpns(vec![ALPN.to_vec()])
        .transport_config(transport)
        .hooks(hooks)
        .clear_ip_transports()
        .bind_addr(SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), port))
        .map_err(|e| format!("bind: {e}"))?
        .bind_addr_with_opts(SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), port), v6)
        .map_err(|e| format!("bind: {e}"))?
        .bind()
        .await
        .map_err(|e| format!("bind UDP port {port}: {e}"))?;
    check_isolated(&ep)?;
    Ok(ep)
}

/// The endpoint may reach nobody but the hosts we dial: no address lookup service, no relay.
pub fn check_isolated(ep: &Endpoint) -> Result<(), String> {
    let lookups = ep.address_lookup().map(|l| l.len()).unwrap_or(0);
    if lookups != 0 {
        return Err(format!("endpoint has {lookups} address lookup services"));
    }
    if !ep.home_relay_status().get().is_empty() || ep.addr().relay_urls().next().is_some() {
        return Err("endpoint has a relay".into());
    }
    Ok(())
}

/// Our dialable addresses as `ip:port` strings (the endpoint's direct addresses, else the bound sockets).
fn own_addrs(ep: &Endpoint) -> Vec<String> {
    let mut v: Vec<String> = ep.addr().ip_addrs().map(|a| a.to_string()).collect();
    if v.is_empty() {
        v = ep.bound_sockets().iter().map(|a| a.to_string()).collect();
    }
    v.sort();
    v.dedup();
    v
}

fn bound_port(ep: &Endpoint) -> u16 {
    let socks = ep.bound_sockets();
    socks.iter().find(|a| a.is_ipv4()).or(socks.first()).map(|a| a.port()).unwrap_or(0)
}

/// An exclusive lock on `<state>/link/lock`, held for the daemon's life: a second daemon on the same state dir
/// refuses to start.
fn lock(path: &Path) -> Result<std::fs::File, String> {
    use std::os::unix::fs::OpenOptionsExt;
    let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).mode(0o600).open(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(format!("another force-link daemon runs on {}", path.parent().unwrap_or(path).display()));
    }
    Ok(f)
}

/// `<state>/link.sock`, 0600 from the start (umask), after removing a stale one (we hold the lock).
fn listen_local(path: &Path) -> Result<UnixListener, String> {
    match std::fs::remove_file(path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", path.display())),
    }
    let old = unsafe { libc::umask(0o177) };
    let l = UnixListener::bind(path);
    unsafe { libc::umask(old) };
    let l = l.map_err(|e| format!("{}: {e}", path.display()))?;
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(l)
}

pub fn run(opts: Opts) -> i32 {
    let rt = match tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build() {
        Ok(rt) => rt,
        Err(e) => {
            log(&format!("runtime: {e}"));
            return 1;
        }
    };
    // main returns only when stopped by SIGTERM/SIGINT: exit as a process killed by it would (143), so a service
    // manager that restarts on failure restarts a daemon killed by hand, and none stays down unnoticed
    match rt.block_on(main(opts)) {
        Ok(()) => 143,
        Err(e) => {
            log(&e);
            1
        }
    }
}

async fn main(opts: Opts) -> Result<(), String> {
    let link_dir = opts.state.join("link");
    identity::create_private_dir(&link_dir)?;
    let _lock = lock(&link_dir.join("lock"))?;
    let key = identity::load_or_create_key(&link_dir.join("key"))?;
    let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
    if !home.starts_with('/') {
        return Err("HOME must be an absolute path".into());
    }
    let path = std::env::var("PATH").unwrap_or_else(|_| "/usr/local/bin:/usr/bin:/bin".into());
    let allow = Allow::new(&home, resolve_tmux(&home, &path));
    let mut peers = PeersFile::new(opts.peers.clone(), PEERS_CHECK);
    if let Some(msg) = peers.refresh(Instant::now()) {
        log(&msg);
    }
    let shared = Arc::new(Shared {
        opts: opts.clone(),
        allow,
        peers: RwLock::new(peers.peers.clone()),
        remotes: Mutex::new(HashMap::new()),
        incoming: Mutex::new(Vec::new()),
        rate: Mutex::new(RateLimit::default()),
        refusals: Mutex::new(LogBudget { start: Instant::now(), used: 0, per_min: 60 }),
        addrs_changed: AtomicBool::new(false),
        port: Mutex::new(0),
        addrs: Mutex::new(Vec::new()),
    });
    let ep = bind_endpoint(key, opts.port, Gate(shared.clone())).await?;
    let port = bound_port(&ep);
    *shared.port.lock().unwrap() = port;
    write_self(&shared, &ep);
    let listener = listen_local(&opts.state.join("link.sock"))?;
    log(&format!("started: id {}, UDP port {port}, tmux {}", ep.id(),
                 shared.allow.tmux.as_deref().unwrap_or("(none)")));

    tokio::spawn(accept_quic(ep.clone(), shared.clone()));
    tokio::spawn(accept_local(listener, ep.clone(), shared.clone()));
    tokio::spawn(watch_addrs(ep.clone(), shared.clone()));
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).map_err(|e| format!("signals: {e}"))?;
    let mut int = signal(SignalKind::interrupt()).map_err(|e| format!("signals: {e}"))?;
    // the manager: peers file, remotes to dial, their status (once a second)
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            _ = tick.tick() => {}
            _ = term.recv() => break,
            _ = int.recv() => break,
        }
        if let Some(msg) = peers.refresh(Instant::now()) {
            log(&msg);
            *shared.peers.write().unwrap() = peers.peers.clone();
            drop_untrusted(&shared);
        }
        manage_remotes(&ep, &shared);
    }
    // stopped: tell the peers at once (they go back to ssh now, not after the idle timeout), mark our remotes ssh
    log("stopping");
    let _ = std::fs::remove_file(opts.state.join("link.sock"));
    for (name, r) in shared.remotes.lock().unwrap().iter_mut() {
        r.conn = None;
        r.set("ssh", "link daemon stopped");
        let _ = identity::write_link_state(&link_json(&shared, name), r.state, &r.reason, r.since_ms);
    }
    let _ = tokio::time::timeout(Duration::from_secs(2), ep.close()).await;
    Ok(())
}

fn write_self(shared: &Shared, ep: &Endpoint) {
    let addrs = own_addrs(ep);
    let port = *shared.port.lock().unwrap();
    *shared.addrs.lock().unwrap() = addrs.clone();
    if let Err(e) = identity::write_self(&shared.opts.state.join("link").join("self.json"), &ep.id(), port, &addrs) {
        log(&format!("self.json: {e}"));
    }
}

async fn watch_addrs(ep: Endpoint, shared: Arc<Shared>) {
    let mut w = ep.watch_addr();
    let mut last = own_addrs(&ep);
    while w.updated().await.is_ok() {
        let now = own_addrs(&ep);
        if now != last {
            log(&format!("addresses: {}", now.join(" ")));
            write_self(&shared, &ep);
            shared.addrs_changed.store(true, Ordering::SeqCst);
            last = now;
        }
    }
}

/// Close incoming connections from ids that are no longer listed (`force link untrust`, a removed host).
fn drop_untrusted(shared: &Shared) {
    let peers = shared.peers.read().unwrap().clone();
    let mut inc = shared.incoming.lock().unwrap();
    inc.retain(|(id, weak)| {
        let Some(conn) = weak.upgrade() else { return false };
        if peers.contains_key(id) {
            return true;
        }
        log(&format!("closed the connection of {} (no longer a peer)", id.fmt_short()));
        conn.close(VarInt::from_u32(403), b"untrusted");
        false
    });
}

// ------------------------------------------------------------------------------------------------- incoming --
async fn accept_quic(ep: Endpoint, shared: Arc<Shared>) {
    while let Some(incoming) = ep.accept().await {
        let ip = match incoming.remote_addr() {
            IncomingAddr::Ip(sa) => sa.ip(),
            _ => {
                incoming.ignore();
                continue;
            }
        };
        // make the source prove its address first (a QUIC retry, stateless): a spoofed address can neither use
        // up another's handshakes nor make us do any work
        if !incoming.remote_addr_validated() {
            if let Err(e) = incoming.retry() {
                e.into_incoming().ignore();
            }
            continue;
        }
        if !shared.rate.lock().unwrap().allow(ip, Instant::now()) {
            shared.refusal(&format!("refused a handshake from {ip}: more than {RATE_PER_MIN} a minute"));
            incoming.ignore();
            continue;
        }
        let shared = shared.clone();
        tokio::spawn(async move {
            match tokio::time::timeout(Duration::from_secs(10), incoming).await {
                Ok(Ok(conn)) => serve_conn(conn, shared).await,
                Ok(Err(e)) => shared.refusal(&format!("handshake from {ip} failed: {e}")),
                Err(_) => shared.refusal(&format!("handshake from {ip} timed out")),
            }
        });
    }
}

async fn serve_conn(conn: Connection, shared: Arc<Shared>) {
    let id = conn.remote_id();
    // the gate already checked this; again here, so no stream is ever served for an unknown id
    let name = match shared.peers.read().unwrap().get(&id) {
        Some(n) => n.clone(),
        None => {
            conn.close(VarInt::from_u32(403), b"unknown");
            return;
        }
    };
    shared.incoming.lock().unwrap().push((id, conn.weak_handle()));
    let peer = if name.is_empty() { id.fmt_short().to_string() } else { name };
    log(&format!("peer {peer} connected"));
    let ready = async {
        let mut s = conn.open_uni().await.ok()?;
        s.write_all(READY).await.ok()?;
        s.finish().ok()
    };
    if tokio::time::timeout(DIAL_TIMEOUT, ready).await.ok().flatten().is_none() {
        log(&format!("peer {peer}: could not send ready"));
        return;
    }
    loop {
        let (send, recv) = match conn.accept_bi().await {
            Ok(s) => s,
            Err(_) => break,
        };
        if !shared.peers.read().unwrap().contains_key(&id) {
            conn.close(VarInt::from_u32(403), b"untrusted");
            break;
        }
        let shared = shared.clone();
        let peer = peer.clone();
        tokio::spawn(async move { serve::serve(send, recv, &shared.allow, &peer).await });
    }
    log(&format!("peer {peer} disconnected"));
}

// ------------------------------------------------------------------------------------------------- outgoing --
/// The enabled hosts of the hosts file (valid names only).
fn enabled_hosts(path: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(path) else { return vec![] };
    let Ok(v) = serde_json::from_str::<Value>(&text) else { return vec![] };
    let mut out = Vec::new();
    for h in v.get("hosts").and_then(|h| h.as_array()).into_iter().flatten() {
        let Some(name) = h.get("name").and_then(|n| n.as_str()) else { continue };
        if crate::hosts::valid_name(name) && h.get("enabled").and_then(|e| e.as_bool()) != Some(false) {
            out.push(name.to_string());
        }
    }
    out
}

fn link_json(shared: &Shared, name: &str) -> PathBuf {
    shared.opts.state.join("remotes").join(name).join("link.json")
}

fn manage_remotes(ep: &Endpoint, shared: &Arc<Shared>) {
    let wanted = enabled_hosts(&shared.opts.hosts);
    let addrs_changed = shared.addrs_changed.swap(false, Ordering::SeqCst);
    let now = Instant::now();
    let mut remotes = shared.remotes.lock().unwrap();
    remotes.retain(|name, r| {
        if wanted.contains(name) {
            return true;
        }
        if let Some(c) = r.conn.take() {
            c.close(VarInt::from_u32(0), b"removed");
        }
        false
    });
    for name in wanted {
        let path = link_json(shared, &name);
        let card = identity::read_card(&path);
        let r = remotes.entry(name.clone()).or_insert_with(|| Remote { state: "ssh", ..Default::default() });
        // a connection that ended, or to an id that is no longer the recorded one
        if let Some(c) = &r.conn {
            let gone = c.close_reason().map(|e| format!("closed: {e}"));
            let other = match &card {
                Some(k) if k.id == c.remote_id() => None,
                _ => Some("not paired".to_string()),
            };
            if let Some(why) = gone.or(other) {
                c.close(VarInt::from_u32(0), b"repaired");
                log(&format!("{name}: ssh ({why})"));
                r.conn = None;
                r.set("ssh", &why);
            }
        }
        r.card = card.clone();
        match &card {
            None => {
                r.set("ssh", "not paired");
            }
            Some(k) => {
                let due = match r.last_dial {
                    None => true,
                    Some(t) => now.duration_since(t) >= REDIAL_EVERY,
                };
                let changed = r.dialed_card.as_ref() != Some(k);
                if r.conn.is_none() && !r.dialing && (due || changed || addrs_changed) {
                    r.dialing = true;
                    r.last_dial = Some(now);
                    r.dialed_card = Some(k.clone());
                    tokio::spawn(dial(ep.clone(), shared.clone(), name.clone(), k.clone()));
                }
            }
        }
        match identity::write_link_state(&path, r.state, &r.reason, r.since_ms) {
            Ok(_) => {}
            Err(e) => log(&format!("{}: {e}", path.display())),
        }
    }
}

/// What a peer sends on its first unidirectional stream once it accepted us. The client side of a QUIC handshake
/// completes before the server has checked the client's key, so a refused dial would otherwise look connected for
/// a round trip.
pub const READY: &[u8] = b"force-link ready\n";

/// Connect and wait for the peer's READY, all within DIAL_TIMEOUT.
async fn connect_ready(ep: &Endpoint, addr: EndpointAddr) -> Result<Connection, String> {
    let conn = ep.connect(addr, ALPN).await.map_err(|e| format!("dial failed: {e}"))?;
    let ready = async {
        let mut s = conn.accept_uni().await.ok()?;
        s.read_to_end(READY.len()).await.ok()
    };
    tokio::select! {
        got = ready => match got {
            Some(b) if b == READY => Ok(conn),
            _ => Err(match conn.close_reason() {
                Some(e) => format!("refused: {e}"),
                None => "no ready".to_string(),
            }),
        },
        e = conn.closed() => Err(format!("refused: {e}")),
    }
}

async fn dial(ep: Endpoint, shared: Arc<Shared>, name: String, card: identity::RemoteCard) {
    let addr = EndpointAddr::new(card.id).with_addrs(card.addrs.iter().map(|a| iroh::TransportAddr::Ip(*a)));
    let res = tokio::time::timeout(DIAL_TIMEOUT, connect_ready(&ep, addr)).await;
    let mut remotes = shared.remotes.lock().unwrap();
    let Some(r) = remotes.get_mut(&name) else { return };
    r.dialing = false;
    match res {
        Ok(Ok(conn)) if conn.remote_id() == card.id && conn.alpn() == ALPN => {
            if r.card.as_ref().map(|k| k.id) != Some(card.id) {
                conn.close(VarInt::from_u32(0), b"repaired"); // re-paired while we dialed
                return;
            }
            let via = conn.paths().iter().find(|p| p.is_selected()).map(|p| format!("{:?}", p.remote_addr()))
                .unwrap_or_default();
            log(&format!("{name}: direct {via}"));
            r.conn = Some(conn);
            r.set("direct", "");
        }
        Ok(Ok(conn)) => {
            conn.close(VarInt::from_u32(0), b"wrong id");
            log(&format!("{name}: ssh (answered with another id)"));
            r.set("ssh", "wrong id");
        }
        Ok(Err(e)) => {
            log(&format!("{name}: ssh ({e})"));
            r.set("ssh", if e.starts_with("refused") { "refused" } else { "dial failed" });
        }
        Err(_) => {
            log(&format!("{name}: ssh (dial timed out)"));
            r.set("ssh", "timeout");
        }
    }
}

/// The selected path's round trip, in ms.
fn rtt_ms(c: &Connection) -> Option<f64> {
    c.paths().iter().find(|p| p.is_selected()).map(|p| p.rtt().as_secs_f64() * 1000.0)
}

// ---------------------------------------------------------------------------------------------- local socket --
async fn accept_local(listener: UnixListener, ep: Endpoint, shared: Arc<Shared>) {
    loop {
        let (stream, _) = match listener.accept().await {
            Ok(s) => s,
            Err(e) => {
                log(&format!("link.sock: {e}"));
                tokio::time::sleep(Duration::from_millis(200)).await;
                continue;
            }
        };
        let shared = shared.clone();
        let ep = ep.clone();
        tokio::spawn(async move { local(stream, ep, shared).await });
    }
}

async fn local(mut stream: UnixStream, ep: Endpoint, shared: Arc<Shared>) {
    // the socket is 0600 in a private folder; the caller must be this user all the same
    match stream.peer_cred() {
        Ok(c) if c.uid() == unsafe { libc::getuid() } => {}
        _ => return,
    }
    let line = match tokio::time::timeout(Duration::from_secs(5), aio::read_line(&mut stream, frame::MAX_HEADER)).await {
        Ok(Ok(l)) => l,
        _ => return,
    };
    if let Ok(v) = serde_json::from_slice::<Value>(&line) {
        if v.get("op").and_then(|o| o.as_str()) == Some("status") {
            let mut s = serde_json::to_vec(&status(&ep, &shared)).unwrap_or_default();
            s.push(b'\n');
            let _ = stream.write_all(&s).await;
            return;
        }
    }
    let conn = Header::parse(&line).ok().and_then(|h| {
        let host = h.host.clone()?;
        let remotes = shared.remotes.lock().unwrap();
        let c = remotes.get(&host)?.conn.clone()?;
        if c.close_reason().is_some() {
            return None;
        }
        Some((h, c))
    });
    let Some((h, conn)) = conn else {
        let _ = stream.write_all(b"ssh\n").await;
        return;
    };
    let opened = tokio::time::timeout(DIAL_TIMEOUT, conn.open_bi()).await;
    let (mut send, recv) = match opened {
        Ok(Ok(s)) => s,
        _ => {
            let _ = stream.write_all(b"ssh\n").await;
            return;
        }
    };
    let quic_header = Header { host: None, ..h };
    if send.write_all(&quic_header.to_line()).await.is_err() || stream.write_all(b"direct\n").await.is_err() {
        let _ = stream.write_all(b"ssh\n").await;
        return;
    }
    // from here the bytes pass through as they are: the serving side checks every frame
    let (mut ur, mut uw) = stream.into_split();
    let mut recv = recv;
    let up = async move {
        let _ = tokio::io::copy(&mut ur, &mut send).await;
        let _ = send.finish(); // the client went away: the channel is closed (the remote hangs up)
    };
    let down = async move {
        let _ = tokio::io::copy(&mut recv, &mut uw).await;
        let _ = uw.shutdown().await;
    };
    tokio::join!(up, down);
}

fn status(ep: &Endpoint, shared: &Shared) -> Value {
    let remotes = shared.remotes.lock().unwrap();
    let mut names: Vec<&String> = remotes.keys().collect();
    names.sort();
    let list: Vec<Value> = names.into_iter().map(|n| {
        let r = &remotes[n];
        json!({
            "name": n,
            "state": r.state,
            "reason": r.reason,
            "since_ms": r.since_ms,
            "rtt_ms": r.conn.as_ref().and_then(rtt_ms),
        })
    }).collect();
    json!({
        "running": true,
        "id": ep.id().to_string(),
        "port": *shared.port.lock().unwrap(),
        "addrs": *shared.addrs.lock().unwrap(),
        "peers": shared.peers.read().unwrap().len(),
        "remotes": list,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opts_parse() {
        let o = parse_opts(["--state", "/s", "--hosts", "/h", "--peers", "/p", "--port", "47823"].map(String::from))
            .unwrap();
        assert_eq!((o.state, o.port), (PathBuf::from("/s"), 47823));
        assert!(parse_opts(["--state", "/s", "--hosts", "/h", "--peers", "/p", "--port", "80"].map(String::from)).is_err());
        assert!(parse_opts(["--state", "/s", "--hosts", "/h", "--peers", "/p"].map(String::from)).is_err());
        assert!(parse_opts(["--port", "0", "--bogus"].map(String::from)).is_err());
        assert_eq!(parse_opts(["--state", "/s", "--hosts", "/h", "--peers", "/p", "--port", "0"].map(String::from))
                       .unwrap().port, 0);
    }

    #[test]
    fn rate_limit_per_address_and_minute() {
        let mut r = RateLimit::default();
        let t = Instant::now();
        let a: IpAddr = "192.0.2.1".parse().unwrap();
        let b: IpAddr = "192.0.2.2".parse().unwrap();
        for _ in 0..RATE_PER_MIN {
            assert!(r.allow(a, t));
        }
        assert!(!r.allow(a, t + Duration::from_secs(30)));
        assert!(r.allow(b, t + Duration::from_secs(30)));
        assert!(r.allow(a, t + Duration::from_secs(61)));
    }

    #[test]
    fn hosts_file_enabled_and_valid_only() {
        let d = std::env::temp_dir().join(format!("force-link-hosts-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("hosts.json");
        std::fs::write(&p, r#"{"hosts":[{"name":"devbox","ssh":"devbox"},{"name":"off","ssh":"x","enabled":false},
                               {"name":"../evil","ssh":"x"},{"ssh":"noname"}]}"#).unwrap();
        assert_eq!(enabled_hosts(&p), vec!["devbox".to_string()]);
        assert!(enabled_hosts(&d.join("missing.json")).is_empty());
        std::fs::remove_dir_all(&d).ok();
    }

    /// The endpoint has no address lookup and no relay, and two of them connect on loopback with nothing else.
    #[tokio::test]
    async fn endpoint_is_isolated_and_pins_ids() {
        #[derive(Debug)]
        struct Open;
        impl EndpointHooks for Open {}
        let a = bind_endpoint(SecretKey::generate(), 0, Open).await.unwrap();
        let b = bind_endpoint(SecretKey::generate(), 0, Open).await.unwrap();
        check_isolated(&a).unwrap();
        assert!(a.address_lookup().unwrap().is_empty());
        assert!(a.addr().relay_urls().next().is_none());
        let port = bound_port(&b);
        let lo = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
        let bb = b.clone();
        tokio::spawn(async move {
            while let Some(inc) = bb.accept().await {
                tokio::spawn(async move { if let Ok(c) = inc.await { c.closed().await; } });
            }
        });
        let c = tokio::time::timeout(Duration::from_secs(5),
                                     a.connect(EndpointAddr::new(b.id()).with_ip_addr(lo), ALPN)).await
            .unwrap().unwrap();
        assert_eq!(c.remote_id(), b.id());
        // dialing b's address with another id fails: TLS verifies the key we pinned
        let other = SecretKey::generate().public();
        let bad = tokio::time::timeout(Duration::from_secs(5),
                                       a.connect(EndpointAddr::new(other).with_ip_addr(lo), ALPN)).await;
        assert!(matches!(bad, Ok(Err(_))), "a connection to the wrong key");
        // with no address there is nothing to look it up with: it never connects
        let none = tokio::time::timeout(Duration::from_millis(300), a.connect(EndpointAddr::new(other), ALPN)).await;
        assert!(!matches!(none, Ok(Ok(_))));
    }
}
