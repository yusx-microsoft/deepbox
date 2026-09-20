"""Small, local existing-profile catalog; SDK discovery runs only in a probe child.

Records containing home/name are private Connector IPC, not capabilities. This
is not a lease: the native SDK acquires the profile at startup, after the user
has stopped every native writer.
"""
from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path
import re
import unicodedata

MAX_PROFILES = 100
_REF = re.compile(r"native-[0-9a-f]{32}\Z")


def safe_existing_name(name) -> bool:
    return (isinstance(name, str) and 1 <= len(name) <= 128
            and name not in (".", "..") and name == name.strip() and not name.endswith(".")
            and not any(c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C") for c in name))


def existing_profile_api(sdk) -> bool:
    """**kwargs alone is not evidence of the public existing-profile API."""
    try:
        parameter = inspect.signature(sdk.EmbeddedRuntime).parameters.get("profile_mode")
        return parameter is not None and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    except (AttributeError, TypeError, ValueError):
        return False


def profile_ref(path: Path) -> str:
    canonical = os.path.normcase(str(path.resolve()))
    return "native-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def selected_profile(record, ref) -> dict | None:
    """Validate a private probe record; never accept browser names or paths."""
    if (not isinstance(ref, str) or not _REF.fullmatch(ref)
            or not isinstance(record, dict) or record.keys() != {"id", "home", "profile_name"}
            or record.get("id") != ref or not safe_existing_name(record.get("profile_name"))):
        return None
    home = record.get("home")
    if not isinstance(home, str) or not home or "\x00" in home or not Path(home).is_absolute():
        return None
    try:
        canonical = Path(home).resolve()
        path = canonical / "agents" / record["profile_name"]
        if path.resolve() != path or profile_ref(path) != ref:
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return {"id": ref, "home": str(canonical), "profile_name": record["profile_name"]}


def discover_profiles(sdk, *, home_override=None) -> list[dict]:
    """Called ONLY by the disposable SDK probe; no start(), config read or write."""
    if not existing_profile_api(sdk):
        return []
    import deeporca
    override = home_override
    if override is not None and not override.strip():
        return []
    records = []
    try:
        home = Path(override if override is not None else deeporca.get_deeporca_dir()).expanduser().resolve()
        agents = home / "agents"
        if agents.resolve() != agents:
            return []
        with os.scandir(agents) as entries:
            # Bound traversal as well as output. Do not recursively inventory a
            # native home or open its state/config/secrets.
            for index, entry in enumerate(entries):
                if index >= MAX_PROFILES:
                    break
                if not safe_existing_name(entry.name):
                    continue
                try:
                    path = Path(entry.path)
                    if (not entry.is_dir(follow_symlinks=False) or path.resolve() != path
                            or not all((path / name).is_file() and not (path / name).is_symlink()
                                       for name in (".state.json", "config.yaml"))):
                        continue
                    # Public constructor validates SDK-specific names and paths.
                    # Acquisition, state validation and busy detection remain
                    # SDK-owned at startup; discovery promises no readiness.
                    sdk.EmbeddedRuntime(entry.name, str(home), home=str(home), profile_mode="existing")
                    records.append({"id": profile_ref(path), "home": str(home), "profile_name": entry.name})
                except Exception:
                    continue
    except (OSError, ValueError, RuntimeError):
        return []
    return sorted(records, key=lambda record: record["profile_name"])


def public_profiles(data) -> list[dict]:
    records = data.get("existing_profiles", [])
    if not isinstance(records, list):
        return []
    result = []
    for record in records[:MAX_PROFILES]:
        selected = selected_profile(record, record.get("id") if isinstance(record, dict) else None)
        if selected is not None:
            result.append({"id": selected["id"], "label": selected["profile_name"]})
    return result


def resolve_existing_profile(ref: str, *, runner=None) -> dict:
    """Fresh disposable discovery at provisioning; no persistent catalog/lease."""
    from .probe import probe_sdk
    from .store import BindingError
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        raise BindingError("existing_profile_unavailable")
    data = probe_sdk(runner=runner)
    if not (data.get("installed") is True and type(data.get("api")) is int and data["api"] == 1):
        raise BindingError("existing_profile_unavailable")
    if data.get("existing_api") is not True:
        raise BindingError("existing_profile_api_unavailable")
    records = data.get("existing_profiles", [])
    if isinstance(records, list):
        for record in records[:MAX_PROFILES]:
            selected = selected_profile(record, ref)
            if selected is not None:
                return selected
    raise BindingError("existing_profile_unavailable")
