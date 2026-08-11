from __future__ import annotations

from pathlib import Path

import yaml


class AccountRegistry:
    """Per-account enable/disable, re-read from disk on every call.

    Reading at signal time rather than at startup is the whole point: an
    account can be switched off mid-session without restarting the process.

    A missing file means "no overrides" and returns None, which callers treat
    as every configured account being enabled.
    """

    def __init__(self, config_path: Path) -> None:
        self._path = config_path

    def enabled_accounts(self) -> set[str] | None:
        if not self._path.exists():
            return None
        loaded = yaml.safe_load(self._path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"account config must contain a mapping: {self._path}")

        enabled: set[str] = set()
        for name, value in loaded.items():
            if not isinstance(value, bool):
                # A truthiness cast would read the quoted string "false" as
                # enabled, silently inverting an operator's intent to switch an
                # account off. On a control that exists to stop trading, reject
                # anything that is not an actual boolean.
                raise ValueError(
                    f"account {name!r} must be true or false, got {value!r} "
                    f"({type(value).__name__}) in {self._path}"
                )
            if value:
                enabled.add(str(name))
        return enabled
