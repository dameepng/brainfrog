"""Proof Inspection UX and Inspection Engine.

Implements BrainFrog Sprint 1.2: Proof UX and Reproducible Demo.
Product thesis: "Don't just trust the agent. Verify it."

Loads and displays authoritative proof evidence, clearly separating:
1. Stored proof artifact data (historical record from task execution)
2. Fresh deterministic re-verification and workspace disk integrity analysis
3. Authenticity boundary notice (unauthenticated JSON vs deterministic checks)

Provides meaningful exit codes:
- 0: VERIFIED (proof valid and verdict VERIFIED)
- 1: INVALID (corrupt, contradictory, tampered, or disk integrity failed)
- 2: INCOMPLETE / MISSING (proof file missing or evidence incomplete)
- 3: FAILED (proof validly records a failed, cancelled, or aborted task)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.runtime.proof import (
    FileProofStore,
    ProofArtifact,
    is_sensitive_path,
    redact_secrets,
)
from core.runtime.proof_verifier import (
    DeterministicProofVerifier,
    ProofVerificationReport,
    ProofVerificationStatus,
)

logger = logging.getLogger(__name__)


class ProofInspectionState(str, Enum):
    """Overall status of proof inspection."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    MISSING = "MISSING"


@dataclass
class ProofInspectionResult:
    """Complete result of a proof inspection invocation."""

    target: str
    state: ProofInspectionState
    exit_code: int
    workspace: Path
    proof: Optional[ProofArtifact] = None
    report: Optional[ProofVerificationReport] = None
    proof_path: Optional[Path] = None
    error_message: Optional[str] = None
    workspace_integrity_checked: bool = False
    workspace_integrity_passed: Optional[bool] = None

    @property
    def is_verified(self) -> bool:
        return self.state == ProofInspectionState.VERIFIED

    @property
    def is_failed(self) -> bool:
        return self.state == ProofInspectionState.FAILED

    @property
    def is_invalid(self) -> bool:
        return self.state == ProofInspectionState.INVALID

    @property
    def is_incomplete(self) -> bool:
        return self.state == ProofInspectionState.INCOMPLETE

    @property
    def is_missing(self) -> bool:
        return self.state == ProofInspectionState.MISSING


def inspect_proof(
    workspace: Union[str, Path],
    target: str,
    *,
    check_workspace: bool = False,
) -> ProofInspectionResult:
    """Inspect and deterministically verify a proof artifact by task ID or path.

    Fail-closed and strictly read-only: does not modify, create, or delete
    any application or workspace file.
    """
    ws_path = Path(workspace).resolve()
    target_clean = (target or "").strip()

    if not target_clean:
        return ProofInspectionResult(
            target="",
            state=ProofInspectionState.MISSING,
            exit_code=2,
            workspace=ws_path,
            error_message="No task ID or proof artifact path specified.",
            workspace_integrity_checked=check_workspace,
        )

    if "\0" in target_clean:
        return ProofInspectionResult(
            target=target_clean.replace("\0", "\\x00"),
            state=ProofInspectionState.INVALID,
            exit_code=1,
            workspace=ws_path,
            error_message="Target path contains embedded null bytes (malformed path).",
            workspace_integrity_checked=check_workspace,
        )

    verifier = DeterministicProofVerifier(ws_path, verify_disk_integrity=check_workspace)
    try:
        resolved_path, derived_task_id = verifier._resolve_target_path(target_clean)
    except Exception as resolve_err:
        return ProofInspectionResult(
            target=target_clean,
            state=ProofInspectionState.INVALID,
            exit_code=1,
            workspace=ws_path,
            error_message=f"Failed to resolve proof path: {resolve_err}",
            workspace_integrity_checked=check_workspace,
        )

    # 1. Run deterministic verification
    report = verifier.verify(target_clean, verify_disk_integrity=check_workspace)

    # Check disk integrity check result if requested
    disk_passed: Optional[bool] = None
    if check_workspace:
        disk_checks = [
            c for c in report.checks
            if c.check_name in ("current_workspace_disk_integrity", "disk_integrity")
        ]
        if disk_checks:
            disk_passed = disk_checks[0].status == ProofVerificationStatus.VALID
        else:
            disk_passed = False

    # 2. Check existence of proof file
    if not resolved_path.exists():
        if target_clean.lower() == "latest":
            msg = f"No proof artifacts found in storage directory '{resolved_path.parent}' for target 'latest'."
        else:
            msg = f"Proof artifact not found for '{target_clean}' at '{resolved_path}'."
        if report.reasons:
            msg = f"{msg} ({'; '.join(report.reasons)})"
        return ProofInspectionResult(
            target=target_clean,
            state=ProofInspectionState.MISSING,
            exit_code=2,
            workspace=ws_path,
            report=report,
            proof_path=resolved_path,
            error_message=msg,
            workspace_integrity_checked=check_workspace,
            workspace_integrity_passed=disk_passed,
        )

    # 3. Load proof JSON
    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            return ProofInspectionResult(
                target=target_clean,
                state=ProofInspectionState.INVALID,
                exit_code=1,
                workspace=ws_path,
                report=report,
                proof_path=resolved_path,
                error_message="Proof JSON root must be a dictionary/object.",
                workspace_integrity_checked=check_workspace,
                workspace_integrity_passed=disk_passed,
            )
        proof = ProofArtifact.from_dict(data)
    except json.JSONDecodeError as jde:
        return ProofInspectionResult(
            target=target_clean,
            state=ProofInspectionState.INVALID,
            exit_code=1,
            workspace=ws_path,
            report=report,
            proof_path=resolved_path,
            error_message=f"Malformed proof JSON syntax: {jde}",
            workspace_integrity_checked=check_workspace,
            workspace_integrity_passed=disk_passed,
        )
    except Exception as parse_err:
        return ProofInspectionResult(
            target=target_clean,
            state=ProofInspectionState.INVALID,
            exit_code=1,
            workspace=ws_path,
            report=report,
            proof_path=resolved_path,
            error_message=f"Failed to parse proof artifact structure: {parse_err}",
            workspace_integrity_checked=check_workspace,
            workspace_integrity_passed=disk_passed,
        )

    # 4. Determine final inspection state and exit code
    if report.overall_status == ProofVerificationStatus.INVALID:
        state = ProofInspectionState.INVALID
        exit_code = 1
    elif report.overall_status == ProofVerificationStatus.INCOMPLETE:
        state = ProofInspectionState.INCOMPLETE
        exit_code = 2
    elif report.overall_status == ProofVerificationStatus.VALID:
        if proof.verdict.status == "VERIFIED":
            state = ProofInspectionState.VERIFIED
            exit_code = 0
        else:
            state = ProofInspectionState.FAILED
            exit_code = 3
    else:
        state = ProofInspectionState.INVALID
        exit_code = 1

    return ProofInspectionResult(
        target=target_clean,
        state=state,
        exit_code=exit_code,
        workspace=ws_path,
        proof=proof,
        report=report,
        proof_path=resolved_path,
        workspace_integrity_checked=check_workspace,
        workspace_integrity_passed=disk_passed,
    )


def print_proof_inspection(
    result: ProofInspectionResult,
    console: Optional[Any] = None,
) -> None:
    """Render a rich, inspectable proof report to the terminal.

    Clearly separates stored historical facts from fresh verification analysis.
    """
    if console is None:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            print(format_proof_inspection_text(result))
            return

    from rich import box
    from rich.align import Align
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # Theme colors
    COLOR_SUCCESS = "#10b981"
    COLOR_ERROR = "#ef4444"
    COLOR_WARNING = "#f59e0b"
    COLOR_INFO = "#3b82f6"
    COLOR_ACCENT = "#8b5cf6"
    COLOR_MUTED = "#6b7280"
    COLOR_FG = "#e5e7eb"

    state_style_map = {
        ProofInspectionState.VERIFIED: {"color": COLOR_SUCCESS, "sym": "✓", "title": "VERIFIED"},
        ProofInspectionState.FAILED: {"color": COLOR_ERROR, "sym": "✗", "title": "FAILED"},
        ProofInspectionState.INCOMPLETE: {"color": COLOR_WARNING, "sym": "⚠", "title": "INCOMPLETE"},
        ProofInspectionState.INVALID: {"color": COLOR_ERROR, "sym": "✗", "title": "INVALID"},
        ProofInspectionState.MISSING: {"color": COLOR_WARNING, "sym": "○", "title": "MISSING PROOF"},
    }
    cfg = state_style_map.get(result.state, state_style_map[ProofInspectionState.INVALID])

    console.print()
    # Main Header Panel
    header_text = Text()
    header_text.append(f"{cfg['sym']} Task Verdict: ", style=f"bold {cfg['color']}")
    header_text.append(f"{cfg['title']}\n", style=f"bold {cfg['color']}")
    header_text.append("Target: ", style=f"bold {COLOR_MUTED}")
    header_text.append(f"{result.target}  ·  ", style=COLOR_FG)
    header_text.append("Workspace: ", style=f"bold {COLOR_MUTED}")
    header_text.append(f"{result.workspace}\n", style=COLOR_FG)

    if result.proof:
        header_text.append("Description: ", style=f"bold {COLOR_MUTED}")
        header_text.append(f"{redact_secrets(result.proof.task.description)}\n", style=f"italic {COLOR_FG}")
        header_text.append("Task ID: ", style=f"bold {COLOR_MUTED}")
        header_text.append(f"{result.proof.task.task_id}  ·  ", style=COLOR_ACCENT)
        header_text.append("Session: ", style=f"bold {COLOR_MUTED}")
        header_text.append(f"{result.proof.task.session_id or 'none'}  ·  ", style=COLOR_FG)
        header_text.append("Duration: ", style=f"bold {COLOR_MUTED}")
        header_text.append(f"{result.proof.task.duration_ms} ms", style=COLOR_FG)

    console.print(
        Panel(
            header_text,
            title="[bold]🐸 BrainFrog Proof Inspection[/bold]",
            subtitle="[italic]Don't just trust the agent. Verify it.[/italic]",
            border_style=cfg["color"],
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )

    # If missing or failed to parse, show diagnostic panel and return
    if not result.proof:
        console.print()
        err_msg = result.error_message or "Proof artifact could not be loaded."
        console.print(
            Panel(
                Text(f"• {err_msg}", style=COLOR_ERROR),
                title="[bold red]Proof Inspection Failure[/bold red]",
                border_style=COLOR_ERROR,
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        _print_authenticity_notice(console, COLOR_MUTED)
        return

    proof = result.proof

    # -------------------------------------------------------------------------
    # Section 1: Stored Proof Artifact Data
    # -------------------------------------------------------------------------
    console.print()
    console.print("[bold cyan]── Section 1: Stored Proof Artifact Data (Historical Record) ──[/bold cyan]")

    overview_table = Table(box=box.ROUNDED, border_style=COLOR_MUTED, show_header=False, padding=(0, 1))
    overview_table.add_column("Property", style=f"bold {COLOR_FG}", width=24)
    overview_table.add_column("Value")

    # Stored Verdict & Reason
    v_color = COLOR_SUCCESS if proof.verdict.status == "VERIFIED" else COLOR_ERROR
    overview_table.add_row("Stored Verdict", f"[{v_color} bold]{proof.verdict.status}[/{v_color} bold]")
    overview_table.add_row("Authoritative Reason", redact_secrets(proof.verdict.reason))

    # Transaction Outcome
    tx_status = (proof.changes.status or "UNKNOWN").upper()
    tx_color = COLOR_SUCCESS if tx_status == "COMMITTED" else (COLOR_WARNING if tx_status in ("ROLLED_BACK", "CANCELLED") else COLOR_ERROR)
    overview_table.add_row("Transaction ID", proof.changes.transaction_id or "(none)")
    overview_table.add_row("Transaction Status", f"[{tx_color} bold]{tx_status}[/{tx_color} bold]")

    # Proof artifact on disk
    if result.proof_path:
        try:
            rel_p = result.proof_path.relative_to(result.workspace)
            p_display = str(rel_p)
        except Exception:
            p_display = str(result.proof_path)
        overview_table.add_row("Proof File (JSON)", f"[bold {COLOR_ACCENT}]{p_display}[/bold {COLOR_ACCENT}]")

    # Git Provenance
    if proof.provenance and proof.provenance.is_git:
        prov = proof.provenance
        overview_table.add_row("Git Branch", prov.branch or "detached")
        overview_table.add_row("Initial HEAD SHA", prov.initial_head_sha or "unknown")
        overview_table.add_row("Final HEAD SHA", prov.final_head_sha or "unknown")
        if prov.commit_sha:
            overview_table.add_row("Task Commit SHA", f"[bold {COLOR_INFO}]{prov.commit_sha}[/bold {COLOR_INFO}]")
        overview_table.add_row("Dirty Before Task", str(prov.was_dirty_before))
    else:
        overview_table.add_row("Git Provenance", "Non-Git Workspace or Metadata Unavailable")

    console.print(overview_table)

    # Changed Files Table
    console.print()
    files_table = Table(
        title="[bold]File Modifications Recorded in Transaction Journal[/bold]",
        box=box.ROUNDED,
        border_style=COLOR_MUTED,
        header_style=f"bold {COLOR_ACCENT}",
        show_header=True,
        padding=(0, 1),
    )
    files_table.add_column("Path", style=COLOR_FG)
    files_table.add_column("Operation", justify="center", width=12)
    files_table.add_column("Before SHA-256", justify="center", width=16)
    files_table.add_column("After SHA-256", justify="center", width=16)
    files_table.add_column("Byte Delta", justify="right", width=14)

    if proof.changes.files:
        for f in proof.changes.files:
            b_hash = f.before_sha256[:12] if f.before_sha256 else "(none)"
            a_hash = f.after_sha256[:12] if f.after_sha256 else "(none)"
            d_str = f"+{f.byte_count_delta}" if f.byte_count_delta > 0 else str(f.byte_count_delta)
            path_display = f.path
            if is_sensitive_path(f.path):
                path_display += " [italic yellow](protected)[/italic yellow]"
            files_table.add_row(path_display, f.operation, b_hash, a_hash, f"{d_str} B")
    else:
        files_table.add_row("(No file modifications committed)", "—", "—", "—", "0 B")

    console.print(files_table)

    # Verification Execution Details
    console.print()
    exec_table = Table(
        title="[bold]Verification Command Execution & Process Evidence[/bold]",
        box=box.ROUNDED,
        border_style=COLOR_MUTED,
        show_header=False,
        padding=(0, 1),
    )
    exec_table.add_column("Property", style=f"bold {COLOR_FG}", width=24)
    exec_table.add_column("Value")

    v_ev = proof.verification
    cmd_str = v_ev.test_command or "(no test command configured)"
    exec_table.add_row("Verification Command", f"[bold {COLOR_FG}]{cmd_str}[/bold {COLOR_FG}]")

    exit_str = str(v_ev.exit_code) if v_ev.exit_code is not None else "(not run)"
    exit_color = COLOR_SUCCESS if v_ev.exit_code == 0 else COLOR_ERROR
    exec_table.add_row("Process Exit Code", f"[{exit_color} bold]{exit_str}[/{exit_color} bold]")

    if v_ev.process_evidence:
        p_ev = v_ev.process_evidence
        exec_table.add_row("Execution Duration", f"{p_ev.duration_ms} ms")
        exec_table.add_row("Timed Out", str(p_ev.timed_out))
        exec_table.add_row("Cancelled", str(p_ev.cancelled))

    console.print(exec_table)

    # Gate Checks Breakdown
    if v_ev.gate_checks:
        console.print()
        gate_table = Table(
            title="[bold]Verification Gate Invariant Checks[/bold]",
            box=box.ROUNDED,
            border_style=COLOR_MUTED,
            header_style=f"bold {COLOR_ACCENT}",
            show_header=True,
            padding=(0, 1),
        )
        gate_table.add_column("Invariant Gate", style=COLOR_FG)
        gate_table.add_column("Status", justify="center", width=14)
        for g_name, g_pass in v_ev.gate_checks.items():
            g_color = COLOR_SUCCESS if g_pass else COLOR_ERROR
            g_sym = "✓ PASS" if g_pass else "✗ FAIL"
            clean_name = g_name.replace("_", " ").title()
            gate_table.add_row(clean_name, f"[{g_color} bold]{g_sym}[/{g_color} bold]")
        console.print(gate_table)

    # Captured Process Outputs (redacted)
    if v_ev.stdout_summary or v_ev.stderr_summary:
        console.print()
        if v_ev.stdout_summary:
            out_scrubbed = redact_secrets(v_ev.stdout_summary)
            console.print(
                Panel(
                    Text(out_scrubbed, style=COLOR_FG),
                    title=f"[bold]Captured Standard Output ({'Truncated' if v_ev.stdout_truncated else 'Complete'})[/bold]",
                    border_style=COLOR_MUTED,
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        if v_ev.stderr_summary:
            err_scrubbed = redact_secrets(v_ev.stderr_summary)
            console.print(
                Panel(
                    Text(err_scrubbed, style=COLOR_ERROR),
                    title=f"[bold red]Captured Standard Error ({'Truncated' if v_ev.stderr_truncated else 'Complete'})[/bold red]",
                    border_style=COLOR_ERROR,
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )

    # -------------------------------------------------------------------------
    # Section 2: Fresh Deterministic Re-verification
    # -------------------------------------------------------------------------
    console.print()
    console.print("[bold yellow]── Section 2: Fresh Deterministic Re-verification (Live State Analysis) ──[/bold yellow]")

    if result.report:
        rep = result.report
        rep_status_map = {
            ProofVerificationStatus.VALID: {"color": COLOR_SUCCESS, "label": "VALID"},
            ProofVerificationStatus.INVALID: {"color": COLOR_ERROR, "label": "INVALID"},
            ProofVerificationStatus.INCOMPLETE: {"color": COLOR_WARNING, "label": "INCOMPLETE"},
        }
        r_cfg = rep_status_map.get(rep.overall_status, rep_status_map[ProofVerificationStatus.INVALID])

        re_table = Table(box=box.ROUNDED, border_style=COLOR_MUTED, show_header=False, padding=(0, 1))
        re_table.add_column("Property", style=f"bold {COLOR_FG}", width=24)
        re_table.add_column("Value")

        re_table.add_row(
            "Live Verifier Status",
            f"[{r_cfg['color']} bold]{r_cfg['label']}[/{r_cfg['color']} bold] "
            f"(Evaluated against current workspace)",
        )

        if result.workspace_integrity_checked:
            if result.workspace_integrity_passed:
                ws_val = f"[{COLOR_SUCCESS} bold]✓ MATCH[/{COLOR_SUCCESS} bold] (Current workspace matches post-proof hashes)"
            else:
                ws_val = f"[{COLOR_ERROR} bold]✗ MISMATCH[/{COLOR_ERROR} bold] (Disk modifications detected after proof generation!)"
            re_table.add_row("Workspace Disk Integrity", ws_val)
        else:
            re_table.add_row("Workspace Disk Integrity", "[italic]Not checked (pass --check-workspace to verify disk bytes)[/italic]")

        console.print(re_table)

        # Verification Checks Breakdown
        if rep.checks:
            console.print()
            check_table = Table(
                title="[bold]Deterministic Verifier Invariant Evaluations[/bold]",
                box=box.ROUNDED,
                border_style=COLOR_MUTED,
                header_style=f"bold {COLOR_ACCENT}",
                show_header=True,
                padding=(0, 1),
            )
            check_table.add_column("Invariant Check", style=COLOR_FG, width=28)
            check_table.add_column("Evaluation", justify="center", width=14)
            check_table.add_column("Message / Diagnostics")

            for c in rep.checks:
                c_col = COLOR_SUCCESS if c.status == ProofVerificationStatus.VALID else COLOR_ERROR
                c_lbl = "VALID" if c.status == ProofVerificationStatus.VALID else c.status.value
                check_table.add_row(c.check_name, f"[{c_col} bold]{c_lbl}[/{c_col} bold]", c.message)
            console.print(check_table)

        # Diagnostic reasons if invalid or incomplete
        if rep.reasons:
            console.print()
            r_reasons = "\n".join(f"• {r}" for r in rep.reasons)
            console.print(
                Panel(
                    Text(r_reasons, style=COLOR_ERROR if rep.is_invalid else COLOR_WARNING),
                    title=f"[bold]{'Verification Failure' if rep.is_invalid else 'Verification Warning'}[/bold]",
                    border_style=COLOR_ERROR if rep.is_invalid else COLOR_WARNING,
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )

    _print_authenticity_notice(console, COLOR_MUTED)


def _print_authenticity_notice(console: Any, color: str) -> None:
    """Print the required authenticity and tamper notice boundary."""
    console.print()
    notice = (
        "ℹ Notice: Stored proof artifacts are unauthenticated historical JSON records.\n"
        "BrainFrog evaluates structural validity, gate checks, and SHA-256 disk consistency.\n"
        "It does NOT assert cryptographic non-repudiation or hardware-signed attestation."
    )
    from rich.text import Text
    console.print(Text(notice, style=f"italic {color}"))
    console.print()


def format_proof_inspection_text(result: ProofInspectionResult) -> str:
    """Format proof inspection as structured plain text."""
    lines: List[str] = []
    lines.append("=================================================================")
    lines.append(f"BrainFrog Proof Inspection: {result.state.value}")
    lines.append("=================================================================")
    lines.append(f"Target: {result.target}")
    lines.append(f"Workspace: {result.workspace}")
    lines.append(f"State: {result.state.value}")
    lines.append(f"Process Exit Code: {result.exit_code}")

    if result.error_message:
        lines.append(f"Error: {result.error_message}")

    if result.proof:
        p = result.proof
        lines.append("")
        lines.append("[Stored Proof Artifact Data]")
        lines.append(f"Task ID: {p.task.task_id}")
        lines.append(f"Description: {redact_secrets(p.task.description)}")
        lines.append(f"Stored Verdict: {p.verdict.status}")
        lines.append(f"Authoritative Reason: {redact_secrets(p.verdict.reason)}")
        lines.append(f"Transaction ID: {p.changes.transaction_id or 'none'}")
        lines.append(f"Transaction Status: {p.changes.status}")
        lines.append(f"Verification Command: {p.verification.test_command or 'none'}")
        lines.append(f"Exit Code: {p.verification.exit_code}")
        if p.verification.stdout_summary:
            lines.append(f"Stdout: {redact_secrets(p.verification.stdout_summary)}")
        if p.verification.stderr_summary:
            lines.append(f"Stderr: {redact_secrets(p.verification.stderr_summary)}")

        if p.changes.files:
            lines.append("Changed Files:")
            for f in p.changes.files:
                b = f.before_sha256[:12] if f.before_sha256 else "none"
                a = f.after_sha256[:12] if f.after_sha256 else "none"
                lines.append(f"  - {f.path} [{f.operation}] (before: {b}, after: {a}, delta: {f.byte_count_delta}B)")
        else:
            lines.append("Changed Files: (none)")

    if result.report:
        lines.append("")
        lines.append("[Fresh Re-verification]")
        lines.append(f"Verifier Status: {result.report.overall_status.value}")
        if result.workspace_integrity_checked:
            lines.append(f"Disk Integrity Check: {'PASS' if result.workspace_integrity_passed else 'FAIL'}")
        if result.report.reasons:
            lines.append("Reasons:")
            for r in result.report.reasons:
                lines.append(f"  - {r}")

    lines.append("")
    lines.append("Notice: Proof artifacts are unauthenticated JSON records evaluated via deterministic consistency.")
    return "\n".join(lines)
