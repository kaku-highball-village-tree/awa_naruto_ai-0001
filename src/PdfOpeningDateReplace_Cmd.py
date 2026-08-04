#!/usr/bin/env python3
"""指定された2箇所の日付だけを安全に差し替えるコマンド。

PyMuPDF が未導入の場合:
    py -m pip install pymupdf
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


INPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_編集前の原稿.pdf"
OUTPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_開講日変更後.pdf"
ERROR_FILE_NAME = "阿波なるとAI塾_PR_ver2_開講日変更後_error.txt"

OPENING_DATE_PAGE_INDEX = 2
OLD_OPENING_DATE_TEXT = "令和８年８月６日開講"
NEW_OPENING_DATE_TEXT = "令和８年９月３日開講／令和８年10月８日開講"

RECEPTION_START_PAGE_INDEX = 3
OLD_RECEPTION_START_TEXT = "令和８年７月１日より受付開始"
NEW_RECEPTION_START_TEXT = "令和８年８月１日より受付開始"

REQUIRED_WEEKLY_TEXT = "毎週木曜日"
EXPECTED_PAGE_COUNT = 5
MAX_FONT_REDUCTION_RATIO = 0.05
COMPARISON_DPI = 144


class ReplacementError(Exception):
    """利用者へ説明して安全に処理を中止できるエラー。"""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class ReplacementSpec:
    """1箇所の置換仕様。"""

    label: str
    page_index: int
    old_text: str
    new_text: str


@dataclass(frozen=True)
class TextStyle:
    """置換対象から取得した文字書式。"""

    bbox: Any
    origin: tuple[float, float]
    font: str
    size: float
    color: int
    flags: int
    ascender: float | None
    descender: float | None


@dataclass(frozen=True)
class PreparedReplacement:
    """編集前の検査を完了した置換情報。"""

    spec: ReplacementSpec
    style: TextStyle
    font_path: Path
    font_size: float
    changed_rect: Any


@dataclass(frozen=True)
class DocumentSnapshot:
    """保存前後の文書を比較するための情報。"""

    page_count: int
    page_sizes: tuple[tuple[float, float], ...]
    page_texts: tuple[str, ...]
    untouched_render_hashes: tuple[tuple[int, str], ...]
    edited_renderings: tuple[tuple[int, int, int, int, int, bytes], ...]


REPLACEMENTS = (
    ReplacementSpec(
        "児童コース開講日",
        OPENING_DATE_PAGE_INDEX,
        OLD_OPENING_DATE_TEXT,
        NEW_OPENING_DATE_TEXT,
    ),
    ReplacementSpec(
        "受付開始日",
        RECEPTION_START_PAGE_INDEX,
        OLD_RECEPTION_START_TEXT,
        NEW_RECEPTION_START_TEXT,
    ),
)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-comparison",
        action="store_true",
        help="3・4ページ目の変更前後を確認用PNGとして出力します。",
    )
    return parser.parse_args(argv)


def load_pymupdf() -> Any:
    """PyMuPDFを読み込み、未導入時は分かりやすく案内する。"""
    if importlib.util.find_spec("pymupdf") is None:
        raise ReplacementError(
            "PyMuPDFがインストールされていません。",
            "次のコマンドでインストールしてください：py -m pip install pymupdf",
        )
    return importlib.import_module("pymupdf")


def find_input_pdf(program_dir: Path) -> Path:
    """プログラムと同じフォルダの入力PDFを確認する。"""
    input_path = program_dir / INPUT_PDF_NAME
    if not input_path.is_file():
        raise ReplacementError(
            "入力PDFが見つかりません。",
            f"{INPUT_PDF_NAME} をプログラムと同じフォルダに配置してください。",
        )
    return input_path


def find_available_path(base_path: Path) -> Path:
    """既存ファイルを上書きしない未使用パスを返す。"""
    if not base_path.exists():
        return base_path
    for number in range(1, 10_000):
        candidate = base_path.with_name(
            f"{base_path.stem}_{number:04d}{base_path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise ReplacementError(
        "未使用の連番ファイル名を確保できませんでした。",
        f"{base_path.name} の連番0001～9999がすべて使用されています。",
    )


def open_pdf(pymupdf: Any, input_path: Path) -> Any:
    """暗号化状態とページ数を検査してPDFを開く。"""
    try:
        doc = pymupdf.open(input_path)
    except Exception as exc:
        raise ReplacementError("入力PDFを開けませんでした。", str(exc)) from exc

    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise ReplacementError("入力PDFは暗号化されているため処理できません。")
    if doc.page_count != EXPECTED_PAGE_COUNT:
        page_count = doc.page_count
        doc.close()
        raise ReplacementError(
            "PDFのページ数が仕様と一致しません。",
            f"検出：{page_count}ページ、必要：{EXPECTED_PAGE_COUNT}ページ",
        )
    return doc


def find_target_text(page: Any, spec: ReplacementSpec) -> Any:
    """指定ページで旧文字列を検索し、厳密に1件の場合だけ返す。"""
    rectangles = page.search_for(spec.old_text)
    print(f"変更対象：{spec.label}")
    print(f"対象ページ：{spec.page_index + 1}ページ目")
    print(f"検索文字列：{spec.old_text}")
    print(f"検出件数：{len(rectangles)}件")
    if not rectangles:
        raise ReplacementError(
            "変更対象の文字列が見つかりませんでした。",
            f"対象ページ：{spec.page_index + 1}ページ目／検索文字列：{spec.old_text}",
        )
    if len(rectangles) != 1:
        raise ReplacementError(
            "変更対象の文字列が複数見つかったため、処理を中止しました。",
            f"対象ページ：{spec.page_index + 1}ページ目／検出件数：{len(rectangles)}件",
        )
    return rectangles[0]


def get_original_text_style(page: Any, target_rect: Any, spec: ReplacementSpec) -> TextStyle:
    """検索矩形と重なるspanを調査して元書式を取得する。"""
    spans: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_rect = target_rect.__class__(span["bbox"])
                if span_rect.intersects(target_rect) and span.get("text"):
                    spans.append(span)

    joined_text = "".join(span["text"] for span in spans)
    if not spans or joined_text != spec.old_text:
        raise ReplacementError(
            "元の文字書式を取得できませんでした。",
            f"処理対象：{spec.label}",
        )

    first = spans[0]
    style_keys = ("font", "size", "color", "flags")
    if any(any(span.get(key) != first.get(key) for key in style_keys) for span in spans):
        raise ReplacementError(
            "対象文字列が異なる書式のspanに分割されています。",
            f"処理対象：{spec.label}",
        )

    print(f"検索座標：{tuple(target_rect)}")
    for number, span in enumerate(spans, start=1):
        print(f"span {number} 文字列：{span.get('text')}")
        print(f"span {number} フォント名：{span.get('font')}")
        print(f"span {number} フォントサイズ：{span.get('size')}")
        print(f"span {number} 文字色：{span.get('color')}")
        print(f"span {number} flags：{span.get('flags')}")
        print(f"span {number} bbox：{span.get('bbox')}")
        print(f"span {number} origin：{span.get('origin')}")
    print()

    return TextStyle(
        bbox=target_rect,
        origin=(float(target_rect.x0), float(first["origin"][1])),
        font=str(first["font"]),
        size=float(first["size"]),
        color=int(first["color"]),
        flags=int(first["flags"]),
        ascender=first.get("ascender"),
        descender=first.get("descender"),
    )


def _font_candidates(is_bold: bool) -> tuple[str, ...]:
    """元の太さを優先したWindows日本語フォント候補を返す。"""
    bold = ("YuGothB.ttc", "meiryob.ttc", "msgothic.ttc")
    regular = ("YuGothM.ttc", "YuGothR.ttc", "meiryo.ttc", "msgothic.ttc")
    return bold + regular if is_bold else regular + bold


def find_japanese_font(style: TextStyle, spec: ReplacementSpec) -> Path:
    """存在を確認した指定候補の日本語フォントだけを選択する。"""
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    font_dirs = (
        windows_dir / "Fonts",
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    )
    is_bold = bool(style.flags & 16) or "bold" in style.font.lower()
    for name in _font_candidates(is_bold):
        for directory in font_dirs:
            path = directory / name
            if path.is_file():
                return path
    raise ReplacementError(
        "日本語フォントが見つからないため、処理を中止しました。",
        f"処理対象：{spec.label}。游ゴシック、メイリオ、またはMS ゴシックが必要です。",
    )


def _span_rectangles(page: Any, excluded_rect: Any) -> list[Any]:
    """置換対象以外の文字span矩形を返す。"""
    rectangles: list[Any] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                rect = excluded_rect.__class__(span["bbox"])
                if span.get("text") and not rect.intersects(excluded_rect):
                    rectangles.append(rect)
    return rectangles


def calculate_placement(
    pymupdf: Any,
    page: Any,
    style: TextStyle,
    font_path: Path,
    spec: ReplacementSpec,
) -> tuple[float, Any]:
    """最大5%の縮小範囲で、他の文字と重ならない配置を検証する。"""
    try:
        font = pymupdf.Font(fontfile=str(font_path))
    except Exception as exc:
        raise ReplacementError("日本語フォントを読み込めませんでした。", str(exc)) from exc

    other_rectangles = _span_rectangles(page, style.bbox)
    for step in range(0, 6):
        font_size = style.size * (1.0 - step / 100.0)
        if font_size < style.size * (1.0 - MAX_FONT_REDUCTION_RATIO):
            break
        width = font.text_length(spec.new_text, fontsize=font_size)
        top = style.origin[1] - font_size * (style.ascender or 1.0)
        bottom = style.origin[1] - font_size * (style.descender or -0.25)
        changed_rect = pymupdf.Rect(
            style.origin[0], min(top, bottom), style.origin[0] + width, max(top, bottom)
        )
        if not page.rect.contains(changed_rect):
            continue
        if any(rect.intersects(changed_rect) for rect in other_rectangles):
            continue
        return font_size, changed_rect | style.bbox

    raise ReplacementError(
        "変更後文字列を元の位置へ安全に配置できません。",
        f"処理対象：{spec.label}。周囲の文字、またはページ領域と重なります。",
    )


def ensure_text_only_redaction_supported(pymupdf: Any, page: Any) -> None:
    """背景を保持できる墨消しAPIが揃っていることを確認する。"""
    parameters = inspect.signature(page.apply_redactions).parameters
    required_parameters = {"images", "graphics", "text"}
    required_constants = (
        "PDF_REDACT_IMAGE_NONE",
        "PDF_REDACT_LINE_ART_NONE",
        "PDF_REDACT_TEXT_REMOVE",
    )
    if not required_parameters.issubset(parameters) or not all(
        hasattr(pymupdf, name) for name in required_constants
    ):
        raise ReplacementError(
            "背景を維持したまま文字だけを削除できないため、処理を中止しました。",
            "必要な墨消しAPIを備えたPyMuPDFへ更新してください。",
        )


def prepare_replacements(pymupdf: Any, doc: Any) -> tuple[PreparedReplacement, ...]:
    """2件すべてを編集前に検査し、部分的な変更を防ぐ。"""
    prepared: list[PreparedReplacement] = []
    for spec in REPLACEMENTS:
        page = doc[spec.page_index]
        target_rect = find_target_text(page, spec)
        style = get_original_text_style(page, target_rect, spec)
        ensure_text_only_redaction_supported(pymupdf, page)
        font_path = find_japanese_font(style, spec)
        font_size, changed_rect = calculate_placement(
            pymupdf, page, style, font_path, spec
        )
        print(f"使用フォント：{font_path}")
        print(f"変更後文字列：{spec.new_text}")
        print(f"挿入文字サイズ：{font_size}\n")
        prepared.append(
            PreparedReplacement(spec, style, font_path, font_size, changed_rect)
        )
    return tuple(prepared)


def remove_original_text(pymupdf: Any, page: Any, target_rect: Any) -> None:
    """画像・図形・背景色を変えず、対象矩形内の文字だけを削除する。"""
    page.add_redact_annot(target_rect, fill=False, cross_out=False)
    applied = page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    if not applied:
        raise ReplacementError("変更前文字列を削除できませんでした。")


def _pdf_color(pymupdf: Any, color: int) -> tuple[float, float, float]:
    """PyMuPDFの整数色を0～1のRGBへ変換する。"""
    red, green, blue = pymupdf.sRGB_to_rgb(color)
    return red / 255.0, green / 255.0, blue / 255.0


def insert_replacement_text(
    pymupdf: Any, page: Any, prepared: PreparedReplacement, font_number: int
) -> None:
    """元の位置・サイズ・色を使って選択可能な新文字列を挿入する。"""
    font_alias = f"replacement_japanese_font_{font_number}"
    try:
        page.insert_font(fontname=font_alias, fontfile=str(prepared.font_path))
        result = page.insert_text(
            prepared.style.origin,
            prepared.spec.new_text,
            fontsize=prepared.font_size,
            fontname=font_alias,
            color=_pdf_color(pymupdf, prepared.style.color),
            overlay=True,
        )
    except Exception as exc:
        raise ReplacementError(
            "新しい文字列を書き込めませんでした。",
            f"処理対象：{prepared.spec.label}／詳細：{exc}",
        ) from exc
    if result < 0:
        raise ReplacementError(
            "新しい文字列を書き込めませんでした。",
            f"処理対象：{prepared.spec.label}",
        )


def apply_replacements(pymupdf: Any, doc: Any, prepared: Sequence[PreparedReplacement]) -> None:
    """検査済みの2件だけを削除・挿入する。"""
    for font_number, item in enumerate(prepared, start=1):
        page = doc[item.spec.page_index]
        remove_original_text(pymupdf, page, item.style.bbox)
        insert_replacement_text(pymupdf, page, item, font_number)


def _render_hash(page: Any) -> str:
    """対象外ページの見た目を比較する等倍RGB画像ハッシュを返す。"""
    pixmap = page.get_pixmap(alpha=False)
    header = f"{pixmap.width}:{pixmap.height}:{pixmap.stride}".encode("ascii")
    return hashlib.sha256(header + pixmap.samples).hexdigest()


def snapshot_document(doc: Any) -> DocumentSnapshot:
    """変更前のページ構成、テキスト、対象外ページの見た目を記録する。"""
    edited_pages = {spec.page_index for spec in REPLACEMENTS}
    sizes = tuple((float(page.rect.width), float(page.rect.height)) for page in doc)
    texts = tuple(page.get_text() for page in doc)
    hashes = tuple(
        (index, _render_hash(doc[index]))
        for index in range(doc.page_count)
        if index not in edited_pages
    )
    renderings = []
    for index in sorted(edited_pages):
        pixmap = doc[index].get_pixmap(alpha=False)
        renderings.append(
            (
                index,
                pixmap.width,
                pixmap.height,
                pixmap.n,
                pixmap.stride,
                bytes(pixmap.samples),
            )
        )
    return DocumentSnapshot(doc.page_count, sizes, texts, hashes, tuple(renderings))


def _validate_edited_page_outside_rect(
    page: Any,
    original: tuple[int, int, int, int, int, bytes],
    allowed_rect: Any,
) -> None:
    """編集対象矩形の外側に視覚的な変化がないことを確認する。"""
    _, width, height, components, stride, original_samples = original
    pixmap = page.get_pixmap(alpha=False)
    if (pixmap.width, pixmap.height, pixmap.n, pixmap.stride) != (
        width,
        height,
        components,
        stride,
    ):
        raise ReplacementError(
            "保存後の検証に失敗しました。", "編集ページの描画サイズが変わっています。"
        )

    # アンチエイリアスの端を含めるため、許可矩形へ2ピクセルの余白を設ける。
    left = max(0, int(allowed_rect.x0) - 2)
    top = max(0, int(allowed_rect.y0) - 2)
    right = min(width, int(allowed_rect.x1 + 0.9999) + 2)
    bottom = min(height, int(allowed_rect.y1 + 0.9999) + 2)
    current_samples = pixmap.samples
    left_bytes = left * components
    right_bytes = right * components
    for y in range(height):
        row_start = y * stride
        row_end = row_start + stride
        if top <= y < bottom:
            if (
                original_samples[row_start : row_start + left_bytes]
                != current_samples[row_start : row_start + left_bytes]
                or original_samples[row_start + right_bytes : row_end]
                != current_samples[row_start + right_bytes : row_end]
            ):
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    "指定された文字列の矩形外で見た目が変わっています。",
                )
        elif original_samples[row_start:row_end] != current_samples[row_start:row_end]:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "指定された文字列の矩形外で見た目が変わっています。",
            )


def validate_output_pdf(
    pymupdf: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
    prepared: Sequence[PreparedReplacement],
) -> None:
    """保存PDFを開き直し、置換結果と対象外ページを検証する。"""
    try:
        output_doc = pymupdf.open(output_path)
    except Exception as exc:
        raise ReplacementError("保存後のPDFを開けませんでした。", str(exc)) from exc

    try:
        if output_doc.needs_pass or output_doc.is_encrypted:
            raise ReplacementError("保存後のPDFが暗号化されています。")
        if output_doc.page_count != snapshot.page_count:
            raise ReplacementError("保存後の検証に失敗しました。", "ページ数が変わっています。")
        output_sizes = tuple(
            (float(page.rect.width), float(page.rect.height)) for page in output_doc
        )
        if output_sizes != snapshot.page_sizes:
            raise ReplacementError("保存後の検証に失敗しました。", "ページサイズが変わっています。")

        for spec in REPLACEMENTS:
            page = output_doc[spec.page_index]
            if len(page.search_for(spec.new_text)) != 1:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"{spec.label}の変更後文字列が1件ではありません。",
                )
            if page.search_for(spec.old_text):
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"{spec.label}の変更前文字列が残っています。",
                )

        if not output_doc[OPENING_DATE_PAGE_INDEX].search_for(REQUIRED_WEEKLY_TEXT):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                f"維持する文字列「{REQUIRED_WEEKLY_TEXT}」が見つかりません。",
            )

        edited_pages = {spec.page_index for spec in REPLACEMENTS}
        for index, original_text in enumerate(snapshot.page_texts):
            if index not in edited_pages and output_doc[index].get_text() != original_text:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"{index + 1}ページ目の文字列が変わっています。",
                )
        for index, original_hash in snapshot.untouched_render_hashes:
            if _render_hash(output_doc[index]) != original_hash:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"{index + 1}ページ目の見た目が変わっています。",
                )
        rendering_by_page = {item[0]: item for item in snapshot.edited_renderings}
        for item in prepared:
            _validate_edited_page_outside_rect(
                output_doc[item.spec.page_index],
                rendering_by_page[item.spec.page_index],
                item.changed_rect,
            )
    finally:
        output_doc.close()


def save_and_validate(
    pymupdf: Any,
    doc: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
    prepared: Sequence[PreparedReplacement],
) -> None:
    """最適化せず一時保存し、検証成功後だけ正式名へ変更する。"""
    temporary_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp.pdf"
    )
    try:
        doc.save(temporary_path)
        validate_output_pdf(pymupdf, temporary_path, snapshot, prepared)
        temporary_path.replace(output_path)
    except ReplacementError:
        raise
    except Exception as exc:
        raise ReplacementError("PDFを保存できませんでした。", str(exc)) from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def _available_comparison_path(program_dir: Path, phase: str, page_number: int) -> Path:
    """確認画像用の未使用ファイル名を返す。"""
    return find_available_path(program_dir / f"確認用_{phase}_{page_number}ページ目.png")


def render_comparison_images(
    doc: Any, program_dir: Path, phase: str
) -> tuple[Path, ...]:
    """3・4ページ目をPDFとは独立した確認用PNGへ描画する。"""
    paths: list[Path] = []
    for page_index in sorted({spec.page_index for spec in REPLACEMENTS}):
        path = _available_comparison_path(program_dir, phase, page_index + 1)
        pixmap = doc[page_index].get_pixmap(dpi=COMPARISON_DPI, alpha=False)
        pixmap.save(path)
        paths.append(path)
    return tuple(paths)


def write_error_file(program_dir: Path, error: ReplacementError) -> Path | None:
    """既存ファイルを上書きせず、日本語のエラー情報を保存する。"""
    try:
        error_path = find_available_path(program_dir / ERROR_FILE_NAME)
        lines = (
            "処理結果：エラー",
            f"入力PDF：{INPUT_PDF_NAME}",
            "対象ページ：3ページ目、4ページ目",
            f"開講日検索文字列：{OLD_OPENING_DATE_TEXT}",
            f"開講日変更後文字列：{NEW_OPENING_DATE_TEXT}",
            f"受付開始日検索文字列：{OLD_RECEPTION_START_TEXT}",
            f"受付開始日変更後文字列：{NEW_RECEPTION_START_TEXT}",
            f"エラー内容：{error.message}",
            f"詳細：{error.detail or 'なし'}",
            f"発生日時：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        )
        error_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        return error_path
    except Exception as exc:
        print(f"エラーファイルを作成できませんでした。詳細：{exc}", file=sys.stderr)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """2箇所の日付差し替えを安全に実行する。"""
    args = parse_arguments(argv)
    program_dir = Path(__file__).resolve().parent
    doc: Any | None = None
    try:
        input_path = find_input_pdf(program_dir)
        output_path = find_available_path(program_dir / OUTPUT_PDF_NAME)
        pymupdf = load_pymupdf()

        print(f"入力PDF：{input_path.name}")
        doc = open_pdf(pymupdf, input_path)
        prepared = prepare_replacements(pymupdf, doc)
        snapshot = snapshot_document(doc)

        apply_replacements(pymupdf, doc, prepared)
        save_and_validate(pymupdf, doc, output_path, snapshot, prepared)

        before_images: tuple[Path, ...] = ()
        after_images: tuple[Path, ...] = ()
        if args.render_comparison:
            with pymupdf.open(input_path) as input_doc:
                before_images = render_comparison_images(input_doc, program_dir, "変更前")
            with pymupdf.open(output_path) as output_doc:
                after_images = render_comparison_images(output_doc, program_dir, "変更後")

        print(f"出力PDF：{output_path.name}")
        for image_path in before_images + after_images:
            print(f"確認用画像：{image_path.name}")
        print("処理が正常に完了しました。")
        return 0
    except ReplacementError as exc:
        print(f"エラー：{exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"詳細：{exc.detail}", file=sys.stderr)
        error_path = write_error_file(program_dir, exc)
        if error_path is not None:
            print(f"エラー情報：{error_path.name}", file=sys.stderr)
        return 1
    except Exception as exc:
        error = ReplacementError(
            "予期しない問題が発生したため、処理を中止しました。",
            f"{type(exc).__name__}: {exc}",
        )
        print(f"エラー：{error.message}", file=sys.stderr)
        print(f"詳細：{error.detail}", file=sys.stderr)
        error_path = write_error_file(program_dir, error)
        if error_path is not None:
            print(f"エラー情報：{error_path.name}", file=sys.stderr)
        return 1
    finally:
        if doc is not None:
            doc.close()


if __name__ == "__main__":
    raise SystemExit(main())
