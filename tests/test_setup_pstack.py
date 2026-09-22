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
            original_rename = module.os.rename

            def fail_publication(source_path: Path, target_path: Path, **kwargs: object) -> None:
                raise OSError("injected publication failure")

            module.os.rename = fail_publication
            try:
                with self.assertRaises(module.InstallError):
                    module.apply_install(source, destination)
            finally:
                module.os.rename = original_rename
            self.assertFalse((root / "new-root").exists())

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

            def swap_source_file(directory_fd: int, name: str) -> bytes:
                if name == "payload.txt":
                    source_file.unlink()
                    os.symlink(outside / "payload.txt", source_file)
                try:
                    return original_read(directory_fd, name)
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

            def fail_cleanup(parent_fd: int, name: str) -> None:
                nonlocal failed
                if not failed and name.startswith(".pstack-agents-"):
                    failed = True
                    raise OSError("injected old-generation cleanup failure")
                original_remove(parent_fd, name)

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
