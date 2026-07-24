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

    def test_microsoft_auth_bicep_is_tenant_scoped_and_secretless(self) -> None:
        bicep = (ROOT / "infra" / "microsoft-auth.bicep").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: 'authsettingsV2'", bicep)
        self.assertIn(
            "federatedCredentialSettingName = "
            "'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'",
            bicep,
        )
        self.assertIn(
            "clientSecretSettingName: federatedCredentialSettingName",
            bicep,
        )
        self.assertNotIn("MICROSOFT_PROVIDER_AUTHENTICATION_SECRET", bicep)
        self.assertNotIn("clientSecret:", bicep)
        self.assertNotIn("@secure()", bicep)
        self.assertIn("environment().authentication.loginEndpoint", bicep)
        self.assertIn("'${tenantId}/v2.0'", bicep)
        self.assertIn("unauthenticatedClientAction: 'AllowAnonymous'", bicep)
        self.assertIn("requireHttps: true", bicep)
        self.assertIn("enabled: false", bicep)

    def test_microsoft_auth_helper_configures_managed_identity_federation(self) -> None:
        script = (ROOT / "scripts" / "configure-microsoft-auth.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("az ad app update", script)
        self.assertIn("--enable-id-token-issuance true", script)
        self.assertIn("az ad sp show", script)
        self.assertIn("az ad sp create", script)
        self.assertIn("az identity create", script)
        self.assertIn("az webapp identity assign", script)
        self.assertIn("az ad app federated-credential create", script)
        self.assertIn("api://AzureADTokenExchange", script)
        self.assertIn("OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID", script)
        self.assertIn("--slot-settings", script)
        self.assertIn("infra/microsoft-auth.bicep", script.replace("\\", "/"))
        self.assertLess(
            script.index("az ad app update"),
            script.index("az deployment group create"),
        )
        self.assertLess(
            script.index("az ad sp create"),
            script.index("az deployment group create"),
        )
        self.assertNotIn("az ad app credential", script)
        self.assertNotIn("MICROSOFT_PROVIDER_AUTHENTICATION_SECRET", script)
        self.assertNotIn("DEEPBOX_AUTH_MODE=", script)


if __name__ == "__main__":
    unittest.main()
