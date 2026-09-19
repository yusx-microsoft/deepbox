"""Probe the optional embedded SDK in a disposable interpreter, never sessiond."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone


_SDK_PROBE = """
import importlib.util, importlib.metadata, json, os, sys
source = os.environ.get('AGENTBRIDGE_DEEPORCA_SOURCE', os.environ.get('DEEPBOX_DEEPORCA_SOURCE', ''))
if source:
    sys.path.insert(0, os.path.abspath(os.path.expanduser(source)))
result = {'installed': False, 'api': None, 'version': None}
try:
    if importlib.util.find_spec('deeporca') is not None:
        result['installed'] = True
        from deeporca.embedded import EMBEDDED_API_VERSION
        result['api'] = EMBEDDED_API_VERSION
        try:
            result['version'] = importlib.metadata.version('deeporca')
        except importlib.metadata.PackageNotFoundError:
            pass
except Exception:
    result['error'] = 'embedded_api_unavailable'
print(json.dumps(result))
"""


def probe(adapter, *, runner, include_models=True) -> dict:
    # Imported when probing, after the platform adapter registry is initialized.
    from ...runtime_probe import CAPABILITY_SCHEMA_VERSION, _safe_version, _surface_json

    try:
        result = runner([sys.executable, "-c", _SDK_PROBE], 8.0)
        data = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        data = {}
    installed = data.get("installed") is True
    compatible = installed and type(data.get("api")) is int and data["api"] == 1
    version = _safe_version(data["version"]) if isinstance(data.get("version"), str) else None
    surface = _surface_json(adapter, compatible)
    surface["features"]["context"] = {
        "continuity": "native", "available": compatible, "resume_scope": "profile",
    }
    return {
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
