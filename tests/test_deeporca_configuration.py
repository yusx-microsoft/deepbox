"""Connector-only, hermetic sealing/configuration/lifecycle regression tests."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agentbridge.integrations.deeporca.contract import (
    binding_identity_revision, binding_revision, validate_runtime_config)
from connector.integrations.deeporca.credentials import (
    AAD_PREFIX, decrypt_credential, key_path, public_key_info, runtime_configuration)
from connector.integrations.deeporca.events import IntegrationError
from connector.integrations.deeporca.probe import probe_sdk
from connector.integrations.deeporca.profiles import (
    existing_profile_api, profile_ref, resolve_existing_profile,
)
from connector.integrations.deeporca.store import BindingError, DeepOrcaStore
from connector.integrations.deeporca.worker import DeepOrcaWorker, _binding
from connector.local_store import LocalProjectStore
from connector.runtime_probe import ProbeResult, probe_family
from connector.supervisor import SessionSupervisor

LLM = {"provider": "openai", "base_url": "http://127.0.0.1:9991/v1", "model": "local-model",
       "context_window": 16000, "reasoning_effort": "high"}
SECRET = "fake-test-credential-not-for-provider"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for name in ("LOCALAPPDATA", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "machine"))
    monkeypatch.delenv("AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR", raising=False)


def seal(info, secret=SECRET, base_url=LLM["base_url"]):
    def number(value):
        return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")
    jwk = info["public_key"]
    public = rsa.RSAPublicNumbers(number(jwk["e"]), number(jwk["n"])).public_key()
    aes = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    ciphertext = AESGCM(aes).encrypt(iv, secret.encode(),
        (AAD_PREFIX + info["key_id"] + "\0" + base_url).encode())
    wrapped = public.encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                              algorithm=hashes.SHA256(), label=None))
    encode = lambda v: base64.b64encode(v).decode()
    return {"mode": "sealed", "key_id": info["key_id"], "wrapped_key": encode(wrapped),
            "iv": encode(iv), "ciphertext": encode(ciphertext)}


def test_public_metadata_atomic_concurrent_creation_and_fresh_process(tmp_path):
    state = tmp_path / "state.db"
    with ThreadPoolExecutor(max_workers=5) as pool:
        descriptors = list(pool.map(lambda _: public_key_info(state), range(5)))
    info = descriptors[0]
    assert all(value == info for value in descriptors)
    assert info["algorithm"] == "RSA-OAEP-256+A256GCM" and info["version"] == 1
    assert len(info["key_id"]) == 64
    assert info["public_key"]["key_ops"] == ["encrypt"]
    assert info["public_key"]["ext"] is True and "=" not in info["public_key"]["n"]
    envelope = seal(info)
    assert decrypt_credential(envelope, LLM["base_url"], private_key_path=key_path(state)) == SECRET
    code = "from connector.integrations.deeporca.credentials import public_key_info; import json,sys; print(json.dumps(public_key_info(sys.argv[1])))"
    result = subprocess.run([sys.executable, "-c", code, str(state)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == info
    assert SECRET not in result.stdout + result.stderr
    if os.name != "nt":
        assert key_path(state).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ["ciphertext", "iv", "wrapped_key", "endpoint", "key_id", "rotation"])
def test_sealed_tamper_endpoint_and_key_rotation_fail_safely(tmp_path, fault):
    state = tmp_path / "state.db"
    info = public_key_info(state)
    envelope = seal(info)
    endpoint = LLM["base_url"]
    if fault in ("ciphertext", "iv", "wrapped_key"):
        value = bytearray(base64.b64decode(envelope[fault]))
        value[-1] ^= 1
        envelope[fault] = base64.b64encode(value).decode()
    elif fault == "endpoint":
        endpoint += "/different"
    elif fault == "key_id":
        envelope["key_id"] = "a" * 64
    else:
        # A newly enrolled machine/replacement key cannot consume the envelope.
        state = tmp_path / "rotated" / "state.db"
        assert public_key_info(state)["key_id"] != info["key_id"]
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        decrypt_credential(envelope, endpoint, private_key_path=key_path(state))


@pytest.mark.parametrize("fault", ["missing", "broken", "missing_pin", "broken_pin"])
def test_key_loss_never_silently_regenerates(tmp_path, fault):
    state = tmp_path / "state.db"
    public_key_info(state)
    target = key_path(state)
    if "pin" in fault:
        target = target.with_suffix(".key-id")
    if fault.startswith("missing"):
        target.unlink()
    else:
        target.write_bytes(b"broken")
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        public_key_info(state)
    assert (not target.exists()) if fault.startswith("missing") else target.read_bytes() == b"broken"


def test_symlink_rejected_before_key_creation(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    try:
        key_path(tmp_path / "state.db").symlink_to(target)
    except OSError:
        pytest.skip("host lacks symlink privilege")
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        public_key_info(tmp_path / "state.db")
    assert target.read_text() == "untouched"


def test_state_pin_survives_loss_of_both_key_files(tmp_path):
    state = tmp_path / "state.db"
    public_key_info(state)
    key_path(state).unlink()
    key_path(state).with_suffix(".key-id").unlink()
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        public_key_info(state)
    assert not key_path(state).exists()


def test_hard_link_rejected_and_memory_state_never_provisions(tmp_path):
    target = tmp_path / "target"
    target.write_text("untouched")
    try:
        os.link(target, key_path(tmp_path / "state.db"))
    except OSError:
        pytest.skip("host lacks hardlink support")
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        public_key_info(tmp_path / "state.db")
    assert target.read_text() == "untouched"
    with pytest.raises(IntegrationError, match="^credential_unavailable$"):
        public_key_info(":memory:")


def test_descriptor_optional_until_sdk_available_and_uses_local_state(tmp_path):
    state = tmp_path / "custom" / "state.db"
    for result in ({"installed": False}, {"installed": True, "api": 1},
                   {"installed": True, "api": 2, "configuration_api": 1}):
        cap = probe_family("deeporca", local_state_path=state,
                           runner=lambda *_: ProbeResult(0, json.dumps(result)))
        assert "credential_key" not in cap["agent_config"]
        assert not state.parent.exists()
    result = {"installed": True, "api": 1, "configuration_api": 1}
    cap = probe_family("deeporca", local_state_path=state,
                       runner=lambda *_: ProbeResult(0, json.dumps(result)))
    assert cap["agent_config"]["credential_key"] == public_key_info(state)
    assert cap["agent_config"]["configuration_api_version"] == 1
    key_path(state).unlink()
    cap = probe_family("deeporca", local_state_path=state,
                       runner=lambda *_: ProbeResult(0, json.dumps(result)))
    assert cap["agent_config"]["credential_error"] == "credential_unavailable"
    assert "credential_key" not in cap["agent_config"]


def test_configuration_absence_and_explicit_none():
    assert runtime_configuration({}) is None
    assert runtime_configuration({"llm": LLM}) == LLM
    assert runtime_configuration({"llm": LLM, "credential": {"mode": "none"}}) == {**LLM, "api_key": ""}


CONFIGURATION_SDK = r'''
import asyncio, json, sys
from pathlib import Path
EMBEDDED_API_VERSION = 1
PROFILE_CONFIGURATION_API_VERSION = 1
def ensure_profile(name, *, home=None, template_dir=None):
    path = Path(home) / 'agents' / name
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    return {'profile':name, 'created':created, 'configured':False}
class EmbeddedRuntime:
    def __init__(self, name, workspace, *, home=None):
        self.workspace = Path(workspace)
        self.task = None
    async def start(self, *, configuration=None):
        expected = json.loads((self.workspace / 'expected.json').read_text())
        assert configuration == expected, 'unsafe fake SDK configuration diagnostic'
        print(configuration, file=sys.stderr)
        print(configuration)
        if (self.workspace / 'fail-start').exists():
            raise RuntimeError(str(configuration))
        with (self.workspace / 'applied-models').open('a') as f:
            f.write(configuration['model'] + '\n')
    async def run_turn(self, session_id, text, *, message_id, model=None, on_event):
        self.task = asyncio.current_task()
        turn_id = 'turn-' + message_id
        await on_event({'type':'begin', 'turn_id':turn_id, 'sequence':1})
        (self.workspace / 'turn-running').write_text('yes')
        try:
            if text == 'wait': await asyncio.Event().wait()
            return {'status':'completed', 'turn_id':turn_id}
        except asyncio.CancelledError:
            return {'status':'cancelled', 'turn_id':turn_id}
    async def interrupt(self):
        if self.task and not self.task.done(): self.task.cancel()
    async def close(self): await self.interrupt()
'''


@pytest.fixture
def configured_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    source = tmp_path / "sdk"
    package = source / "deeporca"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "embedded.py").write_text(CONFIGURATION_SDK)
    monkeypatch.setenv("AGENTBRIDGE_DEEPORCA_SOURCE", str(source))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local = LocalProjectStore(tmp_path / "state.db")
    project = local.add(str(workspace), "Project")
    credential = seal(public_key_info(local.path))
    config = {"llm": LLM, "credential": credential}
    agent = {"id": str(uuid4()), "runtime": "deeporca", "local_project_id": project.id,
             "runtime_config": config, "enabled": True}
    store = DeepOrcaStore(tmp_path / "binding.db")
    # Both SQLite files are beside the same machine-private key.
    sup = SessionSupervisor({agent["id"]: agent}, local_store=local, deeporca_store=store)
    sup.set_enrollment("http://localhost:8077", "test-machine")
    agent = dict(sup.agents[agent["id"]])
    (workspace / "expected.json").write_text(json.dumps({**LLM, "api_key": SECRET}))
    capability = probe_family("deeporca", runner=lambda *_: ProbeResult(0, '{"installed":true,"api":1}'))
    monkeypatch.setattr("connector.supervisor.probe_family", lambda *_, **__: capability)
    yield sup, store, agent, workspace, source
    if not sup._stopped:
        sup.shutdown()
    local.close()


@pytest.mark.asyncio
async def test_worker_decrypts_configuration_restart_no_secret_ipc(configured_runtime):
    sup, store, agent, workspace, source = configured_runtime
    aid = agent["id"]
    try:
        await sup._ensure_library_worker(aid)
        first = sup._deeporca_workers[aid]
        binding = store.get_binding(aid)
        wire_binding = store.worker_binding(aid)
        assert SECRET not in json.dumps(wire_binding)
        assert wire_binding["credential_key_path"] == str(key_path(store.path))
        assert first._startup_info["configured"] is True
        assert SECRET not in json.dumps(first._startup_info)
        await first.close()
        await sup._ensure_library_worker(aid)
        assert sup._deeporca_workers[aid] is not first
        assert (workspace / "applied-models").read_text().splitlines() == [LLM["model"]] * 2
        assert SECRET not in json.dumps(list(sup.pending))
        with store._lock:
            conn = store._conn
            serialized = "\n".join(conn.iterdump())
        assert SECRET not in serialized
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "omitted"])
async def test_worker_none_and_retained_local_credential(configured_runtime, mode):
    sup, store, agent, workspace, source = configured_runtime
    aid = agent["id"]
    config = {"llm": LLM}
    expected = dict(LLM)
    if mode == "none":
        config["credential"] = {"mode": "none"}
        expected["api_key"] = ""
    (workspace / "expected.json").write_text(json.dumps(expected))
    sup.agents[aid] = {**agent, "runtime_config": config}
    try:
        await sup._ensure_library_worker(aid)
        assert sup._deeporca_workers[aid]._startup_info["configured"] is True
    finally:
        await sup.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,code", [("old_sdk", "configuration_api_unavailable"),
                                        ("tamper", "credential_unavailable"),
                                        ("sdk_error", "startup_failed")])
async def test_worker_configuration_errors_are_safe(configured_runtime, fault, code):
    sup, store, agent, workspace, source = configured_runtime
    aid = agent["id"]
    if fault == "old_sdk":
        (source / "deeporca" / "embedded.py").write_text(CONFIGURATION_SDK.replace("PROFILE_CONFIGURATION_API_VERSION = 1", ""))
    elif fault == "tamper":
        config = {**agent["runtime_config"], "llm": {**LLM, "base_url": LLM["base_url"] + "/other"}}
        sup.agents[aid] = {**agent, "runtime_config": config}
    else:
        (workspace / "fail-start").touch()
    try:
        with pytest.raises(BindingError, match="^" + code + "$"):
            await sup._ensure_library_worker(aid)
        assert store.public_status(aid)["runtime_status"]["code"] == code
        assert SECRET not in json.dumps(list(sup.pending))
    finally:
        await sup.aclose()


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError("runtime did not settle")


@pytest.mark.asyncio
async def test_mutable_update_retires_before_apply_and_defends_active_race(configured_runtime):
    sup, store, agent, workspace, source = configured_runtime
    aid, sid = agent["id"], str(uuid4())
    try:
        await sup.open_pty(aid, sid, surface="structured")
        worker = sup._deeporca_workers[aid]
        initial = store.get_binding(aid)
        input_id = str(uuid4())
        await sup.handle_control({"type": "input", "agent_id": aid, "session_id": sid,
                                  "client_input_id": input_id, "data": "wait"})
        await until(lambda: (workspace / "turn-running").exists())
        config = {**agent["runtime_config"], "llm": {**LLM, "model": "replacement-model"}}
        updated = {**agent, "runtime_config": config}
        sup.replace_agents([updated])
        await until(lambda: store.public_status(aid)["runtime_status"].get("code") == "configuration_busy")
        assert worker.is_alive() and sup._deeporca_workers[aid] is worker
        assert store.get_binding(aid)["revision"] == initial["revision"]
        assert store.input_receipt(aid, sid, input_id)["state"] == "running"
        public = [frame["runtime_status"] for frame in sup.pending if frame.get("type") == "agent.runtime_status"][-1]
        assert public["state"] != "ready" and public["code"] == "configuration_busy"
        assert public["revision"] == binding_revision(aid, agent["local_project_id"], config)
        ack = await sup._library_input({"agent_id": aid, "session_id": sid,
            "client_input_id": str(uuid4()), "data": "must not execute"}, sup.ptys[(aid, sid)])
        assert ack["reason"] == "configuration_busy"
        (workspace / "expected.json").write_text(json.dumps({**config["llm"], "api_key": SECRET}))
        await worker.interrupt()
        await until(lambda: store.input_receipt(aid, sid, input_id).get("result") == "cancelled")
        await sup.handle_control({"type": "close", "agent_id": aid, "session_id": sid})
        await until(lambda: store.get_binding(aid)["revision"] != initial["revision"])
        await until(lambda: store.public_status(aid)["runtime_status"]["state"] == "ready")
        assert not worker.is_alive()
        final = store.get_binding(aid)
        assert final["profile_name"] == initial["profile_name"] and final["home"] == initial["home"]
        assert final["workspace"] == initial["workspace"]
        assert store.input_receipt(aid, sid, input_id)["result"] == "cancelled"
        assert binding_identity_revision(aid, agent["local_project_id"], config) == binding_identity_revision(
            aid, agent["local_project_id"], agent["runtime_config"])
        assert SECRET not in json.dumps(list(sup.pending))
    finally:
        await sup.aclose()


def test_binding_mutable_identity_and_browser_private_path_rejection(configured_runtime):
    sup, store, agent, workspace, source = configured_runtime
    initial = store.ensure_binding(agent)
    updated = {**agent, "runtime_config": {**agent["runtime_config"], "llm": {**LLM, "reasoning_effort": "low"}}}
    result = store.ensure_binding(updated)
    assert result["revision"] != initial["revision"] and result["profile_name"] == initial["profile_name"]
    with pytest.raises(BindingError, match="binding_identity_conflict"):
        store.ensure_binding({**updated, "runtime_config": {"model": "other-model"}})
    with pytest.raises(ValueError):
        store.ensure_binding({**agent, "runtime_config": {"credential_key_path": str(workspace)}})
    wire = store.worker_binding(agent["id"])
    with pytest.raises(IntegrationError, match="invalid_binding"):
        _binding({**wire, "config": {"credential_key_path": str(workspace)}})


@pytest.mark.asyncio
async def test_update_waits_for_confirmed_old_process_exit(configured_runtime, monkeypatch):
    sup, store, agent, workspace, source = configured_runtime
    aid = agent["id"]
    try:
        await sup._ensure_library_worker(aid)
        old = sup._deeporca_workers[aid]
        revision = store.get_binding(aid)["revision"]
        config = {**agent["runtime_config"], "llm": {**LLM, "model": "new-model"}}
        sup.agents[aid] = {**agent, "runtime_config": config}
        (workspace / "expected.json").write_text(json.dumps({**config["llm"], "api_key": SECRET}))
        monkeypatch.setattr(old, "is_retired", lambda: False)
        with pytest.raises(BindingError, match="configuration_busy"):
            await sup._ensure_library_worker(aid)
        assert store.get_binding(aid)["revision"] == revision
        assert (workspace / "applied-models").read_text().splitlines() == [LLM["model"]]
        monkeypatch.setattr(old, "is_retired", lambda: True)
        await sup._ensure_library_worker(aid)
        assert store.get_binding(aid)["revision"] != revision
        assert (workspace / "applied-models").read_text().splitlines() == [LLM["model"], "new-model"]
    finally:
        await sup.aclose()

# ---------------------------------------------------------------------------
# Existing-profile discovery, binding, and lifecycle coverage
# ---------------------------------------------------------------------------

EXISTING_PROFILE_SDK = '''
import json
import os
from pathlib import Path
EMBEDDED_API_VERSION = 1
PROFILE_CONFIGURATION_API_VERSION = 1

def ensure_profile(name, *, home=None, template_dir=None):
    root = Path(home) / 'agents' / name
    if (root / '.state.json').exists():
        return {'created': False, 'configured': True}
    root.mkdir(parents=True)
    (root / '.state.json').write_text('{}')
    (root / 'config.yaml').write_text('security: minimal')
    return {'created': True, 'configured': True}

class EmbeddedRuntime:
    def __init__(self, name, workspace, *, home=None, profile_mode='managed'):
        assert Path(home).is_absolute()
        assert Path(workspace).is_absolute()
        assert name not in ('.', '..', 'rejected')
        assert not any(c in name for c in '/\\\\:')
        self.root = Path(home) / 'agents' / name
        self.workspace = Path(workspace)
        self.mode = profile_mode
        if self.mode == 'existing':
            assert self.root.is_dir()
    async def start(self, *args, **kwargs):
        if self.mode == 'existing':
            assert args == () and kwargs == {}, 'never pass configuration'
            assert os.environ['DEEPORCA_HOME'] == str(self.root.parent.parent)
            if (self.root / '.runtime.json').exists() or not (self.root / 'config.yaml').is_file():
                raise RuntimeError('PRIVATE-TOKEN ' + str(self.root))
            if (self.root / '.state.json').read_text() != '{}':
                raise ValueError('PRIVATE-TOKEN ' + str(self.root))
        (self.workspace / 'started.json').write_text(json.dumps({'mode':self.mode, 'home':str(self.root.parent.parent), 'name':self.root.name}))
        count = self.workspace / 'start-count'
        count.write_text(str((int(count.read_text()) if count.exists() else 0) + 1))
        return {'configured': True}
    async def close(self): pass
    async def interrupt(self): pass
    async def run_turn(self, session_id, text, *, message_id, model=None, on_event):
        assert model is None
        await on_event({'type':'done', 'turn_id':'native-turn', 'sequence':1})
        return {'status':'completed', 'turn_id':'native-turn'}
'''


@pytest.fixture
def local(tmp_path, monkeypatch):
    source = tmp_path / 'sdk'
    package = source / 'deeporca'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(
        "import os\nfrom pathlib import Path\ndef get_deeporca_dir(): return Path(os.environ['DEEPORCA_HOME'])\n")
    (package / 'embedded.py').write_text(EXISTING_PROFILE_SDK)
    home = tmp_path / 'private-native-home'
    profile = home / 'agents' / 'Native profile'
    profile.mkdir(parents=True)
    (profile / '.state.json').write_text('{}')
    (profile / 'config.yaml').write_text('model: ORIGINAL\napi_key: PRIVATE-TOKEN\nsecurity: standard\n')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    for key in ('AGENTBRIDGE_DEEPORCA_HOME', 'DEEPBOX_DEEPORCA_HOME',
                'AGENTBRIDGE_DEEPORCA_TEMPLATE_DIR', 'DEEPBOX_DEEPORCA_TEMPLATE_DIR'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AGENTBRIDGE_DEEPORCA_SOURCE', str(source))
    monkeypatch.setenv('DEEPORCA_HOME', str(home))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'appdata'))
    ref = profile_ref(profile)
    selection = {'id': ref, 'home': str(home), 'profile_name': profile.name}
    config = validate_runtime_config({'profile': {'mode': 'bind', 'profile_ref': ref, 'native_stopped': True}})
    agent = {'id': 'agent', 'local_project_id': 'project', 'runtime': 'deeporca',
             'execution_mode': 'library', 'renderer': 'deeporca-chat-v1',
             'cwd': str(workspace), 'runtime_config': config}
    store = DeepOrcaStore(tmp_path / 'local.db', namespace='ns')
    yield SimpleNamespace(source=source, home=home, profile=profile, workspace=workspace,
                          selection=selection, ref=ref, agent=agent, config=config,
                          store=store, tmp=tmp_path)
    store.close()


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


def test_probe_catalog_is_private_bounded_read_only_and_cwd_independent(local, monkeypatch):
    incomplete = local.home / 'agents' / 'incomplete'
    incomplete.mkdir()
    (incomplete / 'config.yaml').write_text('PRIVATE-TOKEN')
    rejected = local.home / 'agents' / 'rejected'
    rejected.mkdir()
    for name in ('.state.json', 'config.yaml'):
        (rejected / name).write_text('{}')
    before = snapshot(local.home)
    monkeypatch.chdir(local.workspace)
    private = probe_sdk()
    assert private['existing_api'] is True
    assert private['existing_profiles'] == [local.selection]
    public = probe_family('deeporca', local_state_path=local.tmp / 'probe.db')
    config = public['agent_config']
    assert config['profile_modes'] == ['create', 'bind']
    assert config['existing_profiles'] == [{'id': local.ref, 'label': local.profile.name}]
    encoded = json.dumps(public)
    assert str(local.home) not in encoded
    assert 'private-native-home' not in encoded and 'PRIVATE-TOKEN' not in encoded
    assert 'profile_name' not in encoded and 'config.yaml' not in encoded
    assert snapshot(local.home) == before
    assert not (local.workspace / 'started.json').exists()
    assert resolve_existing_profile(local.ref) == local.selection


@pytest.mark.parametrize('override', ['AGENTBRIDGE_DEEPORCA_HOME', 'DEEPBOX_DEEPORCA_HOME'])
def test_connector_local_home_override(local, monkeypatch, override):
    empty = local.tmp / 'empty-home'
    empty.mkdir()
    monkeypatch.setenv(override, str(empty))
    assert probe_sdk()['existing_profiles'] == []
    with pytest.raises(BindingError, match='^existing_profile_unavailable$'):
        resolve_existing_profile(local.ref)


def test_catalog_bounds_and_ref_stability(local):
    for index in range(110):
        root = local.home / 'agents' / f'profile-{index}'
        root.mkdir()
        (root / '.state.json').write_text('{}')
        (root / 'config.yaml').write_text('PRIVATE-TOKEN')
    records = probe_sdk()['existing_profiles']
    assert len(records) == 100
    assert profile_ref(local.profile) == local.ref


def test_catalog_does_not_follow_linked_profiles(local):
    outside = local.tmp / 'outside'
    outside.mkdir()
    for name in ('.state.json', 'config.yaml'):
        (outside / name).write_text('{}')
    try:
        (local.home / 'agents' / 'linked').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    assert probe_sdk()['existing_profiles'] == [local.selection]


@pytest.mark.parametrize('ref', ['native-' + 'f' * 32, '../profile', 'Native profile',
                                'native-' + 'A' * 32, 'native-x', None])
def test_missing_or_unsafe_refs_fail_closed(local, ref):
    with pytest.raises(BindingError, match='^existing_profile_unavailable$'):
        resolve_existing_profile(ref)


def test_signature_requires_explicit_existing_keyword(local):
    class Legacy:
        def __init__(self, name, workspace, **kwargs): pass
    assert not existing_profile_api(SimpleNamespace(EmbeddedRuntime=Legacy))
    old = EXISTING_PROFILE_SDK.replace(
        "def __init__(self, name, workspace, *, home=None, profile_mode='managed'):",
        "def __init__(self, name, workspace, *, home=None, **kwargs):\n        profile_mode = 'managed'")
    (local.source / 'deeporca' / 'embedded.py').write_text(old)
    public = probe_family('deeporca', local_state_path=local.tmp / 'probe.db')
    assert public['agent_config']['profile_modes'] == ['create']
    assert not public['agent_config'].get('existing_profiles')
    with pytest.raises(BindingError, match='^existing_profile_api_unavailable$'):
        resolve_existing_profile(local.ref)


def test_store_persists_immutable_selection_and_retires_only_reservation(local):
    before = snapshot(local.home)
    with pytest.raises(BindingError, match='existing_profile_unavailable'):
        local.store.ensure_binding(local.agent)
    stored = local.store.ensure_binding(local.agent, resolved_profile=local.selection)
    assert stored['config'] == local.config
    assert (stored['home'], stored['profile_name']) == (str(local.home), local.profile.name)
    reopened = DeepOrcaStore(local.store.path, namespace='ns')
    assert reopened.get_binding('agent') == stored
    with pytest.raises(BindingError, match='existing_profile_unavailable'):
        reopened.ensure_binding({**local.agent, 'id': 'other'}, resolved_profile=local.selection)
    for changes in ({'local_project_id': 'different'}, {'cwd': str(local.tmp)}):
        with pytest.raises(BindingError, match='binding_identity_conflict'):
            reopened.ensure_binding({**local.agent, **changes}, resolved_profile=local.selection)
    reopened.retire('agent')
    reopened.ensure_binding({**local.agent, 'id': 'other'}, resolved_profile=local.selection)
    reopened.close()
    assert snapshot(local.home) == before


def test_store_revalidates_private_mapping(local):
    local.store.ensure_binding(local.agent, resolved_profile=local.selection)
    with local.store._write() as db:
        db.execute("UPDATE bindings SET profile_name='different' WHERE agent_id='agent'")
    with pytest.raises(BindingError, match='binding_identity_conflict'):
        local.store.ensure_binding(local.agent, resolved_profile=local.selection)
    for bad in ({**local.selection, 'profile_name': '../outside'},
                {**local.selection, 'home': str(local.tmp / 'elsewhere')},
                {**local.selection, 'path': str(local.profile)}):
        with pytest.raises(BindingError, match='existing_profile_unavailable'):
            local.store.ensure_binding(local.agent, resolved_profile=bad)


def worker_binding(local):
    local.store.ensure_binding(local.agent, resolved_profile=local.selection)
    return local.store.worker_binding('agent')


def test_existing_worker_never_ensures_or_configures_and_preserves_profile(local):
    # Remove even the ensure API: bindings must not inspect or invoke it.
    sdk = EXISTING_PROFILE_SDK.replace('def ensure_profile(', 'def forbidden_ensure_profile(')
    sdk = sdk.replace('PROFILE_CONFIGURATION_API_VERSION = 1', '')
    (local.source / 'deeporca' / 'embedded.py').write_text(sdk)
    before = snapshot(local.home)
    async def scenario():
        worker = DeepOrcaWorker(worker_binding(local))
        try:
            ready = await worker.start()
            assert ready['configured'] is True and ready['created'] is False
            started = json.loads((local.workspace / 'started.json').read_text())
            assert started == {'mode': 'existing', 'home': str(local.home), 'name': local.profile.name}
        finally:
            await worker.close()
    asyncio.run(scenario())
    assert snapshot(local.home) == before


@pytest.mark.parametrize('bad_state', ['busy', 'invalid', 'missing'])
def test_native_start_errors_are_safe_and_never_repair_existing(local, bad_state):
    binding = worker_binding(local)
    if bad_state == 'busy':
        (local.profile / '.runtime.json').write_text(json.dumps({'pid': os.getpid()}))
    elif bad_state == 'invalid':
        (local.profile / '.state.json').write_text('PRIVATE-TOKEN')
    else:
        (local.profile / 'config.yaml').unlink()
    before = snapshot(local.home)
    async def scenario():
        worker = DeepOrcaWorker(binding)
        try:
            with pytest.raises(IntegrationError) as error:
                await worker.start()
            assert error.value.code == 'existing_profile_unavailable'
            assert 'PRIVATE-TOKEN' not in str(error.value)
            assert str(local.home) not in str(error.value)
        finally:
            await worker.close()
    asyncio.run(scenario())
    assert snapshot(local.home) == before


def test_existing_worker_fails_closed_on_legacy_sdk(local):
    old = EXISTING_PROFILE_SDK.replace(
        "def __init__(self, name, workspace, *, home=None, profile_mode='managed'):",
        "def __init__(self, name, workspace, *, home=None, **kwargs):\n        profile_mode = 'managed'")
    (local.source / 'deeporca' / 'embedded.py').write_text(old)
    async def scenario():
        worker = DeepOrcaWorker(worker_binding(local))
        try:
            with pytest.raises(IntegrationError) as error:
                await worker.start()
            assert error.value.code == 'existing_profile_api_unavailable'
        finally:
            await worker.close()
    asyncio.run(scenario())
    assert not (local.workspace / 'started.json').exists()


def test_managed_worker_uses_sdk_defaults_without_security_overrides(local):
    agent = {**local.agent, 'runtime_config': {}}
    binding = local.store.ensure_binding(agent)
    async def provision():
        return await DeepOrcaWorker(local.store.worker_binding('agent')).provision()
    assert asyncio.run(provision())['created'] is True
    config = Path(binding['home']) / 'agents' / binding['profile_name'] / 'config.yaml'
    assert config.read_text() == 'security: minimal'
    config.write_text('security: standard\nmodel: unchanged')
    before = snapshot(config.parent)
    assert asyncio.run(provision())['created'] is False
    assert snapshot(config.parent) == before


def test_supervisor_resolves_in_child_and_skips_throwaway_provision(local):
    before = snapshot(local.home)
    async def scenario():
        projects = LocalProjectStore(local.tmp / 'projects.db')
        project = projects.add(str(local.workspace), 'Project')
        agent = {**local.agent, 'local_project_id': project.id}
        supervisor = SessionSupervisor({'agent': agent}, local_store=projects, deeporca_store=local.store)
        try:
            worker = await supervisor._ensure_library_worker('agent')
            assert worker.is_alive()
            assert (local.workspace / 'start-count').read_text() == '1'
            assert local.store.public_status('agent')['runtime_status']['state'] == 'ready'
            supervisor.replace_agents({'agent': agent, 'second': {**agent, 'id': 'second'}})
            with pytest.raises(BindingError, match='existing_profile_unavailable'):
                await supervisor._ensure_library_worker('second')
            assert any(frame.get('agent_id') == 'second'
                       and frame.get('runtime_status', {}).get('code') == 'existing_profile_unavailable'
                       for frame in supervisor.pending)
        finally:
            await supervisor.aclose()
            projects.close()
    asyncio.run(scenario())
    assert snapshot(local.home) == before


def test_authoritative_directory_retires_offline_deleted_binding_only_in_its_namespace(local):
    before = snapshot(local.home)
    local.store.ensure_binding(local.agent, resolved_profile=local.selection)
    foreign = DeepOrcaStore(local.tmp / 'local.db', namespace='other-enrollment')
    foreign.ensure_binding({**local.agent, 'id': 'foreign'}, resolved_profile=local.selection)
    local.store.close()  # Connector was stopped when the Server deleted Agent A.
    restored = DeepOrcaStore(local.tmp / 'local.db', namespace='ns')

    async def scenario():
        supervisor = SessionSupervisor(deeporca_store=restored)
        try:
            supervisor.replace_agents([])  # HTTP bootstrap has no A in memory.
            await supervisor.handle_control({'type': 'agents', 'agents': [{'id': None}]})
            assert restored.active_agent_ids() == {'agent'}
            replacement = {**local.agent, 'id': 'replacement'}
            with pytest.raises(BindingError, match='existing_profile_unavailable'):
                restored.ensure_binding(replacement, resolved_profile=local.selection)
            await supervisor.handle_control({'type': 'agents', 'agents': []})
            assert restored.active_agent_ids() == set()
            restored.ensure_binding(replacement, resolved_profile=local.selection)
            assert restored.active_agent_ids() == {'replacement'}
            assert foreign.active_agent_ids() == {'foreign'}
        finally:
            await supervisor.aclose()

    try:
        asyncio.run(scenario())
    finally:
        foreign.close()
    assert snapshot(local.home) == before


def test_directory_retirement_keeps_reservation_until_worker_exit_is_confirmed(local):
    local.store.ensure_binding(local.agent, resolved_profile=local.selection)

    class Worker:
        retired = False

        async def close(self):
            pass

        def is_retired(self):
            return self.retired

    async def scenario():
        supervisor = SessionSupervisor(deeporca_store=local.store)
        worker = Worker()
        supervisor._deeporca_workers['agent'] = worker
        try:
            with pytest.raises(BindingError, match='stop_uncertain'):
                await supervisor.handle_control({'type': 'agents', 'agents': []})
            assert local.store.active_agent_ids() == {'agent'}
            assert supervisor._deeporca_workers['agent'] is worker
            worker.retired = True
            await supervisor.handle_control({'type': 'agents', 'agents': []})
            assert local.store.active_agent_ids() == set()
            assert 'agent' not in supervisor._deeporca_workers
        finally:
            worker.retired = True
            await supervisor.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['startup', 'provisioning'])
def test_deletion_during_startup_keeps_unconfirmed_worker_owned(local, monkeypatch, stage):
    monkeypatch.setattr('connector.integrations.deeporca.profiles.resolve_existing_profile',
                        lambda ref: local.selection)

    async def scenario():
        entered = asyncio.Event()

        class Worker:
            retired = False

            async def start(self):
                entered.set()
                await asyncio.Event().wait()

            provision = start

            async def close(self):
                pass

            def is_retired(self):
                return self.retired

        worker = Worker()
        supervisor = SessionSupervisor(deeporca_store=local.store,
                                       deeporca_worker_factory=lambda binding: worker)
        agent = local.agent if stage == 'startup' else {**local.agent, 'runtime_config': {
            'integration_version': 1, 'profile': {
                'mode': 'create', 'configuration_template_ref': 'connector-default'}}}
        try:
            supervisor.replace_agents([agent])
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(BindingError, match='stop_uncertain'):
                await supervisor.handle_control({'type': 'agents', 'agents': []})
            assert local.store.active_agent_ids() == {'agent'}
            assert supervisor._deeporca_workers['agent'] is worker
            worker.retired = True
            await supervisor.handle_control({'type': 'agents', 'agents': []})
            assert local.store.active_agent_ids() == set()
            assert 'agent' not in supervisor._deeporca_workers
        finally:
            worker.retired = True
            await supervisor.aclose()

    asyncio.run(scenario())
