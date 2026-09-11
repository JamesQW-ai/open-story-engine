"""Novel-source import for play mode: offline inspect/analyze/build/audit pipeline.

Same local, model-free pipeline as the CLI ``build-story-package`` flow; the
result lands in the API package root and is immediately playable. Existing
package directories are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional

from .api_read import ReadError, ReadService
from .package_builder import (
    StoryPackageBuildError,
    analyze_standard_novel,
    audit_story_package,
    audit_story_package_modules,
    build_source_reader,
    build_story_package,
    build_story_package_modules,
    write_story_package_modules,
)
from .source import SourceNovelError


class ImportService:
    def __init__(self, read: ReadService, lock: Optional[threading.Lock] = None) -> None:
        self.read = read
        self.package_root = read.package_root
        self._lock = lock or threading.Lock()

    def import_novel(self, package_id: str | None, version: str, source_text: str,
                     file_name: str = "source.txt") -> dict[str, Any]:
        if (Path(file_name).name != file_name or "/" in file_name or "\\" in file_name
                or "\x00" in file_name or Path(file_name).suffix.lower() != ".txt"):
            raise ReadError(422, "invalid_source", "请选择标准 UTF-8 编码的 .txt 小说文件")
        if not isinstance(source_text, str) or len(source_text.strip()) < 100 or "\x00" in source_text:
            raise ReadError(422, "invalid_source", "请提供包含标题、章节与正文的标准 TXT 小说，正文至少 100 字")
        automatic = package_id is None
        source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if automatic:
            package_id = "novel-" + source_sha256
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", package_id):
            raise ReadError(422, "invalid_package_ref", "故事包 ID 仅允许字母、数字、下划线与连字符")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ReadError(422, "invalid_package_ref", "版本号必须是 x.y.z 形式")
        directory = self.package_root / package_id / version
        with self._lock:
            if directory.exists():
                if automatic:
                    _, package = self.read.load_package(package_id, version)
                    if package.get("sourceAnalysis", {}).get("sha256") != source_sha256:
                        raise ReadError(409, "source_mismatch", "已有故事与本次文件内容不一致，无法复用")
                    reader = self.read.chapter_reader(package_id, version)
                    return self._result(package_id, version, package, reader)
                raise ReadError(409, "package_exists", "该故事包 ID 与版本已存在，请提高版本号或更换 ID")
            with TemporaryDirectory(prefix="story-import-") as temp_dir:
                source_path = Path(temp_dir) / file_name
                source_path.write_text(source_text, encoding="utf-8")
                try:
                    analysis = analyze_standard_novel(source_path, 12000)
                    package = build_story_package(source_path, analysis, package_id, version)
                    audit = audit_story_package(source_path, analysis, package)
                    reader = build_source_reader(source_path, analysis, package)
                    modules = build_story_package_modules(source_path, analysis, package, reader)
                    module_audit = audit_story_package_modules(source_path, analysis, package, reader, modules)
                except (OSError, SourceNovelError, StoryPackageBuildError, ValueError) as error:
                    raise ReadError(422, "invalid_source", "母本无法构建为故事包：" + str(error)) from error
                if audit["status"] != "passed":
                    raise ReadError(422, "audit_failed", "构建后的完整性审计未通过：" + "；".join(
                        item["message"] for item in audit["issues"]))
                if module_audit["status"] != "passed":
                    raise ReadError(422, "audit_failed", "模块完整性审计未通过：" + "；".join(
                        item["message"] for item in module_audit["issues"]))

                self.package_root.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(prefix=".story-import-", dir=self.package_root) as staging_dir:
                    staging = Path(staging_dir)
                    for name, value in (("package", package), ("analysis", analysis), ("reader", reader), ("audit", audit)):
                        (staging / f"{name}.json").write_text(
                            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    modules_path = staging / "modules"
                    write_story_package_modules(modules_path, modules)
                    (modules_path / "module-audit.json").write_text(
                        json.dumps(module_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    directory.parent.mkdir(parents=True, exist_ok=True)
                    staging.rename(directory)

        return self._result(package_id, version, package, reader)

    @staticmethod
    def _result(package_id: str, version: str, package: dict[str, Any], reader: dict[str, Any]) -> dict[str, Any]:
        return {
            "package_id": package_id,
            "version": version,
            "title": package["metadata"].get("title", package_id),
            "beat_count": len(package["story"]["narrativeGraph"]["beats"]),
            "character_count": len(package["characters"]),
            "chapter_count": len(reader["chapters"]),
        }
