"""Tests for force/procinfo.py (SPEC §51): fake /proc trees and the real system."""
import ctypes
import os
import struct
import sys
import time
from pathlib import Path

import pytest

from force import procinfo
from force.procinfo import _BsdInfo, _bsd_fields, _parse_procargs2


def _write_stat(proc, pid, ppid, comm):
    p = proc / str(pid)
    p.mkdir(parents=True)
    (p / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0")


# --- fake /proc tree ---------------------------------------------------------
def test_pids_ignores_non_digit(tmp_path):
    proc = tmp_path / "proc"
    (proc / "300").mkdir(parents=True)
    (proc / "100").mkdir()
    (proc / "abc").mkdir()
    assert procinfo.pids(proc) == [100, 300]


def test_ppid(tmp_path):
    proc = tmp_path / "proc"
    _write_stat(proc, 100, 1, "(a) b)")
    assert procinfo.ppid(100, proc) == 1
    assert procinfo.ppid(999, proc) is None
    (proc / "1").mkdir()          # no stat -> None
    assert procinfo.ppid(1, proc) is None


def test_children_map_tricky_comm(tmp_path):
    proc = tmp_path / "proc"
    _write_stat(proc, 100, 1, "(bash)")
    _write_stat(proc, 101, 100, "(a) b)")
    _write_stat(proc, 300, 1, "weird) comm)")
    assert procinfo.children_map(proc) == {1: [100, 300], 100: [101]}


def test_children_map_skips_unreadable(tmp_path):
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)   # stat unreadable/failed -> ignored
    _write_stat(proc, 200, 1, "ok")
    assert procinfo.children_map(proc) == {1: [200]}


def test_comm(tmp_path):
    proc = tmp_path / "proc"
    p = proc / "100"
    p.mkdir(parents=True)
    (p / "comm").write_text("bash\n")
    assert procinfo.comm(100, proc) == "bash"
    (p / "comm").write_text("")
    assert procinfo.comm(100, proc) is None
    assert procinfo.comm(999, proc) is None


def test_cmdline(tmp_path):
    proc = tmp_path / "proc"
    p = proc / "100"
    p.mkdir(parents=True)
    (p / "cmdline").write_bytes(b"a\0b c\0\0")
    assert procinfo.cmdline(100, proc) == ["a", "b c"]
    (p / "cmdline").write_bytes(b"\0\0")
    assert procinfo.cmdline(100, proc) == []
    assert procinfo.cmdline(999, proc) is None


def test_exe(tmp_path):
    proc = tmp_path / "proc"
    p = proc / "100"
    p.mkdir(parents=True)
    (p / "exe").symlink_to("/usr/bin/python3")
    assert procinfo.exe(100, proc) == "/usr/bin/python3"
    (p / "exe").unlink()
    assert procinfo.exe(100, proc) is None
    target = proc / "199"
    target.mkdir()
    (target / "exe").symlink_to("/opt/claude/versions/2.1.292 (deleted)")
    assert procinfo.exe(199, proc) == "/opt/claude/versions/2.1.292"
    assert procinfo.exe(999, proc) is None


def test_uid(tmp_path):
    proc = tmp_path / "proc"
    p = proc / "100"
    p.mkdir(parents=True)
    assert procinfo.uid(100, proc) == os.stat(p).st_uid
    assert procinfo.uid(999, proc) is None


def test_missing_pid_returns_none(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    assert procinfo.ppid(1, proc) is None
    assert procinfo.comm(1, proc) is None
    assert procinfo.cmdline(1, proc) is None
    assert procinfo.exe(1, proc) is None
    assert procinfo.uid(1, proc) is None


# --- macOS pure helpers ---------------------------------------------------------
def test_parse_procargs2_basic():
    buf = struct.pack("<i", 2) + b"/usr/bin/x\0\0\0\0" + b"a\0b c\0"
    assert _parse_procargs2(buf) == ["a", "b c"]


def test_parse_procargs2_short_buffer():
    assert _parse_procargs2(b"") is None
    assert _parse_procargs2(b"\x01\x00") is None


def test_parse_procargs2_negative_argc():
    buf = struct.pack("<i", -1) + b"/bin/x\0\0"
    assert _parse_procargs2(buf) is None


def test_parse_procargs2_argc_larger_than_present():
    buf = struct.pack("<i", 5) + b"/bin/x\0\0\0\0" + b"only\0"
    assert _parse_procargs2(buf) == ["only"]


def test_bsd_info_layout():
    assert ctypes.sizeof(_BsdInfo) == 136
    raw = struct.pack("<12I16s32s5Ii2Q",
                     0, 1, 2, 3,      # flags, status, xstatus, pid
                     99, 1000, 6, 7, 8, 9, 10, 11,   # ppid, uid, gid, ruid, rgid, svuid, svgid, rfu_1
                     b"mycomm".ljust(16, b"\0"),
                     b"myname".ljust(32, b"\0"),
                     12, 13, 14, 15, 16,   # nfiles, pgid, pjobc, e_tdev, e_tpgid
                     7,                             # pbi_nice
                     12345, 987654)               # tvsec, tvusec
    info = _BsdInfo.from_buffer_copy(raw)
    ppid, uid, comm, started = _bsd_fields(info)
    assert ppid == 99
    assert uid == 1000
    assert comm == "mycomm"
    assert started == 12345 * 1000 + 987654 // 1000


def test_bsd_comm_empty_is_none():
    raw = struct.pack("<12I16s32s5Ii2Q",
                     0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                     b"\0" * 16, b"\0" * 32,
                     0, 0, 0, 0, 0, 0, 0, 0)
    info = _BsdInfo.from_buffer_copy(raw)
    assert _bsd_fields(info)[2] is None


# --- the real system (native on both Linux and macOS) ------------------------------
def test_started_ms_os_getpid():
    ms = procinfo.started_ms(os.getpid())
    assert ms is not None
    now_ms = time.time() * 1000
    assert ms <= now_ms + 5000        # not in the future (5 s slack)
    assert ms > now_ms - 24 * 3600 * 1000   # within the last 24 h


def test_started_ms_none():
    assert procinfo.started_ms(None) is None


def test_pids_contains_os_getpid():
    assert os.getpid() in procinfo.pids()


def test_ppid_os_getpid():
    assert procinfo.ppid(os.getpid()) == os.getppid()


def test_cmdline_os_getpid():
    argv = procinfo.cmdline(os.getpid())
    assert argv
    assert argv[0].endswith("python") or "python" in argv[0] or "pytest" in argv[0]


def test_exe_os_getpid_is_existing_file():
    p = procinfo.exe(os.getpid())
    assert p
    assert Path(p).is_file()


def test_comm_os_getpid():
    assert procinfo.comm(os.getpid()) is not None


def test_uid_os_getpid():
    assert procinfo.uid(os.getpid()) == os.getuid()
