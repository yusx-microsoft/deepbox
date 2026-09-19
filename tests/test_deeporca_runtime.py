import json
from pathlib import Path
import subprocess
import sys

import pytest

from connector import runtimes
from connector.runtime_probe import ProbeResult, availability, probe_family
from connector.integrations.deeporca.store import BindingError, DeepOrcaStore, enrollment_namespace


def test_library_descriptor_has_no_cli_or_approval_arguments():
    from connector.integrations.deeporca.probe import probe

    adapter = runtimes.get("deeporca")
    assert adapter.capability_probe is probe
    assert adapter.backend == "python-library"
    assert adapter.base_argv == ()
    assert adapter.surface_id == "structured"
    assert adapter.permission_modes == {}
    with pytest.raises(runtimes.InvalidCommandError, match="library"):
        runtimes.build_command("deeporca")


def test_integration_imports_and_worker_pickling_do_not_import_native_sdk():
    # Spawn imports the target by its module name. A clean interpreter verifies
    # both the relocated import boundary and the real pickled entrypoint, with
    # no compatibility aliases and no import of an installed/user-local SDK.
    code = "\n".join((
        "import pickle, sys",
        "from connector import runtime_probe, runtimes, supervisor",
        "from connector.integrations.deeporca import adapter, events, probe, session, store, worker",
        "assert worker._worker_main.__module__ == 'connector.integrations.deeporca.worker'",
        "assert pickle.loads(pickle.dumps(worker._worker_main)) is worker._worker_main",
        "assert adapter.create_adapter().capability_probe is probe.probe",
        "assert not any(n == 'deeporca' or n.startswith('deeporca.') for n in sys.modules)",
        "assert not any(n.startswith('connector.deeporca_') for n in sys.modules)",
    ))
    result = subprocess.run([sys.executable, "-c", code],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("data, installed, compatible", [
    ({"installed": False}, False, False),
    ({"installed": True, "api": 1, "version": "0.8.0"}, True, True),
    ({"installed": True, "api": 2}, True, False),
    ({"installed": True, "api": True}, True, False),
    ({"installed": True, "error": "embedded_api_unavailable"}, True, False),
])
def test_optional_library_probe(data, installed, compatible):
    calls = []
    def runner(argv, timeout):
        calls.append((argv, timeout))
        return ProbeResult(0, json.dumps(data))
    cap = probe_family("deeporca", runner=runner)
    assert len(calls) == 1 and calls[0][0][1] == "-c"
    assert (cap["installation"]["status"] == "installed") is installed
    assert availability(cap, "structured")[0] is compatible
    assert cap["agent_config"]["profile_modes"] == ["create"]
    features = cap["surfaces"][0]["features"]
    assert features["renderer"] == "deeporca-chat-v1"
    assert features["interactive_approval"] is False
    assert features["permission_modes"] == []


def test_probe_never_publishes_raw_diagnostics():
    raw = {"installed": True, "api": 1, "version": "C:/private/token=secret"}
    cap = probe_family("deeporca", runner=lambda *_: ProbeResult(0, json.dumps(raw)))
    assert "secret" not in json.dumps(cap)
    assert cap["installation"]["version"] is None
    broken = probe_family("deeporca", runner=lambda *_: ProbeResult(1, "private error"))
    assert broken["installation"]["status"] == "missing"
    assert "private error" not in json.dumps(broken)


@pytest.fixture
def store(tmp_path):
    value = DeepOrcaStore(tmp_path / "bindings.db", namespace="enrollment-one")
    yield value
    value.close()


def agent(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return {"id": "agent-1", "local_project_id": "project-1", "runtime": "deeporca",
            "cwd": str(project), "runtime_config": {}}


def test_bindings_idempotent_private_and_namespace_isolated(store, tmp_path):
    record = agent(tmp_path)
    first = store.ensure_binding(record)
    assert first == store.ensure_binding({**record, "display_name": "renamed"})
    assert first["profile_name"].startswith("dbx-")
    store.set_status(record["id"], "ready")
    public = store.public_status(record["id"])
    assert public["runtime_status"]["state"] == "ready"
    assert "workspace" not in json.dumps(public) and str(tmp_path) not in json.dumps(public)
    store.set_namespace("enrollment-two")
    second = store.ensure_binding(record)
    assert first["home"] != second["home"]
    assert first["profile_name"] != second["profile_name"]


def test_binding_identity_cannot_be_changed_or_silently_reactivated(store, tmp_path):
    record = agent(tmp_path)
    first = store.ensure_binding(record)
    with pytest.raises(BindingError, match="binding_identity_conflict"):
        store.ensure_binding({**record, "local_project_id": "other-project"})
    store.retire(record["id"])
    assert store.public_status(record["id"]) is None
    assert store.get_binding(record["id"])["home"] == first["home"]
    with pytest.raises(BindingError, match="binding_identity_conflict"):
        store.ensure_binding(record)


def test_native_identity_survives_reopen(tmp_path):
    path = tmp_path / "bindings.db"
    store = DeepOrcaStore(path, namespace="one")
    sid = store.native_session_id("a", "s")
    assert store.native_session_id("b", "s") != sid
    store.close()
    store = DeepOrcaStore(path, namespace="one")
    assert store.native_session_id("a", "s") == sid
    store.set_namespace("two")
    assert store.native_session_id("a", "s") != sid
    store.close()


def test_crash_admission_is_uncertain_not_replayed(tmp_path):
    path = tmp_path / "bindings.db"
    store = DeepOrcaStore(path)
    store.admit_input("a", "s", "input-1", "do not run twice")
    assert store.input_receipt("a", "s", "input-1")["state"] == "running"
    store.close()
    store = DeepOrcaStore(path)
    assert store.recover_interrupted() == [{"agent_id": "a", "session_id": "s", "input_id": "input-1"}]
    assert store.input_receipt("a", "s", "input-1")["result"] == "uncertain"
    assert store.recover_interrupted() == []
    assert store.session_recovery("a", "s")[0]["input_id"] == "input-1"
    # Receipt is retained, but user input is purged after settlement.
    assert store._conn.execute("SELECT text, options FROM inputs").fetchone()["text"] == ""
    store.close()


def test_receipts_are_scoped_and_completed_input_is_not_uncertain(store):
    store.admit_input("a", "s", "i", "hello")
    store.admit_input("b", "s", "i", "different agent")
    store.settle_input("a", "s", "i", "completed")
    assert store.input_receipt("a", "s", "i")["result"] == "completed"
    assert store.input_receipt("a", "other", "i") is None
    assert store.recover_interrupted() == [{"agent_id": "b", "session_id": "s", "input_id": "i"}]


def test_enrollment_identity_is_not_a_machine_token():
    assert enrollment_namespace("http://localhost:80/", "machine-a") == enrollment_namespace("http://localhost", "machine-a")
    assert enrollment_namespace("http://localhost", "machine-a") != enrollment_namespace("http://localhost", "machine-b")


def test_spool_ownership_is_private_per_spool_and_ack_drained(store, tmp_path):
    record = agent(tmp_path)
    old = store.ensure_binding(record)
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True, native_use=True)
    with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
        store.guard_spool_enrollment("spool-a", "new-enrollment", pending=True)
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True)  # token rotation
    # An independent empty spool cannot flush the old spool's private data.
    store.guard_spool_enrollment("spool-b", "new-enrollment", pending=False)
    store.guard_spool_enrollment("spool-a", "new-enrollment", pending=False)
    store.set_namespace("new-enrollment")
    assert store.ensure_binding(record)["profile_name"] != old["profile_name"]
    assert "spool" not in json.dumps(store.public_status(record["id"]))


@pytest.mark.parametrize("legacy_table", ["bindings", "native_sessions", "inputs"])
def test_unpinned_legacy_data_is_not_assigned_to_new_pending_scope(store, tmp_path, legacy_table):
    if legacy_table == "bindings":
        store.ensure_binding(agent(tmp_path))
    elif legacy_table == "native_sessions":
        store.native_session_id("a", "s")
    else:
        store.admit_input("a", "s", "i", "private")
    with pytest.raises(BindingError, match="^enrollment_identity_conflict$"):
        store.guard_spool_enrollment("spool-a", "new-enrollment", pending=True)
    assert not store._conn.execute("SELECT * FROM spool_owners").fetchall()
    # Sole known old scope can be resumed to receive genuine ACKs.
    store.guard_spool_enrollment("spool-a", store.namespace, pending=True)


def test_empty_ledger_does_not_pin_cli_only_spool(store):
    store.guard_spool_enrollment("spool", "one", pending=True)
    store.guard_spool_enrollment("spool", "two", pending=True)
    assert not store._conn.execute("SELECT * FROM spool_owners").fetchall()


def test_default_spool_is_url_token_scoped_but_not_devbox_scoped(tmp_path):
    from connector.spool import spool_path
    # Normal CLI URL/token changes already use different physical spools. The
    # extra guard protects shared/injected paths and same-token devbox changes.
    old = spool_path("http://old", "token", str(tmp_path))
    assert old == spool_path("http://OLD:80/", "token", str(tmp_path))
    assert old != spool_path("http://new", "token", str(tmp_path))
    assert old != spool_path("http://old", "rotated-token", str(tmp_path))
