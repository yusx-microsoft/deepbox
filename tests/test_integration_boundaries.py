"""Guard runtime-specific packaging without importing a user's native SDK."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_deeporca_implementation_is_not_flattened_into_platform_packages():
    for directory in ("agentbridge", "connector", "server/app"):
        assert not list((ROOT / directory).glob("deeporca*.py")), directory
    assert not (ROOT / "web/deeporca-chat.js").exists()
    assert not (ROOT / "web/deeporca-chat.css").exists()
    for directory in ("agentbridge", "connector", "server/app", "web"):
        assert (ROOT / directory / "integrations/deeporca").is_dir()


def test_shared_deeporca_contract_does_not_import_optional_sdk():
    code = "\n".join((
        "import sys",
        "from agentbridge.integrations.deeporca.contract import validate_runtime_config, binding_revision",
        "cfg = validate_runtime_config({})",
        "assert cfg['profile']['mode'] == 'create'",
        "assert len(binding_revision('agent', 'project', cfg)) == 16",
        "assert not any(n == 'deeporca' or n.startswith('deeporca.') for n in sys.modules)",
    ))
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
