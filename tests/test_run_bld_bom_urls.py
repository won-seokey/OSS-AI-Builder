import importlib.util
import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPOSITORY_ROOT / "jenkins_scripts" / "run-bld.py"


spec = importlib.util.spec_from_file_location("run_bld", RUNNER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load runner module: {RUNNER_PATH}")
run_bld = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_bld)


class RunBldBomUrlTests(unittest.TestCase):
    def test_bom_api_url_is_preserved_as_components_deep_link(self) -> None:
        bom_url = (
            "https://192.0.2.10/api/projects/project-1/"
            "versions/version-2/components"
        )

        self.assertEqual(
            run_bld.blackduck_ui_url_from_bom_url(bom_url),
            bom_url,
        )

    def test_detect_output_extracts_the_last_bom_url_for_one_unit(self) -> None:
        fbl_url = (
            "https://192.0.2.10/api/projects/project-1/"
            "versions/fbl-version/components"
        )
        appl_url = (
            "https://192.0.2.10/api/projects/project-1/"
            "versions/appl-version/components"
        )
        output = (
            "Detect output\n"
            f"Black Duck SCA Project BOM: {fbl_url}\n"
            f"Black Duck SCA Project BOM: {appl_url}\n"
        )

        self.assertEqual(
            run_bld.extract_blackduck_urls(output),
            (
                appl_url,
                appl_url,
            ),
        )

    def test_unit_result_keeps_blackduck_identity_and_links_together(self) -> None:
        result = run_bld.unit_result(
            "FBL",
            "SUCCESS",
            False,
            "Renault Gen3",
            "[OSS-42]SW V4.0.0_FBL",
            "https://192.0.2.10/api/projects/project-1/versions/fbl/components",
            "https://192.0.2.10/api/projects/project-1/versions/fbl/components",
        )

        self.assertEqual(result["unit"], "FBL")
        self.assertEqual(result["blackduck_project"], "Renault Gen3")
        self.assertEqual(result["blackduck_version"], "[OSS-42]SW V4.0.0_FBL")
        self.assertTrue(result["blackduck_bom_url"].endswith("/versions/fbl/components"))
        self.assertTrue(result["blackduck_ui_url"].endswith("/versions/fbl/components"))

    def test_detect_capture_writes_unit_log_and_extracts_its_bom_url(self) -> None:
        bom_url = (
            "https://192.0.2.10/api/projects/project-1/"
            "versions/fbl-version/components"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            producer_script = temporary_path / "fake_detect.py"
            producer_script.write_text(
                "print('Black Duck SCA Project BOM: " + bom_url + "')\n",
                encoding="utf-8",
            )
            log_path = temporary_path / "detect_fbl.log"
            command = f'"{sys.executable}" "{producer_script}"'

            return_code, captured_bom_url, captured_ui_url = run_bld.run_blackduck_detect(
                command,
                "FBL",
                log_path,
            )

            self.assertEqual(return_code, 0)
            self.assertEqual(captured_bom_url, bom_url)
            self.assertTrue(captured_ui_url.endswith("/versions/fbl-version/components"))
            self.assertIn(bom_url, log_path.read_text(encoding="utf-8"))

    def test_checkout_commands_use_teamforge_credential_username(self) -> None:
        commands = [
            "git clone https://old-user@teamforge.example.com/gerrit/project",
            "repo init -u ssh://${TEAMFORGE_USER}@teamforge.example.com:29418/manifest",
        ]
        with patch.dict(
            os.environ,
            {"TEAMFORGE_USER": "teamforge-user", "SERVICE_USER": "legacy-user"},
            clear=True,
        ):
            sanitized = run_bld.sanitize_checkout_commands(commands)

        self.assertIn("https://teamforge-user@teamforge.example.com/gerrit/project", sanitized[0])
        self.assertIn("ssh://teamforge-user@teamforge.example.com:29418/manifest", sanitized[1])
        self.assertNotIn("legacy-user", " ".join(sanitized))

    def test_checkout_credential_scope_uses_command_host(self) -> None:
        self.assertEqual(
            run_bld.credential_scope_for_command(
                "git clone https://github.com/example-org/private-repo.git",
            ),
            "github",
        )
        self.assertEqual(
            run_bld.credential_scope_for_command(
                "git clone https://teamforge.example.com/gerrit/project",
            ),
            "teamforge",
        )

    def test_cached_github_remote_uses_github_credential_scope(self) -> None:
        with patch.object(
            run_bld,
            "_git_remote_url",
            return_value="https://github.com/example-org/private-repo.git",
        ):
            self.assertEqual(
                run_bld.credential_scope_for_command(
                    "git fetch --all --prune",
                    Path("C:/workspace/private-repo"),
                ),
                "github",
            )

    def test_wsl_checkout_credential_scope_uses_each_command_host(self) -> None:
        self.assertEqual(
            run_bld.credential_scope_for_wsl_command(
                "git clone https://github.com/example-org/private-repo.git",
                "/home/jenkins/cache",
            ),
            "github",
        )
        self.assertEqual(
            run_bld.credential_scope_for_wsl_command(
                "repo --no-pager sync -j8",
                "/home/jenkins/cache",
            ),
            "teamforge",
        )

    def test_askpass_uses_teamforge_credential_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(
                os.environ,
                {
                    "TEAMFORGE_USER": "teamforge-user",
                    "TEAMFORGE_PASSWORD": " teamforge-password ",
                },
                clear=True,
            ):
                _, cleanup = run_bld.create_askpass(
                    Path(temporary_directory),
                    "windows",
                )
                askpass_content = Path(cleanup[0]).read_text(encoding="utf-8")
                username = subprocess.run(
                    [sys.executable, str(cleanup[0]), "Username for 'https://teamforge.example.com':"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.rstrip("\r\n")
                password = subprocess.run(
                    [sys.executable, str(cleanup[0]), "Password for 'https://teamforge-user@teamforge.example.com':"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.rstrip("\r\n")
                for path in cleanup:
                    path.unlink()

            self.assertIn(" teamforge-password ", askpass_content)
            self.assertEqual(username, "teamforge-user")
            self.assertEqual(password, " teamforge-password ")
            self.assertEqual([path for path in cleanup if path.exists()], [])

    def test_askpass_uses_github_token_for_github_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "github-token"},
                clear=True,
            ):
                _, cleanup = run_bld.create_askpass(
                    Path(temporary_directory),
                    "windows",
                    credential_scope="github",
                )
                username = subprocess.run(
                    [sys.executable, str(cleanup[0]), "Username for 'https://github.com':"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                password = subprocess.run(
                    [sys.executable, str(cleanup[0]), "Password for 'https://github.com':"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                askpass_content = Path(cleanup[0]).read_text(encoding="utf-8")
                for path in cleanup:
                    path.unlink()

            self.assertEqual(username, "x-access-token")
            self.assertEqual(password, "github-token")
            self.assertNotIn("TEAMFORGE_PASSWORD", askpass_content)
            self.assertEqual([path for path in cleanup if path.exists()], [])

    def test_checkout_environment_disables_existing_git_credential_helpers(self) -> None:
        environment = run_bld.checkout_credential_environment("C:/workspace/askpass.bat")

        self.assertEqual(environment["GIT_ASKPASS"], "C:/workspace/askpass.bat")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")

    def test_create_askpass_removes_file_when_wsl_chmod_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(
                os.environ,
                {
                    "TEAMFORGE_USER": "teamforge-user",
                    "TEAMFORGE_PASSWORD": "teamforge-password",
                },
                clear=True,
            ), patch.object(
                run_bld,
                "run_wsl_bash",
                return_value=SimpleNamespace(returncode=1),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed to make askpass executable"):
                    run_bld.create_askpass(Path(temporary_directory), "wsl")

            self.assertEqual(list(Path(temporary_directory).glob("askpass_*")), [])

    def test_wsl_checkout_removes_askpass_file_after_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            calls = []

            def fake_run_wsl_bash(script, distro="default", capture=False, env=None):
                calls.append((script, env))
                if script == "whoami":
                    return subprocess.CompletedProcess([], 0, stdout="wsl-user\n")
                if "set -e" in script:
                    return subprocess.CompletedProcess([], 1, stdout="checkout failed\n")
                return subprocess.CompletedProcess([], 0, stdout="")

            unit = {
                "name": "FBL",
                "checkout_commands": ["repo sync"],
                "scan_paths": [],
                "blackduck_project": "Renault Gen3",
                "allow_failure": False,
            }
            with patch.dict(
                os.environ,
                {
                    "JIRA_KEY": "OSS-42",
                    "OSS_CACHE_ROOT": temporary_directory,
                    "TEAMFORGE_USER": "teamforge-user",
                    "TEAMFORGE_PASSWORD": "teamforge-password",
                },
                clear=True,
            ), patch.object(run_bld, "run_wsl_bash", side_effect=fake_run_wsl_bash), patch.object(
                run_bld, "fix_ssh_permissions"
            ):
                result = run_bld.process_unit(
                    unit,
                    Path(temporary_directory) / "base",
                    "RENAULT",
                )

            self.assertEqual(result["status"], "FAILED")
            self.assertTrue(any("set -e" in script for script, _ in calls))
            self.assertEqual(list(Path(temporary_directory).rglob("askpass_*")), [])


if __name__ == "__main__":
    unittest.main()
