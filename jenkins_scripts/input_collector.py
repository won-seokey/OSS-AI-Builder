import argparse
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

from pipeline_common import (
    JIRA_API_BASE,
    build_basic_auth_header,
    clean_artifacts,
    download_attachment,
    jira_get_json,
    write_text,
)


def query_issue(headers: dict[str, str], jql: str) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "jql": jql,
            "fields": "key,summary,attachment,description",
        },
        quote_via=urllib.parse.quote,
    )
    url = f"{JIRA_API_BASE}/rest/api/3/search/jql?{params}"
    return jira_get_json(url, headers)


def normalize_description_line(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^\s*(?:[-*]|[•◦▪▫·]|\d+[.)]|[a-zA-Z][.)])\s+", "", cleaned)
    return cleaned.strip()


def extract_description_text(node: Any, buf: list[str]) -> None:
    if node is None:
        return

    if isinstance(node, str):
        line = normalize_description_line(node)
        if line:
            buf.append(line)
        return

    if isinstance(node, list):
        for item in node:
            extract_description_text(item, buf)
        return

    if not isinstance(node, dict):
        return

    text_val = str(node.get("text", "") or "")
    text_val = normalize_description_line(text_val)
    if text_val:
        buf.append(text_val)

    marks = node.get("marks")
    if isinstance(marks, list):
        for mark in marks:
            if not isinstance(mark, dict):
                continue
            if str(mark.get("type", "")).strip() != "link":
                continue
            attrs = mark.get("attrs")
            if isinstance(attrs, dict):
                href = str(attrs.get("href", "") or "").strip()
                if href:
                    buf.append(href)

    attrs = node.get("attrs")
    if isinstance(attrs, dict):
        for key in ("url", "href"):
            url_val = str(attrs.get(key, "") or "").strip()
            if url_val:
                buf.append(url_val)

    extract_description_text(node.get("content"), buf)


def description_to_text(description: Any) -> str:
    out: list[str] = []
    extract_description_text(description, out)
    return "\n".join(out).strip()


def load_request_metadata(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not raw:
        raise RuntimeError(f"request JSON is empty: {path}")

    candidates = [raw]
    if '\\"' in raw:
        candidates.append(raw.replace('\\"', '"'))

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(parsed, dict):
            raise RuntimeError(f"request JSON root must be an object: {path}")
        return parsed

    raise RuntimeError(f"request JSON is invalid: {path}: {last_error}")


def request_issue_key(path: Path) -> str:
    try:
        metadata = load_request_metadata(path)
        issue_key = str(metadata.get("jira_key", "") or "").strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", issue_key):
            return issue_key
    except RuntimeError:
        pass

    filename_match = re.match(r"^([A-Z][A-Z0-9]+-\d+)_", path.name, flags=re.I)
    if filename_match:
        issue_key = filename_match.group(1).upper()
        print(f"WARNING: malformed request JSON; using Jira key from filename: {issue_key}")
        return issue_key

    raise RuntimeError(f"request JSON jira_key is invalid and cannot be recovered: {path.name}")


def validate_manifest_file(path: Path) -> None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        raise RuntimeError(f"downloaded file is empty: {path}")
    if '"errorMessages"' in raw:
        raise RuntimeError("attachment download returned Jira error payload")
    if raw.strip().startswith("{"):
        raise RuntimeError("attachment content looks like JSON, not YAML")
    if "scan_units:" not in raw and not re.search(r"^request:", raw, flags=re.M):
        raise RuntimeError("downloaded file does not look like expected manifest YAML")


def collect_input_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        clean_artifacts(output_dir)

    token = (os.environ.get(args.jira_token_env) or "").strip()
    if not token:
        raise RuntimeError(f"{args.jira_token_env} is empty")

    headers = build_basic_auth_header(args.jira_email, token)

    jql = args.jql
    request_issue = ""
    if args.request_json:
        request_path = Path(args.request_json).resolve()
        if not request_path.exists():
            raise RuntimeError(f"request JSON not found: {request_path}")
        request_issue = request_issue_key(request_path)
        jql = (
            f"key = {request_issue} AND status = 'In Progress' "
            "AND labels in ('oss-request') "
            "AND labels not in ('processed', 'processing')"
        )
        print(f"REQUEST_JSON=[{request_path}]")
        print(f"REQUEST_JQL=[{jql}]")

    result = query_issue(headers, jql)
    write_text(output_dir / "result.json", json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("errorMessages"):
        raise RuntimeError("Jira API returned errorMessages")

    issues = result.get("issues")
    if not isinstance(issues, list) or not issues:
        if request_issue:
            write_text(
                output_dir / "pipeline_skip.txt",
                f"SKIPPED: no eligible Jira issue found for {request_issue}\n",
            )
            print(f"SKIP: no eligible Jira issue found for request JSON: {request_issue}")
            return 0
        print("NO ISSUE")
        return 0

    issue = issues[0]
    fields = issue.get("fields") if isinstance(issue, dict) else {}
    if not isinstance(fields, dict):
        fields = {}

    key = str(issue.get("key", "") or "").strip()
    summary = str(fields.get("summary", "") or "").strip()
    attachments = fields.get("attachment")
    if not isinstance(attachments, list):
        attachments = []

    if not key:
        raise RuntimeError("issue key is empty")

    write_text(output_dir / "jira_key.txt", key)
    write_text(output_dir / "jira_summary.txt", summary)

    manifest_list: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename", "") or "")
        if re.search(r"\.ya?ml$", filename, flags=re.I):
            manifest_list.append(item)

    if len(manifest_list) > 1:
        raise RuntimeError("multiple YAML files found. Please attach only one manifest YAML file.")

    if len(manifest_list) == 0:
        description_text = description_to_text(fields.get("description"))
        if not description_text:
            raise RuntimeError("No YAML attachment and empty description. Provide YAML or Jira description text.")
        write_text(output_dir / "jira_request_text.txt", description_text)
        print("NO YAML manifest attachment. Saved Jira description text for AI generation.")
        return 0

    manifest = manifest_list[0]
    manifest_id = str(manifest.get("id", "") or "").strip()
    manifest_url = str(manifest.get("content", "") or "").strip()
    manifest_filename = str(manifest.get("filename", "") or "").strip()

    if not manifest_id:
        raise RuntimeError("manifest id is empty")
    if not manifest_url:
        raise RuntimeError("manifest content url is empty")
    if not manifest_filename:
        raise RuntimeError("manifest filename is empty")
    if re.search(r"[\\/:*?\"<>|]", manifest_filename):
        raise RuntimeError(f"manifest filename contains invalid Windows path characters: {manifest_filename}")
    if not re.search(r"\.ya?ml$", manifest_filename, flags=re.I):
        raise RuntimeError(f"manifest file is not yaml/yml: {manifest_filename}")

    write_text(output_dir / "manifest_id.txt", manifest_id)
    write_text(output_dir / "manifest_url.txt", manifest_url)
    write_text(output_dir / "manifest_filename.txt", manifest_filename)

    oss_root = Path(args.oss_root)
    oss_root.mkdir(parents=True, exist_ok=True)
    out_file = oss_root / manifest_filename
    if out_file.exists():
        out_file.unlink()

    download_attachment(manifest_url, headers, out_file)
    validate_manifest_file(out_file)

    write_text(output_dir / "manifest_path.txt", str(out_file))
    print("SUCCESS: manifest collection completed")
    return 0
