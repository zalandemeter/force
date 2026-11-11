# Contributing to Force

Thanks for wanting to help. Bug reports and ideas go through the
[issue forms](https://github.com/zalandemeter/force/issues/new/choose). A security problem goes by email to
hello@useforce.cc, never into a public issue or pull request ([SECURITY.md](.github/SECURITY.md)). For code, open a
pull request against `main` ([below](#how-a-change-gets-in)).

## How a change gets in

1. **Fork** the repository on GitHub (the Fork button) and clone your fork. Maintainers branch in the repository
   itself; nobody pushes to `main`, which only takes merged pull requests.
2. **Branch** from `main`, named `<type>/<short-slug>`: `fix/sidebar-flicker`, `feat/notify-on-crash`.
3. **Commit** as often as you like, each message a [Conventional Commit](https://www.conventionalcommits.org):
   `<type>(<optional scope>): <what changes, in the imperative>`, such as `fix(sidebar): stop the flicker on resize`.
4. **Try it**: the tests, the sandbox (`./dev.sh`), and for installer changes `scripts/try-release.sh` (below).
5. **Open a pull request** against `main`, its title a Conventional Commit too: it becomes the one commit on `main`.
   CI must pass: the tests on Python 3.11 to 3.13, the Rust crates, the website build and the title check.
6. **The maintainer tries it.** Features are used before they are merged: the maintainer installs the branch build
   on a real machine (`scripts/install-build.sh HOST`, below) and gives feedback. Changes go onto the same branch,
   as more commits, until the feature is right.
7. **Squash merge.** The maintainer reviews the pull request and merges it as one commit, or asks for changes.
   Agents that work on Force with the maintainer's access ([CLAUDE.md](CLAUDE.md)) never merge: they stop at the
   pull request.

| Type | For |
|---|---|
| `feat` | something new for users |
| `fix` | a bug fix |
| `docs` | documentation and the website text |
| `refactor` | a code change that changes no behaviour |
| `perf` | faster or lighter |
| `test` | tests only |
| `build` | the release build, the installer, the dev image |
| `ci` | the workflows |
| `chore` | anything else (dependencies, housekeeping) |

A breaking change adds `!` after the type (`feat!: …`) and says what breaks in the pull request.

## Contribute with Chuck

Chuck, Force's assistant (<kbd>?</kbd> inside Force, or `force support`), knows this repository and this flow. Ask
him to fix an issue and he forks nothing on his own: he clones your fork once, into
`~/.local/share/force/support/src`, and reuses that clone for every fix after; each fix gets a branch `fix/N-slug`
from upstream `main`. He writes the fix and a
test, runs the tests in the development image and tries the change in the sandbox, shows you the diff, pushes after
you say yes, and gives you the pre-filled pull request to open. It runs on your own agent and plan, and every step
is optional.

## The repository

| Path | What it is |
|---|---|
| `force/` | the app: the Python package behind the `force` command, the daemon and the cockpit |
| `crates/sidebar/` | Rust: the sidebar, the header and the popup dialogs (`force-sidebar`, `force-header`, `force-dialog`) |
| `crates/intro/` | Rust: the startup intro (`force-intro`) and the sources its film is made from |
| `share/` | files the app installs: tmux configs, helper scripts, the installer template, the agent guide (`skill.md`) |
| `systemd/` | the user units |
| `tests/` | the test suite (pytest) |
| `build/`, `scripts/` | the release build (the builder container, `build-release.sh`, the installer filler, the notices generator), the development image (`build/dev/`, `devbox.sh`) and `try-release.sh` |
| `dev.sh`, `deploy.sh`, `install.sh` | the sandbox, and installing from the checkout |
| `website/` | https://useforce.cc: the landing page, the docs page and the tools that build them ([website/README.md](website/README.md)) |
| `docs/` | the design ([SPEC.md](docs/SPEC.md), cited in the code as `SPEC §N`) and [OPERATING.md](docs/OPERATING.md) |
| `.github/`, `.devcontainer/` | CI, release, website and image workflows, issue forms; the dev container |

## Working on it

You need Python 3.11+, tmux, git and Rust (stable), or only Docker:

```sh
scripts/devbox.sh                          # a shell in the development image, in this checkout
scripts/devbox.sh python3 -m pytest -q     # or any command in it
```

The image (`ghcr.io/zalandemeter/force-dev`, built from `build/dev/Dockerfile`) has everything below; the
container runs as you, on your checkout. `.devcontainer/` opens the same image in VS Code or Codespaces.

CI builds the website on every pull request too (the built site is attached to the run as `website-preview`). A
merge to `main` is the only thing that deploys: users get code only from tagged releases, and the website deploys
from `main`.

### Try your change in the sandbox

`./dev.sh` runs your checkout as a second, separate Force: the dev instance. It has its own data, settings and tmux
servers, and no background services. If Force is installed on the machine, that Force keeps running untouched.

```sh
./dev.sh                   # tests, build, deploy the checkout into the sandbox; again after every change
force-dev                  # open it (force-dev new, force-dev list, … work like force)
./dev.sh stop              # stop its tmux servers and daemon
./dev.sh reset             # stop it and delete the sandbox
```

### Try a release before it exists

```sh
scripts/try-release.sh            # build the release tarball, install it with the real install.sh in a fresh
                                  # ubuntu:24.04 container as a new user, and run checks
scripts/try-release.sh --shell    # then a shell in that container, to try it by hand
scripts/try-release.sh --image debian:12
```

Nothing is published and nothing on your machine changes; it needs Docker.

### Your own install

`./install.sh` installs Force from this checkout as your everyday Force (test venv, then `./deploy.sh` and
setup), and `./deploy.sh` updates it after a change. That is your live install: try changes in the sandbox
first. Running sessions keep running through a deploy, and `./deploy.sh --rollback` goes back one release.

Tests:

```sh
python3 -m pytest -q                                              # ~2 minutes; tests never touch your live Force
cargo test --manifest-path crates/sidebar/Cargo.toml
cargo test --manifest-path crates/intro/Cargo.toml
python3 website/tools/build.py /tmp/site                         # the website builds
```

Tests run their own tmux servers (sockets named `forcetest-*`) and never use the live ones.

After a change to a `Cargo.lock`, regenerate `THIRD-PARTY-NOTICES.md` with `scripts/notices.sh`
(`tests/test_notices.py` fails until you do).

## Releases

A merge is not a release. `main` collects features, and the maintainer decides when they ship, often after several
of them. Only the maintainer starts a release; an agent working on Force never does. Then:

1. Try it: `scripts/try-release.sh`.
2. Set the version in `pyproject.toml` (plain `X.Y.Z`) in a pull request (`chore: release X.Y.Z`) and merge it.
3. Tag it and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The release workflow checks that the tag matches `pyproject.toml`, runs the tests, builds the tarball in the
builder container (static binaries, Force's own tmux), attests its provenance, publishes the GitHub Release with
the tarball, its `.sha256` and `install.sh`, and redeploys the website so that
`curl -fsSL https://useforce.cc/install.sh | sh` installs the new version. Installed copies find it with
`force update` and in their daily check.

To build a release locally: `scripts/build-release.sh [X.Y.Z]` (needs Docker; output in `build/out/dist`).

### Trying a branch on a real machine

```sh
scripts/install-build.sh HOST          # build this checkout and install it on HOST (ssh), like force update
scripts/install-build.sh HOST --back   # back to the newest published release installed there
```

The build goes beside HOST's releases as `<version>-<branch>-<commit>` and becomes the live one there; sessions keep
running. Its version stays the last published one, so the next `force update` replaces it.
