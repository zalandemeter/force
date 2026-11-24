# Force — the code cockpit

Every coding agent you run, in one terminal.

Force keeps your Claude Code and OpenCode sessions running on your machine or a server, through closed
terminals and reboots, and tells you which one needs you, on your phone too.

```sh
curl -fsSL https://useforce.cc/install.sh | sh
force
```

On Windows, in PowerShell:

```powershell
irm https://useforce.cc/install.ps1 | iex
force
```

Linux (x86-64), macOS on Apple Silicon, and Windows 10 and 11 (Force runs inside WSL; `force` works in PowerShell,
cmd and Windows Terminal). No root, no packages, no config files.

## What it does

- **You see who needs you.** A permission prompt or a question anywhere shows in the sidebar and as a count
  at the top. One click takes you there, and you answer it in place. A session that is still working but has
  written you a finding turns blue until you read it.
- **Close the terminal. It keeps working.** Sessions live in tmux on the machine, not in your terminal
  window. Drop the connection or restart the machine: every session comes back with its conversation, and
  one that crashes restarts by itself.
- **Your turn, on your phone too.** A session that finishes while you are elsewhere waits for you, marked
  your turn. Give Force one command and it runs it when a session needs you or finishes: ntfy, your own
  push server, a desktop notification.
- **Every agent. Every machine.** Claude Code, OpenCode and plain shells in one list, with your Claude plan's
  five-hour and weekly limits under it. `force host add` puts a server's sessions one click away from your
  laptop's, over a direct link when the network allows and ssh otherwise. An agent on one machine can hand work
  to a session on another.
- **Stuck? Ask Chuck.** Chuck is Force's own assistant (`?` in the menu, or `force support`). He answers from the
  docs and the code of the version you run, sets up a host or a notification once you approve, and turns a bug
  into a ready report, or into a fix and its pull request. He runs on your own Claude Code or OpenCode.
- **It names itself.** Start a session without a name and Force names it after your first message.
- **Scripts and agents drive it.** `force prompt`, `wait` and `read` hand work to a session and collect the
  answer. `force skill` is the guide an agent reads to run Force itself.
- **Nothing to set up.** Force brings its own tmux and paints its own theme in any terminal or ssh login.
- **It keeps itself current.** Update from the menu, on every machine at once, or let `autoUpdate` do it.
  Sessions keep running; each agent moves to its newest version once it is idle.

## The commands you will use

| Command | What it does |
|---|---|
| `force` | Open the cockpit. |
| `force new [NAME]` | Start a session in the current folder; without a name it is named after your first message. |
| `force import` | List sessions Force doesn't manage yet; `force import ID` takes one over. |
| `force host add NAME` | Add another machine you reach with ssh. |
| `force prompt NAME TEXT --wait` | Send a session, here or on another machine (`HOST:NAME`), a prompt from a script or another agent and wait for its answer. |
| `force support` | Ask Chuck, Force's assistant: how something works, a setup, a bug report. |
| `force status working` | From a script in a shell session: show what it is doing on its card. |
| `force settings` | Show and change settings, such as the command that notifies you. |
| `force update` | Install the newest release. Sessions keep running. |
| `force uninstall` | Remove Force; your conversations stay. |

Everything else is in the menu (`Ctrl`+`Space` inside Force) and in the documentation:
[docs.md](https://useforce.cc/docs.md) (Markdown) or [docs.html](https://useforce.cc/docs.html).

{{source}}

Open source under the [MIT licence](https://useforce.cc/LICENSE.txt): provided as is, without warranty of
any kind, and used at your own risk; its author is not liable for any damage or loss from using it. An independent project, not affiliated with Anthropic or the makers of
OpenCode. Claude and Claude Code are trademarks of Anthropic, PBC.

Privacy: no cookies, no analytics, no telemetry; details in [privacy.md](https://useforce.cc/privacy.md).
Contact: hello@useforce.cc.
