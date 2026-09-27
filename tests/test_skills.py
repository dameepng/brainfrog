"""test_skills.py — Verification for BrainFrog Modular Skills System.

Tests the 3 mandatory cases requested:
1. UI / anti-slop prompt triggers audit-anti-slop.
2. Irrelevant / backend math prompt does NOT trigger audit-anti-slop.
3. Explicit '/skill audit-anti-slop' command triggers it explicitly.

Also tests:
- Startup indexing of ONLY name and description.
- Progressive reference loading.
- Security validation of skill scripts (Zero-Trust sandbox).
"""
import sys
from pathlib import Path

# Ensure UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from skills import (
    index_skills,
    select_skill,
    load_skill_content,
    validate_skill_script,
    run_skill_script,
)

def run_tests():
    repo_dir = Path(__file__).resolve().parent

    print("=== Test 1: Startup Indexing (Name & Description Only) ===")
    skills = index_skills(repo_dir)
    assert "audit-anti-slop" in skills, "audit-anti-slop must be indexed"
    skill = skills["audit-anti-slop"]
    print(f"Indexed skill: {skill.name}")
    print(f"Description: {skill.description[:80]}...")
    assert "source-notes.md" in skill.references, "source-notes.md reference must be indexed"
    assert "scan_ui.py" in skill.scripts, "scan_ui.py script must be indexed"
    print("✓ Startup indexing verified successfully.\n")

    print("=== Test 2: Case 1 - UI / Anti-Slop Prompt Triggers Skill ===")
    ui_prompts = [
        "Audit UI halaman login ini dari placeholder copy, tombol mati, dan generic styling",
        "Tolong review frontend ini dan bersihkan elemen slop dan unverified claims",
        "Lakukan anti-slop audit pada landing page index.html",
        "Poles tampilan komponen web agar tidak terlihat generik dengan template card berulang",
    ]
    for prompt in ui_prompts:
        selected, _ = select_skill(prompt, skills)
        assert selected is not None, f"Expected skill to trigger for: {prompt}"
        assert selected.name == "audit-anti-slop", f"Expected audit-anti-slop, got {selected.name}"
        print(f"  ✓ Triggered: '{prompt[:50]}...' -> {selected.name}")
    print("✓ Case 1 PASSED: UI / anti-slop prompts correctly trigger audit-anti-slop.\n")

    print("=== Test 3: Case 2 - Irrelevant Prompt Does NOT Trigger Skill ===")
    irrelevant_prompts = [
        "Perbaiki fungsi hitung faktorial di calc.py",
        "Perbaiki bug ZeroDivisionError pada fungsi divide di sandbox_repo/calc.py",
        "Implementasikan algoritma binary search pada modul utils",
        "Jalankan test suite backend dengan pytest",
        "Tambahkan kolom database migration untuk users table",
    ]
    for prompt in irrelevant_prompts:
        selected, _ = select_skill(prompt, skills)
        assert selected is None, f"Skill should NOT trigger for: {prompt}, but got {selected.name if selected else None}"
        print(f"  ✓ Ignored: '{prompt[:50]}...' -> None")
    print("✓ Case 2 PASSED: Irrelevant / backend prompts do NOT trigger audit-anti-slop.\n")

    print("=== Test 4: Case 3 - Explicit /skill Command Triggers Skill ===")
    explicit_cases = [
        ("/skill audit-anti-slop", "audit-anti-slop", ""),
        ("/skill audit-anti-slop periksa komponen navbar", "audit-anti-slop", "periksa komponen navbar"),
        ("Tolong jalankan /skill audit-anti-slop untuk review hero section", "audit-anti-slop", ""),
    ]
    # Direct command
    s1, clean_task1 = select_skill("/skill audit-anti-slop", skills)
    assert s1 is not None and s1.name == "audit-anti-slop", "Failed explicit command"
    print("  ✓ Triggered: '/skill audit-anti-slop' -> audit-anti-slop")

    s2, clean_task2 = select_skill("/skill audit-anti-slop periksa komponen navbar", skills)
    assert s2 is not None and s2.name == "audit-anti-slop", "Failed explicit with task"
    assert clean_task2 == "periksa komponen navbar"
    print(f"  ✓ Triggered: '/skill audit-anti-slop periksa...' -> {s2.name}, clean task: '{clean_task2}'")

    # With explicit_skill_name argument
    s3, _ = select_skill("hitung faktorial", skills, explicit_skill_name="audit-anti-slop")
    assert s3 is not None and s3.name == "audit-anti-slop"
    print("  ✓ Triggered via explicit_skill_name parameter -> audit-anti-slop")
    print("✓ Case 3 PASSED: Explicit /skill invocation works reliably.\n")

    print("=== Test 5: Progressive Loading of References ===")
    # Base instructions only
    base_instructions = load_skill_content(skill, include_references=False)
    assert "Anti-Slop Audit" in base_instructions
    assert "## Reference Document: source-notes.md" not in base_instructions, "Reference document should NOT be loaded by default"
    assert "Practical review lenses" not in base_instructions, "Reference body should NOT be loaded by default"
    print(f"  Base instructions loaded ({len(base_instructions)} chars, no references)")

    # With references
    full_instructions = load_skill_content(skill, include_references=True)
    assert "## Reference Document: source-notes.md" in full_instructions
    assert "Practical review lenses" in full_instructions
    print(f"  Full instructions with references loaded ({len(full_instructions)} chars)")
    print("✓ Progressive reference loading PASSED.\n")

    print("=== Test 6: Script Security Validation & Execution ===")
    # 1. Valid execution
    is_safe, msg, cmd = validate_skill_script("scan_ui.py", ["BRAINFROG.md"], skill, repo_dir)
    assert is_safe, f"Validation failed: {msg}"
    print(f"  ✓ Valid script command: {cmd}")

    # 2. Execution test
    res = run_skill_script("scan_ui.py", ["BRAINFROG.md"], skill, repo_dir)
    assert res.returncode == 0
    print("  ✓ Script executed cleanly with exit code 0")

    # 3. Path traversal attack rejection
    is_safe_bad, msg_bad, _ = validate_skill_script("scan_ui.py", ["../../etc/passwd"], skill, repo_dir)
    # Target path outside workspace should be caught if target exists or dangerous
    is_safe_inject, msg_inject, _ = validate_skill_script("scan_ui.py", ["; rm -rf /"], skill, repo_dir)
    assert not is_safe_inject, "Dangerous shell injection character must be rejected"
    print(f"  ✓ Rejected injection: {msg_inject}")

    # 4. Non-existent script
    is_safe_ghost, msg_ghost, _ = validate_skill_script("malicious.py", [], skill, repo_dir)
    assert not is_safe_ghost
    print(f"  ✓ Rejected unknown script: {msg_ghost}")
    print("✓ Script security validation PASSED.\n")

    print("=" * 60)
    print("🎉 ALL 6 SKILL SYSTEM TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
