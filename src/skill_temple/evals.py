"""Deterministic eval runner for the Skill Temple GPT Actions runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runtime import load_runtime

PASS = "pass"
FAIL = "fail"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            case = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        case.setdefault("id", f"case-{line_number}")
        cases.append(case)
    return cases


def evaluate_case(case: dict[str, Any], skills_dir: Path | None = None) -> dict[str, Any]:
    runtime = load_runtime(skills_dir)
    query = str(case["query"])
    expected_skill = str(case["expected_skill"])
    hinted_skill_ids = [expected_skill] if case.get("use_hint", True) else []

    retrieve = runtime.retrieve(query, hinted_skill_ids=hinted_skill_ids)
    selected_skills = retrieve.get("selected_skills", [])
    selected_skill_ids = [skill["skill_id"] for skill in selected_skills]
    top_skill_ok = bool(selected_skill_ids) and selected_skill_ids[0] == expected_skill

    referenced_paths = {
        path
        for skill in selected_skills
        for path in skill.get("referenced_paths", [])
    }
    expected_paths = [str(path) for path in case.get("expected_paths", [])]
    missing_paths = [path for path in expected_paths if path not in referenced_paths]

    search = runtime.search(
        expected_skill,
        query,
        limit=max(5, len(expected_paths) or 5),
    )
    search_symbols = {
        symbol
        for match in search.get("matches", [])
        for symbol in (
            match.get("rank_features", {}).get("symbol_matches", [])
            + match.get("rank_features", {}).get("document_symbols", [])
        )
    }
    expected_symbols = {str(symbol) for symbol in case.get("expected_symbols", [])}
    missing_symbols = sorted(expected_symbols - search_symbols)

    ok = top_skill_ok and not missing_paths and not missing_symbols
    return {
        "id": case["id"],
        "status": PASS if ok else FAIL,
        "query": query,
        "expected_skill": expected_skill,
        "selected_skill_ids": selected_skill_ids,
        "top_skill_ok": top_skill_ok,
        "expected_paths": expected_paths,
        "referenced_paths": sorted(referenced_paths),
        "missing_paths": missing_paths,
        "expected_symbols": sorted(expected_symbols),
        "search_symbols": sorted(search_symbols),
        "missing_symbols": missing_symbols,
    }


def evaluate_file(path: Path, skills_dir: Path | None = None) -> dict[str, Any]:
    cases = load_cases(path)
    results = [evaluate_case(case, skills_dir=skills_dir) for case in cases]
    failed = [result for result in results if result["status"] != PASS]
    return {
        "case_count": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Skill Temple retrieval quality.")
    parser.add_argument("cases", type=Path, help="Path to a JSONL eval file.")
    parser.add_argument("--skills-dir", type=Path, default=None)
    args = parser.parse_args()

    report = evaluate_file(args.cases, skills_dir=args.skills_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
