"""Human-Readable Markdown Projection for BrainFrog Proof Artifacts.

Implements BrainFrog Phase 4 Markdown Report Generator.
Renders an inspectable, clean verification report from authoritative ProofArtifact facts.
Ensures zero secret leakage and strict workspace-relative paths.
"""
from __future__ import annotations

from typing import List
from core.runtime.proof import ProofArtifact, is_sensitive_path


def render_proof_markdown(artifact: ProofArtifact) -> str:
    """Render a GitHub-flavored markdown report representing the proof artifact."""
    lines: List[str] = []

    # Title & Header
    lines.append("# BrainFrog Verification Proof")
    lines.append("")
    lines.append(f"**Verdict:** `{artifact.verdict.status}`  ")
    lines.append(f"**Task ID:** `{artifact.task.task_id}`  ")
    lines.append(f"**Session ID:** `{artifact.task.session_id}`  ")
    lines.append(f"**Duration:** `{artifact.task.duration_ms} ms`  ")
    lines.append(f"**Completed At:** `{artifact.task.completed_at}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. Task
    lines.append("## 1. Task")
    lines.append(f"> {artifact.task.description}")
    lines.append("")

    # 2. Verdict
    lines.append("## 2. Verdict")
    lines.append(f"- **Status:** `{artifact.verdict.status}`")
    lines.append(f"- **Authoritative Reason:** {artifact.verdict.reason}")
    lines.append("")

    # 3. Verification Gate
    lines.append("## 3. Verification Gate")
    gate_checks = artifact.verification.gate_checks or {}
    lines.append("| Check | Evaluation |")
    lines.append("| :--- | :--- |")
    for check_name, passed in gate_checks.items():
        status_icon = "✓ Passed" if passed else "✗ Failed"
        clean_name = check_name.replace("_", " ").title()
        lines.append(f"| {clean_name} | {status_icon} |")
    lines.append("")

    # 4. Changes
    lines.append("## 4. Changes")
    lines.append(f"- **Transaction ID:** `{artifact.changes.transaction_id or 'none'}`")
    lines.append(f"- **Transaction Status:** `{artifact.changes.status}`")
    lines.append("")
    if artifact.changes.files:
        lines.append("| Path | Operation | Before SHA-256 | After SHA-256 | Byte Delta |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for f in artifact.changes.files:
            b_hash = f.before_sha256[:12] if f.before_sha256 else "(none)"
            a_hash = f.after_sha256[:12] if f.after_sha256 else "(none)"
            delta_str = f"+{f.byte_count_delta}" if f.byte_count_delta > 0 else str(f.byte_count_delta)
            lines.append(f"| `{f.path}` | `{f.operation}` | `{b_hash}` | `{a_hash}` | {delta_str} B |")
    else:
        lines.append("*(No disk modifications committed)*")
    lines.append("")

    # 5. Git Provenance
    lines.append("## 5. Git Provenance")
    if artifact.provenance and artifact.provenance.is_git:
        lines.append(f"- **Repository Root:** `{artifact.provenance.repo_root}`")
        lines.append(f"- **Branch:** `{artifact.provenance.branch or 'unknown'}`")
        lines.append(f"- **Initial HEAD SHA:** `{artifact.provenance.initial_head_sha or 'unknown'}`")
        lines.append(f"- **Final HEAD SHA:** `{artifact.provenance.final_head_sha or 'unknown'}`")
        if getattr(artifact.provenance, "commit_sha", None):
            lines.append(f"- **Task Commit SHA:** `{artifact.provenance.commit_sha}`")
        lines.append(f"- **Was Dirty Before:** `{artifact.provenance.was_dirty_before}`")
    else:
        lines.append("*(Non-Git Workspace or Git Metadata Unavailable)*")
    lines.append("")

    # 6. Reproducibility
    lines.append("## 6. Reproduce")
    lines.append(f"To independently verify this autonomous work in the workspace:")
    lines.append("```bash")
    lines.append(f"{artifact.reproducibility.command or '# (no test command provided)'}")
    lines.append("```")
    lines.append(f"- **Expected Exit Code:** `{artifact.reproducibility.expected_exit_code}`")
    if artifact.reproducibility.expected_file_hashes:
        lines.append("- **Expected File Checksums:**")
        for p, h in artifact.reproducibility.expected_file_hashes.items():
            lines.append(f"  - `{p}`: `{h}`")
    lines.append("")

    # 7. Verification Evidence
    lines.append("## 7. Verification Evidence")
    lines.append(f"- **Test Command:** `{artifact.verification.test_command or '(none)'}`")
    exit_code_display = artifact.verification.exit_code if artifact.verification.exit_code is not None else "(not run)"
    lines.append(f"- **Exit Code:** `{exit_code_display}`")
    lines.append("")

    lines.append("### stdout")
    if artifact.verification.stdout_summary:
        lines.append("```text")
        lines.append(artifact.verification.stdout_summary)
        lines.append("```")
        if artifact.verification.stdout_truncated:
            lines.append("*(stdout was truncated to comply with maximum bounded capture limits)*")
    else:
        lines.append("*(empty)*")
    lines.append("")

    lines.append("### stderr")
    if artifact.verification.stderr_summary:
        lines.append("```text")
        lines.append(artifact.verification.stderr_summary)
        lines.append("```")
        if artifact.verification.stderr_truncated:
            lines.append("*(stderr was truncated to comply with maximum bounded capture limits)*")
    else:
        lines.append("*(empty)*")
    lines.append("")

    return "\n".join(lines)
