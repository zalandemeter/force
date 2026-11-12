"""Process facts: the one module that reads /proc (SPEC §51).

On Linux every function reads `/proc` (or an explicit root, in the same format, for tests); on macOS they
use libproc + sysctl through ctypes. A function given an explicit `root` always reads that directory in the
/proc format, on any OS. All functions return None / [] / {} on any failure and never raise for a missing
or foreign process.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_PROC_ROOT = Path("/proc")

# ---------------------------------------------------------------------------
# Linux /proc readers (also used for an explicit root on any OS - the tests)
# ---------------------------------------------------------------------------


def _linux_pids(root: Path) -> list[int]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    pids = [int(p.name) for p in entries if p.name.isdigit()]
    pids.sort()
    return pids


def _linux_ppid(pid: int, root: Path) -> int | None:
    """Parent pid from /proc/<pid>/stat (parse from the LAST ')')."""
    try:
        text = (root / str(pid) / "stat").read_text()
    except OSError:
        return None
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    parts = text[rparen + 1:].split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _linux_children_map(root: Path) -> dict[int, list[int]]:
    """ppid -> list of child pids from /proc/<pid>/stat (field 4). The comm field in parentheses may
    contain spaces and ')' so it is parsed from the LAST ')'."""
    cmap: dict[int, list[int]] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return cmap
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            text = (p / "stat").read_text()
        except OSError:
            continue
        rparen = text.rfind(")")
        if rparen < 0:
            continue
        parts = text[rparen + 1:].split()
        if len(parts) < 2:
            continue
        try:
            ppid = int(parts[1])
        except ValueError:
            continue
        cmap.setdefault(ppid, []).append(int(p.name))
    for kids in cmap.values():
        kids.sort()          # pid order
    return cmap


def _linux_exe(pid: int, root: Path) -> str | None:
    try:
        link = os.readlink(str(root / str(pid) / "exe"))
    except OSError:
        return None
    return link.removesuffix(" (deleted)")


def _linux_comm(pid: int, root: Path) -> str | None:
    try:
        with open(root / str(pid) / "comm") as f:
            name = f.read().strip()
    except OSError:
        return None
    return name or None


def _linux_cmdline(pid: int, root: Path) -> list[str] | None:
    try:
        data = (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    args = [c.decode("utf-8", "replace") for c in data.split(b"\0") if c]
    return args


def _linux_uid(pid: int, root: Path) -> int | None:
    try:
        return os.stat(root / str(pid)).st_uid
    except OSError:
        return None


# Boot time (epoch s) from /proc/stat, read once: the file is large on a many-core host and
# started_ms is called for every pane on every daemon tick.
_BOOT_TIME: int | None = None


def _boot_time() -> int | None:
    global _BOOT_TIME
    if _BOOT_TIME is None:
        _BOOT_TIME = _read_boot_time()
    return _BOOT_TIME


def _read_boot_time() -> int | None:
    try:
        with open(_PROC_ROOT / "stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _linux_started_ms(pid: int, root: Path) -> int | None:
    try:
        with open(root / str(pid) / "stat") as f:
            line = f.read()
        rparen = line.rfind(")")
        if rparen < 0:
            return None
        post = line[rparen + 1:].split()
        # field 22 = starttime; post[0] is field 3
        start_ticks = int(post[22 - 3])
        clk = os.sysconf("SC_CLK_TCK") or 100
        if clk == 0:
            clk = 100
        btime = _boot_time()
        if btime is None:
            return None
        return int((btime + start_ticks / clk) * 1000)
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# macOS backend (libproc + sysctl through ctypes), loaded lazily
# ---------------------------------------------------------------------------

PROC_PIDTBSDINFO = 3
_PROC_PIDPATHINFO_MAXSIZE = 4096
_KERN_ARGMAX = 262144


class _BsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _bsd_fields(info) -> tuple:
    """(ppid, uid, comm, started_ms) from a _BsdInfo."""
    ppid = int(info.pbi_ppid)
    uid = int(info.pbi_uid)
    comm = info.pbi_comm.split(b"\0")[0].decode("utf-8", "replace")
    comm = comm or None
    started_ms = int(info.pbi_start_tvsec) * 1000 + int(info.pbi_start_tvusec) // 1000
    return ppid, uid, comm, started_ms


def _darwin_pids() -> list[int]:
    d = _load_darwin()
    if not d:                       # not darwin, or the libraries did not load
        return []
    libproc = d["libproc"]
    try:
        n = libproc.proc_listallpids(None, 0)
    except Exception:
        return []
    if n <= 0:
        return []
    buf = (ctypes.c_int * (n + 64))()
    try:
        filled = libproc.proc_listallpids(buf, ctypes.sizeof(buf))
    except Exception:
        return []
    pids = [int(buf[i]) for i in range(max(0, filled)) if buf[i] > 0]
    pids.sort()
    return pids


def _darwin_info(pid: int):
    d = _load_darwin()
    if not d:                       # not darwin, or the libraries did not load
        return None
    libproc = d["libproc"]
    info = _BsdInfo()
    try:
        n = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info),
                                 ctypes.sizeof(info))
    except Exception:
        return None
    if n != ctypes.sizeof(info):
        return None
    return info


def _darwin_bsd(pid: int):
    info = _darwin_info(pid)
    if info is None:
        return None
    return _bsd_fields(info)


def _darwin_ppid(pid: int) -> int | None:
    f = _darwin_bsd(pid)
    return f[0] if f else None


def _darwin_uid(pid: int) -> int | None:
    f = _darwin_bsd(pid)
    return f[1] if f else None


def _darwin_comm(pid: int) -> str | None:
    f = _darwin_bsd(pid)
    return f[2] if f else None


def _darwin_started_ms(pid: int) -> int | None:
    f = _darwin_bsd(pid)
    return f[3] if f else None


def _darwin_children_map() -> dict[int, list[int]]:
    cmap: dict[int, list[int]] = {}
    for pid in _darwin_pids():
        ppid = _darwin_ppid(pid)
        if ppid is not None:
            cmap.setdefault(ppid, []).append(pid)
    for kids in cmap.values():
        kids.sort()
    return cmap


def _darwin_exe(pid: int) -> str | None:
    d = _load_darwin()
    if not d:                       # not darwin, or the libraries did not load
        return None
    libproc = d["libproc"]
    buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    try:
        n = libproc.proc_pidpath(pid, buf, _PROC_PIDPATHINFO_MAXSIZE)
    except Exception:
        return None
    if n > 0:
        return buf.value.decode("utf-8", "replace")
    return None


def _darwin_cmdline(pid: int) -> list[str] | None:
    d = _load_darwin()
    if not d:                       # not darwin, or the libraries did not load
        return None
    libc = d["libc"]
    # KERN_ARGMAX (CTL_KERN=1, KERN_ARGMAX=8), fallback 262144
    argmax_c = ctypes.c_int(0)
    mib = (ctypes.c_int * 2)(1, 8)
    size = ctypes.c_size_t(ctypes.sizeof(argmax_c))
    try:
        rc = libc.sysctl(ctypes.byref(mib), 2, ctypes.byref(argmax_c), ctypes.byref(size), None, 0)
    except Exception:
        rc = -1
    argmax = argmax_c.value if rc == 0 and argmax_c.value > 0 else _KERN_ARGMAX
    buf = ctypes.create_string_buffer(argmax)
    mib2 = (ctypes.c_int * 3)(1, 49, pid)   # CTL_KERN=1, KERN_PROCARGS2=49, pid
    size2 = ctypes.c_size_t(argmax)
    try:
        if libc.sysctl(ctypes.byref(mib2), 3, buf, ctypes.byref(size2), None, 0) != 0:
            return None
    except Exception:
        return None
    return _parse_procargs2(buf.raw[:size2.value])


def _parse_procargs2(buf: bytes) -> list[str] | None:
    """Parse a KERN_PROCARGS2 buffer: first 4 bytes = argc (little-endian int32); then the exec path,
    NUL-terminated; then any NUL padding; then `argc` NUL-terminated strings = argv."""
    if len(buf) < 4:
        return None
    argc = int.from_bytes(buf[0:4], "little", signed=True)
    if argc < 0:
        return None
    rest = buf[4:]
    nul = rest.find(b"\0")
    if nul < 0:
        return None
    rest = rest[nul + 1:]
    rest = rest.lstrip(b"\0")          # padding to the first argv string
    argv: list[str] = []
    for _ in range(argc):
        nul = rest.find(b"\0")
        if nul < 0:
            break
        argv.append(rest[:nul].decode("utf-8", "replace"))
        rest = rest[nul + 1:]
    return argv


_DARWIN = None


def _load_darwin():
    """Load libproc + libc (lazily, only on darwin); None if they cannot be loaded."""
    global _DARWIN
    if _DARWIN is not None:
        return _DARWIN
    if sys.platform != "darwin":
        _DARWIN = False
        return _DARWIN
    libproc = _cdll(("/usr/lib/libproc.dylib",), "proc")
    libc = _cdll(("/usr/lib/libSystem.B.dylib",), "c")
    if libproc is None or libc is None:
        _DARWIN = False
        return _DARWIN
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidpath.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libc.sysctl.restype = ctypes.c_int
    libc.sysctl.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    _DARWIN = {"libproc": libproc, "libc": libc}
    return _DARWIN


def _cdll(paths: tuple, fallback: str):
    for p in paths:
        try:
            return ctypes.CDLL(p)
        except OSError:
            continue
    found = ctypes.util.find_library(fallback)
    if found:
        try:
            return ctypes.CDLL(found)
        except OSError:
            return None
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def pids(root: Path | None = None) -> list[int]:
    """Every pid, sorted ascending."""
    if root is not None:
        return _linux_pids(Path(root))
    if sys.platform.startswith("linux"):
        return _linux_pids(_PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_pids()
    return []


def ppid(pid: int, root: Path | None = None) -> int | None:
    if root is not None:
        return _linux_ppid(pid, Path(root))
    if sys.platform.startswith("linux"):
        return _linux_ppid(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_ppid(pid)
    return None


def children_map(root: Path | None = None) -> dict[int, list[int]]:
    """ppid -> sorted child pids."""
    if root is not None:
        return _linux_children_map(Path(root))
    if sys.platform.startswith("linux"):
        return _linux_children_map(_PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_children_map()
    return {}


def exe(pid: int, root: Path | None = None) -> str | None:
    """Executable path; Linux: readlink(<root>/<pid>/exe) with a trailing " (deleted)" removed."""
    if root is not None:
        return _linux_exe(pid, Path(root))
    if sys.platform.startswith("linux"):
        return _linux_exe(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_exe(pid)
    return None


def comm(pid: int, root: Path | None = None) -> str | None:
    """Short process name; Linux: <root>/<pid>/comm stripped, "" -> None."""
    if root is not None:
        return _linux_comm(pid, Path(root))
    if sys.platform.startswith("linux"):
        return _linux_comm(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_comm(pid)
    return None


def cmdline(pid: int, root: Path | None = None) -> list[str] | None:
    """argv; Linux: <root>/<pid>/cmdline split on NUL, decoded utf-8 "replace"."""
    if root is not None:
        return _linux_cmdline(pid, Path(root))
    if sys.platform.startswith("linux"):
        return _linux_cmdline(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_cmdline(pid)
    return None


def uid(pid: int, root: Path | None = None) -> int | None:
    """Owner uid; Linux: os.stat(<root>/<pid>).st_uid."""
    if root is not None:
        return _linux_uid(pid, Path(root))
    if sys.platform.startswith("linux"):
        return _linux_uid(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_uid(pid)
    return None


def started_ms(pid: int | None) -> int | None:
    """Process start, epoch milliseconds; None for None."""
    if pid is None:
        return None
    if sys.platform.startswith("linux"):
        return _linux_started_ms(pid, _PROC_ROOT)
    if sys.platform == "darwin":
        return _darwin_started_ms(pid)
    return None
