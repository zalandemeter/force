#!/bin/bash
# The cockpit key help, shown in a popup until any key is pressed (bash: dash has no read -n).
printf '\033[?25l'                 # no blinking cursor on the card
cat "$(dirname "$0")/help.ansi"
read -rsn1
