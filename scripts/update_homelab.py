"""Pin both Earwitness services to one tested image in homelab."""

import re
import sys
from pathlib import Path


def update_compose(contents: str, digest: str, revision: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected a SHA-256 image digest")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected a full Git commit SHA")
    for service in ("web", "worker"):
        pattern = (
            rf"(?m)^  {service}:\n"
            r"(?:    # Source: https://github\.com/hearsay-tools/earwitness/commit/[0-9a-f]{40}\n)?"
            r"    image: ghcr\.io/hearsay-tools/earwitness(?::latest|@sha256:[0-9a-f]{64})\n"
        )
        replacement = (
            f"  {service}:\n"
            f"    # Source: https://github.com/hearsay-tools/earwitness/commit/{revision}\n"
            f"    image: ghcr.io/hearsay-tools/earwitness@{digest}\n"
        )
        contents, count = re.subn(pattern, replacement, contents)
        if count != 1:
            raise ValueError(f"Expected exactly one recognized {service} image")
    return contents


def main() -> None:
    checkout, digest, revision = sys.argv[1:]
    path = Path(checkout) / "stacks/earwitness/compose.yaml"
    path.write_text(update_compose(path.read_text(), digest, revision))


if __name__ == "__main__":
    main()
