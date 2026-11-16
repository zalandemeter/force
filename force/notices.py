"""When a session's state change is worth telling you about (SPEC §49.4, §54): one rule for the daemon's
`notifyCommand` and for `force api`'s `notice` events, so every frontend tells you the same moments."""
from __future__ import annotations

from typing import Callable, Iterable

from .state import State

UP = (State.WORKING, State.NEEDS_YOU, State.YOUR_TURN, State.IDLE, State.STALE, State.SHELL)
DOWN_AFTER = 10.0   # a session must stay down this long (a crash restart brings it back sooner)


class Transitions:
    """The events of a stream of session views: `needs-you` (newly NEEDS_YOU), `finished` (YOUR_TURN after WORKING
    or NEEDS_YOU), `down` (DOWN for DOWN_AFTER seconds after it was up, without a restart note, and not stopped by
    Force: `killed(name)`). Archived sessions and a session's first sight give none. `state` and `down` are plain
    dicts keyed by session name (the daemon renames their keys when a session is renamed)."""

    def __init__(self, down_after: float = DOWN_AFTER):
        self.down_after = down_after
        self.state: dict[str, State] = {}       # name -> state at the last step
        self.down: dict[str, tuple] = {}        # name -> (clock since down, state before) while down

    def step(self, views: Iterable, now: float, killed: Callable[[str], bool] | None = None) -> list[tuple]:
        """[(view, event)] for this step's views (anything with name, state, note, archived)."""
        out = []
        names = set()
        for v in views:
            if v.archived:
                continue
            names.add(v.name)
            prev = self.state.get(v.name)
            self.state[v.name] = v.state
            event = None
            if v.state is State.DOWN:
                if prev in UP and v.note is None:
                    self.down[v.name] = (now, prev)
                down = self.down.get(v.name)
                if down is not None and now - down[0] >= self.down_after:
                    self.down.pop(v.name, None)
                    if killed is None or not killed(v.name):
                        event = "down"
            else:
                self.down.pop(v.name, None)
                if prev is None or prev == v.state:
                    continue            # first sight after a start, or no change
                if v.state is State.NEEDS_YOU:
                    event = "needs-you"
                elif v.state is State.YOUR_TURN and prev in (State.WORKING, State.NEEDS_YOU):
                    event = "finished"
            if event is not None:
                out.append((v, event))
        for n in list(self.state):
            if n not in names:
                self.state.pop(n, None)
                self.down.pop(n, None)
        return out


def message(view, event: str) -> str:
    """The one-line text of an event (`FORCE_MESSAGE`, SPEC §49.4)."""
    if event == "needs-you":
        what = view.activity or ("needs permission" if view.kind != "shell" else "needs you")
    elif event == "finished":
        what = "finished"
    else:
        what = "stopped"
    return f"{view.name}: {what}"
