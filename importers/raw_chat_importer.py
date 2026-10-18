"""Losslessly normalize captured chat transcripts without using an LLM or API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
MESSAGE_RE = re.compile(r"(?m)^\[MESSAGE\s+(\d{6})\]\r?\n\[(USER|ASSISTANT)\]\r?\n")
ANY_MESSAGE_RE = re.compile(r"(?m)^\[MESSAGE\s+([^\]]+)\]")
SEPARATOR_RE = re.compile(r"(?m)^={20,}\r?$")
IMAGE_RE = re.compile(r"\[IMAGE:\s*(IMG_\d{6})\s*\]")
OUTPUT_RE = re.compile(r"^\d{6}-\d{6}\.md$")


@dataclass(frozen=True)
class Message:
    message_id: str
    number: int
    role: str
    body: str
    image_refs: tuple[str, ...]
    order: int


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_messages(text: str) -> list[Message]:
    matches = list(MESSAGE_RE.finditer(text))
    if len(matches) != len(ANY_MESSAGE_RE.findall(text)):
        raise ValueError("存在无法识别的 MESSAGE 编号或角色格式")
    messages: list[Message] = []
    for order, match in enumerate(matches, 1):
        separator = SEPARATOR_RE.search(text, match.end())
        if separator is None:
            raise ValueError(f"MESSAGE {match.group(1)} 后缺少结构分隔线")
        next_match = matches[order] if order < len(matches) else None
        if next_match is not None and separator.start() > next_match.start():
            raise ValueError(f"MESSAGE {match.group(1)} 的正文边界无效")
        body_end = separator.start()
        # The newline immediately before the structural separator is not body text.
        if text[max(match.end(), body_end - 2):body_end] == "\r\n":
            body_end -= 2
        elif body_end > match.end() and text[body_end - 1] == "\n":
            body_end -= 1
        body = text[match.end():body_end]
        refs = tuple(IMAGE_RE.findall(body))
        number = int(match.group(1))
        messages.append(Message(match.group(1), number, match.group(2), body, refs, order))
    return messages


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_markdown(messages: list[Message], source_rel: str, project: str, source_sha: str) -> str:
    refs = list(dict.fromkeys(ref for message in messages for ref in message.image_refs))
    lines = [
        "---",
        f"source: {yaml_string(source_rel)}",
        f"project: {yaml_string(project)}",
        f"message_start: {messages[0].message_id}",
        f"message_end: {messages[-1].message_id}",
        f"message_count: {len(messages)}",
        f"image_refs: {json.dumps(refs, ensure_ascii=False)}",
        f"generated_from: {yaml_string('sha256:' + source_sha)}",
        "---",
        "",
    ]
    prefix = "\n".join(lines)
    blocks: list[str] = []
    for message in messages:
        block = f"## MESSAGE {message.message_id}｜{message.role}\n\n{message.body}"
        if message.image_refs:
            labels = "、".join(message.image_refs)
            block += f"\n\n> 图片引用：{labels}"
        blocks.append(block)
    return prefix + "\n\n".join(blocks) + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return True


def import_chat(source: Path, batch_size: int = 80) -> dict:
    if not 50 <= batch_size <= 100:
        raise ValueError("batch_size 必须在 50 到 100 之间")
    source = source.resolve()
    before = source.read_bytes()
    source_sha = digest(before)
    text = before.decode("utf-8-sig")
    messages = parse_messages(text)
    if not messages:
        raise ValueError("未发现 MESSAGE")

    # Supports the legacy unnumbered source directory while creating the canonical tree.
    project_root = source.parent.parent
    project = project_root.name
    raw_dir = project_root / "00_原始文件"
    image_dir = project_root / "01_图片附件"
    output_dir = project_root / "02_标准化对话"
    canonical_source = raw_dir / source.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if canonical_source.exists():
        if digest(canonical_source.read_bytes()) != source_sha:
            raise ValueError("规范目录中的母档与原始母档 SHA-256 不一致")
    else:
        shutil.copyfile(source, canonical_source)
    os.chmod(canonical_source, stat.S_IREAD)

    numbers = [message.number for message in messages]
    unique_numbers = set(numbers)
    missing = [number for number in range(min(numbers), max(numbers) + 1) if number not in unique_numbers]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    all_refs = [ref for message in messages for ref in message.image_refs]
    distinct_refs = list(dict.fromkeys(all_refs))
    source_rel = canonical_source.relative_to(project_root.parents[2]).as_posix()

    generated: list[dict] = []
    expected_names: set[str] = set()
    changed_files = 0
    for offset in range(0, len(messages), batch_size):
        group = messages[offset:offset + batch_size]
        name = f"{group[0].message_id}-{group[-1].message_id}.md"
        expected_names.add(name)
        output = output_dir / name
        rendered = render_markdown(group, source_rel, project, source_sha)
        changed_files += int(write_if_changed(output, rendered))
        generated.append({
            "file": output.relative_to(project_root).as_posix(),
            "message_start": group[0].message_id,
            "message_end": group[-1].message_id,
            "message_count": len(group),
            "sha256": digest(rendered.encode("utf-8")),
        })
    for stale in output_dir.iterdir():
        if stale.is_file() and OUTPUT_RE.fullmatch(stale.name) and stale.name not in expected_names:
            stale.unlink()

    legacy_image_dir = project_root / "图片附件"
    attachment_dirs = [path for path in (image_dir, legacy_image_dir) if path.exists()]
    found_refs = []
    for ref in distinct_refs:
        if any(any(candidate.is_file() for candidate in folder.glob(ref + ".*")) for folder in attachment_dirs):
            found_refs.append(ref)

    old_manifest = {}
    manifest_path = project_root / "manifest.json"
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text("utf-8"))
        except (ValueError, OSError):
            pass
    stable_key = (source_sha, VERSION, batch_size)
    old_key = (old_manifest.get("sha256"), old_manifest.get("importer_version"), old_manifest.get("batch_size"))
    imported_at = old_manifest.get("imported_at") if old_key == stable_key else None
    imported_at = imported_at or datetime.now(timezone.utc).isoformat()
    manifest = {
        "source_file": source_rel,
        "sha256": source_sha,
        "total_bytes": len(before),
        "total_characters": len(text),
        "total_messages": len(messages),
        "user_messages": sum(message.role == "USER" for message in messages),
        "assistant_messages": sum(message.role == "ASSISTANT" for message in messages),
        "min_message_id": f"{min(numbers):06d}",
        "max_message_id": f"{max(numbers):06d}",
        "missing_message_ids": [f"{number:06d}" for number in missing],
        "duplicate_message_ids": [f"{number:06d}" for number in duplicates],
        "image_reference_count": len(all_refs),
        "image_ids": distinct_refs,
        "image_attachments_found": found_refs,
        "image_attachments_missing": [ref for ref in distinct_refs if ref not in found_refs],
        "normalized_files": generated,
        "imported_at": imported_at,
        "importer_version": VERSION,
        "batch_size": batch_size,
        "messages": [{
            "message_id": message.message_id,
            "role": message.role,
            "order": message.order,
            "source": source_rel,
            "image_refs": list(message.image_refs),
            "raw_body_characters": len(message.body),
            "raw_body_sha256": digest(message.body.encode("utf-8")),
        } for message in messages],
        "validation": {
            "message_markers_match": len(messages) == len(ANY_MESSAGE_RE.findall(text)),
            "roles_valid": all(message.role in {"USER", "ASSISTANT"} for message in messages),
            "body_characters_preserved": sum(len(message.body) for message in messages),
            "image_references_preserved": sum(len(message.image_refs) for message in messages) == len(IMAGE_RE.findall(text)),
            "source_sha256_unchanged": digest(source.read_bytes()) == source_sha,
        },
    }
    write_if_changed(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if digest(source.read_bytes()) != source_sha or digest(canonical_source.read_bytes()) != source_sha:
        raise RuntimeError("母档 SHA-256 在导入期间发生变化")
    if missing or duplicates or not all(manifest["validation"].values()):
        raise RuntimeError("导入校验失败，详情见 manifest.json")
    return manifest | {"changed_normalized_files": changed_files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=80)
    args = parser.parse_args()
    result = import_chat(args.source, args.batch_size)
    print(json.dumps({key: result[key] for key in (
        "sha256", "total_messages", "user_messages", "assistant_messages",
        "image_reference_count", "missing_message_ids", "duplicate_message_ids",
        "changed_normalized_files",
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
