//! Snapshot types and parsing (behavioural reference: force/state.py read_snapshot + usage.normalize).
//! Snapshot JSON is written atomically by the daemon. We never panic on bad data; unparseable input
//! yields `None` (== "no snapshot").

use serde_json::Value;

/// Session state. Unknown strings map to `Unknown`.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum State {
    Archived,
    Down,
    Shell,
    Starting,
    NeedsYou,
    Working,
    YourTurn,
    Stale,
    Idle,
    Unknown,
}

impl State {
    pub fn parse(s: &str) -> State {
        match s {
            "ARCHIVED" => State::Archived,
            "DOWN" => State::Down,
            "SHELL" => State::Shell,
            "STARTING" => State::Starting,
            "NEEDS_YOU" => State::NeedsYou,
            "WORKING" => State::Working,
            "YOUR_TURN" => State::YourTurn,
            "STALE" => State::Stale,
            "IDLE" => State::Idle,
            _ => State::Unknown,
        }
    }
}

/// One validated usage window (force/usage.py `_window`).
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct Window {
    pub used_percentage: f64,
    pub resets_at: i64,
}

#[derive(Clone, PartialEq, Debug)]
pub struct Usage {
    pub five_hour: Option<Window>,
    pub seven_day: Option<Window>,
}

/// One provider's plan limits for the dock (SPEC §49.2): "claude" / "Claude" and its windows.
#[derive(Clone, PartialEq, Debug)]
pub struct Limits {
    pub kind: String,
    pub label: String,
    pub usage: Usage,
}

#[derive(Clone, PartialEq, Debug)]
pub struct Session {
    pub name: String,
    pub index: Option<i32>,
    pub state: State,
    pub since_ms: Option<i64>,
    pub version: Option<String>,
    pub outdated: bool,
    pub archived: bool,
    pub order: i64,
    pub cwd: String,
    pub session_id: String,
    pub pid: Option<i64>,
    pub waiting_for: Option<String>,
    pub viewed: bool,
    pub activity: Option<String>,
    /// cleaned pane title of the agent pane (SPEC §43.4).
    pub title: Option<String>,
    pub mode: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub shared: Option<serde_json::Map<String, Value>>,
    /// brief feedback shown instead of the state label ("restarting" / "restarted").
    pub note: Option<String>,
    /// "claude" | "opencode" | "shell" (SPEC §36): the card's kind mark.
    pub kind: String,
    /// Chuck, Force's assistant (SPEC §50): shown last, with his face while idle.
    pub support: bool,
}

#[derive(Clone, PartialEq, Debug)]
pub struct Snapshot {
    pub generated_ms: i64,
    pub latest_version: Option<String>,
    pub sessions: Vec<Session>,
    /// the first limits block's windows (what the bars, their tween and the rail show)
    pub usage: Option<Usage>,
    /// plan limits per provider, in order (SPEC §49.2); empty = no limits panel
    pub limits: Vec<Limits>,
    /// the last `force smoke` run failed (SPEC §37): its short reason ("Claude Code logged out"; older daemons
    /// send none: "self-test failed"). The sidebar shows it above the host chip until a run passes.
    pub smoke_failed: Option<String>,
    /// a newer Force release is available (SPEC §3): its version, shown in the footer.
    pub update: Option<String>,
}

/// Read + parse the snapshot file. Any failure (missing, unreadable, bad JSON, not a dict) -> None.
pub fn load(path: &std::path::Path) -> Option<Snapshot> {
    let raw = std::fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&raw).ok()?;
    from_value(&value)
}

pub fn from_value(value: &Value) -> Option<Snapshot> {
    let obj = value.as_object()?;
    let generated_ms = obj.get("generated_ms").and_then(|v| v.as_i64()).unwrap_or(0);
    let latest_version = obj
        .get("latest_version")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let mut sessions = Vec::new();
    if let Some(arr) = obj.get("sessions").and_then(|v| v.as_array()) {
        for v in arr {
            if let Some(s) = parse_session(v) {
                sessions.push(s);
            }
        }
    }
    // `limits` (SPEC §49.2); a daemon of the previous release sends only `usage`, which was always Claude's
    let limits: Vec<Limits> = match obj.get("limits").and_then(|v| v.as_array()) {
        Some(arr) => arr
            .iter()
            .filter_map(|v| {
                let usage = normalize_usage(v)?;
                let kind = v.get("kind").and_then(|k| k.as_str()).unwrap_or("").to_string();
                let label = v.get("label").and_then(|k| k.as_str()).unwrap_or(&kind).to_string();
                Some(Limits { kind, label, usage })
            })
            .collect(),
        None => obj
            .get("usage")
            .and_then(normalize_usage)
            .map(|usage| vec![Limits { kind: "claude".into(), label: "Claude".into(), usage }])
            .unwrap_or_default(),
    };
    let usage = limits.first().map(|l| l.usage.clone());
    let smoke = obj.get("smoke");
    let smoke_failed = (smoke.and_then(|v| v.get("ok")).and_then(|v| v.as_bool()) == Some(false)).then(|| {
        smoke
            .and_then(|v| v.get("reason"))
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|r| !r.is_empty())
            .unwrap_or("self-test failed")
            .to_string()
    });
    Some(Snapshot {
        update: obj.get("update").and_then(|v| v.as_str()).map(str::to_string),
        smoke_failed,
        generated_ms,
        latest_version,
        sessions,
        usage,
        limits,
    })
}

fn parse_session(v: &Value) -> Option<Session> {
    let obj = v.as_object()?;
    // A session without a name is unusable (it is the key); skip it rather than fail the whole file.
    let name = obj.get("name")?.as_str()?.to_string();
    let shared = obj
        .get("shared")
        .and_then(|v| v.as_object())
        .cloned();
    Some(Session {
        name,
        index: obj.get("index").and_then(|v| v.as_i64()).map(|i| i as i32),
        state: State::parse(obj.get("state").and_then(|v| v.as_str()).unwrap_or("")),
        since_ms: obj.get("since_ms").and_then(|v| v.as_i64()),
        version: obj.get("version").and_then(|v| v.as_str()).map(str::to_string),
        outdated: obj.get("outdated").and_then(|v| v.as_bool()).unwrap_or(false),
        archived: obj.get("archived").and_then(|v| v.as_bool()).unwrap_or(false),
        order: obj.get("order").and_then(|v| v.as_i64()).unwrap_or(0),
        cwd: obj.get("cwd").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        session_id: obj.get("session_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        pid: obj.get("pid").and_then(|v| v.as_i64()),
        waiting_for: obj.get("waiting_for").and_then(|v| v.as_str()).map(str::to_string),
        viewed: obj.get("viewed").and_then(|v| v.as_bool()).unwrap_or(false),
        activity: obj.get("activity").and_then(|v| v.as_str()).map(str::to_string),
        title: obj.get("title").and_then(|v| v.as_str()).map(str::to_string),
        mode: obj.get("mode").and_then(|v| v.as_str()).map(str::to_string),
        model: obj.get("model").and_then(|v| v.as_str()).map(str::to_string),
        effort: obj.get("effort").and_then(|v| v.as_str()).map(str::to_string),
        shared,
        note: obj.get("note").and_then(|v| v.as_str()).map(str::to_string),
        kind: obj.get("kind").and_then(|v| v.as_str()).unwrap_or("claude").to_string(),
        support: obj.get("support").and_then(|v| v.as_bool()).unwrap_or(false),
    })
}

/// force/usage.py `normalize`: None when not a dict or both windows invalid; per-window validation
/// (finite numbers, bool rejected, pct clamped 0..100, resets_at bounds).
fn normalize_usage(raw: &Value) -> Option<Usage> {
    let obj = raw.as_object()?;
    let five_hour = obj.get("five_hour").and_then(window);
    let seven_day = obj.get("seven_day").and_then(window);
    if five_hour.is_none() && seven_day.is_none() {
        return None;
    }
    Some(Usage {
        five_hour,
        seven_day,
    })
}

fn window(raw: &Value) -> Option<Window> {
    let obj = raw.as_object()?;
    let pct = obj.get("used_percentage").and_then(finite_f64)?;
    let reset = obj.get("resets_at").and_then(finite_f64)?;
    if reset <= 0.0 || reset > 4_102_444_800.0 {
        return None;
    }
    Some(Window {
        used_percentage: pct.clamp(0.0, 100.0),
        resets_at: reset as i64,
    })
}

fn finite_f64(v: &Value) -> Option<f64> {
    let f = v.as_f64()?; // None for bools and non-numbers
    if f.is_finite() {
        Some(f)
    } else {
        None
    }
}

/// Snapshot older than 5000 ms -> "daemon not running".
pub fn is_stale(snap: &Snapshot, now_ms: i64) -> bool {
    now_ms - snap.generated_ms > 5000
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn full() -> Value {
        json!({
            "generated_ms": 1791700000000i64,
            "latest_version": "2.1.295",
            "usage": {
                "five_hour": {"used_percentage": 43.0, "resets_at": 1791561000i64},
                "seven_day": {"used_percentage": 13.0, "resets_at": 1792087200i64}
            },
            "sessions": [{
                "name": "portal", "index": 2, "state": "WORKING", "since_ms": 1791699990000i64,
                "version": "2.1.295", "outdated": false, "archived": false, "order": 1,
                "cwd": "/home/alice/portal", "session_id": "s1", "pid": 123,
                "waiting_for": null, "viewed": true, "activity": "editing app.py",
                "mode": "auto", "model": "opus 5.5", "effort": "high", "shared": null
            }]
        })
    }

    #[test]
    fn update_parsed_when_string() {
        let mk = |extra: Value| {
            let mut o = json!({"generated_ms": 1, "sessions": [], "update": "2.2.0"});
            if let Some(v) = extra.as_object() {
                for (k, val) in v {
                    if val.is_null() {
                        o.as_object_mut().unwrap().remove(k);
                    } else {
                        o.as_object_mut().unwrap().insert(k.clone(), val.clone());
                    }
                }
            }
            from_value(&o).expect("parses")
        };
        assert_eq!(mk(json!({})).update.as_deref(), Some("2.2.0"));
        assert_eq!(mk(json!({"update": null})).update, None);
        assert_eq!(mk(json!({"update": 5})).update, None);
        let snap = from_value(&json!({"generated_ms": 1, "sessions": []})).unwrap();
        assert_eq!(snap.update, None);
    }

    #[test]
    fn full_example() {
        let snap = from_value(&full()).expect("parses");
        assert_eq!(snap.generated_ms, 1791700000000);
        assert_eq!(snap.latest_version.as_deref(), Some("2.1.295"));
        assert_eq!(snap.sessions.len(), 1);
        let s = &snap.sessions[0];
        assert_eq!(s.name, "portal");
        assert_eq!(s.index, Some(2));
        assert_eq!(s.state, State::Working);
        assert_eq!(s.since_ms, Some(1791699990000));
        assert!(s.viewed);
        assert_eq!(s.activity.as_deref(), Some("editing app.py"));
        assert_eq!(s.mode.as_deref(), Some("auto"));
        let u = snap.usage.as_ref().expect("usage");
        assert_eq!(u.five_hour.as_ref().unwrap().used_percentage, 43.0);
        assert_eq!(u.seven_day.as_ref().unwrap().resets_at, 1792087200);
    }

    #[test]
    fn title_parsed_when_string() {
        let mk = |extra: Value| {
            let mut o = json!({"name": "x", "why": "status file"});
            o.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
            from_value(&json!({"generated_ms": 1, "sessions": [o]})).unwrap().sessions.remove(0)
        };
        assert_eq!(mk(json!({"title": "fix the build"})).title.as_deref(), Some("fix the build"));
        assert_eq!(mk(json!({})).title, None);
        assert_eq!(mk(json!({"title": null})).title, None);
        assert_eq!(mk(json!({"title": 5})).title, None);
    }

    #[test]
    fn missing_fields_defaults() {
        let snap = from_value(&json!({
            "generated_ms": 1,
            "sessions": [{"name": "x"}]
        }))
        .expect("parses");
        assert_eq!(snap.sessions.len(), 1);
        let s = &snap.sessions[0];
        assert_eq!(s.index, None);
        assert_eq!(s.state, State::Unknown);
        assert!(!s.outdated);
        assert!(!s.archived);
        assert!(!s.viewed);
        assert_eq!(s.activity, None);
        assert!(s.shared.is_none());
        assert!(snap.usage.is_none());
    }

    #[test]
    fn unknown_state_maps_to_unknown() {
        let snap = from_value(&json!({
            "generated_ms": 1,
            "sessions": [{"name": "a", "state": "BANANA"}]
        }))
        .expect("parses");
        assert_eq!(snap.sessions[0].state, State::Unknown);
    }

    #[test]
    fn unknown_fields_ignored() {
        let snap = from_value(&json!({
            "generated_ms": 1,
            "bogus_field": [1, 2, 3],
            "sessions": [{"name": "a", "state": "IDLE", "wat": "x"}]
        }))
        .expect("parses");
        assert_eq!(snap.sessions.len(), 1);
    }

    #[test]
    fn bad_json_is_none() {
        assert!(from_value(&json!(123)).is_none());
        assert!(from_value(&Value::Null).is_none());
        assert!(from_value(&json!("hello")).is_none());
        assert!(from_value(&json!([])).is_none());
    }

    #[test]
    fn limits_per_provider() {
        let w = json!({"used_percentage": 30, "resets_at": 1_800_000_000});
        // limits given: the blocks in order, usage = the first one's windows (the old `usage` key is ignored)
        let snap = from_value(&json!({"generated_ms": 1, "usage": {"five_hour": w},
            "limits": [{"kind": "claude", "label": "Claude", "five_hour": w, "seven_day": w}, {"kind": "x", "five_hour": "bad"}]})).unwrap();
        assert_eq!(snap.limits.len(), 1, "a block without a valid window is dropped");
        assert_eq!(snap.limits[0].label, "Claude");
        assert!(snap.usage.as_ref().unwrap().seven_day.is_some());
        // an empty list: no limits even when the old key is there (only OpenCode sessions on the host)
        let snap = from_value(&json!({"generated_ms": 1, "usage": {"five_hour": w}, "limits": []})).unwrap();
        assert!(snap.limits.is_empty() && snap.usage.is_none());
        // a daemon of the previous release: `usage` alone is Claude's
        let snap = from_value(&json!({"generated_ms": 1, "usage": {"five_hour": w}})).unwrap();
        assert_eq!(snap.limits[0].kind, "claude");
        assert_eq!(snap.limits[0].label, "Claude");
    }

    #[test]
    fn usage_validation() {
        // bool rejected
        let snap = from_value(&json!({"generated_ms": 1, "usage": {
            "five_hour": {"used_percentage": true, "resets_at": 100}
        }})).unwrap();
        assert!(snap.usage.is_none());

        // both invalid -> none
        let snap = from_value(&json!({"generated_ms": 1, "usage": {
            "five_hour": {"used_percentage": 10, "resets_at": 0}
        }})).unwrap();
        assert!(snap.usage.is_none());

        // clamp out-of-range pct, keep valid
        let snap = from_value(&json!({"generated_ms": 1, "usage": {
            "five_hour": {"used_percentage": 500.0, "resets_at": 100},
            "seven_day": {"used_percentage": -5.0, "resets_at": 200}
        }})).unwrap();
        let u = snap.usage.unwrap();
        assert_eq!(u.five_hour.unwrap().used_percentage, 100.0);
        assert_eq!(u.seven_day.unwrap().used_percentage, 0.0);

        // null windows treated as missing
        let snap = from_value(&json!({"generated_ms": 1, "usage": {
            "five_hour": null, "seven_day": {"used_percentage": 30.0, "resets_at": 5}
        }})).unwrap();
        let u = snap.usage.unwrap();
        assert!(u.five_hour.is_none());
        assert!(u.seven_day.is_some());
    }

    #[test]
    fn staleness() {
        let snap = Snapshot { update: None, smoke_failed: None, generated_ms: 1, latest_version: None, sessions: vec![], usage: None, limits: vec![] };
        assert!(is_stale(&snap, 6001));
        assert!(!is_stale(&snap, 5000));
    }

    #[test]
    fn smoke_failed_only_when_the_last_run_failed() {
        let parse = |j: &str| from_value(&serde_json::from_str(j).unwrap()).unwrap().smoke_failed.is_some();
        let reason = |j: &str| from_value(&serde_json::from_str(j).unwrap()).unwrap().smoke_failed;
        assert_eq!(reason(r#"{"sessions":[],"smoke":{"ok":false,"at":1,"reason":"Claude Code logged out"}}"#).as_deref(), Some("Claude Code logged out"));
        assert_eq!(reason(r#"{"sessions":[],"smoke":{"ok":false,"at":1}}"#).as_deref(), Some("self-test failed"), "older daemon: no reason");
        assert_eq!(reason(r#"{"sessions":[],"smoke":{"ok":false,"reason":"  "}}"#).as_deref(), Some("self-test failed"));
        assert_eq!(reason(r#"{"sessions":[],"smoke":{"ok":false,"reason":7}}"#).as_deref(), Some("self-test failed"));
        assert!(!parse(r#"{"sessions":[]}"#));
        assert!(!parse(r#"{"sessions":[],"smoke":{"ok":true}}"#));
        assert!(parse(r#"{"sessions":[],"smoke":{"ok":false,"at":1}}"#));
        assert!(!parse(r#"{"sessions":[],"smoke":"bad"}"#));
    }
}
