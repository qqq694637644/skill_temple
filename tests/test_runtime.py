from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.evals import evaluate_file
from skill_temple.openapi_builder import build_openapi
from skill_temple.prompt_builder import build_instructions, render_catalog
from skill_temple.runtime import (
    SkillNotFoundError,
    SkillPathError,
    SkillRuntime,
    SkillRuntimeError,
    load_runtime,
)


def _write_skill(
    skills_root: Path,
    skill_id: str,
    description: str,
    body: str,
    files: dict[str, str] | None = None,
) -> Path:
    skill_root = skills_root / skill_id
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {skill_id}",
                f"description: {description}",
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    for relative_path, content in (files or {}).items():
        path = skill_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_root


class RuntimeTests(unittest.TestCase):
    def test_packaged_example_is_discovered(self) -> None:
        runtime = load_runtime()
        skills = runtime.list_skills()["skills"]
        example = next(item for item in skills if item["skill_id"] == "idapython")
        self.assertEqual(example["entrypoint"], "SKILL.md")
        self.assertTrue(example["content_hash"].startswith("sha256:"))

    def test_load_skills_returns_full_codex_style_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "api-review",
                "Review APIs.",
                "# Review\n\nRead `docs/openapi.md`.",
                {"docs/openapi.md": "# OpenAPI\n\nReference."},
            )
            runtime = SkillRuntime(root)

            result = runtime.load_skills(["api-review"])
            loaded = result["skills"][0]

            self.assertEqual(result["loaded_skill_ids"], ["api-review"])
            self.assertIn("<skill>", loaded["content"])
            self.assertIn("<name>api-review</name>", loaded["content"])
            self.assertIn("<path>api-review/SKILL.md</path>", loaded["content"])
            self.assertIn("description: Review APIs.", loaded["content"])
            self.assertIn("docs/openapi.md", loaded["referenced_paths"])

    def test_load_skills_preserves_order_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root, "alpha", "Alpha tasks.", "# Alpha")
            _write_skill(root, "beta", "Beta tasks.", "# Beta")
            runtime = SkillRuntime(root)

            result = runtime.load_skills(["beta", "alpha", "beta"])
            self.assertEqual(result["loaded_skill_ids"], ["beta", "alpha"])

    def test_unknown_skill_is_an_error(self) -> None:
        runtime = load_runtime()
        with self.assertRaises(SkillNotFoundError):
            runtime.load_skills(["missing"])

    def test_read_returns_continuation_and_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "demo",
                "Demo tasks.",
                "# Demo",
                {"docs/reference.md": "one\ntwo\nthree\nfour\n"},
            )
            runtime = SkillRuntime(root)

            first = runtime.read("demo", "docs/reference.md", start_line=1, max_lines=2)
            second = runtime.read(
                "demo",
                "docs/reference.md",
                start_line=first["next_start_line"],
                max_lines=2,
            )

            self.assertEqual(first["content"], "one\ntwo")
            self.assertTrue(first["truncated"])
            self.assertEqual(first["next_start_line"], 3)
            self.assertEqual(second["content"], "three\nfour")
            self.assertFalse(second["truncated"])

            for path in ["../README.md", "/etc/passwd", "docs/../../SKILL.md"]:
                with self.subTest(path=path):
                    with self.assertRaises(SkillPathError):
                        runtime.read("demo", path)

    def test_runtime_uses_dotenv_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            skills = temp / "custom-skills"
            _write_skill(skills, "demo", "Demo tasks.", "# Demo")
            (temp / ".env").write_text(
                f'SKILL_TEMPLE_SKILLS_DIR="{skills}"\n', encoding="utf-8"
            )
            previous = Path.cwd()
            try:
                os.chdir(temp)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous)
            self.assertEqual(runtime.skills_dir, skills.resolve())

    def test_invalid_or_duplicate_skill_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root / "one", "same", "One.", "# One")
            _write_skill(root / "two", "same", "Two.", "# Two")
            with self.assertRaisesRegex(SkillRuntimeError, "Duplicate Skill name"):
                SkillRuntime(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            skill_root = root / "bad"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\nname: bad name\ndescription: Bad.\n---\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(SkillRuntimeError, "Invalid Skill name"):
                SkillRuntime(root)

    def test_prompt_builder_includes_metadata_not_skill_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "skills"
            _write_skill(root, "alpha", "Use for alpha work.", "SECRET BODY")
            _write_skill(root, "beta", "Use for beta work.", "ANOTHER BODY")
            runtime = SkillRuntime(root)

            catalog = render_catalog(runtime)
            self.assertIn("alpha: Use for alpha work. (skill_id: alpha)", catalog)
            self.assertIn("beta: Use for beta work. (skill_id: beta)", catalog)
            self.assertNotIn("SECRET BODY", catalog)

            template = temp / "template.md"
            output = temp / "dist" / "instructions.md"
            template.write_text("Before\n{{SKILL_CATALOG}}\nAfter\n", encoding="utf-8")
            built = build_instructions(
                runtime=runtime,
                template_path=template,
                output_path=output,
            )
            text = built.read_text(encoding="utf-8")
            self.assertNotIn("{{SKILL_CATALOG}}", text)
            self.assertIn("skill_id: alpha", text)
            self.assertNotIn("SECRET BODY", text)

    def test_prompt_builder_requires_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "skills"
            _write_skill(root, "demo", "Demo tasks.", "# Demo")
            template = temp / "template.md"
            template.write_text("No catalog here", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SKILL_CATALOG"):
                build_instructions(
                    runtime=SkillRuntime(root),
                    template_path=template,
                    output_path=temp / "out.md",
                )

    def test_openapi_exposes_only_skill_loading_and_workspace_actions(self) -> None:
        schema = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
        }
        self.assertEqual(
            operation_ids,
            {
                "loadSkills",
                "readSkillContent",
                "workspaceCommand",
                "workspaceInspect",
                "workspaceSearch",
                "workspaceReadFiles",
                "workspaceWriteFile",
                "workspaceApplyPatch",
            },
        )
        self.assertNotIn("/v1/skills/retrieve", schema["paths"])
        self.assertNotIn("/v1/skills/search", schema["paths"])
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                self.assertIs(operation.get("x-openai-isConsequential"), False)

    def test_server_url_and_http_endpoints(self) -> None:
        client = TestClient(create_app())
        schema = client.get(
            "/openapi.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "skills.example.com",
            },
        )
        loaded = client.post("/v1/skills/load", json={"skill_ids": ["idapython"]})
        read = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "SKILL.md", "max_lines": 5},
        )
        missing = client.post("/v1/skills/load", json={"skill_ids": ["missing"]})
        unsafe = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "../README.md"},
        )

        self.assertEqual(schema.json()["servers"], [{"url": "https://skills.example.com"}])
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["loaded_skill_ids"], ["idapython"])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["error"]["code"], "skill_not_found")
        self.assertEqual(unsafe.status_code, 404)
        self.assertEqual(unsafe.json()["detail"]["error"]["code"], "unsafe_or_missing_path")

    def test_optional_bearer_auth_and_debug_console(self) -> None:
        with patch.dict(
            os.environ,
            {"SKILL_TEMPLE_BEARER_TOKEN": "secret-token"},
            clear=False,
        ):
            client = TestClient(create_app())
            console = client.get("/console")
            unauthorized = client.post(
                "/v1/skills/load", json={"skill_ids": ["idapython"]}
            )
            console_unauthorized = client.post(
                "/console/load", json={"skill_ids": ["idapython"]}
            )
            authorized = client.post(
                "/v1/skills/load",
                json={"skill_ids": ["idapython"]},
                headers={"Authorization": "Bearer secret-token"},
            )
            console_authorized = client.post(
                "/console/read",
                json={"skill_id": "idapython", "path": "SKILL.md"},
                headers={"Authorization": "Bearer secret-token"},
            )
            schema = client.get("/openapi.json").json()

        self.assertEqual(console.status_code, 200)
        self.assertIn("Skill Temple Retrieval Console", console.text)
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(console_unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(console_authorized.status_code, 200)
        self.assertIn("BearerAuth", schema["components"]["securitySchemes"])
        self.assertEqual(
            schema["paths"]["/v1/skills/load"]["post"]["security"],
            [{"BearerAuth": []}],
        )

    def test_openapi_builder_uses_configured_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "openapi.json"
            with patch.dict(
                os.environ,
                {
                    "SKILL_TEMPLE_SERVER_URL": "https://skills.example.com",
                    "SKILL_TEMPLE_BEARER_TOKEN": "secret-token",
                },
                clear=False,
            ):
                built = build_openapi(output_path=output)
            schema = json.loads(built.read_text(encoding="utf-8"))

        self.assertEqual(schema["servers"], [{"url": "https://skills.example.com"}])
        self.assertIn("BearerAuth", schema["components"]["securitySchemes"])
        self.assertIn("/v1/skills/load", schema["paths"])
        self.assertNotIn("/console", schema["paths"])

    def test_skill_eval_file_passes(self) -> None:
        report = evaluate_file(Path("evals/skill_queries.jsonl"))
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 2)


if __name__ == "__main__":
    unittest.main()
