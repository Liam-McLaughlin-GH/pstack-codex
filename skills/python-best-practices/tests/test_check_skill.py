from __future__ import annotations

import copy
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

PORT_SPEC = importlib.util.spec_from_file_location(
    "python_skill_port_checker",
    Path(__file__).resolve().parents[3] / "scripts" / "verify_port.py",
)
if PORT_SPEC is None or PORT_SPEC.loader is None:
    raise RuntimeError("could not load the shared port checker")
PORT_CHECKER = importlib.util.module_from_spec(PORT_SPEC)
PORT_SPEC.loader.exec_module(PORT_CHECKER)


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

    def test_exact_cues_accept_sentence_punctuation(self) -> None:
        for punctuation in (".", ",", "!", "?", ";", ":"):
            request = f"Review web-app{punctuation}"
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-app")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

        for request, expected_route, expected_repositories in (
            ("Review the Showpass backend.", "web-app", ("web-app",)),
            ("Change packages/core.", "showpass-frontend", ("showpass-frontend",)),
            ("Fix Next.js bridge.", "web-app", ("web-app",)),
            ("Fix the Showpass Next.js bridge.", "web-app", ("web-app",)),
            ("Fix Next.js bridge-route.", "web-app", ("web-app",)),
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), expected_route)
                self.assertEqual(
                    CHECKER.repositories_for_task(request), expected_repositories
                )

    def test_gcloud_read_only_is_safe_but_mutation_is_manual_review(self) -> None:
        for request in (
            "gcloud compute instances list",
            "gcloud auth list",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-k8s")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-k8s",))

        for request in (
            "gcloud compute instances create vm",
            "gcloud compute instances update vm",
            "gcloud compute instances delete vm",
            "gcloud storage rm gs://bucket/object",
            "gcloud projects remove-iam-policy-binding project",
            "gcloud auth revoke user@example.com",
            "gcloud compute instances stop vm",
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

    def test_natural_language_fabric_noun_stays_general(self) -> None:
        request = "Discuss the fabric of the application"

        self.assertEqual(CHECKER.route_for_task(request), "general")
        self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_documented_infrastructure_cues_route_to_web_k8s(self) -> None:
        for request in (
            "Review a Showpass Jinja template",
            "Review a Showpass Kubernetes manifest",
            "Review fabfile.py",
            "Review deploy/fabfile.py",
            "Review a Fabric task",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-k8s")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-k8s",))

    def test_fabricated_and_filename_decoys_stay_general(self) -> None:
        for request in (
            "Review a fabricated operation in beta",
            "Review a fabrication operation in beta",
            "Review a manufactured operation in beta",
            "Review fabric",
            "Review a kubectl-extra parser",
            "Review a gcloud-extra parser",
            "Review a Jinja-extra template",
            "Review a Kubernetes-extra manifest",
            "Review fabfile.py.bak",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "general")
                self.assertEqual(CHECKER.repositories_for_task(request), ())

    def test_bridge_precedes_generic_mixed_backend_frontend_cues(self) -> None:
        for request in (
            "Fix web-app Next.js bridge",
            "Fix Django Next.js bridge",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "web-app")
                self.assertEqual(CHECKER.repositories_for_task(request), ("web-app",))

    def test_explicit_frontend_bridge_cues_keep_both_owners(self) -> None:
        for request in (
            "Fix showpass-frontend Next.js bridge",
            "Fix packages/core Next.js bridge",
        ):
            with self.subTest(request=request):
                self.assertEqual(CHECKER.route_for_task(request), "ambiguous")
                self.assertEqual(
                    CHECKER.repositories_for_task(request),
                    ("web-app", "showpass-frontend"),
                )


class LedgerRegistrationTests(unittest.TestCase):
    def test_new_routing_cases_name_registered_source_rules(self) -> None:
        ledger = CHECKER.load_ledger()
        rule_ids = {rule["id"] for rule in ledger["rules"]}
        cases = {case["name"]: case for case in ledger["routing_cases"]}

        for name in (
            "fabric3-task",
            "fabric-3-task",
            "fabric-beta-operation",
            "gcloud-create",
            "gcloud-update",
            "gcloud-delete",
            "showpass-jinja-template",
            "showpass-kubernetes-manifest",
            "fabfile-python",
            "web-app-nextjs-bridge-only",
            "django-nextjs-bridge-only",
            "frontend-repository-nextjs-bridge-only",
            "frontend-package-nextjs-bridge-only",
            "gcloud-auth-list",
            "gcloud-storage-rm",
            "gcloud-projects-remove-iam-binding",
            "gcloud-auth-revoke",
            "gcloud-instances-stop",
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


class DocumentIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_text = CHECKER.SKILL_PATH.read_text(encoding="utf-8")
        self.ledger = CHECKER.load_ledger()

    def test_required_frontmatter_values_must_be_non_empty_scalar_strings(self) -> None:
        replacements = {
            "name": "name: python-best-practices",
            "description": next(
                line
                for line in self.skill_text.splitlines()
                if line.startswith("description:")
            ),
        }
        for field, original in replacements.items():
            for value in ("", "null", "~", "[]", "{}"):
                with self.subTest(field=field, value=value):
                    candidate = self.skill_text.replace(
                        original,
                        f"{field}: {value}",
                        1,
                    )
                    errors = CHECKER.check_documents(candidate, self.ledger)
                    self.assertTrue(errors)
                    self.assertTrue(any(field in error for error in errors))

    def test_shared_port_boundary_rejects_non_scalar_required_frontmatter(self) -> None:
        for field in ("name", "description"):
            for value in ("", "null", "~", "[]", "{}"):
                with self.subTest(field=field, value=value):
                    metadata = f"{field}: {value}\n"
                    errors = PORT_CHECKER.required_frontmatter_errors(metadata)
                    self.assertTrue(errors)
                    self.assertTrue(any(field in error for error in errors))

    def test_malformed_ledger_records_are_rejected_with_readable_errors(self) -> None:
        mutations = (
            ("top-level extra key", lambda candidate: candidate.__setitem__("unexpected", True), "unknown field"),
            (
                "repository missing field",
                lambda candidate: candidate["repositories"]["web-app"].pop("root"),
                "missing required field",
            ),
            (
                "repository extra key",
                lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                    "unexpected", True
                ),
                "unknown field",
            ),
            (
                "rule missing field",
                lambda candidate: candidate["rules"][0].pop("repo"),
                "missing required field",
            ),
            (
                "rule extra key",
                lambda candidate: candidate["rules"][0].__setitem__("unexpected", True),
                "unknown field",
            ),
            (
                "command missing field",
                lambda candidate: candidate["commands"][0].pop("rule"),
                "missing required field",
            ),
            (
                "command extra key",
                lambda candidate: candidate["commands"][0].__setitem__("unexpected", True),
                "unknown field",
            ),
            (
                "routing case missing field",
                lambda candidate: candidate["routing_cases"][0].pop("expected"),
                "missing required field",
            ),
            (
                "routing case extra key",
                lambda candidate: candidate["routing_cases"][0].__setitem__(
                    "unexpected", True
                ),
                "unknown field",
            ),
            (
                "routing case invalid safety",
                lambda candidate: candidate["routing_cases"][0].__setitem__(
                    "safety", "maybe"
                ),
                "unknown value",
            ),
        )
        for name, mutate, expected_fragment in mutations:
            with self.subTest(name=name):
                candidate = copy.deepcopy(self.ledger)
                mutate(candidate)
                errors = CHECKER.check_documents(self.skill_text, candidate)
                self.assertTrue(errors)
                self.assertTrue(
                    any(expected_fragment in error for error in errors),
                    errors,
                )

    def test_ledger_record_allowed_values_are_rejected(self) -> None:
        mutations = (
            (
                "repository root",
                lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                    "root", "wrong-root"
                ),
                "does not match the pinned evidence",
            ),
            (
                "rule route",
                lambda candidate: candidate["rules"][0].__setitem__(
                    "route", "not-a-route"
                ),
                "route has an unknown route",
            ),
            (
                "command safety",
                lambda candidate: candidate["commands"][0].__setitem__(
                    "safety", "maybe"
                ),
                "unknown safety classification",
            ),
            (
                "command route",
                lambda candidate: candidate["commands"][0].__setitem__(
                    "route", "not-a-route"
                ),
                "unknown route",
            ),
            (
                "routing case expected route",
                lambda candidate: candidate["routing_cases"][0].__setitem__(
                    "expected", "not-a-route"
                ),
                "expected has an unknown route",
            ),
            (
                "unsafe routing case rule",
                lambda candidate: candidate["routing_cases"][0].__setitem__(
                    "safety", "unsafe"
                ),
                "unsafe cases must expect unsafe",
            ),
        )
        for name, mutate, expected_fragment in mutations:
            with self.subTest(name=name):
                candidate = copy.deepcopy(self.ledger)
                mutate(candidate)
                errors = CHECKER.check_documents(self.skill_text, candidate)
                self.assertTrue(errors)
                self.assertTrue(
                    any(expected_fragment in error for error in errors),
                    errors,
                )

    def test_scenario_checker_rejects_malformed_ledger_shapes(self) -> None:
        for field, value in (
            ("rules", None),
            ("commands", []),
            ("routing_cases", {}),
        ):
            with self.subTest(field=field, value=value):
                candidate = copy.deepcopy(self.ledger)
                candidate[field] = value
                errors = CHECKER.check_scenarios(candidate, None)
                self.assertTrue(errors)
                self.assertTrue(any(field.replace("_", " ") in error for error in errors))

    def test_missing_local_references_are_rejected(self) -> None:
        missing_ledger = self.skill_text.replace(
            "references/source-ledger.json", "references/missing-ledger.json", 1
        )
        missing_checker = self.skill_text.replace(
            "skills/python-best-practices/scripts/check_skill.py",
            "skills/python-best-practices/scripts/missing-checker.py",
            1,
        )

        self.assertTrue(CHECKER.check_documents(missing_ledger, self.ledger))
        self.assertTrue(CHECKER.check_documents(missing_checker, self.ledger))

    def test_pinned_ledger_metadata_mutations_are_rejected(self) -> None:
        mutations = (
            lambda candidate: candidate.__setitem__(
                "upstream_sha_checked", "0" * 40
            ),
            lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                "root", "wrong-root"
            ),
            lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                "sha", "0" * 40
            ),
            lambda candidate: candidate["rules"][2].__setitem__(
                "route", "web-k8s"
            ),
            lambda candidate: candidate["rules"][2].__setitem__(
                "repo", "showpass-frontend"
            ),
            lambda candidate: candidate["rules"][0].__setitem__(
                "citation", candidate["rules"][0]["citations"][1]["citation"]
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                candidate = copy.deepcopy(self.ledger)
                mutate(candidate)
                self.assertTrue(CHECKER.check_documents(self.skill_text, candidate))


if __name__ == "__main__":
    unittest.main()
