from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "skills" / "setup-pstack" / "scripts" / "install.py"
CONFIGURER = ROOT / "skills" / "setup-pstack" / "scripts" / "configure.py"
VERIFY = ROOT / "scripts" / "verify_port.py"


def run_script(script: Path, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def write_skill(path: Path, description: str = "fixture skill") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {path.name}\ndescription: {description}\n---\n\n# {path.name}\n",
        encoding="utf-8",
    )


class SetupPstackBehaviorTests(unittest.TestCase):
    def test_installer_preview_apply_repeat_collision_broken_symlink_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-cli-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            destination = root / "repo" / ".agents" / "skills"
            write_skill(source)
            (source / "references").mkdir()
            (source / "references" / "runtime.md").write_text("runtime\n", encoding="utf-8")

            preview = run_script(INSTALLER, "--source", str(source), "--destination", str(destination), "--json")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["status"], "create")
            self.assertFalse((destination / "setup-pstack").exists())

            applied = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--apply",
                "--json",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(applied.stdout)["status"], "create")
            self.assertEqual(
                (destination / "setup-pstack" / "references" / "runtime.md").read_text(encoding="utf-8"),
                "runtime\n",
            )

            repeated = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--apply",
                "--json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "unchanged")

            existing_parent = root / "existing" / ".agents" / "skills"
            existing = existing_parent / "setup-pstack"
            write_skill(existing, "keep this directory")
            before = (existing / "SKILL.md").read_text(encoding="utf-8")
            collision = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(existing_parent),
                "--apply",
            )
            self.assertNotEqual(collision.returncode, 0)
            self.assertEqual((existing / "SKILL.md").read_text(encoding="utf-8"), before)

            broken_parent = root / "broken" / ".agents" / "skills"
            broken_parent.mkdir(parents=True)
            broken_target = broken_parent / "setup-pstack"
            os.symlink(str(root / "missing-target"), broken_target)
            broken = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(broken_parent),
                "--apply",
            )
            self.assertNotEqual(broken.returncode, 0)
            self.assertTrue(broken_target.is_symlink())

            linked_destination = root / "linked-destination"
            linked_destination_target = root / "real-destination"
            linked_destination_target.mkdir()
            os.symlink(str(linked_destination_target), linked_destination, target_is_directory=True)
            destination_link = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(linked_destination),
                "--apply",
            )
            self.assertNotEqual(destination_link.returncode, 0)
            self.assertFalse((linked_destination_target / "setup-pstack").exists())

            bad_source = root / "bad-source" / "setup-pstack"
            write_skill(bad_source)
            os.symlink("missing.txt", bad_source / "zz-broken")
            rollback_parent = root / "rollback" / ".agents" / "skills"
            rollback = run_script(
                INSTALLER,
                "--source",
                str(bad_source),
                "--destination",
                str(rollback_parent),
                "--apply",
            )
            self.assertNotEqual(rollback.returncode, 0)
            self.assertFalse((rollback_parent / "setup-pstack").exists())
            if rollback_parent.exists():
                self.assertEqual(list(rollback_parent.glob(".setup-pstack.install-*")), [])

    def test_configuration_uses_only_catalog_models_and_preserves_repeat_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-cli-test-") as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "codex-test",
                                "supported_reasoning_levels": [
                                    {"effort": "low"},
                                    {"effort": "medium"},
                                    {"effort": "high"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            agents = root / ".codex" / "agents"
            apply_args = (
                "apply",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
                "--json",
            )
            applied = run_script(CONFIGURER, *apply_args)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            result = json.loads(applied.stdout)
            self.assertTrue(result["agents"])
            self.assertEqual({item["effort"] for item in result["agents"]}, {"high"})
            self.assertIn('model = "codex-test"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

            repeated = run_script(CONFIGURER, "preview", *apply_args[1:])
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual({item["status"] for item in json.loads(repeated.stdout)["agents"]}, {"unchanged"})

            invalid = run_script(
                CONFIGURER,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(root / "other"),
                "--budget",
                "medium",
                "--default-model",
                "not-in-catalog",
            )
            self.assertNotEqual(invalid.returncode, 0)

            broken_agents = root / "broken-agents"
            os.symlink(str(root / "missing-agents"), broken_agents)
            broken_root = run_script(
                CONFIGURER,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(broken_agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
            )
            self.assertNotEqual(broken_root.returncode, 0)

    def test_verifier_allows_documented_unsupported_boundary_and_rejects_active_seam(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-verifier-test-") as temporary:
            root = Path(temporary)
            skill = root / "skills" / "fixture"
            write_skill(skill, "fixture")
            skill_file = skill / "SKILL.md"

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n"
                "The Cursor ~/.cursor/rules target is unsupported; never write it.\n"
                "[reference](reference.md)\n",
                encoding="utf-8",
            )
            (skill / "reference.md").write_text("reference\n", encoding="utf-8")
            verifier = importlib.util.spec_from_file_location("verify_port_fixture", VERIFY)
            assert verifier is not None and verifier.loader is not None
            module = importlib.util.module_from_spec(verifier)
            sys.modules[verifier.name] = module
            verifier.loader.exec_module(module)
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n[missing](missing.md)\n",
                encoding="utf-8",
            )
            missing_link_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("link target does not exist" in error for error in missing_link_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\nWrite ~/.cursor/rules/pstack-models.mdc.\n",
                encoding="utf-8",
            )
            errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("active unsupported seam" in error for error in errors))

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_installed_skill_is_visible_to_isolated_codex_prompt_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-discovery-test-") as temporary:
            root = Path(temporary)
            source = ROOT / "skills" / "setup-pstack"
            destination = root / ".agents" / "skills"
            installed = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--apply",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            discovered = subprocess.run(
                ["codex", "debug", "prompt-input"],
                cwd=root,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(discovered.returncode, 0, discovered.stderr)
            self.assertIn("setup-pstack", discovered.stdout)


if __name__ == "__main__":
    unittest.main()
