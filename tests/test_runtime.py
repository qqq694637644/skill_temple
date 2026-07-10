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
from skill_temple.runtime import SkillPathError, SkillRuntime, load_runtime


class RuntimeTests(unittest.TestCase):
    def test_packaged_example_runtime_lists_idapython(self) -> None:
        runtime = load_runtime()
        result = runtime.list_skills()

        skill_ids = {item["skill_id"] for item in result["skills"]}
        self.assertIn("idapython", skill_ids)

    def test_resolve_uses_alias_and_trigger_terms(self) -> None:
        runtime = load_runtime()

        result = runtime.resolve("@idapython write a Hex-Rays ctree visitor")

        self.assertTrue(result["matches"])
        self.assertEqual(result["matches"][0]["skill_id"], "idapython")
        self.assertGreater(result["matches"][0]["confidence"], 0.5)

    def test_retrieve_returns_compact_decision_packet_by_default(self) -> None:
        runtime = load_runtime()

        result = runtime.retrieve(
            "@idapython write a script to find xrefs to strcpy",
            hinted_skill_ids=["idapython"],
        )

        self.assertTrue(result["selected_skills"])
        selected = result["selected_skills"][0]
        self.assertEqual(selected["skill_id"], "idapython")
        self.assertIn("reverse_engineering", selected["capability_tags"])
        self.assertEqual(selected["role"], "primary")
        self.assertTrue(selected["operating_rules"])
        self.assertTrue(selected["evidence"])
        self.assertTrue(selected["response_contract"]["expected_output"])
        self.assertIn("Mention required imports", selected["response_contract"]["must_include"][1])
        self.assertNotEqual(
            selected["operating_rules"][:3],
            selected["response_contract"]["must_include"][:3],
        )
        self.assertNotIn("evidence_paths", selected["response_contract"])
        self.assertTrue(selected["validation_guidance"])
        self.assertNotIn("manifest_summary", selected)
        self.assertNotIn("retrieved_docs", selected)
        self.assertNotIn("rank_features", selected["evidence"][0])
        self.assertNotIn("debug", result)
        self.assertTrue(result["decision"]["ready"])
        self.assertTrue(result["decision"]["stop"])
        self.assertEqual(result["decision"]["next_action"], "answer")
        self.assertGreaterEqual(result["retrieval_budget"]["used_docs"], 1)
        self.assertNotIn("used_chars", result["retrieval_budget"])
        self.assertNotIn("fallback_queries", result)

    def test_retrieve_debug_includes_diagnostics(self) -> None:
        runtime = load_runtime()

        result = runtime.retrieve(
            "@idapython write a script to find xrefs to strcpy",
            hinted_skill_ids=["idapython"],
            include_debug=True,
        )

        selected = result["selected_skills"][0]
        self.assertIn("debug", selected)
        self.assertTrue(selected["debug"]["manifest_summary"]["critical_rules"])
        self.assertTrue(selected["debug"]["manifest_summary"]["module_router"])
        self.assertTrue(selected["debug"]["retrieved_docs"])
        self.assertIn("rank_features", selected["evidence"][0])
        self.assertIn("debug", result)
        self.assertIn("used_chars", result["debug"]["retrieval_budget"])
        self.assertIn("fallback_queries", result["debug"])

    def test_search_returns_relevant_doc_excerpt(self) -> None:
        runtime = load_runtime()

        result = runtime.search("idapython", "ctree_visitor_t cot_call", limit=3)

        self.assertTrue(result["matches"])
        self.assertEqual(result["mode"], "keyword")
        self.assertEqual(result["engine"], "sqlite_fts5_symbol_index")
        self.assertEqual(result["matches"][0]["path"], "docs/ida_hexrays.md")
        self.assertIn("ctree", result["matches"][0]["excerpt"].lower())
        self.assertIn("ctree_visitor_t", result["matches"][0]["symbols"])
        self.assertIn("rank_features", result["matches"][0])
        self.assertIn("why_relevant", result["matches"][0])

    def test_search_rejects_non_keyword_mode(self) -> None:
        runtime = load_runtime()

        with self.assertRaisesRegex(RuntimeError, "Only keyword search mode"):
            runtime.search("idapython", "ctree visitor", mode="hybrid")

    def test_read_file_by_safe_path(self) -> None:
        runtime = load_runtime()

        result = runtime.read("idapython", "SKILL.md", start_line=1, max_lines=5)

        self.assertEqual(result["skill_id"], "idapython")
        self.assertEqual(result["path"], "SKILL.md")
        self.assertEqual(result["start_line"], 1)
        self.assertIn("name: idapython", result["content"])

    def test_read_rejects_unsafe_paths(self) -> None:
        runtime = load_runtime()

        for path in ["../pyproject.toml", "/etc/passwd", "docs/../../SKILL.md"]:
            with self.subTest(path=path):
                with self.assertRaises(SkillPathError):
                    runtime.read("idapython", path)

    def test_runtime_can_load_skills_dir_from_cwd_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skills_root = tmp_path / "custom_skills"
            skill_root = skills_root / "demo"
            docs_root = skill_root / "docs"
            docs_root.mkdir(parents=True)
            (skill_root / "skill.json").write_text(
                json.dumps(
                    {
                        "skill_id": "demo",
                        "name": "demo",
                        "version": "1",
                        "description": "Demo skill loaded from cwd .env.",
                        "aliases": ["@demo"],
                        "activation": {"trigger_terms": ["dotenv-demo"]},
                        "entrypoint": "SKILL.md",
                        "docs": [{"path": "docs/demo.md", "title": "demo"}],
                    }
                ),
                encoding="utf-8",
            )
            (skill_root / "SKILL.md").write_text(
                "# Demo\n\n## Critical Rules\n\n1. Use dotenv configuration.\n",
                encoding="utf-8",
            )
            (docs_root / "demo.md").write_text(
                "# Demo docs\n\ndotenv-demo content.\n",
                encoding="utf-8",
            )
            (tmp_path / ".env").write_text(
                f'SKILL_TEMPLE_SKILLS_DIR = "{skills_root}"\n',
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(runtime.skills_dir, skills_root.resolve())
            result = runtime.retrieve("@demo dotenv-demo")
            self.assertEqual(result["selected_skills"][0]["skill_id"], "demo")

    def test_default_openapi_exposes_only_task_operations(self) -> None:
        app = create_app()
        schema = app.openapi()

        operation_ids = {
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
        }

        self.assertEqual(
            operation_ids,
            {"retrieveSkillContext", "searchSkillDocs", "readSkillContent"},
        )
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                with self.subTest(path=path, method=method, operation=operation["operationId"]):
                    self.assertLessEqual(len(operation.get("description", "")), 300)
                    self.assertIs(operation.get("x-openai-isConsequential"), False)

        retrieve_schema = schema["components"]["schemas"]["RetrieveSkillContextRequest"]
        retrieve_fields = set(retrieve_schema["properties"])
        self.assertEqual(
            retrieve_fields,
            {"query", "hinted_skill_ids", "max_docs", "allow_skill_chaining"},
        )
        search_schema = schema["components"]["schemas"]["SearchSkillDocsRequest"]
        self.assertEqual(set(search_schema["properties"]), {"skill_id", "query", "paths", "limit"})
        read_schema = schema["components"]["schemas"]["ReadSkillContentRequest"]
        self.assertEqual(
            set(read_schema["properties"]),
            {"skill_id", "path", "start_line", "max_lines"},
        )

        retrieve_response = schema["components"]["schemas"]["RetrieveSkillContextResponse"]
        self.assertIn("selected_skills", retrieve_response["properties"])
        self.assertIn("decision", retrieve_response["properties"])

    def test_openapi_json_infers_server_url_for_gpt_action_imports(self) -> None:
        client = TestClient(create_app())

        response = client.get(
            "/openapi.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "skills.example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["servers"], [{"url": "https://skills.example.com"}])
        self.assertIn("/v1/skills/retrieve", schema["paths"])

    def test_configured_server_url_is_published_in_openapi_schema(self) -> None:
        app = create_app(server_url="https://skills.example.com/api/")

        self.assertEqual(app.openapi()["servers"], [{"url": "https://skills.example.com/api"}])

    def test_http_endpoints_work_through_testclient(self) -> None:
        client = TestClient(create_app())

        read_response = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "SKILL.md", "max_lines": 5},
        )
        self.assertEqual(read_response.status_code, 200)
        self.assertIn("name: idapython", read_response.json()["content"])

        search_response = client.post(
            "/v1/skills/search",
            json={
                "skill_id": "idapython",
                "query": "ctree_visitor_t cot_call",
            },
        )
        self.assertEqual(search_response.status_code, 200)
        search_body = search_response.json()
        self.assertEqual(search_body["engine"], "sqlite_fts5_symbol_index")
        self.assertEqual(search_body["matches"][0]["path"], "docs/ida_hexrays.md")

        retrieve_response = client.post(
            "/v1/skills/retrieve",
            json={
                "query": "@idapython write a script to find xrefs to strcpy",
                "hinted_skill_ids": ["idapython"],
            },
        )
        self.assertEqual(retrieve_response.status_code, 200)
        retrieve_body = retrieve_response.json()
        self.assertEqual(retrieve_body["selected_skills"][0]["skill_id"], "idapython")
        self.assertTrue(retrieve_body["decision"]["ready"])
        self.assertNotIn("debug", retrieve_body)

        public_debug_response = client.post(
            "/v1/skills/retrieve",
            json={
                "query": "@idapython write a script to find xrefs to strcpy",
                "hinted_skill_ids": ["idapython"],
                "include_debug": True,
            },
        )
        self.assertEqual(public_debug_response.status_code, 422)

    def test_hidden_console_can_request_debug_output(self) -> None:
        client = TestClient(create_app())

        html_response = client.get("/console")
        self.assertEqual(html_response.status_code, 200)
        self.assertIn("Skill Temple Console", html_response.text)

        debug_response = client.post(
            "/console/retrieve",
            json={
                "query": "@idapython write a script to find xrefs to strcpy",
                "hinted_skill_ids": ["idapython"],
                "include_debug": True,
            },
        )

        self.assertEqual(debug_response.status_code, 200)
        body = debug_response.json()
        self.assertIn("debug", body)
        self.assertIn("debug", body["selected_skills"][0])

    def test_http_expected_errors_are_structured(self) -> None:
        client = TestClient(create_app())

        response = client.post(
            "/v1/skills/read",
            json={"skill_id": "missing", "path": "SKILL.md"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error"]["code"], "skill_not_found")

        path_response = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "../README.md"},
        )

        self.assertEqual(path_response.status_code, 404)
        self.assertEqual(
            path_response.json()["detail"]["error"]["code"],
            "unsafe_or_missing_path",
        )

        retrieve_response = client.post(
            "/v1/skills/retrieve",
            json={"query": "@missing do something", "hinted_skill_ids": ["missing"]},
        )

        self.assertEqual(retrieve_response.status_code, 404)
        self.assertEqual(
            retrieve_response.json()["detail"]["error"]["code"],
            "skill_not_found",
        )

    def test_eval_file_passes_packaged_skill_queries(self) -> None:
        report = evaluate_file(Path("evals/skill_queries.jsonl"))

        self.assertEqual(report["failed"], 0)
        self.assertGreaterEqual(report["passed"], 2)

    def test_runtime_can_load_external_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skill_root = tmp_path / "skills" / "demo"
            docs_root = skill_root / "docs"
            docs_root.mkdir(parents=True)
            (skill_root / "skill.json").write_text(
                json.dumps(
                    {
                        "skill_id": "demo",
                        "name": "demo",
                        "version": "1",
                        "description": "Demo skill for unittest.",
                        "aliases": ["@demo"],
                        "activation": {"trigger_terms": ["unittest-demo"]},
                        "entrypoint": "SKILL.md",
                        "docs": [{"path": "docs/demo.md", "title": "demo"}],
                    }
                ),
                encoding="utf-8",
            )
            (skill_root / "SKILL.md").write_text(
                "# Demo\n\n## Critical Rules\n\n1. Return deterministic examples.\n",
                encoding="utf-8",
            )
            (docs_root / "demo.md").write_text(
                "# Demo docs\n\nunittest-demo explains local skill loading.\n",
                encoding="utf-8",
            )

            runtime = SkillRuntime(tmp_path / "skills")
            result = runtime.retrieve("@demo unittest-demo task", hinted_skill_ids=["demo"])

            self.assertEqual(result["selected_skills"][0]["skill_id"], "demo")
            self.assertEqual(
                result["selected_skills"][0]["evidence"][0]["path"],
                "docs/demo.md",
            )

    def test_runtime_can_return_multiple_skills_when_chaining_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skills_root = tmp_path / "skills"
            for skill_id, trigger in [("alpha", "shared-task"), ("beta", "shared-task")]:
                skill_root = skills_root / skill_id
                docs_root = skill_root / "docs"
                docs_root.mkdir(parents=True)
                (skill_root / "skill.json").write_text(
                    json.dumps(
                        {
                            "skill_id": skill_id,
                            "name": skill_id,
                            "version": "1",
                            "description": f"{skill_id} skill for shared task.",
                            "aliases": [f"@{skill_id}"],
                            "activation": {"trigger_terms": [trigger]},
                            "entrypoint": "SKILL.md",
                            "docs": [{"path": "docs/shared.md", "title": "shared"}],
                            "capability_tags": [skill_id, "shared"],
                            "can_chain_with": ["beta" if skill_id == "alpha" else "alpha"],
                        }
                    ),
                    encoding="utf-8",
                )
                (skill_root / "SKILL.md").write_text(
                    f"# {skill_id}\n\n## Critical Rules\n\n1. Use {skill_id}.\n",
                    encoding="utf-8",
                )
                (docs_root / "shared.md").write_text(
                    f"# Shared\n\n{trigger} content for {skill_id}.\n",
                    encoding="utf-8",
                )

            runtime = SkillRuntime(skills_root)
            single = runtime.retrieve("shared-task")
            chained = runtime.retrieve(
                "shared-task",
                max_skills=2,
                allow_skill_chaining=True,
                include_debug=True,
            )

            self.assertEqual(len(single["selected_skills"]), 1)
            self.assertEqual(len(chained["selected_skills"]), 2)
            self.assertEqual(chained["selected_skills"][0]["role"], "primary")
            self.assertEqual(chained["selected_skills"][1]["role"], "secondary")
            self.assertTrue(chained["debug"]["composition_plan"]["enabled"])

    def test_skill_chaining_respects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skills_root = tmp_path / "skills"
            for skill_id, conflict in [("alpha", "beta"), ("beta", "alpha")]:
                skill_root = skills_root / skill_id
                docs_root = skill_root / "docs"
                docs_root.mkdir(parents=True)
                (skill_root / "skill.json").write_text(
                    json.dumps(
                        {
                            "skill_id": skill_id,
                            "name": skill_id,
                            "version": "1",
                            "description": f"{skill_id} skill for shared task.",
                            "aliases": [f"@{skill_id}"],
                            "activation": {"trigger_terms": ["shared-task"]},
                            "entrypoint": "SKILL.md",
                            "docs": [{"path": "docs/shared.md", "title": "shared"}],
                            "conflicts_with": [conflict],
                        }
                    ),
                    encoding="utf-8",
                )
                (skill_root / "SKILL.md").write_text(
                    f"# {skill_id}\n\n## Critical Rules\n\n1. Use {skill_id}.\n",
                    encoding="utf-8",
                )
                (docs_root / "shared.md").write_text(
                    f"# Shared\n\nshared-task content for {skill_id}.\n",
                    encoding="utf-8",
                )

            runtime = SkillRuntime(skills_root)
            chained = runtime.retrieve("shared-task", max_skills=2, allow_skill_chaining=True)

            self.assertEqual(len(chained["selected_skills"]), 1)

    def test_skill_chaining_respects_can_chain_with_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skills_root = tmp_path / "skills"
            for skill_id in ["alpha", "beta", "gamma"]:
                skill_root = skills_root / skill_id
                docs_root = skill_root / "docs"
                docs_root.mkdir(parents=True)
                metadata = {
                    "skill_id": skill_id,
                    "name": skill_id,
                    "version": "1",
                    "description": f"{skill_id} skill for shared task.",
                    "aliases": [f"@{skill_id}"],
                    "activation": {"trigger_terms": ["shared-task"]},
                    "entrypoint": "SKILL.md",
                    "docs": [{"path": "docs/shared.md", "title": "shared"}],
                }
                if skill_id == "alpha":
                    metadata["can_chain_with"] = ["gamma"]
                if skill_id == "gamma":
                    metadata["can_chain_with"] = ["alpha"]

                (skill_root / "skill.json").write_text(json.dumps(metadata), encoding="utf-8")
                (skill_root / "SKILL.md").write_text(
                    f"# {skill_id}\n\n## Critical Rules\n\n1. Use {skill_id}.\n",
                    encoding="utf-8",
                )
                (docs_root / "shared.md").write_text(
                    f"# Shared\n\nshared-task content for {skill_id}.\n",
                    encoding="utf-8",
                )

            runtime = SkillRuntime(skills_root)
            chained = runtime.retrieve("shared-task", max_skills=3, allow_skill_chaining=True)

            self.assertEqual(
                [skill["skill_id"] for skill in chained["selected_skills"]],
                ["alpha", "gamma"],
            )


if __name__ == "__main__":
    unittest.main()
