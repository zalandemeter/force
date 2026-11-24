# Force — brag video plan

**What it is:** Force keeps your AI coding agent sessions running and shows them all in one terminal.
**For:** developers who run several Claude Code / OpenCode sessions at once, on a laptop or a server.
**Sets it apart:** one window with a sidebar that says which agent needs you; sessions survive closed terminals; one-line install.
**Wink:** taking a seat: you sit back while the agents work, and Force tells you who needs you.
**Visual hook:** the intro's own film (a suited man walking to a leather chair, drawn in terminal half-blocks).
**Real UI shown:** the cockpit (real sidebar/header/agent, staged with invented sessions), a real permission prompt, the install line.
**Tone:** deadpan cinematic. Played straight, lounge-slow music, the comedy is the chair.
**Share caption:** "Seven coding agents, one terminal, one leather chair. Force keeps them running and tells you which one needs you."

## Storyboard (1920×1080, 30 fps; recut 2026-11 to 23.3 s: the screens of Force 1.10, mobile turning blue in scene 5, and scene 8)

| # | time | scene | on screen | motion | sound |
|---|---|---|---|---|---|
| 1 | 0.0–2.6 | Hook | Terminal windows pile up on a #262626 desk, each a different agent session; caption: "Seven agents. Seven terminals." | windows drop in one by one (7 × 0.25 s), slight overlap | music starts: brushed drums, upright bass |
| 2 | 2.6–5.0 | Hook, part 2 | caption replaces: "Which one needs you?" a red "!" blinks in one buried window | windows jitter; the red one pulses | soft tick on the blink |
| 3 | 5.0–8.2 | Reveal | windows collapse into the Force cockpit (real); the FORCE wordmark in the sidebar; caption: "Force: every agent in one terminal." | windows fly to the sidebar cards, cockpit fades up underneath | low warm thump at the collapse |
| 4 | 8.2–11.8 | Highlight 1 | header pill "! billing-fix · permission" pulses; cursor clicks it; main pane switches to the real permission prompt; caption: "It tells you who needs you." | cursor glide + click, pane swap (dip through background) | soft click in key |
| 5 | 11.8–14.8 | Highlight 2 | terminal window closes (shrinks to nothing); a strip of cards keeps spinning ("working", "working"); caption: "Close the terminal. They keep working." | window collapses, cards stay alive, then reopen | gentle swish |
| 6 | 14.8–17.4 | Punchline | the film: the gentleman sits down in the chair; caption: "Sit back." | film plays at 1×, pixel-crisp | music swells a little |
| 7 | 17.4–20.0 | Outro | FORCE wordmark + "The code cockpit"; install line types: `curl -fsSL https://useforce.cc/install.sh \| sh`; then "force" | wordmark settles, typing at ~40 chars/s, cursor blink | music resolves |
| 8 | 19.0–23.3 | Chuck | back on the api screen: the cursor clicks "ask chuck" under the list; Chuck's card opens (what he can do); caption: "Stuck? Ask Chuck."; the card closes and the window stays, as the page shows it | cursor glide + click, card scales in, Esc | — |

Every caption holds ≥ 0.3 s per word after it is fully in. Captions in Archivo 800 (wdth 112), white on the ground; terminal text in DejaVu Sans Mono.

## Build
HTML page drawn as a pure function of time (`render(t)`), frames captured with headless Chromium, encoded with ffmpeg.
Music: MusicGen-small locally ("deadpan lounge jazz, brushed drums, upright bass, warm Rhodes, 72 bpm"); a few soft
effects synthesized in the same key, mixed under the music.
