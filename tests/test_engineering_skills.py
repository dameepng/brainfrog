"""Behavioral coverage for engineering skill routing and prompt integration.

Use the real loader/selector and normal Orchestrator constructor, without API
clients, a user's skills or persistent memory, or running project commands.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.memory import MemoryStore
from core.skills import SkillMetadata, index_skills, load_skill_content, select_skill
from orchestrator import Orchestrator, RunConfig
from system2.antigravity_client import AntigravitySystem2Client
from system2.claude_client import System2Client
from system2.openai_client import OpenAISystem2Client


NEW_SKILLS = (
    "understand-codebase",
    "implement-code-change",
    "refactor-code-safely",
    "review-code-change",
)


class EngineeringSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "unrelated-workspace"
        self.home.mkdir()
        self.workspace.mkdir()
        # Personal skill discovery sees an isolated home; integration tests
        # mock the specific persistent-memory loaders below.
        home_patch = patch("pathlib.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.skills = index_skills(self.workspace)

    def assert_routes(self, cases):
        for task, expected in cases:
            with self.subTest(task=task):
                selected, cleaned = select_skill(task, self.skills)
                self.assertEqual(selected.name if selected else None, expected)
                self.assertEqual(cleaned, task)

    def test_bundled_skills_discovered_outside_source_workspace(self):
        package_root = Path(__file__).resolve().parents[1]
        for name in NEW_SKILLS:
            with self.subTest(name=name):
                skill = self.skills[name]
                self.assertEqual(skill.skill_file, package_root / ".brainfrog" / "skills" / name / "SKILL.md")
                self.assertTrue(skill.description)
                content = load_skill_content(skill)
                self.assertIn(f"# Active Skill: {name}", content)
                self.assertNotIn("## Reference Document:", content)

    def test_primary_english_and_indonesian_actions(self):
        self.assert_routes([
            ("Explain how a request reaches the runtime.", "understand-codebase"),
            ("Explain the code about request routing", "understand-codebase"),
            ("Pahami struktur repo ini sebelum mengubah kode.", "understand-codebase"),
            ("gw mau lu pelajari codebase ini", "understand-codebase"),
            ("How does the parser handle empty input?", "understand-codebase"),
            ("Apa alur request di backend ini?", "understand-codebase"),
            ("Trace the API request through the backend.", "understand-codebase"),
            ("Tambahkan export JSON untuk hasil pekerjaan.", "implement-code-change"),
            ("Implement pagination according to the approved plan; preserve existing behavior elsewhere.", "implement-code-change"),
            ("Implementasikan algoritma binary search pada modul utils", "implement-code-change"),
            ("gw mau lu bikin endpoint untuk export JSON", "implement-code-change"),
            ("Refactor the session manager without changing behavior.", "refactor-code-safely"),
            ("Refactor session storage without changing behavior.", "refactor-code-safely"),
            ("Refactor provider retry logic safely.", "refactor-code-safely"),
            ("Tolong rapikan kode parser tanpa mengubah perilaku.", "refactor-code-safely"),
            ("Can you clean up the parser code?", "refactor-code-safely"),
            ("Tinjau perubahan kode ini untuk bug.", "review-code-change"),
            ("Audit the API diff for regressions.", "review-code-change"),
            ("gw mau lu review kode ini", "review-code-change"),
            ("Fix the failing parser test.", "diagnose-code-failure"),
            ("Perbaiki crash saat input kosong, lalu jalankan regression test.", "diagnose-code-failure"),
            ("Perbaiki bug ZeroDivisionError pada fungsi divide di calc.py", "diagnose-code-failure"),
            ("Investigate the failing parser test.", "diagnose-code-failure"),
            ("Add parser tests for malformed input.", "test-code-behavior"),
            ("Tambahkan regression test untuk perilaku parser yang sudah disepakati.", "test-code-behavior"),
            ("Write unit tests for the API handler.", "test-code-behavior"),
            ("Verify the completed code change against requirements.", "verify-code-result"),
            ("Verifikasi hasil perubahan kode ini; laporkan test yang dijalankan.", "verify-code-result"),
            ("Jalankan test suite backend dengan pytest", "verify-code-result"),
            ("Run pytest", "verify-code-result"),
            ("Check that tests pass", "verify-code-result"),
            ("Run python -m unittest", "verify-code-result"),
        ])

    def test_primary_action_wins_over_secondary_vocabulary(self):
        self.assert_routes([
            ("Review this bug fix", "review-code-change"),
            ("Fix a crash in the code review command", "diagnose-code-failure"),
            ("Implement retry logic and add tests", "implement-code-change"),
            ("Review this implementation and its tests for correctness. Do not modify code.", "review-code-change"),
            ("Tinjau diff ini untuk bug dan risiko; jangan ubah kode.", "review-code-change"),
            ("Refactor this implementation without changing behavior; its existing tests pass.", "refactor-code-safely"),
            ("Explain this code before adding tests.", "understand-codebase"),
            ("Plan the API change and its verification. Do not implement it yet.", "understand-codebase"),
            ("Write an implementation plan for the parser.", "understand-codebase"),
            ("Write a code review for the parser implementation.", "review-code-change"),
            ("Implement a verification_status field in the existing result object.", "implement-code-change"),
            ("Add a parser with tests for malformed inputs.", "implement-code-change"),
            ("Fix the broken API, then review the patch and add tests.", "diagnose-code-failure"),
            ("Review the parser diff.\nExample: implement the new API and test it.", "review-code-change"),
        ])

    def test_request_and_mode_prefixes_preserve_primary_action(self):
        self.assert_routes([
            ("Please explain the codebase.", "understand-codebase"),
            ("Could you please review this code?", "review-code-change"),
            ("I want you to add JSON export.", "implement-code-change"),
            ("Let's refactor the parser.", "refactor-code-safely"),
            ("Bisa kamu bantu saya untuk menambahkan test parser?", "test-code-behavior"),
            ("gw mau lu tolong benerin bug parser", "diagnose-code-failure"),
            ("[MODE: PLAN] Implement a new API endpoint.", "implement-code-change"),
            ("Plan only: add configurable retry support", "implement-code-change"),
            ("/plan review this code", "review-code-change"),
            ("Mode build: verifikasi perubahan kode ini", "verify-code-result"),
            ("In read-only mode: inspect the repository.", "understand-codebase"),
        ])

    def test_requires_a_primary_software_request(self):
        self.assert_routes([
            ("Apa itu unit testing?", None),
            ("What does code review mean?", None),
            ("Add butter to recipe", None),
            ("Write a poem about software", None),
            ("Review a book about Python", None),
            ("Tulis cerita tentang kode Python", None),
            ("Review hotel di Bali.", None),
            ("Tolong cek harga tiket kereta.", None),
            ("Build a garden shed.", None),
            ("Review this book; then fix the parser code.", None),
            ("Do not implement this code.", None),
            ("Thanks for the code review.", None),
            ("I read the implementation plan and the code review.", None),
        ])

    def test_explicit_and_slash_selection_override_automatic_intent(self):
        task = "Fix the failing parser test and remove UI slop"
        for name in NEW_SKILLS:
            with self.subTest(name=name):
                selected, cleaned = select_skill(task, self.skills, explicit_skill_name=name.upper())
                self.assertEqual(selected.name, name)
                self.assertEqual(cleaned, task)
        remainder = "Review the parser.\nDo not modify code."
        selected, cleaned = select_skill("/SKILL IMPLEMENT-CODE-CHANGE " + remainder, self.skills)
        self.assertEqual(selected.name, "implement-code-change")
        self.assertEqual(cleaned, remainder)
        command = "/skill understand-codebase"
        selected, cleaned = select_skill(command, self.skills)
        self.assertEqual(selected.name, "understand-codebase")
        self.assertEqual(cleaned, command)  # Existing name-only slash semantics.
        selected, _ = select_skill("Review the parser code", self.skills, explicit_skill_name="missing")
        self.assertEqual(selected.name, "review-code-change")

    def test_custom_description_fallback_and_missing_builtin(self):
        custom = SkillMetadata(
            name="custom-observe", description="quasar beacon telemetry",
            dir_path=self.workspace, skill_file=self.workspace / "SKILL.md",
        )
        available = dict(self.skills, **{custom.name: custom})
        selected, _ = select_skill("Investigate quasar beacon telemetry", available)
        self.assertIs(selected, custom)
        selected, _ = select_skill("Fix the parser bug", available, explicit_skill_name="custom-observe")
        self.assertIs(selected, custom)
        selected, cleaned = select_skill("/skill custom-observe Review the code", available)
        self.assertIs(selected, custom)
        self.assertEqual(cleaned, "Review the code")
        del available["implement-code-change"]
        selected, _ = select_skill("Implement quasar beacon telemetry in code", available)
        self.assertIs(selected, custom)
        self.assertEqual(select_skill("Review code", {}), (None, "Review code"))

    def test_builtin_selection_is_independent_of_index_order(self):
        task = "Review the implementation code and its tests before verification"
        for available in (self.skills, dict(reversed(list(self.skills.items())))):
            selected, _ = select_skill(task, available)
            self.assertEqual(selected.name, "review-code-change")

    def test_anti_slop_keeps_priority(self):
        self.assert_routes([
            ("Review frontend code for generic styling and dead buttons.", "audit-anti-slop"),
            ("Refactor UI components to remove anti-slop violations.", "audit-anti-slop"),
        ])

    def test_normal_orchestrator_injects_only_selected_main_body(self):
        cases = [
            ("Explain the parser code", "understand-codebase"),
            ("Implement a new API endpoint", "implement-code-change"),
            ("Refactor the parser code", "refactor-code-safely"),
            ("Review the API diff", "review-code-change"),
        ]
        for task, name in cases:
            with self.subTest(name=name), \
                    patch("orchestrator.get_guideline_files", return_value=[]), \
                    patch("orchestrator.load_project_guidelines", return_value="PROJECT_RULE_SENTINEL"), \
                    patch("core.memory.load_workspace_memory", return_value=MemoryStore()), \
                    patch("core.memory.load_global_memory", return_value=MemoryStore()):
                # No provider constructor: only the real composition methods
                # below run unbound against a minimal guidelines holder.
                provider = SimpleNamespace(provider_name="test", guidelines="")
                config = RunConfig(repo_dir=self.workspace, task=task)
                engine = Orchestrator(object(), provider, config)
                self.assertEqual(engine.active_skill.name, name)
                self.assertIn("PROJECT_RULE_SENTINEL", provider.guidelines)
                self.assertIn(load_skill_content(self.skills[name]), provider.guidelines)
                self.assertEqual(provider.guidelines.count("# Active Skill:"), 1)
                self.assertNotIn("## Reference Document:", provider.guidelines)
                for client_class in (System2Client, OpenAISystem2Client, AntigravitySystem2Client):
                    composed = client_class._apply_guidelines(provider, "BASE_SYSTEM_SENTINEL")
                    self.assertIn("BASE_SYSTEM_SENTINEL", composed)
                    self.assertIn(provider.guidelines, composed)

    def test_selection_does_not_upgrade_plan_mode(self):
        with patch("orchestrator.get_guideline_files", return_value=[]), \
                patch("orchestrator.load_project_guidelines", return_value=""), \
                patch("core.memory.load_workspace_memory", return_value=MemoryStore()), \
                patch("core.memory.load_global_memory", return_value=MemoryStore()):
            provider = SimpleNamespace(provider_name="test", guidelines="")
            config = RunConfig(repo_dir=self.workspace, task="Implement JSON export", mode="plan", auto_pr=True)
            engine = Orchestrator(object(), provider, config)
            self.assertEqual(engine.active_skill.name, "implement-code-change")
            self.assertEqual(config.mode, "plan")
            self.assertFalse(config.auto_pr)

    def test_contracted_orchestrator_never_loads_modular_skills(self):
        from core.runtime.approval import ApprovalService, CanonicalOperation, InMemoryApprovalStore
        from core.runtime.capabilities import Capabilities, FilesystemPolicy, NetworkPolicy
        from core.runtime.contract import ApprovedExecutionContract

        service = ApprovalService(InMemoryApprovalStore())
        request = service.create_request(
            session_id="session", session_incarnation_id="incarnation", channel="telegram",
            user_id="alice", conversation_id="chat", operation_type="write_file",
            canonical_operation=CanonicalOperation("write_file", "src/auth.py"),
            capabilities=Capabilities(
                FilesystemPolicy(read=("src/auth.py",), write=("src/auth.py",)),
                network=NetworkPolicy(True),
            ),
            workspace_root=str(self.workspace),
        )
        self.assertTrue(service.approve(request.request_id, "bob", "telegram")[0])
        self.assertTrue(service.verify_and_consume(
            request.request_id, request.operation_digest, "session", "telegram",
            session_incarnation_id="incarnation", requester_id="alice",
        )[0])
        contract = ApprovedExecutionContract.from_approval_request(request, self.workspace)
        config = RunConfig(
            repo_dir=self.workspace, task="Implement code in src/auth.py", skill="implement-code-change",
            execution_contract=contract, origin_channel="telegram", actor=contract.actor,
            session_id=contract.session_id, session_incarnation_id=contract.session_incarnation_id,
        )
        provider = SimpleNamespace(provider_name="test", guidelines="UNSCOPED_RULE_SENTINEL")
        with patch("core.skills.index_skills", side_effect=AssertionError("Unscoped skill index")), \
                patch("core.skills.load_skill_content", side_effect=AssertionError("Unscoped skill read")), \
                patch("orchestrator.get_guideline_files", side_effect=AssertionError("Unscoped guideline index")), \
                patch("orchestrator.load_project_guidelines", side_effect=AssertionError("Unscoped guideline read")):
            engine = Orchestrator(object(), provider, config)
        self.assertIsNone(engine.active_skill)
        self.assertFalse(engine.available_skills)
        self.assertEqual(provider.guidelines, "")


if __name__ == "__main__":
    unittest.main()
