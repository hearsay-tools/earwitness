import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from scripts.update_homelab import main as promote
from scripts.update_homelab import update_compose
from scripts.verify_deployment import read_receipt


class DeploymentConfigurationTests(unittest.TestCase):
    def test_custom_image_and_source(self):
        services = ["web", "worker"]
        image = "ghcr.io/example/earwitness"
        source = "example/earwitness"
        contents = "services:\n" + "".join(
            f"  {service}:\n    image: {image}@sha256:{'a' * 64}\n"
            for service in services
        )
        updated = update_compose(
            contents, "sha256:" + "b" * 64, "c" * 40, image=image, source=source
        )
        self.assertEqual(updated.count("sha256:" + "b" * 64), len(services))
        self.assertIn("https://github.com/example/earwitness/commit/", updated)

    def test_target_path_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as checkout:
            with patch.dict(os.environ, {"DEPLOY_COMPOSE_PATH": "../outside.yaml"}):
                with patch(
                    "sys.argv", ["promote", checkout, "sha256:" + "a" * 64, "b" * 40]
                ):
                    with self.assertRaisesRegex(ValueError, "inside"):
                        promote()

    def test_receipt_branch_is_configurable(self):
        with patch.dict(os.environ, {"DEPLOY_RECEIPT_BRANCH": "receipts/staging"}):
            with patch("scripts.verify_deployment.subprocess.run") as run:
                run.return_value.stdout = '{"status":"healthy"}'
                read_receipt("checkout")
                self.assertIn(
                    "+refs/heads/receipts/staging:refs/deployment-receipt",
                    run.call_args_list[1].args[0],
                )

    def test_deployment_requires_explicit_opt_in(self):
        workflow = yaml.safe_load(
            (Path(__file__).parents[1] / ".github/workflows/docker.yml").read_text()
        )
        job = workflow["jobs"]["deploy"]
        self.assertTrue(job["if"].startswith("vars.DEPLOY_ENABLED == 'true' &&"))
        self.assertIn("github.event_name != 'pull_request'", job["if"])
        self.assertIn("github.event.repository.default_branch", job["if"])
        checkout = next(
            step
            for step in job["steps"]
            if step.get("with", {}).get("path") == "homelab"
        )
        self.assertEqual(
            checkout["with"]["repository"], "${{ secrets.DEPLOY_REPOSITORY }}"
        )
