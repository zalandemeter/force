//! A small dependency-free JSON parser (no serde) for the hosts file. It accepts full JSON values so
//! unknown extra fields anywhere are skipped; only enough of it is used to find a host's `ssh` value.

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(f64),
    Str(String),
    Array(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

fn err(msg: &str) -> String {
    msg.to_string()
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn expect(&mut self, c: u8) -> Result<()> {
        if self.peek() == Some(c) {
            self.i += 1;
            Ok(())
        } else {
            Err(err("expected character"))
        }
    }

    fn value(&mut self) -> Result<Value> {
        self.ws();
        match self.peek() {
            Some(b'{') => self.obj(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') => {
                self.literal(b"true")?;
                Ok(Value::Bool(true))
            }
            Some(b'f') => {
                self.literal(b"false")?;
                Ok(Value::Bool(false))
            }
            Some(b'n') => {
                self.literal(b"null")?;
                Ok(Value::Null)
            }
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(err("unexpected character")),
        }
    }

    fn literal(&mut self, lit: &[u8]) -> Result<()> {
        if self.b[self.i..].starts_with(lit) {
            self.i += lit.len();
            Ok(())
        } else {
            Err(err("bad literal"))
        }
    }

    fn number(&mut self) -> Result<Value> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        while self.peek().is_some_and(|c| c.is_ascii_digit()) {
            self.i += 1;
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        if self.peek() == Some(b'e') || self.peek() == Some(b'E') {
            self.i += 1;
            if self.peek() == Some(b'+') || self.peek() == Some(b'-') {
                self.i += 1;
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).map_err(|_| err("bad number"))?;
        text.parse::<f64>().map(Value::Number).map_err(|_| err("bad number"))
    }

    fn hex4(&mut self) -> Result<u32> {
        let mut v: u32 = 0;
        for _ in 0..4 {
            let c = self.b.get(self.i).copied().ok_or_else(|| err("short \\u"))?;
            self.i += 1;
            let d = match c {
                b'0'..=b'9' => (c - b'0') as u32,
                b'a'..=b'f' => (c - b'a' + 10) as u32,
                b'A'..=b'F' => (c - b'A' + 10) as u32,
                _ => return Err(err("bad hex digit")),
            };
            v = v * 16 + d;
        }
        Ok(v)
    }

    fn string(&mut self) -> Result<String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            match self.peek() {
                None => return Err(err("unterminated string")),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.i += 1;
                    let e = self.b.get(self.i).copied().ok_or_else(|| err("bad escape"))?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000C}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let cp = self.hex4()?;
                            if (0xD800..0xDC00).contains(&cp) {
                                // high surrogate: must pair with a following \uXXXX low surrogate
                                if self.b.get(self.i) == Some(&b'\\')
                                    && self.b.get(self.i + 1) == Some(&b'u')
                                {
                                    self.i += 2;
                                    let lo = self.hex4()?;
                                    if (0xDC00..0xE000).contains(&lo) {
                                        let c = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                        out.push(char::from_u32(c).unwrap_or('\u{FFFD}'));
                                    } else {
                                        return Err(err("bad low surrogate"));
                                    }
                                } else {
                                    return Err(err("missing low surrogate"));
                                }
                            } else if (0xDC00..0xE000).contains(&cp) {
                                return Err(err("unexpected low surrogate"));
                            } else {
                                out.push(char::from_u32(cp).unwrap_or('\u{FFFD}'));
                            }
                        }
                        _ => return Err(err("bad escape")),
                    }
                }
                Some(c) if c < 0x80 => {
                    out.push(c as char);
                    self.i += 1;
                }
                Some(_) => {
                    // a multibyte UTF-8 char: decode just one from the remaining bytes
                    let rest = &self.b[self.i..];
                    match std::str::from_utf8(rest) {
                        Ok(s) => {
                            let c = s.chars().next().unwrap();
                            out.push(c);
                            self.i += c.len_utf8();
                        }
                        Err(e) => {
                            let up = e.valid_up_to();
                            if up == 0 {
                                return Err(err("bad utf-8"));
                            }
                            let c = std::str::from_utf8(&rest[..up]).unwrap().chars().next().unwrap();
                            out.push(c);
                            self.i += c.len_utf8();
                        }
                    }
                }
            }
        }
    }

    fn array(&mut self) -> Result<Value> {
        self.expect(b'[')?;
        let mut out = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok(Value::Array(out));
        }
        loop {
            out.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                }
                Some(b']') => {
                    self.i += 1;
                    return Ok(Value::Array(out));
                }
                _ => return Err(err("expected , or ]")),
            }
        }
    }

    fn obj(&mut self) -> Result<Value> {
        self.expect(b'{')?;
        let mut out = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok(Value::Obj(out));
        }
        loop {
            self.ws();
            let key = self.string()?;
            self.ws();
            self.expect(b':')?;
            let v = self.value()?;
            out.push((key, v));
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                }
                Some(b'}') => {
                    self.i += 1;
                    return Ok(Value::Obj(out));
                }
                _ => return Err(err("expected , or }")),
            }
        }
    }
}

type Result<T> = std::result::Result<T, String>;

/// Parse a whole JSON document (trailing whitespace allowed).
pub fn parse(s: &str) -> Result<Value> {
    let mut p = Parser { b: s.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        Err(err("trailing characters"))
    } else {
        Ok(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hosts_with_surrogate_escapes() {
        let doc = parse(r#"{"hosts":[{"name":"devboxé😀","ssh":"x"}]}"#).unwrap();
        match doc {
            Value::Obj(ref f) => {
                assert_eq!(f[0].0, "hosts");
                if let Value::Array(list) = &f[0].1 {
                    if let Value::Obj(e) = &list[0] {
                        assert_eq!(e[0].1, Value::Str("devboxé😀".into()));
                    } else {
                        panic!("not obj");
                    }
                } else {
                    panic!("not array");
                }
            }
            _ => panic!("not obj"),
        }
    }

    #[test]
    fn skips_extra_fields_and_numbers() {
        let doc = parse(r#"{"hosts":[{"name":"a","ssh":"b","enabled":true,"extra":{"n":1.5,"z":null}}]}"#)
            .unwrap();
        if let Value::Obj(f) = &doc {
            assert_eq!(f.len(), 1);
        } else {
            panic!("not obj");
        }
    }

    #[test]
    fn bad_json_is_an_error() {
        assert!(parse("not json ").is_err());
        assert!(parse(r#"{"hosts":["#).is_err());
        assert!(parse("").is_err());
    }
}
