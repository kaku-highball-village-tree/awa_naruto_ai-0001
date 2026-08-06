#!/usr/bin/env python3
"""印刷用チラシPDFの指定された5箇所だけを安全に差し替えるコマンド。

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
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


INPUT_PDF_NAME = "チラシ用_阿波なるとAI塾_編集前の原稿.pdf"
OUTPUT_PDF_NAME = "チラシ用_阿波なるとAI塾_変更後.pdf"
ERROR_FILE_NAME = "チラシ用_阿波なるとAI塾_変更後_error.txt"

ADDRESS_PAGE_INDEX = 0
OLD_ADDRESS_TEXT = "鳴⾨市⽊津町⽊津野7-11"
NEW_ADDRESS_TEXT = "鳴門市大津町木津野内田7-11"

OPENING_DATE_PAGE_INDEX = 1
OLD_CHILD_OPENING_DATE_TEXT = "令和８年８月６日開講"
NEW_CHILD_OPENING_DATE_TEXT = "令和８年９月３日開講／令和８年10月８日開講"

OLD_GENERAL_OPENING_DATE_TEXT = "令和８年９月４日開講"
NEW_GENERAL_OPENING_DATE_TEXT = "令和８年９月４日開講／令和８年９月18日開講"

OLD_GENERAL_SCHEDULE_TEXT = "第１・第２金曜日　１８：３０～２０：３０"
NEW_GENERAL_SCHEDULE_LINES = (
    "第1･第2金曜日 18:30～20:30",
    "第3･第4金曜日 18:30～20:30",
)
GENERAL_MOVABLE_TEXTS = (
    "全２回：１回２時間",
    "受講料",
    "５５００円×２回分＝１１０００円　(税込１０％)",
)

RECEPTION_START_PAGE_INDEX = 1
OLD_RECEPTION_START_TEXT = "令和８年７月１日より受付開始"
NEW_RECEPTION_START_TEXT = "令和８年８月１日より受付開始"

COMPUTER_NOTE_PAGE_INDEX = 1
OLD_COMPUTER_NOTE_TEXT = "※どちらか一方でも可"
NEW_COMPUTER_NOTE_TEXT = "※どちらか一方でも可(パソコン推奨)"

REQUIRED_WEEKLY_TEXT = "毎週木曜日"
REQUIRED_RECEPTION_TEXTS = ("募集期間", "(各コース開講前まで応募可能)")
EXPECTED_PAGE_COUNT = 2
MAX_OTHER_FONT_REDUCTION_RATIO = 0.05
MAX_SCHEDULE_FONT_REDUCTION_RATIO = 0.10
MULTILINE_MIN_SPACING_RATIO = 1.15
MULTILINE_MAX_SPACING_RATIO = 1.30
MULTILINE_SPACING_STEP = 0.01
MINIMUM_FOLLOWING_GAP = 4.0
MULTILINE_MIN_GAP_RATIO = 0.25
COMPARISON_DPI = 144
DISPLAY_GROUP_OVERLAP_RATIO = 0.8


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
    new_lines: tuple[str, ...]

    @property
    def new_text(self) -> str:
        """ログとエラー情報向けに変更後文字列を改行付きで返す。"""
        return "\n".join(self.new_lines)


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
class SearchGroup:
    """同じ表示位置に重なった検索矩形のグループ。"""

    rectangles: tuple[Any, ...]
    union_rect: Any


@dataclass(frozen=True)
class TextLayer:
    """旧文字列を構成する1つの内部テキストレイヤー。"""

    text: str
    rect: Any
    font: str
    size: float
    color: int
    flags: int
    origin: tuple[float, float]
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
    deletion_rectangles: tuple[Any, ...]
    line_advance: float


@dataclass(frozen=True)
class PreparedMove:
    """一般コース欄内で必要な場合だけ下へ移動する既存行。"""

    text: str
    page_index: int
    style: TextStyle
    font_path: Path
    deletion_rectangles: tuple[Any, ...]
    y_offset: float
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
        "住所",
        ADDRESS_PAGE_INDEX,
        OLD_ADDRESS_TEXT,
        (NEW_ADDRESS_TEXT,),
    ),
    ReplacementSpec(
        "児童コース開講日",
        OPENING_DATE_PAGE_INDEX,
        OLD_CHILD_OPENING_DATE_TEXT,
        (NEW_CHILD_OPENING_DATE_TEXT,),
    ),
    ReplacementSpec(
        "一般コース開講日",
        OPENING_DATE_PAGE_INDEX,
        OLD_GENERAL_OPENING_DATE_TEXT,
        (NEW_GENERAL_OPENING_DATE_TEXT,),
    ),
    ReplacementSpec(
        "一般コース受講日時",
        OPENING_DATE_PAGE_INDEX,
        OLD_GENERAL_SCHEDULE_TEXT,
        NEW_GENERAL_SCHEDULE_LINES,
    ),
    ReplacementSpec(
        "受付開始日",
        RECEPTION_START_PAGE_INDEX,
        OLD_RECEPTION_START_TEXT,
        (NEW_RECEPTION_START_TEXT,),
    ),
    ReplacementSpec(
        "パソコン推奨注意書き",
        COMPUTER_NOTE_PAGE_INDEX,
        OLD_COMPUTER_NOTE_TEXT,
        (NEW_COMPUTER_NOTE_TEXT,),
    ),
)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-comparison",
        action="store_true",
        help="1・2ページ目の変更前後を確認用PNGとして出力します。",
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


def inspect_print_pdf_structure(doc: Any) -> None:
    """印刷用PDFを実測し、ページ構造と全検索対象の所在をログへ出す。"""
    print(f"ページ数：{doc.page_count}")
    for page_index, page in enumerate(doc):
        width = float(page.rect.width)
        height = float(page.rect.height)
        orientation = "横向き" if width > height else "縦向き"
        drawings = page.get_drawings()
        print(
            f"{page_index + 1}ページ目：幅={width}、高さ={height}、"
            f"向き={orientation}、回転={page.rotation}度"
        )
        print(f"{page_index + 1}ページ目の図形数：{len(drawings)}件")

    for spec in REPLACEMENTS:
        print(f"所在確認：{spec.label}／検索文字列：{spec.old_text}")
        found_page_indexes: list[int] = []
        for page_index, page in enumerate(doc):
            rectangles = tuple(page.search_for(spec.old_text))
            groups = group_overlapping_rectangles(rectangles)
            print(
                f"  {page_index + 1}ページ目：生の検出件数={len(rectangles)}件、"
                f"表示グループ数={len(groups)}件"
            )
            for number, rectangle in enumerate(rectangles, start=1):
                print(f"    検索矩形 {number}：{tuple(rectangle)}")
            if groups:
                found_page_indexes.append(page_index)
        if found_page_indexes != [spec.page_index]:
            displayed_pages = "、".join(
                f"{page_index + 1}ページ目" for page_index in found_page_indexes
            ) or "なし"
            raise ReplacementError(
                "変更対象が指定ページだけに存在することを確認できません。",
                f"処理対象：{spec.label}／必要ページ：{spec.page_index + 1}ページ目"
                f"／検出ページ：{displayed_pages}",
            )
    print("住所が1ページ目、その他5つの変更対象が2ページ目にあることを確認しました。\n")


def overlap_ratio(rectangle1: Any, rectangle2: Any) -> float:
    """交差面積が小さい側の矩形面積に占める割合を返す。"""
    area1 = rectangle1.get_area()
    area2 = rectangle2.get_area()
    if area1 <= 0 or area2 <= 0:
        return 0.0
    intersection = rectangle1 & rectangle2
    if intersection.is_empty:
        return 0.0
    return intersection.get_area() / min(area1, area2)


def group_overlapping_rectangles(rectangles: Sequence[Any]) -> tuple[SearchGroup, ...]:
    """80%以上重なる矩形を推移的な表示グループへまとめる。"""
    if not rectangles:
        return ()
    remaining = set(range(len(rectangles)))
    groups: list[SearchGroup] = []
    while remaining:
        pending = [remaining.pop()]
        members: list[int] = []
        while pending:
            current = pending.pop()
            members.append(current)
            connected = {
                candidate
                for candidate in remaining
                if overlap_ratio(rectangles[current], rectangles[candidate])
                >= DISPLAY_GROUP_OVERLAP_RATIO
            }
            remaining.difference_update(connected)
            pending.extend(connected)
        member_rectangles = tuple(rectangles[index] for index in sorted(members))
        union_rect = member_rectangles[0].__class__(member_rectangles[0])
        for rectangle in member_rectangles[1:]:
            union_rect |= rectangle
        groups.append(SearchGroup(member_rectangles, union_rect))
    return tuple(groups)


def find_target_text(page: Any, spec: ReplacementSpec) -> SearchGroup:
    """旧文字列を検索し、表示位置が厳密に1グループの場合だけ返す。"""
    rectangles = tuple(page.search_for(spec.old_text))
    print(f"変更対象：{spec.label}")
    print(f"対象ページ：{spec.page_index + 1}ページ目")
    print(f"検索文字列：{spec.old_text}")
    print(f"生の検出件数：{len(rectangles)}件")
    for number, rectangle in enumerate(rectangles, start=1):
        print(f"検索矩形 {number}：{tuple(rectangle)}")
    if not rectangles:
        raise ReplacementError(
            "変更対象の文字列が見つかりませんでした。",
            f"対象ページ：{spec.page_index + 1}ページ目／検索文字列：{spec.old_text}",
        )
    if any(rectangle.get_area() <= 0 for rectangle in rectangles):
        raise ReplacementError(
            "変更対象の検索結果に不正な矩形が含まれています。",
            f"処理対象：{spec.label}",
        )
    groups = group_overlapping_rectangles(rectangles)
    print(f"表示グループ数：{len(groups)}件")
    if len(groups) != 1:
        raise ReplacementError(
            "変更対象が異なる表示位置に複数見つかったため、処理を中止しました。",
            f"対象ページ：{spec.page_index + 1}ページ目／表示グループ数：{len(groups)}件",
        )
    print(f"表示グループの和集合：{tuple(groups[0].union_rect)}")
    return groups[0]


def normalize_whitespace_for_comparison(text: str) -> str:
    """比較時だけ空白除去とNFKC正規化を行い、PDFへ書く文字は変えない。"""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("・", "･")
    normalized = normalized.replace("〜", "～")
    normalized = normalized.replace("~", "～")
    return "".join(character for character in normalized if not character.isspace())


def _matching_text_layers(
    page: Any, spec: ReplacementSpec, search_group: SearchGroup
) -> tuple[TextLayer, ...]:
    """同一行の複数spanを連結し、空白を除いて一致するレイヤーを取得する。"""
    layers: list[TextLayer] = []
    normalized_old_text = normalize_whitespace_for_comparison(spec.old_text)
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            ordered_spans = sorted(
                line.get("spans", []),
                key=lambda span: (
                    round(float(span["bbox"][1]), 1),
                    float(span["bbox"][0]),
                ),
            )
            for span in ordered_spans:
                for character in span.get("chars", []):
                    character_text = str(character.get("c", ""))
                    character_rect = page.rect.__class__(character["bbox"])
                    if character_text.isspace() or character_rect.intersects(
                        search_group.union_rect
                    ):
                        candidates.append((character, span))
            combined_text = "".join(
                str(character.get("c", "")) for character, _ in candidates
            )
            if (
                not candidates
                or normalize_whitespace_for_comparison(combined_text)
                != normalized_old_text
            ):
                continue

            visible_candidates = [
                item
                for item in candidates
                if not str(item[0].get("c", "")).isspace()
            ]
            if not visible_candidates:
                continue
            rect = page.rect.__class__(visible_candidates[0][0]["bbox"])
            span_counts: dict[int, int] = {}
            spans_by_id: dict[int, dict[str, Any]] = {}
            for character, span in visible_candidates:
                rect |= page.rect.__class__(character["bbox"])
                span_id = id(span)
                span_counts[span_id] = span_counts.get(span_id, 0) + 1
                spans_by_id[span_id] = span
            representative_span = spans_by_id[
                max(span_counts, key=lambda span_id: span_counts[span_id])
            ]
            first_character = visible_candidates[0][0]
            origin_value = first_character.get(
                "origin", representative_span.get("origin", (rect.x0, rect.y1))
            )
            layers.append(
                TextLayer(
                    spec.old_text,
                    rect,
                    str(representative_span.get("font", "")),
                    float(representative_span.get("size", 0.0)),
                    int(representative_span.get("color", 0)),
                    int(representative_span.get("flags", 0)),
                    (float(origin_value[0]), float(origin_value[1])),
                    representative_span.get("ascender"),
                    representative_span.get("descender"),
                )
            )
    return tuple(layers)


def _choose_representative_layer(layers: Sequence[TextLayer]) -> TextLayer:
    """重複レイヤーから多数派かつType3でない代表書式を選ぶ。"""
    if not layers:
        raise ReplacementError("元の文字書式を取得できませんでした。")
    signatures: dict[tuple[str, float, int, int], int] = {}
    for layer in layers:
        signature = (layer.font, layer.size, layer.color, layer.flags)
        signatures[signature] = signatures.get(signature, 0) + 1
    return max(
        layers,
        key=lambda layer: (
            signatures[(layer.font, layer.size, layer.color, layer.flags)],
            "t3" not in layer.font.lower() and "type3" not in layer.font.lower(),
            layer.rect.get_area() > 0,
        ),
    )


def _ensure_deletion_rectangles_are_safe(
    page: Any,
    layers: Sequence[TextLayer],
    search_group: SearchGroup,
    spec: ReplacementSpec,
) -> None:
    """交差span群の空白除去後文字列と座標が対象に一致することを確認する。"""
    normalized_old_text = normalize_whitespace_for_comparison(spec.old_text)
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            intersecting_spans = []
            for span in line.get("spans", []):
                span_text = str(span.get("text", ""))
                span_rect = page.rect.__class__(span["bbox"])
                if span_text and any(layer.rect.intersects(span_rect) for layer in layers):
                    intersecting_spans.append((span, span_rect))
            if not intersecting_spans:
                continue
            intersecting_spans.sort(
                key=lambda item: (
                    round(float(item[0]["bbox"][1]), 1),
                    float(item[0]["bbox"][0]),
                )
            )
            normalized_span_texts = [
                normalize_whitespace_for_comparison(str(span.get("text", "")))
                for span, _ in intersecting_spans
            ]
            combined_text = "".join(
                str(span.get("text", "")) for span, _ in intersecting_spans
            )
            text_matches = (
                all(text == normalized_old_text for text in normalized_span_texts)
                or normalize_whitespace_for_comparison(combined_text)
                == normalized_old_text
            )
            combined_rect = page.rect.__class__(intersecting_spans[0][1])
            for _, span_rect in intersecting_spans[1:]:
                combined_rect |= span_rect
            position_matches = (
                overlap_ratio(combined_rect, search_group.union_rect) >= 0.9
            )
            if not text_matches or not position_matches:
                raise ReplacementError(
                    "変更対象以外の文字を削除する可能性があるため、処理を中止しました。",
                    f"処理対象：{spec.label}／交差文字列：{combined_text}",
                )


def get_original_text_style(
    page: Any, search_group: SearchGroup, spec: ReplacementSpec
) -> tuple[TextStyle, tuple[Any, ...]]:
    """重複レイヤーを調査し、代表書式と最小削除矩形を取得する。"""
    layers = tuple(
        layer
        for layer in _matching_text_layers(page, spec, search_group)
        if overlap_ratio(layer.rect, search_group.union_rect) >= 0.9
    )
    if not layers:
        raise ReplacementError(
            "元の文字書式を取得できませんでした。",
            f"処理対象：{spec.label}",
        )
    layer_groups = group_overlapping_rectangles(tuple(layer.rect for layer in layers))
    if len(layer_groups) != 1:
        raise ReplacementError(
            "対象文字列の内部レイヤーが異なる表示位置に存在します。",
            f"処理対象：{spec.label}／レイヤーグループ数：{len(layer_groups)}件",
        )
    # rawdictの文字レイヤーもsearch_for()の唯一の表示グループと重なる必要がある。
    if overlap_ratio(layer_groups[0].union_rect, search_group.union_rect) <= 0:
        raise ReplacementError(
            "検索結果と内部文字レイヤーの位置が一致しません。",
            f"処理対象：{spec.label}",
        )
    _ensure_deletion_rectangles_are_safe(page, layers, search_group, spec)
    representative = _choose_representative_layer(layers)
    print(f"内部文字レイヤー数：{len(layers)}件")
    for number, layer in enumerate(layers, start=1):
        print(f"layer {number} 文字列：{layer.text}")
        print(f"layer {number} フォント名：{layer.font}")
        print(f"layer {number} フォントサイズ：{layer.size}")
        print(f"layer {number} 文字色：{layer.color}")
        print(f"layer {number} flags：{layer.flags}")
        print(f"layer {number} bbox：{tuple(layer.rect)}")
        print(f"layer {number} origin：{layer.origin}")
    print(f"代表フォント：{representative.font}")
    print()
    style = TextStyle(
        bbox=layer_groups[0].union_rect,
        origin=representative.origin,
        font=representative.font,
        size=representative.size,
        color=representative.color,
        flags=representative.flags,
        ascender=representative.ascender,
        descender=representative.descender,
    )
    return style, tuple(layer.rect for layer in layers)


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


def _span_rectangles(
    page: Any, excluded_rect: Any, ignored_texts: Sequence[str] = ()
) -> list[tuple[Any, str]]:
    """置換対象・移動予定行以外の文字span矩形と文字列を返す。"""
    rectangles: list[tuple[Any, str]] = []
    normalized_ignored_texts = {
        normalize_whitespace_for_comparison(text) for text in ignored_texts
    }
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                rect = excluded_rect.__class__(span["bbox"])
                text = str(span.get("text", ""))
                normalized_text = normalize_whitespace_for_comparison(text)
                if (
                    normalized_text
                    and normalized_text not in normalized_ignored_texts
                    and not rect.intersects(excluded_rect)
                ):
                    rectangles.append((rect, text))
    return rectangles


def _crosses_new_graphics(page: Any, old_rect: Any, new_rect: Any) -> bool:
    """新しい図形への侵入と、元の背景領域からのはみ出しを確認する。"""
    containing_rectangles = []
    for drawing in page.get_drawings():
        drawing_rect = drawing.get("rect")
        if drawing_rect is not None and drawing_rect.contains(old_rect):
            containing_rectangles.append(drawing_rect)
        if (
            drawing_rect is not None
            and new_rect.intersects(drawing_rect)
            and not old_rect.intersects(drawing_rect)
        ):
            return True
    if containing_rectangles and not any(
        drawing_rect.contains(new_rect) for drawing_rect in containing_rectangles
    ):
        return True
    return False


def calculate_placement(
    pymupdf: Any,
    page: Any,
    style: TextStyle,
    font_path: Path,
    spec: ReplacementSpec,
) -> tuple[float, Any, float]:
    """対象別の最大縮小率と安全な行間で、最大の文字サイズを選ぶ。"""
    try:
        font = pymupdf.Font(fontfile=str(font_path))
    except Exception as exc:
        raise ReplacementError("日本語フォントを読み込めませんでした。", str(exc)) from exc

    ignored_texts = GENERAL_MOVABLE_TEXTS if len(spec.new_lines) > 1 else ()
    other_spans = _span_rectangles(page, style.bbox, ignored_texts)
    last_failure = ""
    max_reduction_ratio = (
        MAX_SCHEDULE_FONT_REDUCTION_RATIO
        if len(spec.new_lines) > 1
        else MAX_OTHER_FONT_REDUCTION_RATIO
    )
    maximum_step = round(max_reduction_ratio * 100)
    font_ascender = float(getattr(font, "ascender", style.ascender or 1.0))
    font_descender = float(getattr(font, "descender", style.descender or -0.25))
    spacing_ratios = [0.0]
    if len(spec.new_lines) > 1:
        spacing_step_count = round(
            (MULTILINE_MAX_SPACING_RATIO - MULTILINE_MIN_SPACING_RATIO)
            / MULTILINE_SPACING_STEP
        )
        spacing_ratios = [
            MULTILINE_MIN_SPACING_RATIO + index * MULTILINE_SPACING_STEP
            for index in range(spacing_step_count + 1)
        ]

    for step in range(maximum_step + 1):
        font_size = style.size * (1.0 - step / 100.0)
        top = style.origin[1] - font_size * font_ascender
        bottom = style.origin[1] - font_size * font_descender
        for spacing_ratio in spacing_ratios:
            line_advance = font_size * spacing_ratio if len(spec.new_lines) > 1 else 0.0
            line_rectangles = []
            for line_number, line_text in enumerate(spec.new_lines):
                width = font.text_length(line_text, fontsize=font_size)
                y_offset = line_number * line_advance
                line_rectangles.append(
                    pymupdf.Rect(
                        style.origin[0],
                        min(top, bottom) + y_offset,
                        style.origin[0] + width,
                        max(top, bottom) + y_offset,
                    )
                )
            if any(
                first.intersects(second)
                for index, first in enumerate(line_rectangles)
                for second in line_rectangles[index + 1 :]
            ):
                last_failure = "変更後の行同士が重なります。"
                continue
            changed_rect = pymupdf.Rect(line_rectangles[0])
            for line_rect in line_rectangles[1:]:
                changed_rect |= line_rect
            if not page.rect.contains(changed_rect):
                last_failure = "ページ領域からはみ出します。"
                continue
            if _crosses_new_graphics(page, style.bbox, changed_rect):
                last_failure = "一般コース欄または元の背景領域からはみ出します。"
                continue
            colliding_texts = [
                text
                for rect, text in other_spans
                if any(rect.intersects(line_rect) for line_rect in line_rectangles)
            ]
            if colliding_texts:
                last_failure = f"交差文字列：{' / '.join(colliding_texts)}"
                continue
            return font_size, changed_rect | style.bbox, line_advance

    raise ReplacementError(
        "変更後文字列を元の位置へ安全に配置できません。",
        f"処理対象：{spec.label}。{last_failure or '配置条件を満たしません。'}",
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
    """6件すべてを編集前に検査し、部分的な変更を防ぐ。"""
    prepared: list[PreparedReplacement] = []
    for spec in REPLACEMENTS:
        page = doc[spec.page_index]
        search_group = find_target_text(page, spec)
        style, deletion_rectangles = get_original_text_style(page, search_group, spec)
        ensure_text_only_redaction_supported(pymupdf, page)
        font_path = find_japanese_font(style, spec)
        font_size, changed_rect, line_advance = calculate_placement(
            pymupdf, page, style, font_path, spec
        )
        print(f"使用フォント：{font_path}")
        print(f"変更後文字列：{spec.new_text}")
        print(f"挿入文字サイズ：{font_size}\n")
        if len(spec.new_lines) > 1:
            print(f"2行のベースライン間隔：{line_advance}")
            print(f"行間倍率：{line_advance / font_size}\n")
        prepared.append(
            PreparedReplacement(
                spec,
                style,
                font_path,
                font_size,
                changed_rect,
                deletion_rectangles,
                line_advance,
            )
        )
    return tuple(prepared)


def _find_closest_group_below(page: Any, text: str, reference_rect: Any) -> SearchGroup:
    """同じ文字が複数欄にあっても、一般コース対象の直下を安全に選ぶ。"""
    rectangles = tuple(page.search_for(text))
    groups = group_overlapping_rectangles(rectangles)
    candidates = [group for group in groups if group.union_rect.y0 >= reference_rect.y0]
    if not candidates:
        raise ReplacementError(
            "移動対象の後続行が見つかりませんでした。", f"検索文字列：{text}"
        )
    candidates.sort(key=lambda group: group.union_rect.y0)
    if len(candidates) > 1 and abs(
        candidates[0].union_rect.y0 - candidates[1].union_rect.y0
    ) < 1.0:
        raise ReplacementError(
            "移動対象を一意に特定できませんでした。", f"検索文字列：{text}"
        )
    return candidates[0]


def prepare_following_line_moves(
    pymupdf: Any,
    doc: Any,
    replacements: Sequence[PreparedReplacement],
) -> tuple[PreparedMove, ...]:
    """2行目と重なる場合だけ、一般コースの後続行を同量だけ下へ移動する。"""
    schedule = next(
        item for item in replacements if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
    )
    page = doc[schedule.spec.page_index]
    first_group = _find_closest_group_below(
        page, GENERAL_MOVABLE_TEXTS[0], schedule.style.bbox
    )
    minimum_gap = max(
        MINIMUM_FOLLOWING_GAP,
        schedule.font_size * MULTILINE_MIN_GAP_RATIO,
    )
    required_top = schedule.changed_rect.y1 + minimum_gap
    y_offset = max(0.0, required_top - first_group.union_rect.y0)
    if y_offset <= 0:
        return ()

    moves: list[PreparedMove] = []
    ignored_texts = set(GENERAL_MOVABLE_TEXTS)
    ignored_texts.update(spec.old_text for spec in REPLACEMENTS)
    normalized_ignored_texts = {
        normalize_whitespace_for_comparison(text) for text in ignored_texts
    }
    for text in GENERAL_MOVABLE_TEXTS:
        group = _find_closest_group_below(page, text, schedule.style.bbox)
        move_spec = ReplacementSpec(
            f"一般コース後続行「{text}」",
            schedule.spec.page_index,
            text,
            (text,),
        )
        style, deletion_rectangles = get_original_text_style(page, group, move_spec)
        font_path = find_japanese_font(style, move_spec)
        new_rect = style.bbox.__class__(
            style.bbox.x0,
            style.bbox.y0 + y_offset,
            style.bbox.x1,
            style.bbox.y1 + y_offset,
        )
        if not page.rect.contains(new_rect):
            raise ReplacementError(
                "一般コースの後続行を欄内へ移動できません。",
                f"移動対象：{text}",
            )
        if _crosses_new_graphics(page, style.bbox, new_rect):
            raise ReplacementError(
                "一般コースの後続行が背景線または図形へ重なるため移動できません。",
                f"移動対象：{text}",
            )
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_text = str(span.get("text", ""))
                    normalized_span_text = normalize_whitespace_for_comparison(span_text)
                    if (
                        not normalized_span_text
                        or normalized_span_text in normalized_ignored_texts
                    ):
                        continue
                    span_rect = page.rect.__class__(span["bbox"])
                    if new_rect.intersects(span_rect):
                        raise ReplacementError(
                            "一般コースの後続行を安全に移動できません。",
                            f"移動対象：{text}／交差文字列：{span_text}",
                        )
        moves.append(
            PreparedMove(
                text,
                schedule.spec.page_index,
                style,
                font_path,
                deletion_rectangles,
                y_offset,
                style.bbox | new_rect,
            )
        )
    print(f"一般コース後続行のY方向移動量：{y_offset}")
    print(f"一般コース受講日時と後続行の最低余白：{minimum_gap}")
    return tuple(moves)


def remove_original_text(
    pymupdf: Any, page: Any, target_rectangles: Sequence[Any]
) -> None:
    """画像・図形・背景色を変えず、全重複レイヤーの文字だけを削除する。"""
    for target_rect in target_rectangles:
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
        results = []
        for line_number, line_text in enumerate(prepared.spec.new_lines):
            origin = (
                prepared.style.origin[0],
                prepared.style.origin[1] + line_number * prepared.line_advance,
            )
            results.append(
                page.insert_text(
                    origin,
                    line_text,
                    fontsize=prepared.font_size,
                    fontname=font_alias,
                    color=_pdf_color(pymupdf, prepared.style.color),
                    overlay=True,
                )
            )
    except Exception as exc:
        raise ReplacementError(
            "新しい文字列を書き込めませんでした。",
            f"処理対象：{prepared.spec.label}／詳細：{exc}",
        ) from exc
    if any(result < 0 for result in results):
        raise ReplacementError(
            "新しい文字列を書き込めませんでした。",
            f"処理対象：{prepared.spec.label}",
        )


def insert_moved_text(
    pymupdf: Any, page: Any, move: PreparedMove, font_number: int
) -> None:
    """後続行の内容と書式を変えず、必要最小限だけ下へ再配置する。"""
    alias = f"moved_japanese_font_{font_number}"
    try:
        page.insert_font(fontname=alias, fontfile=str(move.font_path))
        result = page.insert_text(
            (move.style.origin[0], move.style.origin[1] + move.y_offset),
            move.text,
            fontsize=move.style.size,
            fontname=alias,
            color=_pdf_color(pymupdf, move.style.color),
            overlay=True,
        )
    except Exception as exc:
        raise ReplacementError(
            "一般コースの後続行を移動できませんでした。",
            f"移動対象：{move.text}／詳細：{exc}",
        ) from exc
    if result < 0:
        raise ReplacementError(
            "一般コースの後続行を移動できませんでした。",
            f"移動対象：{move.text}",
        )


def apply_replacements(
    pymupdf: Any,
    doc: Any,
    prepared: Sequence[PreparedReplacement],
    moves: Sequence[PreparedMove],
) -> None:
    """先に全旧文字を削除し、新文字が後続redactionで消えないよう挿入する。"""
    deletion_by_page: dict[int, list[Any]] = {}
    for item in prepared:
        deletion_by_page.setdefault(item.spec.page_index, []).extend(
            item.deletion_rectangles
        )
    for move in moves:
        deletion_by_page.setdefault(move.page_index, []).extend(move.deletion_rectangles)

    for page_index, rectangles in deletion_by_page.items():
        remove_original_text(pymupdf, doc[page_index], rectangles)

    for font_number, item in enumerate(prepared, start=1):
        page = doc[item.spec.page_index]
        insert_replacement_text(pymupdf, page, item, font_number)
    for font_number, move in enumerate(moves, start=1):
        page = doc[move.page_index]
        insert_moved_text(pymupdf, page, move, font_number)


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


def _validate_edited_page_outside_rectangles(
    page: Any,
    original: tuple[int, int, int, int, int, bytes],
    allowed_rectangles: Sequence[Any],
) -> None:
    """複数の編集対象矩形の外側に視覚的な変化がないことを確認する。"""
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

    current_samples = pixmap.samples
    for y in range(height):
        row_start = y * stride
        row_end = row_start + stride
        intervals = []
        for rectangle in allowed_rectangles:
            top = max(0, int(rectangle.y0) - 2)
            bottom = min(height, int(rectangle.y1 + 0.9999) + 2)
            if top <= y < bottom:
                intervals.append(
                    (
                        max(0, int(rectangle.x0) - 2),
                        min(width, int(rectangle.x1 + 0.9999) + 2),
                    )
                )
        intervals.sort()
        merged: list[tuple[int, int]] = []
        for left, right in intervals:
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        cursor = 0
        for left, right in merged:
            start = row_start + cursor * components
            end = row_start + left * components
            if original_samples[start:end] != current_samples[start:end]:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    "指定された文字列の矩形外で見た目が変わっています。",
                )
            cursor = max(cursor, right)
        tail_start = row_start + cursor * components
        if original_samples[tail_start:row_end] != current_samples[tail_start:row_end]:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "指定された文字列の矩形外で見た目が変わっています。",
            )


def _has_standalone_text_layer(page: Any, text: str) -> bool:
    """別文字列の一部ではなく、単独spanとして旧文字列が残っているか確認する。"""
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = "".join(char.get("c", "") for char in span.get("chars", []))
                if span_text == text:
                    return True
    return False


def _expanded_rect(rect: Any, margin_x: float, margin_y: float) -> Any:
    """検索失敗時の周辺span確認用に矩形を少し広げる。"""
    return rect.__class__(
        rect.x0 - margin_x,
        rect.y0 - margin_y,
        rect.x1 + margin_x,
        rect.y1 + margin_y,
    )


def _line_rectangles_for_validation(pymupdf: Any, prepared: PreparedReplacement) -> tuple[Any, ...]:
    """保存後検証用に、挿入時と同じ基準で各行の想定bboxを再計算する。"""
    try:
        font = pymupdf.Font(fontfile=str(prepared.font_path))
    except Exception:
        font = None
    font_ascender = float(getattr(font, "ascender", prepared.style.ascender or 1.0))
    font_descender = float(getattr(font, "descender", prepared.style.descender or -0.25))
    top = prepared.style.origin[1] - prepared.font_size * font_ascender
    bottom = prepared.style.origin[1] - prepared.font_size * font_descender
    rectangles = []
    for line_number, line_text in enumerate(prepared.spec.new_lines):
        if font is not None:
            width = font.text_length(line_text, fontsize=prepared.font_size)
        else:
            width = max(prepared.style.bbox.width, prepared.changed_rect.width)
        y_offset = line_number * prepared.line_advance
        rectangles.append(
            pymupdf.Rect(
                prepared.style.origin[0],
                min(top, bottom) + y_offset,
                prepared.style.origin[0] + width,
                max(top, bottom) + y_offset,
            )
        )
    return tuple(rectangles)


def _line_candidates_near_rect(page: Any, expected_rect: Any) -> tuple[tuple[str, Any, str], ...]:
    """想定bbox付近のspan/wordを行ごとに連結して返す。"""
    margin_y = max(4.0, expected_rect.height * 0.75)
    probe_rect = _expanded_rect(expected_rect, 8.0, margin_y)
    candidates: list[tuple[str, Any, str]] = []

    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            line_spans = []
            for span in line.get("spans", []):
                span_text = str(span.get("text", ""))
                if not span_text:
                    continue
                span_rect = expected_rect.__class__(span["bbox"])
                if span_rect.intersects(probe_rect):
                    line_spans.append((span_rect, span_text))
            if not line_spans:
                continue
            line_spans.sort(key=lambda item: item[0].x0)
            line_rect = expected_rect.__class__(line_spans[0][0])
            for span_rect, _ in line_spans[1:]:
                line_rect |= span_rect
            candidates.append(
                ("".join(span_text for _, span_text in line_spans), line_rect, "dict")
            )

    words_by_line: list[list[tuple[Any, str]]] = []
    for word in page.get_text("words"):
        if len(word) < 5:
            continue
        word_rect = expected_rect.__class__(word[:4])
        if not word_rect.intersects(probe_rect):
            continue
        center_y = (word_rect.y0 + word_rect.y1) / 2.0
        for line_words in words_by_line:
            existing = line_words[0][0]
            existing_center_y = (existing.y0 + existing.y1) / 2.0
            if abs(center_y - existing_center_y) <= max(3.0, expected_rect.height * 0.35):
                line_words.append((word_rect, str(word[4])))
                break
        else:
            words_by_line.append([(word_rect, str(word[4]))])
    for line_words in words_by_line:
        line_words.sort(key=lambda item: item[0].x0)
        line_rect = expected_rect.__class__(line_words[0][0])
        for word_rect, _ in line_words[1:]:
            line_rect |= word_rect
        candidates.append(
            ("".join(word_text for _, word_text in line_words), line_rect, "words")
        )
    return tuple(candidates)


def _format_validation_candidates(candidates: Sequence[tuple[str, Any, str]]) -> str:
    """保存後検証エラー時に、対象付近で抽出できた文字列を表示する。"""
    if not candidates:
        return "対象bbox付近に抽出可能なspan/wordがありません。"
    lines = []
    for index, (text, rect, source) in enumerate(candidates, start=1):
        lines.append(f"{index}. {source} text={text!r} bbox={tuple(rect)}")
    return "／".join(lines)


def _validate_inserted_line(
    page: Any,
    spec: ReplacementSpec,
    line_number: int,
    new_line: str,
    expected_rect: Any,
) -> None:
    """search_for失敗時も、抽出文字列と想定bboxで新しい1行を検証する。"""
    new_rectangles = tuple(page.search_for(new_line))
    new_groups = group_overlapping_rectangles(new_rectangles)
    matching_groups = [
        group
        for group in new_groups
        if overlap_ratio(group.union_rect, expected_rect) >= 0.5
    ]
    if len(matching_groups) == 1 and len(new_groups) == 1:
        return

    expected_normalized = normalize_whitespace_for_comparison(new_line)
    candidates = _line_candidates_near_rect(page, expected_rect)
    matching_candidates = [
        (text, rect, source)
        for text, rect, source in candidates
        if normalize_whitespace_for_comparison(text) == expected_normalized
        and overlap_ratio(rect, expected_rect) >= 0.5
    ]
    if len(matching_candidates) == 1:
        print(
            f"保存後検証：{spec.label}の{line_number}行目は"
            "search_for()では確認できませんでしたが、"
            "span/wordと座標で確認しました。"
        )
        print(f"保存後検証対象文字列：{new_line}")
        print(f"保存後検証想定bbox：{tuple(expected_rect)}")
        return

    raise ReplacementError(
        "保存後の検証に失敗しました。",
        f"{spec.label}の{line_number}行目が見た目上1箇所ではありません。"
        f"（生の検出件数：{len(new_rectangles)}件／"
        f"表示グループ数：{len(new_groups)}件／"
        f"span/word一致件数：{len(matching_candidates)}件）"
        f"／想定bbox：{tuple(expected_rect)}"
        f"／周辺抽出：{_format_validation_candidates(candidates)}",
    )


def validate_output_pdf(
    pymupdf: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
    prepared: Sequence[PreparedReplacement],
    moves: Sequence[PreparedMove],
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

        for item in prepared:
            spec = item.spec
            page = output_doc[spec.page_index]
            expected_line_rectangles = _line_rectangles_for_validation(pymupdf, item)
            for line_number, new_line in enumerate(spec.new_lines, start=1):
                _validate_inserted_line(
                    page,
                    spec,
                    line_number,
                    new_line,
                    expected_line_rectangles[line_number - 1],
                )
            old_is_contained = any(spec.old_text in line for line in spec.new_lines)
            old_remains = (
                _has_standalone_text_layer(page, spec.old_text)
                if old_is_contained
                else bool(page.search_for(spec.old_text))
            )
            if old_remains:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"{spec.label}の変更前文字列が残っています。",
                )

        for move in moves:
            page = output_doc[move.page_index]
            groups = group_overlapping_rectangles(tuple(page.search_for(move.text)))
            expected_rect = move.style.bbox.__class__(
                move.style.bbox.x0,
                move.style.bbox.y0 + move.y_offset,
                move.style.bbox.x1,
                move.style.bbox.y1 + move.y_offset,
            )
            matching_groups = [
                group
                for group in groups
                if overlap_ratio(group.union_rect, expected_rect) > 0
            ]
            if len(matching_groups) != 1:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"移動後の文字列「{move.text}」を確認できません。",
                )

        if not output_doc[OPENING_DATE_PAGE_INDEX].search_for(REQUIRED_WEEKLY_TEXT):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                f"維持する文字列「{REQUIRED_WEEKLY_TEXT}」が見つかりません。",
            )
        reception_page = output_doc[RECEPTION_START_PAGE_INDEX]
        for required_text in REQUIRED_RECEPTION_TEXTS:
            if not reception_page.search_for(required_text):
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"維持する文字列「{required_text}」が見つかりません。",
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
        allowed_by_page: dict[int, list[Any]] = {}
        for item in prepared:
            allowed_by_page.setdefault(item.spec.page_index, []).append(item.changed_rect)
        for move in moves:
            allowed_by_page.setdefault(move.page_index, []).append(move.changed_rect)
        for page_index, allowed_rectangles in allowed_by_page.items():
            _validate_edited_page_outside_rectangles(
                output_doc[page_index],
                rendering_by_page[page_index],
                allowed_rectangles,
            )
    finally:
        output_doc.close()


def save_and_validate(
    pymupdf: Any,
    doc: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
    prepared: Sequence[PreparedReplacement],
    moves: Sequence[PreparedMove],
) -> None:
    """最適化せず一時保存し、検証成功後だけ正式名へ変更する。"""
    temporary_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp.pdf"
    )
    try:
        doc.save(temporary_path)
        validate_output_pdf(pymupdf, temporary_path, snapshot, prepared, moves)
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
    """変更対象の1・2ページ目をPDFとは独立した確認用PNGへ描画する。"""
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
        lines = [
            "処理結果：エラー",
            f"入力PDF：{INPUT_PDF_NAME}",
            "対象ページ：1ページ目、2ページ目",
        ]
        for replacement in REPLACEMENTS:
            lines.extend(
                (
                    f"{replacement.label}検索文字列：{replacement.old_text}",
                    f"{replacement.label}変更後文字列：{replacement.new_text}",
                )
            )
        lines.extend(
            (
                f"エラー内容：{error.message}",
                f"詳細：{error.detail or 'なし'}",
                f"発生日時：{datetime.now().astimezone().isoformat(timespec='seconds')}",
            )
        )
        error_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        return error_path
    except Exception as exc:
        print(f"エラーファイルを作成できませんでした。詳細：{exc}", file=sys.stderr)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """印刷用チラシの6箇所の文字列差し替えを安全に実行する。"""
    args = parse_arguments(argv)
    program_dir = Path(__file__).resolve().parent
    doc: Any | None = None
    try:
        input_path = find_input_pdf(program_dir)
        output_path = find_available_path(program_dir / OUTPUT_PDF_NAME)
        pymupdf = load_pymupdf()

        print(f"入力PDF：{input_path.name}")
        doc = open_pdf(pymupdf, input_path)
        inspect_print_pdf_structure(doc)
        prepared = prepare_replacements(pymupdf, doc)
        moves = prepare_following_line_moves(pymupdf, doc, prepared)
        snapshot = snapshot_document(doc)

        apply_replacements(pymupdf, doc, prepared, moves)
        save_and_validate(pymupdf, doc, output_path, snapshot, prepared, moves)

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
