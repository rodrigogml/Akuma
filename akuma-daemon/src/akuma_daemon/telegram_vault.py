from __future__ import annotations

import configparser
import json
import shlex
from pathlib import Path
import subprocess
from typing import Any


class VaultResolutionError(RuntimeError):
    pass


def load_profile(path: str | Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeError) as exc:
        raise VaultResolutionError(f"invalid Telegram profile: {type(exc).__name__}") from exc
    if not parser.has_section("telegram") or not parser.has_section("vault"):
        raise VaultResolutionError("Telegram profile requires [telegram] and [vault] sections")
    values = {key: value.strip() for section in ("telegram", "vault") for key, value in parser[section].items()}
    required = {"provider_command", "entry"}
    missing = sorted(required - values.keys())
    if missing:
        raise VaultResolutionError(f"Telegram profile missing: {', '.join(missing)}")
    values.setdefault("password_field", "password")
    values.setdefault("auth_mode", "windows_credential_manager")
    values.setdefault("credential_target", "Akuma/KeePassXC/KeeVault")
    values.setdefault("timeout_seconds", "30")
    return values


class VaultTokenResolver:
    """Ask the configured KeePassVault provider for a bot token."""

    def resolve(self, profile_path: str | Path) -> str:
        profile = load_profile(profile_path)
        auth: dict[str, Any] = {"mode": profile["auth_mode"]}
        if profile.get("credential_target"):
            auth["target"] = profile["credential_target"]
        request = {"version": 1, "operation": "read",
                   "entry": {"path": profile["entry"]},
                   "field": profile["password_field"], "auth": auth}
        try:
            command = shlex.split(profile["provider_command"], posix=False)
            completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False),
                                       text=True, capture_output=True,
                                       timeout=float(profile["timeout_seconds"]), check=False)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise VaultResolutionError(f"KeePassVault provider unavailable: {type(exc).__name__}") from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise VaultResolutionError("KeePassVault provider returned invalid JSON") from exc
        if completed.returncode != 0 or response.get("ok") is not True:
            raise VaultResolutionError("KeePassVault provider rejected the secret request")
        value = response.get("data", {}).get("value")
        if not isinstance(value, str) or not value:
            raise VaultResolutionError("KeePassVault provider returned no token")
        return value
