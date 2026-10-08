#!/usr/bin/env python3
"""PDFから通常のテキスト描画を取り除き、背景を保ったPDFを作成します。

使い方::

    python MakeBackgroundImagePdf.py input.pdf output.pdf
    python MakeBackgroundImagePdf.py input.pdf

PyMuPDF が必要です (``python -m pip install pymupdf``)。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterator

try:
    import fitz  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the user's environment
    fitz = None


# 注釈はページ描画ストリームとは別のPDFオブジェクトとして格納される。
# FreeText は注釈内の文字を含み得るため、線・図形等に限定する。
REMOVABLE_ANNOTATION_TYPES = {
    "Line",
    "Ink",
    "Square",
    "Circle",
    "Polygon",
    "PolyLine",
    "Highlight",
    "Underline",
    "Squiggly",
    "StrikeOut",
}

# 添付原稿で確認した図形の位置・色・透明度。色だけでは判定せず、ページと
# バウンディングボックスを含む署名が一致した対象だけを透明な redaction で除去する。
DECORATION_TARGETS = {
    0: [
        ("オレンジ横線", (103.27, 151.83, 252.35, 156.10), (0.6784, 0.3373, 0.1176), 1.0),
        ("右下の矢印 >>>", (669.03, 964.37, 719.34, 991.19), (0.2, 0.149, 0.149), 1.0),
    ],
    1: [
        ("ピンク長方形", (7.00, 491.86, 161.80, 550.93), (1.0, 0.4000, 0.7686), 0.25),
        ("オレンジ横線", (599.64, 684.91, 771.30, 689.83), (0.6784, 0.3373, 0.1176), 1.0),
    ],
    3: [
        ("上部の黄色半透明長方形", (25.21, 584.68, 350.41, 661.25), (1.0, 0.9294, 0.0), 0.45),
        ("下部の黄色半透明長方形", (25.21, 816.72, 289.32, 893.29), (1.0, 0.9294, 0.0), 0.45),
    ],
    4: [
        ("下部の茶色半透明長方形", (336.10, 805.04, 829.98, 977.30), (0.6784, 0.3373, 0.1176), 1.0),
    ],
}

SMILE_SHAPES = (
    ((294.42, 949.23, 347.50, 1003.22), (0.9765, 0.9255, 0.1922)),
    ((309.82, 972.39, 329.16, 982.42), (0.0, 0.0, 0.0)),
    ((292.49, 945.31, 347.96, 997.38), (0.0, 0.0, 0.0)),
    ((308.26, 959.35, 316.24, 967.38), (0.0, 0.0, 0.0)),
    ((323.11, 962.91, 330.63, 970.93), (0.0, 0.0, 0.0)),
)

# 2ページ目中央の黒・緑の線は、原稿内では513×9pxの小さな画像として格納されている。
RASTERIZED_LINE_BBOX = (0.0, 627.0, 513.0, 636.0)


class PdfProcessingError(Exception):
    """入力PDFを安全に処理できない場合のエラー。"""


def _skip_space_and_comments(data: bytes, pos: int) -> int:
    """PDF構文の空白とコメントを読み飛ばす。"""
    length = len(data)
    while pos < length:
        byte = data[pos]
        if byte in b"\x00\t\n\x0c\r ":
            pos += 1
        elif byte == ord("%"):
            newline = data.find(b"\n", pos)
            pos = length if newline < 0 else newline + 1
        else:
            break
    return pos


def _tokens(data: bytes) -> Iterator[tuple[bytes, int, int]]:
    """文字列やコメント内部を除外し、PDF内容ストリームの字句を返す。

    インライン画像は ID から EI までを読み飛ばし、画像バイト内にある
    ``BT`` / ``ET`` を誤認しないようにする。
    """
    pos = 0
    length = len(data)
    while True:
        pos = _skip_space_and_comments(data, pos)
        if pos >= length:
            return
        start = pos
        byte = data[pos]

        if byte == ord("("):
            pos += 1
            depth = 1
            while pos < length and depth:
                if data[pos] == ord("\\"):
                    pos += 2
                    continue
                if data[pos] == ord("("):
                    depth += 1
                elif data[pos] == ord(")"):
                    depth -= 1
                pos += 1
            yield b"<string>", start, pos
            continue

        if byte == ord("<") and not data.startswith(b"<<", pos):
            close = data.find(b">", pos + 1)
            pos = length if close < 0 else close + 1
            yield b"<hex>", start, pos
            continue

        if byte in b"[]{}":
            pos += 2 if byte in b"{}" and data[pos : pos + 2] in (b"<<", b">>") else 1
            yield data[start:pos], start, pos
            continue

        if byte == ord("/"):
            pos += 1
            while pos < length and data[pos] not in b"\x00\t\n\x0c\r ()<>[]{}/%":
                pos += 1
            yield data[start:pos], start, pos
            continue

        while pos < length and data[pos] not in b"\x00\t\n\x0c\r ()<>[]{}/%":
            pos += 1
        if pos == start:  # unexpected delimiter; make forward progress
            pos += 1
        token = data[start:pos]
        yield token, start, pos

        if token == b"ID":
            # IDの後は1つ以上の空白があり、その後にバイナリ画像データが続く。
            if pos < length and data[pos] in b"\x00\t\n\x0c\r ":
                pos += 1
            marker = re.search(rb"\sEI(?=\s|$)", data[pos:])
            if marker is None:
                return
            pos += marker.end()
            yield b"<inline-image>", pos - marker.end(), pos


def _remove_text_objects(contents: bytes) -> tuple[bytes, int]:
    """BTから対応するETまでを削除し、削除したテキストオブジェクト数を返す。"""
    ranges: list[tuple[int, int]] = []
    text_start: int | None = None
    for token, start, end in _tokens(contents):
        if token == b"BT" and text_start is None:
            text_start = start
        elif token == b"ET" and text_start is not None:
            ranges.append((text_start, end))
            text_start = None

    # 不正なPDFでETのないテキストブロックを見つけた場合、後続内容を壊さず中止。
    if text_start is not None:
        raise PdfProcessingError("閉じていないBTテキストブロックがあるため、このPDFは変更しません。")

    if not ranges:
        return contents, 0
    output = bytearray()
    cursor = 0
    for start, end in ranges:
        output.extend(contents[cursor:start])
        output.extend(b"\n")
        cursor = end
    output.extend(contents[cursor:])
    return bytes(output), len(ranges)


def _is_form_xobject(document: "fitz.Document", xref: int) -> bool:
    try:
        kind, value = document.xref_get_key(xref, "Subtype")
    except Exception:
        return False
    return kind == "name" and value == "/Form"


def _form_xobject_refs(document: "fitz.Document", xref: int) -> set[int]:
    """XObjectリソース内の参照先を集める。リソースのPDF表記を構文解析する。"""
    try:
        kind, value = document.xref_get_key(xref, "Resources/XObject")
    except Exception:
        return set()
    if kind != "dict":
        return set()
    return {int(match.group(1)) for match in re.finditer(rb"(?<!\d)(\d+)\s+\d+\s+R", value.encode("latin-1"))}


def _remove_text_from_forms(document: "fitz.Document") -> int:
    """Form XObject内のテキストを再帰的に取り除く。"""
    visited: set[int] = set()
    removed = 0

    def visit(xref: int) -> None:
        nonlocal removed
        if xref in visited or not _is_form_xobject(document, xref):
            return
        visited.add(xref)
        for child in _form_xobject_refs(document, xref):
            visit(child)
        try:
            stream = document.xref_stream(xref)
            if stream is None:
                return
            filtered, count = _remove_text_objects(stream)
            if count:
                document.update_stream(xref, filtered, compress=True)
                removed += count
        except PdfProcessingError:
            raise
        except Exception as exc:
            raise PdfProcessingError(f"Form XObject {xref} を解析できません: {exc}") from exc

    # ページから呼ばれないフォームも含め、文書内のフォームを処理する。
    for xref in range(1, document.xref_length()):
        if _is_form_xobject(document, xref):
            visit(xref)
    return removed


def _default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_背景のみ.pdf")


def _same_page_size(first: fitz.Page, second: fitz.Page) -> bool:
    return abs(first.rect.width - second.rect.width) < 0.01 and abs(first.rect.height - second.rect.height) < 0.01


def _near_bbox(rectangle: "fitz.Rect", expected: tuple[float, float, float, float], tolerance: float = 2.0) -> bool:
    return all(abs(actual - target) <= tolerance for actual, target in zip(rectangle, expected))


def _near_color(actual: tuple[float, ...] | None, expected: tuple[float, float, float]) -> bool:
    return actual is not None and len(actual) >= 3 and all(abs(actual[i] - expected[i]) <= 0.015 for i in range(3))


def _drawing_matches(drawing: dict, bbox: tuple[float, float, float, float], color: tuple[float, float, float], opacity: float) -> bool:
    return (
        _near_bbox(drawing["rect"], bbox)
        and _near_color(drawing.get("fill"), color)
        and abs(float(drawing.get("fill_opacity", 1.0) or 0.0) - opacity) <= 0.06
    )


def _remove_known_decorations(page: "fitz.Page", page_index: int) -> tuple["fitz.Page", list[str]]:
    """指定ページの視覚署名が一致する図形だけを、背景画像を保って除去する。"""
    if page_index == 2:
        return page, ["追加図形除去: 対象なし"]

    drawings = page.get_drawings()
    pending: list[tuple[str, "fitz.Rect", list[tuple[tuple[float, float, float, float], tuple[float, float, float]]]]] = []
    statuses: list[tuple[str, str]] = []

    if page_index == 0:
        matches: list[int] = []
        for bbox, color in SMILE_SHAPES:
            found = [i for i, drawing in enumerate(drawings) if _drawing_matches(drawing, bbox, color, 1.0)]
            if len(found) != 1:
                matches = []
                break
            matches.extend(found)
        if len(matches) == len(SMILE_SHAPES):
            smile_rect = fitz.Rect(292.49, 945.31, 347.96, 1003.22)
            pending.append(("下部の黄色いスマイルマーク", smile_rect, list(SMILE_SHAPES)))
        else:
            statuses.append(("下部の黄色いスマイルマーク", "識別できず未除去"))

    for label, bbox, color, opacity in DECORATION_TARGETS.get(page_index, []):
        matches = [drawing for drawing in drawings if _drawing_matches(drawing, bbox, color, opacity)]
        if len(matches) == 1:
            pending.append((label, fitz.Rect(*bbox), [(bbox, color)]))
        else:
            statuses.append((label, "識別できず未除去"))

    for label, rectangle, _ in pending:
        # fill=None は透明。画像を塗りつぶさず、矩形に触れるベクター描画だけを除去する。
        # 2ptの余白を設け、曲線や線幅によるbbox境界の丸め誤差を吸収する。
        page.add_redact_annot(rectangle + (-2.0, -2.0, 2.0, 2.0), fill=None, cross_out=False)
    if pending:
        page.apply_redactions(images=0, graphics=1, text=1)
        # MuPDFのページ描画キャッシュを更新してから、実際に消えたか確認する。
        page = page.parent.reload_page(page)

    remaining = page.get_drawings()
    for label, _, signatures in pending:
        still_present = any(
            _drawing_matches(drawing, bbox, color, 1.0 if page_index == 0 else next(
                (opacity for target_label, target_bbox, _, opacity in DECORATION_TARGETS.get(page_index, [])
                 if target_label == label and target_bbox == bbox), 1.0
            ))
            for drawing in remaining
            for bbox, color in signatures
        )
        statuses.append((label, "識別した描画を除去" if not still_present else "除去を確認できず"))

    if page_index == 1:
        images = page.get_image_info(xrefs=True)
        raster_match = any(_near_bbox(fitz.Rect(image["bbox"]), RASTERIZED_LINE_BBOX, 1.5) for image in images)
        if raster_match:
            statuses.append(("中央付近の黒色と緑色の横線", "画像に焼き込まれているため未除去"))
        else:
            statuses.append(("中央付近の黒色と緑色の横線", "対象画像を識別できず未除去"))

    return page, [f"{label}: {result}" for label, result in statuses]


def process_pdf(input_path: Path, output_path: Path) -> list[str]:
    """PDFを処理し、ページごとの検証・警告メッセージを返す。"""
    if fitz is None:
        raise PdfProcessingError("PyMuPDFが必要です。`python -m pip install pymupdf` を実行してください。")
    if not input_path.is_file():
        raise PdfProcessingError(f"入力ファイルが見つかりません: {input_path}")
    if input_path.resolve() == output_path.resolve():
        raise PdfProcessingError("入力PDFを保護するため、入力と異なる出力ファイルを指定してください。")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source = fitz.open(input_path)
    except Exception as exc:
        raise PdfProcessingError(f"PDFを開けません: {exc}") from exc
    if source.is_encrypted:
        source.close()
        raise PdfProcessingError("暗号化されたPDFは処理できません。")

    page_details: list[tuple[int, int, int, int, int, list[str]]] = []
    warnings: list[str] = []
    original_sizes = [(page.rect.width, page.rect.height) for page in source]
    page_count = source.page_count
    try:
        removed_forms = _remove_text_from_forms(source)
        for page_index in range(page_count):
            page = source[page_index]
            content_xrefs = page.get_contents()
            text_objects = 0
            if content_xrefs:
                # /Contents の各ストリームは連結して解釈される。BT/ETが
                # ストリーム境界をまたぐ場合も扱えるよう、順序通りに結合する。
                original_stream = b"\n".join(source.xref_stream(ref) or b"" for ref in content_xrefs)
                filtered, text_objects = _remove_text_objects(original_stream)
                source.update_stream(content_xrefs[0], filtered, compress=True)
                source.xref_set_key(page.xref, "Contents", f"{content_xrefs[0]} 0 R")

            removed_annotations = 0
            annotation = page.first_annot
            while annotation is not None:
                following = annotation.next
                if annotation.type[1] in REMOVABLE_ANNOTATION_TYPES:
                    page.delete_annot(annotation)
                    removed_annotations += 1
                annotation = following

            page, decoration_statuses = _remove_known_decorations(page, page_index)

            residual_text = len(page.get_text("text").strip())
            try:
                drawing_count = len(page.get_drawings())
            except Exception:
                drawing_count = -1
            page_details.append((text_objects, removed_annotations, residual_text, drawing_count, len(page.get_images(full=True)), decoration_statuses))

        # 一時ファイルに保存し、再度開けることを確認してから出力先へ移す。
        with tempfile.NamedTemporaryFile(prefix="background_pdf_", suffix=".pdf", dir=output_path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            source.save(temp_path, garbage=4, deflate=True)
        finally:
            source.close()

        try:
            with fitz.open(temp_path) as check:
                if check.page_count != page_count:
                    raise PdfProcessingError("保存後のページ数が元PDFと一致しません。")
                for index, (width, height) in enumerate(original_sizes):
                    page = check[index]
                    if abs(page.rect.width - width) >= 0.01 or abs(page.rect.height - height) >= 0.01:
                        raise PdfProcessingError(f"{index + 1}ページ目のサイズが元PDFと一致しません。")
                    if page.get_text("text").strip():
                        warnings.append(f"{index + 1}ページ目: 一部の文字が残っています（注釈・特殊構造等の可能性）。")
            os.replace(temp_path, output_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    except PdfProcessingError:
        source.close()
        raise
    except Exception as exc:
        source.close()
        raise PdfProcessingError(f"PDFを処理できませんでした: {exc}") from exc

    messages = [f"出力しました: {output_path}", f"ページ数: {page_count}（維持を確認）", f"Form XObject内のテキストブロック除去数: {removed_forms}"]
    for index, (text_objects, annotations, residual, drawings, images, decorations) in enumerate(page_details, 1):
        width, height = original_sizes[index - 1]
        messages.append(
            f"{index}ページ目: サイズ {width:.2f} × {height:.2f} pt（維持）, "
            f"テキストブロック除去 {text_objects}, 図形注釈除去 {annotations}, "
            f"残存テキスト文字数 {residual}, 描画要素 {drawings}, 画像 {images}"
        )
        messages.extend(f"  {status}" for status in decorations)
        for status in decorations:
            if "未除去" in status or "識別できず" in status or "確認できず" in status:
                warnings.append(f"{index}ページ目: {status}")
        if drawings > 0:
            warnings.append(
                f"{index}ページ目: コンテンツストリーム内にベクター描画要素が残っています。"
                "それが後から追加された線・図形か背景要素かをPDF構造だけでは識別できないため、保持しました。"
            )
        if images:
            warnings.append(
                f"{index}ページ目: 画像に焼き込まれた文字・線・図形は自動除去していません。"
            )
    messages.extend(f"警告: {warning}" for warning in dict.fromkeys(warnings))
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDFから通常の文字を除去し、背景を保ったPDFを作成します。")
    parser.add_argument("input", type=Path, help="入力PDF")
    parser.add_argument("output", type=Path, nargs="?", help="出力PDF（省略時は *_背景のみ.pdf）")
    args = parser.parse_args(argv)
    output_path = args.output or _default_output_path(args.input)
    try:
        for message in process_pdf(args.input, output_path):
            print(message)
    except PdfProcessingError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
