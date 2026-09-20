"""Existing-profile Connector coverage, exclusively disposable SDK/home/state."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge.integrations.deeporca.contract import validate_runtime_config
from connector.integrations.deeporca.events import IntegrationError
from connector.integrations.deeporca.probe import probe_sdk
from connector.integrations.deeporca.profiles import (
    existing_profile_api, profile_ref, resolve_existing_profile,
)
from connector.integrations.deeporca.store import BindingError, DeepOrcaStore
from connector.integrations.deeporca.worker import DeepOrcaWorker
from connector.local_store import LocalProjectStore
from connector.runtime_probe import probe_family
from connector.supervisor import SessionSupervisor


SDK = '''
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
    (package / 'embedded.py').write_text(SDK)
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
    old = SDK.replace(
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
    sdk = SDK.replace('def ensure_profile(', 'def forbidden_ensure_profile(')
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
    old = SDK.replace(
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
