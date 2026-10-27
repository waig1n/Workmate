from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import sync_images_to_remote as sync  # noqa: E402


class ImageSpecTests(unittest.TestCase):
    def test_parse_image_spec_maps_local_to_registry_repository(self) -> None:
        spec = sync.parse_image_spec(
            "workmate-frontend:2.0=workmate-frontend:2.0",
        )

        self.assertEqual(spec.local_ref, "workmate-frontend:2.0")
        self.assertEqual(spec.registry_ref, "workmate-frontend:2.0")

    def test_parse_image_spec_rejects_missing_mapping_separator(self) -> None:
        with self.assertRaisesRegex(ValueError, "LOCAL=REGISTRY"):
            sync.parse_image_spec("workmate-frontend:2.0")

    def test_parse_image_spec_rejects_shell_metacharacters(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid image reference"):
            sync.parse_image_spec(
                "workmate-frontend:2.0=workmate-frontend:2.0;whoami",
            )


class ValidationTests(unittest.TestCase):
    def test_cross_architecture_apply_is_rejected_before_transfer(self):
        from types import SimpleNamespace
        runner = SimpleNamespace(
            remote_command=lambda *a, **k: SimpleNamespace(stdout='linux/x86_64'),
            local=lambda *a, **k: SimpleNamespace(stdout='linux/arm64'),
        )
        with self.assertRaisesRegex(sync.SyncError, 'Registry-only'):
            sync.verify_target_platform(runner, sync.DEFAULT_IMAGES)

    def test_x86_daemon_alias_matches_amd64_image(self):
        from types import SimpleNamespace
        runner = SimpleNamespace(
            remote_command=lambda *a, **k: SimpleNamespace(stdout='linux/x86_64'),
            local=lambda *a, **k: SimpleNamespace(stdout='linux/amd64'),
        )
        sync.verify_target_platform(runner, sync.DEFAULT_IMAGES)

    def test_validate_remote_accepts_ssh_user_and_host(self) -> None:
        self.assertEqual(
            sync.validate_remote("operator@example.test"),
            "operator@example.test",
        )

    def test_validate_remote_rejects_ssh_options(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid SSH target"):
            sync.validate_remote("-oProxyCommand=bad")

    def test_registry_binding_requires_loopback_only(self) -> None:
        safe = json.dumps(
            {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5000"}]},
        )
        public = json.dumps(
            {"5000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5000"}]},
        )

        self.assertTrue(sync.registry_binding_is_safe(safe, 5000))
        self.assertFalse(sync.registry_binding_is_safe(public, 5000))
        self.assertFalse(sync.registry_binding_is_safe("null", 5000))

    def test_registry_binding_allows_custom_loopback_host_port(self) -> None:
        bindings = json.dumps(
            {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5500"}]},
        )

        self.assertTrue(sync.registry_binding_is_safe(bindings, 5500))


class SyncPlanTests(unittest.TestCase):
    def test_build_sync_plan_skips_identical_images(self) -> None:
        specs = sync.DEFAULT_IMAGES
        local = {spec.local_ref: f"sha256:{index}" for index, spec in enumerate(specs)}
        remote = dict(local)

        plan = sync.build_sync_plan(specs, local, remote)

        self.assertEqual(plan, [])

    def test_build_sync_plan_selects_missing_and_changed_images(self) -> None:
        specs = sync.DEFAULT_IMAGES
        local = {spec.local_ref: f"sha256:new-{index}" for index, spec in enumerate(specs)}
        remote = {
            specs[0].local_ref: local[specs[0].local_ref],
            specs[1].local_ref: "sha256:old-api",
        }

        plan = sync.build_sync_plan(specs, local, remote)

        self.assertEqual(plan, [specs[1], specs[2]])


if __name__ == "__main__":
    unittest.main()
