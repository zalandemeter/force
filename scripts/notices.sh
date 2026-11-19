#!/usr/bin/env bash
# Writes THIRD-PARTY-NOTICES.md: the licence texts of everything compiled into Force's binaries (the Rust crates of
# crates/sidebar, crates/intro and crates/link, via cargo-about and about.toml) and of the third-party material shipped with it. Run it
# after changing a Cargo.lock; deploy.sh and the release build ship the file next to LICENSE.
# Needs cargo-about (cargo install --locked --features cli cargo-about).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/THIRD-PARTY-NOTICES.md"
export PATH="$HOME/.cargo/bin:$PATH"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
{
  echo "# Third-party notices"
  echo
  echo "Force is MIT-licensed (\`LICENSE\`). It ships the following third-party material."
  echo
  echo "## Not code"
  echo
  echo "| What | Licence |"
  echo "|---|---|"
  echo "| \`crates/intro/assets/intro.film\`: the startup film, rendered from 3D models by Quaternius and Poly Haven (\`crates/intro/ASSETS.md\`) | CC0 1.0 (public domain) |"
  echo "| \`crates/intro/assets/force.mask\`: the word FORCE drawn in Archivo Black, Copyright 2017 The Archivo Black Project Authors | SIL Open Font License 1.1 (\`crates/intro/assets/ArchivoBlack-OFL.txt\`) |"
  echo
  for crate in crates/sidebar crates/intro crates/link; do
    echo "## Rust crates compiled into \`$crate\`"
    echo
    cargo about generate --config "$REPO/about.toml" --manifest-path "$REPO/$crate/Cargo.toml" --locked \
      "$REPO/scripts/about.hbs"
  done
} > "$tmp"
mv "$tmp" "$OUT"
trap - EXIT
echo "wrote $OUT ($(wc -l < "$OUT") lines)"
