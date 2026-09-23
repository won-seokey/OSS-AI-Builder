import base64
import json
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


JIRA_API_BASE = "https://jira.example.com"
DEFAULT_JQL = (
    "project = OSS AND status = 'In Progress' "
    "AND labels in ('oss-request') AND labels not in ('processed', 'processing', 'oss-failed') "
    "ORDER BY created ASC"
)

ARTIFACT_FILES = [
    "result.json",
    "jira_key.txt",
    "jira_summary.txt",
    "manifest_id.txt",
    "manifest_url.txt",
    "manifest_filename.txt",
    "manifest_path.txt",
    "jira_request_text.txt",
    "pipeline_skip.txt",
    "scan-manifest.yaml",
    "oss_build.log",
]


def _default_ssl_context() -> ssl.SSLContext:
    # Corporate environments often lack public CA trust; bypass by default.
    return ssl._create_unverified_context()


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def clean_artifacts(output_dir: Path) -> None:
    for name in ARTIFACT_FILES:
        p = output_dir / name
        if p.exists():
            p.unlink()


def build_basic_auth_header(email: str, token: str) -> dict[str, str]:
    pair = f"{email}:{token}".encode("ascii", errors="ignore")
    encoded = base64.b64encode(pair).decode("ascii")
    return {
        "Authorization": f"Basic {encoded}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _jira_request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 40,
) -> bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url=url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_default_ssl_context()) as resp:
        return resp.read()


def jira_get_json(url: str, headers: dict[str, str], timeout: int = 40) -> dict[str, Any]:
    body = _jira_request(url, headers, method="GET", timeout=timeout)
    return json.loads(body.decode("utf-8"))


def jira_put_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 40) -> None:
    _jira_request(url, headers, method="PUT", payload=payload, timeout=timeout)


def jira_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 40) -> None:
    _jira_request(url, headers, method="POST", payload=payload, timeout=timeout)


def download_attachment(url: str, headers: dict[str, str], out_file: Path, timeout: int = 60) -> None:
    req_headers = {"Authorization": headers["Authorization"], "Accept": "*/*"}
    req = urllib.request.Request(url=url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_default_ssl_context()) as resp:
        data = resp.read()
    out_file.write_bytes(data)


def run_with_retry(cmd: list[str], max_attempts: int = 3) -> int:
    last_code = 1
    for attempt in range(1, max_attempts + 1):
        print(f"RUN ATTEMPT {attempt}/{max_attempts}: {' '.join(cmd)}")
        proc = subprocess.run(cmd, check=False)
        last_code = proc.returncode
        if last_code == 0:
            return 0
        if last_code == 2:
            print(f"NO RETRY: non-retryable exit code {last_code}")
            return last_code
        if attempt < max_attempts:
            sleep_sec = attempt * 2
            print(f"RETRY: exit code {last_code}, waiting {sleep_sec}s")
            time.sleep(sleep_sec)
    return last_code


def run_and_tee(cmd: list[str], cwd: Path, log_file: Path, env: dict[str, str]) -> int:
    with log_file.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        if proc.stdout is None:
            raise RuntimeError("subprocess stdout is unexpectedly None")
        for line in proc.stdout:
            print(line, end="")
            lf.write(line)

        return proc.wait()
