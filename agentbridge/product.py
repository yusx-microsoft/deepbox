"""Small branding/configuration boundary for the phased AgentBridge rename.

``DISPLAY_NAME`` is human-facing branding; ``NAME`` is the lowercase Python/CLI
and service/log identifier. Environment keys retain the ``AGENTBRIDGE_`` prefix.

Branding is not an identity migration. Keep the existing ``deepbox.db`` default
and database/schema identifiers; the platform ``deepbox`` state/spool directories
hold device IDs, registrations and replay offsets. IPC still uses
``deepbox-sessiond-*``, the ``deepbox`` runtime directory (including the
``~/.deepbox/deepbox`` fallback), and its existing ``client:`` / ``server:`` HMAC
domains so an upgraded transport can attach without rotating supervisor secrets.

Likewise, ``deepbox_session`` / ``deepbox-session`` cookies and signing salt,
``deepbox_inv_`` invitation tokens, ``deepbox_*`` agent control request IDs,
``<deepbox_attachments>`` prompt delimiters and ``x-deepbox-identity-*`` trusted
headers are compatibility identifiers, not display names. The ``hpc_box_`` token
format and hashes, spool URL/token-hash namespace derivation, protocol v3
and the logging handler's private ``_deepbox_json`` idempotence marker stay intact.
Changing any of these requires a separate, explicit migration, not a name swap.
"""

from __future__ import annotations

import os
from pathlib import Path

DISPLAY_NAME = "AgentBridge"
NAME = "agentbridge"
LEGACY_NAME = "deepbox"


def env(stem: str, default: str | None = None) -> str | None:
    """Read AGENTBRIDGE_<stem>, then DEEPBOX_<stem>, then ``default``.

    Presence, not truthiness, determines precedence: an explicit empty canonical
    value never reveals a legacy value. Full keys from either prefix are accepted
    here for compatibility; new callers should use unprefixed uppercase stems.
    """
    for prefix in ("AGENTBRIDGE_", "DEEPBOX_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    canonical = f"AGENTBRIDGE_{stem}"
    if canonical in os.environ:
        return os.environ[canonical]
    return os.environ.get(f"DEEPBOX_{stem}", default)


def local_home() -> Path:
    """Resolve the install home without creating directories or reading files.

    Explicit HOME overrides win. Otherwise prefer ~/.agentbridge, reusing an
    existing ~/.deepbox only when the new home is absent. Nothing is moved or
    copied. An explicit empty HOME is invalid rather than a legacy fallback (or
    an accidental current-directory install). Persistent state and IPC retain
    their independent legacy roots for identity continuity.
    """
    override = env("HOME")
    if override is not None:
        if not override.strip():
            raise ValueError("AGENTBRIDGE_HOME (or DEEPBOX_HOME) must not be empty")
        return Path(override).expanduser()
    home = Path.home()
    canonical = home / f".{NAME}"
    legacy = home / f".{LEGACY_NAME}"
    return legacy if not canonical.exists() and legacy.exists() else canonical
