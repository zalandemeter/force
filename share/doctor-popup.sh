#!/bin/bash
# `force doctor --fix` in a popup; stays open until a key is pressed.
printf '\033[?25l'                 # no blinking cursor on the card
"${FORCE_BIN:-$HOME/.local/bin/force}" doctor --fix --card
read -rsn1
