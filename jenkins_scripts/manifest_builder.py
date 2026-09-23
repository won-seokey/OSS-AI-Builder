import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Please install with: pip install pyyaml")
    sys.exit(1)

from manifest_validator import ManifestValidator, render_text_report


FORBIDDEN_LINUX_CMDS = ["chmod", "chown", "sudo", "apt", "yum", "dnf", "apk"]


def ask_text(prompt: str, default: str = "", required: bool = False, upper: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default:
            value = default
        if upper:
            value = value.upper()
        if required and not value:
            print("  - This field is required.")
            continue
        return value


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    default = "Y/n" if default_yes else "y/N"
    while True:
        raw = input(f"{prompt} [{default}]: ").strip().lower()
        if not raw:
            return default_yes
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("  - Enter y or n.")


def ask_lines(title: str) -> list[str]:
    print(title)
    print("  - Enter one line at a time, then press Enter on empty line to finish.")
    rows: list[str] = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        rows.append(line)
    return rows


def ask_block(title: str, end_token: str = "END") -> list[str]:
    print(title)
    print(f"  - Paste lines, then type '{end_token}' on a new line.")
    rows: list[str] = []
    while True:
        line = input("  | ")
        if line.strip() == end_token:
            break
        rows.append(line.rstrip())
    return rows


def strip_markdown_link(text: str) -> str:
    # Convert [label](url) into url for copied Jira/Markdown content.
    m = re.fullmatch(r"\[(.*?)\]\((.*?)\)", text.strip())
    if m:
        return m.group(2).strip()
    return text


def _clean_list_prefix(text: str) -> str:
    return re.sub(r"^\s*(?:[-*]|\d+[.)]|[a-zA-Z][.)])\s*", "", text)


def _normalize_path_token(text: str) -> str:
    token = strip_markdown_link(text).strip()
    token = _clean_list_prefix(token)
    token = token.strip().strip('"').strip("'").rstrip(",;:")
    token = re.sub(r"\s*/\s*", "/", token)
    token = token.replace("\\", "/")
    token = re.sub(r"/{2,}", "/", token)
    return token.strip()


def _join_paths(parent: str, child: str) -> str:
    p = parent.rstrip("/")
    c = child.lstrip("/")
    if not p:
        return c
    if not c:
        return p
    if c.lower().startswith((p + "/").lower()):
        return c
    return f"{p}/{c}"


def expand_scan_paths(raw_lines: list[str]) -> list[str]:
    records: list[dict[str, Any]] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        token = _normalize_path_token(raw)
        if not token:
            continue
        if token.endswith(":") and "/" not in token:
            continue
        records.append({"indent": indent, "token": token})

    if not records:
        return []

    nodes: list[dict[str, Any]] = []
    stack: list[tuple[int, int]] = []
    for rec in records:
        indent = int(rec["indent"])
        token = str(rec["token"])

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent_idx = stack[-1][1] if stack else None
        node_idx = len(nodes)
        nodes.append({"token": token, "parent": parent_idx, "children": 0})
        if parent_idx is not None:
            nodes[parent_idx]["children"] += 1

        stack.append((indent, node_idx))

    paths: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        if node["children"] > 0:
            continue

        path = str(node["token"])
        parent_idx = node["parent"]
        while parent_idx is not None:
            parent_token = str(nodes[parent_idx]["token"])
            path = _join_paths(parent_token, path)
            parent_idx = nodes[parent_idx]["parent"]

        key = path.lower()
        if key not in seen:
            seen.add(key)
            paths.append(path)

    return paths


def normalize_checkout_line(text: str) -> str:
    line = strip_markdown_link(text).strip()
    if not line:
        return ""
    line = re.sub(r"^\s*(?:[-*]|\d+[.)]|[a-zA-Z][.)])\s*", "", line)
    line = line.replace('\\"', '"').replace("\\'", "'")
    return line.strip()


def looks_like_checkout_command(line: str) -> bool:
    low = line.strip().lower()
    return low.startswith(
        (
            "git ",
            "repo ",
            "cd ",
            "curl ",
            "scp ",
            "ssh ",
            "export ",
            "set ",
            "python ",
            "powershell ",
            "bash ",
            "./",
        )
    )


def checkout_command_violations(commands: list[str]) -> list[str]:
    violations: list[str] = []
    for idx, cmd in enumerate(commands):
        loc = f"command[{idx}]"
        if "&&" in cmd:
            violations.append(f"{loc}: '&&' is not allowed. Use one command per line.")

        low = cmd.lower()
        for forbidden in FORBIDDEN_LINUX_CMDS:
            if re.search(rf"(^|\s){re.escape(forbidden)}(\s|$)", low):
                violations.append(f"{loc}: linux command '{forbidden}' is not allowed.")
                break

    return violations


def split_chained_commands(command: str) -> list[str]:
    # Wiki rule requires one command per line. Split common chain separators.
    parts = re.split(r"\s*(?:&&|;)\s*", command)
    return [p.strip() for p in parts if p.strip()]


def is_forbidden_linux_command(command: str) -> tuple[bool, str]:
    low = command.strip().lower()
    if not low:
        return False, ""

    tokens = low.split()
    if not tokens:
        return False, ""

    first = tokens[0]
    if first in FORBIDDEN_LINUX_CMDS:
        return True, first

    for forbidden in FORBIDDEN_LINUX_CMDS:
        if re.search(rf"(^|\s){re.escape(forbidden)}(\s|$)", low):
            return True, forbidden
    return False, ""


def auto_normalize_checkout_commands(commands: list[str]) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    notes: list[str] = []

    for idx, cmd in enumerate(commands):
        split_parts = split_chained_commands(cmd)
        if len(split_parts) > 1:
            notes.append(f"command[{idx}] was split into {len(split_parts)} commands")

        for part in split_parts:
            forbidden, matched = is_forbidden_linux_command(part)
            if forbidden:
                notes.append(f"removed forbidden linux command '{matched}': {part}")
                continue
            normalized.append(part)

    return normalized, notes


def print_checkout_rule_guide() -> None:
    print("  - checkout_commands rules:")
    print("    1) One command per line (no '&&').")
    print("    2) Linux commands are not allowed (e.g. chmod).")


def normalize_scan_path_line(text: str) -> str:
    line = _normalize_path_token(text)
    if not line:
        return ""
    if line.endswith(":") and "/" not in line:
        return ""

    return line


def ask_choice(prompt: str, options: list[tuple[str, str]], default_key: str) -> str:
    option_text = ", ".join(f"{k}={label}" for k, label in options)
    valid = {k for k, _ in options}
    while True:
        picked = input(f"{prompt} ({option_text}) [{default_key}]: ").strip().lower()
        if not picked:
            return default_key
        if picked == "end":
            # During paste-driven flow users often type END by habit.
            return default_key
        if picked in valid:
            return picked
        print(f"  - Invalid choice. Pick one of: {', '.join(sorted(valid))}")


def build_checkout_commands(unit_name: str) -> list[str]:
    mode = ask_choice(
        f"[{unit_name}] checkout pattern",
        [
            ("p", "paste command block"),
            ("g", "guided git clone"),
            ("r", "guided repo init/sync"),
            ("m", "manual line input"),
        ],
        "p",
    )

    if mode == "p":
        while True:
            lines = ask_block(
                f"[{unit_name}] paste checkout commands (auto-split '&&', auto-remove forbidden linux commands)",
                end_token="END",
            )
            normalized = [normalize_checkout_line(x) for x in lines]
            commands = [x for x in normalized if x]

            commands, notes = auto_normalize_checkout_commands(commands)
            if notes:
                print("  - Auto-normalized checkout commands:")
                for n in notes:
                    print(f"  - {n}")

            if commands and not any(looks_like_checkout_command(x) for x in commands):
                print("  - This block looks like scan paths, not checkout commands.")
                if ask_yes_no("  Re-paste checkout commands now?", default_yes=True):
                    continue
                return commands

            invalid_lines = [i for i, x in enumerate(commands) if not looks_like_checkout_command(x)]
            if invalid_lines:
                print("  - Some lines are not recognized as commands.")
                for i in invalid_lines:
                    print(f"  - command[{i}]: '{commands[i]}'")
                if ask_yes_no("  Re-paste checkout commands now?", default_yes=True):
                    continue

            violations = checkout_command_violations(commands)
            if violations:
                print_checkout_rule_guide()
                for v in violations:
                    print(f"  - {v}")
                if ask_yes_no("  Re-paste checkout commands with valid format?", default_yes=True):
                    continue

            return commands

    if mode == "g":
        teamforge_user = ask_text("  TeamForge user (account)", default="${TEAMFORGE_USER}")
        repo = ask_text("  repo name (e.g. renault_gen3_appl)", required=True)
        repo_dir = ask_text("  repo dir", default=repo)
        branch = ask_text("  branch/tag", required=True)
        user_name = ask_text("  git user.name", default="<USER_NAME>")
        user_email = ask_text("  git user.email", default="<USER_EMAIL>")

        return [
            f"git clone --recurse-submodules https://{teamforge_user}@teamforge.example.com/gerrit/{repo}",
            f'cd "{repo_dir}"',
            f'git config user.name "{user_name}"',
            f'git config user.email "{user_email}"',
            f"curl -o .git/hooks/commit-msg https://{teamforge_user}@teamforge.example.com/gerrit/tools/hooks/commit-msg",
            f"git checkout {branch}",
        ]

    if mode == "r":
        teamforge_user = ask_text("  TeamForge user (account)", default="${TEAMFORGE_USER}")
        manifest_repo = ask_text("  manifest repo path", required=True)
        manifest_branch = ask_text("  manifest branch", required=True)
        manifest_file = ask_text("  manifest file", required=True)
        work_branch = ask_text("  repo start branch", default="blackduck_scan")

        return [
            f"repo init -u ssh://{teamforge_user}@teamforge.example.com:29418/{manifest_repo} -b {manifest_branch} -m {manifest_file}",
            "repo sync",
            f"repo start {work_branch} --all",
        ]

    while True:
        commands = ask_lines(f"[{unit_name}] checkout commands")
        commands, notes = auto_normalize_checkout_commands(commands)
        if notes:
            print("  - Auto-normalized checkout commands:")
            for n in notes:
                print(f"  - {n}")

        violations = checkout_command_violations(commands)
        if not violations:
            return commands

        print_checkout_rule_guide()
        for v in violations:
            print(f"  - {v}")
        if not ask_yes_no("  Re-enter checkout commands?", default_yes=True):
            return commands


def build_scan_paths(unit_name: str) -> list[str]:
    mode = ask_choice(
        f"[{unit_name}] scan path input",
        [("p", "paste path block"), ("l", "line-by-line input")],
        "p",
    )

    if mode == "p":
        lines = ask_block(
            f"[{unit_name}] paste scan paths (bullets/numbering/indented subtree allowed)",
            end_token="END",
        )
        return expand_scan_paths(lines)

    manual_lines = ask_lines(f"[{unit_name}] scan paths")
    normalized = [normalize_scan_path_line(x) for x in manual_lines]
    return [x for x in normalized if x]


def build_scan_unit(default_name: str = "APPL") -> dict[str, Any]:
    print("\n=== Scan Unit ===")
    name = ask_text("unit name", default=default_name, required=True, upper=True)

    checkout_commands = build_checkout_commands(name)
    if not checkout_commands:
        print("  - No checkout commands entered. You can still save and edit later.")

    scan_paths = build_scan_paths(name)
    if not scan_paths:
        print("  - No scan paths entered. You can still save and edit later.")

    blackduck_project = ask_text("blackduck project", required=True)
    blackduck_version = ask_text("blackduck version", required=True)

    return {
        "name": name,
        "checkout_commands": checkout_commands,
        "scan_paths": scan_paths,
        "blackduck_project": blackduck_project,
        "blackduck_version": blackduck_version,
    }


def build_manifest() -> dict[str, Any]:
    print("=== Black Duck Manifest Builder (Phase 2A) ===")
    print("Create a manifest YAML by answering prompts.")

    oem = ask_text("OEM", required=True, upper=True)
    project_name = ask_text("project name", required=True)
    sw_version = ask_text("sw_version", required=True)

    units: list[dict[str, Any]] = []
    units.append(build_scan_unit(default_name="APPL"))

    while ask_yes_no("Add another scan unit?", default_yes=True):
        next_default = "FBL" if len(units) == 1 else f"UNIT_{len(units) + 1}"
        units.append(build_scan_unit(default_name=next_default))

    return {
        "request": {
            "oem": oem,
            "project_name": project_name,
            "sw_version": sw_version,
        },
        "scan_units": units,
    }


def run_validation(manifest_path: Path, strict_wiki: bool = False) -> int:
    mode = "core" if strict_wiki else "off"
    validator = ManifestValidator(manifest_path, strict_wiki_mode=mode)
    if validator.load():
        validator.validate()

    print("\n=== Validation Result ===")
    print(render_text_report(validator.issues))

    has_error = any(i.level == "ERROR" for i in validator.issues)
    return 1 if has_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive builder for Black Duck manifest YAML")
    parser.add_argument(
        "--output",
        default="generated_manifest.yaml",
        help="output file path (default: generated_manifest.yaml)",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="skip post-generation validation",
    )
    parser.add_argument(
        "--strict-wiki",
        action="store_true",
        help="run post-generation validation with strict wiki rules",
    )
    args = parser.parse_args()

    data = build_manifest()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)

    print(f"\nSaved manifest: {out_path}")

    if args.skip_validate:
        return 0
    return run_validation(out_path, strict_wiki=args.strict_wiki)


if __name__ == "__main__":
    sys.exit(main())
