"""Orchestrator: the loop that ties System 1 (Jev, fast gate) to
System 2 (Claude, slow generation) for an agentic coding task.

    plan (Claude)
      -> for each step:
           write code (Claude)
           run tests (real subprocess)
           ask Jev: next_action given {test_passed, retry_count, ...}
           branch on Jev's answer + confidence:
             open_pr        -> break out, go draft a PR
             retry_fix      -> Claude reads the failure, patches, loop again
             escalate_human -> stop, print a clear report, wait for a human
             abandon        -> stop, report why
      -> draft PR copy (Claude), gate once more on diff risk (Jev),
         then actually branch/commit/push (+ `gh pr create` if available
         and confidence clears the bar)

Every Jev call is one cheap request with a handful of typed questions —
never a free-text prompt. Claude is only called for planning, writing,
diagnosing failures, and writing PR prose: the things that genuinely
need open-ended generation.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import Domain, resolve_focus_tree
from system1.base import Answer, ChoiceQuestion, NoulQuestion, ScoreQuestion, SystemOneClient
from system2.claude_client import PlanStep, System2Client


@dataclass
class StepResult:
    step: PlanStep
    outcome: str  # "opened_pr" | "escalated" | "abandoned"
    retries: int
    detail: str


@dataclass
class RunConfig:
    repo_dir: Path
    task: str
    test_command: List[str]
    max_retries: int = 3
    auto_pr: bool = False
    pr_risk_ceiling: str = "medium"  # open PR automatically only up to this risk
    branch_prefix: str = "agentic/"
    domains: Dict[str, Domain] = field(default_factory=dict)
    min_domain_confidence: float = 0.55


@dataclass
class ScopeDecision:
    domain: Optional[Domain]
    change_type: str
    focus_tree: str
    clarify_message: Optional[str] = None


def _run(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _repo_tree(repo_dir: Path, max_files: int = 200) -> str:
    files = []
    for p in sorted(repo_dir.rglob("*")):
        if any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in p.parts):
            continue
        if p.is_file():
            files.append(str(p.relative_to(repo_dir)))
        if len(files) >= max_files:
            break
    return "\n".join(files)


def _read_files_from_tree(repo_dir: Path, tree: str, max_files: int = 40) -> Dict[str, str]:
    paths = [p for p in tree.splitlines() if p.strip()][:max_files]
    return _read_files(repo_dir, paths)


def _read_files(repo_dir: Path, paths: List[str]) -> Dict[str, str]:
    out = {}
    for rel in paths:
        f = repo_dir / rel
        out[rel] = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
    return out


def _write_files(repo_dir: Path, files: Dict[str, str]) -> None:
    for rel, content in files.items():
        f = repo_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")


def _diff_stat(repo_dir: Path) -> Dict[str, int]:
    res = _run(["git", "diff", "--numstat"], repo_dir)
    lines, files = 0, 0
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files += 1
            try:
                lines += int(parts[0]) + int(parts[1])
            except ValueError:
                pass
    return {"diff_lines_changed": lines, "files_changed": files}


NEXT_ACTION_QUESTION = ChoiceQuestion(
    instructions=(
        "Given the current step's test outcome and retry history, what should "
        "happen next in this coding agent's loop?"
    ),
    criteria={
        "open_pr": "Tests passed; the change is ready to be proposed as a pull request.",
        "retry_fix": "Tests failed but retries remain; worth having the engineer patch and retry.",
        "escalate_human": "Retries are exhausted or the situation is ambiguous; a human should look.",
        "abandon": "The approach is clearly wrong and retrying will not help.",
    },
)

RISK_QUESTION = ScoreQuestion(
    instructions="How risky is this diff to merge, based on its size and scope?",
    scale=["low", "medium", "high"],
)

SAFE_TO_PROCEED_QUESTION = NoulQuestion(
    instructions="Is it safe to proceed automatically (open a PR / continue) without a human checking first?"
)

CHANGE_TYPE_QUESTION = ChoiceQuestion(
    instructions="What kind of ask is this user prompt, on its own, with no code changes made yet?",
    criteria={
        "bug_investigation": "Reports something broken/failing and wants to know why or have it fixed.",
        "feature_request": "Asks for new behavior that doesn't exist yet.",
        "question_only": "Asks to understand or be shown something; not clearly asking for a code change.",
        "unclear": "Too vague to tell what's actually being asked for.",
    },
)

RISK_RANK = {"low": 0, "medium": 1, "high": 2}


class Orchestrator:
    def __init__(self, system1: SystemOneClient, system2: System2Client, config: RunConfig):
        self.s1 = system1
        self.s2 = system2
        self.cfg = config

    def run(self) -> List[StepResult]:
        scope = self._scope_gate()

        if scope.clarify_message:
            print(f"[system1/jev:{self.s1.name}] scope gate: not confident enough, asking the user")
            print(f"\n{scope.clarify_message}\n")
            return [StepResult(PlanStep("0", "scope clarification", []), "needs_clarification", 0, scope.clarify_message)]

        domain_label = scope.domain.key if scope.domain else "unscoped"
        print(f"[system1/jev:{self.s1.name}] scope gate: domain='{domain_label}' change_type='{scope.change_type}'")

        if scope.change_type == "question_only":
            focus_files = _read_files_from_tree(self.cfg.repo_dir, scope.focus_tree)
            print(f"[system2/claude] this reads as a question, not a change — diagnosing only ...")
            answer = self.s2.diagnose(self.cfg.task, focus_files, domain_label)
            print(f"\n{answer}\n")
            return [StepResult(PlanStep("0", "diagnosis", []), "diagnosed", 0, answer)]

        print(f"[system2/claude] planning task via {self.s2.model} (scoped to '{domain_label}') ...")
        steps = self.s2.plan_task(self.cfg.task, scope.focus_tree)
        print(f"[system2/claude] plan has {len(steps)} step(s)")

        results: List[StepResult] = []
        for step in steps:
            result = self._run_step(step, sensitive=bool(scope.domain and scope.domain.sensitive))
            results.append(result)
            if result.outcome in ("escalated", "abandoned"):
                break  # stop the whole run; don't push more steps on a shaky base
        return results

    def _scope_gate(self) -> ScopeDecision:
        """Ask Jev, before Claude sees anything, which part of the codebase
        this prompt is about and what kind of ask it is — so Claude gets a
        scoped file list instead of scanning the whole repo, and so an
        under-confident guess stops here instead of quietly going wrong.
        """
        if not self.cfg.domains:
            # no module map at all: nothing to route against, fall back to
            # the old behavior (full repo tree, no domain narrowing).
            return ScopeDecision(domain=None, change_type="unclear", focus_tree=_repo_tree(self.cfg.repo_dir))

        criteria = {key: d.description for key, d in self.cfg.domains.items()}
        criteria["unrelated"] = "Doesn't clearly match any of the other listed areas."
        domain_question = ChoiceQuestion(
            instructions="Which part of the codebase is this user prompt most likely about?",
            criteria=criteria,
        )
        state = {"user_prompt": self.cfg.task, "domain_options": list(criteria.keys())}
        answers = self.s1.decide(
            state, {"likely_domain": domain_question, "change_type": CHANGE_TYPE_QUESTION}
        )
        domain_answer = answers["likely_domain"]
        change_type = answers["change_type"].choice or "unclear"
        print(
            f"[system1/jev:{self.s1.name}] likely_domain = {domain_answer}, "
            f"change_type = {answers['change_type']}"
        )

        threshold = 0.35 if len(self.cfg.domains) == 1 else self.cfg.min_domain_confidence
        low_confidence = domain_answer.confidence < threshold
        unresolved = domain_answer.choice in (None, "unrelated")
        if low_confidence or unresolved:
            options = ", ".join(self.cfg.domains.keys())
            msg = (
                f"I'm not confident enough about which part of the codebase this is about "
                f"(best guess: '{domain_answer.choice}', confidence {domain_answer.confidence:.2f}). "
                f"Could you say which area this touches? Known areas: {options}. "
                f"Or just add more detail to the request."
            )
            return ScopeDecision(domain=None, change_type=change_type, focus_tree="", clarify_message=msg)

        domain = self.cfg.domains[domain_answer.choice]
        focus_tree = resolve_focus_tree(self.cfg.repo_dir, domain)
        if not focus_tree.strip():
            # domain matched but its configured paths don't exist on disk yet
            focus_tree = _repo_tree(self.cfg.repo_dir)
        return ScopeDecision(domain=domain, change_type=change_type, focus_tree=focus_tree)

    def _run_step(self, step: PlanStep, sensitive: bool = False) -> StepResult:
        print(f"\n=== Step {step.id}: {step.description} ===")
        file_contents = _read_files(self.cfg.repo_dir, step.files)

        print("[system2/claude] writing code ...")
        new_files = self.s2.write_code(step, self.cfg.task, file_contents)
        _write_files(self.cfg.repo_dir, new_files)

        retries = 0
        while True:
            test_proc = _run(self.cfg.test_command, self.cfg.repo_dir)
            passed = test_proc.returncode == 0
            output = (test_proc.stdout or "") + (test_proc.stderr or "")
            print(f"[tests] {'PASS' if passed else 'FAIL'} (exit {test_proc.returncode})")

            state: Dict[str, Any] = {
                "task": self.cfg.task,
                "step": step.description,
                "test_passed": passed,
                "test_summary": output[-800:],
                "retry_count": retries,
                "max_retries": self.cfg.max_retries,
                **_diff_stat(self.cfg.repo_dir),
            }
            answers = self.s1.decide(state, {"next_action": NEXT_ACTION_QUESTION})
            decision = answers["next_action"]
            print(f"[system1/jev:{self.s1.name}] next_action = {decision}")

            if decision.choice == "open_pr":
                return self._finalize_pr(step, retries, output, sensitive=sensitive)

            if decision.choice == "retry_fix" and retries < self.cfg.max_retries:
                retries += 1
                print(f"[system2/claude] reviewing failure, attempt {retries}/{self.cfg.max_retries} ...")
                current = _read_files(self.cfg.repo_dir, list(new_files.keys()) or step.files)
                fixed = self.s2.review_and_fix(self.cfg.task, step, current, output)
                _write_files(self.cfg.repo_dir, fixed)
                new_files.update(fixed)
                continue

            if decision.choice == "abandon":
                return StepResult(step, "abandoned", retries, "Jev classified this approach as unrecoverable.")

            # escalate_human, or retries exhausted, or low-confidence anything
            return StepResult(
                step,
                "escalated",
                retries,
                f"Stopped for human review after {retries} retr(ies). Last test output:\n{output[-1500:]}",
            )

    def _finalize_pr(self, step: PlanStep, retries: int, test_output: str, sensitive: bool = False) -> StepResult:
        stat = _diff_stat(self.cfg.repo_dir)
        pr_state = {"task": self.cfg.task, "test_passed": True, "sensitive_domain": sensitive, **stat}
        risk_answers = self.s1.decide(
            pr_state,
            {"diff_risk": RISK_QUESTION, "safe_to_proceed": SAFE_TO_PROCEED_QUESTION},
        )
        risk: Answer = risk_answers["diff_risk"]
        safe: Answer = risk_answers["safe_to_proceed"]
        print(f"[system1/jev:{self.s1.name}] diff_risk = {risk}, safe_to_proceed = {safe}")
        if sensitive:
            print("[orchestrator] this change touches a domain flagged sensitive=true in modules.json")

        changed = _run(["git", "diff", "--name-only"], self.cfg.repo_dir).stdout.split()
        pr_copy = self.s2.draft_pr(self.cfg.task, changed, "PASS" if not test_output.strip() else "PASS (see logs)")

        # a sensitive domain (auth, billing, migrations, ...) never gets to
        # skip human eyes, no matter how confident the gates are.
        ceiling = "low" if sensitive else self.cfg.pr_risk_ceiling
        safety_bar = 0.9 if sensitive else 0.7
        within_ceiling = RISK_RANK.get(risk.score, 2) <= RISK_RANK.get(ceiling, 0)
        auto_ok = (
            self.cfg.auto_pr
            and not sensitive
            and within_ceiling
            and safe.noul is not None
            and safe.noul >= safety_bar
        )

        if auto_ok:
            self._open_pr(step, pr_copy)
            detail = f"Auto-opened PR: {pr_copy['title']}"
        else:
            detail = (
                f"PR drafted but NOT auto-opened (risk={risk.score}, "
                f"safe_to_proceed={safe.noul:.2f} conf={safe.confidence:.2f}).\n"
                f"Title: {pr_copy['title']}\nBody:\n{pr_copy['body']}"
            )
            print("[orchestrator] " + detail)
        return StepResult(step, "opened_pr" if auto_ok else "drafted_pr", retries, detail)

    def _open_pr(self, step: PlanStep, pr_copy: Dict[str, str]) -> None:
        branch = f"{self.cfg.branch_prefix}{step.id}"
        _run(["git", "checkout", "-b", branch], self.cfg.repo_dir)
        _run(["git", "add", "-A"], self.cfg.repo_dir)
        _run(["git", "commit", "-m", pr_copy["title"]], self.cfg.repo_dir)
        push = _run(["git", "push", "-u", "origin", branch], self.cfg.repo_dir)
        if push.returncode != 0:
            print(f"[orchestrator] push failed (no remote configured?): {push.stderr}")
            return
        gh = _run(
            ["gh", "pr", "create", "--title", pr_copy["title"], "--body", pr_copy["body"]],
            self.cfg.repo_dir,
        )
        if gh.returncode != 0:
            print(f"[orchestrator] branch pushed, but `gh pr create` failed (is gh installed & authed?): {gh.stderr}")
        else:
            print(f"[orchestrator] PR opened: {gh.stdout.strip()}")
