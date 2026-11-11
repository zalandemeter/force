# CLAUDE.md

Instructions for agents working on Force. Read the documents below instead of guessing; this file only points to
them and adds the rules that apply to agents.

| Read | For |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | how a change gets in (branches, Conventional Commits, pull requests, who merges, releases), the repository layout, the sandbox, the tests, the dev image |
| [docs/SPEC.md](docs/SPEC.md) | the design; the code cites it as `SPEC §N`. A change of behaviour changes the SPEC first |
| [share/support/architecture.md](share/support/architecture.md) | a one-page map of the code: processes, modules, where state lives |
| [website/README.md](website/README.md) | the website (useforce.cc) |
| [share/support/CHUCK.md](share/support/CHUCK.md) | Chuck's brief: keep it true when what he can do changes |

## Rules for agents

1. **Never merge, tag or release.** Work on a `<type>/<slug>` branch, open the pull request, and stop there: the
   maintainer tries the branch, gives feedback (more commits on the same branch), and alone decides when it is
   merged and, separately, when `main` is released (CONTRIBUTING.md, "How a change gets in" and "Releases"). A goal
   like "finish the feature" does not change this. To let the maintainer try a branch on a machine of theirs, run
   `scripts/install-build.sh HOST` only when they ask for it.
2. **Never touch a live install.** Try changes in the sandbox (`./dev.sh`, `force-dev`); tests use their own tmux
   servers, never the sockets `force` or `force-ui`.
3. **This repository is public.** No internal host names, people's names or e-mail addresses, private paths,
   tokens or keys in files, commits or pull requests.
