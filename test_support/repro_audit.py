"""Read-only reproducibility audit for long-form sources and StoryPackages.

The audit never edits a source, package, module, reader, or old package version.
Duplicate text is reported as a candidate for human review; it is never removed
or used to inflate the CJK count.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]")
SPACE = re.compile(r"\s+")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def cjk_count(text: str) -> int:
    return len(CJK.findall(text))


def _paragraphs(path: Path) -> List[Dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: List[Dict[str, Any]] = []
    start = 1
    buffer: List[str] = []
    for number, line in enumerate(lines + [""], 1):
        if line.strip():
            if not buffer:
                start = number
            buffer.append(line.strip())
            continue
        if buffer:
            text = "\n".join(buffer)
            normalized = SPACE.sub("", text)
            result.append({"line": start, "end_line": number - 1, "text": text, "normalized": normalized})
            buffer = []
    return result


def duplicate_candidates(path: Path, limit: int = 100) -> Dict[str, Any]:
    paragraphs = _paragraphs(path)
    exact: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in paragraphs:
        if len(item["normalized"]) >= 40:
            exact[item["normalized"]].append(item)
    exact_groups = [
        {"kind": "exact", "occurrences": [{"line": p["line"], "end_line": p["end_line"]} for p in values], "text": key[:240]}
        for key, values in exact.items() if len(values) > 1
    ]
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in paragraphs:
        value = item["normalized"]
        if len(value) >= 80:
            buckets[value[:32]].append(item)
    near: List[Dict[str, Any]] = []
    for candidates in buckets.values():
        for left_index, left in enumerate(candidates):
            for right in candidates[left_index + 1:]:
                ratio = difflib.SequenceMatcher(None, left["normalized"], right["normalized"]).ratio()
                if 0.94 <= ratio < 1.0:
                    near.append({"kind": "near", "ratio": round(ratio, 4), "left": {"line": left["line"], "end_line": left["end_line"]}, "right": {"line": right["line"], "end_line": right["end_line"]}})
                    if len(near) >= limit:
                        break
            if len(near) >= limit:
                break
        if len(near) >= limit:
            break
    return {"paragraph_count": len(paragraphs), "exact": exact_groups[:limit], "near": near, "candidate_count": len(exact_groups) + len(near)}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _diff_keys(expected: Any, actual: Any, prefix: str = "") -> List[str]:
    if type(expected) is not type(actual):
        return [prefix or "$" ]
    if isinstance(expected, dict):
        keys = set(expected) | set(actual)
        return [item for key in sorted(keys) for item in _diff_keys(expected.get(key), actual.get(key), f"{prefix}.{key}" if prefix else key) if item][:40]
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [prefix + ".length"]
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _diff_keys(left, right, f"{prefix}[{index}]")
            if difference:
                return difference
        return []
    return [] if expected == actual else [prefix or "$" ]


def _module_audit(source: Path, package_path: Path, analysis: Dict[str, Any], package: Dict[str, Any], reader: Dict[str, Any], modules_path: Path) -> Dict[str, Any]:
    try:
        from open_story_engine.package_builder import audit_story_package_modules, read_story_package_modules
        modules = read_story_package_modules(modules_path)
        return audit_story_package_modules(source, analysis, package, reader, modules)
    except Exception as error:  # a reportable audit failure, not a silent skip
        return {"status": "blocked", "issues": [{"id": "module_audit_error", "message": str(error)}]}


def audit_one(source: Path, package_path: Path, package_root: Path) -> Dict[str, Any]:
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    package = _load(package_path)
    source_bytes = source.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    source_text = source_bytes.decode("utf-8")
    package_hash = package.get("sourceAnalysis", {}).get("sha256")
    if package_hash != source_hash:
        errors.append({"id": "source_hash", "message": f"package sourceAnalysis.sha256={package_hash!r} != {source_hash}"})
    reader_path = package_path.with_name("reader.json")
    reader = _load(reader_path) if reader_path.is_file() else {}
    reader_hash = reader.get("source", {}).get("sha256")
    if reader_hash != source_hash:
        errors.append({"id": "reader_source_hash", "message": f"reader source sha256={reader_hash!r} != {source_hash}"})
    modules_path = package_path.parent / "modules"
    module_index_path = modules_path / "package-index.json"
    module_index: Dict[str, Any] = _load(module_index_path) if module_index_path.is_file() else {}
    if module_index:
        if module_index.get("source", {}).get("sha256") != source_hash:
            errors.append({"id": "module_source_hash", "message": "modules/package-index.json 未绑定当前母本"})
        if module_index.get("package") != {"id": package.get("id"), "version": package.get("version")}:
            errors.append({"id": "module_package_ref", "message": "模块索引未绑定当前 StoryPackage 版本"})
    else:
        errors.append({"id": "module_index_missing", "message": "缺少 modules/package-index.json"})
    analysis_path = package_path.with_name("analysis.json")
    analysis = _load(analysis_path) if analysis_path.is_file() else {}
    module_audit = _module_audit(source, package_path, analysis, package, reader, modules_path) if modules_path.is_dir() else {"status": "blocked", "issues": [{"id": "modules_missing", "message": "缺少 modules/"}]}
    if module_audit.get("status") != "passed":
        errors.append({"id": "module_audit", "message": "模块审计未通过或无法执行"})
    rebuild: Dict[str, Any] = {"status": "blocked", "reason": "缺少 analysis.json 或构建器"}
    if analysis:
        try:
            from open_story_engine.package_builder import build_story_package
            rebuilt = build_story_package(source, analysis, package["id"], package["version"])
            differences = _diff_keys(rebuilt, package)
            rebuild = {"status": "passed" if not differences else "failed", "differences": differences[:40], "rebuilt_sha256": sha256_bytes(_canonical(rebuilt).encode("utf-8")), "package_sha256": sha256_bytes(_canonical(package).encode("utf-8"))}
            if differences:
                errors.append({"id": "rebuild_mismatch", "message": "固定母本与 analysis.json 重新构建结果不一致"})
        except Exception as error:
            rebuild = {"status": "blocked", "reason": str(error)}
            errors.append({"id": "rebuild_error", "message": str(error)})
    duplicate = duplicate_candidates(source)
    if duplicate["candidate_count"]:
        warnings.append({"id": "duplicate_candidates", "message": f"发现 {duplicate['candidate_count']} 组重复段落候选；仅记录，不修改正文或字数"})
    return {
        "source": {"path": str(source), "sha256": source_hash, "bytes": len(source_bytes), "cjk": cjk_count(source_text)},
        "package": {"path": str(package_path), "id": package.get("id"), "version": package.get("version"), "source_sha256": package_hash},
        "reader": {"path": str(reader_path), "exists": reader_path.is_file(), "source_sha256": reader_hash, "chapter_count": len(reader.get("chapters", [])) if isinstance(reader, dict) else None},
        "modules": {"path": str(modules_path), "exists": modules_path.is_dir(), "module_index_sha256": package.get("moduleIndexSha256"), "audit": module_audit},
        "duplicates": duplicate,
        "rebuild": rebuild,
        "errors": errors,
        "warnings": warnings,
        "status": "passed" if not errors else "failed",
    }


def audit(args: argparse.Namespace) -> Dict[str, Any]:
    source_roots = [Path(item).resolve() for item in (args.source_root or [str(ROOT / "content/source")])]
    package_root = Path(args.package_root).resolve()
    sources: Dict[str, Path] = {}
    for source_root in source_roots:
        for source in sorted(source_root.rglob("*.txt")):
            digest = sha256_bytes(source.read_bytes())
            if cjk_count(source.read_text(encoding="utf-8")) >= args.minimum_cjk:
                sources[digest] = source
    packages = []
    for package_path in sorted(package_root.glob("*/*/package.json")):
        package = _load(package_path)
        if args.package_id and package.get("id") != args.package_id:
            continue
        if args.package_version and package.get("version") != args.package_version:
            continue
        packages.append((package_path, package))
    records: List[Dict[str, Any]] = []
    for digest, source in sources.items():
        matches = [(path, package) for path, package in packages if package.get("sourceAnalysis", {}).get("sha256") == digest]
        if not matches:
            records.append({"source": {"path": str(source), "sha256": digest, "cjk": cjk_count(source.read_text(encoding="utf-8"))}, "status": "blocked", "errors": [{"id": "package_missing", "message": "十万 CJK 母本没有绑定的 StoryPackage"}]})
            continue
        records.extend(audit_one(source, path, package_root) for path, _ in matches)
    versions: Dict[str, List[str]] = defaultdict(list)
    for path, package in packages:
        versions[package.get("id", "")].append(package.get("version", ""))
    version_issues = [{"id": package_id, "versions": values} for package_id, values in versions.items() if len(values) != len(set(values))]
    report = {"schema": "story-repro-audit/0.1", "status": "passed" if records and all(record.get("status") == "passed" for record in records) and not version_issues else "failed", "policy": {"minimum_cjk": args.minimum_cjk, "duplicate_text_action": "report_only", "modify_source": False, "overwrite_old_package": False}, "sources": records, "version_issues": version_issues}
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="长篇母本与 StoryPackage 只读复现审计")
    parser.add_argument("--source-root", action="append", help="母本扫描根目录；可重复传入多个目录")
    parser.add_argument("--package-root", default=str(ROOT / "content/packages"))
    parser.add_argument("--package-id")
    parser.add_argument("--package-version")
    parser.add_argument("--minimum-cjk", type=int, default=100000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = audit(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "sources": len(report["sources"]), "version_issues": len(report["version_issues"])}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
