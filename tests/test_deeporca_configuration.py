"""Connector-only, hermetic sealing/configuration/lifecycle regression tests."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agentbridge.integrations.deeporca.contract import binding_identity_revision, binding_revision
from connector.integrations.deeporca.credentials import (
    AAD_PREFIX, decrypt_credential, key_path, public_key_info, runtime_configuration)
from connector.integrations.deeporca.events import IntegrationError
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


SDK = r'''
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
    (package / "embedded.py").write_text(SDK)
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
        (source / "deeporca" / "embedded.py").write_text(SDK.replace("PROFILE_CONFIGURATION_API_VERSION = 1", ""))
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
