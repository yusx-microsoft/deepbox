import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from connector.client import Connector
from connector.local_store import LocalProjectStore
from connector.supervisor import SessionSupervisor


class LocalProjectStoreTests(unittest.TestCase):
    def test_project_ids_survive_reopen_and_public_shape_never_contains_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "repo"
            project_path.mkdir()
            db_path = root / "projects.db"

            with LocalProjectStore(db_path) as store:
                first = store.add(project_path, "Deepbox")
                again = store.add(project_path, "Renamed")
                self.assertEqual(first.id, again.id)
                self.assertEqual(again.name, "Renamed")
                self.assertEqual(
                    store.public_projects(),
                    [{"id": first.id, "name": "Renamed"}],
                )

            with LocalProjectStore(db_path) as reopened:
                project = reopened.get(first.id)
                self.assertIsNotNone(project)
                self.assertEqual(project.path, str(project_path.resolve()))
                self.assertNotIn(str(project_path), repr(reopened.public_projects()))

    def test_environment_variable_syntax_in_path_is_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            literal = root / "%DEEPBOX_LITERAL_PROJECT%"
            literal.mkdir()
            with patch.dict("os.environ",
                            {"DEEPBOX_LITERAL_PROJECT": "expanded-elsewhere"}):
                with LocalProjectStore(root / "projects.db") as store:
                    added = store.add(literal)
                    found = store.get_by_path(literal)
                    self.assertIsNotNone(found)
                    self.assertEqual(found.id, added.id)
                    self.assertEqual(found.path, str(literal.resolve()))

    def test_rejects_relative_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            with LocalProjectStore(Path(directory) / "projects.db") as store:
                with self.assertRaises(ValueError):
                    store.add("relative/path")
                with self.assertRaises(ValueError):
                    store.add(Path(directory) / "missing")

    def test_running_connector_and_cli_store_can_write_same_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            db_path = root / "projects.db"
            with LocalProjectStore(db_path) as connector_store:
                with LocalProjectStore(db_path) as cli_store:
                    first = cli_store.add(one)
                    second = cli_store.add(two)
                self.assertEqual(
                    {item.id for item in connector_store.list_projects()},
                    {first.id, second.id},
                )
                self.assertTrue(connector_store.remove(first.id))
                self.assertFalse(connector_store.remove(first.id))


class SupervisorProjectResolutionTests(unittest.TestCase):
    def test_legacy_cwd_is_imported_once_and_replaced_by_project_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "legacy-repo"
            project_path.mkdir()
            with LocalProjectStore(root / "projects.db") as store:
                supervisor = SessionSupervisor(local_store=store)
                supervisor.replace_agents([{
                    "id": "a1",
                    "handle": "claude",
                    "runtime": "claude-code",
                    "cwd": str(project_path),
                    "runtime_config": {"model": "sonnet"},
                }])
                agent = supervisor.agents["a1"]
                project_id = agent["local_project_id"]
                self.assertEqual(agent["cwd"], str(project_path.resolve()))
                self.assertNotIn("cwd", supervisor.pending_project_migrations()[0])
                self.assertEqual(supervisor.pending_project_migrations(), [{
                    "agent_id": "a1", "local_project_id": project_id}])

                supervisor.clear_project_migrations(
                    supervisor.pending_project_migrations())
                self.assertEqual(supervisor.pending_project_migrations(), [])

                supervisor.replace_agents([{
                    "id": "a1",
                    "handle": "claude",
                    "runtime": "claude-code",
                    "local_project_id": project_id,
                    "runtime_config": {"permission_mode": "acceptEdits"},
                }])
                agent = supervisor.agents["a1"]
                self.assertEqual(agent["cwd"], str(project_path.resolve()))
                self.assertEqual(agent["runtime_config"], {
                    "permission_mode": "acceptEdits"})

    def test_unknown_project_id_fails_closed_without_a_server_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with LocalProjectStore(Path(directory) / "projects.db") as store:
                supervisor = SessionSupervisor(local_store=store)
                supervisor.replace_agents([{
                    "id": "a1", "handle": "claude", "runtime": "claude-code",
                    "local_project_id": "missing",
                }])
                self.assertIsNone(supervisor.agents["a1"]["cwd"])


class _Response:
    def __init__(self, error=None):
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


class _HttpClient:
    def __init__(self, capture, error=None):
        self.capture = capture
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.capture.append((url, kwargs))
        return _Response(self.error)


class ConnectorProjectReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_is_path_free_and_acknowledges_migration_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "private-repo"
            project_path.mkdir()
            with LocalProjectStore(root / "projects.db") as store:
                connector = Connector("https://deepbox.example", "secret-token",
                                      local_store=store)
                connector.supervisor.replace_agents([{
                    "id": "a1", "handle": "claude", "runtime": "claude-code",
                    "cwd": str(project_path),
                }])
                capture = []
                with patch("connector.client.httpx.AsyncClient",
                           return_value=_HttpClient(capture)):
                    await connector.report_projects("box-1")

                self.assertEqual(len(capture), 1)
                url, request = capture[0]
                self.assertTrue(url.endswith("/api/devboxes/box-1/projects"))
                self.assertEqual(request["json"]["projects"][0].keys(),
                                 {"id", "name"})
                self.assertNotIn(str(project_path), repr(request["json"]))
                self.assertEqual(connector.supervisor.pending_project_migrations(), [])

    async def test_failed_report_keeps_migration_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "repo"
            project_path.mkdir()
            with LocalProjectStore(root / "projects.db") as store:
                connector = Connector("https://deepbox.example", "secret-token",
                                      local_store=store)
                connector.supervisor.replace_agents([{
                    "id": "a1", "handle": "claude", "runtime": "claude-code",
                    "cwd": str(project_path),
                }])
                with patch("connector.client.httpx.AsyncClient", return_value=
                           _HttpClient([], RuntimeError("offline"))):
                    with self.assertRaises(RuntimeError):
                        await connector.report_projects("box-1")
                self.assertEqual(len(
                    connector.supervisor.pending_project_migrations()), 1)


if __name__ == "__main__":
    unittest.main()
