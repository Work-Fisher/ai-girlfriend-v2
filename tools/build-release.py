"""Build a clean release tree without copying the 15 GB payload twice."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path


SKIP_TOP_LEVEL = {".git", ".installed.json", ".playwright-mcp", "CLAUDE.md"}
SKIP_PREFIXES = {
    Path("app/logs"),
    Path("app/output"),
    Path("app/heygem-data"),
    Path("app/voices"),
    Path("app/dsh-bridge/config.json"),
    Path("app/dsh-bridge/session.json"),
    Path("app/dsh-bridge/dsh-home"),
    Path("app/dsh-bridge/tests"),
    Path("app/.venv/Lib/site-packages/bin"),
    Path("runtime/python311/Scripts"),
    Path("包信息.json"),
}
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".orig", ".tmp", ".log"}


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def should_skip(relative: Path) -> bool:
    if relative.parts and relative.parts[0] in SKIP_TOP_LEVEL:
        return True
    if any(part in SKIP_PARTS for part in relative.parts):
        return True
    if relative.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(relative == prefix or is_relative_to(relative, prefix) for prefix in SKIP_PREFIXES)


def link_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def link_tree(source: Path, destination: Path, *, skip_backup: bool = False) -> int:
    count = 0
    for current, directories, files in os.walk(source):
        current_path = Path(current)
        relative_directory = current_path.relative_to(source)
        directories[:] = [name for name in directories if name not in SKIP_PARTS]
        (destination / relative_directory).mkdir(parents=True, exist_ok=True)
        for name in files:
            if Path(name).suffix.lower() in SKIP_SUFFIXES:
                continue
            if skip_backup and name.endswith(".bak"):
                continue
            link_file(current_path / name, destination / relative_directory / name)
            count += 1
    return count


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    stage = args.stage.resolve()
    if stage.exists():
        raise SystemExit(f"发行目录已存在，拒绝覆盖：{stage}")
    if stage == source or is_relative_to(stage, source):
        raise SystemExit("发行目录不能位于源项目内部。")
    if source.drive.lower() != stage.drive.lower():
        raise SystemExit("发行目录必须与源项目位于同一磁盘，才能用 hardlink 节省空间。")

    stage.mkdir(parents=True)
    linked_files = 0
    for current, directories, files in os.walk(source):
        current_path = Path(current)
        relative_directory = current_path.relative_to(source)
        directories[:] = [
            name
            for name in directories
            if not should_skip(relative_directory / name)
        ]
        (stage / relative_directory).mkdir(parents=True, exist_ok=True)
        for name in files:
            relative = relative_directory / name
            if should_skip(relative):
                continue
            link_file(current_path / name, stage / relative)
            linked_files += 1

    # DSH runtime profiles are required, while its sessions, indexes and credentials are user data.
    profiles_source = source / "app/dsh-bridge/dsh-home/profiles"
    if not profiles_source.is_dir():
        raise SystemExit(f"缺少 DSH profiles：{profiles_source}")
    linked_files += link_tree(
        profiles_source,
        stage / "app/dsh-bridge/dsh-home/profiles",
        skip_backup=True,
    )
    for relative in (
        "app/dsh-bridge/dsh-home/sessions",
        "app/dsh-bridge/dsh-home/storages",
        "app/dsh-bridge/workspace",
        "app/logs",
        "app/output",
        "app/heygem-data/log",
        "app/heygem-data/result",
        "app/heygem-data/runtime",
        "app/heygem-data/temp",
    ):
        (stage / relative).mkdir(parents=True, exist_ok=True)

    write_json(
        stage / "app/dsh-bridge/config.json",
        {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "base_url": "",
            "api_key": "",
            "profile": "sdk",
            "max_tokens": None,
            "request_timeout_seconds": 180,
        },
    )

    # Only the reviewed demo media enters the release. Uploaded avatars and relationship state stay local.
    input_source = source / "app/heygem-data/input"
    input_stage = stage / "app/heygem-data/input"
    for name in ("avatar-demo.mp4", "heygem-warmup.wav"):
        candidate = input_source / name
        if not candidate.is_file():
            raise SystemExit(f"缺少发行素材：{candidate}")
        link_file(candidate, input_stage / name)
        linked_files += 1
    write_json(
        stage / "app/heygem-data/state.json",
        {
            "lipsync_engine": "heygem",
            "avatars": {
                "video": {"filename": "avatar-demo.mp4", "original_name": "avatar-demo.mp4"}
            },
            "avatar_driver": "video",
            "avatar": "avatar-demo.mp4",
            "original_name": "avatar-demo.mp4",
            "lipsync_enabled": False,
        },
    )
    write_json(stage / "app/heygem-data/affinity.json", {})
    write_json(stage / "app/heygem-data/llm-provider.json", {"active": "", "providers": []})

    # Ship one reviewed default voice, never the user's later voice clones.
    voices_source = source / "app/voices"
    for relative in (Path("README.md"), Path("苏晚-单句/ref_audio.wav"), Path("苏晚-单句/ref_text.txt")):
        candidate = voices_source / relative
        if not candidate.is_file():
            raise SystemExit(f"缺少默认声音文件：{candidate}")
        link_file(candidate, stage / "app/voices" / relative)
        linked_files += 1
    (stage / "app/voices/.active").write_text("苏晚-单句\n", encoding="utf-8")

    package_info_path = source / "包信息.json"
    package_info = json.loads(package_info_path.read_text(encoding="utf-8"))
    package_info["built_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    package_info["size_bytes"] = 0
    stage_info = stage / "包信息.json"
    for _ in range(3):
        write_json(stage_info, package_info)
        package_info["size_bytes"] = tree_size(stage)
    write_json(stage_info, package_info)

    print(f"RELEASE_STAGE={stage}")
    print(f"LINKED_FILES={linked_files}")
    print(f"SIZE_BYTES={tree_size(stage)}")


if __name__ == "__main__":
    main()
