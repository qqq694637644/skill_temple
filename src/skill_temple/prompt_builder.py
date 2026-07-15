"""Build final Custom GPT Instructions with the current Skill catalog."""

from __future__ import annotations

import argparse
from pathlib import Path

from .runtime import SkillRuntime, load_runtime

CATALOG_PLACEHOLDER = "{{SKILL_CATALOG}}"
DEFAULT_TEMPLATE_PATH = Path("GPT_ACTION_PROMPT.md")
DEFAULT_OUTPUT_PATH = Path("dist/GPT_INSTRUCTIONS.md")


def render_catalog(runtime: SkillRuntime) -> str:
    lines: list[str] = []
    for skill in runtime.list_skills()["skills"]:
        description = " ".join(str(skill["description"]).split())
        lines.append(
            f'- {skill["name"]}: {description} (skill_id: {skill["skill_id"]})'
        )
    return "\n".join(lines) if lines else "- No Skills are currently installed."


def build_instructions(
    *,
    runtime: SkillRuntime,
    template_path: Path,
    output_path: Path,
) -> Path:
    template = template_path.read_text(encoding="utf-8")
    if CATALOG_PLACEHOLDER not in template:
        raise ValueError(f"Template is missing {CATALOG_PLACEHOLDER}")
    rendered = template.replace(CATALOG_PLACEHOLDER, render_catalog(runtime))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8", newline="\n")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile Skill metadata into final Custom GPT Instructions."
    )
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    output = build_instructions(
        runtime=load_runtime(args.skills_dir),
        template_path=args.template,
        output_path=args.output,
    )
    print(output)


if __name__ == "__main__":  # pragma: no cover
    main()
