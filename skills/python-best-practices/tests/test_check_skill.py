from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CHECKER_PATH = SKILL_DIR / "scripts" / "check_skill.py"
SPEC = importlib.util.spec_from_file_location("python_skill_checker", CHECKER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load the Python skill checker")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class RoutingRegressionTests(unittest.TestCase):
    def test_fabric3_forms_are_manual_review(self) -> None:
        for request in (
            "Review a Fabric3 task",
            "Review a Fabric 3 task",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "unsafe")

    def test_generic_production_and_live_operations_are_manual_review(self) -> None:
        for request in (
            "Run a production operation",
            "Review the live environment",
            "production",
            "live",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "unsafe")

    def test_legacy_bridge_signals_route_to_web_app(self) -> None:
        for request in (
            "Fix the legacy UI bridge",
            "Fix the AngularJS bridge",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-app")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

    def test_modern_and_legacy_request_keeps_both_repository_owners(self) -> None:
        request = "Fix the AngularJS bridge and update the React component"

        self.assertEqual(CHECKER.route_for_task(request), "ambiguous")
        self.assertEqual(
            CHECKER.repositories_for_task(request),
            ("web-app", "showpass-frontend"),
        )

    def test_existing_safe_source_routing_is_preserved(self) -> None:
        self.assertEqual(
            CHECKER.route_for_task(
                "Write a showpass-frontend Next.js component"
            ),
            "showpass-frontend",
        )
        self.assertEqual(CHECKER.route_for_task("fab --list"), "web-k8s")

    def test_production_and_live_wording_override_frontend_ownership(self) -> None:
        for request in (
            "Write a production showpass-frontend Next.js component",
            "Review a live packages/core component",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "unsafe")

    def test_registered_repository_tokens_require_a_real_suffix_boundary(self) -> None:
        for token in (
            "web-app",
            "web-k8s",
            "showpass-frontend",
            "packages/core",
            "packages/next-app",
            "packages/mobile",
            "packages/desktop",
        ):
            for suffix in ("-extra", "_extra", ".extra"):
                request = f"Change {token}{suffix}"
                with self.subTest(request=request):
                    self.assertEqual(CHECKER.route_for_task(request), "general")
                    self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_backend_and_infrastructure_word_cues_require_a_real_boundary(self) -> None:
        for request in (
            "Change django-extra",
            "Review the Showpass backend-extra",
            "Review a fabricated operation in beta",
            "Review a fabrication operation in beta",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "general")
                self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_showpass_framework_file_suffixes_do_not_claim_frontend_ownership(self) -> None:
        for request in (
            "Change a Showpass Next.jsx component",
            "Change a Showpass React.jsx component",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "general")
                self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_frontend_package_file_suffixes_do_not_claim_frontend_ownership(self) -> None:
        for package in ("core", "next-app", "mobile", "desktop"):
            for suffix in (".ts", ".extra"):
                request = f"Change packages/{package}{suffix}"
                with self.subTest(request=request):
                    self.assertEqual(CHECKER.route_for_task(request), "general")
                    self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_framework_only_component_requests_stay_general(self) -> None:
        for request in (
            "Change a React component",
            "Change a Next.js component",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "general")

    def test_showpass_or_repository_cues_route_modern_ui_to_frontend(self) -> None:
        for request in (
            "Change a Showpass Next.js component",
            "Change a showpass-frontend React component",
            "Change the packages/next-app component",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "showpass-frontend")

    def test_nextjs_bridge_wording_routes_bridge_only_work_to_web_app(self) -> None:
        for request in (
            "Fix the Next.js bridge",
            "Fix the Next.js redirect",
            "Fix the Next.js bridge-route",
            "Fix the Next.js bridge route",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-app")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

    def test_showpass_nextjs_bridge_wording_keeps_bridge_precedence(self) -> None:
        for request in (
            "Fix the Showpass Next.js bridge",
            "Fix the Showpass Next.js redirect",
            "Fix the Showpass Next.js bridge-route",
            "Fix the Showpass Next.js bridge route",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-app")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

    def test_nextjs_bridge_with_modern_ui_work_keeps_both_owners(self) -> None:
        for request in (
            "Fix the Next.js bridge and update the React component",
            "Fix the Next.js redirect and update the modern UI",
            "Fix the Next.js bridge and update the React client",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "ambiguous")
                self.assertEqual(
                    CHECKER.repositories_for_task(request),
                    ("web-app", "showpass-frontend"),
                )

    def test_showpass_nextjs_bridge_with_modern_ui_work_keeps_both_owners(self) -> None:
        for request in (
            "Fix the Showpass Next.js bridge and update the React component",
            "Fix the Showpass Next.js redirect and update the modern UI",
            "Fix the Showpass Next.js bridge-route and update the Next.js client",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "ambiguous")
                self.assertEqual(
                    CHECKER.repositories_for_task(request),
                    ("web-app", "showpass-frontend"),
                )

    def test_explicit_frontend_package_cues_route_to_frontend(self) -> None:
        for package in ("core", "next-app", "mobile", "desktop"):
            request = f"Change packages/{package}"
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "showpass-frontend")
                self.assertEqual(
                    CHECKER.repositories_for_task(request),
                    ("showpass-frontend",),
                )

    def test_unknown_frontend_package_cues_stay_general(self) -> None:
        request = "Change packages/unknown"

        self.assertEqual(CHECKER.route_for_task(request), "general")
        self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_showpass_backend_cue_routes_to_web_app(self) -> None:
        request = "Review the Showpass backend"

        self.assertEqual(CHECKER.route_for_task(request), "web-app")
        self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

    def test_natural_language_fabric_environment_operations_are_manual_review(self) -> None:
        for request in (
            "Run a Fabric operation in beta",
            "Use Fabric against the live environment",
            "Review a Fabric operation against production",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "unsafe")


class LedgerRegistrationTests(unittest.TestCase):
    def test_new_routing_cases_name_registered_source_rules(self) -> None:
        ledger = CHECKER.load_ledger()
        rule_ids = {rule["id"] for rule in ledger["rules"]}
        cases = {case["name"]: case for case in ledger["routing_cases"]}

        for name in (
            "fabric3-task",
            "fabric-3-task",
            "fabric-beta-operation",
            "legacy-ui-bridge",
            "angularjs-bridge",
            "nextjs-bridge-only",
            "nextjs-redirect-only",
            "nextjs-bridge-route-only",
            "nextjs-bridge-modern-ui",
            "showpass-nextjs-bridge-only",
            "showpass-nextjs-redirect-only",
            "showpass-nextjs-bridge-route-only",
            "showpass-nextjs-bridge-route-space-only",
            "showpass-nextjs-bridge-modern-ui",
            "showpass-nextjs-redirect-modern-ui",
            "showpass-nextjs-bridge-route-modern-ui",
            "frontend-core-package",
            "frontend-next-app-package",
            "frontend-mobile-package",
            "frontend-desktop-package",
            "showpass-backend",
            "production-frontend-source",
            "live-frontend-package-source",
            "mixed-angularjs-react",
        ):
            with self.subTest(name=name):
                self.assertIn("rule", cases[name])
                self.assertIn(cases[name]["rule"], rule_ids)


class CommandTableConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_text = CHECKER.SKILL_PATH.read_text(encoding="utf-8")
        self.ledger = CHECKER.load_ledger()

    def _row(self, command: dict[str, str]) -> str:
        return (
            "| `{status}` | `{command}` | `{route}`; source: `{citation}` |".format(
                **command
            )
        )

    def test_status_claim_must_match_the_ledger(self) -> None:
        command = self.ledger["commands"][0]
        stale_row = self._row(command).replace(
            "`%s`" % command["status"], "`UNSAFE: stale status / NOT RUN`", 1
        )
        candidate = self.skill_text.replace(self._row(command), stale_row, 1)

        self.assertTrue(CHECKER.check_documents(candidate, self.ledger))

    def test_safety_claim_must_match_the_ledger(self) -> None:
        command = self.ledger["commands"][6]
        contradictory_row = self._row(command).replace(
            command["status"], CHECKER.SAFE_COMMAND_STATUS, 1
        )
        candidate = self.skill_text.replace(self._row(command), contradictory_row, 1)

        self.assertTrue(CHECKER.check_documents(candidate, self.ledger))

    def test_route_claim_must_match_the_ledger(self) -> None:
        command = self.ledger["commands"][0]
        stale_row = self._row(command).replace("`web-app`", "`web-k8s`", 1)
        candidate = self.skill_text.replace(self._row(command), stale_row, 1)

        self.assertTrue(CHECKER.check_documents(candidate, self.ledger))

    def test_source_claim_must_match_the_ledger_even_when_citation_is_registered(self) -> None:
        command = self.ledger["commands"][0]
        registered_other_citation = self.ledger["rules"][1]["citations"][0]["citation"]
        stale_row = self._row(command).replace(
            command["citation"], registered_other_citation, 1
        )
        candidate = self.skill_text.replace(self._row(command), stale_row, 1)

        self.assertTrue(CHECKER.check_documents(candidate, self.ledger))


if __name__ == "__main__":
    unittest.main()
