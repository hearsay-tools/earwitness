"""Wait for Komodo's health receipt without inbound access to the homelab."""

import json
import subprocess
import sys
import time


def read_receipt(checkout: str) -> dict:
    subprocess.run(
        [
            "git",
            "-C",
            checkout,
            "fetch",
            "--quiet",
            "origin",
            "+refs/heads/deployment-status/earwitness:refs/remotes/origin/deployment-status/earwitness",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            checkout,
            "show",
            "refs/remotes/origin/deployment-status/earwitness:status.json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def main() -> None:
    checkout, revision, digest = sys.argv[1:]
    expected_image = f"ghcr.io/hearsay-tools/earwitness@{digest}"
    deadline = time.monotonic() + 600
    last_observation = "No deployment receipt"
    while time.monotonic() < deadline:
        try:
            receipt = read_receipt(checkout)
            if (
                receipt.get("status") == "healthy"
                and receipt.get("revision") == revision
                and receipt.get("image") == expected_image
            ):
                print(
                    f"Deployment healthy: {revision} ({receipt.get('verified_at')})",
                    flush=True,
                )
                return
            last_observation = (
                f"status={receipt.get('status')}, revision={receipt.get('revision')}"
            )
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            last_observation = str(exc)
        print(f"Waiting for {revision[:12]}: {last_observation}", flush=True)
        time.sleep(10)
    raise SystemExit(
        f"No matching healthy deployment receipt within 10 minutes: {last_observation}"
    )


if __name__ == "__main__":
    main()
