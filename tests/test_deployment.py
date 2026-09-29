from unittest.mock import patch

import pytest
import yaml
from scripts.update_homelab import update_compose
from scripts.verify_deployment import main as verify_deployment

DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40
COMPOSE = """name: earwitness
services:
  web:
    image: ghcr.io/hearsay-tools/earwitness:latest
    environment: &env
      SECRET_KEY: ${SECRET_KEY}
    volumes: [/srv/homelab/earwitness/output:/app/output]
  worker:
    image: ghcr.io/hearsay-tools/earwitness:latest
    environment:
      <<: *env
      SERVICE: worker
    volumes: [/srv/homelab/earwitness/output:/app/output]
"""


def test_both_services_promote_atomically_and_preserve_runtime_settings():
    updated = update_compose(COMPOSE, DIGEST, REVISION)
    expected = yaml.safe_load(COMPOSE)
    for service in expected["services"].values():
        service["image"] = f"ghcr.io/hearsay-tools/earwitness@{DIGEST}"
    assert yaml.safe_load(updated) == expected
    assert updated.count(REVISION) == 2
    assert update_compose(updated, DIGEST, REVISION) == updated
    newer = update_compose(updated, "sha256:" + "c" * 64, "d" * 40)
    assert DIGEST not in newer
    assert REVISION not in newer


@pytest.mark.parametrize(
    "contents", [COMPOSE.replace("  worker:", "  renamed:"), COMPOSE + COMPOSE]
)
def test_rejects_incomplete_or_duplicate_services(contents):
    with pytest.raises(ValueError):
        update_compose(contents, DIGEST, REVISION)


def test_rejects_invalid_release_identifiers():
    with pytest.raises(ValueError):
        update_compose(COMPOSE, "latest\n    privileged: true", REVISION)
    with pytest.raises(ValueError):
        update_compose(COMPOSE, DIGEST, "main")


def test_verification_waits_for_matching_healthy_image_and_revision():
    receipt = {
        "status": "healthy",
        "revision": REVISION,
        "image": f"ghcr.io/hearsay-tools/earwitness@{DIGEST}",
    }
    with (
        patch("sys.argv", ["verify", "homelab", REVISION, DIGEST]),
        patch("scripts.verify_deployment.time.sleep") as sleep,
        patch(
            "scripts.verify_deployment.read_receipt",
            side_effect=[
                receipt | {"revision": "c" * 40},
                receipt | {"image": "old"},
                receipt | {"status": "unhealthy"},
                receipt,
            ],
        ),
    ):
        verify_deployment()
        assert sleep.call_count == 3


def test_verification_timeout_is_failure():
    with (
        patch("sys.argv", ["verify", "homelab", REVISION, DIGEST]),
        patch("scripts.verify_deployment.time.monotonic", side_effect=[0, 601]),
        pytest.raises(SystemExit, match="No matching healthy deployment receipt"),
    ):
        verify_deployment()
