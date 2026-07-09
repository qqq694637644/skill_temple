from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from skill_temple.app import create_app
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

    def test_retrieve_returns_manifest_summary_and_docs(self) -> None:
        runtime = load_runtime()

        result = runtime.retrieve(
            "@idapython write a script to find xrefs to strcpy",
            hinted_skill_ids=["idapython"],
        )

        self.assertTrue(result["selected_skills"])
        selected = result["selected_skills"][0]
        self.assertEqual(selected["skill_id"], "idapython")
        self.assertTrue(selected["manifest_summary"]["critical_rules"])
        self.assertTrue(selected["manifest_summary"]["module_router"])
        self.assertTrue(selected["retrieved_docs"])
        self.assertEqual(result["recommended_next_action"], "answer")

    def test_search_returns_relevant_doc_excerpt(self) -> None:
        runtime = load_runtime()

        result = runtime.search("idapython", "ctree_visitor_t cot_call", limit=3)

        self.assertTrue(result["matches"])
        self.assertEqual(result["mode"], "keyword")
        self.assertEqual(result["engine"], "sqlite_fts5_symbol_index")
        self.assertEqual(result["matches"][0]["path"], "docs/ida_hexrays.md")
        self.assertIn("ctree", result["matches"][0]["excerpt"].lower())
        self.assertIn("ctree_visitor_t", result["matches"][0]["symbols"])

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

    def test_default_openapi_exposes_only_task_operations(self) -> None:
        app = create_app()

        operation_ids = {
            operation["operationId"]
            for path_item in app.openapi()["paths"].values()
            for operation in path_item.values()
        }

        self.assertEqual(
            operation_ids,
            {"retrieveSkillContext", "searchSkillDocs", "readSkillContent"},
        )

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
                "mode": "keyword",
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
                result["selected_skills"][0]["retrieved_docs"][0]["path"],
                "docs/demo.md",
            )


if __name__ == "__main__":
    unittest.main()
