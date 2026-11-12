# Force: a guide for agents

Force keeps coding-agent sessions (Claude Code, OpenCode) and shells running in tmux on this machine, one
session per name, and shows them in a sidebar. You can drive any of them from a shell with the commands below.
Run them as the user; never use tmux directly on Force's sockets.

## See what is running

    force list --json              # every session: name, state, activity, kind, cwd, model, limits, ...
    force explain NAME             # why a session shows its state, and its last screen rows
    force read NAME [--lines N]    # the session's screen as text (default 80 rows)

States: `needs-you` (a permission prompt or question waits), `working`, `your-turn` (finished, not looked at
yet), `idle`, `stale` (idle for hours), `starting`, `down`, `shell` (a shell at its prompt).

## Talk to a session

    force prompt NAME TEXT...      # type TEXT into the agent and press Enter ("-" reads stdin)
    force prompt NAME TEXT... --wait [--until S]... [--timeout SEC] [--json]
                                   # ... then wait until it is your-turn, idle or needs-you (prints the state)
    force wait NAME [--until S]... [--timeout SEC]
    force keys NAME KEY...         # keys: enter esc tab space up down left right backspace shift+tab ctrl+c
    force keys NAME --literal TEXT # type text without pressing Enter

Use `HOST:NAME` (or `--host HOST`) for a session on another host: a name from `force host list`, or a host
whose Force shows this one. The receiving host must list this one in its `acceptPromptsFrom` setting. A message
arrives there with a `[from SESSION on HOST · reply: force prompt HOST:SESSION "…"]` line above it; reply with
exactly that command. To a host that only watches this one, only `force prompt` (without `--wait`) works; the message
is queued and delivered when that host connects.

`prompt` refuses a session that needs you: read its screen (`force read`), answer with `force keys`.
A timeout does not prove nothing was sent: `force read` before you retry.

## Manage sessions

    force new [NAME] [--cwd DIR] [--kind claude|opencode|shell] [--no-switch]
                                   # no NAME: named after its first prompt (prints the name it picked)
    force rename OLD NEW
    force restart NAME             # or --all
    force move NAME DIR            # continue the conversation in another folder
    force archive NAME             # stop it and hide it; force unarchive NAME brings it back
    force delete NAME --yes        # remove it for good (the agent's own transcript stays)
    force kinds                    # which agents are installed here

## Report from a script in a shell session

    force status working --summary "building the image"
    force status needs-you --summary "approve the deploy?"
    force status your-turn         # done; holds until someone looks at the session
    force status idle              # clear it

Without a report, a shell session shows `working` while a program runs and `your-turn` when it ends.

## Settings and notifications

    force settings                 # show; force settings set KEY VALUE; force settings unset KEY
    force settings set notifyCommand 'notify-send Force "$FORCE_MESSAGE"'
    force notify-test [needs-you|finished|down]

`notifyCommand` runs through `sh -c` when a session needs you, finishes or stops, with FORCE_EVENT,
FORCE_SESSION, FORCE_KIND, FORCE_HOST, FORCE_STATE, FORCE_MESSAGE and FORCE_VIEWED set.

## Other machines

    force host list                # hosts this Force can show
    force host add NAME [SSH]      # add one reachable with ssh (installs Force there)

Session commands run on the host the terminal currently shows.
