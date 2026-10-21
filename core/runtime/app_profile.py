# -*- coding: utf-8 -*-
"""Trusted Application Profiles for BrainFrog Phase 1D Browser Verification.

Product Invariant:
- Application startup commands MUST come from trusted, pre-approved configuration.
- Untrusted spec.md may only reference a profile_id; it CANNOT supply arbitrary
  shell commands, Docker options, images, or startup scripts.
- Unapproved profiles fail closed with BLOCKED (Unsupported Application Startup Profile).
- No profile installs dependencies, runs a repository Dockerfile, or contacts the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

# Fixed in-container locations. The application source snapshot is mounted read-only
# at APP_SOURCE_MOUNT; BrainFrog's launcher is mounted read-only at APP_HARNESS_MOUNT.
APP_SOURCE_MOUNT = "/app"
APP_HARNESS_MOUNT = "/bf-harness"
# Must not collide with a DNS TLD: Chromium applies the HSTS preload of the `.app` TLD to a
# bare host named "app" and forces HTTPS. A hyphenated label can never be a TLD.
APP_NETWORK_ALIAS = "bf-app"


class UnsupportedProfileError(ValueError):
    """Raised when a requested application startup profile is not recognized or authorized."""
    pass


@dataclass(frozen=True)
class AppProfile:
    profile_id: str
    approved_container_image: str
    approved_startup_argv: Tuple[str, ...]
    working_directory: str = APP_SOURCE_MOUNT
    internal_port: int = 8080
    readiness_endpoint: str = "/"
    startup_timeout_seconds: float = 20.0
    environment_allowlist: Tuple[str, ...] = ()
    cpus: float = 1.0
    memory_mb: int = 256
    pids_limit: int = 64
    tmp_tmpfs_mb: int = 16
    description: str = ""

    @property
    def origin(self) -> str:
        """The single application origin the browser may reach."""
        return f"http://{APP_NETWORK_ALIAS}:{self.internal_port}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "approved_container_image": self.approved_container_image,
            "approved_startup_argv": list(self.approved_startup_argv),
            "working_directory": self.working_directory,
            "internal_port": self.internal_port,
            "readiness_endpoint": self.readiness_endpoint,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "environment_allowlist": list(self.environment_allowlist),
            "application_origin": self.origin,
            "limits": {"cpus": self.cpus, "memory_mb": self.memory_mb, "pids_limit": self.pids_limit,
                       "tmp_tmpfs_mb": self.tmp_tmpfs_mb},
            "description": self.description,
        }


# Trusted Profiles Registry (BrainFrog/operator approved). Read-only at runtime.
APPROVED_PROFILES: Mapping[str, AppProfile] = MappingProxyType({
    "python-static": AppProfile(
        profile_id="python-static",
        approved_container_image="python:3.12-slim",
        # BrainFrog's stdlib launcher: verifies the mounted snapshot digest, then serves
        # the snapshot as static files. No repository code is executed server-side.
        approved_startup_argv=(
            "python", "-I", "-B", f"{APP_HARNESS_MOUNT}/app_launcher.py",
            "--source", APP_SOURCE_MOUNT,
            "--port", "8080",
            "--nonce-file", f"{APP_HARNESS_MOUNT}/nonce",
        ),
        working_directory=APP_SOURCE_MOUNT,
        internal_port=8080,
        readiness_endpoint="/",
        startup_timeout_seconds=20.0,
        description="Isolated Python standard-library static web server for HTML/CSS/JS fixtures",
    ),
})


def get_app_profile(profile_id: Optional[str], registry: Optional[Mapping[str, AppProfile]] = None) -> AppProfile:
    """Retrieve an approved application startup profile by ID.

    Fails closed if the profile is None, empty, or unapproved. ``registry`` is an
    operator/API-only override (used by tests); spec content can only supply an ID.
    """
    if not profile_id or not isinstance(profile_id, str):
        raise UnsupportedProfileError("Missing application profile ID.")

    profiles = APPROVED_PROFILES if registry is None else registry
    pid = profile_id.strip().lower()
    if pid not in profiles:
        valid_ids = ", ".join(sorted(profiles.keys()))
        raise UnsupportedProfileError(
            f"Unsupported Application Startup Profile '{profile_id}'. "
            f"Approved profiles: [{valid_ids}]"
        )
    return profiles[pid]
