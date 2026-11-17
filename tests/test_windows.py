"""share/windows/force-wsl (SPEC §55): what force.cmd runs inside WSL. A login shell, then the real launcher with the
arguments exactly as given (spaces and quotes included)."""
import stat
import subprocess
from pathlib import Path

RUN = Path(__file__).resolve().parent.parent / "share" / "windows" / "force-wsl"


def test_it_is_executable_and_a_login_shell():
    assert RUN.stat().st_mode & stat.S_IXUSR
    assert RUN.read_text().splitlines()[0] == "#!/bin/bash -l"


def test_it_runs_the_launcher_with_the_arguments_as_given(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    launcher = home / ".local" / "bin" / "force"
    launcher.write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\necho "PATH=$PATH"\n')
    launcher.chmod(0o755)
    (home / ".profile").write_text('PATH="$HOME/profile-bin:$PATH"\n')   # a login shell reads it
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([str(RUN), "new", "my session", 'say "hi"', ""], env=env, capture_output=True, text=True,
                       timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[:4] == ["[new]", "[my session]", '[say "hi"]', "[]"]
    assert f"{home}/profile-bin" in r.stdout.splitlines()[4]


def test_a_missing_launcher_fails(tmp_path):
    r = subprocess.run([str(RUN), "list"], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode in (126, 127)            # 127 from bash 4+ (WSL's); macOS's bash 3.2 says 126


def test_install_ps1_gives_the_previous_distro_its_profile_back():
    # the "Force" profile follows force.cmd (share/fonts.sh); moving it to another distro rewrites the previous
    # distro's "Force (<distro>)" with that distro's own `force fonts` (SPEC §45)
    ps1 = (RUN.parent.parent.parent / "website" / "root" / "install.ps1").read_text()
    read, write, restore = (ps1.index("$previous = "), ps1.index("Set-Content -Encoding Ascii -Path $shim"),
                            ps1.index("& wsl.exe -d $previous --exec sh -lc"))
    assert read < write < restore, "read before force.cmd is rewritten, restored after the install"
    assert "exec ~/.local/bin/force fonts" in ps1[restore:restore + 120]
