"""Pin both Earwitness services to one tested image in homelab."""

import os
import re
import sys
from pathlib import Path


def update_compose(
    contents: str,
    digest: str,
    revision: str,
    *,
    image: str = "ghcr.io/hearsay-tools/earwitness",
    source: str = "hearsay-tools/earwitness",
) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*", image) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source
    ):
        raise ValueError("Invalid image or source repository")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected a SHA-256 image digest")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected a full Git commit SHA")
    for service in ("web", "worker"):
        pattern = (
            rf"(?m)^  {service}:\n"
            rf"(?:    # Source: https://github\.com/{re.escape(source)}/commit/[0-9a-f]{{40}}\n)?"
            rf"    image: {re.escape(image)}(?::latest|@sha256:[0-9a-f]{{64}})\n"
        )
        replacement = (
            f"  {service}:\n"
            f"    # Source: https://github.com/{source}/commit/{revision}\n"
            f"    image: {image}@{digest}\n"
        )
        contents, count = re.subn(pattern, replacement, contents)
        if count != 1:
            raise ValueError(f"Expected exactly one recognized {service} image")
    return contents


def main() -> None:
    checkout, digest, revision = sys.argv[1:]
    relative = Path(
        os.environ.get("DEPLOY_COMPOSE_PATH", "stacks/earwitness/compose.yaml")
    )
    path = (Path(checkout) / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(Path(checkout).resolve())
    ):
        raise ValueError("Compose path must stay inside the deployment checkout")
    source = os.environ.get("SOURCE_REPOSITORY", "hearsay-tools/earwitness")
    image = os.environ.get("IMAGE_REPOSITORY", f"ghcr.io/{source.lower()}")
    path.write_text(
        update_compose(path.read_text(), digest, revision, image=image, source=source)
    )


if __name__ == "__main__":
    main()
