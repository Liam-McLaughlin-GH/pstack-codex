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
                "Write a production showpass-frontend Next.js component"
            ),
            "showpass-frontend",
        )
        self.assertEqual(CHECKER.route_for_task("fab --list"), "web-k8s")


class LedgerRegistrationTests(unittest.TestCase):
    def test_new_routing_cases_name_registered_source_rules(self) -> None:
        ledger = CHECKER.load_ledger()
        rule_ids = {rule["id"] for rule in ledger["rules"]}
        cases = {case["name"]: case for case in ledger["routing_cases"]}

        for name in (
            "fabric3-task",
            "fabric-3-task",
            "legacy-ui-bridge",
            "angularjs-bridge",
            "mixed-angularjs-react",
        ):
            with self.subTest(name=name):
                self.assertIn("rule", cases[name])
                self.assertIn(cases[name]["rule"], rule_ids)


if __name__ == "__main__":
    unittest.main()
