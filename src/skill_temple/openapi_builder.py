"""Generate an importable OpenAPI document for Custom GPT Actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import create_app
from .runtime import env_value_from_environment_or_dotenv

DEFAULT_SERVER_URL = "https://skills.example.com"
DEFAULT_OUTPUT_PATH = Path("openapi.json")


def build_openapi(
    *,
    skills_dir: Path | None = None,
    server_url: str | None = None,
    output_path: Path | None = None,
) -> Path:
    resolved_server_url = (
        server_url
        or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
        or DEFAULT_SERVER_URL
    )
    configured_output = env_value_from_environment_or_dotenv("SKILL_TEMPLE_OPENAPI_OUTPUT")
    resolved_output = output_path or Path(configured_output or DEFAULT_OUTPUT_PATH)

    schema = create_app(skills_dir, server_url=resolved_server_url).openapi()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return resolved_output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate openapi.json for importing into Custom GPT Actions."
    )
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    output = build_openapi(
        skills_dir=args.skills_dir,
        server_url=args.server_url,
        output_path=args.output,
    )
    print(output)


if __name__ == "__main__":  # pragma: no cover
    main()
