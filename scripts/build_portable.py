"""Build a reviewed allowlist ZIP without credentials or host-specific files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "kitchen-assistant-portable"
ROOT_FILES = (
    "robot_main.py", "chat_handler.py", "text_input.py", "voice_input.py",
    "requirements.txt", "requirements-voice.txt", "README.md", "START_HERE.md",
    "install.cmd", "start.cmd", "check.cmd", "install.sh", "start.sh", "check.sh",
    "config/console.json.example", "config/qwen.env.example",
    "scripts/portable.py", "scripts/build_portable.py",
    "scripts/check_qwen_connection.py", "scripts/validate_recipe_catalog.py",
)
SUFFIXES = {".py", ".json", ".js", ".css", ".html", ".md"}


def package_files(root: Path, *, include_local_recipes: bool = False) -> list[Path]:
    files = [root / name for name in ROOT_FILES]
    for directory in ("runtime_core", "skills"):
        for path in sorted((root / directory).rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if "recipes" in relative.parts and any(part in {"generated", "imported"} for part in relative.parts):
                if not include_local_recipes:
                    continue
            if path.is_file() and path.suffix in SUFFIXES:
                files.append(path)
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"拒绝打包缺失文件、符号链接或目录外文件：{path.relative_to(root)}")
    return sorted(set(files))


def build(output: Path, *, include_local_recipes: bool = False, root: Path = ROOT) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"package": PACKAGE_NAME, "preferred_port": 18765,
                "include_local_recipes": include_local_recipes, "files": {}}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in package_files(root, include_local_recipes=include_local_recipes):
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            # Windows batch files should use native newlines even when built on Mac.
            if path.suffix == ".cmd":
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(f"{PACKAGE_NAME}/{relative}", date_time=(2026, 9, 17, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            archive.writestr(info, data)
        archive.writestr(f"{PACKAGE_NAME}/PACKAGE_MANIFEST.json",
                         json.dumps(manifest, ensure_ascii=False, indent=2))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    return {"path": str(output), "files": len(manifest["files"]), "bytes": output.stat().st_size,
            "sha256": checksum, "include_local_recipes": include_local_recipes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{PACKAGE_NAME}.zip")
    parser.add_argument("--include-local-recipes", action="store_true", help="带上本机已生成和已导入的菜谱，不包含密钥和日志")
    args = parser.parse_args()
    print(json.dumps(build(args.output, include_local_recipes=args.include_local_recipes), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
