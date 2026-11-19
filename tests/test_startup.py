"""`force` starts fast (SPEC §8): a command imports only what it uses. Deterministic: which modules a fresh
interpreter has loaded, never a timing."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# loaded only by the commands that need them (the network, OpenCode's API, the automation commands)
HEAVY = ["http.client", "ssl", "urllib.request", "email.parser", "force.opencode", "force.automation",
         "force.messages", "force.mcp", "force.net", "force.api", "force.daemon", "force.kinds", "force.doctor"]


def _loaded(code: str) -> set[str]:
    p = subprocess.run([sys.executable, "-I", "-c",
                        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{code}\n"
                        "import json; print(json.dumps(sorted(sys.modules)))"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return set(json.loads(p.stdout.splitlines()[-1]))


def test_import_cli_loads_no_heavy_module():
    loaded = _loaded("import force.cli")
    assert not loaded & set(HEAVY), sorted(loaded & set(HEAVY))


def test_the_parser_of_a_command_loads_no_heavy_module():
    """build_parser runs on every call: neither it nor the update check's version lookup may load urllib."""
    loaded = _loaded("from force import cli\ncli.build_parser(only=cli._command_word(['list', '--json']))\n"
                     "cli.build_parser()")
    assert not loaded & set(HEAVY), sorted(loaded & set(HEAVY))


def test_lazy_names_still_reachable():
    """cli.opencode, cli.automation, … load on first use (tests and callers patch them there)."""
    from force import automation, cli, opencode, services, share
    assert cli.opencode is opencode and cli.automation is automation
    assert cli.services is services and cli.share is share
    import getpass
    assert cli.getpass is getpass
    assert automation.CmdError is cli.CmdError and automation.UsageError is cli.UsageError


def test_only_the_named_subcommand_is_built():
    from force import cli
    assert cli._command_word([]) == ""
    assert cli._command_word(["--version"]) == ""
    assert cli._command_word(["--remote", "devbox"]) == ""
    assert cli._command_word(["--glyphs", "rich", "attach", "x"]) == "attach"
    assert cli._command_word(["list", "--json"]) == "list"
    assert cli._command_word(["-h"]) is None and cli._command_word(["--", "x"]) is None
    p = cli.build_parser(only="list")
    assert p.only_found
    assert vars(p.parse_args(["list", "--json"])) == vars(cli.build_parser().parse_args(["list", "--json"]))
    assert not cli.build_parser(only="nosuch").only_found
    for argv in (["dialog", "rename", "x"], ["support", "search", "a", "b"], ["net", "hint", "devbox", "off"],
                 ["switch", "--index", "2", "--client", "c"], ["--remote", "devbox"], []):
        p = cli.build_parser(only=cli._command_word(argv))
        assert p.only_found, argv
        assert vars(p.parse_args(argv)) == vars(cli.build_parser().parse_args(argv)), argv


def test_unknown_command_reads_as_before(capsys):
    """An unknown command falls back to the whole parser: the same error, every command listed."""
    import pytest
    from force import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["bogus"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    # the wording differs between Python versions (quoted choices or not); every command is listed in each
    assert "invalid choice" in err and "bogus" in err
    assert all(c in err for c in ("list", "dialog", "nested-report", "support"))


def test_the_dialogs_forward_the_same_commands():
    """force-dialog sends forwarded commands straight through force-link (SPEC §40): its list is cli._FORWARDED."""
    import re
    from force import cli
    src = (ROOT / "crates" / "sidebar" / "src" / "dialog.rs").read_text()
    body = re.search(r"pub const FORWARDED: &\[&str\] = &\[(.*?)\];", src, re.S).group(1)
    assert set(re.findall(r'"([^"]+)"', body)) == cli._FORWARDED
