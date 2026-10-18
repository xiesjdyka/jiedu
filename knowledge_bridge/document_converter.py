"""Local document conversion contract used by import jobs.

Office conversion never invokes hosted OCR. Images and PDFs use the managed
offline worker; originals remain immutable with explicit conversion status.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import io
import zipfile


TEXT_EXTENSIONS = {".md", ".txt", ".json", ".html", ".htm", ".xml", ".yaml", ".yml"}
ANYDOC_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm",
    ".ppsx", ".ppsm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods",
    ".odp", ".rtf", ".epub", ".csv", ".pdf",
}


@dataclass(frozen=True)
class ConversionResult:
    status: str
    source_format: str
    converter: str
    markdown: str | None = None
    reason: str | None = None
    requires_ocr: bool = False
    metadata: dict | None = None

    def public(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "markdown"}


class DocumentConverter:
    """Convert supported files to Markdown without network access."""

    MEMORY_INPUT_LIMIT = 16 * 1024 * 1024
    MEMORY_OUTPUT_LIMIT = 32 * 1024 * 1024
    PROTECTED_FORMATS = {".docx", ".xlsx", ".pptx", ".pdf", ".csv"}

    def convert_bytes(self, data: bytes, suffix: str, *, progress=None, cancel=None) -> ConversionResult:
        """Use the installed anydoc 0.2.4 byte parser; no path or hosted fallback.

        Office ZIP entries are read through Rust Cursor, PDF through load_mem.
        Bounds also limit decompression; embedded images remain in the original.
        """
        suffix = suffix.casefold()
        from .local_ocr import OCR_FORMATS, recognize_document
        if suffix in OCR_FORMATS:
            return recognize_document(data, suffix, progress=progress, cancel=cancel)
        converter = "native-text-memory" if suffix in TEXT_EXTENSIONS else "anydoc-bytes-0.2.4"
        if len(data) > self.MEMORY_INPUT_LIMIT:
            return ConversionResult("size_limit", suffix.lstrip("."), converter,
                                    reason="原件已保存；内存转换输入上限为 16 MiB")
        try:
            if suffix in TEXT_EXTENSIONS:
                encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                text = data.decode(encoding, errors="strict")
                if suffix == ".md": return ConversionResult("source_markdown", "markdown", converter)
                markdown = text if suffix == ".txt" else f"```{suffix.lstrip('.')}\n{text.rstrip()}\n```\n"
            elif suffix in self.PROTECTED_FORMATS:
                if suffix in {".docx", ".xlsx", ".pptx"}:
                    with zipfile.ZipFile(io.BytesIO(data)) as package:
                        entries = package.infolist()
                        if len(entries) > 10000 or sum(e.file_size for e in entries) > 32*1024*1024:
                            return ConversionResult("size_limit", suffix.lstrip("."), converter,
                                                    reason="原件已保存；Office 展开内容上限 32 MiB / 10000 项")
                import anydoc
                markdown = anydoc.to_markdown_bytes(data, suffix.lstrip("."), ocr="reject")
            else:
                return ConversionResult("unsupported", suffix.lstrip(".") or "unknown", "none",
                                        reason="原件已保存；此格式尚未验证内存转换，不产生明文临时文件")
            if len(markdown.encode("utf-8")) > self.MEMORY_OUTPUT_LIMIT:
                return ConversionResult("size_limit", suffix.lstrip("."), converter,
                                        reason="原件已保存；派生正文超过 32 MiB 上限")
            return ConversionResult("converted", suffix.lstrip("."), converter, markdown=markdown)
        except Exception as exc:
            name = type(exc).__name__
            needs_ocr = name == "NeedsOcrError"
            reason = "扫描或混合 PDF 需要 OCR；本轮只保留原件" if needs_ocr else f"内存转换失败（{name}），原件已保留"
            return ConversionResult("needs_ocr" if needs_ocr else "failed", suffix.lstrip("."),
                                    converter, reason=reason, requires_ocr=needs_ocr)

    def inspect(self, path: Path) -> dict:
        suffix = path.suffix.casefold()
        from .local_ocr import OCR_FORMATS
        if suffix in OCR_FORMATS:
            return ConversionResult('ready',suffix.lstrip('.'),'local-ocr',requires_ocr=True).public()
        if suffix == ".md":
            return ConversionResult("source_markdown", "markdown", "native-text").public()
        if suffix in TEXT_EXTENSIONS:
            return ConversionResult("ready", suffix.lstrip("."), "native-text").public()
        if suffix in ANYDOC_EXTENSIONS:
            try:
                import anydoc  # noqa: F401
            except ImportError:
                return ConversionResult(
                    "converter_unavailable", suffix.lstrip("."), "anydoc",
                    reason="内置文档转换器不可用",
                ).public()
            return ConversionResult("ready", suffix.lstrip("."), "anydoc").public()
        return ConversionResult(
            "unsupported", suffix.lstrip(".") or "unknown", "none",
            reason="此文件类型只保留原件，暂不生成 Markdown",
        ).public()

    def convert(self, path: Path, *, progress=None, cancel=None) -> ConversionResult:
        suffix = path.suffix.casefold()
        from .local_ocr import OCR_FORMATS
        if suffix in OCR_FORMATS:
            if path.stat().st_size > self.MEMORY_INPUT_LIMIT:
                return ConversionResult('size_limit',suffix.lstrip('.'),'local-ocr',reason='OCR 输入超过 16 MiB；原件保留')
            return self.convert_bytes(path.read_bytes(),suffix,progress=progress,cancel=cancel)
        if suffix == ".md":
            return ConversionResult("source_markdown", "markdown", "native-text")
        if suffix in TEXT_EXTENSIONS:
            try:
                text = path.read_text("utf-8-sig", errors="replace")
            except OSError:
                return ConversionResult("failed", suffix.lstrip("."), "native-text",
                                        reason="本地文件读取失败")
            if suffix == ".txt":
                markdown = text
            else:
                language = suffix.lstrip(".") or "text"
                markdown = f"```{language}\n{text.rstrip()}\n```\n"
            return ConversionResult("converted", suffix.lstrip("."), "native-text", markdown=markdown)
        if suffix not in ANYDOC_EXTENSIONS:
            return ConversionResult("unsupported", suffix.lstrip(".") or "unknown", "none",
                                    reason="此文件类型只保留原件，暂不生成 Markdown")
        try:
            import anydoc
            markdown = anydoc.to_markdown(path, ocr="reject")
            return ConversionResult("converted", suffix.lstrip("."), "anydoc", markdown=markdown)
        except ImportError:
            return ConversionResult("converter_unavailable", suffix.lstrip("."), "anydoc",
                                    reason="内置文档转换器不可用")
        except Exception as exc:
            # The converter may echo source material or paths in exception text.
            # Expose only the stable exception type to UI, logs, and API callers.
            name = type(exc).__name__
            requires_ocr = name == "NeedsOcrError"
            reason = "扫描版 PDF 需要 OCR；界渡不会默认上传文件" if requires_ocr else f"本地转换失败（{name}）"
            return ConversionResult("needs_ocr" if requires_ocr else "failed", suffix.lstrip("."),
                                    "anydoc", reason=reason, requires_ocr=requires_ocr)
