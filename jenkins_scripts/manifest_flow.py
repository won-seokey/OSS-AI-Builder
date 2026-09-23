import argparse
import os
import subprocess
import sys
from pathlib import Path

from pipeline_common import run_with_retry, write_text


def generate_manifest_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    request_text_file = output_dir / "jira_request_text.txt"
    manifest_path_file = output_dir / "manifest_path.txt"
    generated_manifest = output_dir / "scan-manifest.yaml"

    if not request_text_file.exists():
        print("NO TEXT INPUT - skip AI manifest generation")
        return 0

    if manifest_path_file.exists():
        existing_path = manifest_path_file.read_text(encoding="utf-8").strip()
        if existing_path and Path(existing_path).exists():
            print("MANIFEST PATH already available - skip AI generation")
            return 0

    if not (os.environ.get(args.openai_key_env) or "").strip():
        raise RuntimeError(f"{args.openai_key_env} is empty")

    python_cmd = args.python_cmd or sys.executable
    builder = Path(args.builder_path).resolve()
    validator = Path(args.validator_path).resolve()
    catalog = Path(args.catalog_path).resolve()

    if not builder.exists():
        raise RuntimeError(f"builder not found: {builder}")
    if not validator.exists():
        raise RuntimeError(f"validator not found: {validator}")
    if not catalog.exists():
        raise RuntimeError(f"catalog not found: {catalog}")

    if generated_manifest.exists():
        generated_manifest.unlink()

    print("===== Generate Manifest with AI Builder =====")
    build_cmd = [
        python_cmd,
        str(builder),
        "--input-text-file",
        str(request_text_file),
        "--output",
        str(generated_manifest),
        "--model",
        args.model,
        "--base-url",
        args.base_url,
        "--reasoning-effort",
        args.reasoning_effort,
        "--non-interactive",
        "--strict-wiki-full",
        "--bd-catalog",
        str(catalog),
        "--system-prompt-path",
        str(Path(args.system_prompt_path).resolve()),
    ]
    if args.insecure:
        build_cmd.append("--insecure")
    else:
        build_cmd.append("--verify-ssl")

    build_exit = run_with_retry(build_cmd, max_attempts=args.max_attempts)
    if build_exit != 0:
        raise RuntimeError(f"AI manifest generation failed with exit code {build_exit}")

    if not generated_manifest.exists():
        raise RuntimeError(f"generated manifest not found: {generated_manifest}")

    print("===== Validate Generated Manifest (strict-bd-catalog) =====")
    validate_cmd = [
        python_cmd,
        str(validator),
        str(generated_manifest),
        "--strict-wiki-full",
        "--strict-bd-catalog",
        "--bd-catalog",
        str(catalog),
    ]
    validate_proc = subprocess.run(validate_cmd, check=False)
    if validate_proc.returncode != 0:
        raise RuntimeError(
            f"generated manifest validation failed with exit code {validate_proc.returncode}"
        )

    write_text(manifest_path_file, str(generated_manifest))
    print("SUCCESS: AI manifest generation completed")
    return 0
