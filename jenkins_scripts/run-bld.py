import os
import sys
import json
import shutil
import shlex
import subprocess
import stat
import time
import re
import difflib
import threading
import getpass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# Corporate environment SSL/Network bypass
os.environ["PYTHONHTTPSVERIFY"] = "0"
os.environ["GIT_SSL_NO_VERIFY"] = "true"

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Please install with: pip install pyyaml")
    sys.exit(1)

# --- Constants ---
MAX_WORKSPACE_SIZE_GB = 200
EXCLUDE_DIR_NAMES = {".git", ".repo", "obj", "bin", "Debug", "Release", "GenData", "__pycache__", ".vscode", ".idea"}
EXCLUDE_FILE_EXTENSIONS = {
    ".exe", ".dll", ".so", ".o", ".obj", ".a", ".lib", ".pyc", 
    ".png", ".gif", ".jpg", ".jpeg", ".bmp", ".ico", ".pdf", 
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bin", ".out", ".jar",
    ".docx", ".xlsx", ".pptx", ".mp4", ".wav",
    ".hex", ".srec", ".elf", ".map", ".d", ".pdb", ".suo", ".user"
}
LOG_LOCK = threading.Lock()
WSL_SETUP_LOCK = threading.Lock()
GLOBAL_EXECUTION_LOCK = threading.Lock()
JIRA_KEY_PATTERN = re.compile(r'^OSS-\d+$')
GITHUB_HOST = "github.com"
TEAMFORGE_HOST = "teamforge.example.com"
BLACKDUCK_BOM_PATTERN = re.compile(r"Black Duck SCA Project BOM:\s*(https?://\S+)")
BLACKDUCK_BOM_PATH_PATTERN = re.compile(
    r"^(?P<prefix>.*)/api/projects/(?P<project_id>[^/]+)/versions/"
    r"(?P<version_id>[^/?]+)/components(?:/.*)?$"
)


def read_jira_key() -> str:
    jira_key = (os.environ.get("JIRA_KEY") or "").strip().upper()
    if not jira_key or not JIRA_KEY_PATTERN.fullmatch(jira_key):
        return ""
    return jira_key


def normalize_blackduck_url(value: str) -> str:
    return str(value or "").strip().rstrip(".,);]")


def blackduck_ui_url_from_bom_url(bom_url: str) -> str:
    normalized = normalize_blackduck_url(bom_url)
    if not normalized:
        return ""

    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    path_match = BLACKDUCK_BOM_PATH_PATTERN.match(parsed.path)
    if not path_match:
        return ""

    return normalized


def extract_blackduck_urls(output: str) -> tuple[str, str]:
    matches = BLACKDUCK_BOM_PATTERN.findall(output or "")
    if not matches:
        return "", ""

    bom_url = normalize_blackduck_url(matches[-1])
    return bom_url, blackduck_ui_url_from_bom_url(bom_url)


def unit_artifact_name(unit_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(unit_name).strip().lower()) or "unit"


def with_jira_prefix(version_name: str, jira_key: str) -> str:
    if re.match(r'^\[OSS-\d+\]', version_name):
        return version_name
    return f"[{jira_key}]{version_name}"

def get_dir_size_gb(path: Path) -> float:
    total = 0
    try:
        for f in path.rglob('*'):
            if f.is_file(): total += f.stat().st_size
    except: pass
    return total / (1024**3)

def manage_disk_space(cache_root: Path):
    if not cache_root.exists(): return
    with GLOBAL_EXECUTION_LOCK:
        try:
            current_size_gb = get_dir_size_gb(cache_root)
            if current_size_gb > MAX_WORKSPACE_SIZE_GB:
                log(f"[DISK] Current cache size ({current_size_gb:.1f}GB) exceeds limit. Cleaning up...")
                folders = []
                for oem_dir in cache_root.iterdir():
                    if oem_dir.is_dir():
                        for unit_dir in oem_dir.iterdir():
                            if unit_dir.is_dir():
                                folders.append((unit_dir, unit_dir.stat().st_mtime))
                folders.sort(key=lambda x: x[1])
                for folder, _ in folders:
                    log(f"[DISK] Deleting old workspace: {folder.parent.name}/{folder.name}")
                    shutil.rmtree(long_path(folder), ignore_errors=True)
                    if get_dir_size_gb(cache_root) <= (MAX_WORKSPACE_SIZE_GB * 0.7): break
        except Exception as e:
            log(f"[DISK] Error during cleanup: {e}")

def log(msg: str, unit: str = "") -> None:
    prefix = f"[{unit}] " if unit else ""
    with LOG_LOCK:
        try:
            print(f"{prefix}{msg}", flush=True)
        except:
            # 인코딩 문제로 로그 출력이 실패하는 경우 방지
            print(f"{prefix}[LOG_ERROR] Message contains unprintable characters", flush=True)

def long_path(path: Path) -> str:
    p = str(path.resolve())
    if os.name == "nt" and not p.startswith("\\\\?\\"): return "\\\\?\\" + p
    return p

def robocopy_safe_path(path: Path) -> str:
    # Robocopy is more reliable with regular absolute Windows paths than \\?\-prefixed paths.
    return str(path.resolve())

def strip_windows_extended_prefix(path_str: str) -> str:
    if os.name != "nt":
        return path_str
    if path_str.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_str[8:]
    if path_str.startswith("\\\\?\\"):
        return path_str[4:]
    return path_str

def wsl_path_to_windows(wsl_path: str, distro: str = "Ubuntu") -> str:
    if not wsl_path: return ""
    if wsl_path.startswith("/mnt/"):
        drive = wsl_path[5].upper()
        path_part = wsl_path[7:].replace('/', '\\')
        return f"{drive}:\\{path_part}"
    clean_path = wsl_path.replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}{clean_path}"

def windows_path_to_wsl(path: Path) -> str:
    p = str(path.resolve())
    if len(p) >= 2 and p[1] == ":": return f"/mnt/{p[0].lower()}{p[2:].replace('\\', '/')}"
    return p.replace("\\", "/")

def get_rsync_exclude_args() -> str:
    args = []
    for ext in EXCLUDE_FILE_EXTENSIONS: args.append(f"--exclude='*{ext}'")
    for d in EXCLUDE_DIR_NAMES: args.append(f"--exclude='{d}'")
    return " ".join(args)

def _credential_scope_from_text(value: str) -> str | None:
    text = str(value or "")
    for host, scope in ((GITHUB_HOST, "github"), (TEAMFORGE_HOST, "teamforge")):
        if re.search(rf"(?:^|[^A-Za-z0-9_.-]){re.escape(host)}(?=$|[^A-Za-z0-9_.-])", text, flags=re.IGNORECASE):
            return scope
    return None

def _git_remote_url(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_path),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else ""

def _is_network_checkout_command(command: str) -> bool:
    low = str(command or "").strip().lower()
    return bool(
        re.search(r"(?:^|&&\s*)git\s+(?:clone|fetch|pull|push|submodule|ls-remote)\b", low)
        or re.search(r"(?:^|&&\s*)repo\s+(?:init|sync)\b", low)
        or re.match(r"^(?:curl|scp|ssh)\s+", low)
    )

def credential_scope_for_command(command: str, cwd: Path | None = None) -> str | None:
    direct_scope = _credential_scope_from_text(command)
    if direct_scope:
        return direct_scope

    low = str(command or "").strip().lower()
    if re.search(r"(?:^|&&\s*)repo\s+(?:init|sync)\b", low):
        return "teamforge"

    if not _is_network_checkout_command(command):
        return None

    if cwd:
        remote_scope = _credential_scope_from_text(_git_remote_url(cwd))
        if remote_scope:
            return remote_scope

    return "teamforge"

def sanitize_checkout_commands(commands: list[str]) -> list[str]:
    teamforge_user = (os.environ.get("TEAMFORGE_USER") or "").strip()
    needs_teamforge_user = any(
        _credential_scope_from_text(command) == "teamforge"
        or re.search(r"(?:^|&&\s*)repo\s+", str(command).strip(), flags=re.IGNORECASE)
        for command in commands
    )
    if needs_teamforge_user and not teamforge_user:
        raise RuntimeError("TEAMFORGE_USER is required for TeamForge checkout")

    sanitized = []
    for cmd in commands:
        new_cmd = re.sub(
            r'(\$\{TEAMFORGE_USER\}|[a-zA-Z0-9._%+-]+)@teamforge\.example\.com',
            f'{teamforge_user}@teamforge.example.com',
            cmd,
        )
        if "scp " in new_cmd and "teamforge.example.com" in new_cmd and " -O" not in new_cmd:
            new_cmd = new_cmd.replace("scp ", "scp -O ")
        if os.name == "nt":
            new_cmd = re.sub(r'\s*&&\s*chmod\s+[^\s&|;]+(?:\s+[^\s&|;]+)*', '', new_cmd)
        if "repo init" in new_cmd:
            if "--no-clone-bundle" not in new_cmd: new_cmd = new_cmd.replace("repo init", "repo init --no-clone-bundle")
            if "--quiet" not in new_cmd: new_cmd = new_cmd.replace("repo init", "repo init --quiet")
            new_cmd = new_cmd.replace("repo ", "repo --no-pager ")
            new_cmd = f"export TERM=dumb && {new_cmd}"
        if "repo sync" in new_cmd:
            if " -j" not in new_cmd: new_cmd = new_cmd.replace("repo sync", "repo sync -j8")
            if " -c" not in new_cmd: new_cmd = new_cmd.replace("repo sync", "repo sync -c")
            new_cmd = new_cmd.replace("repo ", "repo --no-pager ")
        sanitized.append(new_cmd)
    return sanitized

def _clone_positional_args(command: str) -> list[str]:
    c_str = str(command)
    if "clone" not in c_str.lower():
        return []
    clone_part = re.split(r'\s*(?:&&|\|\||;)\s*', c_str)[0].strip()
    tokens = shlex.split(clone_part, posix=False)
    try:
        clone_idx = next(i for i, t in enumerate(tokens) if t.lower() == "clone")
    except StopIteration:
        return []

    option_values = {"-b", "--branch", "-o", "--origin", "--depth", "--config", "--upload-pack"}
    non_flag_args: list[str] = []
    skip_next = False
    for t in tokens[clone_idx + 1:]:
        t_clean = t.strip().strip('"\'()[]{}')
        if not t_clean or t_clean in ('&&', '||', ';'):
            continue
        if skip_next:
            skip_next = False
            continue
        if t_clean in option_values:
            skip_next = True
            continue
        if t_clean.startswith('-'):
            continue
        non_flag_args.append(t_clean)
    return non_flag_args


def _extract_clone_target_dir(command: str) -> str | None:
    non_flag_args = _clone_positional_args(command)
    if not non_flag_args:
        return None

    if len(non_flag_args) >= 2:
        target = non_flag_args[1]
    elif len(non_flag_args) == 1:
        target = non_flag_args[0].rstrip('/').split('/')[-1].replace('.git', '')
    else:
        return None

    target = target.strip().strip('"\'()[]{}').rstrip('.').rstrip('/').rstrip('\\')
    return target or None


def _extract_clone_source(command: str) -> str | None:
    args = _clone_positional_args(command)
    return args[0] if args else None


def _normalize_git_remote(value: str) -> str:
    normalized = value.strip().lower().rstrip("/")
    normalized = re.sub(r"^(https?://)[^/@]+@", r"\1", normalized)
    normalized = re.sub(r"^[^@/]+@", "", normalized)
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def cached_repo_matches_source(repo_path: Path, expected_source: str) -> bool:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo_path),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    actual_source = result.stdout.strip() if result.returncode == 0 else ""
    if not actual_source:
        return False
    return _normalize_git_remote(actual_source) == _normalize_git_remote(expected_source)


def refresh_cached_git_repo(repo_path: Path, unit_name: str) -> bool:
    refresh_commands = [
        "git reset --hard",
        "git clean -fd",
        "git fetch --all --prune",
    ]
    for refresh_cmd in refresh_commands:
        credential_scope = credential_scope_for_command(refresh_cmd, repo_path)
        if run_command(refresh_cmd, repo_path, "windows", credential_scope=credential_scope) != 0:
            log(f"ERROR: cached repository refresh failed: {refresh_cmd}", unit=unit_name)
            return False

    # A normal branch can advance incrementally. Detached tags/commits are
    # intentionally left for the manifest's later checkout command.
    credential_scope = credential_scope_for_command("git pull --ff-only", repo_path)
    if run_command("git pull --ff-only", repo_path, "windows", credential_scope=credential_scope) != 0:
        log("Cached repository is not on a fast-forwardable branch; continuing with manifest checkout", unit=unit_name)
    return True


def run_windows_checkout_commands(commands: list[str], start_dir: Path, unit_name: str) -> bool:
    current_dir = start_dir.resolve()
    for raw_cmd in commands:
        cmd = str(raw_cmd).strip()
        if not cmd:
            continue

        # Support chained commands like: clone && cd repo && git checkout ...
        segments = [s.strip() for s in re.split(r'\s*&&\s*', cmd) if s.strip()]
        for seg in segments:
            clone_target = _extract_clone_target_dir(seg)
            if clone_target:
                clone_path = (current_dir / clone_target).resolve()
                if clone_path.is_dir():
                    if (clone_path / ".git").exists():
                        clone_source = _extract_clone_source(seg)
                        if clone_source and not cached_repo_matches_source(clone_path, clone_source):
                            log(f"Cached repository remote changed; recreating: {clone_target}", unit=unit_name)
                            shutil.rmtree(long_path(clone_path), ignore_errors=True)
                        else:
                            log(f"Clone target exists, refreshing cached repository: {clone_target}", unit=unit_name)
                            if not refresh_cached_git_repo(clone_path, unit_name):
                                return False
                            continue
                    elif clone_path.exists():
                        log(f"Clone target is not a Git repository; recreating: {clone_target}", unit=unit_name)
                        shutil.rmtree(long_path(clone_path), ignore_errors=True)

            cd_match = re.fullmatch(r'cd\s+["\']?([^"\']+)["\']?', seg, re.IGNORECASE)
            if cd_match:
                target = cd_match.group(1).strip()
                next_dir = Path(target)
                if not next_dir.is_absolute():
                    next_dir = (current_dir / next_dir).resolve()
                else:
                    next_dir = next_dir.resolve()
                if not next_dir.exists():
                    log(f"ERROR: cd target does not exist: {next_dir}", unit=unit_name)
                    return False
                current_dir = next_dir
                continue

            credential_scope = credential_scope_for_command(seg, current_dir)
            rc = run_command(seg, current_dir, "windows", credential_scope=credential_scope)
            if rc != 0:
                log(f"ERROR: checkout command failed (exit {rc}): {seg}", unit=unit_name)
                return False
            if re.match(r"^git\s+(checkout|switch)\b", seg, flags=re.IGNORECASE):
                credential_scope = credential_scope_for_command("git pull --ff-only", current_dir)
                if run_command("git pull --ff-only", current_dir, "windows", credential_scope=credential_scope) != 0:
                    log("Selected Git ref is not a fast-forwardable branch; continuing", unit=unit_name)

    return True

def fix_ssh_permissions():
    if os.name == "nt":
        ssh_key = Path.home() / ".ssh" / "id_rsa"
        if ssh_key.exists():
            username = getpass.getuser()
            subprocess.run(f'icacls "{ssh_key}" /inheritance:r /grant "{username}":R', shell=True, capture_output=True)

def run_wsl_bash(script: str, distro: str = "default", capture: bool = False, env: dict = None) -> subprocess.CompletedProcess:
    cmd = ["wsl"]
    if distro and distro.lower() != "default": cmd.extend(["-d", distro])
    env_exports = ['export PYTHONHTTPSVERIFY=0', 'export GIT_SSL_NO_VERIFY=true', 'export GIT_TERMINAL_PROMPT=0', 'export TERM=dumb']
    if env:
        for k, v in env.items(): env_exports.append(f'export {k}={shlex.quote(str(v))}')
    full_script = "\n".join(env_exports) + "\n" + script
    # Remove null characters that cause ValueError on Windows
    full_script = full_script.replace('\x00', '')
    cmd.extend(["bash", "-lc", full_script])
    return subprocess.run(cmd, capture_output=capture, text=True, encoding='utf-8', errors='replace')

def cleanup_askpass_files(paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as error:
            log(f"[WARN] failed to remove askpass file: {path} ({error})")


def create_askpass(
    cwd: Path,
    shell_type: str,
    wsl_distro: str = "",
    credential_scope: str = "teamforge",
) -> tuple[str, list[Path]]:
    ts = int(time.time() * 1000)
    askpass_py = cwd / f"askpass_{ts}.py"
    if credential_scope == "github":
        credential_username = "x-access-token"
        credential_password = os.environ.get("GITHUB_TOKEN") or ""
        if not credential_password:
            raise RuntimeError("GITHUB_TOKEN is required for GitHub checkout")
    elif credential_scope == "teamforge":
        credential_username = (os.environ.get("TEAMFORGE_USER") or "").strip()
        credential_password = os.environ.get("TEAMFORGE_PASSWORD") or ""
        if not credential_username:
            raise RuntimeError("TEAMFORGE_USER is required for TeamForge checkout")
        if not credential_password:
            raise RuntimeError("TEAMFORGE_PASSWORD is required for TeamForge checkout")
    else:
        raise RuntimeError(f"unsupported checkout credential scope: {credential_scope}")

    created_files = [askpass_py]
    try:
        with open(askpass_py, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"credential_username = {credential_username!r}\n"
                f"credential_password = {credential_password!r}\n"
                "prompt = \" \".join(sys.argv[1:]).lower()\n"
                "is_username_prompt = \"username\" in prompt or prompt.startswith(\"user \")\n"
                "value = credential_username if is_username_prompt else credential_password\n"
                "sys.stdout.write(value + \"\\n\")"
            )
        if shell_type == "windows":
            askpass_bat = cwd / f"askpass_{ts}.bat"
            created_files.append(askpass_bat)
            with open(askpass_bat, "w", encoding="utf-8") as f:
                f.write(f'@python "{askpass_py}" %*')
            return str(askpass_bat), created_files

        wsl_path = windows_path_to_wsl(askpass_py)
        chmod_result = run_wsl_bash(f"chmod +x {shlex.quote(wsl_path)}", distro=wsl_distro)
        if chmod_result.returncode != 0:
            raise RuntimeError(f"failed to make askpass executable: {askpass_py}")
        return wsl_path, created_files
    except Exception:
        cleanup_askpass_files(created_files)
        raise

def checkout_credential_environment(askpass_exe: str) -> dict[str, str]:
    return {
        "GIT_ASKPASS": askpass_exe,
        "SSH_ASKPASS": askpass_exe,
        "SSH_ASKPASS_REQUIRE": "force",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "DISPLAY": ":0",
    }

def run_command(
    command: str,
    cwd: Path,
    shell_type: str,
    wsl_distro: str = "",
    credential_scope: str | None = None,
) -> int:
    selected_scope = credential_scope if credential_scope is not None else credential_scope_for_command(command, cwd)
    cleanup: list[Path] = []
    env = os.environ.copy()
    if selected_scope:
        askpass_exe, cleanup = create_askpass(
            cwd,
            shell_type,
            wsl_distro,
            credential_scope=selected_scope,
        )
        env.update(checkout_credential_environment(askpass_exe))
    try:
        if shell_type == "windows":
            return subprocess.run(command, cwd=str(cwd), shell=True, env=env, encoding='utf-8', errors='replace').returncode
        else:
            wsl_cwd = windows_path_to_wsl(cwd)
            wsl_env = checkout_credential_environment(askpass_exe) if selected_scope else None
            return run_wsl_bash(f"cd {shlex.quote(wsl_cwd)} && {command}", distro=wsl_distro, env=wsl_env).returncode
    finally:
        cleanup_askpass_files(cleanup)
    return 1

def _wsl_git_remote_url(wsl_cwd: str, distro: str) -> str:
    result = run_wsl_bash(
        f"cd {shlex.quote(wsl_cwd)} && git remote get-url origin",
        distro=distro,
        capture=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout else ""

def credential_scope_for_wsl_command(command: str, wsl_cwd: str, distro: str = "default") -> str | None:
    direct_scope = _credential_scope_from_text(command)
    if direct_scope:
        return direct_scope

    low = str(command or "").strip().lower()
    if re.search(r"(?:^|&&\s*)repo(?:\s+--[^\s]+)*\s+(?:init|sync)\b", low):
        return "teamforge"

    if not _is_network_checkout_command(command):
        return None

    remote_scope = _credential_scope_from_text(_wsl_git_remote_url(wsl_cwd, distro))
    return remote_scope or "teamforge"

def resolve_scan_paths(base: Path, paths: list[str], shell: str, distro: str, unit_name: str) -> list[Path]:
    resolved = []
    base_abs = base.resolve()
    for rel in paths:
        clean_rel = rel.replace("\\", "/").strip("/")
        if clean_rel.startswith("./"): clean_rel = clean_rel[2:]
        found_path = None
        full = base_abs / clean_rel
        if shell == "wsl": exists = (run_wsl_bash(f"test -d {shlex.quote(windows_path_to_wsl(full))}", distro).returncode == 0)
        else: exists = full.exists()
        if exists: found_path = full
        else:
            log(f"Searching for '{clean_rel}'...", unit=unit_name)
            if shell == "wsl":
                last_part = clean_rel.split("/")[-1]
                find_cmd = f"find {shlex.quote(windows_path_to_wsl(base))} -type d -iname {shlex.quote(last_part)} | grep -i {shlex.quote(clean_rel)} | head -n 1"
                res = run_wsl_bash(find_cmd, distro, capture=True).stdout.strip()
                if res: found_path = Path(wsl_path_to_windows(res, distro))
            else:
                clean_parts = tuple(p.lower() for p in clean_rel.split('/') if p)
                for root, dirs, _ in os.walk(str(base_abs)):
                    for d in dirs:
                        full_d = Path(root) / d
                        full_parts = tuple(p.lower() for p in full_d.parts)
                        if len(full_parts) >= len(clean_parts) and full_parts[-len(clean_parts):] == clean_parts:
                            found_path = full_d
                            break
                    if found_path:
                        break
        if found_path:
            normalized = Path(strip_windows_extended_prefix(str(found_path)))
            resolved.append(normalized.resolve())
            log(f"Matched: {rel} -> {found_path.name}", unit=unit_name)
        else: log(f"[SKIP] Path NOT found (ignored): {rel}", unit=unit_name)
    return resolved

def copy_tree_with_fallback(src: Path, dst: Path, unit_name: str) -> bool:
    src_abs = Path(strip_windows_extended_prefix(str(src.resolve()))).resolve()
    dst_abs = Path(strip_windows_extended_prefix(str(dst.resolve()))).resolve()

    if os.name == "nt":
        exclude_dirs = " ".join(EXCLUDE_DIR_NAMES)
        cmd = (
            f'robocopy "{robocopy_safe_path(src_abs)}" "{robocopy_safe_path(dst_abs)}" '
            f'/E /XD {exclude_dirs} /MT:8 /R:1 /W:1 /NDL /NJH /NJS /nc /ns /np'
        )
        rc = subprocess.run(cmd, shell=True, capture_output=True).returncode
        if rc < 8:
            return True
        log(f"[WARN] robocopy failed (exit {rc}), fallback to shutil.copytree: {src_abs} -> {dst_abs}", unit=unit_name)

    def _ignore(current_dir, names):
        ignored = []
        for name in names:
            candidate = Path(current_dir) / name
            if candidate.is_dir() and name in EXCLUDE_DIR_NAMES:
                ignored.append(name)
            elif candidate.is_file() and candidate.suffix.lower() in EXCLUDE_FILE_EXTENSIONS:
                ignored.append(name)
        return ignored

    try:
        shutil.copytree(src_abs, dst_abs, dirs_exist_ok=True, ignore=_ignore)
        return True
    except Exception as ex:
        log(f"[WARN] fallback copy failed: {src_abs} -> {dst_abs} ({ex})", unit=unit_name)
        return False


def run_blackduck_detect(command: str, unit_name: str, log_path: Path) -> tuple[int, str, str]:
    bom_url = ""
    ui_url = ""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as unit_log:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("Black Duck Detect output stream is unavailable")

        with process.stdout as output_stream:
            for raw_line in output_stream:
                unit_log.write(raw_line)
                unit_log.flush()
                line = raw_line.rstrip("\r\n")
                log(line, unit=unit_name)
                line_bom_url, line_ui_url = extract_blackduck_urls(line)
                if line_bom_url:
                    bom_url = line_bom_url
                    ui_url = line_ui_url

        return_code = process.wait()

    return return_code, bom_url, ui_url


def unit_result(
    unit_name: str,
    status: str,
    allow_failure: bool,
    blackduck_project: str,
    blackduck_version: str = "",
    blackduck_bom_url: str | None = None,
    blackduck_ui_url: str | None = None,
    **details,
) -> dict:
    result = {
        "unit": unit_name,
        "status": status,
        "allow_failure": allow_failure,
        "blackduck_project": blackduck_project,
        "blackduck_version": blackduck_version,
        "blackduck_bom_url": blackduck_bom_url,
        "blackduck_ui_url": blackduck_ui_url,
    }
    result.update(details)
    return result


def process_unit(
    unit: dict,
    base_dir: Path,
    oem: str,
    dry_run: bool = False,
    artifact_dir: Path | None = None,
) -> dict:
    name = unit["name"]
    allow_failure = bool(unit.get("allow_failure", False))
    blackduck_project = str(unit.get("blackduck_project", "")).strip()
    if not bool(unit.get("enabled", True)):
        return unit_result(name, "SKIPPED", allow_failure, blackduck_project)

    jira_key = read_jira_key()
    if not jira_key:
        return unit_result(
            name,
            "FAILED",
            allow_failure,
            blackduck_project,
            stage="config",
            message="Missing or invalid JIRA_KEY. Expected format: OSS-<number>",
        )

    blackduck_version = ""
    fix_ssh_permissions()
    cache_root = Path(os.environ.get("OSS_CACHE_ROOT", "C:/OSS_CACHE/WORKSPACES"))
    workdir = cache_root / oem.upper() / name
    staging = base_dir / f"staging_{name}"
    out_dir = base_dir / f"out_{name}"
    manage_disk_space(cache_root)

    for d in [staging, out_dir]:
        if d.exists(): shutil.rmtree(long_path(d), ignore_errors=True)
        os.makedirs(long_path(d), exist_ok=True)
    if not workdir.exists(): os.makedirs(long_path(workdir), exist_ok=True)

    log(f"Analyzing {oem}...", unit=name)
    cmds_list = sanitize_checkout_commands(unit.get("checkout_commands", []))
    is_repo = any("repo " in str(c).lower() for c in cmds_list)
    shell = "wsl" if is_repo else str(unit.get("shell_type", "windows")).lower()
    distro = str(unit.get("wsl_distro", "default"))
    
    checkout_success = False
    if shell == "wsl":
        res = run_wsl_bash("whoami", distro=distro, capture=True)
        wsl_user = res.stdout.replace('\x00', '').strip() if res.stdout else "oss-builder"
        wsl_cache = f"/home/{wsl_user}/blackduck_cache/{oem.upper()}/{name}"
        with WSL_SETUP_LOCK:
            run_wsl_bash(f"mkdir -p {wsl_cache}", distro=distro)
            setup_cmds = [f'git config --global user.email "{wsl_user}@example.com"', f'git config --global user.name "{wsl_user}"', 'git config --global http.postBuffer 524288000']
            setup_cmds += [c for c in cmds_list if "init" in c or "config" in c]
            run_wsl_bash(f"cd {shlex.quote(wsl_cache)} && " + " && ".join(setup_cmds), distro=distro)
        heavy_cmds = [c for c in cmds_list if "init" not in c and "config" not in c]
        checkout_success = True
        current_wsl_cwd = wsl_cache
        for raw_command in heavy_cmds:
            segments = [segment.strip() for segment in re.split(r'\s*&&\s*', raw_command) if segment.strip()]
            for segment in segments:
                cd_match = re.fullmatch(r'cd\s+["\']?([^"\']+)["\']?', segment, re.IGNORECASE)
                if cd_match:
                    target = cd_match.group(1).strip()
                    next_wsl_cwd = target if target.startswith("/") else f"{current_wsl_cwd.rstrip('/')}/{target}"
                    if run_wsl_bash(f"test -d {shlex.quote(next_wsl_cwd)}", distro=distro).returncode != 0:
                        log(f"ERROR: cd target does not exist: {next_wsl_cwd}", unit=name)
                        checkout_success = False
                        break
                    current_wsl_cwd = next_wsl_cwd
                    continue

                credential_scope = credential_scope_for_wsl_command(segment, current_wsl_cwd, distro)
                cleanup: list[Path] = []
                checkout_env = None
                try:
                    if credential_scope:
                        askpass_exe, cleanup = create_askpass(
                            workdir,
                            "wsl",
                            distro,
                            credential_scope=credential_scope,
                        )
                        checkout_env = checkout_credential_environment(askpass_exe)
                    rc = run_wsl_bash(
                        f"cd {shlex.quote(current_wsl_cwd)} && set -e && {segment}",
                        distro=distro,
                        env=checkout_env,
                    ).returncode
                finally:
                    cleanup_askpass_files(cleanup)

                if rc != 0:
                    log(f"ERROR: checkout command failed (exit {rc}): {segment}", unit=name)
                    checkout_success = False
                    break
            if not checkout_success:
                break
        if checkout_success:
            log(f"Syncing to Windows...", unit=name)
            for rel_path in unit.get("scan_paths", []):
                clean_rel = rel_path.replace("\\", "/").strip("/")
                find_cmd = f"find {shlex.quote(wsl_cache)} -type d -iname {shlex.quote(clean_rel.split('/')[-1])} | grep -i {shlex.quote(clean_rel)} | head -n 1"
                actual_src = run_wsl_bash(find_cmd, distro=distro, capture=True).stdout.strip()
                if actual_src:
                    dst_path = workdir / Path(*clean_rel.split("/"))
                    os.makedirs(long_path(dst_path.parent), exist_ok=True)
                    run_wsl_bash(f"rsync -av --inplace --delete {get_rsync_exclude_args()} {shlex.quote(actual_src)}/ {shlex.quote(windows_path_to_wsl(dst_path))}/", distro=distro)
                    log(f"Synced: {clean_rel} -> {dst_path}", unit=name)
                else:
                    log(f"[WARN] Source not found in WSL cache for: {clean_rel}", unit=name)
            checkout_success = True
    # else:
    #     new_cmds = []
    #     for c in cmds_list:
    #         new_cmds.append(c)
    #         match = re.search(r'cd\s+["\']?([^"\'\s&|;]+)', c)
    #         if match:
    #             folder = match.group(1).strip('"').strip("'")
    #             new_cmds.append(f'if exist "{folder}" (cd "{folder}" && git reset --hard HEAD && git clean -fd)')
    #     if run_command(" && ".join(new_cmds), workdir, "windows") == 0: checkout_success = True

    else:
        if run_windows_checkout_commands(cmds_list, workdir, name):
            checkout_success = True

    
    if not checkout_success:
        return unit_result(
            name,
            "FAILED",
            allow_failure,
            blackduck_project,
            stage="checkout",
        )

    try:
        workdir_abs = workdir.resolve()

        # For Windows git clone units, use the cloned repo subdirectory as the scan base
        # so that scan_paths like 'Components/' are found directly without relying on os.walk.
        # For WSL units, files are already rsynced into workdir directly, so workdir is correct.
        scan_base = workdir
        if shell == "windows":
            for c in cmds_list:
                repo_folder = _extract_clone_target_dir(str(c))
                if not repo_folder:
                    continue
                candidate = workdir / repo_folder
                if candidate.is_dir():
                    scan_base = candidate
                    log(f"Scan base resolved to: {scan_base}", unit=name)
                    break

            if scan_base == workdir:
                repo_candidates = [p for p in workdir.iterdir() if p.is_dir() and (p / ".git").is_dir()]
                if len(repo_candidates) == 1:
                    scan_base = repo_candidates[0]
                    log(f"Scan base resolved from workdir git folder: {scan_base}", unit=name)

        resolved = resolve_scan_paths(scan_base, unit.get("scan_paths", []), "windows", distro, name)
        if not resolved:
            log("ERROR: No valid scan paths found.", unit=name)
            return unit_result(
                name,
                "FAILED",
                allow_failure,
                blackduck_project,
                stage="path_resolution",
            )
        scan_base_abs = scan_base.resolve()
        normalized_requested_paths = [p.replace("\\", "/").strip("/") for p in unit.get("scan_paths", [])]
        for src in resolved:
            src_norm = Path(strip_windows_extended_prefix(str(src.resolve()))).resolve()
            try:
                rel_parts = src_norm.relative_to(scan_base_abs).parts
            except ValueError:
                try:
                    rel_parts = src_norm.relative_to(workdir_abs).parts
                except ValueError:
                    # Recover a stable destination name from the requested scan path suffix.
                    rel_parts = None
                    src_norm_posix = src_norm.as_posix().lower()
                    for req in normalized_requested_paths:
                        if src_norm_posix.endswith(req.lower()):
                            rel_parts = tuple(req.split("/"))
                            break
                    if not rel_parts:
                        rel_parts = [src_norm.name]
            # dst = staging / "__".join(rel_parts)
            dst = staging.joinpath(*rel_parts)
            log(f"Staging: {src_norm.name}", unit=name)
            if not dry_run:
                os.makedirs(long_path(dst.parent), exist_ok=True)
                if src_norm.is_dir():
                    if not copy_tree_with_fallback(src_norm, dst, name):
                        return {
                            "unit": name,
                            "status": "FAILED",
                            "stage": "staging_copy",
                            "message": f"Failed to copy {src_norm} to {dst}",
                            "allow_failure": allow_failure,
                        }
                else:
                    os.makedirs(long_path(dst.parent), exist_ok=True)
                    shutil.copy2(src_norm, dst)
        
        if not dry_run and resolved:
            jar = os.environ.get("BLACKDUCK_DETECT_JAR", r"C:\tools\blackduck\11.0.0\detect-11.0.0.jar")
            url = os.environ.get("BLACKDUCK_URL", "https://192.0.2.10")
            token = os.environ.get("BLACKDUCK_TOKEN")
            if not jar or not url or not token:
                log(f"ERROR: Missing environment variables. URL={url}, TOKEN={'SET' if token else 'MISSING'}", unit=name)
                return unit_result(
                    name,
                    "FAILED",
                    allow_failure,
                    blackduck_project,
                    stage="config",
                )
            blackduck_version = with_jira_prefix(unit["blackduck_version"], jira_key)
            bd_cmd = (f'java -jar "{jar}" --blackduck.url="{url}" --blackduck.api.token="{token}" '
                      f'--detect.project.name="{blackduck_project}" --detect.project.version.name="{blackduck_version}" '
                      f'--detect.source.path="{long_path(staging)}" --detect.output.path="{long_path(out_dir)}" '
                      f'--blackduck.trust.cert=true --detect.tools=SIGNATURE_SCAN --detect.cleanup=false '
                      f'--detect.blackduck.signature.scanner.snippet.matching=SNIPPET_MATCHING '
                      f'--detect.blackduck.signature.scanner.license.search=true '
                      f'--detect.blackduck.signature.scanner.copyright.search=true')
            detect_log_path = (artifact_dir or base_dir) / f"detect_{unit_artifact_name(name)}.log"
            detect_return_code, bom_url, ui_url = run_blackduck_detect(
                bd_cmd,
                name,
                detect_log_path,
            )
            if detect_return_code != 0:
                return unit_result(
                    name,
                    "FAILED",
                    allow_failure,
                    blackduck_project,
                    blackduck_version,
                    bom_url or None,
                    ui_url or None,
                    stage="scan",
                )
            if not bom_url:
                log("WARN: Black Duck Detect completed without a BOM URL", unit=name)
            return unit_result(
                name,
                "SUCCESS",
                allow_failure,
                blackduck_project,
                blackduck_version,
                bom_url or None,
                ui_url or None,
            )
    except Exception as e:
        log(f"Error: {e}", unit=name)
        return unit_result(
            name,
            "FAILED",
            allow_failure,
            blackduck_project,
            blackduck_version,
            stage="process",
            message=str(e),
        )
    return unit_result(
        name,
        "SUCCESS",
        allow_failure,
        blackduck_project,
        blackduck_version,
    )

def main():
    if len(sys.argv) < 2:
        print("Usage: python run-bld.py <manifest.yaml> [OSS-<number>] [--dry-run]")
        sys.exit(1)

    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args:
        print("Usage: python run-bld.py <manifest.yaml> [OSS-<number>] [--dry-run]")
        sys.exit(1)

    m_path = Path(args[0]).resolve()
    jira_arg = args[1].strip().upper() if len(args) >= 2 else ""
    if jira_arg:
        if not JIRA_KEY_PATTERN.fullmatch(jira_arg):
            print(f"ERROR: Invalid Jira ticket format: {jira_arg}. Expected OSS-<number>")
            sys.exit(1)
        os.environ["JIRA_KEY"] = jira_arg
        log(f"[JIRA] using ticket: {jira_arg}")

    with m_path.open("r", encoding="utf-8") as f: data = yaml.safe_load(f)
    oem = data.get("request", {}).get("oem", "COMMON")
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    base_dir = m_path.parent / "workspace_runs" / run_id
    base_dir.mkdir(parents=True, exist_ok=True)
    log(f"Starting execution for OEM: {oem}...")
    try:
        for unit in data["scan_units"]:
            unit_name = str(unit.get("name", "unit"))
            detect_log_path = m_path.parent / f"detect_{unit_artifact_name(unit_name)}.log"
            if detect_log_path.exists():
                detect_log_path.unlink()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(process_unit, u, base_dir, oem, dry_run, m_path.parent)
                for u in data["scan_units"]
            ]
            results = [f.result() for f in futures]
        summary = {"run_id": run_id, "oem": oem, "status": "SUCCESS" if all(r["status"] == "SUCCESS" or r.get("allow_failure", False) for r in results) else "FAILED", "units": results}
        log("\n" + "=" * 100 + "\n[SUMMARY]\n" + json.dumps(summary, indent=2))
        (m_path.parent / "last_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        if summary["status"] != "SUCCESS":
            log("ERROR: One or more scan_units failed.")
            sys.exit(1)

        log("SUCCESS: all scan_units completed")
        sys.exit(0)
    except KeyboardInterrupt: log("\n[ABORT] Execution interrupted by user."); sys.exit(1)

if __name__ == "__main__": main()
