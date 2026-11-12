"""THIRD-PARTY-NOTICES.md lists every crate compiled into the shipped binaries (scripts/notices.sh regenerates it)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CARGO = shutil.which("cargo") or os.path.expanduser("~/.cargo/bin/cargo")


def _shipped_crates(crate_dir: Path) -> set[str]:
    """`name version` of every crate built into the binary for Linux and macOS (normal and build deps with the features
    actually enabled: cargo tree, as cargo-about resolves them; not cargo metadata, which lists more)."""
    out = set()
    for target in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "aarch64-apple-darwin"):
        lines = subprocess.run(
            [CARGO, "tree", "-e", "normal,build", "--target", target, "--prefix", "none", "--format", "{p}",
             "--locked", "--offline"],
            cwd=crate_dir, capture_output=True, text=True, check=True, timeout=120).stdout.splitlines()
        for line in lines[1:]:                                   # the first line is the crate itself
            name, version = line.split()[:2]
            out.add(f"{name} {version.lstrip('v')}")
    return out


@pytest.mark.skipif(not os.path.exists(CARGO), reason="no cargo")
@pytest.mark.parametrize("crate", ["crates/sidebar", "crates/intro"])
def test_notices_cover_every_shipped_crate(crate):
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
    shipped = _shipped_crates(ROOT / crate)
    assert len(shipped) > 20                                     # the check itself works
    missing = sorted(c for c in shipped if f"`{c}`" not in notices)
    assert not missing, f"run scripts/notices.sh: {missing}"


def test_licence_files_ship():
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    deploy = (ROOT / "deploy.sh").read_text()
    assert '"$REPO/LICENSE" "$REPO/THIRD-PARTY-NOTICES.md"' in deploy
