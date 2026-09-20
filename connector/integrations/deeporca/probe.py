"""Probe the optional embedded SDK in a disposable interpreter, never sessiond."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentbridge.product import env


_SDK_PROBE = """
import importlib.util, importlib.metadata, json, os, sys, runpy
settings = json.loads(sys.argv[1])
source = settings['source']
if source:
    sys.path.insert(0, os.path.abspath(os.path.expanduser(source)))
result = {'installed': False, 'api': None, 'version': None}
try:
    if importlib.util.find_spec('deeporca') is not None:
        result['installed'] = True
        from deeporca.embedded import EMBEDDED_API_VERSION
        result['api'] = EMBEDDED_API_VERSION
        import deeporca.embedded as sdk
        result['configuration_api'] = getattr(sdk, 'PROFILE_CONFIGURATION_API_VERSION', None)
        helpers = runpy.run_path(PROFILES_HELPER)
        result['existing_api'] = helpers['existing_profile_api'](sdk)
        if type(result['api']) is int and result['api'] == 1 and result['existing_api']:
            result['existing_profiles'] = helpers['discover_profiles'](sdk, home_override=settings['home'])
        try:
            result['version'] = importlib.metadata.version('deeporca')
        except importlib.metadata.PackageNotFoundError:
            pass
except Exception:
    result['error'] = 'embedded_api_unavailable'
print(json.dumps(result))
""".replace("PROFILES_HELPER", repr(str(Path(__file__).with_name("profiles.py").resolve())))


def probe_sdk(*, runner=None) -> dict:
    """Return private subprocess metadata, never directly an inventory payload."""
    if runner is None:
        from ...runtime_probe import run_probe
        runner = run_probe
    try:
        settings = json.dumps({"source": env("DEEPORCA_SOURCE"), "home": env("DEEPORCA_HOME")})
        result = runner([sys.executable, "-c", _SDK_PROBE, settings], 8.0)
        data = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        data = {}
    return data


def probe(adapter, *, runner, include_models=True, local_state_path=None) -> dict:
    # Imported when probing, after the platform adapter registry is initialized.
    from ...runtime_probe import CAPABILITY_SCHEMA_VERSION, _safe_version, _surface_json
    from .profiles import public_profiles

    data = probe_sdk(runner=runner)
    installed = data.get("installed") is True
    compatible = installed and type(data.get("api")) is int and data["api"] == 1
    version = _safe_version(data["version"]) if isinstance(data.get("version"), str) else None
    surface = _surface_json(adapter, compatible)
    surface["features"]["context"] = {
        "continuity": "native", "available": compatible, "resume_scope": "profile",
    }
    descriptor = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "runtime": "deeporca", "label": "DeepOrca", "backend": "python-library",
        "legacy_runtime_ids": ["deeporca"],
        "installation": {"status": "installed" if installed else "missing", "version": version},
        "compatibility": {
            "status": "compatible" if compatible else "incompatible" if installed else "unknown",
            **({"reason": "embedded_api_unavailable"} if installed and not compatible else {}),
        },
        "authentication": {"status": "unknown", "reason": "profile_local_configuration"},
        "surfaces": [surface],
        "models": {"status": "unknown", "source": "none", "items": [],
                   "default": None, "allow_custom": True,
                   "probed_at": datetime.now(timezone.utc).isoformat()},
        "agent_config": {
            "profile_modes": ["create"],
            "configuration_templates": [{"id": "connector-default", "label": "Connector default"}],
            "execution_policy": "non_interactive",
        },
    }
    if compatible:
        if data.get("existing_api") is True:
            descriptor["agent_config"]["profile_modes"] = ["create", "bind"]
            descriptor["agent_config"]["existing_profiles"] = public_profiles(data)
        supported = type(data.get("configuration_api")) is int and data["configuration_api"] == 1
        descriptor["agent_config"]["configuration_api_version"] = 1 if supported else 0
        if supported:
            # No SDK, no cryptography import and no state-directory/key writes.
            # The browser can select neither this path nor any private material.
            from .credentials import public_key_info
            from .events import IntegrationError
            try:
                descriptor["agent_config"]["credential_key"] = public_key_info(local_state_path)
            except IntegrationError:
                descriptor["agent_config"]["credential_error"] = "credential_unavailable"
    return descriptor
