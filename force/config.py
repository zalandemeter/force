"""Paths and user settings (SPEC §2).

Settings live in `~/.config/force/settings.json` (env FORCE_CONFIG), remote hosts in `~/.config/force/hosts.json`
(see force/remote.py).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path          # FORCE_HOME: registry.json, registry.lock, archive/
    state: Path         # FORCE_STATE: snapshot.json, seen.json, header/, daemon.lock
    claude_dir: Path    # FORCE_CLAUDE_DIR: sessions/, projects/
    claude_bin: Path    # FORCE_CLAUDE_BIN
    tmux_socket: str    # FORCE_TMUX_SOCKET (tmux -L)
    tmux_conf: Path     # FORCE_TMUX_CONF
    config_file: Path   # FORCE_CONFIG: ~/.config/force/settings.json
    ui_socket: str = "force-ui"   # FORCE_UI_SOCKET: the ui tmux server in split mode (SPEC §39)

    @property
    def ui_conf(self) -> Path:
        """The ui server's tmux.conf, next to the host's (share/tmux-ui.conf)."""
        return self.tmux_conf.parent / "tmux-ui.conf"

    @property
    def registry(self) -> Path:
        return self.home / "registry.json"

    @property
    def registry_lock(self) -> Path:
        return self.home / "registry.lock"

    @property
    def archive(self) -> Path:
        return self.home / "archive"

    @property
    def snapshot(self) -> Path:
        return self.state / "snapshot.json"

    @property
    def seen(self) -> Path:
        return self.state / "seen.json"

    @property
    def header_dir(self) -> Path:
        return self.state / "header"

    @property
    def daemon_lock(self) -> Path:
        return self.state / "daemon.lock"


@dataclass(frozen=True)
class Config:
    stale_hours: float = 12
    upgrade_idle_minutes: float = 10
    auto_upgrade: bool = True
    sidebar_width: int = 29
    crash_restart_max: int = 3
    crash_window_minutes: float = 10
    start_grace_seconds: float = 30
    dev_reload: bool = True
    sharing: bool = False       # SPEC §19, parked 2026-10-09: no proxies, `force share` refuses
    animations: str = "full"     # full | subtle (no continuous shimmer) | off (SPEC §23)
    glyphs: str = "auto"         # auto (by terminal) | rich (Nerd Font icons) | plain (SPEC §28)
    # auto: terminals that get the rich tier, an extended regex matched (case-insensitively) against
    # tmux's "#{client_termname} #{client_termtype}", e.g. add "|vte" for Tilix/GNOME Terminal with a
    # Nerd Font symbols font installed (SPEC §28)
    rich_terminals: str = "kitty|ghostty|wezterm"
    intro: bool = True           # startup intro on attach (SPEC §31)
    # cell height/width for the intro when the terminal does not report its pixel size (VTE/Tilix
    # does not); 0 = the intro's own guess (2.0)
    intro_cell_aspect: float = 0
    # OpenCode sessions (SPEC §36): false = the `opencode` on PATH (on a shared server often the
    # admin's, kept current by them); true = Force keeps its own copy in ~/.local/share/force/opencode, updated to the latest
    # release like Claude Code updates itself (with auto_upgrade)
    opencode_managed: bool = False
    # check once a day whether a newer Force exists (SPEC §3); a found update shows in the sidebar footer
    check_updates: bool = True
    # classic: sidebar and header panes live in every session window of the host tmux (one server);
    # split: sessions alone in the host tmux, sidebar/header in a ui tmux per terminal that shows the
    # session through a nested client (SPEC §39; `force ui split|classic`)
    ui: str = "split"
    # a shell command run when a session needs you, finishes or stops (SPEC §49.4); "" = off
    notify_command: str = ""
    # also notify about the session a terminal is showing
    notify_when_viewed: bool = False


@dataclass(frozen=True)
class SettingSpec:
    """One user setting: the JSON key in settings.json (camelCase) -> a Config field (snake_case)."""
    key: str            # JSON key
    field: str          # Config field
    kind: str           # bool | int | number | number0 (>= 0) | choice | icons | layout
    allowed: tuple      # allowed JSON values for choice/icons/layout (() otherwise)
    public: bool        # shown by `force settings` without --all
    description: str    # one line for the schema


# kind -> the JSON types (schema) and how a value maps to the Config field.
# icons: "auto"|"on"|"off" -> glyphs "auto"|"rich"|"plain"; layout: "split"|"classic" -> ui as-is.
_KIND_TYPE = {"bool": "boolean", "int": "integer", "number": "number", "number0": "number",
             "string": "string", "choice": "string", "icons": "string", "layout": "string"}
_ICONS_JSON = {"auto": "auto", "on": "rich", "off": "plain"}
_ICONS_FIELD = {"auto": "auto", "rich": "on", "plain": "off"}

SETTINGS = [
    SettingSpec("animations", "animations", "choice", ("full", "subtle", "off"), True,
               "animate the working shimmer: full | subtle | off"),
    SettingSpec("intro", "intro", "bool", (), True,
               "play the startup intro on attach (force --no-intro skips one attach)"),
    SettingSpec("icons", "glyphs", "icons", ("auto", "on", "off"), True,
               "icon tier: auto (by terminal) | on (Nerd Font symbols) | off"),
    SettingSpec("sidebarWidth", "sidebar_width", "int", (), True,
               "sidebar width in columns"),
    SettingSpec("staleAfterHours", "stale_hours", "number", (), True,
               "after how many idle hours an unseen session is stale"),
    SettingSpec("autoUpgradeSessions", "auto_upgrade", "bool", (), True,
               "restart idle outdated agent sessions automatically"),
    SettingSpec("autoUpgradeIdleMinutes", "upgrade_idle_minutes", "number", (), True,
               "how long an outdated session must be idle to auto-upgrade"),
    SettingSpec("layout", "ui", "layout", ("split", "classic"), True,
               "split (ui tmux per terminal) | classic (one tmux)"),
    SettingSpec("opencodeManaged", "opencode_managed", "bool", (), True,
               "keep Force's own OpenCode copy on the latest release"),
    SettingSpec("checkForUpdates", "check_updates", "bool", (), True,
               "check once a day whether a newer Force exists"),
    SettingSpec("notifyCommand", "notify_command", "string", (), True,
               "command run when a session needs you, finishes or stops ($FORCE_MESSAGE, $FORCE_EVENT, …)"),
    SettingSpec("notifyWhenViewed", "notify_when_viewed", "bool", (), False,
               "also notify about a session a terminal is showing"),
    SettingSpec("crashRestartMax", "crash_restart_max", "int", (), False,
               "how often a session is restarted after a crash"),
    SettingSpec("crashWindowMinutes", "crash_window_minutes", "number", (), False,
               "the crash window those restarts count in"),
    SettingSpec("startGraceSeconds", "start_grace_seconds", "number", (), False,
               "how long a session may stay down before a restart counts"),
    SettingSpec("richTerminals", "rich_terminals", "string", (), False,
               "terminals that get the rich icons in auto mode (a regex)"),
    SettingSpec("introCellAspect", "intro_cell_aspect", "number0", (), False,
               "intro cell height/width when the terminal reports no pixel size (0 = automatic)"),
    SettingSpec("reloadOnChange", "dev_reload", "bool", (), False,
               "reload code and config changes without restarting the daemon"),
    SettingSpec("sharing", "sharing", "bool", (), False,
               "session sharing with another user (parked)"),
]

_BY_KEY = {s.key: s for s in SETTINGS}
_BY_FIELD = {s.field: s for s in SETTINGS}


def _expand_home(value: str, env: dict[str, str], fallback_home: str) -> str:
    """Expand a leading '~' in `value` using the env's HOME (or os-level expanduser)."""
    if value == "~":
        return env.get("HOME") or fallback_home
    if value.startswith("~/"):
        return os.path.join(env.get("HOME") or fallback_home, value[2:])
    return value


def _claude_bin(env, expand) -> Path:
    """FORCE_CLAUDE_BIN, else Claude Code's own installer location ~/.local/bin/claude, else `claude` on PATH
    (a system package: /usr/bin/claude), else the installer location (reported missing where it is used)."""
    if env.get("FORCE_CLAUDE_BIN"):
        return Path(expand(env["FORCE_CLAUDE_BIN"]))
    default = Path(expand("~/.local/bin/claude"))
    if os.access(default, os.X_OK):
        return default
    found = shutil.which("claude", path=env.get("PATH", ""))
    return Path(found) if found else default


WSL_ICONS_HINT = ("On WSL, Windows Terminal draws Force with Windows fonts, so Force shows plain symbols. For its icons: "
                  "install \"Symbols Nerd Font Mono\" in Windows, add it to your Windows Terminal profile's font, "
                  "then run: force settings set icons on")


def is_wsl(env: dict[str, str] | None = None, proc: Path = Path("/proc")) -> bool:
    """Running under Windows Subsystem for Linux. The terminal is then a Windows program that draws with Windows'
    fonts, so the Linux font set (fc-list) says nothing about which glyphs it can show."""
    env = os.environ if env is None else env
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    if (proc / "sys/fs/binfmt_misc/WSLInterop").exists():
        return True
    try:
        return "microsoft" in (proc / "sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


INSTANCE_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
INSTANCE_ERROR = "FORCE_INSTANCE must be a short lowercase name (a-z, 0-9)"


def instance(env: dict[str, str] | None = None) -> str | None:
    """The development instance this process belongs to (SPEC §2.1), or None for the live install.

    FORCE_INSTANCE unset or empty = the live install. An invalid name raises ValueError (INSTANCE_ERROR)."""
    env = os.environ if env is None else env
    name = env.get("FORCE_INSTANCE") or ""
    if not name:
        return None
    if not INSTANCE_RE.match(name):
        raise ValueError(INSTANCE_ERROR)
    return name


def launcher_path(env: dict[str, str] | None = None) -> str:
    """The `force` launcher that runs this install: FORCE_BIN (set by an instance's launcher, SPEC §2.1), else
    ~/.local/bin/force."""
    env = os.environ if env is None else env
    return env.get("FORCE_BIN") or os.path.join(os.path.expanduser("~"), ".local", "bin", "force")


def get_paths(env: dict[str, str] | None = None) -> Paths:
    """Resolve all paths from env (default os.environ), applying SPEC §2 defaults. Does not create dirs."""
    if env is None:
        env = os.environ
    fallback_home = os.path.expanduser("~")
    expand = lambda v: _expand_home(v, env, fallback_home)
    # abspath, not resolve(): through the live `app` symlink this must stay the symlinked path, so
    # the daemon sees (and source-files) the tmux.conf of whatever release is live now.
    repo_share = Path(os.path.abspath(__file__)).parent.parent / "share" / "tmux.conf"

    # a development instance (SPEC §2.1) gets its own defaults; explicit FORCE_* variables still win
    inst = instance(env)
    name = f"force-{inst}" if inst else "force"
    return Paths(
        home=Path(expand(env.get("FORCE_HOME", f"~/.local/share/{name}"))),
        state=Path(expand(env.get("FORCE_STATE", f"~/.local/state/{name}"))),
        claude_dir=Path(expand(env.get("FORCE_CLAUDE_DIR", "~/.claude"))),
        claude_bin=_claude_bin(env, expand),
        tmux_socket=env.get("FORCE_TMUX_SOCKET", name),
        tmux_conf=Path(expand(env.get("FORCE_TMUX_CONF", str(repo_share)))),
        config_file=Path(expand(env.get("FORCE_CONFIG", f"~/.config/{name}/settings.json"))),
        ui_socket=env.get("FORCE_UI_SOCKET", f"{name}-ui"),
    )


def tmux_bin_dir(paths: Paths) -> Path | None:
    """The dir holding Force's own static tmux (`<app>/share/tmux-bin/`), if the tmux binary is present.

    Returns None on dev deploys where no tmux is shipped, so the system tmux is used as today."""
    d = paths.tmux_conf.parent / "tmux-bin"
    if (d / "tmux").is_file() and os.access(d / "tmux", os.X_OK):
        return d
    return None


def _check_json(spec: SettingSpec, value) -> object:
    """Check `value` under the NEW rules (a settings.json value); return the Config field value.
    Raises ValueError with the reason."""
    kind = spec.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be a JSON bool")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an int")
        if value <= 0:
            raise ValueError("must be positive")
        return value
    if kind in ("number", "number0"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if not math.isfinite(float(value)):
            raise ValueError("must be a number")
        if (kind == "number" and value <= 0) or (kind == "number0" and value < 0):
            raise ValueError("must be positive" if kind == "number" else "must be >= 0")
        return value
    if kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value
    if kind == "choice":
        if value not in spec.allowed:
            raise ValueError(f"must be one of {', '.join(spec.allowed)}")
        return value
    if kind == "icons":
        if value not in spec.allowed:
            raise ValueError("must be one of auto, on, off")
        return _ICONS_JSON[value]
    if kind == "layout":
        if value not in spec.allowed:
            raise ValueError("must be one of split, classic")
        return value
    raise ValueError("unknown kind")


def _check_field(spec: SettingSpec, value):
    """Check a Config field value under the OLD (TOML) rules (migration): the config.toml used the snake_case
    Config fields, so icons was \"auto\"|\"rich\"|\"plain\" and naive numeric/layout rules applied."""
    kind = spec.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be a bool")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an int")
        if value <= 0:
            raise ValueError("must be positive")
    elif kind in ("number", "number0"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if not math.isfinite(float(value)):
            raise ValueError("must be a number")
        if (kind == "number" and value <= 0) or (kind == "number0" and value < 0):
            raise ValueError("must be positive" if kind == "number" else "must be >= 0")
    elif kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
    else:                                   # choice, icons, layout
        allowed = spec.allowed if spec.kind != "icons" else ("auto", "rich", "plain")
        if value not in allowed:
            raise ValueError("not allowed")
    return value


def _to_json(spec: SettingSpec, field_value) -> object:
    """A Config field value -> the settings.json value (e.g. glyphs rich -> icons \"on\")."""
    if spec.kind == "icons":
        return _ICONS_FIELD.get(field_value, field_value)
    return field_value


def load_config(path: Path) -> Config:
    """Parse settings.json; missing file -> defaults; invalid JSON / not an object -> one warning + defaults;
    wrong-typed, not allowed or non-positive values -> warning, that key's default."""
    values: dict[str, object] = {f.name: f.default for f in fields(Config)}

    try:
        raw = path.read_text()
    except FileNotFoundError:
        return Config(**values)  # type: ignore[arg-type]

    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"force: warning: {path}: invalid JSON ({e}); using defaults", file=sys.stderr)
        return Config(**values)  # type: ignore[arg-type]

    if not isinstance(data, dict):
        print(f"force: warning: {path}: not a JSON object; using defaults", file=sys.stderr)
        return Config(**values)  # type: ignore[arg-type]

    for key, value in data.items():
        if key == "$schema":
            continue                              # allowed and ignored
        spec = _BY_KEY.get(key)
        if spec is None:
            print(f"force: warning: unknown settings key {key!r} ignored", file=sys.stderr)
            continue
        try:
            field_value = _check_json(spec, value)
        except ValueError as e:
            print(f"force: warning: settings {key}: {e}; using default", file=sys.stderr)
            continue
        values[spec.field] = field_value

    return Config(**values)  # type: ignore[arg-type]


def _read_object(path: Path) -> dict:
    """The file's JSON object (missing -> {}); an invalid JSON or non-object file raises ValueError."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ValueError(f"{path}: invalid JSON ({e}); not overwritten") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object; not overwritten")
    return data


def _write_json_atomic(path: Path, data: dict) -> None:
    """Pretty JSON (indent 2, trailing newline), written through a tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def save_setting(path: Path, key: str, value) -> None:
    """Set one setting, keeping the file's other keys, their order and \"$schema\" (missing file -> {}).
    A bad value (or an unknown key) raises ValueError."""
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown settings key {key!r} (public keys: "
                        + ", ".join(s.key for s in SETTINGS if s.public) + ")")
    try:
        _check_json(spec, value)
    except ValueError as e:
        raise ValueError(f"settings {key}: {e}") from None
    data = _read_object(path)
    data[key] = value
    _write_json_atomic(path, data)


def unset_setting(path: Path, key: str) -> None:
    """Remove a setting, keeping the file's other keys, their order and \"$schema\".
    An unknown key raises ValueError; the other keys are never touched."""
    if key not in _BY_KEY:
        raise ValueError(f"unknown settings key {key!r} (public keys: "
                       + ", ".join(s.key for s in SETTINGS if s.public) + ")")
    data = _read_object(path)
    if key in data:
        del data[key]
        _write_json_atomic(path, data)


def parse_value(key: str, text: str):
    """CLI text -> a typed setting value (bool: true/false/on/off/yes/no case-insensitive; numbers parse;
    strings/choices checked). Unknown key -> ValueError naming the public keys."""
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown settings key {key!r} (public keys: "
                       + ", ".join(s.key for s in SETTINGS if s.public) + ")")
    if spec.kind == "bool":
        low = (text or "").strip().lower()
        if low in ("true", "on", "yes"):
            value = True
        elif low in ("false", "off", "no"):
            value = False
        else:
            raise ValueError(f"settings {key}: expected true/false/on/off/yes/no, got {text!r}")
    elif spec.kind == "int":
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"settings {key}: expected an int, got {text!r}") from None
    elif spec.kind in ("number", "number0"):
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"settings {key}: expected a number, got {text!r}") from None
    else:
        value = text
    try:
        _check_json(spec, value)
    except ValueError as e:
        raise ValueError(f"settings {key}: {e}") from None
    return value


def schema() -> dict:
    """A JSON Schema (draft-07) for settings.json, generated from the SETTINGS table."""
    properties: dict = {}
    for spec in SETTINGS:
        props = {"type": _KIND_TYPE[spec.kind], "default": _to_json(spec, getattr(Config(), spec.field)),
                "description": spec.description}
        if spec.allowed:
            props["enum"] = list(spec.allowed)
        properties[spec.key] = props
    properties["$schema"] = {"type": "string", "description": "the settings schema (ignored)"}
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Force settings",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def ensure_dirs(paths: Paths) -> None:
    """mkdir -p home, home/archive, state, state/header (mode 0700)."""
    for p in (paths.home, paths.home / "archive", paths.state, paths.state / "header"):
        os.makedirs(p, mode=0o700, exist_ok=True)


if __name__ == "__main__":      # python -m force.config --schema
    print(json.dumps(schema(), indent=2))
