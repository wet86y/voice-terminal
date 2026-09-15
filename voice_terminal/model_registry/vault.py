from __future__ import annotations

import sys
from typing import Protocol


class CredentialVaultUnavailable(RuntimeError):
    """Raised when Windows Credential Manager cannot be used safely."""


class CredentialVault(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def diagnostic(self) -> str | None: ...

    def put(self, reference: str, secret: str) -> None: ...

    def get(self, reference: str) -> str | None: ...

    def delete(self, reference: str) -> None: ...


class WindowsCredentialVault:
    """Narrow adapter around the Windows Generic Credential keyring backend.

    We intentionally reject every other keyring backend.  A file-based
    keyring fallback would silently violate the product's secret boundary.
    """

    service_name = "Voice Terminal"

    def __init__(self) -> None:
        self._backend = None
        self._diagnostic: str | None = None
        if sys.platform != "win32":
            self._diagnostic = "产品托管 API Key 仅在 Windows Credential Manager 环境可用。"
            return
        try:
            import keyring  # type: ignore

            backend = keyring.get_keyring()
            backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
            if "WinVaultKeyring" not in backend_name:
                self._diagnostic = f"未选中 Windows Credential Manager backend（当前：{backend_name}）。"
                return
            self._backend = backend
        except Exception as exc:
            self._diagnostic = f"Windows Credential Manager 不可用：{type(exc).__name__}。"

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def diagnostic(self) -> str | None:
        return self._diagnostic

    def _require_backend(self):
        if self._backend is None:
            raise CredentialVaultUnavailable(self._diagnostic or "Windows Credential Manager 不可用。")
        return self._backend

    def put(self, reference: str, secret: str) -> None:
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("API Key 不能为空。")
        self._require_backend().set_password(self.service_name, reference, secret)

    def get(self, reference: str) -> str | None:
        return self._require_backend().get_password(self.service_name, reference)

    def delete(self, reference: str) -> None:
        backend = self._require_backend()
        try:
            backend.delete_password(self.service_name, reference)
        except Exception:
            # A failed cleanup must not turn an already-safe registry update
            # into a plaintext fallback or erase the registry state.
            pass
