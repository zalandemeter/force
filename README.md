<p align="center">
  <a href="https://useforce.cc"><img src=".github/assets/banner.webp" alt="Force, the code cockpit" width="100%"></a>
</p>

<p align="center">
  <a href="https://github.com/zalandemeter/force/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/zalandemeter/force/ci.yml?branch=main&style=flat-square&label=CI" alt="CI"></a>
  <a href="https://github.com/zalandemeter/force/releases/latest"><img src="https://img.shields.io/github/v/release/zalandemeter/force?style=flat-square&color=d97757&label=release" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/platform-Linux%20x86--64-3c3c3c?style=flat-square" alt="Linux x86-64">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-3c3c3c?style=flat-square" alt="MIT licence"></a>
  <a href="https://useforce.cc"><img src="https://img.shields.io/badge/useforce.cc-website-d97757?style=flat-square" alt="Website"></a>
</p>

<p align="center">
  <b>Every coding agent you run, in one terminal.</b><br>
  Force keeps your agent sessions running on your machine or a server, through closed terminals and reboots,
  and tells you which one needs you, on your phone too.
</p>

<p align="center">
  <a href="https://useforce.cc">Website</a> ·
  <a href="https://useforce.cc/docs.html">Documentation</a> ·
  <a href="https://github.com/zalandemeter/force/issues/new?template=bug_report.yml">Report&nbsp;an&nbsp;issue</a> ·
  <a href="https://github.com/zalandemeter/force/issues/new?template=feature_request.yml">Suggest&nbsp;an&nbsp;idea</a>
</p>

<p align="center">
  <img src=".github/assets/cockpit.webp" alt="The Force cockpit: a sidebar of sessions with their states, and the selected Claude Code session on the right" width="100%">
</p>

## Install

```sh
curl -fsSL https://useforce.cc/install.sh | sh
force
```

Linux on x86-64. No root, no packages, no config files: Force installs as your user, brings its own tmux and, if
the system's Python is older than 3.11, its own Python. The installer pins the release's SHA-256 and checks it
before unpacking. Today Force runs Claude Code, OpenCode and plain shell sessions.

## What it does

- **You see who needs you.** A permission prompt or a question anywhere shows in the sidebar and as a count at the
  top. One click takes you there, and you answer it in place.
- **Close the terminal. It keeps working.** Sessions live in tmux on the machine, not in your terminal window.
  Drop the connection or restart the machine: every session comes back with its conversation, and one that
  crashes restarts by itself.
- **Every agent. Every machine.** Agents and plain shells in one list, with your Claude plan's five-hour and
  weekly limits under it. <code>force&nbsp;host&nbsp;add</code> puts a server's sessions one click away from your laptop's.
- **It names itself.** Start a session without a name and Force names it after your first message.
- **Scripts and agents drive it.** <code>force&nbsp;prompt</code>, `wait` and `read` hand work to a session and collect the
  answer. <code>force&nbsp;skill</code> is the guide an agent reads to run Force itself.
- **Nothing to set up.** Force paints its own theme in any terminal or ssh login.
- **Ask Chuck.** Force's assistant (<kbd>?</kbd> in the menu) answers how things work, sets things up for you,
  writes bug reports and ideas you submit with one click, and can build the fix. He runs on your own agent.

### Your turn, on your phone too

A session that finishes while you are elsewhere waits for you, marked *your turn*. Give Force one command and it
runs it when a session needs you or finishes, so a push notification reaches you wherever you are:

```sh
force settings set notifyCommand 'curl -d "$FORCE_MESSAGE" ntfy.sh/my-force'
```

Force fits a phone's terminal too: open it over ssh and the sidebar becomes the whole screen.

<p align="center">
  <img src=".github/assets/phone.webp" alt="Force at phone width: the session list with one session needing permission" width="300">
</p>

## The commands you will use

- <code>force</code>  
  Open the cockpit.
- <code>force&nbsp;new&nbsp;[NAME]</code>  
  Start a session in the current folder; without a name it is named after your first message.
- <code>force&nbsp;import</code>  
  List agent sessions Force doesn't manage yet; <code>force&nbsp;import&nbsp;ID</code> takes one over.
- <code>force&nbsp;host&nbsp;add&nbsp;NAME</code>  
  Add another machine you reach with ssh.
- <code>force&nbsp;prompt&nbsp;NAME&nbsp;TEXT&nbsp;--wait</code>  
  Send a session a prompt from a script or another agent and wait for its answer.
- <code>force&nbsp;support</code>  
  Ask Chuck, Force's assistant: questions, setup, bug reports and ideas, fixes.
- <code>force&nbsp;settings</code>  
  Show and change settings, such as the command that notifies you.
- <code>force&nbsp;update</code>  
  Install the newest release. Sessions keep running.
- <code>force&nbsp;doctor&nbsp;--fix</code>  
  Check the setup and repair what it can.
- <code>force&nbsp;uninstall</code>  
  Remove Force; your conversations stay.

Everything else is in the menu (<kbd>Ctrl</kbd>&#8288;+&#8288;<kbd>Space</kbd> inside Force) and in the
[documentation](https://useforce.cc/docs.html).

## Contributing

Bug reports and ideas are welcome: use the [issue forms](https://github.com/zalandemeter/force/issues/new/choose),
or let Chuck (<kbd>?</kbd> inside Force) check for duplicates and draft them. To work on Force itself, see
[CONTRIBUTING.md](CONTRIBUTING.md): Chuck can build a fix with you there too; the design is written down in
[docs/SPEC.md](docs/SPEC.md). Found a security problem? Email hello@useforce.cc instead of opening an issue ([SECURITY.md](.github/SECURITY.md)).

## Licence

[MIT](LICENSE): provided as is, without warranty of any kind. The third-party code and fonts Force ships with, and
their licences, are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Force is an independent project, not affiliated with Anthropic or the makers of OpenCode. Claude and Claude Code
are trademarks of Anthropic, PBC.
