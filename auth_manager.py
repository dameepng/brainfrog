"""Google Antigravity Multi-Account Authentication Manager.

Provides credential vault management, zero-friction account switching (<100ms),
automatic JWT email extraction, and session safety snapshots for Antigravity CLI.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def get_gemini_dir() -> Path:
    """Return the base ~/.gemini configuration directory."""
    custom = os.environ.get("GEMINI_HOME")
    if custom:
        return Path(custom).resolve()
    return (Path.home() / ".gemini").resolve()


def get_vault_dir() -> Path:
    """Return the profile vault directory (~/.gemini/profiles)."""
    vault = get_gemini_dir() / "profiles"
    vault.mkdir(parents=True, exist_ok=True)
    return vault


def decode_jwt_payload(jwt_str: str) -> Dict[str, Any]:
    """Safely decode JWT payload into a dictionary without external libraries."""
    if not jwt_str or not isinstance(jwt_str, str):
        return {}
    try:
        parts = jwt_str.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            rem = len(payload) % 4
            if rem > 0:
                payload += "=" * (4 - rem)
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8", errors="replace")
            return json.loads(decoded)
    except Exception:
        pass
    return {}


def extract_email_from_oauth_file(path: Path) -> Optional[str]:
    """Extract Google account email from an oauth_creds.json file via id_token JWT."""
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        id_token = data.get("id_token")
        if id_token:
            payload = decode_jwt_payload(id_token)
            email = payload.get("email")
            if email and isinstance(email, str) and email.strip():
                return email.strip().lower()
    except Exception:
        pass
    return None


def get_active_account() -> Optional[str]:
    """Return the currently active Google email for Antigravity."""
    gemini_dir = get_gemini_dir()

    # 1. Try to extract directly from active oauth_creds.json id_token
    creds_path = gemini_dir / "oauth_creds.json"
    email = extract_email_from_oauth_file(creds_path)
    if email:
        return email

    # 2. Fall back to google_accounts.json active field
    accts_path = gemini_dir / "google_accounts.json"
    if accts_path.exists():
        try:
            data = json.loads(accts_path.read_text(encoding="utf-8"))
            active = data.get("active")
            if isinstance(active, str) and active.strip():
                return active.strip().lower()
        except Exception:
            pass

    return None


def save_current_account_to_vault() -> Optional[str]:
    """Snapshot the currently active oauth_creds.json into the profile vault.

    Returns the email saved, or None if no active credentials exist.
    """
    gemini_dir = get_gemini_dir()
    creds_path = gemini_dir / "oauth_creds.json"
    if not creds_path.exists():
        return None

    email = get_active_account()
    if not email:
        return None

    email_clean = email.lower().strip()
    profile_dir = get_vault_dir() / email_clean
    profile_dir.mkdir(parents=True, exist_ok=True)

    dest_creds = profile_dir / "oauth_creds.json"
    shutil.copy2(creds_path, dest_creds)

    # Save companion metadata
    meta = {
        "email": email_clean,
        "last_saved": datetime.now().isoformat(),
        "source": str(creds_path),
    }
    (profile_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return email_clean


def list_accounts() -> List[Dict[str, Any]]:
    """List all known Google accounts with their status and vault presence.

    Auto-snapshots current active account to vault if not already there.
    """
    # Auto-snapshot current active credentials
    active_email = get_active_account()
    if active_email:
        save_current_account_to_vault()

    known_accounts: Dict[str, Dict[str, Any]] = {}
    vault_dir = get_vault_dir()

    # 1. Check vault profiles
    if vault_dir.exists():
        for p in vault_dir.iterdir():
            if p.is_dir() and (p / "oauth_creds.json").exists():
                email = p.name.lower()
                known_accounts[email] = {
                    "email": email,
                    "is_active": bool(active_email and email == active_email.lower()),
                    "has_creds": True,
                    "vault_path": p / "oauth_creds.json",
                }

    # 2. Check google_accounts.json
    gemini_dir = get_gemini_dir()
    accts_path = gemini_dir / "google_accounts.json"
    if accts_path.exists():
        try:
            data = json.loads(accts_path.read_text(encoding="utf-8"))
            act = data.get("active")
            if act and isinstance(act, str):
                act_clean = act.strip().lower()
                if act_clean not in known_accounts:
                    known_accounts[act_clean] = {
                        "email": act_clean,
                        "is_active": True,
                        "has_creds": (gemini_dir / "oauth_creds.json").exists(),
                        "vault_path": None,
                    }
                else:
                    known_accounts[act_clean]["is_active"] = True

            old_list = data.get("old", [])
            if isinstance(old_list, list):
                for old in old_list:
                    if isinstance(old, str):
                        old_clean = old.strip().lower()
                        if old_clean not in known_accounts:
                            known_accounts[old_clean] = {
                                "email": old_clean,
                                "is_active": False,
                                "has_creds": False,
                                "vault_path": None,
                            }
        except Exception:
            pass

    # Sort with active account first, then alphabetically
    result = list(known_accounts.values())
    result.sort(key=lambda x: (not x["is_active"], not x["has_creds"], x["email"]))
    return result


def switch_account(target_email: str) -> Tuple[bool, str]:
    """Switch the active Google account to target_email.

    Steps:
    1. Snapshot current active credentials to vault.
    2. Check target account vault file.
    3. Copy target credentials to ~/.gemini/oauth_creds.json.
    4. Update ~/.gemini/google_accounts.json.
    """
    target = target_email.strip().lower()
    active_email = get_active_account()

    if active_email and target == active_email.lower():
        return True, f"Akun [bold]{target}[/bold] sudah merupakan akun aktif."

    # 1. Snapshot current active account
    save_current_account_to_vault()

    # 2. Verify target exists in vault
    target_creds = get_vault_dir() / target / "oauth_creds.json"
    if not target_creds.exists():
        return False, (
            f"Kredensial untuk akun '{target}' belum tersimpan di vault profil.\n"
            f"Gunakan `/auth login` untuk menghubungkan akun ini terlebih dahulu."
        )

    # 3. Swap active oauth_creds.json
    gemini_dir = get_gemini_dir()
    dest_creds = gemini_dir / "oauth_creds.json"
    shutil.copy2(target_creds, dest_creds)

    # 4. Update google_accounts.json
    accts_path = gemini_dir / "google_accounts.json"
    accts_data: Dict[str, Any] = {"active": target, "old": []}
    if accts_path.exists():
        try:
            accts_data = json.loads(accts_path.read_text(encoding="utf-8"))
        except Exception:
            accts_data = {"active": target, "old": []}

    old_active = accts_data.get("active")
    current_old: List[str] = accts_data.get("old", [])
    if old_active and old_active.lower() != target:
        if old_active.lower() not in [o.lower() for o in current_old]:
            current_old.insert(0, old_active)

    # Filter out target from old list
    current_old = [o for o in current_old if o.lower() != target]
    accts_data["active"] = target
    accts_data["old"] = current_old

    accts_path.write_text(json.dumps(accts_data, indent=2), encoding="utf-8")

    return True, f"Berhasil beralih ke akun Google: [bold]{target}[/bold]"


def remove_account(target_email: str) -> Tuple[bool, str]:
    """Remove a profile from the vault (cannot remove the currently active account)."""
    target = target_email.strip().lower()
    active_email = get_active_account()

    if active_email and target == active_email.lower():
        return False, f"Tidak dapat menghapus akun '{target}' karena sedang aktif digunakan."

    profile_dir = get_vault_dir() / target
    if not profile_dir.exists():
        return False, f"Akun '{target}' tidak ditemukan di vault profil."

    shutil.rmtree(profile_dir, ignore_errors=True)

    # Also remove from old list in google_accounts.json if present
    gemini_dir = get_gemini_dir()
    accts_path = gemini_dir / "google_accounts.json"
    if accts_path.exists():
        try:
            data = json.loads(accts_path.read_text(encoding="utf-8"))
            data["old"] = [o for o in data.get("old", []) if o.lower() != target]
            accts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    return True, f"Akun Google '{target}' berhasil dihapus dari vault profil."


def login_new_account_flow(agy_bin: Optional[str] = None) -> Tuple[bool, str]:
    """Guide the user to log in with a new Google account and register it to the vault."""
    from system2 import find_antigravity_bin
    binary = agy_bin or find_antigravity_bin()
    if not binary:
        return False, "Binary Google Antigravity CLI ('agy.exe') tidak ditemukan di sistem."

    gemini_dir = get_gemini_dir()
    creds_path = gemini_dir / "oauth_creds.json"
    bak_path = gemini_dir / "oauth_creds.json.bak_auth_flow"

    # 1. Snapshot current active account to vault first
    current_email = get_active_account()
    if current_email and creds_path.exists():
        save_current_account_to_vault()

    # 2. Backup current credentials temporarily
    if creds_path.exists():
        shutil.copy2(creds_path, bak_path)
        try:
            creds_path.unlink()
        except Exception:
            pass

    # 3. Launch agy so Google OAuth flow triggers in terminal/browser
    try:
        import subprocess
        # Run agy interactive so user authenticates
        subprocess.run([binary], shell=False)
    except Exception as e:
        if bak_path.exists() and not creds_path.exists():
            shutil.copy2(bak_path, creds_path)
            bak_path.unlink(missing_ok=True)
        return False, f"Gagal menjalankan proses login Antigravity: {e}"

    # 4. Check if new oauth_creds.json was created
    if creds_path.exists():
        new_email = extract_email_from_oauth_file(creds_path)
        if new_email:
            save_current_account_to_vault()
            bak_path.unlink(missing_ok=True)
            return True, f"Login berhasil! Akun Google baru [bold]{new_email}[/bold] telah ditambahkan ke vault dan kini aktif."

    # If login was canceled or aborted, restore backup
    if bak_path.exists():
        shutil.copy2(bak_path, creds_path)
        bak_path.unlink(missing_ok=True)
        return False, "Proses login dibatalkan atau tidak selesai. Akun sebelumnya tetap aktif."

    return False, "Tidak ada kredensial baru yang terdeteksi."
