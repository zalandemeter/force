//! The channel framing (SPEC §56.2), the same on the local socket and on a QUIC stream: one JSON header line,
//! then binary frames `type:u8, len:u32 BE, payload` (payload at most 64 KiB).

use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};

pub const STDIN: u8 = 0;
pub const STDOUT: u8 = 1;
pub const STDERR: u8 = 2;
pub const RESIZE: u8 = 3;
pub const EXIT: u8 = 4;
pub const STDIN_EOF: u8 = 5;
pub const ERROR: u8 = 6;

/// The largest payload a frame may carry; a larger length is a protocol error and ends the channel.
pub const MAX_PAYLOAD: usize = 64 * 1024;
/// The longest header line (bytes, newline included).
pub const MAX_HEADER: usize = 64 * 1024;
/// At most this many argv words, each at most MAX_WORD bytes.
pub const MAX_ARGS: usize = 256;
pub const MAX_WORD: usize = 16 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub kind: u8,
    pub payload: Vec<u8>,
}

/// The header of a channel: `{"v":1,"host":NAME,"argv":[...],"tty":bool,"cols":N,"rows":N,"term":"..."}`.
/// Over QUIC `host` is left out (the connection names the host). Unknown fields are refused.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Header {
    pub v: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
    pub argv: Vec<String>,
    #[serde(default)]
    pub tty: bool,
    #[serde(default)]
    pub cols: u16,
    #[serde(default)]
    pub rows: u16,
    #[serde(default)]
    pub term: String,
}

impl Header {
    /// The header as one line (newline included).
    pub fn to_line(&self) -> Vec<u8> {
        let mut v = serde_json::to_vec(self).expect("header serialises");
        v.push(b'\n');
        v
    }

    /// Parse one header line (without its newline) and check its shape: version 1, 1..=MAX_ARGS words of at most
    /// MAX_WORD bytes without NUL, a TERM of [A-Za-z0-9._+-] (at most 64 bytes, required with a tty).
    pub fn parse(line: &[u8]) -> Result<Header, String> {
        let h: Header = serde_json::from_slice(line).map_err(|e| format!("bad header: {e}"))?;
        if h.v != 1 {
            return Err(format!("unsupported version {}", h.v));
        }
        if h.argv.is_empty() || h.argv.len() > MAX_ARGS {
            return Err("argv must have 1 to 256 words".into());
        }
        if h.argv.iter().any(|w| w.len() > MAX_WORD || w.as_bytes().contains(&0)) {
            return Err("bad argv word".into());
        }
        if !valid_term(&h.term) || (h.tty && h.term.is_empty()) {
            return Err("bad term".into());
        }
        Ok(h)
    }
}

fn valid_term(t: &str) -> bool {
    t.len() <= 64 && t.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'+' | b'-'))
}

/// The bytes of one frame. Panics on a payload over MAX_PAYLOAD (callers chunk their data).
pub fn encode(kind: u8, payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= MAX_PAYLOAD, "frame payload too large");
    let mut v = Vec::with_capacity(5 + payload.len());
    v.push(kind);
    v.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    v.extend_from_slice(payload);
    v
}

pub fn resize_payload(cols: u16, rows: u16) -> [u8; 4] {
    let c = cols.to_be_bytes();
    let r = rows.to_be_bytes();
    [c[0], c[1], r[0], r[1]]
}

pub fn parse_resize(p: &[u8]) -> Option<(u16, u16)> {
    if p.len() != 4 {
        return None;
    }
    Some((u16::from_be_bytes([p[0], p[1]]), u16::from_be_bytes([p[2], p[3]])))
}

pub fn parse_exit(p: &[u8]) -> Option<i32> {
    if p.len() != 4 {
        return None;
    }
    Some(i32::from_be_bytes([p[0], p[1], p[2], p[3]]))
}

/// An error frame's payload: `{"code","message"}`.
pub fn error_payload(code: &str, message: &str) -> Vec<u8> {
    let mut m = message.to_string();
    m.truncate(1024);
    serde_json::to_vec(&serde_json::json!({"code": code, "message": m})).expect("json")
}

/// (code, message) of an error frame's payload; unreadable payloads give ("error", "").
pub fn parse_error(p: &[u8]) -> (String, String) {
    let v: serde_json::Value = serde_json::from_slice(p).unwrap_or(serde_json::Value::Null);
    let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    let code = s("code");
    (if code.is_empty() { "error".into() } else { code }, s("message"))
}

/// A frame's type and length are checked before the payload is read: an unknown type or a length over
/// MAX_PAYLOAD is a protocol error.
pub fn check_head(head: &[u8; 5]) -> io::Result<(u8, usize)> {
    let kind = head[0];
    let len = u32::from_be_bytes([head[1], head[2], head[3], head[4]]) as usize;
    if kind > ERROR {
        return Err(io::Error::new(io::ErrorKind::InvalidData, format!("unknown frame type {kind}")));
    }
    if len > MAX_PAYLOAD {
        return Err(io::Error::new(io::ErrorKind::InvalidData, format!("frame of {len} bytes")));
    }
    Ok((kind, len))
}

/// Read one frame; Ok(None) at a clean end of the stream between frames.
pub fn read_frame<R: Read>(r: &mut R) -> io::Result<Option<Frame>> {
    let mut head = [0u8; 5];
    let mut got = 0;
    while got < head.len() {
        match r.read(&mut head[got..]) {
            Ok(0) if got == 0 => return Ok(None),
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    let (kind, len) = check_head(&head)?;
    let mut payload = vec![0u8; len];
    r.read_exact(&mut payload)?;
    Ok(Some(Frame { kind, payload }))
}

pub fn write_frame<W: Write>(w: &mut W, kind: u8, payload: &[u8]) -> io::Result<()> {
    w.write_all(&encode(kind, payload))
}

/// Read one line (at most `max` bytes, newline included) a byte at a time, so nothing after it is consumed.
/// Returns the line without its newline; EOF before the newline is an error.
pub fn read_line_unbuffered<R: Read>(r: &mut R, max: usize) -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut b = [0u8; 1];
    loop {
        match r.read(&mut b) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(_) => {
                if b[0] == b'\n' {
                    return Ok(out);
                }
                out.push(b[0]);
                if out.len() >= max {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "line too long"));
                }
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
}

/// The async side (daemon, remote): the same frames over tokio streams.
pub mod aio {
    use super::*;
    use tokio::io::{AsyncRead, AsyncReadExt};

    pub async fn read_frame<R: AsyncRead + Unpin>(r: &mut R) -> io::Result<Option<Frame>> {
        let mut head = [0u8; 5];
        let mut got = 0;
        while got < head.len() {
            let n = r.read(&mut head[got..]).await?;
            if n == 0 {
                if got == 0 {
                    return Ok(None);
                }
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            got += n;
        }
        let (kind, len) = check_head(&head)?;
        let mut payload = vec![0u8; len];
        r.read_exact(&mut payload).await?;
        Ok(Some(Frame { kind, payload }))
    }

    #[cfg(test)]
    pub async fn write_frame<W: tokio::io::AsyncWrite + Unpin>(w: &mut W, kind: u8, payload: &[u8]) -> io::Result<()> {
        use tokio::io::AsyncWriteExt;
        w.write_all(&encode(kind, payload)).await
    }

    /// One line (without its newline), at most `max` bytes, read a byte at a time: nothing after it is consumed.
    pub async fn read_line<R: AsyncRead + Unpin>(r: &mut R, max: usize) -> io::Result<Vec<u8>> {
        let mut out = Vec::new();
        let mut b = [0u8; 1];
        loop {
            let n = r.read(&mut b).await?;
            if n == 0 {
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            if b[0] == b'\n' {
                return Ok(out);
            }
            out.push(b[0]);
            if out.len() >= max {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "line too long"));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frames_round_trip() {
        let mut buf = Vec::new();
        write_frame(&mut buf, STDIN, b"hello").unwrap();
        write_frame(&mut buf, RESIZE, &resize_payload(120, 40)).unwrap();
        write_frame(&mut buf, EXIT, &(-3i32).to_be_bytes()).unwrap();
        write_frame(&mut buf, STDIN_EOF, b"").unwrap();
        write_frame(&mut buf, STDOUT, &vec![7u8; MAX_PAYLOAD]).unwrap();
        let mut r = Cursor::new(buf);
        assert_eq!(read_frame(&mut r).unwrap().unwrap(), Frame { kind: STDIN, payload: b"hello".to_vec() });
        let f = read_frame(&mut r).unwrap().unwrap();
        assert_eq!((f.kind, parse_resize(&f.payload)), (RESIZE, Some((120, 40))));
        let f = read_frame(&mut r).unwrap().unwrap();
        assert_eq!((f.kind, parse_exit(&f.payload)), (EXIT, Some(-3)));
        assert_eq!(read_frame(&mut r).unwrap().unwrap().kind, STDIN_EOF);
        assert_eq!(read_frame(&mut r).unwrap().unwrap().payload.len(), MAX_PAYLOAD);
        assert_eq!(read_frame(&mut r).unwrap(), None); // clean end
    }

    #[test]
    fn oversize_frame_rejected_before_its_payload() {
        let mut buf = vec![STDOUT];
        buf.extend_from_slice(&((MAX_PAYLOAD as u32) + 1).to_be_bytes());
        let e = read_frame(&mut Cursor::new(buf)).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn unknown_type_and_truncation_are_errors() {
        let mut buf = vec![9u8];
        buf.extend_from_slice(&0u32.to_be_bytes());
        assert!(read_frame(&mut Cursor::new(buf)).is_err());
        assert!(read_frame(&mut Cursor::new(vec![STDIN, 0, 0])).is_err()); // cut in the head
        let mut cut = encode(STDIN, b"abcdef");
        cut.truncate(8);
        assert!(read_frame(&mut Cursor::new(cut)).is_err()); // cut in the payload
    }

    #[test]
    #[should_panic]
    fn encoding_an_oversize_payload_panics() {
        encode(STDOUT, &vec![0u8; MAX_PAYLOAD + 1]);
    }

    #[tokio::test]
    async fn async_frames_round_trip_and_oversize() {
        let mut buf = Vec::new();
        aio::write_frame(&mut buf, STDERR, b"err").await.unwrap();
        let mut r = &buf[..];
        assert_eq!(aio::read_frame(&mut r).await.unwrap().unwrap(), Frame { kind: STDERR, payload: b"err".to_vec() });
        assert_eq!(aio::read_frame(&mut r).await.unwrap(), None);
        let mut big = vec![STDIN];
        big.extend_from_slice(&(1u32 << 20).to_be_bytes());
        let mut r = &big[..];
        assert!(aio::read_frame(&mut r).await.is_err());
    }

    #[test]
    fn header_round_trip_and_checks() {
        let h = Header { v: 1, host: Some("devbox".into()), argv: vec!["~/.local/bin/force".into(), "list".into()],
                         tty: true, cols: 80, rows: 24, term: "xterm-256color".into() };
        let line = h.to_line();
        assert_eq!(*line.last().unwrap(), b'\n');
        assert_eq!(Header::parse(&line[..line.len() - 1]).unwrap(), h);
        let quic = Header { host: None, ..h.clone() };
        assert!(!String::from_utf8(quic.to_line()).unwrap().contains("host"));
        assert!(Header::parse(br#"{"v":2,"argv":["x"]}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":[]}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":["x"],"evil":1}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":["x"],"tty":true,"term":"a b"}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":["x"],"tty":true}"#).is_err()); // a tty needs a TERM
        assert!(Header::parse(br#"{"v":1,"argv":["a\u0000b"]}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":["x"],"cols":70000}"#).is_err());
        assert!(Header::parse(br#"{"v":1,"argv":["x"]}"#).is_ok());
    }

    #[test]
    fn line_reader_stops_at_newline_and_limit() {
        let mut r = Cursor::new(b"direct\nREST".to_vec());
        assert_eq!(read_line_unbuffered(&mut r, 64).unwrap(), b"direct");
        let mut rest = Vec::new();
        r.read_to_end(&mut rest).unwrap();
        assert_eq!(rest, b"REST");
        assert!(read_line_unbuffered(&mut Cursor::new(vec![b'a'; 100]), 10).is_err());
        assert!(read_line_unbuffered(&mut Cursor::new(b"no newline".to_vec()), 64).is_err());
    }

    #[test]
    fn error_payload_round_trip() {
        let p = error_payload("not_allowed", "sh is not allowed");
        assert_eq!(parse_error(&p), ("not_allowed".into(), "sh is not allowed".into()));
        assert_eq!(parse_error(b"junk").0, "error");
    }
}
