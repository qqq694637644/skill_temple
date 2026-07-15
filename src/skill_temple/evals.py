"""Deterministic checks for compiled Skill catalogs and exact Skill loading."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .prompt_builder import render_catalog
from .runtime import SkillPathError, load_runtime

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


def _read_all(runtime: Any, skill_id: str, path: str) -> str:
    chunks: list[str] = []
    start_line = 1
    while True:
        result = runtime.read(skill_id, path, start_line=start_line, max_lines=10_000)
        chunks.append(result["content"])
        if not result["truncated"]:
            return "\n".join(chunks)
        start_line = int(result["next_start_line"])


def evaluate_case(case: dict[str, Any], skills_dir: Path | None = None) -> dict[str, Any]:
    runtime = load_runtime(skills_dir)
    expected_skill = str(case["expected_skill"])
    catalog = render_catalog(runtime)
    catalog_ok = f"skill_id: {expected_skill}" in catalog

    loaded = runtime.load_skills([expected_skill])
    packet = loaded["skills"][0]
    referenced_paths = set(packet.get("referenced_paths", []))
    expected_paths = [str(path) for path in case.get("expected_paths", [])]
    missing_paths = [path for path in expected_paths if path not in referenced_paths]

    expected_symbols = {str(symbol) for symbol in case.get("expected_symbols", [])}
    searchable_paths = expected_paths or sorted(referenced_paths)
    combined_text_parts: list[str] = []
    unreadable_paths: list[str] = []
    for path in searchable_paths:
        try:
            combined_text_parts.append(_read_all(runtime, expected_skill, path))
        except SkillPathError:
            unreadable_paths.append(path)
    combined_text = "\n".join(combined_text_parts)
    missing_symbols = sorted(symbol for symbol in expected_symbols if symbol not in combined_text)

    ok = catalog_ok and not missing_paths and not unreadable_paths and not missing_symbols
    return {
        "id": case["id"],
        "status": PASS if ok else FAIL,
        "query": str(case.get("query", "")),
        "expected_skill": expected_skill,
        "catalog_ok": catalog_ok,
        "loaded_skill_ids": loaded["loaded_skill_ids"],
        "expected_paths": expected_paths,
        "referenced_paths": sorted(referenced_paths),
        "missing_paths": missing_paths,
        "unreadable_paths": unreadable_paths,
        "expected_symbols": sorted(expected_symbols),
        "missing_symbols": missing_symbols,
    }


def evaluate_file(path: Path, skills_dir: Path | None = None) -> dict[str, Any]:
    results = [evaluate_case(case, skills_dir=skills_dir) for case in load_cases(path)]
    failed = [result for result in results if result["status"] != PASS]
    return {
        "case_count": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate compiled Skill catalog, exact loading, and references."
    )
    parser.add_argument("cases", type=Path, help="Path to a JSONL eval file.")
    parser.add_argument("--skills-dir", type=Path, default=None)
    args = parser.parse_args()

    report = evaluate_file(args.cases, skills_dir=args.skills_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
