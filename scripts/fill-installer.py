#!/usr/bin/env python3
"""Fill the @VERSION@, @BASE_URL@, @RELEASES_URL@, @SHA_x86_64@, @SHA_aarch64@, @SHA_darwin_arm64@ placeholders in
share/get-force.sh into a standalone, executable install.sh.

Used by scripts/build-release.sh (for the published <OUT>/install.sh) and by the tests (against an install.sh
built on the fly from the working tree).

Usage: fill-installer.py <template> <out> <version> <base_url> <releases_url> <sha_x86_64> <sha_aarch64>
                         [<sha_darwin_arm64>]       (an empty SHA = "no build yet" for that platform)
"""
import os
import sys


def main(argv) -> int:
    if len(argv) not in (7, 8):
        print(f"usage: {sys.argv[0]} <template> <out> <version> <base_url> <releases_url> "
              "<sha_x86_64> <sha_aarch64> [<sha_darwin_arm64>]", file=sys.stderr)
        return 2
    tpl, out, version, base_url, releases_url, sha_x86_64, sha_aarch64 = argv[:7]
    sha_darwin_arm64 = argv[7] if len(argv) == 8 else ""
    text = open(tpl).read()
    text = (text.replace("@VERSION@", version)
               .replace("@BASE_URL@", base_url)
               .replace("@RELEASES_URL@", releases_url)
               .replace("@SHA_x86_64@", sha_x86_64)
               .replace("@SHA_aarch64@", sha_aarch64)
               .replace("@SHA_darwin_arm64@", sha_darwin_arm64))
    if ("@VERSION@" in text or "@BASE_URL@" in text or "@RELEASES_URL@" in text
            or "@SHA_x86_64@" in text or "@SHA_aarch64@" in text or "@SHA_darwin_arm64@" in text):
        print("fill-installer: an @...@ placeholder survived the fill", file=sys.stderr)
        return 2
    with open(out, "w") as f:
        f.write(text)
    os.chmod(out, 0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
