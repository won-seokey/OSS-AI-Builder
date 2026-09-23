import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
JENKINS_SCRIPTS = REPOSITORY_ROOT / "jenkins_scripts"
if str(JENKINS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(JENKINS_SCRIPTS))

finalize_flow = importlib.import_module("finalize_flow")
pipeline_common = importlib.import_module("pipeline_common")


class FailedRequestCleanupTests(unittest.TestCase):
    def test_default_jql_excludes_failed_requests(self) -> None:
        self.assertIn(
            "labels not in ('processed', 'processing', 'oss-failed')",
            pipeline_common.DEFAULT_JQL,
        )

    def test_cleanup_marks_failure_and_removes_request_trigger_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            (output_directory / "jira_key.txt").write_text("OSS-42\n", encoding="utf-8")
            arguments = SimpleNamespace(
                output_dir=str(output_directory),
                jira_token_env="JIRA_OSS_TOKEN",
                jira_email="operator@example.com",
                jira_base_url="https://jira.example.com",
            )
            updated_labels = ["oss-failed"]

            with patch.dict(
                os.environ,
                {"JIRA_OSS_TOKEN": "test-token"},
                clear=True,
            ), patch.object(
                finalize_flow,
                "build_basic_auth_header",
                return_value={"Authorization": "Bearer test-token"},
            ), patch.object(
                finalize_flow,
                "jira_put_json",
            ) as jira_put, patch.object(
                finalize_flow,
                "jira_get_json",
                return_value={"fields": {"labels": updated_labels}},
            ):
                result = finalize_flow.cleanup_processing_command(arguments)

        self.assertEqual(result, 0)
        jira_put.assert_called_once_with(
            "https://jira.example.com/rest/api/3/issue/OSS-42",
            {"Authorization": "Bearer test-token"},
            {
                "update": {
                    "labels": [
                        {"add": "oss-failed"},
                        {"remove": "processing"},
                        {"remove": "oss-request"},
                    ]
                }
            },
        )


class MailFailureHandlingTests(unittest.TestCase):
    def test_summary_status_and_successful_units_control_failure_mail_content(self) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is unavailable (expected 'powershell' or 'pwsh')")

        mail_script = REPOSITORY_ROOT / "jenkins_scripts" / "step7_mail.ps1"
        harness = r"""
$ErrorActionPreference = "Stop"
$source = Get-Content -Path '__MAIL_SCRIPT__' -Raw

function Import-Function([string]$name) {
    $pattern = "(?ms)^function $([Regex]::Escape($name))[^\{]*\{.*?(?=^function |\z)"
    $match = [Regex]::Match($source, $pattern)
    if (!$match.Success) { throw "function not found: $name" }
    Invoke-Expression ($match.Value -replace '^function ', 'function global:')
}

Import-Function "Get-PipelineStatus"
Import-Function "Test-HttpUrl"
Import-Function "Get-BlackDuckEntriesFromSummary"
Import-Function "Build-BlackDuckButtonsHtml"
Import-Function "HtmlEncode"

$failedSummary = [pscustomobject]@{
    status = "FAILED"
    units = @(
        [pscustomobject]@{ unit = "FBL"; status = "FAILED"; blackduck_project = "Renault Gen3"; blackduck_version = "FBL" }
        [pscustomobject]@{ unit = "APPL"; status = "FAILED"; blackduck_project = "Renault Gen3"; blackduck_version = "APPL" }
    )
}
if ((Get-PipelineStatus -summary $failedSummary) -ne "FAIL") { throw "failed summary did not map to FAIL" }
$unknownSummary = [pscustomobject]@{ status = "IN_PROGRESS"; units = @() }
if ((Get-PipelineStatus -summary $unknownSummary) -ne "UNKNOWN") { throw "unknown summary did not map to UNKNOWN" }
$failedEntries = @(Get-BlackDuckEntriesFromSummary -summary $failedSummary -fallbackProjects @("Renault Gen3", "Renault Gen3") -fallbackVersions @("FBL", "APPL"))
if ($failedEntries.Count -ne 0) { throw "failed units produced Black Duck entries" }
if (![string]::IsNullOrEmpty((Build-BlackDuckButtonsHtml -entries $failedEntries))) { throw "failed units produced Black Duck buttons" }

$partialSummary = [pscustomobject]@{
    status = "FAILED"
    units = @(
        [pscustomobject]@{ unit = "FBL"; status = "FAILED"; blackduck_project = "Renault Gen3"; blackduck_version = "FBL" }
        [pscustomobject]@{ unit = "APPL"; status = "SUCCESS"; blackduck_project = "Renault Gen3"; blackduck_version = "APPL"; blackduck_bom_url = "https://blackduck.example/api/projects/p/versions/a/components" }
    )
}
$partialEntries = @(Get-BlackDuckEntriesFromSummary -summary $partialSummary -fallbackProjects @() -fallbackVersions @())
if ($partialEntries.Count -ne 1 -or $partialEntries[0].Unit -ne "APPL") { throw "failed unit was not filtered from partial result" }
Write-Output "PowerShell failure handling OK"
"""
        harness = harness.replace("__MAIL_SCRIPT__", str(mail_script).replace("'", "''"))

        with tempfile.TemporaryDirectory() as temporary_directory:
            harness_path = Path(temporary_directory) / "failure_handling_harness.ps1"
            harness_path.write_text(harness, encoding="utf-8")
            completed = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness_path)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PowerShell failure handling OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
