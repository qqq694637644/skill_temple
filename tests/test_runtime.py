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
from skill_temple.runtime import (
    DEFAULT_MANIFEST_MAX_CHARS,
    DEFAULT_MAX_SKILLS,
    RETRIEVE_INSTRUCTIONS_MAX_CHARS,
    SKILL_CATALOG_MAX_CHARS,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_NAME_MAX_CHARS,
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
    docs: dict[str, str] | None = None,
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
    for relative_path, content in (docs or {}).items():
        path = skill_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_root


class RuntimeTests(unittest.TestCase):
    def test_packaged_example_uses_skill_md_as_the_only_entrypoint(self) -> None:
        runtime = load_runtime()
        result = runtime.list_skills()

        skill = next(item for item in result["skills"] if item["skill_id"] == "idapython")
        self.assertEqual(skill["entrypoint"], "SKILL.md")
        self.assertIn("IDAPython", skill["description"])
        self.assertTrue(skill["content_hash"].startswith("sha256:"))
        root = Path(runtime.skills_dir) / "idapython"
        self.assertTrue((root / "SKILL.md").is_file())
        self.assertFalse((root / "skill.json").exists())
        self.assertFalse((root / "INDEX.md").exists())

    def test_resolve_uses_exact_mentions_and_explicit_hints(self) -> None:
        runtime = load_runtime()
        results = [
            runtime.resolve("@idapython inspect references"),
            runtime.resolve("use $idapython for this task"),
            runtime.resolve("中文任务", hinted_skill_ids=["idapython"]),
        ]

        for result in results:
            self.assertEqual(result["matches"][0]["skill_id"], "idapython")
            self.assertNotIn("confidence", result["matches"][0])
            self.assertEqual(result["available_skills"][0]["skill_id"], "idapython")

    def test_resolve_does_not_make_server_side_semantic_selection(self) -> None:
        runtime = load_runtime()
        for query in [
            "please use this tool",
            "decompile a binary",
            "反编译 main 函数",
            "函数求导怎么做",
            "give me a software patch",
        ]:
            with self.subTest(query=query):
                result = runtime.resolve(query)
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["available_skills"][0]["skill_id"], "idapython")

    def test_retrieve_returns_selected_skill_entrypoint_and_references(self) -> None:
        runtime = load_runtime()
        result = runtime.retrieve("use $idapython", hinted_skill_ids=["idapython"])

        selected = result["selected_skills"][0]
        self.assertEqual(selected["skill_id"], "idapython")
        self.assertEqual(selected["role"], "primary")
        self.assertEqual(selected["source_path"], "SKILL.md")
        self.assertIn("name: idapython", selected["instructions"])
        self.assertIn("docs/idautils.md", selected["referenced_paths"])
        self.assertIn("docs/ida_hexrays.md", selected["referenced_paths"])
        self.assertFalse(selected["truncated"])
        self.assertTrue(result["decision"]["selected"])
        self.assertEqual(result["decision"]["next_action"], "followSkillInstructions")
        self.assertEqual(result["available_skills"], [])
        self.assertFalse(result["catalog_included"])

    def test_retrieve_returns_bounded_catalog_before_selection(self) -> None:
        runtime = load_runtime()
        result = runtime.retrieve("unselected task")

        self.assertEqual(result["selected_skills"], [])
        self.assertEqual(result["decision"]["next_action"], "selectSkillOrAnswer")
        self.assertTrue(result["catalog_included"])
        self.assertEqual(result["available_skill_count"], 1)
        self.assertEqual(result["included_skill_count"], 1)
        self.assertEqual(result["omitted_skill_count"], 0)

    def test_multiple_explicit_skills_auto_chain_and_preserve_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            for skill_id in ["alpha", "beta"]:
                _write_skill(root, skill_id, f"Use for {skill_id} tasks.", f"# {skill_id}")
            runtime = SkillRuntime(root)

            mentioned = runtime.retrieve("@alpha $beta do work")
            hinted = runtime.retrieve("do work", hinted_skill_ids=["alpha", "beta"])

            self.assertEqual(
                [item["skill_id"] for item in mentioned["selected_skills"]],
                ["alpha", "beta"],
            )
            self.assertEqual(len(hinted["selected_skills"]), 2)
            self.assertEqual(mentioned["selected_skills"][0]["role"], "primary")
            self.assertEqual(mentioned["selected_skills"][1]["role"], "secondary")

    def test_more_than_three_explicit_skills_are_not_partially_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            skill_ids = ["alpha", "beta", "gamma", "delta"]
            for skill_id in skill_ids:
                _write_skill(root, skill_id, f"Use for {skill_id} tasks.", f"# {skill_id}")
            runtime = SkillRuntime(root)

            for result in [
                runtime.retrieve("@alpha @beta @gamma @delta"),
                runtime.retrieve("work", hinted_skill_ids=skill_ids),
            ]:
                self.assertEqual(result["selected_skills"], [])
                self.assertEqual(result["explicit_skill_ids"], skill_ids)
                self.assertEqual(
                    result["omitted_explicit_skill_ids"],
                    skill_ids[DEFAULT_MAX_SKILLS:],
                )
                self.assertEqual(result["decision"]["next_action"], "retryWithFewerSkills")

    def test_unknown_mentions_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root, "alpha", "Use for alpha tasks.", "# Alpha")
            runtime = SkillRuntime(root)

            missing = runtime.retrieve("@missing do work")
            mixed = runtime.retrieve("$alpha @missing do work")

            self.assertEqual(missing["unknown_skill_mentions"], ["missing"])
            self.assertIn("unavailable", missing["decision"]["reason"].lower())
            self.assertEqual(mixed["selected_skills"][0]["skill_id"], "alpha")
            self.assertEqual(mixed["unknown_skill_mentions"], ["missing"])

    def test_multiple_skills_share_global_instruction_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            body = "# Large\n\n" + "\n".join("X" * 200 for _ in range(220))
            ids = ["alpha", "beta", "gamma"]
            for skill_id in ids:
                _write_skill(root, skill_id, f"Use for {skill_id}.", body)
            runtime = SkillRuntime(root)

            result = runtime.retrieve("work", hinted_skill_ids=ids, include_debug=True)
            lengths = [len(item["instructions"]) for item in result["selected_skills"]]

            self.assertLessEqual(sum(lengths), RETRIEVE_INSTRUCTIONS_MAX_CHARS)
            self.assertTrue(all(length <= DEFAULT_MANIFEST_MAX_CHARS for length in lengths))
            self.assertTrue(all(item["truncated"] for item in result["selected_skills"]))
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 100_000)

    def test_catalog_has_independent_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            for index in range(140):
                skill_id = f"skill{index:03d}"
                _write_skill(
                    root,
                    skill_id,
                    f"Use {skill_id} for specialized work. " + "D" * 850,
                    f"# {skill_id}",
                )
            runtime = SkillRuntime(root)
            result = runtime.retrieve("discover")
            catalog = json.dumps(
                result["available_skills"], ensure_ascii=False, separators=(",", ":")
            )

            self.assertLessEqual(len(catalog), SKILL_CATALOG_MAX_CHARS)
            self.assertEqual(result["available_skill_count"], 140)
            self.assertEqual(
                result["included_skill_count"] + result["omitted_skill_count"], 140
            )
            self.assertLess(result["included_skill_count"], 140)
            self.assertTrue(result["descriptions_truncated"])
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 100_000)

            client = TestClient(create_app(root))
            response = client.post("/v1/skills/retrieve", json={"query": "discover"})
            self.assertEqual(response.status_code, 200)
            self.assertLess(len(response.text), 100_000)

    def test_entrypoint_hash_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root, "alpha", "Use for alpha tasks.", "# Alpha")
            runtime = SkillRuntime(root)
            with patch(
                "skill_temple.runtime._content_hash",
                side_effect=AssertionError("entrypoint hash should be cached"),
            ):
                catalog = runtime.resolve("discover")
                selected = runtime.retrieve("$alpha")
            self.assertTrue(catalog["available_skills"][0]["content_hash"])
            self.assertEqual(selected["selected_skills"][0]["skill_id"], "alpha")

    def test_search_finds_relevant_reference(self) -> None:
        runtime = load_runtime()
        result = runtime.search("idapython", "ctree_visitor_t cot_call", limit=3)

        self.assertTrue(result["matches"])
        self.assertEqual(result["engine"], "sqlite_fts5_symbol_index")
        self.assertEqual(result["matches"][0]["path"], "docs/ida_hexrays.md")
        self.assertIn("ctree_visitor_t", result["matches"][0]["symbols"])
        self.assertEqual(result["recommended_next_action"], "readSkillContent")

    def test_search_rejects_non_keyword_mode(self) -> None:
        runtime = load_runtime()
        with self.assertRaisesRegex(RuntimeError, "Only keyword search mode"):
            runtime.search("idapython", "ctree", mode="hybrid")

    def test_read_returns_continuation_and_rejects_unsafe_paths(self) -> None:
        runtime = load_runtime()
        result = runtime.read("idapython", "SKILL.md", max_lines=5)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_start_line"], 6)
        for path in ["../README.md", "/etc/passwd", "docs/../../SKILL.md"]:
            with self.subTest(path=path):
                with self.assertRaises(SkillPathError):
                    runtime.read("idapython", path)

    def test_read_does_not_lose_an_oversized_single_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            long_line = "X" * 100
            _write_skill(
                root,
                "demo",
                "Use for long-line tests.",
                "# Demo",
                {"docs/long.txt": long_line},
            )
            runtime = SkillRuntime(root)
            result = runtime.read("demo", "docs/long.txt", max_chars=10)
            self.assertEqual(result["content"], long_line)
            self.assertFalse(result["truncated"])

    def test_runtime_loads_skills_from_cwd_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            root = tmp / "custom_skills"
            _write_skill(root, "demo", "Use for demo tasks.", "# Demo")
            (tmp / ".env").write_text(
                f'SKILL_TEMPLE_SKILLS_DIR = "{root}"\n', encoding="utf-8"
            )
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous)
            self.assertEqual(runtime.skills_dir, root.resolve())
            self.assertEqual(runtime.retrieve("$demo")["selected_skills"][0]["skill_id"], "demo")

    def test_nested_scan_depth_duplicate_and_frontmatter_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            accepted = root.joinpath(*[f"a{index}" for index in range(5)])
            rejected = root.joinpath(*[f"b{index}" for index in range(6)])
            _write_skill(accepted, "accepted", "Use for accepted tasks.", "# Accepted")
            _write_skill(rejected, "rejected", "Use for rejected tasks.", "# Rejected")
            runtime = SkillRuntime(root)
            ids = {item["skill_id"] for item in runtime.list_skills()["skills"]}
            self.assertIn("accepted", ids)
            self.assertNotIn("rejected", ids)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root / "one", "same", "Use for one.", "# One")
            _write_skill(root / "two", "same", "Use for two.", "# Two")
            with self.assertRaisesRegex(SkillRuntimeError, "Duplicate skill name"):
                SkillRuntime(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "n" * (SKILL_NAME_MAX_CHARS + 1),
                "Use for long names.",
                "# Long",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "name exceeds"):
                SkillRuntime(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "long-description",
                "D" * (SKILL_DESCRIPTION_MAX_CHARS + 1),
                "# Long",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "description exceeds"):
                SkillRuntime(root)

    def test_openapi_exposes_only_three_skill_actions(self) -> None:
        schema = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
        }
        self.assertEqual(
            operation_ids,
            {"retrieveSkillContext", "searchSkillDocs", "readSkillContent"},
        )
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                self.assertLessEqual(len(operation.get("description", "")), 300)
                self.assertIs(operation.get("x-openai-isConsequential"), False)

        request_schema = schema["components"]["schemas"]["RetrieveSkillContextRequest"]
        self.assertEqual(
            set(request_schema["properties"]),
            {"query", "hinted_skill_ids", "allow_skill_chaining"},
        )
        response = schema["components"]["schemas"]["RetrieveSkillContextResponse"]
        for field in [
            "available_skills",
            "available_skill_count",
            "omitted_skill_count",
            "explicit_skill_ids",
            "unknown_skill_mentions",
            "omitted_explicit_skill_ids",
            "decision",
        ]:
            self.assertIn(field, response["properties"])

    def test_server_url_and_optional_bearer_auth(self) -> None:
        client = TestClient(create_app())
        response = client.get(
            "/openapi.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "skills.example.com",
            },
        )
        self.assertEqual(response.json()["servers"], [{"url": "https://skills.example.com"}])

        with patch.dict(
            os.environ,
            {"SKILL_TEMPLE_BEARER_TOKEN": "secret-token"},
            clear=False,
        ):
            protected = TestClient(create_app())
            unauthorized = protected.post("/v1/skills/retrieve", json={"query": "discover"})
            authorized = protected.post(
                "/v1/skills/retrieve",
                json={"query": "discover"},
                headers={"Authorization": "Bearer secret-token"},
            )
            schema = protected.get("/openapi.json").json()

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertIn("BearerAuth", schema["components"]["securitySchemes"])

    def test_http_endpoints_console_and_structured_errors(self) -> None:
        client = TestClient(create_app())

        discovery = client.post("/v1/skills/retrieve", json={"query": "discover"})
        selected = client.post(
            "/v1/skills/retrieve",
            json={"query": "task", "hinted_skill_ids": ["idapython"]},
        )
        read = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "SKILL.md", "max_lines": 5},
        )
        search = client.post(
            "/v1/skills/search",
            json={"skill_id": "idapython", "query": "ctree_visitor_t"},
        )
        console = client.get("/console")
        debug = client.post(
            "/console/retrieve",
            json={
                "query": "$idapython",
                "hinted_skill_ids": ["idapython"],
                "include_debug": True,
            },
        )
        bad_hint = client.post(
            "/v1/skills/retrieve",
            json={"query": "task", "hinted_skill_ids": ["missing"]},
        )
        unsafe = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "../README.md"},
        )

        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["selected_skills"][0]["skill_id"], "idapython")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(search.status_code, 200)
        self.assertIn("Skill Temple Console", console.text)
        self.assertIn("debug", debug.json())
        self.assertEqual(bad_hint.status_code, 404)
        self.assertEqual(bad_hint.json()["detail"]["error"]["code"], "skill_not_found")
        self.assertEqual(unsafe.status_code, 404)
        self.assertEqual(unsafe.json()["detail"]["error"]["code"], "unsafe_or_missing_path")

    def test_eval_file_passes(self) -> None:
        report = evaluate_file(Path("evals/skill_queries.jsonl"))
        self.assertEqual(report["failed"], 0)
        self.assertGreaterEqual(report["passed"], 2)


if __name__ == "__main__":
    unittest.main()
