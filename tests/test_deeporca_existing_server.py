"""Existing-profile references are inventory-scoped and never browser paths."""
import pytest

from agentbridge.integrations.deeporca.contract import binding_identity_revision, validate_runtime_config
from test_deeporca_routes import app_client, create, machine


REF = 'native-' + 'a' * 32
OTHER = 'native-' + 'b' * 32


def binding(ref=REF, **profile):
    return {'integration_version': 1, 'profile': {
        'mode': 'bind', 'profile_ref': ref, 'native_stopped': True, **profile}}


def advertise(main, box, refs=(REF,), modes=('create', 'bind'), *, legacy=False):
    descriptor = {'runtime': 'deeporca', 'agent_config': {'profile_modes': list(modes),
        'existing_profiles': [{'id': ref, 'label': 'Existing native profile'} for ref in refs]}}
    capabilities = [descriptor]
    if legacy:
        descriptor['id'] = descriptor.pop('runtime')
        capabilities = {'runtimes': [descriptor]}
    with main.models.SessionLocal() as db:
        db.get(main.Devbox, box).capabilities = capabilities
        db.commit()


def test_bind_contract_keeps_exact_opaque_reference_and_consent():
    desired = binding()
    parsed = validate_runtime_config(desired)
    assert parsed == desired
    parsed['profile']['profile_ref'] = OTHER
    assert desired == binding()
    assert binding_identity_revision('agent', 'project', desired) != binding_identity_revision('agent', 'project', binding(OTHER))
    assert binding_identity_revision('agent', 'project', desired) != binding_identity_revision('agent', 'project', {})


@pytest.mark.parametrize('confirmation', [None, False, 0, 1, 'true', {}, []])
def test_bind_requires_explicit_boolean_native_stop_confirmation(confirmation):
    with pytest.raises(ValueError, match='Stop native DeepOrca'):
        validate_runtime_config(binding(native_stopped=confirmation))


@pytest.mark.parametrize('profile', [
    {'mode': 'bind'}, {'mode': 'bind', 'profile_ref': REF},
    {**binding()['profile'], 'home': 'C:/private-home'},
    {**binding()['profile'], 'profile_name': 'secret-name'},
    {**binding()['profile'], 'configuration_template_ref': 'connector-default'},
    {**binding()['profile'], 'profile_ref': 'C:/private-home'},
    {**binding()['profile'], 'profile_ref': 'secret-name'},
    {**binding()['profile'], 'profile_ref': '../private-home'},
])
def test_bind_rejects_names_paths_templates_and_incomplete_configuration(profile):
    with pytest.raises(ValueError) as error:
        validate_runtime_config({'profile': profile})
    assert 'private-home' not in str(error.value)
    assert 'secret-name' not in str(error.value)


@pytest.mark.parametrize('field', ['llm', 'credential', 'model'])
def test_bound_profile_never_accepts_configuration_overrides(field):
    with pytest.raises(ValueError, match='read-only'):
        validate_runtime_config({**binding(), field: 'secret-value'})


@pytest.mark.parametrize('legacy', [False, True])
def test_create_binding_requires_inventory_from_target_machine(app_client, legacy):
    client, main = app_client
    box, _, project = machine(client)
    other, _, _ = machine(client, 'other')
    advertise(main, other)
    response = create(client, box, project, runtime_config=binding())
    assert response.status_code == 422  # A different Machine's catalog is not authority.
    advertise(main, box, modes=('create',))
    assert create(client, box, project, runtime_config=binding()).status_code == 422
    advertise(main, box, refs=(OTHER,))
    assert create(client, box, project, runtime_config=binding()).status_code == 422
    advertise(main, box, legacy=legacy)
    response = create(client, box, project, runtime_config=binding())
    assert response.status_code == 200, response.text
    agent = response.json()
    assert agent['runtime_config'] == binding()
    assert agent['runtime_status']['state'] == 'pending'
    assert create(client, box, None, handle='missing-project', runtime_config=binding()).status_code == 422


@pytest.mark.parametrize('capabilities', [None, {}, {'runtimes': None}, {'runtimes': {}},
    {'runtimes': [None, {'id': 'deeporca', 'agent_config': None}]},
    {'runtimes': [{'id': 'deeporca', 'agent_config': {'profile_modes': 'bind'}}]},
    {'runtimes': [{'id': 'deeporca', 'agent_config': {'profile_modes': ['bind'], 'existing_profiles': None}}]}])
def test_bad_or_old_inventory_fails_safely_but_create_mode_unchanged(app_client, capabilities):
    client, main = app_client
    box, _, project = machine(client)
    with main.models.SessionLocal() as db:
        db.get(main.Devbox, box).capabilities = capabilities
        db.commit()
    assert create(client, box, project, runtime_config=binding()).status_code == 422
    assert create(client, box, project, runtime_config={}).status_code == 200


def test_bound_agent_name_only_and_retry_do_not_edit_native_configuration(app_client):
    client, main = app_client
    box, _, project = machine(client)
    advertise(main, box, refs=(REF, OTHER))
    response = create(client, box, project, runtime_config=binding())
    assert response.status_code == 200
    agent = response.json()
    url = f'/api/agents/{agent["id"]}'
    old_revision = agent['runtime_status']['revision']
    updated = client.patch(url, json={'display_name': 'Renamed', 'runtime_config': binding()})
    assert updated.status_code == 200, updated.text
    assert updated.json()['runtime_status']['revision'] == old_revision
    assert client.patch(url, json={'runtime_config': binding(OTHER)}).status_code == 409
    assert client.patch(url, json={'runtime_config': {}}).status_code == 409
    for field in ('llm', 'model', 'credential'):
        assert client.patch(url, json={'runtime_config': {**binding(), field: 'private-key'}}).status_code == 422
    # Disappearing inventory does not prevent renaming/retrying an immutable
    # binding; the Connector must revalidate its pinned local target on startup.
    advertise(main, box, refs=())
    assert client.patch(url, json={'display_name': 'Still renameable'}).status_code == 200
    retry = client.post(url + '/runtime/retry', json={})
    assert retry.status_code == 200
    assert client.get(url).json()['runtime_config'] == binding()


def test_unauthorized_bind_does_not_disclose_machine_inventory(app_client):
    client, main = app_client
    box, _, project = machine(client)
    advertise(main, box)
    assert client.post('/api/auth/register', json={'username': 'outsider', 'password': 'strong-password'}).status_code == 200
    response = create(client, box, project, runtime_config=binding())
    assert response.status_code == 404
    assert REF not in response.text
