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
import math
import os
import struct
import sys
import unicodedata
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


INPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_編集前の原稿.pdf"
OUTPUT_PDF_NAME = "阿波なるとAI塾_PR_ver2_開講日変更後.pdf"
ERROR_FILE_NAME = "阿波なるとAI塾_PR_ver2_開講日変更後_error.txt"

OPENING_DATE_PAGE_INDEX = 2
OLD_CHILD_OPENING_DATE_TEXT = "令和８年８月６日開講"
NEW_CHILD_OPENING_DATE_TEXT = "令和８年９月３日開講／令和８年10月８日開講"

OLD_GENERAL_OPENING_DATE_TEXT = "令和８年９月４日開講"
NEW_GENERAL_OPENING_DATE_LINES = (
    "令和８年９月４日開講／令和８年９月18日開講",
    "令和８年10月２日開講／令和８年10月16日開講",
)
GENERAL_SCHEDULE_HEADING_TEXT = "受講日時"
GENERAL_DESCRIPTION_LAST_LINE_TEXT = "つける事が大切です。"
YOUTH_COURSE_HEADING_TEXT = "Youth Course"
GENERAL_COURSE_HEADING_TEXT = "General Course"
COURSE_GAP_TOLERANCE = 0.5
MINIMUM_GENERAL_DESCRIPTION_GAP = 2.0

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

RECEPTION_START_PAGE_INDEX = 3
OLD_RECEPTION_START_TEXT = "令和８年７月１日より受付開始"
NEW_RECEPTION_START_TEXT = "令和８年８月１日より受付開始"

COMPUTER_NOTE_PAGE_INDEX = 3
OLD_COMPUTER_NOTE_TEXT = "※どちらか一方でも可"
NEW_COMPUTER_NOTE_TEXT = "※どちらか一方でも可(パソコン推奨)"

REQUIRED_WEEKLY_TEXT = "毎週木曜日"
REQUIRED_RECEPTION_TEXTS = ("募集期間", "(各コース開講前まで応募可能)")
EXPECTED_PAGE_COUNT = 5
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
        self.error_pdf_path: Path | None = None
        self.diagnostics: tuple[DifferenceDiagnosis, ...] = ()


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
    origin_offset_y: float = 0.0


@dataclass(frozen=True)
class PreparedMove:
    """一般コース欄内で必要最小限だけ移動する既存行。"""

    text: str
    page_index: int
    style: TextStyle
    font_path: Path
    deletion_rectangles: tuple[Any, ...]
    y_offset: float
    changed_rect: Any


@dataclass(frozen=True)
class GeneralDescriptionPlan:
    """Youth Courseと同じ見出し間隔へ近づける説明文移動計画。"""

    moves: tuple[PreparedMove, ...]
    youth_gap: float
    original_gap: float
    upward_offset: float
    general_heading_rect: Any
    original_first_line_rect: Any


@dataclass(frozen=True)
class DocumentSnapshot:
    """保存前後の文書を比較するための情報。"""

    page_count: int
    page_sizes: tuple[tuple[float, float], ...]
    page_texts: tuple[str, ...]
    untouched_render_hashes: tuple[tuple[int, str], ...]
    edited_renderings: tuple[tuple[int, int, int, int, int, bytes], ...]


@dataclass(frozen=True)
class AllowedChange:
    """画像差分を許可する編集対象名と矩形。"""

    label: str
    rectangle: Any


@dataclass(frozen=True)
class DifferenceDiagnosis:
    """編集許可矩形外で検出した画像差分の診断結果。"""

    page_index: int
    pixel_count: int
    first_coordinate: tuple[int, int]
    last_coordinate: tuple[int, int]
    bounding_box: tuple[int, int, int, int]
    original_rgb: tuple[int, int, int]
    current_rgb: tuple[int, int, int]
    maximum_rgb_difference: int
    average_rgb_difference: float
    nearest_label: str
    minimum_distance: float
    within_one_ratio: float
    within_two_ratio: float
    within_three_ratio: float
    causes: tuple[str, ...]
    confidence: str
    reasons: tuple[str, ...]
    before_image_path: Path
    after_image_path: Path
    difference_image_path: Path


REPLACEMENTS = (
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
        NEW_GENERAL_OPENING_DATE_LINES,
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


def _single_search_group(page: Any, text: str, label: str) -> SearchGroup:
    """見出し文字列を表示位置で1件に特定する。"""
    groups = group_overlapping_rectangles(tuple(page.search_for(text)))
    if len(groups) != 1:
        raise ReplacementError(
            f"{label}を一意に特定できません。",
            f"検索文字列：{text}／表示グループ数：{len(groups)}件",
        )
    return groups[0]


def _block_lines(block: dict[str, Any], rect_class: Any) -> tuple[tuple[str, Any, tuple[dict[str, Any], ...]], ...]:
    """テキストブロックの非空行を文字列・矩形・spanで返す。"""
    result = []
    for line in block.get("lines", []):
        spans = tuple(span for span in line.get("spans", []) if str(span.get("text", "")))
        if not spans:
            continue
        text = "".join(str(span.get("text", "")) for span in spans)
        rect = rect_class(spans[0]["bbox"])
        for span in spans[1:]:
            rect |= rect_class(span["bbox"])
        result.append((text, rect, spans))
    return tuple(result)


def prepare_general_description_spacing(pymupdf: Any, doc: Any) -> GeneralDescriptionPlan:
    """General Course説明文をYouth Courseと同じ見出し間隔まで上へ移動する。"""
    page = doc[OPENING_DATE_PAGE_INDEX]
    youth_heading = _single_search_group(
        page, YOUTH_COURSE_HEADING_TEXT, "Youth Course見出し"
    ).union_rect
    general_heading = _single_search_group(
        page, GENERAL_COURSE_HEADING_TEXT, "General Course見出し"
    ).union_rect
    child_opening = _single_search_group(
        page, OLD_CHILD_OPENING_DATE_TEXT, "児童コース開講日"
    ).union_rect
    general_opening = _single_search_group(
        page, OLD_GENERAL_OPENING_DATE_TEXT, "一般コース開講日"
    ).union_rect

    blocks_with_lines = [
        (block, _block_lines(block, page.rect.__class__))
        for block in page.get_text("dict").get("blocks", [])
    ]
    youth_candidates = [
        lines
        for _, lines in blocks_with_lines
        if lines
        and lines[0][1].y0 >= youth_heading.y1
        and lines[0][1].y0 < child_opening.y0
        and YOUTH_COURSE_HEADING_TEXT not in lines[0][0]
    ]
    if not youth_candidates:
        raise ReplacementError("Youth Course説明文の先頭行を確認できません。")
    youth_lines = min(youth_candidates, key=lambda lines: lines[0][1].y0)

    general_candidates = [
        lines
        for _, lines in blocks_with_lines
        if lines
        and lines[0][1].y0 >= general_heading.y1
        and lines[-1][1].y1 < general_opening.y0
        and any(GENERAL_DESCRIPTION_LAST_LINE_TEXT in text for text, _, _ in lines)
    ]
    if len(general_candidates) != 1:
        raise ReplacementError(
            "一般コース説明文を一意に特定できません。",
            f"説明文候補数：{len(general_candidates)}件",
        )
    general_lines = general_candidates[0]
    youth_gap = youth_lines[0][1].y0 - youth_heading.y1
    general_gap = general_lines[0][1].y0 - general_heading.y1
    if youth_gap <= 0 or general_gap <= 0:
        raise ReplacementError(
            "コース見出しと説明文の間隔が不正です。",
            f"Youth Course：{youth_gap}／General Course：{general_gap}",
        )
    upward_offset = max(0.0, general_gap - MINIMUM_GENERAL_DESCRIPTION_GAP)
    description_texts = tuple(
        str(span.get("text", ""))
        for _, _, spans in general_lines
        for span in spans
    )
    ignored_texts = (*description_texts, *(spec.old_text for spec in REPLACEMENTS))
    moves: list[PreparedMove] = []
    movable_lines = general_lines if upward_offset > 0 else ()
    for line_number, (_, _, spans) in enumerate(movable_lines, start=1):
        for span_number, span in enumerate(spans, start=1):
            text = str(span.get("text", ""))
            rect = page.rect.__class__(span["bbox"])
            origin_value = span.get("origin", (rect.x0, rect.y1))
            style = TextStyle(
                rect,
                (float(origin_value[0]), float(origin_value[1])),
                str(span.get("font", "")),
                float(span.get("size", 0.0)),
                int(span.get("color", 0)),
                int(span.get("flags", 0)),
                float(span["ascender"]) if span.get("ascender") is not None else None,
                float(span["descender"]) if span.get("descender") is not None else None,
            )
            move_spec = ReplacementSpec(
                f"一般コース説明文{line_number}行目span{span_number}",
                OPENING_DATE_PAGE_INDEX,
                text,
                (text,),
            )
            font_path = find_japanese_font(style, move_spec)
            y_offset = -upward_offset
            new_rect = rect.__class__(rect.x0, rect.y0 + y_offset, rect.x1, rect.y1 + y_offset)
            if not page.rect.contains(new_rect) or _crosses_new_graphics(page, rect, new_rect):
                raise ReplacementError(
                    "一般コース説明文を安全に上へ移動できません。",
                    f"対象：{text}／移動後bbox：{tuple(new_rect)}",
                )
            collisions = [
                collision
                for other_rect, collision in _span_rectangles(page, rect, ignored_texts)
                if new_rect.intersects(other_rect)
            ]
            if collisions:
                raise ReplacementError(
                    "一般コース説明文を安全に上へ移動できません。",
                    f"対象：{text}／交差文字列：{' / '.join(collisions)}",
                )
            moves.append(
                PreparedMove(
                    text,
                    OPENING_DATE_PAGE_INDEX,
                    style,
                    font_path,
                    (rect,),
                    y_offset,
                    rect | new_rect,
                )
            )
    print(f"Youth Course見出しbbox：{tuple(youth_heading)}")
    print(f"Youth Course説明文1行目bbox：{tuple(youth_lines[0][1])}")
    print(f"Youth Course基準間隔：{youth_gap}")
    print(f"General Course見出しbbox：{tuple(general_heading)}")
    print(f"一般コース説明文1行目bbox：{tuple(general_lines[0][1])}")
    print(f"General Course現在間隔：{general_gap}")
    print(f"General Course最低安全間隔：{MINIMUM_GENERAL_DESCRIPTION_GAP}")
    print(f"一般コース説明文の上方向移動量：{upward_offset}\n")
    return GeneralDescriptionPlan(
        tuple(moves),
        youth_gap,
        general_gap,
        upward_offset,
        general_heading,
        general_lines[0][1],
    )


def calculate_placement(
    pymupdf: Any,
    page: Any,
    style: TextStyle,
    font_path: Path,
    spec: ReplacementSpec,
    origin_offset_y: float = 0.0,
    line_advance_override: float | None = None,
    additional_ignored_texts: Sequence[str] = (),
) -> tuple[float, Any, float]:
    """対象別の最大縮小率と安全な行間で、最大の文字サイズを選ぶ。"""
    try:
        font = pymupdf.Font(fontfile=str(font_path))
    except Exception as exc:
        raise ReplacementError("日本語フォントを読み込めませんでした。", str(exc)) from exc

    ignored_texts: tuple[str, ...] = ()
    if len(spec.new_lines) > 1:
        ignored_texts = GENERAL_MOVABLE_TEXTS
    if spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT:
        ignored_texts += (OLD_GENERAL_SCHEDULE_TEXT,)
    ignored_texts += tuple(additional_ignored_texts)
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
    if len(spec.new_lines) > 1 and line_advance_override is None:
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
        insertion_origin_y = style.origin[1] + origin_offset_y
        top = insertion_origin_y - font_size * font_ascender
        bottom = insertion_origin_y - font_size * font_descender
        for spacing_ratio in spacing_ratios:
            line_advance = (
                line_advance_override
                if line_advance_override is not None
                else font_size * spacing_ratio
                if len(spec.new_lines) > 1
                else 0.0
            )
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


def prepare_replacements(
    pymupdf: Any, doc: Any, description_plan: GeneralDescriptionPlan
) -> tuple[PreparedReplacement, ...]:
    """5件すべてを編集前に検査し、部分的な変更を防ぐ。"""
    prepared_by_old_text: dict[str, PreparedReplacement] = {}
    for spec in REPLACEMENTS:
        page = doc[spec.page_index]
        search_group = find_target_text(page, spec)
        style, deletion_rectangles = get_original_text_style(page, search_group, spec)
        ensure_text_only_redaction_supported(pymupdf, page)
        font_path = find_japanese_font(style, spec)
        origin_offset_y = 0.0
        line_advance_override: float | None = None
        description_texts = tuple(move.text for move in description_plan.moves)
        if spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT:
            origin_offset_y = -description_plan.upward_offset
        font_size, changed_rect, line_advance = calculate_placement(
            pymupdf,
            page,
            style,
            font_path,
            spec,
            origin_offset_y,
            line_advance_override,
            description_texts,
        )
        if spec.old_text == OLD_GENERAL_SCHEDULE_TEXT:
            opening_date = prepared_by_old_text[OLD_GENERAL_OPENING_DATE_TEXT]
            opening_rectangles = _planned_line_rectangles(pymupdf, opening_date)
            schedule_probe = PreparedReplacement(
                spec,
                style,
                font_path,
                font_size,
                changed_rect,
                deletion_rectangles,
                line_advance,
            )
            schedule_rectangles = _planned_line_rectangles(pymupdf, schedule_probe)
            minimum_gap = max(
                MINIMUM_FOLLOWING_GAP,
                opening_date.font_size * MULTILINE_MIN_GAP_RATIO,
            )
            origin_offset_y = (
                opening_rectangles[-1].y1
                + minimum_gap
                - schedule_rectangles[0].y0
            )
            font_size, changed_rect, line_advance = calculate_placement(
                pymupdf,
                page,
                style,
                font_path,
                spec,
                origin_offset_y,
                additional_ignored_texts=description_texts,
            )
        print(f"使用フォント：{font_path}")
        print(f"変更後文字列：{spec.new_text}")
        print(f"挿入文字サイズ：{font_size}\n")
        if spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT:
            print(f"一般コース開講日のY方向移動量：{origin_offset_y}\n")
        if spec.old_text == OLD_GENERAL_SCHEDULE_TEXT:
            print(f"一般コース受講日時の下方向移動量：{origin_offset_y}\n")
        if len(spec.new_lines) > 1:
            print(f"2行のベースライン間隔：{line_advance}")
            print(f"行間倍率：{line_advance / font_size}\n")
        prepared_by_old_text[spec.old_text] = PreparedReplacement(
            spec,
            style,
            font_path,
            font_size,
            changed_rect,
            deletion_rectangles,
            line_advance,
            origin_offset_y,
        )
    return tuple(prepared_by_old_text[spec.old_text] for spec in REPLACEMENTS)


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


def _select_general_schedule_heading(
    heading_groups: Sequence[SearchGroup], opening_rect: Any, schedule_rect: Any
) -> SearchGroup:
    """2見出しから一般コースにY方向が最も近い候補を返す。"""
    if len(heading_groups) != 2:
        raise ReplacementError(
            "一般コースの受講日時見出しを一意に特定できません。",
            f"全表示グループ数：{len(heading_groups)}件（必要：2件）",
        )

    opening_center_y = (opening_rect.y0 + opening_rect.y1) / 2.0
    schedule_center_y = (schedule_rect.y0 + schedule_rect.y1) / 2.0
    ranked: list[tuple[float, float, SearchGroup]] = []
    for number, group in enumerate(heading_groups, start=1):
        rect = group.union_rect
        if rect.get_area() <= 0:
            raise ReplacementError(
                "一般コースの受講日時見出しの矩形が不正です。",
                f"候補：{number}／bbox：{tuple(rect)}",
            )
        center_x = (rect.x0 + rect.x1) / 2.0
        center_y = (rect.y0 + rect.y1) / 2.0
        opening_distance = abs(center_y - opening_center_y)
        schedule_distance = abs(center_y - schedule_center_y)
        print(f"受講日時見出し候補 {number}：bbox={tuple(rect)}")
        print(
            f"  中心X={center_x}／中心Y={center_y}／"
            f"開講日との中心Y距離={opening_distance}／"
            f"受講日時本文との中心Y距離={schedule_distance}"
        )
        ranked.append((opening_distance, schedule_distance, group))

    ranked.sort(key=lambda item: (item[0], item[1]))
    distance_gap = ranked[1][0] - ranked[0][0]
    if distance_gap <= 1.0:
        raise ReplacementError(
            "一般コースの受講日時見出しを一意に特定できません。",
            f"開講日との中心Y距離差：{distance_gap}／必要：1.0より大きい値",
        )
    selected = ranked[0][2]
    selected_center_y = (selected.union_rect.y0 + selected.union_rect.y1) / 2.0
    if selected_center_y >= schedule_center_y:
        raise ReplacementError(
            "一般コースの受講日時見出しが受講日時本文より上にありません。",
            f"見出し中心Y：{selected_center_y}／本文中心Y：{schedule_center_y}",
        )
    print(f"一般コース受講日時見出しの中心Y距離差：{distance_gap}")
    return selected


def validate_general_schedule_heading_position(
    pymupdf: Any,
    doc: Any,
    replacements: Sequence[PreparedReplacement],
) -> Any:
    """一般コースの受講日時見出しを特定し、元位置で安全か確認する。"""
    opening_date = next(
        item
        for item in replacements
        if item.spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT
    )
    schedule = next(
        item for item in replacements if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
    )
    page = doc[OPENING_DATE_PAGE_INDEX]
    heading_groups = group_overlapping_rectangles(
        tuple(page.search_for(GENERAL_SCHEDULE_HEADING_TEXT))
    )
    selected = _select_general_schedule_heading(
        heading_groups, opening_date.style.bbox, schedule.style.bbox
    )
    heading_rect = selected.union_rect
    opening_rectangles = _planned_line_rectangles(pymupdf, opening_date)
    if any(heading_rect.intersects(rect) for rect in opening_rectangles):
        raise ReplacementError(
            "一般コース受講日時見出しと開講日が重なります。"
        )
    print(f"一般コース受講日時見出しbbox：{tuple(heading_rect)}")
    print("一般コース受講日時見出しは元位置を維持します。\n")
    return heading_rect


def prepare_following_line_moves(
    pymupdf: Any,
    doc: Any,
    replacements: Sequence[PreparedReplacement],
) -> tuple[PreparedMove, ...]:
    """全2回行と受講料行を別グループとして必要最小限だけ下へ移動する。"""
    schedule = next(
        item for item in replacements if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
    )
    page = doc[schedule.spec.page_index]
    ignored_texts = set(GENERAL_MOVABLE_TEXTS)
    ignored_texts.update(spec.old_text for spec in REPLACEMENTS)
    normalized_ignored_texts = {
        normalize_whitespace_for_comparison(text) for text in ignored_texts
    }

    prepared_rows: dict[str, tuple[TextStyle, tuple[Any, ...], Path]] = {}
    for text in GENERAL_MOVABLE_TEXTS:
        group = _find_closest_group_below(page, text, schedule.style.bbox)
        move_spec = ReplacementSpec(
            f"一般コース後続行「{text}」",
            schedule.spec.page_index,
            text,
            (text,),
        )
        style, deletion_rectangles = get_original_text_style(page, group, move_spec)
        prepared_rows[text] = (
            style,
            deletion_rectangles,
            find_japanese_font(style, move_spec),
        )

    count_style = prepared_rows[GENERAL_MOVABLE_TEXTS[0]][0]
    fee_style = prepared_rows[GENERAL_MOVABLE_TEXTS[1]][0]
    price_style = prepared_rows[GENERAL_MOVABLE_TEXTS[2]][0]
    fee_center_y = (fee_style.bbox.y0 + fee_style.bbox.y1) / 2.0
    price_center_y = (price_style.bbox.y0 + price_style.bbox.y1) / 2.0
    fee_row_tolerance = max(fee_style.bbox.height, price_style.bbox.height) * 0.5
    if abs(fee_center_y - price_center_y) > fee_row_tolerance:
        raise ReplacementError(
            "受講料と料金を同じ行として確認できません。",
            f"受講料中心Y：{fee_center_y}／料金中心Y：{price_center_y}／"
            f"許容差：{fee_row_tolerance}",
        )

    minimum_schedule_gap = max(
        MINIMUM_FOLLOWING_GAP,
        schedule.font_size * MULTILINE_MIN_GAP_RATIO,
    )
    count_offset = max(
        0.0, schedule.changed_rect.y1 + minimum_schedule_gap - count_style.bbox.y0
    )
    moved_count_rect = count_style.bbox.__class__(
        count_style.bbox.x0,
        count_style.bbox.y0 + count_offset,
        count_style.bbox.x1,
        count_style.bbox.y1 + count_offset,
    )
    minimum_fee_gap = max(
        MINIMUM_FOLLOWING_GAP,
        count_style.size * MULTILINE_MIN_GAP_RATIO,
    )
    fee_row_top = min(fee_style.bbox.y0, price_style.bbox.y0)
    fee_row_offset = max(
        0.0, moved_count_rect.y1 + minimum_fee_gap - fee_row_top
    )
    offsets = {
        GENERAL_MOVABLE_TEXTS[0]: count_offset,
        GENERAL_MOVABLE_TEXTS[1]: fee_row_offset,
        GENERAL_MOVABLE_TEXTS[2]: fee_row_offset,
    }

    moves: list[PreparedMove] = []
    for text in GENERAL_MOVABLE_TEXTS:
        style, deletion_rectangles, font_path = prepared_rows[text]
        y_offset = offsets[text]
        if y_offset <= 0:
            continue
        new_rect = style.bbox.__class__(
            style.bbox.x0,
            style.bbox.y0 + y_offset,
            style.bbox.x1,
            style.bbox.y1 + y_offset,
        )
        print(
            f"一般コース後続行予定bbox：{text}／"
            f"移動前={tuple(style.bbox)}／移動後={tuple(new_rect)}／"
            f"ページ={tuple(page.rect)}"
        )
        if not page.rect.contains(new_rect):
            overflow = max(0.0, new_rect.y1 - page.rect.y1)
            raise ReplacementError(
                "一般コースの後続行をページ内へ移動できません。",
                f"移動対象：{text}／Y移動量：{y_offset}／"
                f"移動後下端：{new_rect.y1}／ページ下端：{page.rect.y1}／"
                f"超過量：{overflow}",
            )
        if _crosses_new_graphics(page, style.bbox, new_rect):
            raise ReplacementError(
                "一般コースの後続行が背景線または図形へ重なるため移動できません。",
                f"移動対象：{text}／移動後bbox：{tuple(new_rect)}",
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

    print(f"全2回行のY方向移動量：{count_offset}")
    print(f"受講料行の共通Y方向移動量：{fee_row_offset}")
    print(f"受講日時と全2回行の最低余白：{minimum_schedule_gap}")
    print(f"全2回行と受講料行の最低余白：{minimum_fee_gap}\n")
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
                prepared.style.origin[1]
                + prepared.origin_offset_y
                + line_number * prepared.line_advance,
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
    """既存行の内容と書式を変えず、必要最小限だけ再配置する。"""
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
            "一般コースの既存行を移動できませんでした。",
            f"移動対象：{move.text}／詳細：{exc}",
        ) from exc
    if result < 0:
        raise ReplacementError(
            "一般コースの既存行を移動できませんでした。",
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


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """標準ライブラリだけでRGB PNGを書くためのチャンクを返す。"""
    payload = chunk_type + data
    return (
        struct.pack(">I", len(data))
        + payload
        + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def _save_rgb_png(
    path: Path,
    width: int,
    height: int,
    components: int,
    stride: int,
    samples: bytes | bytearray,
) -> None:
    """PixmapのRGB画素を外部ライブラリなしでPNGへ保存する。"""
    if components < 3:
        raise ReplacementError(
            "差分確認画像を作成できませんでした。",
            f"RGB成分数が不足しています。（検出：{components}）",
        )
    rows = []
    for y in range(height):
        row_start = y * stride
        if components == 3:
            row = bytes(samples[row_start : row_start + width * 3])
        else:
            row = bytes(
                channel
                for x in range(width)
                for channel in samples[
                    row_start + x * components : row_start + x * components + 3
                ]
            )
        rows.append(b"\x00" + row)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
    png += _png_chunk(b"IEND", b"")
    path.write_bytes(png)


def _expanded_pixel_rectangle(rectangle: Any) -> tuple[int, int, int, int]:
    """既存比較と同じ上下左右2ピクセル付きの許可範囲を返す。"""
    return (
        int(rectangle.x0) - 2,
        int(rectangle.y0) - 2,
        int(rectangle.x1 + 0.9999) + 2,
        int(rectangle.y1 + 0.9999) + 2,
    )


def _pixel_distance_to_rectangle(
    x: int, y: int, rectangle: tuple[int, int, int, int]
) -> float:
    """画素と許可矩形とのユークリッド距離を返す。"""
    left, top, right, bottom = rectangle
    dx = max(left - x, 0, x - (right - 1))
    dy = max(top - y, 0, y - (bottom - 1))
    return math.hypot(dx, dy)


def _set_rgb_pixel(
    samples: bytearray,
    x: int,
    y: int,
    color: tuple[int, int, int],
    width: int,
    height: int,
    components: int,
    stride: int,
) -> None:
    """範囲内の画素へ診断用の色を設定する。"""
    if not (0 <= x < width and 0 <= y < height):
        return
    offset = y * stride + x * components
    samples[offset : offset + 3] = bytes(color)


def _draw_pixel_rectangle(
    samples: bytearray,
    rectangle: tuple[int, int, int, int],
    color: tuple[int, int, int],
    width: int,
    height: int,
    components: int,
    stride: int,
) -> None:
    """差分強調画像へ1ピクセル幅の矩形を描く。"""
    left, top, right, bottom = rectangle
    for x in range(left, right):
        _set_rgb_pixel(samples, x, top, color, width, height, components, stride)
        _set_rgb_pixel(samples, x, bottom - 1, color, width, height, components, stride)
    for y in range(top, bottom):
        _set_rgb_pixel(samples, left, y, color, width, height, components, stride)
        _set_rgb_pixel(samples, right - 1, y, color, width, height, components, stride)


def _diagnostic_image_path(
    program_dir: Path, phase: str, page_number: int
) -> Path:
    """差分診断画像用の未使用ファイル名を返す。"""
    return find_available_path(program_dir / f"確認用_{phase}_{page_number}ページ目.png")


def _infer_difference_causes(
    nearest_label: str,
    within_one_ratio: float,
    within_three_ratio: float,
    average_rgb_difference: float,
    density: float,
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """画素差の特徴から原因候補、確度、根拠を保守的に推定する。"""
    scored: list[tuple[float, str]] = []
    if within_one_ratio >= 0.8:
        scored.append((0.95, "4. PDF座標と画像ピクセル座標の丸め差"))
    elif within_three_ratio >= 0.8:
        scored.append((0.75, "4. PDF座標と画像ピクセル座標の丸め差"))
    if within_three_ratio >= 0.7 and average_rgb_difference <= 48:
        scored.append((0.85, "1. 文字アンチエイリアス"))
    if density >= 0.65:
        scored.append((0.65, "2. redaction処理による矩形周辺の描画差"))
    if nearest_label and within_three_ratio >= 0.6:
        scored.append((0.6, "3. 元フォントと差し込みフォントの描画差"))
    if within_three_ratio < 0.5:
        scored.append((0.8, "5. 背景画像または半透明部分の再描画差"))
    scored.sort(key=lambda item: item[0], reverse=True)
    causes = tuple(cause for _, cause in scored[:2])
    if not causes:
        return (
            (),
            "低",
            ("差分の位置・形状が、定義済みの原因判定条件に十分一致しません。",),
        )
    top_score = scored[0][0]
    confidence = "高" if top_score >= 0.9 else "中" if top_score >= 0.7 else "低"
    reasons = (
        f"差分の{within_one_ratio:.1%}が編集許可矩形から1ピクセル以内です。",
        f"差分の{within_three_ratio:.1%}が編集許可矩形から3ピクセル以内です。",
        f"差分の平均RGB差は{average_rgb_difference:.2f}です。",
    )
    return causes, confidence, reasons


def _format_difference_diagnosis(diagnosis: DifferenceDiagnosis) -> str:
    """コンソールとエラーファイルへ記録する日本語診断文を返す。"""
    lines = [
        f"差分ページ：{diagnosis.page_index + 1}ページ目",
        f"差分画素数：{diagnosis.pixel_count}",
        f"最初の差分座標：{diagnosis.first_coordinate}",
        f"最後の差分座標：{diagnosis.last_coordinate}",
        f"差分外接矩形：{diagnosis.bounding_box}",
        f"最も近い編集対象：{diagnosis.nearest_label}",
        f"許可矩形からの最短距離：{diagnosis.minimum_distance:.2f}ピクセル",
        f"1ピクセル以内の差分割合：{diagnosis.within_one_ratio:.1%}",
        f"2ピクセル以内の差分割合：{diagnosis.within_two_ratio:.1%}",
        f"3ピクセル以内の差分割合：{diagnosis.within_three_ratio:.1%}",
        f"変更前RGB：{diagnosis.original_rgb}",
        f"変更後RGB：{diagnosis.current_rgb}",
        f"RGB差の最大値：{diagnosis.maximum_rgb_difference}",
        f"RGB差の平均値：{diagnosis.average_rgb_difference:.2f}",
        "",
    ]
    if diagnosis.causes:
        lines.append("推定原因：")
        for index, cause in enumerate(diagnosis.causes, start=1):
            lines.append(f"第{index}候補：{cause}")
        lines.extend(("", f"確度：{diagnosis.confidence}", "根拠："))
        lines.extend(f"・{reason}" for reason in diagnosis.reasons)
    else:
        lines.extend(
            (
                "推定原因：特定できません",
                "注意：対象外要素が実際に変化している可能性があります。",
            )
        )
    lines.extend(
        (
            "",
            f"変更前画像：{diagnosis.before_image_path.name}",
            f"変更後画像：{diagnosis.after_image_path.name}",
            f"差分確認画像：{diagnosis.difference_image_path.name}",
        )
    )
    return "\n".join(lines)


def _diagnose_edited_page_outside_rectangles(
    page: Any,
    page_index: int,
    original: tuple[int, int, int, int, int, bytes],
    allowed_changes: Sequence[AllowedChange],
    program_dir: Path,
) -> DifferenceDiagnosis | None:
    """許可矩形外の全差分を収集し、診断画像と原因候補を作る。"""
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

    current_samples = bytes(pixmap.samples)
    expanded_rectangles = [
        (change, _expanded_pixel_rectangle(change.rectangle))
        for change in allowed_changes
    ]
    outside_differences: list[
        tuple[int, int, tuple[int, int, int], tuple[int, int, int], float, str]
    ] = []
    all_differences: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * components
            original_rgb = tuple(original_samples[offset : offset + 3])
            current_rgb = tuple(current_samples[offset : offset + 3])
            if original_rgb == current_rgb:
                continue
            all_differences.append((x, y))
            containing = [
                change
                for change, rectangle in expanded_rectangles
                if rectangle[0] <= x < rectangle[2]
                and rectangle[1] <= y < rectangle[3]
            ]
            if containing:
                continue
            distances = [
                (_pixel_distance_to_rectangle(x, y, rectangle), change.label)
                for change, rectangle in expanded_rectangles
            ]
            distance, label = min(distances, default=(math.inf, "特定できません"))
            outside_differences.append(
                (x, y, original_rgb, current_rgb, distance, label)
            )
    if not outside_differences:
        return None

    xs = [difference[0] for difference in outside_differences]
    ys = [difference[1] for difference in outside_differences]
    bounding_box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    pixel_count = len(outside_differences)
    minimum_distance, nearest_label = min(
        (difference[4], difference[5]) for difference in outside_differences
    )
    within_one_ratio = sum(item[4] <= 1.0 for item in outside_differences) / pixel_count
    within_two_ratio = sum(item[4] <= 2.0 for item in outside_differences) / pixel_count
    within_three_ratio = sum(item[4] <= 3.0 for item in outside_differences) / pixel_count
    channel_differences = [
        abs(old_channel - new_channel)
        for _, _, old_rgb, new_rgb, _, _ in outside_differences
        for old_channel, new_channel in zip(old_rgb, new_rgb)
    ]
    bbox_area = max(1, (bounding_box[2] - bounding_box[0]) * (bounding_box[3] - bounding_box[1]))
    density = pixel_count / bbox_area
    causes, confidence, reasons = _infer_difference_causes(
        nearest_label,
        within_one_ratio,
        within_three_ratio,
        sum(channel_differences) / len(channel_differences),
        density,
    )

    before_path = _diagnostic_image_path(program_dir, "変更前", page_index + 1)
    after_path = _diagnostic_image_path(program_dir, "変更後", page_index + 1)
    difference_path = _diagnostic_image_path(program_dir, "差分", page_index + 1)
    _save_rgb_png(
        before_path, width, height, components, stride, original_samples
    )
    _save_rgb_png(after_path, width, height, components, stride, current_samples)
    highlighted = bytearray(current_samples)
    outside_coordinates = {(item[0], item[1]) for item in outside_differences}
    for x, y in all_differences:
        color = (255, 0, 0) if (x, y) in outside_coordinates else (0, 96, 255)
        _set_rgb_pixel(highlighted, x, y, color, width, height, components, stride)
    for _, rectangle in expanded_rectangles:
        _draw_pixel_rectangle(
            highlighted, rectangle, (0, 200, 0), width, height, components, stride
        )
    _draw_pixel_rectangle(
        highlighted, bounding_box, (255, 255, 0), width, height, components, stride
    )
    first_x, first_y = outside_differences[0][0], outside_differences[0][1]
    for delta in range(-4, 5):
        _set_rgb_pixel(
            highlighted,
            first_x + delta,
            first_y,
            (255, 255, 0),
            width,
            height,
            components,
            stride,
        )
        _set_rgb_pixel(
            highlighted,
            first_x,
            first_y + delta,
            (255, 255, 0),
            width,
            height,
            components,
            stride,
        )
    _save_rgb_png(difference_path, width, height, components, stride, highlighted)
    first = outside_differences[0]
    last = outside_differences[-1]
    return DifferenceDiagnosis(
        page_index,
        pixel_count,
        (first[0], first[1]),
        (last[0], last[1]),
        bounding_box,
        first[2],
        first[3],
        max(channel_differences),
        sum(channel_differences) / len(channel_differences),
        nearest_label,
        minimum_distance,
        within_one_ratio,
        within_two_ratio,
        within_three_ratio,
        causes,
        confidence,
        reasons,
        before_path,
        after_path,
        difference_path,
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


def _planned_line_rectangles(pymupdf: Any, prepared: PreparedReplacement) -> tuple[Any, ...]:
    """挿入時と同じ基準で各行の予定bboxを計算する。"""
    try:
        font = pymupdf.Font(fontfile=str(prepared.font_path))
    except Exception:
        font = None
    font_ascender = float(getattr(font, "ascender", prepared.style.ascender or 1.0))
    font_descender = float(getattr(font, "descender", prepared.style.descender or -0.25))
    insertion_origin_y = prepared.style.origin[1] + prepared.origin_offset_y
    top = insertion_origin_y - prepared.font_size * font_ascender
    bottom = insertion_origin_y - prepared.font_size * font_descender
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
    original_heading_rect: Any,
    description_plan: GeneralDescriptionPlan,
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
            expected_line_rectangles = _planned_line_rectangles(pymupdf, item)
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
            old_position_groups = [
                group
                for group in groups
                if group.union_rect.intersects(move.style.bbox)
                and overlap_ratio(group.union_rect, expected_rect) == 0
            ]
            if move.y_offset and old_position_groups:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"移動前の文字列「{move.text}」が残っています。",
                )

        general_schedule = next(
            item
            for item in prepared
            if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
        )
        fee_group = _find_closest_group_below(
            output_doc[OPENING_DATE_PAGE_INDEX],
            GENERAL_MOVABLE_TEXTS[1],
            general_schedule.style.bbox,
        )
        price_group = _find_closest_group_below(
            output_doc[OPENING_DATE_PAGE_INDEX],
            GENERAL_MOVABLE_TEXTS[2],
            general_schedule.style.bbox,
        )
        fee_center_y = (fee_group.union_rect.y0 + fee_group.union_rect.y1) / 2.0
        price_center_y = (price_group.union_rect.y0 + price_group.union_rect.y1) / 2.0
        fee_row_tolerance = max(
            fee_group.union_rect.height, price_group.union_rect.height
        ) * 0.5
        if abs(fee_center_y - price_center_y) > fee_row_tolerance:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "受講料と料金が同じ論理行にありません。"
                f"（中心Y差：{abs(fee_center_y - price_center_y)}／"
                f"許容差：{fee_row_tolerance}）",
            )
        general_opening = next(
            item
            for item in prepared
            if item.spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT
        )
        general_page = output_doc[OPENING_DATE_PAGE_INDEX]
        opening_rectangles = _planned_line_rectangles(pymupdf, general_opening)
        schedule_rectangles = _planned_line_rectangles(pymupdf, general_schedule)
        heading_groups = group_overlapping_rectangles(
            tuple(general_page.search_for(GENERAL_SCHEDULE_HEADING_TEXT))
        )
        try:
            heading_group = _select_general_schedule_heading(
                heading_groups,
                general_opening.style.bbox,
                general_schedule.style.bbox,
            )
        except ReplacementError as exc:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                f"一般コース受講日時見出し：{exc.message}／{exc.detail}",
            ) from exc
        heading_rect = heading_group.union_rect
        if any(
            abs(actual - expected) > 0.5
            for actual, expected in zip(tuple(heading_rect), tuple(original_heading_rect))
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース受講日時見出しの位置が変わっています。"
                f"（保存前：{tuple(original_heading_rect)}／"
                f"保存後：{tuple(heading_rect)}）",
            )
        if (
            len(opening_rectangles) != 2
            or opening_rectangles[0].y0 >= opening_rectangles[1].y0
            or opening_rectangles[0].intersects(opening_rectangles[1])
            or abs(
                general_opening.origin_offset_y + description_plan.upward_offset
            )
            > 0.001
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース開講日の2行の順序または行間が不正です。",
            )
        minimum_gap = max(
            MINIMUM_FOLLOWING_GAP,
            general_opening.font_size * MULTILINE_MIN_GAP_RATIO,
        )
        if not schedule_rectangles:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース受講日時の配置を確認できません。",
            )
        actual_gap = schedule_rectangles[0].y0 - opening_rectangles[1].y1
        if actual_gap < minimum_gap:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース開講日2行目と受講日時1行目の余白が不足しています。",
            )
        description_groups = group_overlapping_rectangles(
            tuple(general_page.search_for(GENERAL_DESCRIPTION_LAST_LINE_TEXT))
        )
        description_candidates = [
            group
            for group in description_groups
            if group.union_rect.y1 <= heading_rect.y0
            and group.union_rect.x0 >= heading_rect.x0 - 1.0
        ]
        if len(description_candidates) != 1:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース説明文の最終行を期待位置で確認できません。",
            )
        description_rect = description_candidates[0].union_rect
        if description_rect.intersects(heading_rect) or description_rect.intersects(
            opening_rectangles[0]
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース説明文と見出しまたは開講日が重なっています。",
            )

        general_course_heading = _single_search_group(
            general_page,
            GENERAL_COURSE_HEADING_TEXT,
            "General Course見出し",
        ).union_rect
        if any(
            abs(actual - expected) > COURSE_GAP_TOLERANCE
            for actual, expected in zip(
                tuple(general_course_heading), tuple(description_plan.general_heading_rect)
            )
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "General Course見出しの位置が変わっています。",
            )
        moved_first_line_rect = description_plan.original_first_line_rect.__class__(
            description_plan.original_first_line_rect.x0,
            description_plan.original_first_line_rect.y0
            - description_plan.upward_offset,
            description_plan.original_first_line_rect.x1,
            description_plan.original_first_line_rect.y1
            - description_plan.upward_offset,
        )
        saved_general_gap = moved_first_line_rect.y0 - general_course_heading.y1
        planned_general_gap = (
            description_plan.original_gap - description_plan.upward_offset
        )
        if (
            saved_general_gap < MINIMUM_GENERAL_DESCRIPTION_GAP
            or abs(saved_general_gap - planned_general_gap) > COURSE_GAP_TOLERANCE
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "General Course見出しと説明文の間隔が"
                f"計画と一致しません。（実際：{saved_general_gap}／"
                f"期待：{planned_general_gap}／"
                f"最低：{MINIMUM_GENERAL_DESCRIPTION_GAP}）",
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
        allowed_by_page: dict[int, list[AllowedChange]] = {}
        for item in prepared:
            allowed_by_page.setdefault(item.spec.page_index, []).append(
                AllowedChange(item.spec.label, item.changed_rect)
            )
        for move in moves:
            allowed_by_page.setdefault(move.page_index, []).append(
                AllowedChange(move.text, move.changed_rect)
            )
        diagnostics = []
        for page_index, allowed_changes in allowed_by_page.items():
            diagnosis = _diagnose_edited_page_outside_rectangles(
                output_doc[page_index],
                page_index,
                rendering_by_page[page_index],
                allowed_changes,
                output_path.parent,
            )
            if diagnosis is not None:
                diagnostics.append(diagnosis)
        if diagnostics:
            error = ReplacementError(
                "保存後の検証に失敗しました。",
                "指定された文字列の矩形外で見た目が変わっています。\n\n"
                + "\n\n".join(
                    _format_difference_diagnosis(diagnosis)
                    for diagnosis in diagnostics
                ),
            )
            error.diagnostics = tuple(diagnostics)
            raise error
    finally:
        output_doc.close()


def save_and_validate(
    pymupdf: Any,
    doc: Any,
    output_path: Path,
    snapshot: DocumentSnapshot,
    prepared: Sequence[PreparedReplacement],
    moves: Sequence[PreparedMove],
    original_heading_rect: Any,
    description_plan: GeneralDescriptionPlan,
) -> None:
    """一時保存を検証し、失敗時は確認用_error PDFとして残す。"""
    temporary_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp.pdf"
    )
    try:
        doc.save(temporary_path)
        try:
            validate_output_pdf(
                pymupdf,
                temporary_path,
                snapshot,
                prepared,
                moves,
                original_heading_rect,
                description_plan,
            )
        except Exception as validation_exc:
            exc = (
                validation_exc
                if isinstance(validation_exc, ReplacementError)
                else ReplacementError(
                    "保存後の検証中に予期しない問題が発生しました。",
                    f"{type(validation_exc).__name__}: {validation_exc}",
                )
            )
            error_pdf_base = output_path.with_name(
                f"{output_path.stem}_error{output_path.suffix}"
            )
            try:
                error_pdf_path = find_available_path(error_pdf_base)
                temporary_path.replace(error_pdf_path)
                exc.error_pdf_path = error_pdf_path
            except Exception as preserve_exc:
                preserve_detail = (
                    "検証失敗PDFを保存できませんでした。"
                    f"（{type(preserve_exc).__name__}: {preserve_exc}）"
                )
                exc.detail = (
                    f"{exc.detail}\n{preserve_detail}"
                    if exc.detail
                    else preserve_detail
                )
            if exc is validation_exc:
                raise
            raise exc from validation_exc
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
        lines = [
            "処理結果：エラー",
            f"入力PDF：{INPUT_PDF_NAME}",
            "対象ページ：3ページ目、4ページ目",
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
            )
        )
        if error.error_pdf_path is not None:
            lines.extend(
                (
                    f"検証失敗PDF：{error.error_pdf_path.name}",
                    "注意：このPDFは保存後検証に失敗しているため、確認用です。",
                )
            )
        lines.append(
            f"発生日時：{datetime.now().astimezone().isoformat(timespec='seconds')}"
        )
        error_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        return error_path
    except Exception as exc:
        print(f"エラーファイルを作成できませんでした。詳細：{exc}", file=sys.stderr)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """4箇所の文字列差し替えを安全に実行する。"""
    args = parse_arguments(argv)
    program_dir = Path(__file__).resolve().parent
    doc: Any | None = None
    try:
        input_path = find_input_pdf(program_dir)
        output_path = find_available_path(program_dir / OUTPUT_PDF_NAME)
        pymupdf = load_pymupdf()

        print(f"入力PDF：{input_path.name}")
        doc = open_pdf(pymupdf, input_path)
        description_plan = prepare_general_description_spacing(pymupdf, doc)
        prepared = prepare_replacements(pymupdf, doc, description_plan)
        general_schedule_heading_rect = validate_general_schedule_heading_position(
            pymupdf, doc, prepared
        )
        moves = description_plan.moves + prepare_following_line_moves(
            pymupdf, doc, prepared
        )
        snapshot = snapshot_document(doc)

        apply_replacements(pymupdf, doc, prepared, moves)
        save_and_validate(
            pymupdf,
            doc,
            output_path,
            snapshot,
            prepared,
            moves,
            general_schedule_heading_rect,
            description_plan,
        )

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
        if exc.error_pdf_path is not None:
            print(f"検証失敗PDF：{exc.error_pdf_path.name}", file=sys.stderr)
            print(
                "注意：このPDFは保存後検証に失敗しているため、確認用です。",
                file=sys.stderr,
            )
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
