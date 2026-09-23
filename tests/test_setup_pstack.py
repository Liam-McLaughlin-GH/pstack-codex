from __future__ import annotations

import json
import importlib.util
import errno
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
SAFE_FS = ROOT / "skills" / "setup-pstack" / "scripts" / "safe_fs.py"
VERIFY = ROOT / "scripts" / "verify_port.py"


def run_script(script: Path, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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

            file_link_source = root / "file-link-source" / "setup-pstack"
            write_skill(file_link_source)
            (file_link_source / "payload.txt").write_text("payload\n", encoding="utf-8")
            os.symlink("payload.txt", file_link_source / "alias.txt")
            file_link_destination = root / "file-link-destination" / ".agents" / "skills"
            for _attempt in range(2):
                rejected = run_script(
                    INSTALLER,
                    "--source",
                    str(file_link_source),
                    "--destination",
                    str(file_link_destination),
                    "--apply",
                )
                self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(file_link_destination.exists())

            directory_link_source = root / "directory-link-source" / "setup-pstack"
            write_skill(directory_link_source)
            (directory_link_source / "payload").mkdir()
            (directory_link_source / "payload" / "nested.txt").write_text("nested\n", encoding="utf-8")
            os.symlink("payload", directory_link_source / "alias")
            directory_link_destination = root / "directory-link-destination" / ".agents" / "skills"
            for _attempt in range(2):
                rejected = run_script(
                    INSTALLER,
                    "--source",
                    str(directory_link_source),
                    "--destination",
                    str(directory_link_destination),
                    "--apply",
                )
                self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(directory_link_destination.exists())

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

            linked_parent_target = root / "real-parent"
            linked_parent_destination = linked_parent_target / ".agents" / "skills"
            linked_parent_destination.mkdir(parents=True)
            linked_parent = root / "linked-parent"
            os.symlink(str(linked_parent_target), linked_parent, target_is_directory=True)
            ancestor_link = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(linked_parent / ".agents" / "skills"),
                "--apply",
            )
            self.assertNotEqual(ancestor_link.returncode, 0)
            self.assertFalse((linked_parent_target / "setup-pstack").exists())

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
            self.assertFalse(rollback_parent.exists())

    def test_installer_failure_removes_staging_and_new_parent_directories(self) -> None:
        module = load_module(INSTALLER, "install_for_rollback_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-rollback-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            destination = root / "new-root" / ".agents" / "skills"
            original_publish = module.atomic_rename_noreplace_at

            def fail_publication(parent_fd: int, source_name: str, target_name: str) -> None:
                raise OSError("injected publication failure")

            module.atomic_rename_noreplace_at = fail_publication
            try:
                with self.assertRaises(module.InstallError):
                    module.apply_install(source, destination)
            finally:
                module.atomic_rename_noreplace_at = original_publish
            self.assertFalse((root / "new-root").exists())

    def test_installer_rollback_reports_a_replaced_created_parent(self) -> None:
        module = load_module(INSTALLER, "install_for_created_parent_cleanup_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-created-parent-cleanup-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            destination = root / "new-root" / ".agents" / "skills"
            original_publish = module.atomic_rename_noreplace_at
            original_cleanup = module.cleanup_created_directories

            def fail_publication(parent_fd: int, source_name: str, target_name: str) -> None:
                raise OSError("injected publication failure")

            def replace_created_parent(created: list[object]) -> None:
                entry = next(item for item in created if getattr(item, "name") == "skills")
                original_path = entry.path
                os.rename(original_path, root / "original-skills")
                original_path.mkdir()
                (original_path / "operator.txt").write_text("keep me\n", encoding="utf-8")
                original_cleanup(created)

            module.atomic_rename_noreplace_at = fail_publication
            module.cleanup_created_directories = replace_created_parent
            try:
                with self.assertRaises(module.InstallError) as raised:
                    module.apply_install(source, destination)
            finally:
                module.atomic_rename_noreplace_at = original_publish
                module.cleanup_created_directories = original_cleanup
            self.assertEqual(raised.exception.status, "cleanup_pending")
            self.assertEqual(raised.exception.recovery_state, "rollback_parent_cleanup_pending")
            self.assertIn("created parent cleanup", str(raised.exception))
            self.assertIsNotNone(raised.exception.recovery_receipt)
            self.assertLessEqual(len(raised.exception.recovery_receipt.entries), 8)
            self.assertIn("skills", {entry.path.name for entry in raised.exception.recovery_receipt.entries})
            self.assertIn("Recovery receipt:", str(raised.exception))
            self.assertEqual(raised.exception.as_dict()["recovery_receipt"], raised.exception.recovery_receipt.as_dict())
            self.assertEqual(raised.exception.as_dict()["recovery_state"], "rollback_parent_cleanup_pending")
            self.assertEqual((destination / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue((root / "original-skills").is_dir())

        safe_fs = sys.modules["safe_fs"]
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-final-parent-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            destination = root / "new-root" / ".agents" / "skills"
            replacement = root / "replacement-skills"
            replacement.mkdir()
            (replacement / "operator.txt").write_text("keep me\n", encoding="utf-8")
            expected_parent = root / "expected-skills"
            original_publish = module.atomic_rename_noreplace_at
            original_unlink = safe_fs._native_unlink_at
            directory_checks = 0
            swapped = False

            def fail_publication(parent_fd: int, source_name: str, target_name: str) -> None:
                raise OSError("injected publication failure")

            def replace_final_parent(
                parent_fd: int,
                name: str,
                *,
                is_directory: bool,
                **kwargs: object,
            ) -> None:
                nonlocal directory_checks, swapped
                if is_directory:
                    directory_checks += 1
                if is_directory and directory_checks == 2 and not swapped:
                    swapped = True
                    os.rename(destination, expected_parent)
                    os.rename(replacement, destination)
                    raise safe_fs.SafeFilesystemError("injected final parent replacement")
                original_unlink(parent_fd, name, is_directory=is_directory, **kwargs)

            module.atomic_rename_noreplace_at = fail_publication
            safe_fs._native_unlink_at = replace_final_parent
            try:
                with self.assertRaises(module.InstallError) as raised:
                    module.apply_install(source, destination)
            finally:
                module.atomic_rename_noreplace_at = original_publish
                safe_fs._native_unlink_at = original_unlink
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.status, "cleanup_pending")
            self.assertEqual(raised.exception.recovery_state, "rollback_parent_cleanup_pending")
            self.assertIsNotNone(raised.exception.recovery_receipt)
            self.assertIn("created-parent identities", raised.exception.retry or "")
            self.assertEqual((destination / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue(expected_parent.is_dir())

    def test_installer_rollback_reports_identity_bound_cleanup_receipt(self) -> None:
        module = load_module(INSTALLER, "install_for_cleanup_receipt_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-cleanup-receipt-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            destination = root / "destination"
            destination.mkdir()
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "operator.txt").write_text("keep me\n", encoding="utf-8")
            original_publish = module.atomic_rename_noreplace_at
            original_remove = module.remove_tree_at
            swapped = False

            def fail_publication(parent_fd: int, source_name: str, target_name: str) -> None:
                raise OSError("injected publication failure")

            def swap_staging(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    residue = destination / name
                    os.rename(residue, root / "original-staging")
                    os.rename(replacement, residue)
                original_remove(parent_fd, name, **kwargs)

            module.atomic_rename_noreplace_at = fail_publication
            module.remove_tree_at = swap_staging
            try:
                with self.assertRaises(module.InstallError) as raised:
                    module.apply_install(source, destination)
            finally:
                module.atomic_rename_noreplace_at = original_publish
                module.remove_tree_at = original_remove
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.status, "cleanup_pending")
            self.assertIsNotNone(raised.exception.residue)
            self.assertIn("install.py cleanup --residue", raised.exception.retry or "")
            self.assertIn("--expected-dev", raised.exception.retry or "")
            self.assertIn("cleanup_pending", str(raised.exception))
            self.assertEqual((destination / raised.exception.residue.name / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue((root / "original-staging" / "SKILL.md").is_file())

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_installer_rejects_a_hardlink_source_swap(self) -> None:
        module = load_module(INSTALLER, "install_for_hardlink_race_test")
        safe_fs = sys.modules["safe_fs"]
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-hardlink-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            source_file = source / "payload.txt"
            source_file.write_text("safe\n", encoding="utf-8")
            outside_file = root / "outside.txt"
            outside_file.write_text("SECRET\n", encoding="utf-8")
            destination = root / "destination"
            original_read = safe_fs.read_regular_file_at
            swapped = False

            def swap_source_file(directory_fd: int, name: str, **kwargs: object) -> bytes:
                nonlocal swapped
                if name == "payload.txt" and not swapped:
                    swapped = True
                    source_file.unlink()
                    try:
                        os.link(outside_file, source_file)
                    except OSError as exc:
                        source_file.write_text("safe\n", encoding="utf-8")
                        self.skipTest(f"hardlink probe unavailable: {exc}")
                try:
                    return original_read(directory_fd, name, **kwargs)
                finally:
                    if source_file.is_symlink() or source_file.stat().st_ino == outside_file.stat().st_ino:
                        source_file.unlink()
                    source_file.write_text("safe\n", encoding="utf-8")

            safe_fs.read_regular_file_at = swap_source_file
            try:
                with self.assertRaises(module.InstallError):
                    module.apply_install(source, destination)
            finally:
                safe_fs.read_regular_file_at = original_read
            self.assertTrue(swapped)
            self.assertFalse(destination.exists())
            self.assertEqual(source_file.read_text(encoding="utf-8"), "safe\n")

    def test_installer_noreplace_refuses_competitor_after_absent_check(self) -> None:
        module = load_module(INSTALLER, "install_for_noreplace_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-noreplace-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            destination = root / "destination"
            destination.mkdir()
            competitor = destination / source.name
            original_publish = module.atomic_rename_noreplace_at
            injected = False

            def publish_with_competitor(parent_fd: int, source_name: str, target_name: str) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    competitor.mkdir()
                    (competitor / "SKILL.md").write_text("competitor\n", encoding="utf-8")
                original_publish(parent_fd, source_name, target_name)

            module.atomic_rename_noreplace_at = publish_with_competitor
            try:
                with self.assertRaises(module.InstallError):
                    module.apply_install(source, destination)
            finally:
                module.atomic_rename_noreplace_at = original_publish
            self.assertTrue(injected)
            self.assertEqual((competitor / "SKILL.md").read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual(list(destination.glob(".setup-pstack-install-*")), [])

    def test_self_tests_and_repeated_preview_leave_no_bytecode_residue(self) -> None:
        def bytecode_paths(path: Path) -> set[Path]:
            return {
                candidate.relative_to(path)
                for candidate in path.rglob("*")
                if candidate.is_file() and (candidate.suffix == ".pyc" or "__pycache__" in candidate.parts)
            }

        with tempfile.TemporaryDirectory(prefix="setup-pstack-bytecode-test-") as temporary:
            root = Path(temporary)
            skill = root / "skill" / "setup-pstack"
            shutil.copytree(
                ROOT / "skills" / "setup-pstack",
                skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            catalog = root / "models.json"
            catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "codex-test",
                                "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high")],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            agents = root / ".codex" / "agents"
            configure = skill / "scripts" / "configure.py"
            installer = skill / "scripts" / "install.py"
            before = bytecode_paths(root)
            first = run_script(
                configure,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
                "--json",
                cwd=root,
            )
            second = run_script(
                configure,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
                "--json",
                cwd=root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            for script, arguments in (
                (installer, ("--self-test",)),
                (configure, ("self-test",)),
            ):
                completed = run_script(script, *arguments, cwd=root)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(bytecode_paths(root), before)

    def test_installer_keeps_writes_on_open_directory_and_rejects_source_swap(self) -> None:
        module = load_module(INSTALLER, "install_for_race_test")
        safe_fs = sys.modules["safe_fs"]
        with tempfile.TemporaryDirectory(prefix="setup-pstack-install-race-test-") as temporary:
            root = Path(temporary)
            source = root / "source" / "setup-pstack"
            write_skill(source)
            source_file = source / "payload.txt"
            source_file.write_text("safe\n", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "payload.txt").write_text("SECRET\n", encoding="utf-8")

            destination_parent = root / "requested"
            destination = destination_parent / ".agents" / "skills"
            destination.mkdir(parents=True)
            redirected = root / "redirected"
            redirected.mkdir()
            original_target_plan = module._target_plan
            switched = False

            def replace_ancestor(*args: object, **kwargs: object):
                nonlocal switched
                if not switched:
                    switched = True
                    os.rename(destination_parent, root / "requested-original")
                    os.symlink(redirected, destination_parent, target_is_directory=True)
                return original_target_plan(*args, **kwargs)

            module._target_plan = replace_ancestor
            try:
                installed = module.apply_install(source, destination)
            finally:
                module._target_plan = original_target_plan
            self.assertEqual(installed.status, "create")
            self.assertFalse((redirected / ".agents" / "skills" / "setup-pstack").exists())
            self.assertTrue((root / "requested-original" / ".agents" / "skills" / "setup-pstack" / "SKILL.md").is_file())

            original_read = safe_fs.read_regular_file_at

            def swap_source_file(directory_fd: int, name: str, **kwargs: object) -> bytes:
                if name == "payload.txt":
                    source_file.unlink()
                    os.symlink(outside / "payload.txt", source_file)
                try:
                    return original_read(directory_fd, name, **kwargs)
                finally:
                    if source_file.is_symlink():
                        source_file.unlink()
                    source_file.write_text("safe\n", encoding="utf-8")

            safe_fs.read_regular_file_at = swap_source_file
            try:
                with self.assertRaises(module.InstallError):
                    module.apply_install(source, root / "source-swap-destination")
            finally:
                safe_fs.read_regular_file_at = original_read
            self.assertFalse((root / "source-swap-destination").exists())

    def test_configuration_keeps_writes_on_open_directory_and_swaps_generations(self) -> None:
        module = load_module(CONFIGURER, "configure_for_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-race-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            requested = root / "requested"
            agents = requested / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")

            redirected = root / "redirected"
            redirected.mkdir()
            original_open_root = module._open_agents_root_at
            switched = False

            def replace_ancestor(parent_fd: int, name: str):
                nonlocal switched
                if not switched:
                    switched = True
                    os.rename(requested, root / "requested-original")
                    os.symlink(redirected, requested, target_is_directory=True)
                return original_open_root(parent_fd, name)

            module._open_agents_root_at = replace_ancestor
            try:
                module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module._open_agents_root_at = original_open_root
            self.assertIn(
                'model_reasoning_effort = "xhigh"',
                (root / "requested-original" / ".codex" / "agents" / "pstack-feature.toml").read_text(encoding="utf-8"),
            )
            self.assertFalse((redirected / ".codex" / "agents").exists())

            requested = root / "atomic-requested"
            agents = requested / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            observed: list[tuple[str, int]] = []
            original_exchange = module.atomic_exchange_at

            def observe_publication(parent_fd: int, first_name: str, second_name: str) -> None:
                original_exchange(parent_fd, first_name, second_name)
                if first_name == agents.name:
                    files = list(agents.glob("pstack-*.toml")) if agents.exists() else []
                    efforts = {
                        path.read_text(encoding="utf-8").count('model_reasoning_effort = "xhigh"')
                        for path in files
                    }
                    observed.append(("present" if files else "absent", len(files) if efforts else 0))

            module.atomic_exchange_at = observe_publication
            try:
                module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.atomic_exchange_at = original_exchange
            self.assertIn(("present", 30), observed)
            self.assertNotIn(("present", 1), observed)
            self.assertNotIn(("absent", 0), observed)
            self.assertEqual(list(agents.parent.glob(".pstack-agents-old-*")), [])

            raced_agents = requested / ".codex" / "agents-race"
            initial = module.build_plan(models, raced_agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, raced_agents)
            raced_replacement = module.build_plan(models, raced_agents, budget="large")
            raced_replacement_root = root / "raced-replacement"
            raced_replacement_root.mkdir()
            (raced_replacement_root / "operator.txt").write_text("keep me\n", encoding="utf-8")
            original_exchange = module.atomic_exchange_at
            switched = False

            def replace_root_before_exchange(parent_fd: int, first_name: str, second_name: str) -> None:
                nonlocal switched
                if not switched and first_name == raced_agents.name:
                    switched = True
                    os.rename(raced_agents, root / "raced-original")
                    os.rename(raced_replacement_root, raced_agents)
                original_exchange(parent_fd, first_name, second_name)

            module.atomic_exchange_at = replace_root_before_exchange
            try:
                with self.assertRaises(module.ConfigurationError):
                    module.apply_plan(raced_replacement, raced_agents, allow_replace=True)
            finally:
                module.atomic_exchange_at = original_exchange
            self.assertEqual((raced_agents / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse((raced_agents / "pstack-feature.toml").exists())

    def test_configuration_revalidates_the_live_root_before_reporting_published(self) -> None:
        module = load_module(CONFIGURER, "configure_for_final_publication_check_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-final-publication-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            competitor = root / "competitor"
            competitor.mkdir()
            (competitor / "operator.txt").write_text("keep me\n", encoding="utf-8")
            original_state = module._publication_state
            swapped = False

            def swap_after_publication_check(*args: object, **kwargs: object):
                nonlocal swapped
                outcome = original_state(*args, **kwargs)
                if not swapped and outcome.status == "published":
                    swapped = True
                    os.rename(agents, root / "published-generation")
                    os.rename(competitor, agents)
                return outcome

            module._publication_state = swap_after_publication_check
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module._publication_state = original_state
            self.assertTrue(swapped)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertEqual((agents / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertIn('model_reasoning_effort = "xhigh"', (root / "published-generation" / "pstack-feature.toml").read_text(encoding="utf-8"))

    def test_configuration_does_not_report_published_after_the_final_root_check_is_raced(self) -> None:
        module = load_module(CONFIGURER, "configure_for_final_root_check_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-final-root-race-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            competitor = root / "competitor"
            competitor.mkdir()
            (competitor / "operator.txt").write_text("keep me\n", encoding="utf-8")
            original_state = module._publication_state
            published_checks = 0

            def swap_after_last_check(*args: object, **kwargs: object):
                nonlocal published_checks
                outcome = original_state(*args, **kwargs)
                if outcome.status == "published":
                    published_checks += 1
                    if published_checks == 2:
                        os.rename(agents, root / "published-generation")
                        os.rename(competitor, agents)
                return outcome

            module._publication_state = swap_after_last_check
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module._publication_state = original_state
            self.assertGreaterEqual(published_checks, 2)
            self.assertNotEqual(outcome.status, "published")
            self.assertEqual((agents / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertIn('model_reasoning_effort = "xhigh"', (root / "published-generation" / "pstack-feature.toml").read_text(encoding="utf-8"))

    def test_configuration_refuses_a_live_root_state_change_before_reporting_published(self) -> None:
        module = load_module(CONFIGURER, "configure_for_final_state_check_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-final-state-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            original_state = module._publication_state
            mutated = False

            def mutate_after_publication_check(*args: object, **kwargs: object):
                nonlocal mutated
                outcome = original_state(*args, **kwargs)
                if not mutated and outcome.status == "published":
                    mutated = True
                    (agents / "pstack-feature.toml").write_text("operator edit\n", encoding="utf-8")
                return outcome

            module._publication_state = mutate_after_publication_check
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module._publication_state = original_state
            self.assertTrue(mutated)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertEqual((agents / "pstack-feature.toml").read_text(encoding="utf-8"), "operator edit\n")

    def test_configuration_rejects_stale_preview_and_reports_cleanup_residue(self) -> None:
        module = load_module(CONFIGURER, "configure_for_transaction_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-transaction-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)

            stale = module.build_plan(models, agents, budget="large")
            feature = agents / "pstack-feature.toml"
            feature.write_bytes(feature.read_bytes() + b"# external edit\n")
            with self.assertRaises(module.ConfigurationError):
                module.apply_plan(stale, agents, allow_replace=True)
            self.assertIn("# external edit\n", feature.read_text(encoding="utf-8"))

            fresh = module.build_plan(models, agents, budget="large")
            original_remove = module.remove_tree_at
            failed = False

            def fail_cleanup(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal failed
                if not failed and name.startswith(".pstack-agents-"):
                    failed = True
                    raise OSError("injected old-generation cleanup failure")
                original_remove(parent_fd, name, **kwargs)

            module.remove_tree_at = fail_cleanup
            try:
                outcome = module.apply_plan(fresh, agents, allow_replace=True)
            finally:
                module.remove_tree_at = original_remove
            self.assertEqual(outcome.status, "published_cleanup_pending")
            self.assertIsNotNone(outcome.residue)
            self.assertTrue(outcome.residue.is_dir())
            self.assertIn('model_reasoning_effort = "xhigh"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

            cleanup = run_script(CONFIGURER, "cleanup", "--residue", str(outcome.residue), "--json")
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertEqual(json.loads(cleanup.stdout)["status"], "cleanup_complete")
            self.assertFalse(outcome.residue.exists())

    def test_configuration_refuses_swapped_live_root_or_residue_during_cleanup(self) -> None:
        module = load_module(CONFIGURER, "configure_for_cleanup_identity_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-cleanup-identity-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            original_remove = module.remove_tree_at

            competitor_root = root / "competitor-root"
            competitor_root.mkdir()
            (competitor_root / "operator.txt").write_text("keep root\n", encoding="utf-8")
            swapped = False

            def swap_live_root(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal swapped
                if not swapped and kwargs.get("guard_name") == agents.name:
                    swapped = True
                    os.rename(agents, root / "published-generation")
                    os.rename(competitor_root, agents)
                original_remove(parent_fd, name, **kwargs)

            module.remove_tree_at = swap_live_root
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.remove_tree_at = original_remove
            self.assertTrue(swapped)
            self.assertEqual(outcome.status, "root_replacement_cleanup_pending")
            self.assertEqual((agents / "operator.txt").read_text(encoding="utf-8"), "keep root\n")
            self.assertTrue((root / "published-generation" / "pstack-feature.toml").is_file())
            self.assertTrue(outcome.residue is not None and outcome.residue.is_dir())

            agents = root / "residue-agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            competitor_residue = root / "competitor-residue"
            competitor_residue.mkdir()
            (competitor_residue / "operator.txt").write_text("keep residue\n", encoding="utf-8")
            swapped = False

            def swap_residue(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal swapped
                if not swapped and kwargs.get("expected_metadata") is not None:
                    residue = agents.parent / name
                    swapped = True
                    os.rename(residue, root / "original-residue")
                    os.rename(competitor_residue, residue)
                original_remove(parent_fd, name, **kwargs)

            module.remove_tree_at = swap_residue
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.remove_tree_at = original_remove
            self.assertTrue(swapped)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertEqual((outcome.residue / "operator.txt").read_text(encoding="utf-8"), "keep residue\n")
            self.assertTrue((root / "original-residue" / "pstack-feature.toml").is_file())

            safe_fs = sys.modules["safe_fs"]
            rollback_root = root / "rollback-config"
            rollback_agents = rollback_root / ".codex" / "agents"
            rollback_plan = module.build_plan(models, rollback_agents, budget="medium")
            replacement_parent = rollback_root / "replacement-parent"
            replacement_parent.mkdir(parents=True)
            (replacement_parent / "operator.txt").write_text("keep config parent\n", encoding="utf-8")
            expected_parent = rollback_root / "expected-parent"
            original_publish = module.atomic_rename_noreplace_at
            original_unlink = safe_fs._native_unlink_at
            directory_checks = 0
            swapped = False

            def fail_publication(parent_fd: int, source_name: str, target_name: str) -> None:
                raise OSError("injected configuration publication failure")

            def replace_final_parent(
                parent_fd: int,
                name: str,
                *,
                is_directory: bool,
                **kwargs: object,
            ) -> None:
                nonlocal directory_checks, swapped
                if is_directory:
                    directory_checks += 1
                if is_directory and directory_checks == 2 and not swapped:
                    swapped = True
                    os.rename(rollback_agents.parent, expected_parent)
                    os.rename(replacement_parent, rollback_agents.parent)
                    raise safe_fs.SafeFilesystemError("injected final configuration parent replacement")
                original_unlink(parent_fd, name, is_directory=is_directory, **kwargs)

            module.atomic_rename_noreplace_at = fail_publication
            safe_fs._native_unlink_at = replace_final_parent
            try:
                with self.assertRaises(module.ConfigurationError) as raised:
                    module.apply_plan(rollback_plan, rollback_agents)
            finally:
                module.atomic_rename_noreplace_at = original_publish
                safe_fs._native_unlink_at = original_unlink
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.status, "cleanup_pending")
            self.assertEqual(raised.exception.recovery_state, "rollback_parent_cleanup_pending")
            self.assertIsNotNone(raised.exception.recovery_receipt)
            self.assertIn("created-parent identities", raised.exception.retry or "")
            self.assertEqual((rollback_agents.parent / "operator.txt").read_text(encoding="utf-8"), "keep config parent\n")
            self.assertTrue(expected_parent.is_dir())

    def test_publication_cleanup_preserves_replacement_residue_and_never_reports_published(self) -> None:
        module = load_module(CONFIGURER, "configure_for_publication_cleanup_replacement_test")
        safe_fs = sys.modules["safe_fs"]
        with tempfile.TemporaryDirectory(prefix="setup-pstack-publication-cleanup-replacement-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            replacement_residue = root / "replacement-residue"
            replacement_residue.mkdir()
            (replacement_residue / "operator.txt").write_text("keep residue\n", encoding="utf-8")
            original_rename = safe_fs.atomic_rename_noreplace_at
            swapped = False

            def replace_after_residue_binding(parent_fd: int, source_name: str, target_name: str) -> None:
                nonlocal swapped
                original_rename(parent_fd, source_name, target_name)
                if not swapped and source_name.startswith(".pstack-agents-") and target_name.startswith(".pstack-cleanup-"):
                    swapped = True
                    os.rename(replacement_residue, agents.parent / source_name)

            safe_fs.atomic_rename_noreplace_at = replace_after_residue_binding
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                safe_fs.atomic_rename_noreplace_at = original_rename
            self.assertTrue(swapped)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertNotEqual(outcome.status, "published")
            self.assertEqual((outcome.residue / "operator.txt").read_text(encoding="utf-8"), "keep residue\n")

            private_agents = root / ".codex" / "agents-private"
            initial = module.build_plan(models, private_agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, private_agents)
            private_replacement = root / "private-replacement"
            private_replacement.mkdir()
            (private_replacement / "operator.txt").write_text("keep private replacement\n", encoding="utf-8")
            original_rename = safe_fs.atomic_rename_noreplace_at
            swapped = False

            def replace_private_quarantine(parent_fd: int, source_name: str, target_name: str) -> None:
                nonlocal swapped
                original_rename(parent_fd, source_name, target_name)
                if not swapped and source_name.startswith(".pstack-agents-") and target_name.startswith(".pstack-cleanup-"):
                    swapped = True
                    residue_path = private_agents.parent / target_name
                    os.rename(residue_path, root / "expected-private-agents")
                    os.rename(private_replacement, private_agents.parent / target_name)

            safe_fs.atomic_rename_noreplace_at = replace_private_quarantine
            try:
                outcome = module.apply_plan(
                    module.build_plan(models, private_agents, budget="large"),
                    private_agents,
                    allow_replace=True,
                )
            finally:
                safe_fs.atomic_rename_noreplace_at = original_rename
            self.assertTrue(swapped)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertIsNotNone(outcome.recovery_receipt, outcome.as_dict())
            self.assertEqual(
                (outcome.residue / "operator.txt").read_text(encoding="utf-8"),
                "keep private replacement\n",
            )

    def test_publication_cleanup_without_receipt_never_reports_published(self) -> None:
        module = load_module(CONFIGURER, "configure_for_cleanup_without_receipt_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-publication-no-receipt-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            module.apply_plan(module.build_plan(models, agents, budget="medium", default_model="codex-test"), agents)
            replacement = module.build_plan(models, agents, budget="large")
            original_remove = module.remove_tree_at
            failed = False

            def remove_then_lose_residue(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal failed
                if not failed and kwargs.get("guard_name") == agents.name:
                    failed = True
                    residue = agents.parent / name
                    os.rename(residue, root / "unproved-deletion")
                    shutil.rmtree(root / "unproved-deletion")
                    raise OSError("injected cleanup reporting failure")
                original_remove(parent_fd, name, **kwargs)

            module.remove_tree_at = remove_then_lose_residue
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.remove_tree_at = original_remove
            self.assertTrue(failed)
            self.assertEqual(outcome.status, "publication_recovery_pending")
            self.assertNotEqual(outcome.status, "published")
            self.assertEqual(outcome.recovery_state, "publication_cleanup_recovery_pending")

    def test_configuration_fallback_publishes_and_cleans_by_identity(self) -> None:
        module = load_module(CONFIGURER, "configure_for_fallback_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-fallback-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            replacement = module.build_plan(models, agents, budget="large")
            original_exchange = module.atomic_exchange_at

            def unavailable_exchange(parent_fd: int, first_name: str, second_name: str) -> None:
                raise module.SafeFilesystemError("injected unavailable exchange")

            module.atomic_exchange_at = unavailable_exchange
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.atomic_exchange_at = original_exchange
            self.assertEqual(outcome.status, "published")
            self.assertIn('model_reasoning_effort = "xhigh"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))
            self.assertEqual(list(agents.parent.glob(".pstack-agents-old-*")), [])

    def test_cleanup_residue_refuses_a_swapped_victim(self) -> None:
        module = load_module(CONFIGURER, "configure_for_cleanup_residue_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-cleanup-residue-test-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            residue = parent / ".pstack-agents-old-test"
            residue.mkdir()
            (residue / "expected.txt").write_text("expected\n", encoding="utf-8")
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "victim.txt").write_text("victim\n", encoding="utf-8")
            original_remove = module.remove_tree_at
            swapped = False

            def swap_victim(parent_fd: int, name: str, **kwargs: object) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(residue, root / "expected-residue")
                    os.rename(replacement, residue)
                original_remove(parent_fd, name, **kwargs)

            module.remove_tree_at = swap_victim
            try:
                with self.assertRaises(module.ConfigurationError) as raised:
                    module._cleanup_residue(residue)
            finally:
                module.remove_tree_at = original_remove
            self.assertTrue(swapped)
            self.assertIn("refused", str(raised.exception))
            self.assertEqual((residue / "victim.txt").read_text(encoding="utf-8"), "victim\n")
            self.assertEqual((root / "expected-residue" / "expected.txt").read_text(encoding="utf-8"), "expected\n")

    def test_cleanup_residue_binds_nested_file_and_directory_identities(self) -> None:
        module = load_module(CONFIGURER, "configure_for_nested_cleanup_identity_test")
        safe_fs = sys.modules["safe_fs"]
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="setup-pstack-nested-cleanup-test-") as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                residue = parent / ".pstack-agents-old-test"
                residue.mkdir()
                victim = residue / "victim"
                replacement = root / "replacement"
                expected_private = root / "expected-victim"
                if kind == "file":
                    victim.write_text("expected\n", encoding="utf-8")
                    replacement.write_text("replacement\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    (victim / "expected.txt").write_text("expected\n", encoding="utf-8")
                    replacement.mkdir()
                    (replacement / "replacement.txt").write_text("replacement\n", encoding="utf-8")
                original_lstat = safe_fs.lstat_at
                seen = 0

                def swap_nested_entry(directory_fd: int, name: str) -> os.stat_result:
                    nonlocal seen
                    if name == "victim":
                        seen += 1
                        if seen == 2:
                            os.rename(victim.name, expected_private, src_dir_fd=directory_fd)
                            os.rename(replacement, victim.name, dst_dir_fd=directory_fd)
                    return original_lstat(directory_fd, name)

                safe_fs.lstat_at = swap_nested_entry
                try:
                    with self.assertRaises(module.ConfigurationError) as raised:
                        module._cleanup_residue(residue)
                finally:
                    safe_fs.lstat_at = original_lstat
                self.assertIn("refused", str(raised.exception))
                if kind == "file":
                    self.assertEqual((victim).read_text(encoding="utf-8"), "replacement\n")
                    self.assertEqual(expected_private.read_text(encoding="utf-8"), "expected\n")
                else:
                    self.assertEqual((victim / "replacement.txt").read_text(encoding="utf-8"), "replacement\n")
                    self.assertEqual((expected_private / "expected.txt").read_text(encoding="utf-8"), "expected\n")

            stable_residue = parent / ".pstack-agents-old-stable"
            nested = stable_residue / "first" / "second"
            nested.mkdir(parents=True)
            (nested / "expected.txt").write_text("expected\n", encoding="utf-8")
            stable_outcome = module._cleanup_residue(stable_residue)
            self.assertEqual(stable_outcome.status, "cleanup_complete")
            self.assertFalse(stable_residue.exists())

    def test_recursive_cleanup_preserves_replacement_after_binding_an_entry(self) -> None:
        module = load_module(CONFIGURER, "configure_for_recursive_cleanup_replacement_test")
        safe_fs = sys.modules["safe_fs"]
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="setup-pstack-bound-cleanup-test-") as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                residue = parent / ".pstack-agents-old-test"
                residue.mkdir()
                victim = residue / "victim"
                replacement = root / "replacement"
                expected_private = root / "expected-victim"
                if kind == "file":
                    victim.write_text("expected\n", encoding="utf-8")
                    replacement.write_text("replacement\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    (victim / "expected.txt").write_text("expected\n", encoding="utf-8")
                    replacement.mkdir()
                    (replacement / "replacement.txt").write_text("replacement\n", encoding="utf-8")
                original_rename = safe_fs.atomic_rename_noreplace_at
                swapped = False

                def replace_after_binding(parent_fd: int, source_name: str, target_name: str) -> None:
                    nonlocal swapped
                    original_rename(parent_fd, source_name, target_name)
                    if not swapped and source_name == "victim":
                        swapped = True
                        os.rename(target_name, expected_private, src_dir_fd=parent_fd)
                        os.rename(replacement, source_name, dst_dir_fd=parent_fd)

                safe_fs.atomic_rename_noreplace_at = replace_after_binding
                try:
                    with self.assertRaises(module.ConfigurationError) as raised:
                        module._cleanup_residue(residue)
                finally:
                    safe_fs.atomic_rename_noreplace_at = original_rename
                self.assertTrue(swapped)
                self.assertEqual(raised.exception.status, "cleanup_refused")
                if kind == "file":
                    self.assertEqual(victim.read_text(encoding="utf-8"), "replacement\n")
                else:
                    self.assertEqual((victim / "replacement.txt").read_text(encoding="utf-8"), "replacement\n")

        for kind in ("file", "directory"):
            with self.subTest(private_kind=kind), tempfile.TemporaryDirectory(prefix="setup-pstack-private-cleanup-test-") as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                residue = parent / ".pstack-agents-old-private"
                residue.mkdir()
                victim = residue / "victim"
                replacement = root / "replacement"
                expected_private = root / "expected-private"
                if kind == "file":
                    victim.write_text("expected\n", encoding="utf-8")
                    replacement.write_text("replacement\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    (victim / "expected.txt").write_text("expected\n", encoding="utf-8")
                    replacement.mkdir()
                    (replacement / "replacement.txt").write_text("replacement\n", encoding="utf-8")
                original_lstat = safe_fs.lstat_at
                swapped = False
                private_checks = 0

                def replace_private_quarantine(directory_fd: int, name: str) -> os.stat_result:
                    nonlocal private_checks, swapped
                    metadata = original_lstat(directory_fd, name)
                    if name.startswith(".pstack-cleanup-"):
                        private_checks += 1
                    if name.startswith(".pstack-cleanup-") and private_checks == 3 and not swapped:
                        swapped = True
                        os.rename(name, expected_private, src_dir_fd=directory_fd)
                        os.rename(replacement, name, dst_dir_fd=directory_fd)
                        metadata = original_lstat(directory_fd, name)
                    return metadata

                safe_fs.lstat_at = replace_private_quarantine
                try:
                    with self.assertRaises(module.ConfigurationError) as raised:
                        module._cleanup_residue(residue)
                finally:
                    safe_fs.lstat_at = original_lstat
                self.assertTrue(swapped)
                self.assertEqual(raised.exception.status, "cleanup_refused")
                self.assertEqual(raised.exception.recovery_state, "cleanup_recovery_pending")
                self.assertIsNotNone(raised.exception.recovery_receipt)
                self.assertIn("--expected-dev", raised.exception.retry or "")
                self.assertTrue(expected_private.exists())
                if kind == "file":
                    self.assertEqual(victim.read_text(encoding="utf-8"), "replacement\n")
                else:
                    self.assertEqual((victim / "replacement.txt").read_text(encoding="utf-8"), "replacement\n")

        for kind in ("file", "directory"):
            with self.subTest(private_final_kind=kind), tempfile.TemporaryDirectory(prefix="setup-pstack-private-final-test-") as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                residue = parent / ".pstack-agents-old-final"
                residue.mkdir()
                victim = residue / "victim"
                replacement = root / "replacement"
                if kind == "file":
                    victim.write_text("expected\n", encoding="utf-8")
                    replacement.write_text("replacement\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    (victim / "expected.txt").write_text("expected\n", encoding="utf-8")
                    replacement.mkdir()
                    (replacement / "replacement.txt").write_text("replacement\n", encoding="utf-8")
                swapped = False
                original_delete = safe_fs.os.unlink if kind == "file" else safe_fs.os.rmdir

                def replace_final_private(name: str, *, dir_fd: int | None = None) -> None:
                    nonlocal swapped
                    if not swapped and dir_fd is not None and name.startswith(".pstack-cleanup-"):
                        swapped = True
                        os.rename(name, root / "expected-final", src_dir_fd=dir_fd)
                        os.rename(replacement, name, dst_dir_fd=dir_fd)
                    original_delete(name, dir_fd=dir_fd)

                if kind == "file":
                    safe_fs.os.unlink = replace_final_private
                else:
                    safe_fs.os.rmdir = replace_final_private
                try:
                    outcome = module._cleanup_residue(residue)
                finally:
                    if kind == "file":
                        safe_fs.os.unlink = original_delete
                    else:
                        safe_fs.os.rmdir = original_delete
                self.assertEqual(outcome.status, "cleanup_complete")
                self.assertFalse(swapped)
                self.assertTrue(replacement.exists())
                self.assertFalse(residue.exists())

        with tempfile.TemporaryDirectory(prefix="setup-pstack-public-root-quarantine-test-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            residue = parent / ".pstack-agents-old-root"
            residue.mkdir()
            (residue / "expected.txt").write_text("expected\n", encoding="utf-8")
            replacement = root / "replacement-root"
            replacement.mkdir()
            (replacement / "operator.txt").write_text("keep me\n", encoding="utf-8")
            original_rename = safe_fs.atomic_rename_noreplace_at
            swapped = False

            def replace_after_root_quarantine(parent_fd: int, source_name: str, target_name: str) -> None:
                nonlocal swapped
                original_rename(parent_fd, source_name, target_name)
                if not swapped and source_name == residue.name:
                    swapped = True
                    os.rename(target_name, root / "expected-root", src_dir_fd=parent_fd)
                    os.rename(replacement, source_name, dst_dir_fd=parent_fd)

            safe_fs.atomic_rename_noreplace_at = replace_after_root_quarantine
            try:
                with self.assertRaises(module.ConfigurationError) as raised:
                    module._cleanup_residue(residue)
            finally:
                safe_fs.atomic_rename_noreplace_at = original_rename
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.status, "cleanup_refused")
            self.assertIsNotNone(raised.exception.recovery_receipt)
            self.assertEqual((residue / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual((root / "expected-root" / "expected.txt").read_text(encoding="utf-8"), "expected\n")

    def test_final_quarantine_race_preserves_competitor_and_reports_receipt(self) -> None:
        module = load_module(CONFIGURER, "configure_for_final_quarantine_race_test")
        safe_fs = sys.modules["safe_fs"]
        for kind in ("file", "empty-directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="setup-pstack-final-quarantine-race-test-") as temporary:
                root = Path(temporary)
                parent = root / "parent"
                parent.mkdir()
                residue = parent / ".pstack-agents-old-final"
                residue.mkdir()
                victim = residue / "victim"
                competitor = root / "competitor"
                if kind == "file":
                    victim.write_text("expected\n", encoding="utf-8")
                    competitor.write_text("competitor\n", encoding="utf-8")
                else:
                    victim.mkdir()
                    competitor.mkdir()
                original_delete = safe_fs._native_unlink_at
                swapped = False

                def replace_after_quarantine_check(
                    directory_fd: int,
                    name: str,
                    *,
                    is_directory: bool,
                    **kwargs: object,
                ) -> None:
                    nonlocal swapped
                    if name == "entry" and not swapped:
                        swapped = True
                        os.rename(name, root / "expected-private", src_dir_fd=directory_fd)
                        os.rename(competitor, name, dst_dir_fd=directory_fd)
                    original_delete(
                        directory_fd,
                        name,
                        is_directory=is_directory,
                        **kwargs,
                    )

                safe_fs._native_unlink_at = replace_after_quarantine_check
                try:
                    with self.assertRaises(module.ConfigurationError) as raised:
                        module._cleanup_residue(residue)
                finally:
                    safe_fs._native_unlink_at = original_delete

                self.assertTrue(swapped)
                self.assertEqual(raised.exception.status, "cleanup_refused")
                self.assertEqual(raised.exception.recovery_state, "cleanup_recovery_pending")
                self.assertIsNotNone(raised.exception.recovery_receipt)
                self.assertIn("--expected-dev", raised.exception.retry or "")
                quarantine_dirs = sorted(residue.glob(".pstack-dispose-*"))
                self.assertEqual(len(quarantine_dirs), 1)
                preserved_competitor = quarantine_dirs[0] / "entry"
                self.assertTrue(preserved_competitor.exists())
                if kind == "file":
                    self.assertEqual(preserved_competitor.read_text(encoding="utf-8"), "competitor\n")
                else:
                    self.assertTrue(preserved_competitor.is_dir())
                    self.assertEqual(list(preserved_competitor.iterdir()), [])

    def test_status_minus_47_fallback_does_not_delete_a_replacement_after_claim(self) -> None:
        safe_fs = load_module(SAFE_FS, "safe_fs_minus_47_fallback_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-minus-47-race-test-") as temporary:
            root = Path(temporary)
            victim = root / "victim"
            competitor = root / "competitor"
            victim.write_text("expected\n", encoding="utf-8")
            competitor.write_text("competitor\n", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY)
            original_lstat = safe_fs.lstat_at
            original_delete = safe_fs._darwin_delete_reference
            swapped = False

            def fail_reference(reference: object, name: str) -> None:
                raise OSError(errno.EIO, "FSDeleteObject failed with status -47", name)

            def swap_after_first_claim(directory_fd: int, name: str) -> os.stat_result:
                nonlocal swapped
                metadata = original_lstat(directory_fd, name)
                if name.startswith(".pstack-unlink-") and not swapped:
                    swapped = True
                    os.rename(name, "expected-private", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    os.rename(competitor, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                return metadata

            safe_fs._darwin_delete_reference = fail_reference
            safe_fs.lstat_at = swap_after_first_claim
            try:
                with self.assertRaises(safe_fs.SafeFilesystemError):
                    safe_fs._native_unlink_at(
                        parent_fd,
                        "victim",
                        is_directory=False,
                        expected_metadata=original_lstat(parent_fd, "victim"),
                        identity_reference=safe_fs._DarwinFSRef(),
                    )
            finally:
                safe_fs.lstat_at = original_lstat
                safe_fs._darwin_delete_reference = original_delete
                os.close(parent_fd)

            self.assertTrue(swapped)
            self.assertEqual(victim.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual((root / "expected-private").read_text(encoding="utf-8"), "expected\n")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin FSRef behavior is platform-specific")
    def test_darwin_reference_rejects_a_renamed_directory_path(self) -> None:
        safe_fs = load_module(SAFE_FS, "safe_fs_darwin_path_binding_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-darwin-path-race-test-") as temporary:
            root = Path(temporary)
            original = root / "private-dir"
            replacement = root / "replacement-dir"
            original.mkdir()
            replacement.mkdir()
            (original / "entry").write_text("expected\n", encoding="utf-8")
            (replacement / "entry").write_text("replacement\n", encoding="utf-8")
            directory_fd = os.open(original, safe_fs._DIRECTORY_FLAGS)
            expected_metadata = safe_fs.lstat_at(directory_fd, "entry")
            original_path_for_fd = safe_fs._darwin_path_for_fd
            swapped = False

            def swap_directory_path(fd: int) -> bytes:
                nonlocal swapped
                path = original_path_for_fd(fd)
                if not swapped:
                    swapped = True
                    os.rename(original, root / "moved-private")
                    os.rename(replacement, original)
                return path

            safe_fs._darwin_path_for_fd = swap_directory_path
            try:
                with self.assertRaises(safe_fs.SafeFilesystemError):
                    safe_fs._darwin_identity_reference_at(
                        directory_fd,
                        "entry",
                        expected_metadata,
                        receipt_path=root / "receipt" / "entry",
                    )
            finally:
                safe_fs._darwin_path_for_fd = original_path_for_fd
                os.close(directory_fd)

            self.assertTrue(swapped)
            self.assertEqual((root / "moved-private" / "entry").read_text(encoding="utf-8"), "expected\n")
            self.assertEqual((root / "private-dir" / "entry").read_text(encoding="utf-8"), "replacement\n")

    def test_status_minus_47_fallback_does_not_delete_a_replacement_after_second_claim(self) -> None:
        safe_fs = load_module(SAFE_FS, "safe_fs_minus_47_second_claim_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-minus-47-second-race-test-") as temporary:
            root = Path(temporary)
            victim = root / "victim"
            competitor = root / "competitor"
            victim.write_text("expected\n", encoding="utf-8")
            competitor.write_text("competitor\n", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY)
            original_lstat = safe_fs.lstat_at
            original_delete = safe_fs._darwin_delete_reference
            swapped = False

            def fail_reference(reference: object, name: str) -> None:
                raise OSError(errno.EIO, "FSDeleteObject failed with status -47", name)

            def swap_after_second_claim(directory_fd: int, name: str) -> os.stat_result:
                nonlocal swapped
                metadata = original_lstat(directory_fd, name)
                if name.startswith(".pstack-delete-") and not swapped:
                    swapped = True
                    os.rename(name, "expected-private", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    os.rename(competitor, "victim", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                return metadata

            safe_fs._darwin_delete_reference = fail_reference
            safe_fs.lstat_at = swap_after_second_claim
            try:
                with self.assertRaises(safe_fs.SafeFilesystemError):
                    safe_fs._native_unlink_at(
                        parent_fd,
                        "victim",
                        is_directory=False,
                        expected_metadata=original_lstat(parent_fd, "victim"),
                        identity_reference=safe_fs._DarwinFSRef(),
                    )
            finally:
                safe_fs.lstat_at = original_lstat
                safe_fs._darwin_delete_reference = original_delete
                os.close(parent_fd)

            self.assertTrue(swapped)
            self.assertEqual(victim.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual((root / "expected-private").read_text(encoding="utf-8"), "expected\n")

    def test_status_minus_47_fallback_does_not_delete_a_replacement_after_final_claim(self) -> None:
        safe_fs = load_module(SAFE_FS, "safe_fs_minus_47_final_claim_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-minus-47-final-race-test-") as temporary:
            root = Path(temporary)
            victim = root / "victim"
            competitor = root / "competitor"
            victim.write_text("expected\n", encoding="utf-8")
            competitor.write_text("competitor\n", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY)
            original_lstat = safe_fs.lstat_at
            original_delete = safe_fs._darwin_delete_reference
            swapped = False

            def fail_reference(reference: object, name: str) -> None:
                raise OSError(errno.EIO, "FSDeleteObject failed with status -47", name)

            def swap_after_final_claim(directory_fd: int, name: str) -> os.stat_result:
                nonlocal swapped
                metadata = original_lstat(directory_fd, name)
                if name.startswith(".pstack-final-") and not swapped:
                    swapped = True
                    os.rename(name, "expected-private", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    os.rename(competitor, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                return metadata

            safe_fs._darwin_delete_reference = fail_reference
            safe_fs.lstat_at = swap_after_final_claim
            try:
                with self.assertRaises(safe_fs.SafeFilesystemError):
                    safe_fs._native_unlink_at(
                        parent_fd,
                        "victim",
                        is_directory=False,
                        expected_metadata=original_lstat(parent_fd, "victim"),
                        identity_reference=safe_fs._DarwinFSRef(),
                    )
            finally:
                safe_fs.lstat_at = original_lstat
                safe_fs._darwin_delete_reference = original_delete
                os.close(parent_fd)

            self.assertTrue(swapped)
            self.assertEqual(victim.read_text(encoding="utf-8"), "competitor\n")
            self.assertEqual((root / "expected-private").read_text(encoding="utf-8"), "expected\n")

    def test_darwin_status_minus_47_disposal_replacement_fails_closed_with_receipt(self) -> None:
        safe_fs = load_module(SAFE_FS, "safe_fs_darwin_disposal_race_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-darwin-disposal-race-test-") as temporary:
            root = Path(temporary)
            victim = root / "victim"
            competitor = root / "competitor"
            victim.write_text("expected\n", encoding="utf-8")
            competitor.write_text("competitor\n", encoding="utf-8")
            parent_fd = os.open(root, os.O_RDONLY)
            original_platform = safe_fs.sys.platform
            original_rename = safe_fs.atomic_rename_noreplace_at
            original_delete = safe_fs._darwin_delete_reference
            original_cdll = safe_fs.ctypes.CDLL
            disposal_unlink_attempted = False

            class FakeUnlinkAt:
                def __call__(self, directory_fd: int, encoded_name: bytes, flags: int) -> int:
                    nonlocal disposal_unlink_attempted
                    name = os.fsdecode(encoded_name)
                    if not name.startswith(".pstack-dispose-"):
                        raise AssertionError(f"unexpected fallback unlink: {name}")
                    disposal_unlink_attempted = True
                    os.rename(name, "expected-private", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    os.rename(competitor, name, dst_dir_fd=directory_fd)
                    os.unlink(name, dir_fd=directory_fd)
                    return 0

            class FakeLibc:
                def __init__(self) -> None:
                    self.unlinkat = FakeUnlinkAt()

            def fail_reference(reference: object, name: str) -> None:
                raise OSError(errno.EIO, "FSDeleteObject failed with status -47", name)

            def fallback_rename(parent: int, source_name: str, target_name: str) -> None:
                os.rename(source_name, target_name, src_dir_fd=parent, dst_dir_fd=parent)

            safe_fs.sys.platform = "darwin"
            safe_fs.atomic_rename_noreplace_at = fallback_rename
            safe_fs._darwin_delete_reference = fail_reference
            safe_fs.ctypes.CDLL = lambda *args, **kwargs: FakeLibc()
            try:
                with self.assertRaises(safe_fs.SafeFilesystemError) as raised:
                    safe_fs._native_unlink_at(
                        parent_fd,
                        "victim",
                        is_directory=False,
                        expected_metadata=safe_fs.lstat_at(parent_fd, "victim"),
                        identity_reference=safe_fs._DarwinFSRef(),
                        receipt_path=root / "receipt" / "victim",
                    )
            finally:
                safe_fs.sys.platform = original_platform
                safe_fs.atomic_rename_noreplace_at = original_rename
                safe_fs._darwin_delete_reference = original_delete
                safe_fs.ctypes.CDLL = original_cdll
                os.close(parent_fd)

            self.assertFalse(disposal_unlink_attempted)
            self.assertEqual(victim.read_text(encoding="utf-8"), "expected\n")
            self.assertEqual(competitor.read_text(encoding="utf-8"), "competitor\n")
            receipt = raised.exception.cleanup_receipt
            self.assertIsNotNone(receipt)
            self.assertLessEqual(len(receipt.entries), 8)
            self.assertEqual(receipt.entries[-1].reason, "identity-delete-unavailable")

    def test_created_parent_recovery_receipt_is_bounded_and_deterministic(self) -> None:
        module = load_module(CONFIGURER, "configure_for_bounded_cleanup_receipt_test")
        safe_fs = sys.modules["safe_fs"]
        with tempfile.TemporaryDirectory(prefix="setup-pstack-cleanup-receipt-bound-test-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            parent_fd = os.open(parent, os.O_RDONLY)
            created: list[object] = []
            try:
                for index in range(12):
                    name = f"created-{index:02d}"
                    path = parent / name
                    path.mkdir()
                    metadata = safe_fs.lstat_at(parent_fd, name)
                    os.rename(path, root / f"original-{index:02d}")
                    path.mkdir()
                    (path / "operator.txt").write_text("keep me\n", encoding="utf-8")
                    created.append(safe_fs.CreatedDirectory(os.dup(parent_fd), name, path, metadata))
            finally:
                os.close(parent_fd)
            with self.assertRaises(safe_fs.SafeFilesystemError) as raised:
                safe_fs.cleanup_created_directories(created)
            receipt = raised.exception.cleanup_receipt
            self.assertIsNotNone(receipt)
            self.assertEqual(len(receipt.entries), 8)
            self.assertTrue(receipt.truncated)
            self.assertEqual([entry.path.name for entry in receipt.entries], [f"created-{index:02d}" for index in range(8)])
            self.assertEqual(receipt.render(), receipt.render())
            for index in range(12):
                self.assertEqual((parent / f"created-{index:02d}" / "operator.txt").read_text(encoding="utf-8"), "keep me\n")

            merged = safe_fs.merge_cleanup_receipts(receipt)
            self.assertIsNotNone(merged)
            self.assertTrue(merged.truncated)

    def test_cleanup_retry_refuses_a_replaced_residue_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-cleanup-retry-identity-test-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            residue = parent / ".pstack-agents-old-test"
            residue.mkdir()
            (residue / "expected.txt").write_text("expected\n", encoding="utf-8")
            expected = residue.stat()
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "operator.txt").write_text("keep me\n", encoding="utf-8")
            os.rename(residue, root / "original-residue")
            os.rename(replacement, residue)
            cleanup = run_script(
                CONFIGURER,
                "cleanup",
                "--residue",
                str(residue),
                "--expected-dev",
                str(expected.st_dev),
                "--expected-ino",
                str(expected.st_ino),
                "--json",
            )
            self.assertNotEqual(cleanup.returncode, 0)
            self.assertIn("identity changed", cleanup.stderr)
            self.assertEqual((residue / "operator.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual((root / "original-residue" / "expected.txt").read_text(encoding="utf-8"), "expected\n")

    def test_configuration_cleanup_failure_has_status_and_retry_command(self) -> None:
        module = load_module(CONFIGURER, "configure_for_cleanup_status_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-cleanup-status-test-") as temporary:
            root = Path(temporary)
            parent = root / "parent"
            parent.mkdir()
            residue = parent / ".pstack-agents-old-test"
            residue.mkdir()
            (residue / "expected.txt").write_text("expected\n", encoding="utf-8")
            original_remove = module.remove_tree_at

            def fail_cleanup(parent_fd: int, name: str, **kwargs: object) -> None:
                raise OSError("injected cleanup failure")

            module.remove_tree_at = fail_cleanup
            try:
                with self.assertRaises(module.ConfigurationError) as raised:
                    module._cleanup_residue(residue)
            finally:
                module.remove_tree_at = original_remove
            self.assertEqual(raised.exception.status, "cleanup_pending")
            self.assertEqual(raised.exception.residue, module.canonical_path(residue))
            self.assertIn("configure.py cleanup --residue", raised.exception.retry or "")
            self.assertIn("--expected-dev", raised.exception.retry or "")
            self.assertTrue((residue / "expected.txt").is_file())

    def test_configuration_updates_budget_and_default_without_losing_reviewed_choices(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-cli-test-") as temporary:
            root = Path(temporary)
            catalog = root / "models.json"
            efforts = [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]
            catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": model, "supported_reasoning_levels": efforts}
                            for model in ("codex-test", "codex-special", "codex-panel-a", "codex-panel-b", "codex-new")
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
            self.assertEqual(result["transaction"]["status"], "published")
            self.assertIn('model = "codex-test"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))
            unrelated = agents / "operator-notes.txt"
            unrelated.parent.mkdir(parents=True, exist_ok=True)
            unrelated.write_text("keep this file\n", encoding="utf-8")

            repeated = run_script(CONFIGURER, "preview", *apply_args[1:])
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual({item["status"] for item in json.loads(repeated.stdout)["agents"]}, {"unchanged"})

            changed_budget = run_script(CONFIGURER, "preview", *apply_args[1:-1], "--budget", "large", "--json")
            self.assertEqual(changed_budget.returncode, 0, changed_budget.stderr)
            self.assertEqual({item["status"] for item in json.loads(changed_budget.stdout)["agents"]}, {"update"})
            rejected_replacement = run_script(CONFIGURER, "apply", *apply_args[1:-1], "--budget", "large", "--json")
            self.assertNotEqual(rejected_replacement.returncode, 0)
            self.assertIn('model_reasoning_effort = "high"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))
            applied_budget = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "large",
                "--replace",
                "--json",
            )
            self.assertEqual(applied_budget.returncode, 0, applied_budget.stderr)
            self.assertEqual({item["effort"] for item in json.loads(applied_budget.stdout)["agents"]}, {"xhigh"})
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep this file\n")

            role_override = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "large",
                "--role",
                "feature=codex-special",
                "--replace",
                "--json",
            )
            self.assertEqual(role_override.returncode, 0, role_override.stderr)
            self.assertIn('model = "codex-special"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

            panel_override = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "large",
                "--role",
                "arena-runners=codex-panel-a,codex-panel-b,codex-panel-a,codex-panel-b",
                "--replace",
                "--json",
            )
            self.assertEqual(panel_override.returncode, 0, panel_override.stderr)
            panel_models = ["codex-panel-a", "codex-panel-b", "codex-panel-a", "codex-panel-b"]
            self.assertEqual(
                [
                    next(
                        line.split("=", 1)[1].strip().strip('"')
                        for line in (agents / f"pstack-arena-runners-{index}.toml").read_text(encoding="utf-8").splitlines()
                        if line.startswith("model =")
                    )
                    for index in range(1, 5)
                ],
                panel_models,
            )

            changed_default = run_script(
                CONFIGURER,
                "preview",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--default-model",
                "codex-new",
                "--json",
            )
            self.assertEqual(changed_default.returncode, 0, changed_default.stderr)
            changed_default_agents = json.loads(changed_default.stdout)["agents"]
            self.assertEqual(next(item["model"] for item in changed_default_agents if item["name"] == "pstack-bug-fix"), "codex-new")
            self.assertEqual(next(item["model"] for item in changed_default_agents if item["name"] == "pstack-feature"), "codex-special")
            self.assertEqual([item["model"] for item in changed_default_agents if item["role"] == "arena-runners"], panel_models)
            applied_default = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--default-model",
                "codex-new",
                "--replace",
                "--json",
            )
            self.assertEqual(applied_default.returncode, 0, applied_default.stderr)
            self.assertIn('model = "codex-new"', (agents / "pstack-bug-fix.toml").read_text(encoding="utf-8"))
            self.assertIn('model = "codex-special"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

            alias_override = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--role",
                "why-synthesizer=inherit-parent",
                "--replace",
                "--json",
            )
            self.assertEqual(alias_override.returncode, 0, alias_override.stderr)
            alias_contents = (agents / "pstack-why-synthesizer.toml").read_text(encoding="utf-8")
            self.assertNotIn("model =", alias_contents)
            self.assertNotIn("model_reasoning_effort =", alias_contents)

            reset = run_script(
                CONFIGURER,
                "preview",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--default-model",
                "inherit-parent",
                "--json",
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            reset_agents = json.loads(reset.stdout)["agents"]
            self.assertEqual({item["status"] for item in reset_agents}, {"update", "unchanged"})
            self.assertGreater(sum(item["status"] == "update" for item in reset_agents), 0)
            self.assertEqual({item["model"] for item in reset_agents}, {None})
            self.assertEqual({item["effort"] for item in reset_agents}, {None})
            applied_reset = run_script(
                CONFIGURER,
                "apply",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--default-model",
                "inherit-parent",
                "--replace",
                "--json",
            )
            self.assertEqual(applied_reset.returncode, 0, applied_reset.stderr)
            self.assertNotIn("model =", (agents / "pstack-feature.toml").read_text(encoding="utf-8"))
            self.assertNotIn("model_reasoning_effort =", (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

            alias_majority_default_change = run_script(
                CONFIGURER,
                "preview",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
                "--json",
            )
            self.assertEqual(alias_majority_default_change.returncode, 0, alias_majority_default_change.stderr)
            alias_majority_agents = json.loads(alias_majority_default_change.stdout)["agents"]
            self.assertEqual({item["status"] for item in alias_majority_agents}, {"unchanged"})
            self.assertEqual({item["model"] for item in alias_majority_agents}, {None})
            self.assertEqual({item["effort"] for item in alias_majority_agents}, {None})

            majority_agents = root / ".codex" / "majority-agents"
            majority_role_args: list[str] = []
            for role in (
                "feature",
                "refactoring",
                "bug-fix",
                "perf-issue",
                "hillclimb",
                "judgment-and-prose",
                "hardest-tasks",
                "how-explorer",
                "how-explainer",
            ):
                majority_role_args.extend(("--role", f"{role}=inherit-parent"))
            majority_apply = run_script(
                CONFIGURER,
                "apply",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(majority_agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
                *majority_role_args,
                "--json",
            )
            self.assertEqual(majority_apply.returncode, 0, majority_apply.stderr)
            majority_default_change = run_script(
                CONFIGURER,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(majority_agents),
                "--budget",
                "medium",
                "--default-model",
                "codex-new",
                "--json",
            )
            self.assertEqual(majority_default_change.returncode, 0, majority_default_change.stderr)
            majority_changed_agents = json.loads(majority_default_change.stdout)["agents"]
            self.assertEqual(
                {item["model"] for item in majority_changed_agents if item["role"] in {"feature", "refactoring", "bug-fix"}},
                {None},
            )
            self.assertEqual(
                next(item["model"] for item in majority_changed_agents if item["role"] == "why-investigators"),
                "codex-new",
            )

            rejected_upstream_role = run_script(
                CONFIGURER,
                "preview",
                *apply_args[1:-1],
                "--budget",
                "medium",
                "--role",
                "divergent=codex-test",
            )
            self.assertNotEqual(rejected_upstream_role.returncode, 0)
            self.assertIn("no active Codex consumer", rejected_upstream_role.stderr)

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

            config_parent_target = root / "config-parent-target"
            (config_parent_target / "agents").mkdir(parents=True)
            config_parent = root / "config-parent"
            os.symlink(str(config_parent_target), config_parent, target_is_directory=True)
            config_ancestor_link = run_script(
                CONFIGURER,
                "preview",
                "--catalog",
                str(catalog),
                "--agents-root",
                str(config_parent / "agents"),
                "--budget",
                "medium",
                "--default-model",
                "codex-test",
            )
            self.assertNotEqual(config_ancestor_link.returncode, 0)
            self.assertEqual(list((config_parent_target / "agents").iterdir()), [])

    def test_configuration_replacement_rolls_back_all_written_files(self) -> None:
        module = load_module(CONFIGURER, "configure_for_rollback_test")
        with tempfile.TemporaryDirectory(prefix="setup-pstack-config-rollback-test-") as temporary:
            root = Path(temporary)
            models = {"codex-test": module.Model("codex-test", ("low", "medium", "high", "xhigh"))}
            agents = root / ".codex" / "agents"
            initial = module.build_plan(models, agents, budget="medium", default_model="codex-test")
            module.apply_plan(initial, agents)
            before = {path.name: path.read_bytes() for path in agents.glob("pstack-*.toml")}
            replacement = module.build_plan(models, agents, budget="large")
            self.assertTrue(all(item.status == "update" for item in replacement))

            original_exchange = module.atomic_exchange_at

            def fail_publication(parent_fd: int, first_name: str, second_name: str) -> None:
                raise OSError("injected publish failure")

            module.atomic_exchange_at = fail_publication
            try:
                with self.assertRaises(module.ConfigurationError):
                    module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.atomic_exchange_at = original_exchange
            self.assertEqual({path.name: path.read_bytes() for path in agents.glob("pstack-*.toml")}, before)

            def fail_after_exchange(parent_fd: int, first_name: str, second_name: str) -> None:
                original_exchange(parent_fd, first_name, second_name)
                raise OSError("injected post-exchange reporting failure")

            module.atomic_exchange_at = fail_after_exchange
            try:
                outcome = module.apply_plan(replacement, agents, allow_replace=True)
            finally:
                module.atomic_exchange_at = original_exchange
            self.assertEqual(outcome.status, "published")
            self.assertIn('model_reasoning_effort = "xhigh"', (agents / "pstack-feature.toml").read_text(encoding="utf-8"))

    def test_verifier_allows_documented_unsupported_boundary_and_rejects_active_seam(self) -> None:
        with tempfile.TemporaryDirectory(prefix="setup-pstack-verifier-test-") as temporary:
            root = Path(temporary)
            skill = root / "skills" / "fixture"
            write_skill(skill, "fixture")
            skill_file = skill / "SKILL.md"

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n"
                "The Cursor ~/.cursor/rules target is unsupported; never write it. <!-- UNSUPPORTED-SEAM: Cursor path -->\n"
                "[reference](reference.md)\n"
                "[reference][runtime-doc]\n\n"
                "[runtime-doc]: reference.md\n",
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
                "---\nname: fixture\ndescription: fixture\n---\n\n[runtime][missing-reference]\n",
                encoding="utf-8",
            )
            missing_reference_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("unresolved Markdown reference" in error for error in missing_reference_errors))

            skill_file.write_text(
                "---\nname: fixture\n  malformed\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            malformed_frontmatter_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("invalid frontmatter" in error for error in malformed_frontmatter_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: foo: bar\n---\n",
                encoding="utf-8",
            )
            malformed_scalar_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("mapping syntax" in error for error in malformed_scalar_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: |\n---\n",
                encoding="utf-8",
            )
            block_scalar_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("not a supported scalar" in error for error in block_scalar_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: true\n---\n",
                encoding="utf-8",
            )
            typed_scalar_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("not a string scalar" in error for error in typed_scalar_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\nWrite ~/.cursor/rules/pstack-models.mdc.\n",
                encoding="utf-8",
            )
            errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("active unsupported seam" in error for error in errors))

            helper = skill / "scripts" / "worker.py"
            helper.parent.mkdir()
            helper.write_text("Call the Cursor Task tool for this work.\n", encoding="utf-8")
            helper_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in helper_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: \"bad\\q\"\n---\n",
                encoding="utf-8",
            )
            invalid_escape_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("invalid YAML escape" in error for error in invalid_escape_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n[empty]()\n",
                encoding="utf-8",
            )
            empty_link_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("empty destination" in error for error in empty_link_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n[bad](<reference.md>junk)\n",
                encoding="utf-8",
            )
            malformed_angle_link_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("malformed Markdown link destination" in error for error in malformed_angle_link_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n\n[bad](reference.md\n",
                encoding="utf-8",
            )
            unterminated_link_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("unterminated Markdown link" in error for error in unterminated_link_errors))

            skill_file.write_text(
                "---\nname: fixture\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            helper.write_text("run_in_background = true\n", encoding="utf-8")
            mjs_helper = skill / "scripts" / "worker.mjs"
            mjs_helper.write_text("run_in_background = true\n", encoding="utf-8")
            extensionless_helper = skill / "scripts" / "worker"
            extensionless_helper.write_text("unsupported capability\nrun_in_background = true\n", encoding="utf-8")
            format_errors = module.verify_port(root, run_helpers=False)
            background_errors = [error for error in format_errors if "background-agent setting" in error]
            self.assertGreaterEqual(len(background_errors), 3)

            helper.write_text("run_in_background = true # UNSUPPORTED-SEAM: background-agent setting\n", encoding="utf-8")
            mjs_helper.write_text("run_in_background = true // UNSUPPORTED-SEAM: background-agent setting\n", encoding="utf-8")
            extensionless_helper.write_text("run_in_background = true # UNSUPPORTED-SEAM: background-agent setting\n", encoding="utf-8")
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

            helper.write_text("result = Task(prompt, model=\"grok\")\n", encoding="utf-8")
            mjs_helper.write_text("from cursor_tools import Task\nTask(prompt)\n", encoding="utf-8")
            extensionless_helper.write_text("from cursor_tools import Task\n", encoding="utf-8")
            task_errors = module.verify_port(root, run_helpers=False)
            self.assertGreaterEqual(len([error for error in task_errors if "Cursor Task call" in error]), 3)

            helper.write_text("from cursor_tools import (\n    Task,\n    OtherTool,\n)\n", encoding="utf-8")
            multiline_task_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in multiline_task_errors))

            mjs_helper.write_text("from cursor_tools import Task  // UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            extensionless_helper.write_text("from cursor_tools import Task  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            helper.write_text("from cursor_tools import (\n    OtherTool as Task,\n)\n", encoding="utf-8")
            alias_only_task_errors = module.verify_port(root, run_helpers=False)
            self.assertFalse(any("Cursor Task call" in error for error in alias_only_task_errors))

            mjs_helper.write_text("from cursor_tools import Task  // UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            extensionless_helper.write_text("from cursor_tools import Task  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            helper.write_text(
                "from cursor_tools import (\n"
                "    # Task is only mentioned in a comment\n"
                "    OtherTool,\n"
                ")\n",
                encoding="utf-8",
            )
            comment_only_task_errors = module.verify_port(root, run_helpers=False)
            self.assertFalse(any("Cursor Task call" in error for error in comment_only_task_errors))

            helper.write_text(
                "from cursor_tools import (\n"
                "    OtherTool,  # Task is still only a comment here\n"
                "    Task,\n"
                ")\n",
                encoding="utf-8",
            )
            valid_commented_task_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in valid_commented_task_errors))

            helper.write_text("from cursor_tools import \\\n    Task,\n", encoding="utf-8")
            backslash_task_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in backslash_task_errors))

            helper.write_text("from cursor_tools import \\\n    Task,  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            mjs_helper.write_text("from cursor_tools import Task  // UNSUPPORTED-SEAM: Cursor Task\nTask(prompt)  // UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            extensionless_helper.write_text("from cursor_tools import Task  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

            helper.write_text("from cursor_tools \\\n    import Task,\n", encoding="utf-8")
            continued_module_import_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in continued_module_import_errors))

            helper.write_text("from cursor_tools \\\n    import (\n        Task,\n    )\n", encoding="utf-8")
            continued_parenthesized_import_errors = module.verify_port(root, run_helpers=False)
            self.assertTrue(any("Cursor Task call" in error for error in continued_parenthesized_import_errors))

            helper.write_text(
                "from cursor_tools import (\n"
                "    Task,\n"
                ")  # UNSUPPORTED-SEAM: Cursor Task\n",
                encoding="utf-8",
            )
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

            helper.write_text(
                "from cursor_tools import (\n"
                "    Task,  # UNSUPPORTED-SEAM: Cursor Task\n"
                ")\n",
                encoding="utf-8",
            )
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

            helper.write_text("result = Task(prompt, model=\"grok\")  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            mjs_helper.write_text("from cursor_tools import Task  // UNSUPPORTED-SEAM: Cursor Task\nTask(prompt)  // UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            extensionless_helper.write_text("from cursor_tools import Task  # UNSUPPORTED-SEAM: Cursor Task\n", encoding="utf-8")
            self.assertEqual(module.verify_port(root, run_helpers=False), [])

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_installed_skill_is_visible_to_isolated_codex_prompt_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-discovery-probe-") as temporary:
            root = Path(temporary)
            source = ROOT / "skills" / "setup-pstack"
            destination = root / ".agents" / "skills"
            codex_home = root / "codex-home"
            codex_home.mkdir()
            expected_skill = (destination / "setup-pstack" / "SKILL.md").resolve()
            expected_user_skill = (codex_home / "skills" / "setup-pstack" / "SKILL.md").resolve()

            negative = subprocess.run(
                ["codex", "debug", "prompt-input"],
                cwd=root,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(negative.returncode, 0, negative.stderr)
            self.assertNotIn(str(expected_skill), negative.stdout)
            self.assertNotIn(str(expected_user_skill), negative.stdout)

            installed = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--apply",
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_self_test = run_script(
                destination / "setup-pstack" / "scripts" / "install.py",
                "--self-test",
                cwd=root,
            )
            self.assertEqual(installed_self_test.returncode, 0, installed_self_test.stderr)
            installed_configure = destination / "setup-pstack" / "scripts" / "configure.py"
            installed_self_test = run_script(installed_configure, "self-test", cwd=root)
            self.assertEqual(installed_self_test.returncode, 0, installed_self_test.stderr)
            discovered = subprocess.run(
                ["codex", "debug", "prompt-input"],
                cwd=root,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(discovered.returncode, 0, discovered.stderr)
            self.assertIn(f"(file: {expected_skill})", discovered.stdout)
            self.assertIn("- setup-pstack: Discover models from the active Codex runtime", discovered.stdout)

            user_installed = run_script(
                INSTALLER,
                "--source",
                str(source),
                "--destination",
                str(codex_home / "skills"),
                "--apply",
            )
            self.assertEqual(user_installed.returncode, 0, user_installed.stderr)
            user_discovered = subprocess.run(
                ["codex", "debug", "prompt-input"],
                cwd=root,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(user_discovered.returncode, 0, user_discovered.stderr)
            self.assertIn(f"(file: {expected_user_skill})", user_discovered.stdout)


if __name__ == "__main__":
    unittest.main()
