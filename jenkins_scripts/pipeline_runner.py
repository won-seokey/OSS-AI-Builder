import argparse
import os
import sys
import urllib.error

from build_flow import run_build_command
from finalize_flow import finalize_command
from input_collector import collect_input_command
from manifest_flow import generate_manifest_command

from pipeline_common import (
    DEFAULT_JQL,
    JIRA_API_BASE,
)


def cmd_collect_input(args: argparse.Namespace) -> int:
    return collect_input_command(args)


def cmd_generate_manifest(args: argparse.Namespace) -> int:
    return generate_manifest_command(args)


def cmd_run_build(args: argparse.Namespace) -> int:
    return run_build_command(args)


def cmd_finalize(args: argparse.Namespace) -> int:
    return finalize_command(args)


def cmd_placeholder(_args: argparse.Namespace) -> int:
    print("Not implemented yet. Use collect-input for now.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline runner for OSS automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect-input", help="collect Jira input and prepare manifest-related files")
    p_collect.add_argument("--output-dir", default="jenkins_runtime", help="directory to write contract files")
    p_collect.add_argument("--oss-root", default="jenkins_runtime/manifests", help="directory used for downloaded YAML attachment")
    p_collect.add_argument("--jira-email", default="oss-automation@example.com", help="Jira account email")
    p_collect.add_argument("--jira-token-env", default="JIRA_OSS_TOKEN", help="env var name for Jira API token")
    p_collect.add_argument("--jql", default=DEFAULT_JQL, help="Jira JQL to find target issue")
    p_collect.add_argument("--request-json", help="repository-relative pending request JSON; limits JQL to its jira_key")
    p_collect.add_argument("--clean", action="store_true", default=True, help="clean old contract files before run")
    p_collect.add_argument("--no-clean", action="store_false", dest="clean", help="do not clean old contract files before run")
    p_collect.set_defaults(func=cmd_collect_input)

    p_generate = sub.add_parser("generate-manifest", help="run AI manifest generation and strict validation")
    p_generate.add_argument("--output-dir", default="jenkins_runtime", help="directory containing contract files")
    p_generate.add_argument("--python-cmd", default=sys.executable, help="python executable used for subprocess calls")
    p_generate.add_argument("--builder-path", default="jenkins_scripts/manifest_builder_llm.py", help="path to manifest builder script")
    p_generate.add_argument("--validator-path", default="jenkins_scripts/manifest_validator.py", help="path to validator script")
    p_generate.add_argument("--catalog-path", default="jenkins_scripts/blackduck_catalog.yaml", help="path to Black Duck catalog file")
    p_generate.add_argument("--system-prompt-path", default="jenkins_scripts/prompts/manifest_system_prompt.md", help="path to system prompt markdown file")
    p_generate.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"), help="OpenAI-compatible model name")
    p_generate.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), help="OpenAI-compatible API base URL")
    p_generate.add_argument("--reasoning-effort", default=os.environ.get("OPENAI_REASONING_EFFORT", "xhigh"), choices=("none", "low", "medium", "high", "xhigh"), help="Reasoning effort for supported models")
    p_generate.add_argument("--openai-key-env", default="OPENAI_API_KEY", help="env var name for OpenAI API key")
    p_generate.add_argument("--max-attempts", type=int, default=3, help="retry attempts for AI generation")
    p_generate.add_argument("--insecure", action="store_true", default=True, help="pass --insecure to builder")
    p_generate.add_argument("--verify-ssl", action="store_false", dest="insecure", help="do not pass --insecure to builder")
    p_generate.set_defaults(func=cmd_generate_manifest)

    p_build = sub.add_parser("run-build", help="run OSS build with Jira processing-label guard")
    p_build.add_argument("--output-dir", default="jenkins_runtime", help="directory containing contract files")
    p_build.add_argument("--python-cmd", default=sys.executable, help="python executable used to run run-bld.py")
    p_build.add_argument("--runner-path", default="jenkins_scripts/run-bld.py", help="path to OSS runner script")
    p_build.add_argument("--jira-email", default="oss-automation@example.com", help="Jira account email")
    p_build.add_argument("--jira-token-env", default="JIRA_OSS_TOKEN", help="env var name for Jira API token")
    p_build.add_argument("--jira-base-url", default=JIRA_API_BASE, help="Jira base URL")
    p_build.set_defaults(func=cmd_run_build)

    p_finalize = sub.add_parser("finalize", help="finalize Jira labels/status and optionally run notification")
    p_finalize.add_argument("--output-dir", default="jenkins_runtime", help="directory containing contract files")
    p_finalize.add_argument("--jira-email", default="oss-automation@example.com", help="Jira account email")
    p_finalize.add_argument("--jira-token-env", default="JIRA_OSS_TOKEN", help="env var name for Jira API token")
    p_finalize.add_argument("--jira-base-url", default=JIRA_API_BASE, help="Jira base URL")
    p_finalize.add_argument("--notify", action="store_true", help="run notification script after finalize")
    p_finalize.add_argument("--notify-only", action="store_true", help="skip Jira finalize and run notification only")
    p_finalize.add_argument("--cleanup-processing", action="store_true", help="mark a failed request and remove its retry-trigger labels")
    p_finalize.add_argument("--notify-script", default="jenkins_scripts/step7_mail.ps1", help="notification script path")
    p_finalize.add_argument("--powershell-cmd", default="powershell", help="powershell executable command")
    p_finalize.set_defaults(func=cmd_finalize)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        print(f"ERROR: HTTP {e.code}: {detail}")
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: URL error: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
