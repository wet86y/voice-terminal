"""Product-facing, secret-safe model registry services.

The registry is deliberately independent of the WebSocket protocol.  Its
public snapshots contain only redacted provider metadata and model entries.
"""

from .service import ModelRegistryError, ModelRegistryService
from .vault import CredentialVault, CredentialVaultUnavailable, WindowsCredentialVault

__all__ = [
    "CredentialVault",
    "CredentialVaultUnavailable",
    "ModelRegistryError",
    "ModelRegistryService",
    "WindowsCredentialVault",
]
