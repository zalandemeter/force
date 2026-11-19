"""The command errors `force` turns into exit codes (cli.main). Here, not in automation.py, so that catching them
does not import the automation commands (and what they import) on every `force` call."""
from __future__ import annotations


class CmdError(Exception):
    """A command failed: (code, message). Printed as `force: MESSAGE` (or an error object with --json)
    and the process exits 1."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UsageError(Exception):
    """Bad usage: printed to stderr, process exits 2."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
