#!/usr/bin/env python3
"""児童コースの開講日だけを安全に差し替えるコマンドラインプログラム。

PyMuPDF が未導入の場合:
    py -m pip install pymupdf
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_編集前の原稿.pdf"
OUTPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_開講日変更後.pdf"
TARGET_PAGE_INDEX = 2
OLD_TEXT = "令和8年8月6日開講"
NEW_TEXT = "令和8年9月3日開講"
REQUIRED_TEXT = "毎週木曜日"


class ReplacementError(Exception):
    """利用者に説明して安全に処理を中止できるエラー。"""


@dataclass(frozen=True)
class TextStyle:
    """置換対象から取得した文字書式。"""

    bbox: Any
    origin: Any
    font: str
    size: float
    color: int
    flags: int
    ascender: float | None
    descender: float | None


@dataclass(frozen=True)
class DocumentSnapshot:
    """保存前後の文書を比較するための情報。"""

    page_count: int
    page_sizes: tuple[tuple[float, float], ...]
    page_texts: tuple[str, ...]
    other_page_render_hashes: tuple[tuple[int, str], ...]


def load_pymupdf() -> Any:
    """PyMuPDFを読み込み、未導入時は分かりやすいエラーにする。"""
    if importlib.util.find_spec("pymupdf") is None:
        raise ReplacementError(
            "PyMuPDFがインストールされていません。\n"
            "次のコマンドでインストールしてください。\n"
            "py -m pip install pymupdf"
        )
    return importlib.import_module("pymupdf")


def find_input_pdf(program_dir: Path) -> Path:
    """プログラムと同じフォルダにある入力PDFを確認する。"""
    input_path = program_dir / INPUT_PDF_NAME
    if not input_path.is_file():
        raise ReplacementError(
            "入力PDFが見つかりません。\n"
            f"{INPUT_PDF_NAME} をプログラムと同じフォルダに配置してください。"
        )
    return input_path


def open_pdf(pymupdf: Any, input_path: Path) -> Any:
    """暗号化およびページ数を検査してPDFを開く。"""
    try:
        doc = pymupdf.open(input_path)
    except Exception as exc:
        raise ReplacementError(f"入力PDFを開けませんでした。\n詳細：{exc}") from exc

    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise ReplacementError("入力PDFは暗号化されているため処理できません。")
    if doc.page_count <= TARGET_PAGE_INDEX:
        page_count = doc.page_count
        doc.close()
        raise ReplacementError(
            "PDFのページ数が不足しています。\n"
            f"ページ数：{page_count}ページ（3ページ以上が必要です）"
        )
    return doc


def find_target_text(page: Any) -> Any:
    """対象文字列を検索し、1件の場合だけ座標を返す。"""
    rectangles = page.search_for(OLD_TEXT)
    print(f"検出件数：\n{len(rectangles)}件\n")
    if not rectangles:
        raise ReplacementError(
            "変更対象の文字列が見つかりませんでした。\n"
            f"検索文字列：{OLD_TEXT}"
        )
    if len(rectangles) != 1:
        raise ReplacementError(
            "変更対象の文字列が複数見つかったため、処理を中止しました。\n"
            f"検出件数：{len(rectangles)}件"
        )
    return rectangles[0]


def get_original_text_style(page: Any, target_rect: Any) -> TextStyle:
    """検索座標と重なるspanを調査し、元の文字書式を取得する。"""
    spans: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_rect = target_rect.__class__(span["bbox"])
                if span_rect.intersects(target_rect) and span.get("text"):
                    spans.append(span)

    joined_text = "".join(span["text"] for span in spans)
    if not spans or OLD_TEXT not in joined_text:
        raise ReplacementError(
            "元の文字書式を取得できなかったため、処理を中止しました。"
        )

    first = spans[0]
    keys = ("font", "size", "color", "flags")
    if any(any(span.get(key) != first.get(key) for key in keys) for span in spans):
        raise ReplacementError(
            "対象文字列が異なる書式のspanに分割されているため、処理を中止しました。"
        )

    print("調査結果：")
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

    # x座標は検索矩形を使い、span内の前置き文字に影響されないようにする。
    origin = (target_rect.x0, first["origin"][1])
    return TextStyle(
        bbox=target_rect,
        origin=origin,
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


def find_japanese_font(style: TextStyle) -> Path:
    """存在を確認したWindows日本語フォントだけを選択する。"""
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
        "日本語フォントが見つからないため、処理を中止しました。\n"
        "游ゴシック、メイリオ、またはMS ゴシックをインストールしてください。"
    )


def ensure_text_only_redaction_supported(pymupdf: Any, page: Any) -> None:
    """背景を保持できる墨消しAPIが揃っていることを確認する。"""
    parameters = inspect.signature(page.apply_redactions).parameters
    required = {"images", "graphics", "text"}
    constants = (
        "PDF_REDACT_IMAGE_NONE",
        "PDF_REDACT_LINE_ART_NONE",
        "PDF_REDACT_TEXT_REMOVE",
    )
    if not required.issubset(parameters) or not all(hasattr(pymupdf, n) for n in constants):
        raise ReplacementError(
            "このPyMuPDFでは、背景を維持したまま文字だけを削除できないため、"
            "処理を中止しました。PyMuPDFを最新版へ更新してください。"
        )


def remove_original_text(pymupdf: Any, page: Any, target_rect: Any) -> None:
    """画像・図形・背景色を変えず、対象矩形内の文字だけを削除する。"""
    ensure_text_only_redaction_supported(pymupdf, page)
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
    pymupdf: Any, page: Any, style: TextStyle, font_path: Path
) -> None:
    """取得した位置・サイズ・色を使い、新しい選択可能な文字列を挿入する。"""
    font_alias = "replacement_japanese_font"
    try:
        page.insert_font(fontname=font_alias, fontfile=str(font_path))
        result = page.insert_text(
            style.origin,
            NEW_TEXT,
            fontsize=style.size,
            fontname=font_alias,
            color=_pdf_color(pymupdf, style.color),
            overlay=True,
        )
    except Exception as exc:
        raise ReplacementError(f"新しい文字列を書き込めませんでした。\n詳細：{exc}") from exc
    if result < 0:
        raise ReplacementError("新しい文字列を書き込めませんでした。")


def _render_hash(page: Any) -> str:
    """ページの見た目を比較するため、等倍RGB画像のハッシュを作る。"""
    pixmap = page.get_pixmap(alpha=False)
    return hashlib.sha256(pixmap.samples).hexdigest()


def snapshot_document(doc: Any) -> DocumentSnapshot:
    """変更前文書のページ構成、テキスト、対象外ページの見た目を記録する。"""
    sizes = tuple((float(page.rect.width), float(page.rect.height)) for page in doc)
    texts = tuple(page.get_text() for page in doc)
    hashes = tuple(
        (index, _render_hash(doc[index]))
        for index in range(doc.page_count)
        if index != TARGET_PAGE_INDEX
    )
    return DocumentSnapshot(doc.page_count, sizes, texts, hashes)


def validate_output_pdf(
    pymupdf: Any, output_path: Path, snapshot: DocumentSnapshot
) -> None:
    """保存したPDFを開き直し、指定外の変更がないことを自動検証する。"""
    try:
        output_doc = pymupdf.open(output_path)
    except Exception as exc:
        raise ReplacementError(f"保存後のPDFを開けませんでした。\n詳細：{exc}") from exc

    try:
        if output_doc.page_count != snapshot.page_count:
            raise ReplacementError("保存後の検証に失敗しました：ページ数が変わっています。")
        output_sizes = tuple(
            (float(page.rect.width), float(page.rect.height)) for page in output_doc
        )
        if output_sizes != snapshot.page_sizes:
            raise ReplacementError("保存後の検証に失敗しました：ページサイズが変わっています。")

        page = output_doc[TARGET_PAGE_INDEX]
        if len(page.search_for(NEW_TEXT)) != 1:
            raise ReplacementError("保存後の検証に失敗しました：変更後文字列が1件ではありません。")
        if page.search_for(OLD_TEXT):
            raise ReplacementError("保存後の検証に失敗しました：変更前文字列が残っています。")
        if not page.search_for(REQUIRED_TEXT):
            raise ReplacementError("保存後の検証に失敗しました：維持する文字列が見つかりません。")

        for index, original_text in enumerate(snapshot.page_texts):
            if index != TARGET_PAGE_INDEX and output_doc[index].get_text() != original_text:
                raise ReplacementError(
                    f"保存後の検証に失敗しました：{index + 1}ページ目の文字列が変わっています。"
                )
        for index, original_hash in snapshot.other_page_render_hashes:
            if _render_hash(output_doc[index]) != original_hash:
                raise ReplacementError(
                    f"保存後の検証に失敗しました：{index + 1}ページ目の見た目が変わっています。"
                )
    finally:
        output_doc.close()


def save_and_validate(
    pymupdf: Any,
    doc: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
) -> None:
    """一時ファイルへ保存し、検証成功後だけ正式名へ変更する。"""
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp.pdf")
    try:
        doc.save(temporary_path, garbage=4, deflate=True)
        validate_output_pdf(pymupdf, temporary_path, snapshot)
        temporary_path.replace(output_path)
    except ReplacementError:
        raise
    except Exception as exc:
        raise ReplacementError(f"PDFを保存できませんでした。\n詳細：{exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    """開講日の差し替えを実行する。"""
    doc: Any | None = None
    try:
        program_dir = Path(__file__).resolve().parent
        input_path = find_input_pdf(program_dir)
        output_path = program_dir / OUTPUT_PDF_NAME
        if output_path.exists():
            raise ReplacementError(
                "出力PDFが既に存在するため、処理を中止しました。\n"
                f"既存ファイル：{output_path.name}"
            )

        pymupdf = load_pymupdf()
        print(f"入力PDF：\n{input_path.name}\n")
        print("対象ページ：\n3ページ目\n")
        print(f"検索文字列：\n{OLD_TEXT}\n")
        doc = open_pdf(pymupdf, input_path)
        snapshot = snapshot_document(doc)
        page = doc[TARGET_PAGE_INDEX]
        target_rect = find_target_text(page)
        style = get_original_text_style(page, target_rect)
        font_path = find_japanese_font(style)
        print(f"使用フォント：\n{font_path}\n")

        remove_original_text(pymupdf, page, target_rect)
        insert_replacement_text(pymupdf, page, style, font_path)
        print(f"変更後文字列：\n{NEW_TEXT}\n")
        save_and_validate(pymupdf, doc, output_path, snapshot)
        print(f"出力PDF：\n{output_path.name}\n")
        print("処理が正常に完了しました。")
        return 0
    except ReplacementError as exc:
        print(f"エラー：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print("エラー：予期しない問題が発生したため、処理を中止しました。", file=sys.stderr)
        print(f"詳細：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if doc is not None:
            doc.close()


if __name__ == "__main__":
    raise SystemExit(main())
