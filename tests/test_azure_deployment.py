from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AzureDeploymentTests(unittest.TestCase):
    def test_startup_script_resolves_the_oryx_app_root(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes)
        script_path = ROOT / "azure-startup.sh"
        self.assertNotIn(b"\r\n", script_path.read_bytes())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn('dirname -- "${BASH_SOURCE[0]}"', script)
        self.assertIn("server.app.main:app", script)
        self.assertIn('--chdir "$app_root"', script)
        self.assertIn("--workers 1", script)

    def test_deploy_package_contains_server_web_and_startup_script(self) -> None:
        script = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")
        self.assertIn("@('server', 'web', 'requirements.txt', 'azure-startup.sh')", script)
        self.assertIn("tar.exe -a -c -f $zipPath --exclude=*/__pycache__ --exclude=*.pyc @include", script)
        self.assertNotIn("\nCompress-Archive -Path", script)

    def test_bicep_uses_the_extracted_startup_script(self) -> None:
        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("ls -t /tmp/*/azure-startup.sh | head -n 1", bicep)
        self.assertIn("healthCheckPath: '/api/ready'", bicep)
        self.assertIn("numberOfWorkers: 1", bicep)


if __name__ == "__main__":
    unittest.main()
