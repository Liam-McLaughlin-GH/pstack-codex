from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "route_task.py"
SKILL = SKILL_DIR / "SKILL.md"
EVIDENCE = SKILL_DIR / "references" / "repository-evidence.md"


def run_route(request: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), request],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class RouteTaskTests(unittest.TestCase):
    def test_backend_request_routes_to_web_app(self) -> None:
        decision = run_route("Add a DRF serializer and focused API integration test for a ticket basket endpoint")
        self.assertEqual(decision["category"], "backend")
        self.assertEqual(decision["repositories"], ["web-app"])
        self.assertTrue(any(signal.startswith("backend:") for signal in decision["signals"]))


    def test_frontend_request_routes_to_modern_frontend(self) -> None:
        decision = run_route("Update the Next.js invoice page repository and service hook, then add a component test")
        self.assertEqual(decision["category"], "frontend")
        self.assertEqual(decision["repositories"], ["showpass-frontend"])


    def test_infrastructure_request_routes_to_web_k8s(self) -> None:
        decision = run_route("Change the Kubernetes ingress and HPA rollout in web-k8s")
        self.assertEqual(decision["category"], "infrastructure")
        self.assertEqual(decision["repositories"], ["web-k8s"])


    def test_documented_fabric_forms_route_to_web_k8s(self) -> None:
        for request in (
            "fab beta",
            "fab demo",
            "fab live",
            "fab beta web.rolling_update",
            "fab demo web.frontend.deploy_nextjs",
            "fab live web.rolling_update",
            "Run Fabric3 against beta",
            "Run Fabric 3 against beta",
        ):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "infrastructure")
                self.assertEqual(decision["repositories"], ["web-k8s"])


    def test_deployment_context_routes_to_web_k8s(self) -> None:
        for request in (
            "Deploy to production",
            "Production deploy",
            "Deploying to live",
            "Production deployment",
            "Review production environment",
            "Switch to beta environment",
            "deployed test against beta",
            "python -m tests deployed --env live",
            "Run web.frontend.deploy_nextjs",
        ):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "infrastructure")
                self.assertEqual(decision["repositories"], ["web-k8s"])


    def test_application_deployments_keep_application_and_infrastructure_owners(self) -> None:
        expected = {
            "Deploy the Next.js app to beta": ["showpass-frontend", "web-k8s"],
            "Deploy the AngularJS bridge to beta": ["web-app", "web-k8s"],
            "Deploy the Django service to beta": ["web-app", "web-k8s"],
        }
        for request, repositories in expected.items():
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "mixed")
                self.assertEqual(decision["repositories"], repositories)


    def test_mixed_request_preserves_flow_order(self) -> None:
        decision = run_route(
            "Change the Django ViewSet contract, update the Next.js repository and page, and adjust the beta Kubernetes rollout"
        )
        self.assertEqual(decision["category"], "mixed")
        self.assertEqual(decision["repositories"], ["web-app", "showpass-frontend", "web-k8s"])


    def test_legacy_frontend_request_stays_in_web_app(self) -> None:
        decision = run_route("Fix the AngularJS bridge template and gulp test")
        self.assertEqual(decision["category"], "legacy-frontend")
        self.assertEqual(decision["repositories"], ["web-app"])


    def test_explicit_legacy_frontend_overrides_generic_frontend_wording(self) -> None:
        for request in (
            "Fix the AngularJS frontend bridge",
            "Fix the legacy frontend template redirect",
        ):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "legacy-frontend")
                self.assertEqual(decision["repositories"], ["web-app"])


    def test_distinct_modern_signal_keeps_legacy_request_mixed(self) -> None:
        decision = run_route("Fix the AngularJS frontend bridge and update the React component")
        self.assertEqual(decision["category"], "mixed")
        self.assertEqual(decision["repositories"], ["web-app", "showpass-frontend"])


    def test_weak_terms_do_not_override_django_owner(self) -> None:
        for request in ("Fix a Django page", "Update a Django route"):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "backend")
                self.assertEqual(decision["repositories"], ["web-app"])


    def test_generic_frontend_and_client_terms_fail_closed(self) -> None:
        expected = {
            "Fix the client timeout": ("unknown", []),
            "Update the frontend page": ("unknown", []),
            "Update the frontend route": ("unknown", []),
            "Fix a database client timeout": ("backend", ["web-app"]),
            "Fix a client permission check": ("backend", ["web-app"]),
        }
        for request, (category, repositories) in expected.items():
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], category)
                self.assertEqual(decision["repositories"], repositories)


    def test_generic_angular_and_deployment_terms_fail_closed(self) -> None:
        for request in (
            "Fix angular momentum calculations",
            "beta",
            "demo",
            "live",
            "production",
            "deploy",
            "Deploy the app",
        ):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "unknown")
                self.assertEqual(decision["repositories"], [])


    def test_weak_terms_need_modern_context(self) -> None:
        for request in ("Review repository setup", "Update a page", "Update a route", "Edit a template"):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "unknown")
                self.assertEqual(decision["repositories"], [])


    def test_next_template_is_modern_frontend_not_legacy(self) -> None:
        decision = run_route("Update the Next.js template")
        self.assertEqual(decision["category"], "frontend")
        self.assertEqual(decision["repositories"], ["showpass-frontend"])


    def test_monitoring_and_environment_route_to_infrastructure(self) -> None:
        for request in ("Review the monitoring configuration", "Update environment configuration"):
            with self.subTest(request=request):
                decision = run_route(request)
                self.assertEqual(decision["category"], "infrastructure")
                self.assertEqual(decision["repositories"], ["web-k8s"])


    def test_unknown_request_requires_source_trace(self) -> None:
        decision = run_route("Write a project status summary")
        self.assertEqual(decision["category"], "unknown")
        self.assertEqual(decision["repositories"], [])
        self.assertIn("AGENTS.md", decision["next_step"])


    def test_empty_request_fails(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "   "],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("request must not be empty", result.stderr)


    def test_skill_has_frontmatter_and_local_references(self) -> None:
        skill_text = SKILL.read_text()
        lines = skill_text.splitlines()
        self.assertEqual(lines[0], "---")
        closing = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:closing])
        self.assertIn("name: repo-best-practices", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertIn("references/repository-evidence.md", skill_text)
        self.assertIn("scripts/route_task.py", skill_text)
        self.assertTrue(EVIDENCE.is_file())

    def test_local_skill_links_resolve(self) -> None:
        skill_text = SKILL.read_text()
        links = re.findall(r"\]\(([^)]+)\)", skill_text)
        local_links = [link for link in links if "://" not in link and not link.startswith("#")]
        self.assertGreaterEqual(len(local_links), 2)
        for link in local_links:
            self.assertTrue((SKILL_DIR / link).is_file(), link)

    def test_installed_helper_runs_from_another_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            code_home = temp_root / "codex-home"
            installed_skill = code_home / "skills" / "repo-best-practices"
            installed_skill.parent.mkdir(parents=True)
            installed_skill.symlink_to(SKILL_DIR, target_is_directory=True)
            target_repository = temp_root / "target-repository"
            target_repository.mkdir()

            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(code_home)
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(environment["CODEX_HOME"])
                        / "skills"
                        / "repo-best-practices"
                        / "scripts"
                        / "route_task.py"
                    ),
                    "Add a Django serializer",
                ],
                check=True,
                capture_output=True,
                cwd=target_repository,
                env=environment,
                text=True,
            )

            decision = json.loads(result.stdout)
            self.assertEqual(decision["category"], "backend")
            self.assertEqual(decision["repositories"], ["web-app"])
