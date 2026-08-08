# Assisted by Claude Opus 4.6
"""CLI entry point and orchestration."""

import argparse
import json
from pathlib import Path

from jinja2 import TemplateError
from pydantic import ValidationError

from .common import create_jinja_env
from .step_generator import generate_steps, load_steps_file
from .writers.manual import write_manual
from .writers.tekton import write_tekton


def main() -> None:
    parser = argparse.ArgumentParser(description="UAT Test Harness Generator")
    parser.add_argument("--test-suite")
    parser.add_argument("--test-lib")
    parser.add_argument("--cluster")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--run-id", default="manual-run")
    parser.add_argument("--output", default="build")
    parser.add_argument("--scripts-dir", default="scripts")
    parser.add_argument("--templates-dir", default="templates")
    parser.add_argument(
        "--steps",
        default=None,
        help="Generate from a steps.json file instead of config",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)

    templates_dir = Path(args.templates_dir)
    if not templates_dir.is_dir():
        print(f"Error: templates directory not found: {args.templates_dir}")
        raise SystemExit(1)
    try:
        jinja_env = create_jinja_env(templates_dir)
    except (OSError, TemplateError) as e:
        print(f"Error initializing template engine: {e}")
        raise SystemExit(1)

    if args.steps:
        try:
            all_steps, tc, cs = load_steps_file(Path(args.steps))
        except FileNotFoundError:
            print(f"Error: steps file not found: {args.steps}")
            raise SystemExit(1)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            print(f"Error loading steps file: {e}")
            raise SystemExit(1)
        print(f"Loaded steps from {args.steps}")
        print(f"Namespace: {cs.namespace}")
    else:
        if not args.test_suite or not args.test_lib or not args.cluster:
            print(
                "Error: --test-suite, --test-lib, and --cluster are required "
                "when --steps is not provided"
            )
            raise SystemExit(1)

        all_steps, tc, cs = generate_steps(
            config_path=args.config,
            test_suite_path=args.test_suite,
            test_lib_path=args.test_lib,
            cluster_path=args.cluster,
            scripts_dir=args.scripts_dir,
            output_dir=output_dir,
            jinja_env=jinja_env,
        )

    try:
        write_manual(all_steps, output_dir, args.run_id, jinja_env, cs.namespace)
    except (OSError, TemplateError, ValueError) as e:
        print(f"Error writing manual output: {e}")
        raise SystemExit(1)

    try:
        write_tekton(all_steps, tc, cs, jinja_env, output_dir)
    except (OSError, TemplateError, ValueError) as e:
        print(f"Error writing Tekton output: {e}")
        raise SystemExit(1)

    print(f"\nOutput written to {output_dir}/")
