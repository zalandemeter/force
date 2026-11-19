//! The hosts file (`~/.config/force/hosts.json`, SPEC §40) and host names.

use crate::json;

/// `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$` (matches `force/remote.py`'s NAME_RE).
pub fn valid_name(name: &str) -> bool {
    let b = name.as_bytes();
    if b.is_empty() || b.len() > 32 {
        return false;
    }
    if !b[0].is_ascii_alphanumeric() {
        return false;
    }
    b[1..].iter().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
}

/// The `ssh` value of the entry whose `name` equals `host`, or None when the document is not the hosts
/// shape or no such entry exists. Unknown extra fields anywhere are skipped; `enabled` is ignored (in part A
/// a host is reached by plain ssh whatever its enabled flag says).
pub fn host_ssh(doc: &json::Value, host: &str) -> Option<String> {
    if let json::Value::Obj(fields) = doc {
        for (k, v) in fields {
            if k == "hosts" {
                if let json::Value::Array(list) = v {
                    for entry in list {
                        if let Some(ssh) = entry_ssh(entry, host) {
                            return Some(ssh);
                        }
                    }
                }
            }
        }
    }
    None
}

fn entry_ssh(entry: &json::Value, host: &str) -> Option<String> {
    if let json::Value::Obj(fields) = entry {
        let mut name: Option<&str> = None;
        let mut ssh: Option<&str> = None;
        for (k, v) in fields {
            match k.as_str() {
                "name" => {
                    if let json::Value::Str(s) = v {
                        name = Some(s);
                    }
                }
                "ssh" => {
                    if let json::Value::Str(s) = v {
                        ssh = Some(s);
                    }
                }
                _ => {} // unknown fields (incl. "enabled") are skipped
            }
        }
        if name == Some(host) {
            return ssh.map(str::to_string);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;

    fn host(contents: &str, name: &str) -> Option<String> {
        parse(contents).ok().and_then(|doc| host_ssh(&doc, name))
    }

    #[test]
    fn name_validation() {
        assert!(valid_name("devbox"));
        assert!(valid_name("a"));
        assert!(valid_name("a.b-c_9"));
        assert!(!valid_name(""));
        assert!(valid_name("9bad2")); // a leading digit is fine
        assert!(!valid_name("has space"));
        assert!(!valid_name("-x"));
        assert!(!valid_name(&"a".repeat(33)));
    }

    #[test]
    fn valid_host_found() {
        assert_eq!(host(r#"{"hosts":[{"name":"devbox","ssh":"devbox"}]}"#, "devbox"),
                   Some("devbox".into()));
    }

    #[test]
    fn bad_json_is_no_host() {
        assert_eq!(parse("not json").ok().and_then(|d| host_ssh(&d, "x")), None);
    }

    #[test]
    fn wrong_name_is_no_host() {
        assert_eq!(host(r#"{"hosts":[{"name":"a","ssh":"a"}]}"#, "devbox"), None);
    }

    #[test]
    fn entry_with_extra_fields_and_disabled_flag() {
        assert_eq!(
            host(r#"{"unknown":1,"hosts":[{"name":"devbox","ssh":"devbox","enabled":false,"age":2}]}"#,
                 "devbox"),
            Some("devbox".into())
        );
    }

    #[test]
    fn empty_hosts_is_no_host() {
        assert_eq!(host(r#"{"hosts":[]}"#, "devbox"), None);
        assert_eq!(host("", "devbox"), None); // unparseable
    }
}
