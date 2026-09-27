"""Google Antigravity Multi-Account Authentication Manager.

Provides credential vault management, zero-friction account switching (<100ms),
automatic JWT email extraction, Windows Keyring integration, and session safety snapshots
for Antigravity CLI and BrainFrog.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
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


def read_windows_keyring_account() -> Optional[Tuple[str, Dict[str, Any]]]:
    """Read Google account credentials from Windows Credential Manager ('gemini:antigravity').

    Returns (email, creds_dict) if present and valid, else None.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
        CredRead = advapi32.CredReadW
        CredRead.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
        CredRead.restype = wintypes.BOOL
        CredFree = advapi32.CredFree
        CredFree.argtypes = [ctypes.c_void_p]

        pcred = PCREDENTIAL()
        if CredRead("gemini:antigravity", 1, 0, ctypes.byref(pcred)):
            try:
                blob = bytes(pcred.contents.CredentialBlob[:pcred.contents.CredentialBlobSize])
                data = json.loads(blob.decode("utf-8"))
                t = data.get("token", {})
                id_tok = data.get("id_token", "")
                if not id_tok:
                    return None
                payload = decode_jwt_payload(id_tok)
                email = payload.get("email")
                if not email or not isinstance(email, str):
                    return None
                email_clean = email.strip().lower()

                # Parse ISO expiry date to milliseconds timestamp
                exp_str = t.get("expiry", "")
                exp_ms = 0
                if exp_str:
                    try:
                        exp_ms = int(datetime.fromisoformat(exp_str).timestamp() * 1000)
                    except Exception:
                        exp_ms = 0

                creds_obj = {
                    "access_token": t.get("access_token", ""),
                    "refresh_token": t.get("refresh_token", ""),
                    "scope": "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email openid",
                    "token_type": t.get("token_type", "Bearer"),
                    "id_token": id_tok,
                    "expiry_date": exp_ms,
                }
                return email_clean, creds_obj
            finally:
                CredFree(pcred)
    except Exception:
        pass
    return None


def write_windows_keyring_account(email: str, creds_dict: Dict[str, Any]) -> bool:
    """Optionally sync active account token to Windows Credential Manager ('gemini:antigravity')."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.windll.advapi32

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        CredWrite = advapi32.CredWriteW
        CredWrite.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        CredWrite.restype = wintypes.BOOL

        exp_ms = creds_dict.get("expiry_date", 0)
        exp_iso = datetime.fromtimestamp(exp_ms / 1000.0).isoformat() if exp_ms else ""
        payload = {
            "token": {
                "access_token": creds_dict.get("access_token", ""),
                "token_type": creds_dict.get("token_type", "Bearer"),
                "refresh_token": creds_dict.get("refresh_token", ""),
                "expiry": exp_iso,
            },
            "auth_method": "consumer",
            "id_token": creds_dict.get("id_token", ""),
        }
        blob = json.dumps(payload).encode("utf-8")
        blob_arr = (ctypes.c_byte * len(blob))(*blob)

        cred = CREDENTIAL()
        cred.Flags = 0
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = "gemini:antigravity"
        cred.Comment = "Google Antigravity OAuth Token"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(blob_arr, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = "antigravity"

        return bool(CredWrite(ctypes.byref(cred), 0))
    except Exception:
        return False


def sync_windows_keyring_to_vault() -> Optional[str]:
    """Auto-discover accounts from Windows Keyring and sync them to ~/.gemini/profiles."""
    res = read_windows_keyring_account()
    if not res:
        return None
    email, creds_dict = res
    profile_dir = get_vault_dir() / email
    target_creds = profile_dir / "oauth_creds.json"
    if not target_creds.exists() or target_creds.stat().st_size == 0:
        profile_dir.mkdir(parents=True, exist_ok=True)
        target_creds.write_text(json.dumps(creds_dict, indent=2), encoding="utf-8")
        meta = {
            "email": email,
            "last_saved": datetime.now().isoformat(),
            "source": "windows_keyring",
        }
        (profile_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return email


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

    Auto-snapshots current active account to vault and imports from Windows Keyring.
    """
    # Auto-discover from Windows Keyring
    sync_windows_keyring_to_vault()

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
    """Switch the active Google account to target_email (<10ms).

    Steps:
    1. Snapshot current active credentials to vault.
    2. Check target account vault file.
    3. Copy target credentials to ~/.gemini/oauth_creds.json.
    4. Sync Windows Keyring 'gemini:antigravity'.
    5. Update ~/.gemini/google_accounts.json.
    """
    target = target_email.strip().lower()
    active_email = get_active_account()

    # If already active, still ensure creds are synced
    if active_email and target == active_email.lower():
        return True, f"Akun [bold]{target}[/bold] sudah merupakan akun aktif."

    # 1. Snapshot current active account
    save_current_account_to_vault()

    # 2. Verify target exists in vault (try keyring sync first if missing)
    target_creds = get_vault_dir() / target / "oauth_creds.json"
    if not target_creds.exists():
        sync_windows_keyring_to_vault()

    if not target_creds.exists():
        return False, (
            f"Kredensial untuk akun '{target}' belum tersimpan di vault profil.\n"
            f"Gunakan `/auth login` untuk menghubungkan akun ini terlebih dahulu."
        )

    # 3. Swap active oauth_creds.json
    gemini_dir = get_gemini_dir()
    dest_creds = gemini_dir / "oauth_creds.json"
    shutil.copy2(target_creds, dest_creds)

    # 4. Sync Windows Keyring if possible
    try:
        creds_obj = json.loads(target_creds.read_text(encoding="utf-8"))
        write_windows_keyring_account(target, creds_obj)
    except Exception:
        pass

    # 5. Update google_accounts.json
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


def login_new_account_flow(agy_bin: Optional[str] = None, timeout_seconds: int = 90) -> Tuple[bool, str]:
    """Guide the user to log in with a new Google account and register it to the vault.

    Opens Antigravity in an isolated console window on Windows to complete OAuth
    via the browser, without disrupting BrainFrog's active REPL session.
    """
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

    # Sync any existing keyring account
    sync_windows_keyring_to_vault()

    # 2. Backup current credentials temporarily
    if creds_path.exists():
        shutil.copy2(creds_path, bak_path)
        try:
            creds_path.unlink()
        except Exception:
            pass

    # 3. Launch agy in an isolated console window so it can trigger browser OAuth
    # without hijacking BrainFrog's REPL
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
    proc = None
    try:
        proc = subprocess.Popen([binary], creationflags=creationflags)
    except Exception as e:
        if bak_path.exists() and not creds_path.exists():
            shutil.copy2(bak_path, creds_path)
            bak_path.unlink(missing_ok=True)
        return False, f"Gagal meluncurkan proses login Antigravity: {e}"

    # 4. Polling loop: monitor for new credentials appearing in oauth_creds.json or Windows Keyring
    start_time = time.time()
    new_email = None

    try:
        while time.time() - start_time < timeout_seconds:
            # Check 1: did oauth_creds.json get written with a valid email?
            if creds_path.exists() and creds_path.stat().st_size > 50:
                detected = extract_email_from_oauth_file(creds_path)
                if detected and (not current_email or detected != current_email.lower()):
                    new_email = detected
                    break

            # Check 2: did Windows Keyring receive a token?
            keyring_res = read_windows_keyring_account()
            if keyring_res:
                k_email, k_creds = keyring_res
                if k_email and (not current_email or k_email != current_email.lower()):
                    creds_path.write_text(json.dumps(k_creds, indent=2), encoding="utf-8")
                    new_email = k_email
                    break

            # If user explicitly closed the popup console before login completed
            if proc and proc.poll() is not None:
                # 1 second grace period for OS disk flush
                time.sleep(1)
                if creds_path.exists() and creds_path.stat().st_size > 50:
                    detected = extract_email_from_oauth_file(creds_path)
                    if detected:
                        new_email = detected
                        break
                keyring_res = read_windows_keyring_account()
                if keyring_res:
                    k_email, k_creds = keyring_res
                    if k_email and (not current_email or k_email != current_email.lower()):
                        creds_path.write_text(json.dumps(k_creds, indent=2), encoding="utf-8")
                        new_email = k_email
                        break
                break

            time.sleep(1)
    finally:
        # Close the popup helper process once login is completed or aborted
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # 5. Handle outcome
    if new_email and creds_path.exists():
        save_current_account_to_vault()
        bak_path.unlink(missing_ok=True)

        # Sync keyring
        try:
            creds_data = json.loads(creds_path.read_text(encoding="utf-8"))
            write_windows_keyring_account(new_email, creds_data)
        except Exception:
            pass

        # Update google_accounts.json
        accts_path = gemini_dir / "google_accounts.json"
        try:
            ga = json.loads(accts_path.read_text(encoding="utf-8")) if accts_path.exists() else {"active": new_email, "old": []}
            old = ga.get("old", [])
            if current_email and current_email.lower() != new_email:
                if current_email.lower() not in [o.lower() for o in old]:
                    old.insert(0, current_email.lower())
            ga["active"] = new_email
            ga["old"] = [o for o in old if o.lower() != new_email]
            accts_path.write_text(json.dumps(ga, indent=2), encoding="utf-8")
        except Exception:
            pass

        return True, f"Login berhasil! Akun Google baru [bold]{new_email}[/bold] telah ditambahkan ke vault dan kini aktif."

    # If login was canceled, closed or timed out, restore backup
    if bak_path.exists():
        shutil.copy2(bak_path, creds_path)
        bak_path.unlink(missing_ok=True)

    return False, "Proses login dibatalkan atau jendela ditutup sebelum login selesai. Akun sebelumnya tetap aktif."
