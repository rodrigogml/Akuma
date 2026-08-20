from __future__ import annotations

import configparser
import json
import shlex
from pathlib import Path
import subprocess
from typing import Any


class VaultResolutionError(RuntimeError):
    pass


def load_profile(path: str | Path, section: str = "telegram") -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeError) as exc:
        raise VaultResolutionError(f"invalid Telegram profile: {type(exc).__name__}") from exc
    if not parser.has_section(section) or not parser.has_section("vault"):
        raise VaultResolutionError(f"Telegram profile requires [{section}] and [vault] sections")
    values = {key: value.strip() for name in (section, "vault") for key, value in parser[name].items()}
    if not values.get("provider_command"):
        raise VaultResolutionError("Telegram profile missing: provider_command")
    values.setdefault("auth_mode", "windows_credential_manager")
    values.setdefault("credential_target", "Akuma/KeePassXC/KeeVault")
    values.setdefault("timeout_seconds", "30")
    return values


class VaultTokenResolver:
    """Ask the configured KeePassVault provider for Telegram secrets and TOTP metadata."""

    def resolve(self, profile_path: str | Path) -> str:
        profile = load_profile(profile_path)
        entry = profile.get("entry")
        if not entry:
            raise VaultResolutionError("Telegram profile missing: entry")
        return self.read(profile, entry, profile.get("password_field", "password"))

    @staticmethod
    def _auth(profile: dict[str, str]) -> dict[str, Any]:
        auth: dict[str, Any] = {"mode": profile["auth_mode"]}
        if profile.get("credential_target"):
            auth["target"] = profile["credential_target"]
        return auth

    def _request(self, profile: dict[str, str], request: dict[str, Any]) -> dict[str, Any]:
        request = {"version": 1, "auth": self._auth(profile), **request}
        try:
            command = shlex.split(profile["provider_command"], posix=False)
            completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), text=True,
                                       capture_output=True, timeout=float(profile["timeout_seconds"]), check=False)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise VaultResolutionError(f"KeePassVault provider unavailable: {type(exc).__name__}") from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise VaultResolutionError("KeePassVault provider returned invalid JSON") from exc
        if completed.returncode != 0 or response.get("ok") is not True:
            raise VaultResolutionError("KeePassVault provider rejected the secret request")
        data = response.get("data")
        if not isinstance(data, dict):
            raise VaultResolutionError("KeePassVault provider returned invalid data")
        return data

    def read(self, profile: dict[str, str], entry: str, field: str = "password") -> str:
        value = self._request(profile, {"operation": "read", "entry": {"path": entry}, "field": field}).get("value")
        if not isinstance(value, str) or not value:
            raise VaultResolutionError("KeePassVault provider returned no value")
        return value

    def totp_profile(self, profile_path: str | Path) -> dict[str, str]:
        profile = load_profile(profile_path, "totp")
        missing = sorted({"real_password_entry", "fake_password_entry"} - profile.keys())
        if missing:
            raise VaultResolutionError(f"TOTP profile missing: {', '.join(missing)}")
        return profile

    def list_totp(self, profile: dict[str, str]) -> list[str]:
        entries = self._request(profile, {"operation": "list.totp"}).get("entries")
        if not isinstance(entries, list) or not all(isinstance(item, dict) and isinstance(item.get("path"), str) for item in entries):
            raise VaultResolutionError("KeePassVault provider returned invalid TOTP entries")
        return [item["path"] for item in entries]

    def current_totp(self, profile: dict[str, str], entry: str) -> str:
        return self.read(profile, entry, "totp")
