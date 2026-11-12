"""`force doctor` health check with optional repair (SPEC §14-16).

Public API:
    Problem(    code, session, detail, fixable)
    check(      cockpit, *, systemd=True) -> list[Problem]
    repair(     cockpit, problems)         -> list[str]   one human line per action taken
    run_doctor( cockpit, fix=False, out=print, systemd=True) -> int
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import state
from .registry import RegistryError, load as registry_load
from .tmux import LEGACY_AGENT_ROLE, TmuxError

_SNAPSHOT_FRESH_MS = 10_000      # daemon writes every ~1 s; 10 s staleness => dead daemon


@dataclass(frozen=True)
class Problem:
    code: str
    session: str | None
    detail: str
    fixable: bool


# --- window info -------------------------------------------------------------
# One line per pane: the fields doctor needs beyond cockpit.tmux.panes(): session, window id,
# window index, window name, pane id and role.
_WIN_FMT = "#{session_name} #{window_id} #{window_index} #{window_name} #{pane_id} #{?@force_role,#{@force_role},-}"


def _window_rows(tmux) -> list[dict]:
    """[(session, window_id, window_index, window_name, pane_id, role)] for every pane."""
    try:
        out = tmux.run("list-panes", "-a", "-F", _WIN_FMT)
    except TmuxError as e:
        if "no current target" in str(e):      # a server without sessions
            return []
        raise
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        role = parts[5]
        if role == LEGACY_AGENT_ROLE:
            role = "agent"        # a pre-rebrand pane tagged "claude" reads as an agent pane
        rows.append({
            "session": parts[0],
            "window_id": parts[1],
            "window_index": parts[2],
            "window_name": parts[3],
            "pane_id": parts[4],
            "role": None if role == "-" else role,
        })
    return rows


def _windows(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Group window rows by (session, window_id) with their pane roles."""
    windows: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["session"], r["window_id"])
        win = windows.setdefault(key, {
            "session": r["session"],
            "window_id": r["window_id"],
            "index": r["window_index"],
            "name": r["window_name"],
            "roles": [],
        })
        win["roles"].append(r["role"])
    return windows


# --- checks -------------------------------------------------------------
def check(cockpit, *, systemd: bool = True) -> list[Problem]:
    problems: list[Problem] = []
    # the last self-test against the real Claude Code / OpenCode (SPEC §37)
    from .smoke import read_last, reason as smoke_reason
    last = read_last(cockpit.paths.state / "smoke")
    if last is not None and not last.get("ok"):
        failed = ", ".join(c["name"] for c in last.get("checks") or []
                          if isinstance(c, dict) and not c.get("ok"))
        why = last.get("reason") or smoke_reason(last.get("checks")) or "self-test failed"
        problems.append(Problem("self-test", None,
                               f"last `force smoke` failed: {why} — {failed} (force smoke --last)", False))

    # remotes (SPEC §40): a connected remote is healthy; a remote on another Force version or offline is not.
    from . import remote
    try:
        remotes = remote.load(cockpit.paths)
    except Exception as e:
        problems.append(Problem("remotes", None, f"hosts.json: {e}", False))
        remotes = []
    for r in remotes:
        if not r.enabled:
            continue
        try:
            st = remote.read_status(cockpit.paths, r.name)
        except Exception as e:
            problems.append(Problem("remote-offline", None, f"{r.name}: {e}", False))
            continue
        st_state = st.get("state")
        detail = st.get("detail") or ""
        if st_state == "connected":
            continue
        if st_state == "version":
            problems.append(Problem(
                "remote-version", None,
                f"{r.name}: other Force version{(' (' + detail + ')') if detail else ''}; "
                f"force host upgrade {r.name}", False))
        else:
            problems.append(Problem(
                "remote-offline", None,
                f"{r.name}: {st_state or 'unknown'}{(' — ' + detail) if detail else ''}", False))

    if systemd:
        for unit in ("force.service", "force-tmux.service"):
            if not _unit_active(unit):
                problems.append(Problem(
                    "service", None, f"{unit} not active", False,
                ))

    tmux = cockpit.tmux
    if not tmux.server_running():
        problems.append(Problem("tmux", None, "tmux server not running", False))
        return problems

    try:
        reg = registry_load(cockpit.paths.registry)
    except RegistryError as e:
        problems.append(Problem("registry", None, str(e), False))
        return problems

    snap = state.read_snapshot(cockpit.paths.snapshot)
    now_ms = int(time.time() * 1000)
    fresh = snap is not None and now_ms - snap.generated_ms <= _SNAPSHOT_FRESH_MS
    if not fresh:
        problems.append(Problem(
            "snapshot", None, "daemon not writing (snapshot missing or stale)", False,
        ))

    try:
        panes = tmux.panes(strict=True)
    except TmuxError as e:
        problems.append(Problem("tmux", None, f"tmux listing failed: {e}", False))
        return problems

    rows = _window_rows(tmux)
    windows = _windows(rows)
    window_of = {r["pane_id"]: r for r in rows}
    session_names = {p.session for p in panes}
    live_names = {e.name for e in reg.live()}

    sidebar_on = cockpit.sidebar_enabled()
    hidden = tmux.get_option("@force_sidebar_hidden") == "1"

    for entry in reg.live():
        name = entry.name
        if name not in session_names:
            problems.append(Problem("no-session", name, "tmux session missing", True))
            continue
        sp = [p for p in panes if p.session == name]
        agent = [p for p in sp if p.role == "agent"]
        headers = [p for p in sp if p.role == "header"]
        sidebars = [p for p in sp if p.role == "sidebar"]

        if not agent:
            problems.append(Problem("no-agent", name, "no agent pane", True))
        if cockpit.split:
            # split mode (SPEC §39): host sessions hold only their main pane; the daemon removes the rest
            extra = [p for p in sp if p.role != "agent"]
            if extra:
                problems.append(Problem("split-extra-pane", name,
                                        f"{len(extra)} pane(s) besides the main pane (split mode)", False))
            continue
        if len(headers) == 0:
            problems.append(Problem("header-missing", name, "missing header pane", True))
        elif len(headers) > 1:
            problems.append(Problem("header-dup", name, f"{len(headers)} header panes", True))
        if sidebar_on:
            if len(sidebars) == 0:
                problems.append(Problem("sidebar-missing", name, "missing sidebar pane", True))
            elif len(sidebars) > 1:
                problems.append(Problem("sidebar-dup", name, f"{len(sidebars)} sidebar panes", True))
            if agent:
                cwin = window_of.get(agent[0].pane_id, {}).get("window_id")
                for sb in sidebars:
                    swin = window_of.get(sb.pane_id, {}).get("window_id")
                    if hidden and swin == cwin:
                        problems.append(Problem(
                            "sidebar-placement", name,
                            "sidebar in the agent window while hidden", True))
                        break
                    if not hidden and swin != cwin:
                        problems.append(Problem(
                            "sidebar-placement", name,
                            "sidebar parked while not hidden", True))
                        break

    # stray windows: in live sessions, any window with index != 0 that is not a force-stash window that
    # actually holds that session's sidebar.
    for (sess, _wid), win in windows.items():
        if sess not in live_names:
            continue                            # orphan sessions are reported, never touched here
        if win["index"] == "0":
            continue
        if cockpit.split:
            problems.append(Problem("stray-window", sess, f"extra window {win['name']!r} (split mode)", True))
            continue
        is_stash = win["name"] == "force-stash"
        has_sidebar = "sidebar" in win["roles"]
        if not is_stash or not has_sidebar:
            problems.append(Problem(
                "stray-window", sess,
                f"window {win['name']!r} (index {win['index']}) is not a force-stash with a sidebar",
                True))

    # orphan tmux sessions: not a live registry session. Not fixable (may be archived / being created).
    for sess in session_names:
        if sess not in live_names:
            problems.append(Problem(
                "orphan-session", sess, "tmux session not in the live registry", False))

    if cockpit.split:
        problems += _check_ui(cockpit)

    cockpit_state = tmux.get_option("@force_state")
    cockpit_app = tmux.get_option("@force_app")
    if not cockpit_state or not cockpit_app:
        problems.append(Problem(
            "options", None, "missing global @force_state/@force_app option", True))

    return problems


def _check_ui(cockpit) -> list[Problem]:
    """Split mode: every ui session (one per terminal) has its main pane, header and (unless switched off)
    sidebar; a pane that died is respawned by the daemon, a missing one needs `force` again."""
    ui = cockpit.ui_tmux
    if not ui.server_running():
        return []
    out = []
    sidebar_on = ui.get_option("@force_sidebar") != "off"
    by_session: dict[str, list[str]] = {}
    for p in ui.panes():
        by_session.setdefault(p.session, []).append(p.role or "-")
    for sess, roles in by_session.items():
        want = ["agent", "header"] + (["sidebar"] if sidebar_on else [])
        missing = [r for r in want if r not in roles]
        if missing:
            out.append(Problem("ui-incomplete", sess, f"ui session without {', '.join(missing)} (run force again)", False))
    return out


def _unit_active(unit: str) -> bool:
    """Thin wrapper: the active check lives in force/services (SPEC §51)."""
    from . import services
    name = {"force.service": "daemon", "force-tmux.service": "tmux",
            "force-smoke.service": "smoke", "force-smoke.timer": "smoke"}.get(unit)
    return services.is_active(name) if name else False


# --- repair -------------------------------------------------------------
def repair(cockpit, problems: list[Problem]) -> list[str]:
    lines: list[str] = []
    for p in problems:
        if not p.fixable:
            continue
        if p.code == "no-session":
            entry = _entry(cockpit, p.session)
            if entry is not None:
                cockpit.create(entry)
                lines.append(f"fixed no-session: created {p.session}")
        elif p.code == "no-agent":
            cockpit.restart(p.session)
            lines.append(f"fixed no-agent: restarted {p.session}")
        elif p.code == "header-missing":
            cockpit.ensure_header(p.session)
            lines.append(f"fixed header-missing: added header to {p.session}")
        elif p.code == "header-dup":
            cockpit.dedupe_sidebars()
            lines.append(f"fixed header-dup: removed extra headers in {p.session}")
        elif p.code == "sidebar-missing":
            cockpit.ensure_sidebar(p.session)
            lines.append(f"fixed sidebar-missing: added sidebar to {p.session}")
        elif p.code == "sidebar-dup":
            cockpit.dedupe_sidebars()
            lines.append(f"fixed sidebar-dup: removed extra sidebars in {p.session}")
        elif p.code == "sidebar-placement":
            _fix_placement(cockpit, p.session)
            lines.append(f"fixed sidebar-placement: restored sidebar position in {p.session}")
        elif p.code == "stray-window":
            _fix_stray_windows(cockpit, p.session)
            lines.append(f"fixed stray-window: removed stray windows in {p.session}")
        elif p.code == "options":
            cockpit.tmux.set_option("@force_state", str(cockpit.paths.state))
            cockpit.tmux.set_option("@force_app", str(Path(cockpit.paths.tmux_conf).parent.parent))
            lines.append("fixed options: set @force_state/@force_app")
    return lines


def _entry(cockpit, name):
    from .registry import locked
    with locked(cockpit.paths.home) as reg:
        return reg.get(name)


def _fix_placement(cockpit, name) -> None:
    hidden = cockpit.tmux.get_option("@force_sidebar_hidden") == "1"
    rows = _window_rows(cockpit.tmux)
    window_of = {r["pane_id"]: r for r in rows}
    panes = cockpit.tmux.panes()
    agent = next((p for p in panes if p.session == name and p.role == "agent"), None)
    cwin = window_of.get(agent.pane_id, {}).get("window_id") if agent else None
    for sb in (p for p in panes if p.session == name and p.role == "sidebar"):
        swin = window_of.get(sb.pane_id, {}).get("window_id")
        if hidden and cwin and swin == cwin:
            cockpit.tmux.run("break-pane", "-d", "-s", sb.pane_id, "-t", f"={name}:",
                         "-n", "force-stash")
            return
        if not hidden and agent and swin != cwin:
            cockpit.tmux.run("join-pane", "-d", "-h", "-b", "-f",
                         "-l", str(cockpit.cfg.sidebar_width), "-s", sb.pane_id,
                         "-t", agent.pane_id)
            return


def _fix_stray_windows(cockpit, name) -> None:
    rows = _window_rows(cockpit.tmux)
    windows = _windows(rows)
    for (sess, wid) in list(windows):
        win = windows[(sess, wid)]
        if sess != name or win["index"] == "0":
            continue
        is_stash = win["name"] == "force-stash"
        has_sidebar = "sidebar" in win["roles"]
        if not is_stash or not has_sidebar:
            cockpit.tmux.run("kill-window", "-t", wid)


# --- CLI driver ---------------------------------------------------------
def run_doctor(cockpit, fix: bool = False, out=print, systemd: bool = True) -> int:
    def report(problems: list[Problem]) -> None:
        for prob in problems:
            where = prob.session or "-"
            out(f"✗ {where} {prob.code}: {prob.detail}")

    problems = check(cockpit, systemd=systemd)
    if not problems:
        out("✓ healthy")
        return 0
    report(problems)
    if not fix:
        return 1
    for action in repair(cockpit, problems):
        out(f"✓ {action}")
    remaining = check(cockpit, systemd=systemd)
    if remaining:
        report(remaining)
    return 0 if not remaining else 1
