import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackduck_catalog import build_catalog_index, load_catalog, normalize_key, normalize_oem

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Please install with: pip install pyyaml")
    sys.exit(1)


@dataclass
class Issue:
    level: str
    code: str
    message: str
    location: str


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+")
TEAMFORGE_USER_RE = re.compile(r"([a-zA-Z0-9._%+-]+)@teamforge\.example\.com")
BLACKDUCK_VERSION_STRICT_RE = re.compile(r"^SW\s*V\d+(?:\.\d+)*_(APPL|FBL)$")
FORBIDDEN_LINUX_CMDS = ["chmod", "chown", "sudo", "apt", "yum", "dnf", "apk"]


class ManifestValidator:
    def __init__(
        self,
        manifest_path: Path,
        strict_wiki_mode: str = "off",
        strict_bd_catalog: bool = False,
        bd_catalog_index: dict[str, Any] | None = None,
    ):
        self.manifest_path = manifest_path
        self.strict_wiki_mode = strict_wiki_mode
        self.strict_bd_catalog = strict_bd_catalog
        self.bd_catalog_index = bd_catalog_index or {}
        self.issues: list[Issue] = []
        self.data: dict[str, Any] = {}

    @property
    def strict_wiki_core(self) -> bool:
        return self.strict_wiki_mode in {"core", "full"}

    @property
    def strict_wiki_full(self) -> bool:
        return self.strict_wiki_mode == "full"

    def add_issue(self, level: str, code: str, message: str, location: str) -> None:
        self.issues.append(Issue(level=level, code=code, message=message, location=location))

    def load(self) -> bool:
        if not self.manifest_path.exists():
            self.add_issue("ERROR", "FILE_NOT_FOUND", "manifest file does not exist", "$")
            return False

        try:
            with self.manifest_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            self.add_issue("ERROR", "YAML_PARSE_ERROR", str(exc), "$")
            return False
        except OSError as exc:
            self.add_issue("ERROR", "FILE_READ_ERROR", str(exc), "$")
            return False

        if not isinstance(loaded, dict):
            self.add_issue("ERROR", "ROOT_TYPE", "manifest root must be a mapping object", "$")
            return False

        self.data = loaded
        return True

    def validate(self) -> None:
        self._validate_request()
        self._validate_scan_units()

    def _validate_request(self) -> None:
        request = self.data.get("request")
        if not isinstance(request, dict):
            self.add_issue("ERROR", "REQUEST_MISSING", "'request' must be an object", "$.request")
            return

        for key in ["oem", "project_name", "sw_version"]:
            val = request.get(key)
            if not isinstance(val, str) or not val.strip():
                self.add_issue(
                    "ERROR",
                    "REQUEST_FIELD",
                    f"'request.{key}' must be a non-empty string",
                    f"$.request.{key}",
                )

        oem = request.get("oem")
        if isinstance(oem, str) and oem and oem != oem.upper():
            self.add_issue(
                "WARN",
                "OEM_CASE",
                "OEM is not uppercase; uppercase is recommended",
                "$.request.oem",
            )

    def _validate_scan_units(self) -> None:
        units = self.data.get("scan_units")
        if not isinstance(units, list):
            self.add_issue("ERROR", "SCAN_UNITS_TYPE", "'scan_units' must be an array", "$.scan_units")
            return

        if not units:
            self.add_issue("ERROR", "SCAN_UNITS_EMPTY", "'scan_units' must contain at least one unit", "$.scan_units")
            return

        for idx, unit in enumerate(units):
            loc = f"$.scan_units[{idx}]"
            if not isinstance(unit, dict):
                self.add_issue("ERROR", "UNIT_TYPE", "scan unit must be an object", loc)
                continue
            self._validate_unit(idx, unit)

    def _validate_unit(self, idx: int, unit: dict[str, Any]) -> None:
        loc = f"$.scan_units[{idx}]"
        request_oem = normalize_oem(str(self.data.get("request", {}).get("oem", "")))

        required_str_fields = ["name", "blackduck_project", "blackduck_version"]
        for key in required_str_fields:
            val = unit.get(key)
            if not isinstance(val, str) or not val.strip():
                self.add_issue(
                    "ERROR",
                    "UNIT_FIELD",
                    f"'{key}' must be a non-empty string",
                    f"{loc}.{key}",
                )

        if self.strict_wiki_full:
            unit_name = unit.get("name")
            if isinstance(unit_name, str) and unit_name.strip() and unit_name not in {"APPL", "FBL"}:
                self.add_issue(
                    "ERROR",
                    "WIKI_UNIT_NAME",
                    "strict-wiki: unit name must be APPL or FBL",
                    f"{loc}.name",
                )

            bd_project = unit.get("blackduck_project")
            req_project = self.data.get("request", {}).get("project_name")
            if isinstance(bd_project, str) and isinstance(req_project, str):
                if bd_project.strip() and req_project.strip() and bd_project.strip() != req_project.strip():
                    self.add_issue(
                        "WARN",
                        "WIKI_PROJECT_NAME_MISMATCH",
                        "strict-wiki: blackduck_project and request.project_name should match PMS project name",
                        f"{loc}.blackduck_project",
                    )

            bd_version = unit.get("blackduck_version")
            if isinstance(bd_version, str) and bd_version.strip():
                if not BLACKDUCK_VERSION_STRICT_RE.fullmatch(bd_version.strip()):
                    self.add_issue(
                        "ERROR",
                        "WIKI_BLACKDUCK_VERSION",
                        "strict-wiki: blackduck_version must match 'SW Vx.x.x_APPL|FBL'",
                        f"{loc}.blackduck_version",
                    )
                else:
                    suffix = bd_version.strip().split("_")[-1]
                    if isinstance(unit_name, str) and unit_name.strip() and suffix != unit_name.strip():
                        self.add_issue(
                            "ERROR",
                            "WIKI_BLACKDUCK_SUFFIX",
                            "strict-wiki: blackduck_version suffix must match unit name",
                            f"{loc}.blackduck_version",
                        )

        if self.strict_bd_catalog:
            project = unit.get("blackduck_project")
            if isinstance(project, str) and project.strip():
                by_project = self.bd_catalog_index.get("by_project", {})
                p_entry = by_project.get(normalize_key(project))
                if not p_entry:
                    self.add_issue(
                        "ERROR",
                        "BD_PROJECT_NOT_FOUND",
                        "strict-bd-catalog: blackduck_project is not in known catalog",
                        f"{loc}.blackduck_project",
                    )
                else:
                    catalog_oem = normalize_oem(p_entry.get("oem", ""))
                    if request_oem and catalog_oem and request_oem != catalog_oem:
                        self.add_issue(
                            "ERROR",
                            "BD_PROJECT_OEM_MISMATCH",
                            "strict-bd-catalog: project does not belong to request.oem",
                            f"{loc}.blackduck_project",
                        )

        checkout_commands = unit.get("checkout_commands")
        if not isinstance(checkout_commands, list):
            self.add_issue(
                "ERROR",
                "CHECKOUT_TYPE",
                "'checkout_commands' must be an array of command strings",
                f"{loc}.checkout_commands",
            )
        elif not checkout_commands:
            self.add_issue(
                "WARN",
                "CHECKOUT_EMPTY",
                "'checkout_commands' is empty",
                f"{loc}.checkout_commands",
            )
        else:
            self._validate_checkout_commands(idx, checkout_commands)

        scan_paths = unit.get("scan_paths")
        if not isinstance(scan_paths, list):
            self.add_issue(
                "ERROR",
                "SCAN_PATHS_TYPE",
                "'scan_paths' must be an array of path strings",
                f"{loc}.scan_paths",
            )
        elif not scan_paths:
            self.add_issue(
                "WARN",
                "SCAN_PATHS_EMPTY",
                "'scan_paths' is empty",
                f"{loc}.scan_paths",
            )
        else:
            self._validate_scan_paths(idx, scan_paths)

    def _validate_checkout_commands(self, idx: int, commands: list[Any]) -> None:
        unit_loc = f"$.scan_units[{idx}].checkout_commands"
        seen_hardcoded_account = False

        for cmd_idx, cmd in enumerate(commands):
            loc = f"{unit_loc}[{cmd_idx}]"
            if not isinstance(cmd, str) or not cmd.strip():
                self.add_issue("ERROR", "CHECKOUT_CMD_TYPE", "command must be a non-empty string", loc)
                continue

            # Current policy is warn-only for personal account/email hardcoding.
            if EMAIL_RE.search(cmd) and "${" not in cmd:
                self.add_issue(
                    "WARN",
                    "HARDCODED_EMAIL",
                    "hardcoded email detected in checkout command",
                    loc,
                )

            if TEAMFORGE_USER_RE.search(cmd) and "${" not in cmd:
                seen_hardcoded_account = True

            if self.strict_wiki_core:
                cmd_text = cmd.strip()
                low = cmd_text.lower()
                if "&&" in cmd_text:
                    self.add_issue(
                        "ERROR",
                        "WIKI_CHECKOUT_CHAIN",
                        "strict-wiki: one command per line only; '&&' is not allowed",
                        loc,
                    )

                for forbidden in FORBIDDEN_LINUX_CMDS:
                    if re.search(rf"(^|\s){re.escape(forbidden)}(\s|$)", low):
                        self.add_issue(
                            "ERROR",
                            "WIKI_FORBIDDEN_LINUX_CMD",
                            f"strict-wiki: linux command '{forbidden}' is not allowed",
                            loc,
                        )
                        break

        if seen_hardcoded_account:
            self.add_issue(
                "WARN",
                "HARDCODED_TEAMFORGE_USER",
                "hardcoded teamforge account detected; consider env var substitution",
                unit_loc,
            )

    def _validate_scan_paths(self, idx: int, scan_paths: list[Any]) -> None:
        unit_loc = f"$.scan_units[{idx}].scan_paths"
        normalized_seen: dict[str, int] = {}
        has_forward_slash = False
        has_backslash = False

        for path_idx, path_val in enumerate(scan_paths):
            loc = f"{unit_loc}[{path_idx}]"

            if not isinstance(path_val, str) or not path_val.strip():
                self.add_issue("ERROR", "SCAN_PATH_TYPE", "scan path must be a non-empty string", loc)
                continue

            text = path_val.strip()
            if "/" in text:
                has_forward_slash = True
            if "\\" in text:
                has_backslash = True

            norm = text.replace("\\", "/").rstrip("/").lower()
            if norm in normalized_seen:
                first_idx = normalized_seen[norm]
                self.add_issue(
                    "WARN",
                    "SCAN_PATH_DUPLICATE",
                    f"duplicate scan path also appears at index {first_idx}",
                    loc,
                )
            else:
                normalized_seen[norm] = path_idx

            if "{" in text or "}" in text:
                self.add_issue(
                    "WARN",
                    "SCAN_PATH_SUSPECT",
                    "scan path contains braces; verify this is not nested-yaml style content",
                    loc,
                )

            if self.strict_wiki_core:
                norm = text.replace("\\", "/").strip()
                padded = f"/{norm}/"

                if norm.startswith("./") or norm.startswith(".\\") or norm == ".":
                    self.add_issue(
                        "ERROR",
                        "WIKI_SCAN_PATH_DOT_PREFIX",
                        "strict-wiki: scan_paths must not start with './' or '.\\'",
                        loc,
                    )

                if (
                    norm.startswith("../")
                    or norm == ".."
                    or "/../" in padded
                    or padded.endswith("/../")
                ):
                    self.add_issue(
                        "ERROR",
                        "WIKI_SCAN_PATH_TRAVERSAL",
                        "strict-wiki: scan_paths must not contain parent traversal ('../' or '..\\')",
                        loc,
                    )

        if has_forward_slash and has_backslash:
            self.add_issue(
                "WARN",
                "SCAN_PATH_SEPARATOR_MIX",
                "mixed '/' and '\\' separators detected in one scan unit; use one style consistently",
                unit_loc,
            )


def render_text_report(issues: list[Issue]) -> str:
    lines: list[str] = []

    if not issues:
        return "[PASS] No issues found."

    for i in issues:
        lines.append(f"[{i.level}] {i.code} {i.location} - {i.message}")

    err = sum(1 for i in issues if i.level == "ERROR")
    warn = sum(1 for i in issues if i.level == "WARN")
    info = sum(1 for i in issues if i.level == "INFO")
    lines.append("")
    lines.append(f"Summary: ERROR={err}, WARN={warn}, INFO={info}")
    return "\n".join(lines)


def to_json_report(path: Path, issues: list[Issue]) -> dict[str, Any]:
    return {
        "manifest": str(path),
        "summary": {
            "error": sum(1 for i in issues if i.level == "ERROR"),
            "warn": sum(1 for i in issues if i.level == "WARN"),
            "info": sum(1 for i in issues if i.level == "INFO"),
        },
        "issues": [
            {
                "level": i.level,
                "code": i.code,
                "location": i.location,
                "message": i.message,
            }
            for i in issues
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Black Duck manifest YAML")
    parser.add_argument("manifest", help="path to manifest yaml")
    parser.add_argument("--json", action="store_true", help="output machine-readable JSON report")
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="treat WARN as failure (non-zero exit)",
    )
    parser.add_argument(
        "--strict-wiki",
        action="store_true",
        help="enforce core wiki rules (checkout_commands and scan_paths)",
    )
    parser.add_argument(
        "--strict-wiki-full",
        action="store_true",
        help="enforce full wiki rules (core + version/project/unit naming checks)",
    )
    parser.add_argument(
        "--strict-bd-catalog",
        action="store_true",
        help="enforce Black Duck project catalog matching (project existence + OEM mapping)",
    )
    parser.add_argument(
        "--bd-catalog",
        default=str(Path(__file__).resolve().parent / "blackduck_catalog.yaml"),
        help="path to Black Duck catalog yaml",
    )
    args = parser.parse_args()

    strict_mode = "off"
    if args.strict_wiki:
        strict_mode = "core"
    if args.strict_wiki_full:
        strict_mode = "full"

    bd_index: dict[str, Any] | None = None
    if args.strict_bd_catalog:
        catalog_entries = load_catalog(
            catalog_path=Path(args.bd_catalog),
            cache_path=None,
            api_url=None,
            api_token=None,
            ssl_context=None,
        )
        bd_index = build_catalog_index(catalog_entries)

    manifest_path = Path(args.manifest)
    validator = ManifestValidator(
        manifest_path,
        strict_wiki_mode=strict_mode,
        strict_bd_catalog=args.strict_bd_catalog,
        bd_catalog_index=bd_index,
    )
    loaded = validator.load()
    if loaded:
        validator.validate()

    if args.json:
        print(json.dumps(to_json_report(manifest_path, validator.issues), ensure_ascii=False, indent=2))
    else:
        print(render_text_report(validator.issues))

    has_error = any(i.level == "ERROR" for i in validator.issues)
    has_warn = any(i.level == "WARN" for i in validator.issues)
    if has_error:
        return 1
    if args.fail_on_warn and has_warn:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
