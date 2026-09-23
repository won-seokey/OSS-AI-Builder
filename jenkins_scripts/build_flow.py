import argparse
import os
import re
import time
from pathlib import Path

from pipeline_common import build_basic_auth_header, jira_get_json, jira_put_json, run_and_tee


def ensure_processing_label(issue_key: str, headers: dict[str, str], base_url: str, max_attempts: int = 3) -> None:
    issue_url = f"{base_url}/rest/api/3/issue/{issue_key}"
    body = {
        "update": {
            "labels": [
                {"add": "processing"},
            ]
        }
    }

    for attempt in range(1, max_attempts + 1):
        print(f"PROCESSING LABEL ATTEMPT {attempt}/{max_attempts}")
        try:
            jira_put_json(issue_url, headers, body)
        except Exception as e:  # noqa: BLE001
            if attempt == max_attempts:
                raise RuntimeError(f"failed to add processing label: {e}") from e

        try:
            verify = jira_get_json(f"{issue_url}?fields=labels", headers)
            labels = verify.get("fields", {}).get("labels", [])
            if isinstance(labels, list):
                if "processing" in labels:
                    print("SUCCESS: processing label confirmed")
                    return
            if attempt == max_attempts:
                raise RuntimeError("processing label not present after retries")
        except Exception as e:  # noqa: BLE001
            if attempt == max_attempts:
                raise RuntimeError(f"failed to verify processing label: {e}") from e

        time.sleep(1)


def run_build_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    manifest_path_file = output_dir / "manifest_path.txt"
    if not manifest_path_file.exists():
        print("NO MANIFEST PATH - skip OSS build")
        return 0

    manifest_path_raw = manifest_path_file.read_text(encoding="utf-8", errors="replace").strip()
    if not manifest_path_raw:
        raise RuntimeError("manifest_path.txt is empty")

    manifest_path = Path(manifest_path_raw)
    if not manifest_path.exists():
        raise RuntimeError(f"manifest file not found: {manifest_path}")

    runner_path = Path(args.runner_path).resolve()
    if not runner_path.exists():
        raise RuntimeError(f"runner not found: {runner_path}")

    jira_key_file = output_dir / "jira_key.txt"
    if not jira_key_file.exists():
        raise RuntimeError("jira_key.txt not found")

    jira_key = jira_key_file.read_text(encoding="utf-8", errors="replace").strip().upper()
    if not jira_key:
        raise RuntimeError("jira key is empty")

    token = (os.environ.get(args.jira_token_env) or "").strip()
    if not token:
        raise RuntimeError(f"{args.jira_token_env} is empty")

    headers = build_basic_auth_header(args.jira_email, token)
    print("===== Ensure processing label before build =====")
    print(f"ISSUE_KEY=[{jira_key}]")
    ensure_processing_label(jira_key, headers, args.jira_base_url)

    log_file = output_dir / "oss_build.log"
    if log_file.exists():
        log_file.unlink()

    run_env = os.environ.copy()
    run_env["JIRA_KEY"] = jira_key

    print("===== Run OSS Build =====")
    print(f"MANIFEST_PATH=[{manifest_path}]")
    cmd = [args.python_cmd, str(runner_path), str(manifest_path)]
    exit_code = run_and_tee(cmd, cwd=output_dir, log_file=log_file, env=run_env)
    print(f"OSS BUILD EXIT CODE: {exit_code}")
    if exit_code != 0:
        raise RuntimeError(f"OSS build failed with exit code {exit_code}")

    log_raw = log_file.read_text(encoding="utf-8", errors="replace")
    if re.search(r'"status"\s*:\s*"FAILED"', log_raw):
        raise RuntimeError("OSS build failed. Summary status is FAILED.")
    if re.search(r"ERROR:\s*Missing environment variables", log_raw):
        raise RuntimeError("OSS build failed. Missing required environment variables.")
    if re.search(r"\[.*\]\s*ERROR:", log_raw):
        raise RuntimeError("OSS build failed. Unit error detected in log.")

    print("SUCCESS: OSS build completed")
    return 0
