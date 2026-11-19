//! The files of the direct link (SPEC §56.2): this install's key, the peers it accepts, its own address card
//! (`self.json`) and the per-remote status (`link.json`).

use iroh::{PublicKey, SecretKey};
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

/// A link id as Force writes it: iroh's display form of a public key, 64 lowercase hex digits.
pub fn parse_id(s: &str) -> Option<PublicKey> {
    if s.len() != 64 || !s.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f')) {
        return None;
    }
    s.parse::<PublicKey>().ok()
}

fn uid() -> u32 {
    unsafe { libc::getuid() }
}

/// `<state>/link/key`: 32 secret bytes. Made (0600, never replaced) when missing; refused when it can be read or
/// written by anyone but its owner, is not ours, or is not 32 bytes.
pub fn load_or_create_key(path: &Path) -> Result<SecretKey, String> {
    if let Some(dir) = path.parent() {
        create_private_dir(dir)?;
    }
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if !meta.file_type().is_file() {
                return Err(format!("{}: not a regular file", path.display()));
            }
            if meta.mode() & 0o077 != 0 {
                return Err(format!("{}: mode {:o}: group or other may read it (chmod 600)", path.display(),
                                   meta.mode() & 0o777));
            }
            if meta.uid() != uid() {
                return Err(format!("{}: not owned by this user", path.display()));
            }
            let mut f = fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
            let mut buf = Vec::new();
            f.read_to_end(&mut buf).map_err(|e| format!("{}: {e}", path.display()))?;
            let bytes: [u8; 32] = buf.as_slice().try_into()
                .map_err(|_| format!("{}: not a 32-byte key", path.display()))?;
            Ok(SecretKey::from_bytes(&bytes))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let key = SecretKey::generate();
            let tmp = path.with_extension(format!("tmp.{}", std::process::id()));
            let mut f = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)
                .map_err(|e| format!("{}: {e}", tmp.display()))?;
            f.write_all(&key.to_bytes()).and_then(|_| f.sync_all())
                .map_err(|e| format!("{}: {e}", tmp.display()))?;
            // a hard link fails when the key appeared meanwhile: never replace a key
            let r = fs::hard_link(&tmp, path);
            let _ = fs::remove_file(&tmp);
            match r {
                Ok(()) => Ok(key),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => load_or_create_key(path),
                Err(e) => Err(format!("{}: {e}", path.display())),
            }
        }
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// mkdir -p with mode 0700 for what it creates.
pub fn create_private_dir(dir: &Path) -> Result<(), String> {
    use std::os::unix::fs::DirBuilderExt;
    fs::DirBuilder::new().recursive(true).mode(0o700).create(dir).map_err(|e| format!("{}: {e}", dir.display()))
}

/// Write `data` to `path` through a temp file in the same folder and a rename (mode 0600).
pub fn write_atomic(path: &Path, data: &[u8]) -> io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = dir.join(format!(".{name}.{}.{:?}.tmp", std::process::id(), std::thread::current().id()));
    let r = (|| {
        let mut f = OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
        f.write_all(data)?;
        drop(f);
        fs::rename(&tmp, path)
    })();
    if r.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    r
}

/// The peers this daemon accepts: `{"peers":[{"id","name"}]}` from `link-peers.json`. The file must be ours and
/// not writable or readable by group or other; otherwise (or unreadable, or not that shape) nobody is accepted.
/// Entries with a bad id are skipped.
pub fn read_peers(path: &Path) -> Result<HashMap<PublicKey, String>, String> {
    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(HashMap::new()),
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    if meta.mode() & 0o077 != 0 {
        return Err(format!("{}: mode {:o}: group or other has access (chmod 600); accepting nobody",
                           path.display(), meta.mode() & 0o777));
    }
    if meta.uid() != uid() {
        return Err(format!("{}: not owned by this user; accepting nobody", path.display()));
    }
    let text = fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("{}: {e}; accepting nobody", path.display()))?;
    let mut out = HashMap::new();
    if let Some(list) = v.get("peers").and_then(|p| p.as_array()) {
        for p in list {
            let id = p.get("id").and_then(|x| x.as_str()).and_then(parse_id);
            let name = p.get("name").and_then(|x| x.as_str()).unwrap_or("").to_string();
            if let Some(id) = id {
                out.insert(id, name);
            }
        }
    }
    Ok(out)
}

/// The peers file, reloaded when its mtime (or size) changes, checked at most every `every`.
pub struct PeersFile {
    path: PathBuf,
    every: Duration,
    last_check: Option<Instant>,
    loaded: bool,
    stamp: Option<(SystemTime, u64, u32, u64)>,
    pub peers: HashMap<PublicKey, String>,
}

impl PeersFile {
    pub fn new(path: PathBuf, every: Duration) -> PeersFile {
        PeersFile { path, every, last_check: None, loaded: false, stamp: None, peers: HashMap::new() }
    }

    /// Reload when due and changed. Returns Some(message) when it reloaded (for the log), None otherwise.
    pub fn refresh(&mut self, now: Instant) -> Option<String> {
        if let Some(t) = self.last_check {
            if now.duration_since(t) < self.every {
                return None;
            }
        }
        self.last_check = Some(now);
        let stamp = fs::metadata(&self.path).ok()
            .map(|m| (m.modified().unwrap_or(SystemTime::UNIX_EPOCH), m.len(), m.mode(), m.ino()));
        if self.loaded && stamp == self.stamp {
            return None;
        }
        let first = !self.loaded;
        self.loaded = true;
        self.stamp = stamp;
        match read_peers(&self.path) {
            Ok(p) => {
                let changed = p != self.peers;
                self.peers = p;
                if changed || first {
                    Some(format!("peers: {} accepted", self.peers.len()))
                } else {
                    None
                }
            }
            Err(e) => {
                self.peers.clear();
                Some(e)
            }
        }
    }
}

/// `<state>/link/self.json`: `{"id","port","addrs"}`.
pub fn write_self(path: &Path, id: &PublicKey, port: u16, addrs: &[String]) -> io::Result<()> {
    let v = serde_json::json!({"id": id.to_string(), "port": port, "addrs": addrs});
    write_atomic(path, &serde_json::to_vec(&v).expect("json"))
}

/// The dialable part of a remote's `link.json` (written by the Python side): its id and addresses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemoteCard {
    pub id: PublicKey,
    pub addrs: Vec<std::net::SocketAddr>,
}

/// Parse the `addrs` of a card: `"ip:port"` strings, or bare IPs combined with `port`. At most 16; others skipped.
pub fn parse_addrs(v: &Value) -> Vec<std::net::SocketAddr> {
    let port = v.get("port").and_then(|p| p.as_u64()).filter(|p| (1..=65535).contains(p)).map(|p| p as u16);
    let mut out = Vec::new();
    for a in v.get("addrs").and_then(|a| a.as_array()).into_iter().flatten() {
        let Some(s) = a.as_str() else { continue };
        if let Ok(sa) = s.parse::<std::net::SocketAddr>() {
            if sa.port() != 0 {
                out.push(sa);
            }
        } else if let (Ok(ip), Some(p)) = (s.parse::<std::net::IpAddr>(), port) {
            out.push(std::net::SocketAddr::new(ip, p));
        }
        if out.len() >= 16 {
            break;
        }
    }
    out
}

/// A remote's card from its `link.json`, when it has an id and at least one address.
pub fn read_card(path: &Path) -> Option<RemoteCard> {
    let v: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let id = parse_id(v.get("id")?.as_str()?)?;
    let addrs = parse_addrs(&v);
    if addrs.is_empty() {
        return None;
    }
    Some(RemoteCard { id, addrs })
}

/// Read-modify-write `link.json`: set `state`, `reason`, `since_ms`, keep everything else (id, addrs, port belong
/// to the Python side). Nothing is written when the file is gone or not an object (the host was removed) or when
/// the three values are already there.
pub fn write_link_state(path: &Path, state: &str, reason: &str, since_ms: u64) -> io::Result<bool> {
    let bytes = match fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let mut obj: Map<String, Value> = match serde_json::from_slice(&bytes) {
        Ok(Value::Object(m)) => m,
        _ => return Ok(false),
    };
    let same = obj.get("state").and_then(|v| v.as_str()) == Some(state)
        && obj.get("reason").and_then(|v| v.as_str()) == Some(reason)
        && obj.get("since_ms").and_then(|v| v.as_u64()) == Some(since_ms);
    if same {
        return Ok(false);
    }
    obj.insert("state".into(), Value::from(state));
    obj.insert("reason".into(), Value::from(reason));
    obj.insert("since_ms".into(), Value::from(since_ms));
    write_atomic(path, &serde_json::to_vec(&Value::Object(obj)).expect("json"))?;
    Ok(true)
}

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("force-link-{tag}-{}-{:?}", std::process::id(),
                                                  std::thread::current().id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn key_is_made_0600_and_kept() {
        let d = tmpdir("key");
        let p = d.join("link/key");
        let k1 = load_or_create_key(&p).unwrap();
        let meta = fs::metadata(&p).unwrap();
        assert_eq!(meta.mode() & 0o777, 0o600);
        assert_eq!(meta.len(), 32);
        assert_eq!(fs::metadata(d.join("link")).unwrap().mode() & 0o777, 0o700);
        let k2 = load_or_create_key(&p).unwrap();
        assert_eq!(k1.public(), k2.public());
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn key_with_group_or_other_bits_is_refused() {
        let d = tmpdir("keyperm");
        let p = d.join("key");
        load_or_create_key(&p).unwrap();
        for mode in [0o640, 0o604, 0o660, 0o644] {
            fs::set_permissions(&p, fs::Permissions::from_mode(mode)).unwrap();
            let e = load_or_create_key(&p).unwrap_err();
            assert!(e.contains("group or other"), "{e}");
        }
        fs::set_permissions(&p, fs::Permissions::from_mode(0o400)).unwrap();
        assert!(load_or_create_key(&p).is_ok());
        fs::write(d.join("short"), b"abc").unwrap();
        fs::set_permissions(d.join("short"), fs::Permissions::from_mode(0o600)).unwrap();
        assert!(load_or_create_key(&d.join("short")).unwrap_err().contains("32-byte"));
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn ids_are_64_lowercase_hex() {
        let id = SecretKey::generate().public();
        let s = id.to_string();
        assert_eq!(s.len(), 64);
        assert_eq!(parse_id(&s), Some(id));
        assert_eq!(parse_id(&s.to_uppercase()), None);
        assert_eq!(parse_id(&s[..63]), None);
        assert_eq!(parse_id(&id.to_z32()), None); // iroh's other spelling is not Force's
    }

    fn write_peers(p: &Path, body: &str, mode: u32) {
        fs::write(p, body).unwrap();
        fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
    }

    #[test]
    fn peers_read_and_mode_checked() {
        let d = tmpdir("peers");
        let p = d.join("link-peers.json");
        assert!(read_peers(&p).unwrap().is_empty()); // missing = nobody
        let a = SecretKey::generate().public();
        write_peers(&p, &format!(r#"{{"peers":[{{"id":"{a}","name":"laptop"}},{{"id":"zz","name":"bad"}}]}}"#), 0o600);
        let got = read_peers(&p).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got.get(&a).map(String::as_str), Some("laptop"));
        fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_peers(&p).is_err());
        fs::set_permissions(&p, fs::Permissions::from_mode(0o620)).unwrap();
        assert!(read_peers(&p).is_err());
        write_peers(&p, "not json", 0o600);
        assert!(read_peers(&p).is_err());
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn peers_reload_on_change_at_most_every_interval() {
        let d = tmpdir("reload");
        let p = d.join("link-peers.json");
        let a = SecretKey::generate().public();
        let b = SecretKey::generate().public();
        write_peers(&p, &format!(r#"{{"peers":[{{"id":"{a}","name":"a"}}]}}"#), 0o600);
        let mut pf = PeersFile::new(p.clone(), Duration::from_secs(2));
        let t0 = Instant::now();
        assert!(pf.refresh(t0).is_some());
        assert!(pf.peers.contains_key(&a));
        // a changed file (other size) is not seen before the interval passed
        write_peers(&p, &format!(r#"{{"peers":[{{"id":"{b}","name":"bb"}}]}}"#), 0o600);
        assert!(pf.refresh(t0 + Duration::from_millis(500)).is_none());
        assert!(pf.peers.contains_key(&a));
        assert!(pf.refresh(t0 + Duration::from_secs(3)).is_some());
        assert!(pf.peers.contains_key(&b) && !pf.peers.contains_key(&a));
        // unchanged: no reload
        assert!(pf.refresh(t0 + Duration::from_secs(6)).is_none());
        // removed: nobody
        fs::remove_file(&p).unwrap();
        pf.refresh(t0 + Duration::from_secs(9));
        assert!(pf.peers.is_empty());
        // a file others can write: nobody, and the refusal is reported
        write_peers(&p, &format!(r#"{{"peers":[{{"id":"{a}","name":"a"}}]}}"#), 0o666);
        let msg = pf.refresh(t0 + Duration::from_secs(12)).unwrap();
        assert!(msg.contains("accepting nobody"), "{msg}");
        assert!(pf.peers.is_empty());
        fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn link_state_keeps_the_python_fields() {
        let d = tmpdir("linkjson");
        let p = d.join("link.json");
        assert!(!write_link_state(&p, "ssh", "x", 1).unwrap()); // no file: nothing written
        let id = SecretKey::generate().public();
        fs::write(&p, format!(r#"{{"id":"{id}","addrs":["127.0.0.1:5","10.0.0.1"],"port":47823,"extra":1}}"#)).unwrap();
        assert!(write_link_state(&p, "direct", "", 42).unwrap());
        assert!(!write_link_state(&p, "direct", "", 42).unwrap()); // unchanged: no write
        let v: Value = serde_json::from_slice(&fs::read(&p).unwrap()).unwrap();
        assert_eq!(v["state"], "direct");
        assert_eq!(v["since_ms"], 42);
        assert_eq!(v["port"], 47823);
        assert_eq!(v["extra"], 1);
        let card = read_card(&p).unwrap();
        assert_eq!(card.id, id);
        assert_eq!(card.addrs, vec!["127.0.0.1:5".parse().unwrap(), "10.0.0.1:47823".parse().unwrap()]);
        fs::write(&p, r#"{"id":"nothex","addrs":["127.0.0.1:5"]}"#).unwrap();
        assert!(read_card(&p).is_none());
        fs::remove_dir_all(&d).ok();
    }
}
