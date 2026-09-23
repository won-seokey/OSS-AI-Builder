import argparse
import os
import subprocess
from pathlib import Path

from pipeline_common import build_basic_auth_header, jira_get_json, jira_post_json, jira_put_json


def cleanup_processing_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    jira_key_file = output_dir / "jira_key.txt"
    if not jira_key_file.exists():
        print("NO JIRA KEY - skip processing-label cleanup")
        return 0

    issue_key = jira_key_file.read_text(encoding="utf-8", errors="replace").strip().upper()
    if not issue_key:
        raise RuntimeError("jira_key.txt is empty")

    token = (os.environ.get(args.jira_token_env) or "").strip()
    if not token:
        raise RuntimeError(f"{args.jira_token_env} is empty")

    headers = build_basic_auth_header(args.jira_email, token)
    issue_url = f"{args.jira_base_url}/rest/api/3/issue/{issue_key}"
    label_body = {
        "update": {
            "labels": [
                {"add": "oss-failed"},
                {"remove": "processing"},
                {"remove": "oss-request"},
            ]
        }
    }

    print("===== Cleanup Jira Processing Label After Failure =====")
    print(f"ISSUE_KEY=[{issue_key}]")
    jira_put_json(issue_url, headers, label_body)

    verify = jira_get_json(f"{issue_url}?fields=labels", headers)
    labels = verify.get("fields", {}).get("labels", [])
    if not isinstance(labels, list):
        labels = []
    if "processing" in labels or "oss-request" in labels or "oss-failed" not in labels:
        raise RuntimeError("failed label cleanup verification failed")

    print("SUCCESS: failure labels applied and request trigger removed")
    return 0


def finalize_command(args: argparse.Namespace) -> int:
    if args.cleanup_processing:
        return cleanup_processing_command(args)

    output_dir = Path(args.output_dir).resolve()
    jira_key_file = output_dir / "jira_key.txt"

    if not args.notify_only:
        if not jira_key_file.exists():
            print("NO JIRA KEY - skip transition")
            return 0

        issue_key = jira_key_file.read_text(encoding="utf-8", errors="replace").strip()
        if not issue_key:
            raise RuntimeError("jira_key.txt is empty")

        token = (os.environ.get(args.jira_token_env) or "").strip()
        if not token:
            raise RuntimeError(f"{args.jira_token_env} is empty")

        headers = build_basic_auth_header(args.jira_email, token)
        issue_url = f"{args.jira_base_url}/rest/api/3/issue/{issue_key}"
        transition_url = f"{args.jira_base_url}/rest/api/3/issue/{issue_key}/transitions"

        print("===== Jira Finalize Issue =====")
        print(f"ISSUE_KEY=[{issue_key}]")

        label_body = {
            "update": {
                "labels": [
                    {"add": "processed"},
                    {"remove": "processing"},
                ]
            }
        }
        jira_put_json(issue_url, headers, label_body)

        verify = jira_get_json(f"{issue_url}?fields=labels", headers)
        labels = verify.get("fields", {}).get("labels", [])
        if not isinstance(labels, list):
            labels = []

        print("CURRENT LABELS AFTER FINALIZE: " + ", ".join(str(x) for x in labels))
        if "processing" in labels or "processed" not in labels:
            raise RuntimeError("final labels verification failed")

        transitions = jira_get_json(transition_url, headers).get("transitions", [])
        if not isinstance(transitions, list):
            transitions = []

        print("Available transitions:")
        for t in transitions:
            if not isinstance(t, dict):
                continue
            t_id = str(t.get("id", ""))
            t_name = str(t.get("name", ""))
            to_name = str((t.get("to") or {}).get("name", "")) if isinstance(t.get("to"), dict) else ""
            print(f" - id={t_id}, name={t_name}, to={to_name}")

        resolved = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            t_name = str(t.get("name", ""))
            to_name = str((t.get("to") or {}).get("name", "")) if isinstance(t.get("to"), dict) else ""
            t_id = str(t.get("id", ""))
            if t_name in {"Resolve Issue", "Resolved"} or to_name == "Resolved" or t_id == "5":
                resolved = t
                break

        if resolved is None:
            print("Resolved transition not found. Skip transition.")
        else:
            trans_id = str(resolved.get("id", "")).strip()
            if not trans_id:
                raise RuntimeError("resolved transition id is empty")
            print(f"SELECTED TRANSITION_ID=[{trans_id}]")
            print(f"SELECTED TRANSITION_NAME=[{str(resolved.get('name', ''))}]")
            jira_post_json(transition_url, headers, {"transition": {"id": trans_id}})
            print("SUCCESS: Jira issue transitioned to Resolved")

    if args.notify:
        notify_script = Path(args.notify_script).resolve()
        if not notify_script.exists():
            raise RuntimeError(f"notify script not found: {notify_script}")
        cmd = [
            args.powershell_cmd,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(notify_script),
        ]
        print("===== Run Notification Script =====")
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"notification script failed with exit code {proc.returncode}")

    return 0
