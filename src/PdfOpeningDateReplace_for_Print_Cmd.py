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
import math
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
IMAGE_OUTPUT_PDF_NAME = "チラシ用_阿波なるとAI塾_変更後_画像版.pdf"
IMAGE_OUTPUT_PAGE1_PDF_NAME = "チラシ用_阿波なるとAI塾_変更後_画像版p1.pdf"
IMAGE_OUTPUT_PAGE2_PDF_NAME = "チラシ用_阿波なるとAI塾_変更後_画像版p2.pdf"
ERROR_FILE_NAME = "チラシ用_阿波なるとAI塾_変更後_error.txt"

ADDRESS_PAGE_INDEX = 0
OLD_ADDRESS_TEXT = "鳴門市木津町木津野7-11"
OLD_ADDRESS_TEXT_ALIASES = ("鳴⾨市⽊津町⽊津野7-11",)
NEW_ADDRESS_TEXT = "鳴門市大津町木津野内田7-11"
REQUIRED_POSTAL_CODE_TEXT = "〒772-0031"
REQUIRED_PHONE_TEXT = "０９０−４７８０−２９６７"

OPENING_DATE_PAGE_INDEX = 1
OLD_CHILD_OPENING_DATE_TEXT = "令和８年８月６日開講"
NEW_CHILD_OPENING_DATE_TEXT = "令和８年９月３日開講／令和８年10月８日開講"

OLD_GENERAL_OPENING_DATE_TEXT = "令和８年９月４日開講"
NEW_GENERAL_OPENING_DATE_LINES = (
    "令和８年９月４日開講／令和８年９月18日開講",
    "令和８年10月２日開講／令和８年10月16日開講",
)
GENERAL_SCHEDULE_HEADING_TEXT = "受講日時"
GENERAL_DESCRIPTION_LAST_LINE_TEXT = "つける事が大切です。"

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
IMAGE_OUTPUT_DPI = 350
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
    old_text_aliases: tuple[str, ...] = ()

    @property
    def new_text(self) -> str:
        """ログとエラー情報向けに変更後文字列を改行付きで返す。"""
        return "\n".join(self.new_lines)

    @property
    def old_text_candidates(self) -> tuple[str, ...]:
        """PDF内部表記の違いを許容する旧文字列候補を返す。"""
        return (self.old_text, *self.old_text_aliases)


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
    characters: tuple[str, ...]
    character_rectangles: tuple[Any, ...]


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
    protected_text_renderings: tuple[
        tuple[str, tuple[float, float, float, float], int, int, int, int, bytes], ...
    ]


@dataclass(frozen=True)
class RasterizedPage:
    """画像版PDFの全出力で共有する1ページ分の可逆RGB画像。"""

    page_index: int
    png_bytes: bytes
    pixel_width: int
    pixel_height: int
    sample_hash: str
    page_width: float
    page_height: float
    normalized_box: tuple[float, float, float, float]
    rotation: int


REPLACEMENTS = (
    ReplacementSpec(
        "住所",
        ADDRESS_PAGE_INDEX,
        OLD_ADDRESS_TEXT,
        (NEW_ADDRESS_TEXT,),
        OLD_ADDRESS_TEXT_ALIASES,
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
        if spec.old_text_aliases:
            print(f"検索別表記：{'／'.join(spec.old_text_aliases)}")
            for candidate in spec.old_text_candidates:
                code_points = " ".join(
                    f"{character}=U+{ord(character):04X}" for character in candidate
                )
                print(f"検索候補repr：{candidate!r}")
                print(f"検索候補コードポイント：{code_points}")
        found_page_indexes: list[int] = []
        for page_index, page in enumerate(doc):
            rectangles = search_for_old_text_candidates(page, spec)
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
    rectangles = search_for_old_text_candidates(page, spec)
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


def normalize_address_for_comparison(text: str) -> str:
    """住所の検証時だけ、PDF再抽出で変化するハイフン類をASCIIへ統一する。"""
    normalized = normalize_whitespace_for_comparison(text)
    hyphen_variants = {
        "\u002d",  # HYPHEN-MINUS
        "\u2010",  # HYPHEN
        "\u2011",  # NON-BREAKING HYPHEN
        "\u2013",  # EN DASH
        "\u2212",  # MINUS SIGN
        "\uff0d",  # FULLWIDTH HYPHEN-MINUS
    }
    return "".join(
        "-" if character in hyphen_variants else character
        for character in normalized
    )


def search_for_old_text_candidates(page: Any, spec: ReplacementSpec) -> tuple[Any, ...]:
    """通常漢字・PDF由来の部首文字候補を検索し、同一矩形をまとめて返す。"""
    rectangles: list[Any] = []
    for candidate in spec.old_text_candidates:
        candidate_rectangles = tuple(page.search_for(candidate))
        if candidate_rectangles:
            print(f"採用可能な検索候補：{candidate!r}（{len(candidate_rectangles)}件）")
        for candidate_rect in candidate_rectangles:
            if not any(
                overlap_ratio(candidate_rect, existing_rect) >= 0.99
                for existing_rect in rectangles
            ):
                rectangles.append(candidate_rect)
    return tuple(rectangles)


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
                    tuple(
                        str(character.get("c", ""))
                        for character, _ in visible_candidates
                    ),
                    tuple(
                        page.rect.__class__(character["bbox"])
                        for character, _ in visible_candidates
                    ),
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


def _intersection_detail(rectangle1: Any, rectangle2: Any) -> tuple[Any, float, float, float, float, float]:
    """2矩形の交差矩形、寸法、面積、および双方に対する面積率を返す。"""
    intersection = rectangle1 & rectangle2
    if intersection.is_empty:
        return intersection, 0.0, 0.0, 0.0, 0.0, 0.0
    area = intersection.get_area()
    area1 = rectangle1.get_area()
    area2 = rectangle2.get_area()
    return (
        intersection,
        max(0.0, float(intersection.width)),
        max(0.0, float(intersection.height)),
        area,
        area / area1 if area1 > 0 else 0.0,
        area / area2 if area2 > 0 else 0.0,
    )


def _address_character_rectangles(layers: Sequence[TextLayer]) -> tuple[Any, ...]:
    """重複レイヤーを含む住所文字だけのbboxを、同一矩形をまとめて返す。"""
    rectangles: list[Any] = []
    for layer_number, layer in enumerate(layers, start=1):
        for character_number, (character, rectangle) in enumerate(
            zip(layer.characters, layer.character_rectangles), start=1
        ):
            print(
                f"住所 layer {layer_number} 文字 {character_number}："
                f"{character!r} U+{ord(character):04X} bbox={tuple(rectangle)}"
            )
            if not any(tuple(existing) == tuple(rectangle) for existing in rectangles):
                rectangles.append(rectangle)
    if not rectangles:
        raise ReplacementError("住所を構成する文字bboxを取得できませんでした。")
    print(f"住所文字単位の最終削除矩形数：{len(rectangles)}件")
    for number, rectangle in enumerate(rectangles, start=1):
        print(f"住所最終削除矩形 {number}：{tuple(rectangle)}")
    return tuple(rectangles)


def _clip_address_rectangles_away_from_non_target_characters(
    page: Any,
    layers: Sequence[TextLayer],
    deletion_rectangles: Sequence[Any],
) -> tuple[Any, ...]:
    """隣接する対象外文字bboxとの境界まで、住所削除矩形の各辺を切り詰める。"""
    target_characters = tuple(
        (normalize_whitespace_for_comparison(character), rectangle)
        for layer in layers
        for character, rectangle in zip(layer.characters, layer.character_rectangles)
    )
    non_target_characters: list[tuple[str, Any]] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for character in span.get("chars", []):
                    character_text = str(character.get("c", ""))
                    if not character_text:
                        continue
                    character_rect = page.rect.__class__(character["bbox"])
                    normalized_character = normalize_whitespace_for_comparison(
                        character_text
                    )
                    if any(
                        normalized_character == target_character
                        and overlap_ratio(character_rect, target_rect) >= 0.99
                        for target_character, target_rect in target_characters
                    ):
                        continue
                    non_target_characters.append((character_text, character_rect))

    clipped_rectangles: list[Any] = []
    for number, deletion_rect in enumerate(deletion_rectangles, start=1):
        target_center_x = (deletion_rect.x0 + deletion_rect.x1) / 2.0
        target_center_y = (deletion_rect.y0 + deletion_rect.y1) / 2.0
        blockers = [
            (character_text, character_rect)
            for character_text, character_rect in non_target_characters
            if character_rect.x0 < deletion_rect.x1
            and character_rect.x1 > deletion_rect.x0
            and character_rect.y0 < deletion_rect.y1
            and character_rect.y1 > deletion_rect.y0
        ]
        if not blockers:
            clipped_rectangles.append(deletion_rect)
            continue

        safe_left = float(deletion_rect.x0)
        safe_top = float(deletion_rect.y0)
        safe_right = float(deletion_rect.x1)
        safe_bottom = float(deletion_rect.y1)
        for character_text, character_rect in blockers:
            character_center_x = (character_rect.x0 + character_rect.x1) / 2.0
            character_center_y = (character_rect.y0 + character_rect.y1) / 2.0
            delta_x = character_center_x - target_center_x
            delta_y = character_center_y - target_center_y
            # 中心間の差が大きい軸を使い、住所から見た隣接方向を決める。
            if abs(delta_x) >= abs(delta_y):
                if delta_x < 0:
                    safe_left = max(
                        safe_left,
                        math.nextafter(float(character_rect.x1), math.inf),
                    )
                else:
                    safe_right = min(
                        safe_right,
                        math.nextafter(float(character_rect.x0), -math.inf),
                    )
            elif delta_y < 0:
                safe_top = max(
                    safe_top,
                    math.nextafter(float(character_rect.y1), math.inf),
                )
            else:
                safe_bottom = min(
                    safe_bottom,
                    math.nextafter(float(character_rect.y0), -math.inf),
                )

        if safe_left >= safe_right or safe_top >= safe_bottom:
            blocker_detail = "／".join(
                f"{text!r} bbox={tuple(rect)}" for text, rect in blockers
            )
            raise ReplacementError(
                "住所文字だけを削除できる領域を確保できませんでした。",
                f"住所削除矩形：{tuple(deletion_rect)}／対象外文字：{blocker_detail}",
            )
        clipped_rect = page.rect.__class__(
            safe_left,
            safe_top,
            safe_right,
            safe_bottom,
        )
        if clipped_rect.get_area() <= 0 or (clipped_rect & deletion_rect).get_area() <= 0:
            raise ReplacementError("住所の削除矩形を安全に切り詰められませんでした。")
        print(f"住所削除矩形 {number} 切り詰め前：{tuple(deletion_rect)}")
        print(
            f"住所削除矩形 {number} 安全な境界："
            f"left={safe_left}, top={safe_top}, right={safe_right}, bottom={safe_bottom}"
        )
        print(f"住所削除矩形 {number} 切り詰め後：{tuple(clipped_rect)}")
        clipped_rectangles.append(clipped_rect)
    return tuple(clipped_rectangles)


def _ensure_address_character_rectangles_are_safe(
    page: Any,
    layers: Sequence[TextLayer],
    deletion_rectangles: Sequence[Any],
) -> None:
    """住所文字bboxが郵便番号・電話番号などの対象外文字へ侵入しないことを確認する。"""
    target_characters = tuple(
        (normalize_whitespace_for_comparison(character), rectangle)
        for layer in layers
        for character, rectangle in zip(layer.characters, layer.character_rectangles)
    )
    address_rect = page.rect.__class__(deletion_rectangles[0])
    for rectangle in deletion_rectangles[1:]:
        address_rect |= rectangle
    print(f"住所文字bboxの和集合：{tuple(address_rect)}")

    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = "".join(
                    str(character.get("c", "")) for character in span.get("chars", [])
                )
                if not span_text:
                    continue
                span_rect = page.rect.__class__(span["bbox"])
                span_intersection = _intersection_detail(address_rect, span_rect)
                if span_intersection[3] > 0:
                    print(f"住所周辺span文字列：{span_text!r}")
                    print(f"住所周辺span bbox：{tuple(span_rect)}")
                    print(f"span交差bbox：{tuple(span_intersection[0])}")
                    print(f"span交差幅：{span_intersection[1]}")
                    print(f"span交差高さ：{span_intersection[2]}")
                    print(f"span交差面積：{span_intersection[3]}")
                    print(f"住所bboxに対するspan交差率：{span_intersection[4]}")
                    print(f"span bboxに対する交差率：{span_intersection[5]}")

                for character in span.get("chars", []):
                    character_text = str(character.get("c", ""))
                    if not character_text:
                        continue
                    character_rect = page.rect.__class__(character["bbox"])
                    normalized_character = normalize_whitespace_for_comparison(
                        character_text
                    )
                    is_target_character = any(
                        normalized_character == target_character
                        and overlap_ratio(character_rect, target_rect) >= 0.99
                        for target_character, target_rect in target_characters
                    )
                    if is_target_character:
                        continue
                    for deletion_rect in deletion_rectangles:
                        detail = _intersection_detail(deletion_rect, character_rect)
                        if detail[3] <= 0:
                            continue
                        collision_detail = (
                            f"対象外文字：{character_text!r}／"
                            f"文字bbox：{tuple(character_rect)}／"
                            f"削除矩形：{tuple(deletion_rect)}／"
                            f"交差bbox：{tuple(detail[0])}／交差幅：{detail[1]}／"
                            f"交差高さ：{detail[2]}／交差面積：{detail[3]}／"
                            f"削除矩形に対する交差率：{detail[4]}／"
                            f"対象外文字bboxに対する交差率：{detail[5]}"
                        )
                        print("住所文字単位安全確認：" + collision_detail)
                        raise ReplacementError(
                            "住所の削除矩形が対象外文字へ侵入するため、処理を中止しました。",
                            collision_detail,
                        )
    print("住所の削除矩形が対象外の各文字bboxへ侵入しないことを確認しました。")


def _ensure_deletion_rectangles_are_safe(
    page: Any,
    layers: Sequence[TextLayer],
    search_group: SearchGroup,
    spec: ReplacementSpec,
) -> tuple[Any, ...]:
    """安全確認済みの最小削除矩形を返す。住所だけ文字単位で処理する。"""
    if spec.label == "住所":
        deletion_rectangles = _address_character_rectangles(layers)
        deletion_rectangles = _clip_address_rectangles_away_from_non_target_characters(
            page, layers, deletion_rectangles
        )
        _ensure_address_character_rectangles_are_safe(
            page, layers, deletion_rectangles
        )
        return deletion_rectangles
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
            normalized_combined_text = normalize_whitespace_for_comparison(
                combined_text
            )
            text_matches = (
                all(text == normalized_old_text for text in normalized_span_texts)
                or normalized_combined_text == normalized_old_text
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
    return tuple(layer.rect for layer in layers)


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
    deletion_rectangles = _ensure_deletion_rectangles_are_safe(
        page, layers, search_group, spec
    )
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
    return style, deletion_rectangles


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


def _address_new_rect_collisions(
    page: Any, old_address_rect: Any, new_address_rect: Any
) -> list[str]:
    """新住所bboxと、旧住所文字を除く各文字bboxとの実質的な交差を返す。"""
    collisions: list[str] = []
    normalized_old_address = normalize_whitespace_for_comparison(OLD_ADDRESS_TEXT)
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            line_characters = [
                character
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            ]
            line_text = "".join(
                str(character.get("c", "")) for character in line_characters
            )
            is_address_line = normalized_old_address in normalize_whitespace_for_comparison(
                line_text
            )
            for character_index, character in enumerate(line_characters):
                character_text = str(character.get("c", ""))
                if not character_text:
                    continue
                character_rect = page.rect.__class__(character["bbox"])
                if is_address_line and overlap_ratio(character_rect, old_address_rect) > 0:
                    continue
                detail = _intersection_detail(new_address_rect, character_rect)
                if detail[3] > 0:
                    has_non_whitespace_after = any(
                        not str(later_character.get("c", "")).isspace()
                        for later_character in line_characters[character_index + 1 :]
                        if str(later_character.get("c", ""))
                    )
                    is_usable_trailing_whitespace = (
                        character_text.isspace()
                        and is_address_line
                        and character_rect.x0 >= old_address_rect.x1
                        and not has_non_whitespace_after
                        and character_rect.y0 < new_address_rect.y1
                        and character_rect.y1 > new_address_rect.y0
                    )
                    if is_usable_trailing_whitespace:
                        print("住所配置確認：旧住所直後の末尾空白領域を使用します。")
                        print(f"空白bbox：{tuple(character_rect)}")
                        print(f"新住所bbox：{tuple(new_address_rect)}")
                        print("空白の後ろに非空白文字：なし")
                        continue
                    new_center_y = (new_address_rect.y0 + new_address_rect.y1) / 2.0
                    character_center_y = (character_rect.y0 + character_rect.y1) / 2.0
                    if (
                        character_center_y > new_center_y
                        and character_rect.y0 > new_address_rect.y0
                        and detail[0].y1 == new_address_rect.y1
                    ):
                        print(
                            "新住所bbox下辺と直下文字bboxの形式上の交差："
                            f"文字={character_text!r}／交差bbox={tuple(detail[0])}／"
                            f"交差面積={detail[3]}。保存後の保護領域画素で検証します。"
                        )
                        continue
                    collisions.append(
                        f"{character_text!r} bbox={tuple(character_rect)} "
                        f"intersection={tuple(detail[0])} area={detail[3]}"
                    )
    return collisions


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
    origin_offset_y: float = 0.0,
    line_advance_override: float | None = None,
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
            if spec.label == "住所":
                colliding_texts = [
                    collision
                    for line_rect in line_rectangles
                    for collision in _address_new_rect_collisions(
                        page, style.bbox, line_rect
                    )
                ]
            else:
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
    prepared_by_old_text: dict[str, PreparedReplacement] = {}
    schedule_spec = next(
        spec for spec in REPLACEMENTS if spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
    )
    ordered_specs = (schedule_spec,) + tuple(
        spec for spec in REPLACEMENTS if spec is not schedule_spec
    )
    for spec in ordered_specs:
        page = doc[spec.page_index]
        search_group = find_target_text(page, spec)
        style, deletion_rectangles = get_original_text_style(page, search_group, spec)
        ensure_text_only_redaction_supported(pymupdf, page)
        font_path = find_japanese_font(style, spec)
        origin_offset_y = 0.0
        line_advance_override: float | None = None
        if spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT:
            schedule = prepared_by_old_text[OLD_GENERAL_SCHEDULE_TEXT]
            origin_offset_y = -schedule.line_advance
            line_advance_override = schedule.line_advance
        font_size, changed_rect, line_advance = calculate_placement(
            pymupdf,
            page,
            style,
            font_path,
            spec,
            origin_offset_y,
            line_advance_override,
        )
        print(f"使用フォント：{font_path}")
        print(f"変更後文字列：{spec.new_text}")
        print(f"挿入文字サイズ：{font_size}\n")
        if origin_offset_y:
            print(f"一般コース開講日の上方向移動量：{origin_offset_y}\n")
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


def _is_general_schedule_heading_candidate(
    page: Any, candidate_rect: Any, opening_rect: Any
) -> bool:
    """同じ段の2見出しから、右側の一般コース見出しだけを判定する。"""
    page_center_x = page.rect.x0 + page.rect.width / 2.0
    candidate_center_x = (candidate_rect.x0 + candidate_rect.x1) / 2.0
    candidate_center_y = (candidate_rect.y0 + candidate_rect.y1) / 2.0
    opening_center_y = (opening_rect.y0 + opening_rect.y1) / 2.0
    return (
        candidate_rect.x1 < opening_rect.x0
        and candidate_center_x > page_center_x
        and abs(candidate_center_y - opening_center_y)
        <= max(candidate_rect.height, opening_rect.height)
    )


def prepare_general_schedule_heading_move(
    pymupdf: Any,
    doc: Any,
    replacements: Sequence[PreparedReplacement],
) -> PreparedMove:
    """一般コースの受講日時見出しだけを、実測した1行分だけ上へ移動する。"""
    schedule = next(
        item for item in replacements if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
    )
    opening_date = next(
        item
        for item in replacements
        if item.spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT
    )
    if schedule.line_advance <= 0:
        raise ReplacementError(
            "一般コースの受講日時見出しを安全に移動できません。",
            "受講日時2行のベースライン間隔を取得できません。",
        )

    page = doc[OPENING_DATE_PAGE_INDEX]
    heading_groups = group_overlapping_rectangles(
        tuple(page.search_for(GENERAL_SCHEDULE_HEADING_TEXT))
    )
    page_center_x = page.rect.x0 + page.rect.width / 2.0
    opening_center_y = (
        opening_date.style.bbox.y0 + opening_date.style.bbox.y1
    ) / 2.0
    for index, group in enumerate(heading_groups, start=1):
        candidate_rect = group.union_rect
        candidate_center_x = (candidate_rect.x0 + candidate_rect.x1) / 2.0
        candidate_center_y = (candidate_rect.y0 + candidate_rect.y1) / 2.0
        horizontal_gap = opening_date.style.bbox.x0 - candidate_rect.x1
        vertical_center_gap = abs(candidate_center_y - opening_center_y)
        print(f"受講日時見出し候補 {index}：bbox={tuple(candidate_rect)}")
        print(
            f"  中心X={candidate_center_x}／中心Y={candidate_center_y}／"
            f"開講日までの水平距離={horizontal_gap}／"
            f"Y方向中心差={vertical_center_gap}／"
            f"ページ右半分={candidate_center_x > page_center_x}"
        )
    heading_candidates = [
        group
        for group in heading_groups
        if _is_general_schedule_heading_candidate(
            page, group.union_rect, opening_date.style.bbox
        )
    ]
    if len(heading_candidates) != 1:
        raise ReplacementError(
            "一般コースの受講日時見出しを一意に特定できません。",
            f"表示候補数：{len(heading_candidates)}件",
        )

    move_spec = ReplacementSpec(
        "一般コース受講日時見出し",
        OPENING_DATE_PAGE_INDEX,
        GENERAL_SCHEDULE_HEADING_TEXT,
        (GENERAL_SCHEDULE_HEADING_TEXT,),
    )
    style, deletion_rectangles = get_original_text_style(
        page, heading_candidates[0], move_spec
    )
    font_path = find_japanese_font(style, move_spec)
    y_offset = -schedule.line_advance
    new_rect = style.bbox.__class__(
        style.bbox.x0,
        style.bbox.y0 + y_offset,
        style.bbox.x1,
        style.bbox.y1 + y_offset,
    )
    if not page.rect.contains(new_rect):
        raise ReplacementError(
            "一般コースの受講日時見出しを欄内へ移動できません。"
        )
    if _crosses_new_graphics(page, style.bbox, new_rect):
        raise ReplacementError(
            "一般コースの受講日時見出しが背景線または図形へ重なります。"
        )

    description_groups = group_overlapping_rectangles(
        tuple(page.search_for(GENERAL_DESCRIPTION_LAST_LINE_TEXT))
    )
    description_candidates = [
        group
        for group in description_groups
        if group.union_rect.y1 <= style.bbox.y0
        and group.union_rect.x0 >= style.bbox.x0 - 1.0
    ]
    if not description_candidates:
        raise ReplacementError(
            "一般コース説明文の最終行を確認できません。",
            f"検索文字列：{GENERAL_DESCRIPTION_LAST_LINE_TEXT}",
        )
    description_group = max(
        description_candidates, key=lambda group: group.union_rect.y1
    )
    description_gap = new_rect.y0 - description_group.union_rect.y1
    if description_gap <= 0:
        raise ReplacementError(
            "一般コースの受講日時見出しを上へ移動できません。",
            f"説明文との余白：{description_gap}",
        )

    ignored_texts = tuple(spec.old_text for spec in REPLACEMENTS)
    collisions = [
        text
        for rect, text in _span_rectangles(page, style.bbox, ignored_texts)
        if new_rect.intersects(rect)
    ]
    if collisions:
        raise ReplacementError(
            "一般コースの受講日時見出しを安全に移動できません。",
            f"交差文字列：{' / '.join(collisions)}",
        )

    print(f"一般コース受講日時見出しのY方向移動量：{y_offset}")
    print(f"一般コース説明文との移動後余白：{description_gap}")
    print(f"一般コース受講日時見出しの移動前bbox：{tuple(style.bbox)}")
    print(f"一般コース受講日時見出しの移動後bbox：{tuple(new_rect)}")
    return PreparedMove(
        GENERAL_SCHEDULE_HEADING_TEXT,
        OPENING_DATE_PAGE_INDEX,
        style,
        font_path,
        deletion_rectangles,
        y_offset,
        style.bbox | new_rect,
    )


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
    protected_renderings = []
    address_page = doc[ADDRESS_PAGE_INDEX]
    for required_text in (REQUIRED_POSTAL_CODE_TEXT, REQUIRED_PHONE_TEXT):
        groups = group_overlapping_rectangles(
            tuple(address_page.search_for(required_text))
        )
        if len(groups) != 1:
            raise ReplacementError(
                "住所周辺の維持対象文字列を一意に確認できませんでした。",
                f"維持対象：{required_text}／表示グループ数：{len(groups)}件",
            )
        protected_rect = groups[0].union_rect
        protected_pixmap = address_page.get_pixmap(
            clip=protected_rect, alpha=False
        )
        protected_renderings.append(
            (
                required_text,
                tuple(float(value) for value in protected_rect),
                protected_pixmap.width,
                protected_pixmap.height,
                protected_pixmap.n,
                protected_pixmap.stride,
                bytes(protected_pixmap.samples),
            )
        )
    return DocumentSnapshot(
        doc.page_count,
        sizes,
        texts,
        hashes,
        tuple(renderings),
        tuple(protected_renderings),
    )


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
    normalized_text = normalize_whitespace_for_comparison(text)
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = "".join(char.get("c", "") for char in span.get("chars", []))
                if normalize_whitespace_for_comparison(span_text) == normalized_text:
                    return True
    return False


def _normalize_for_old_text_removal_check(text: str) -> str:
    """旧表記の残存確認では、全角・半角を維持して改行だけを除く。"""
    return text.replace("\r", "").replace("\n", "")


def _old_general_schedule_text_remains(page: Any, spec: ReplacementSpec) -> bool:
    """受講日時の新旧表記を区別し、旧全角表記だけの残存を確認する。"""
    old_search_rectangles = tuple(page.search_for(spec.old_text))
    print(
        "保存後検証：一般コース受講日時の変更前文字列search_for()件数："
        f"{len(old_search_rectangles)}件"
    )
    print(f"変更前文字列repr：{spec.old_text!r}")
    print(f"変更後1行目repr：{spec.new_lines[0]!r}")
    print(
        "変更前文字列の現在の正規化結果："
        f"{normalize_whitespace_for_comparison(spec.old_text)!r}"
    )
    print(
        "変更後1行目の現在の正規化結果："
        f"{normalize_whitespace_for_comparison(spec.new_lines[0])!r}"
    )
    if old_search_rectangles:
        return True

    old_text = _normalize_for_old_text_removal_check(spec.old_text)
    rawdict = page.get_text("rawdict")
    for block in rawdict.get("blocks", []):
        for line in block.get("lines", []):
            line_parts: list[str] = []
            for span in line.get("spans", []):
                span_text = "".join(
                    str(character.get("c", ""))
                    for character in span.get("chars", [])
                )
                line_parts.append(span_text)
                if "金曜日" in span_text or "20" in span_text or "２０" in span_text:
                    print(
                        "保存後受講日時周辺span："
                        f"text={span_text!r}／bbox={tuple(span.get('bbox', ()))!r}"
                    )
            line_text = _normalize_for_old_text_removal_check("".join(line_parts))
            if old_text in line_text:
                return True
    return False


def _old_text_remains(page: Any, spec: ReplacementSpec) -> bool:
    """検索候補とrawdictの双方で変更前文字列が残っていないか確認する。"""
    if spec.old_text == OLD_GENERAL_SCHEDULE_TEXT:
        return _old_general_schedule_text_remains(page, spec)
    if search_for_old_text_candidates(page, spec):
        return True
    normalized_candidates = {
        normalize_whitespace_for_comparison(candidate)
        for candidate in spec.old_text_candidates
    }
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            line_text = "".join(
                str(character.get("c", ""))
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            )
            normalized_line = normalize_whitespace_for_comparison(line_text)
            if any(candidate in normalized_line for candidate in normalized_candidates):
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

    normalize_for_validation = (
        normalize_address_for_comparison
        if spec.label == "住所"
        else normalize_whitespace_for_comparison
    )
    expected_normalized = normalize_for_validation(new_line)
    candidates = _line_candidates_near_rect(page, expected_rect)
    matching_candidates = [
        (text, rect, source)
        for text, rect, source in candidates
        if normalize_for_validation(text) == expected_normalized
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
                else _old_text_remains(page, spec)
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
            if move.text == GENERAL_SCHEDULE_HEADING_TEXT:
                page_center_x = page.rect.x0 + page.rect.width / 2.0
                child_heading_groups = [
                    group
                    for group in groups
                    if (group.union_rect.x0 + group.union_rect.x1) / 2.0
                    <= page_center_x
                ]
                if len(groups) != 2 or len(child_heading_groups) != 1:
                    raise ReplacementError(
                        "保存後の検証に失敗しました。",
                        "児童・一般コースの受講日時見出し2件を確認できません。"
                        f"（全表示グループ数：{len(groups)}件／"
                        f"児童コース側：{len(child_heading_groups)}件）",
                    )
                old_position_groups = [
                    group
                    for group in groups
                    if group.union_rect.intersects(move.style.bbox)
                    and overlap_ratio(group.union_rect, expected_rect) == 0
                ]
                if old_position_groups:
                    raise ReplacementError(
                        "保存後の検証に失敗しました。",
                        "一般コース受講日時見出しの変更前レイヤーが残っています。",
                    )

        general_opening = next(
            item
            for item in prepared
            if item.spec.old_text == OLD_GENERAL_OPENING_DATE_TEXT
        )
        general_schedule = next(
            item
            for item in prepared
            if item.spec.old_text == OLD_GENERAL_SCHEDULE_TEXT
        )
        heading_move = next(
            move for move in moves if move.text == GENERAL_SCHEDULE_HEADING_TEXT
        )
        general_page = output_doc[OPENING_DATE_PAGE_INDEX]
        opening_rectangles = _line_rectangles_for_validation(
            pymupdf, general_opening
        )
        schedule_rectangles = _line_rectangles_for_validation(
            pymupdf, general_schedule
        )
        moved_heading_rect = heading_move.style.bbox.__class__(
            heading_move.style.bbox.x0,
            heading_move.style.bbox.y0 + heading_move.y_offset,
            heading_move.style.bbox.x1,
            heading_move.style.bbox.y1 + heading_move.y_offset,
        )
        if (
            len(opening_rectangles) != 2
            or opening_rectangles[0].y0 >= opening_rectangles[1].y0
            or opening_rectangles[0].intersects(opening_rectangles[1])
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース開講日の2行の順序または行間が不正です。",
            )
        if not schedule_rectangles or opening_rectangles[1].intersects(
            schedule_rectangles[0]
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース開講日2行目が受講日時1行目と重なっています。",
            )
        description_groups = group_overlapping_rectangles(
            tuple(general_page.search_for(GENERAL_DESCRIPTION_LAST_LINE_TEXT))
        )
        description_candidates = [
            group
            for group in description_groups
            if group.union_rect.y1 <= moved_heading_rect.y0
            and group.union_rect.x0 >= moved_heading_rect.x0 - 1.0
        ]
        if len(description_candidates) != 1:
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース説明文の最終行を期待位置で確認できません。",
            )
        description_rect = description_candidates[0].union_rect
        if description_rect.intersects(moved_heading_rect) or description_rect.intersects(
            opening_rectangles[0]
        ):
            raise ReplacementError(
                "保存後の検証に失敗しました。",
                "一般コース説明文と移動後の見出しまたは開講日が重なっています。",
            )

        address_page = output_doc[ADDRESS_PAGE_INDEX]
        for required_text, label in (
            (REQUIRED_POSTAL_CODE_TEXT, "郵便番号"),
            (REQUIRED_PHONE_TEXT, "電話番号"),
        ):
            if not address_page.search_for(required_text):
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"維持する{label}「{required_text}」が見つかりません。",
                )
        for (
            required_text,
            rect_values,
            width,
            height,
            components,
            stride,
            original_samples,
        ) in snapshot.protected_text_renderings:
            protected_rect = output_doc[ADDRESS_PAGE_INDEX].rect.__class__(rect_values)
            protected_pixmap = address_page.get_pixmap(
                clip=protected_rect, alpha=False
            )
            if (
                protected_pixmap.width,
                protected_pixmap.height,
                protected_pixmap.n,
                protected_pixmap.stride,
            ) != (width, height, components, stride) or bytes(
                protected_pixmap.samples
            ) != original_samples:
                raise ReplacementError(
                    "保存後の検証に失敗しました。",
                    f"維持対象「{required_text}」の見た目が変わっています。",
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


def find_available_image_output_paths(program_dir: Path) -> tuple[Path, Path, Path]:
    """画像版3ファイルで共通の未使用連番を確保する。"""
    base_paths = (
        program_dir / IMAGE_OUTPUT_PDF_NAME,
        program_dir / IMAGE_OUTPUT_PAGE1_PDF_NAME,
        program_dir / IMAGE_OUTPUT_PAGE2_PDF_NAME,
    )
    if not any(path.exists() for path in base_paths):
        return base_paths
    for number in range(1, 10_000):
        candidates = tuple(
            path.with_name(f"{path.stem}_{number:04d}{path.suffix}")
            for path in base_paths
        )
        if not any(path.exists() for path in candidates):
            return candidates
    raise ReplacementError(
        "画像版PDFの未使用連番ファイル名を確保できませんでした。",
        "画像版3ファイルの連番0001～9999がすべて使用されています。",
    )


def _pixmap_hash(pixmap: Any) -> str:
    """ピクセル寸法・色成分・画素を含む画像ハッシュを返す。"""
    header = (
        f"{pixmap.width}:{pixmap.height}:{pixmap.n}:{pixmap.stride}"
    ).encode("ascii")
    return hashlib.sha256(header + bytes(pixmap.samples)).hexdigest()


def _rect_values(rect: Any) -> tuple[float, float, float, float]:
    """PyMuPDFの矩形を保存・比較可能な4要素へ変換する。"""
    return float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)


def _rect_dimensions(
    values: tuple[float, float, float, float],
) -> tuple[float, float]:
    """矩形の原点に依存しない幅と高さを返す。"""
    return values[2] - values[0], values[3] - values[1]


def _dimensions_match(
    values: tuple[float, float, float, float], width: float, height: float
) -> bool:
    """PDF座標の丸めを許容し、矩形がページと同じ物理寸法か確認する。"""
    actual_width, actual_height = _rect_dimensions(values)
    return abs(actual_width - width) <= 0.001 and abs(actual_height - height) <= 0.001


def _log_source_page_boxes(page_index: int, page: Any) -> None:
    """画像版へ変換する前の座標系と全ページボックスを記録する。"""
    print(f"画像版ページボックス調査 {page_index + 1}ページ目：")
    print(f"  page.rect={_rect_values(page.rect)}")
    print(f"  page.mediabox={_rect_values(page.mediabox)}")
    print(f"  page.cropbox={_rect_values(page.cropbox)}")
    print(f"  page.trimbox={_rect_values(page.trimbox)}")
    print(f"  page.bleedbox={_rect_values(page.bleedbox)}")
    print(f"  page.artbox={_rect_values(page.artbox)}")
    print(f"  page.rotation={page.rotation}")
    print(f"  page.transformation_matrix={page.transformation_matrix}")


def _normalized_image_page_box(
    pymupdf: Any, page_index: int, page: Any
) -> tuple[float, float, float, float]:
    """表示ページ全体を原点(0, 0)へ正規化できることを確認する。"""
    width = float(page.rect.width)
    height = float(page.rect.height)
    source_boxes = (
        ("MediaBox", _rect_values(page.mediabox)),
        ("CropBox", _rect_values(page.cropbox)),
        ("TrimBox", _rect_values(page.trimbox)),
        ("BleedBox", _rect_values(page.bleedbox)),
        ("ArtBox", _rect_values(page.artbox)),
    )
    different_boxes = tuple(
        (label, values, _rect_dimensions(values))
        for label, values in source_boxes
        if not _dimensions_match(values, width, height)
    )
    if different_boxes:
        details = "／".join(
            f"{label}={values}（幅x高さ={dimensions[0]}x{dimensions[1]}）"
            for label, values, dimensions in different_boxes
        )
        raise ReplacementError(
            "画像版PDFのページボックスを安全に正規化できませんでした。",
            f"対象ページ：{page_index + 1}ページ目／"
            f"表示ページ寸法：{width}x{height}／"
            f"表示ページと異なるボックス：{details}",
        )

    normalized = (0.0, 0.0, width, height)
    normalized_rect = pymupdf.Rect(normalized)
    if not normalized_rect.is_valid or normalized_rect.is_empty:
        raise ReplacementError(
            "画像版PDFの正規化ページボックスが不正です。",
            f"対象ページ：{page_index + 1}ページ目／box={normalized}",
        )
    print(f"  正規化後MediaBox/CropBox/TrimBox/BleedBox/ArtBox={normalized}")
    print("  正規化後CropBoxがMediaBoxに含まれるか=True")
    return normalized


def rasterize_output_pdf(pymupdf: Any, output_path: Path) -> tuple[RasterizedPage, ...]:
    """検証済み文字PDFの全ページをRGB・350 DPIの可逆画像へ変換する。"""
    try:
        source_doc = pymupdf.open(output_path)
    except Exception as exc:
        raise ReplacementError(
            "画像版の基準となる文字PDFを開けませんでした。", str(exc)
        ) from exc

    rasterized_pages: list[RasterizedPage] = []
    try:
        if source_doc.page_count != EXPECTED_PAGE_COUNT:
            raise ReplacementError(
                "画像版PDFを作成できません。",
                f"基準文字PDFのページ数：{source_doc.page_count}",
            )
        for page_index, page in enumerate(source_doc):
            _log_source_page_boxes(page_index, page)
            if page.rotation != 0:
                raise ReplacementError(
                    "画像版PDFでページ回転を安全に再現できません。",
                    f"対象ページ：{page_index + 1}ページ目／回転：{page.rotation}度",
                )
            normalized_box = _normalized_image_page_box(pymupdf, page_index, page)
            pixmap = page.get_pixmap(
                dpi=IMAGE_OUTPUT_DPI,
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            if pixmap.n != 3 or pixmap.alpha:
                raise ReplacementError(
                    "画像版PDF用のRGB画像を作成できません。",
                    f"対象ページ：{page_index + 1}ページ目／"
                    f"色成分数：{pixmap.n}／alpha：{pixmap.alpha}",
                )
            expected_width = round(page.rect.width / 72.0 * IMAGE_OUTPUT_DPI)
            expected_height = round(page.rect.height / 72.0 * IMAGE_OUTPUT_DPI)
            if (
                abs(pixmap.width - expected_width) > 1
                or abs(pixmap.height - expected_height) > 1
            ):
                raise ReplacementError(
                    "画像版PDF用画像のピクセル寸法が不正です。",
                    f"対象ページ：{page_index + 1}ページ目／"
                    f"実寸：{pixmap.width}x{pixmap.height}／"
                    f"期待値：{expected_width}x{expected_height}",
                )
            rasterized_pages.append(
                RasterizedPage(
                    page_index,
                    bytes(pixmap.tobytes("png")),
                    pixmap.width,
                    pixmap.height,
                    _pixmap_hash(pixmap),
                    float(page.rect.width),
                    float(page.rect.height),
                    normalized_box,
                    int(page.rotation),
                )
            )
            print(
                f"画像化 {page_index + 1}ページ目："
                f"{pixmap.width}x{pixmap.height}px／RGB／{IMAGE_OUTPUT_DPI} DPI"
            )
    finally:
        source_doc.close()
    return tuple(rasterized_pages)


def _set_image_page_boxes(pymupdf: Any, page: Any, raster: RasterizedPage) -> None:
    """画像ページへ原点を正規化した安全なページボックスを設定する。"""
    try:
        normalized_box = pymupdf.Rect(raster.normalized_box)
        page.set_mediabox(normalized_box)
        if not page.mediabox.contains(normalized_box):
            raise ValueError(
                f"正規化CropBoxがMediaBoxに含まれません。"
                f"MediaBox={_rect_values(page.mediabox)}／"
                f"CropBox={raster.normalized_box}"
            )
        page.set_cropbox(normalized_box)
        page.set_trimbox(normalized_box)
        page.set_bleedbox(normalized_box)
        page.set_artbox(normalized_box)
        page.set_rotation(raster.rotation)
        print(
            f"画像版 {raster.page_index + 1}ページ目ページボックス設定後："
            f"MediaBox={_rect_values(page.mediabox)}／"
            f"CropBox={_rect_values(page.cropbox)}／"
            f"TrimBox={_rect_values(page.trimbox)}／"
            f"BleedBox={_rect_values(page.bleedbox)}／"
            f"ArtBox={_rect_values(page.artbox)}"
        )
    except Exception as exc:
        raise ReplacementError(
            "画像版PDFへ正規化したページボックスを設定できませんでした。",
            f"対象ページ：{raster.page_index + 1}ページ目／詳細：{exc}",
        ) from exc


def _save_image_pdf(
    pymupdf: Any,
    temporary_path: Path,
    rasterized_pages: Sequence[RasterizedPage],
) -> None:
    """指定された可逆画像だけを配置したフォントなしPDFを一時保存する。"""
    image_doc = pymupdf.open()
    try:
        for raster in rasterized_pages:
            page = image_doc.new_page(
                width=raster.page_width, height=raster.page_height
            )
            _set_image_page_boxes(pymupdf, page, raster)
            page_ratio = page.rect.width / page.rect.height
            image_ratio = raster.pixel_width / raster.pixel_height
            ratio_tolerance = max(
                1.0 / raster.pixel_width, 1.0 / raster.pixel_height
            )
            if abs(page_ratio - image_ratio) > ratio_tolerance:
                raise ReplacementError(
                    "画像版PDFのページと画像の縦横比が一致しません。",
                    f"対象ページ：{raster.page_index + 1}ページ目／"
                    f"ページ比率：{page_ratio}／画像比率：{image_ratio}",
                )
            print(
                f"画像版 {raster.page_index + 1}ページ目画像配置："
                f"page.rect={_rect_values(page.rect)}／"
                f"画像サイズ={raster.pixel_width}x{raster.pixel_height}px／"
                f"ページ縦横比={page_ratio}／画像縦横比={image_ratio}／"
                f"縦横比差={abs(page_ratio - image_ratio)}／"
                f"許容値={ratio_tolerance}／keep_proportion=False／"
                f"挿入先rect={_rect_values(page.rect)}"
            )
            page.insert_image(
                page.rect,
                stream=raster.png_bytes,
                keep_proportion=False,
                overlay=True,
            )
        image_doc.save(temporary_path, garbage=4, deflate=True)
    except ReplacementError:
        raise
    except Exception as exc:
        raise ReplacementError("画像版PDFを保存できませんでした。", str(exc)) from exc
    finally:
        image_doc.close()


def _rect_values_match(
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
) -> bool:
    """PDF保存時の微小な座標丸めを許容して矩形を比較する。"""
    return all(abs(left - right) <= 0.001 for left, right in zip(actual, expected))


def validate_image_pdf(
    pymupdf: Any,
    image_pdf_path: Path,
    expected_pages: Sequence[RasterizedPage],
) -> None:
    """画像版PDFのページ、画像、フォント不在、見た目を検証する。"""
    try:
        image_doc = pymupdf.open(image_pdf_path)
    except Exception as exc:
        raise ReplacementError("画像版PDFを開き直せませんでした。", str(exc)) from exc
    try:
        if image_doc.needs_pass or image_doc.is_encrypted:
            raise ReplacementError("画像版PDFが暗号化されています。")
        if image_doc.page_count != len(expected_pages):
            raise ReplacementError(
                "画像版PDFのページ数が不正です。",
                f"実際：{image_doc.page_count}／期待：{len(expected_pages)}",
            )
        for output_index, raster in enumerate(expected_pages):
            page = image_doc[output_index]
            box_pairs = (
                ("MediaBox", _rect_values(page.mediabox), raster.normalized_box),
                ("CropBox", _rect_values(page.cropbox), raster.normalized_box),
                ("TrimBox", _rect_values(page.trimbox), raster.normalized_box),
                ("BleedBox", _rect_values(page.bleedbox), raster.normalized_box),
                ("ArtBox", _rect_values(page.artbox), raster.normalized_box),
            )
            for label, actual, expected in box_pairs:
                if not _rect_values_match(actual, expected):
                    raise ReplacementError(
                        "画像版PDFのページボックスが変わっています。",
                        f"出力{output_index + 1}ページ目／{label}／"
                        f"実際：{actual}／期待：{expected}",
                    )
            if not page.mediabox.contains(page.cropbox):
                raise ReplacementError(
                    "画像版PDFのCropBoxがMediaBoxに含まれていません。",
                    f"出力{output_index + 1}ページ目／"
                    f"MediaBox={_rect_values(page.mediabox)}／"
                    f"CropBox={_rect_values(page.cropbox)}",
                )
            if page.rotation != raster.rotation:
                raise ReplacementError(
                    "画像版PDFのページ回転が変わっています。",
                    f"出力{output_index + 1}ページ目",
                )
            fonts = tuple(page.get_fonts(full=True))
            if fonts:
                raise ReplacementError(
                    "画像版PDFにフォントリソースが残っています。",
                    f"出力{output_index + 1}ページ目／フォント数：{len(fonts)}",
                )
            if page.get_text().strip():
                raise ReplacementError(
                    "画像版PDFに選択可能なテキストが残っています。",
                    f"出力{output_index + 1}ページ目",
                )
            images = tuple(page.get_images(full=True))
            if len(images) != 1:
                raise ReplacementError(
                    "画像版PDFのページ画像数が不正です。",
                    f"出力{output_index + 1}ページ目／画像数：{len(images)}",
                )
            image_xref = int(images[0][0])
            image_rectangles = tuple(page.get_image_rects(image_xref))
            actual_image_rects = tuple(
                _rect_values(rect) for rect in image_rectangles
            )
            expected_image_rect = _rect_values(page.rect)
            image_rect_differences = tuple(
                tuple(
                    actual_value - expected_value
                    for actual_value, expected_value in zip(
                        actual_rect, expected_image_rect
                    )
                )
                for actual_rect in actual_image_rects
            )
            print(
                f"画像版 {output_index + 1}ページ目保存後画像bbox："
                f"実際={actual_image_rects}／期待={expected_image_rect}／"
                f"差={image_rect_differences}"
            )
            if len(image_rectangles) != 1 or not _rect_values_match(
                _rect_values(image_rectangles[0]), expected_image_rect
            ):
                raise ReplacementError(
                    "画像版PDFの画像がページ全面へ配置されていません。",
                    f"出力{output_index + 1}ページ目",
                )
            extracted_image = image_doc.extract_image(image_xref)
            extracted_bytes = bytes(extracted_image.get("image", b""))
            if not extracted_bytes:
                raise ReplacementError(
                    "画像版PDFからページ画像を抽出できません。",
                    f"出力{output_index + 1}ページ目",
                )
            extracted_pixmap = pymupdf.Pixmap(extracted_bytes)
            if (
                extracted_pixmap.width,
                extracted_pixmap.height,
                extracted_pixmap.n,
            ) != (raster.pixel_width, raster.pixel_height, 3) or _pixmap_hash(
                extracted_pixmap
            ) != raster.sample_hash:
                raise ReplacementError(
                    "画像版PDF内の画像が基準画像と一致しません。",
                    f"出力{output_index + 1}ページ目／"
                    f"基準ページ：{raster.page_index + 1}ページ目",
                )
            pixmap = page.get_pixmap(
                dpi=IMAGE_OUTPUT_DPI,
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            if (pixmap.width, pixmap.height) != (
                raster.pixel_width,
                raster.pixel_height,
            ):
                raise ReplacementError(
                    "画像版PDFのレンダリング寸法が基準文字PDFと一致しません。",
                    f"出力{output_index + 1}ページ目／"
                    f"基準ページ：{raster.page_index + 1}ページ目",
                )
    finally:
        image_doc.close()


def create_image_output_pdfs(
    pymupdf: Any,
    output_path: Path,
    program_dir: Path,
) -> tuple[Path, Path, Path]:
    """検証済み文字PDFからRGB・350 DPIの画像版3ファイルを原子的に作る。"""
    final_paths = find_available_image_output_paths(program_dir)
    temporary_paths = tuple(
        path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.pdf")
        for path in final_paths
    )
    rasterized_pages = rasterize_output_pdf(pymupdf, output_path)
    if len(rasterized_pages) != EXPECTED_PAGE_COUNT:
        raise ReplacementError("画像版PDF用の2ページ画像を作成できませんでした。")

    page_sets = (
        rasterized_pages,
        (rasterized_pages[0],),
        (rasterized_pages[1],),
    )
    committed_paths: list[Path] = []
    try:
        for temporary_path, pages in zip(temporary_paths, page_sets):
            _save_image_pdf(pymupdf, temporary_path, pages)
        for temporary_path, pages in zip(temporary_paths, page_sets):
            validate_image_pdf(pymupdf, temporary_path, pages)
        if any(path.exists() for path in final_paths):
            raise ReplacementError(
                "画像版PDFの出力先に別ファイルが作成されたため中止しました。",
                "既存ファイルは上書きしていません。",
            )
        for temporary_path, final_path in zip(temporary_paths, final_paths):
            temporary_path.replace(final_path)
            committed_paths.append(final_path)
    except Exception:
        for path in committed_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)

    for path in final_paths:
        print(f"画像版PDFファイルサイズ：{path.name}／{path.stat().st_size} bytes")
    return final_paths


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
            f"画像版PDF：{IMAGE_OUTPUT_PDF_NAME}",
            f"画像版1ページ目PDF：{IMAGE_OUTPUT_PAGE1_PDF_NAME}",
            f"画像版2ページ目PDF：{IMAGE_OUTPUT_PAGE2_PDF_NAME}",
            f"画像版解像度：{IMAGE_OUTPUT_DPI} DPI",
            "画像版色空間：RGB",
            "画像版アルファチャンネル：なし",
        ]
        for replacement in REPLACEMENTS:
            lines.extend(
                (
                    f"{replacement.label}検索文字列：{replacement.old_text}",
                    f"{replacement.label}変更後文字列：{replacement.new_text}",
                )
            )
            if replacement.old_text_aliases:
                lines.append(
                    f"{replacement.label}検索別表記："
                    + "／".join(replacement.old_text_aliases)
                )
                for candidate in replacement.old_text_candidates:
                    lines.append(f"{replacement.label}検索候補repr：{candidate!r}")
                    lines.append(
                        f"{replacement.label}検索候補コードポイント："
                        + " ".join(
                            f"{character}=U+{ord(character):04X}"
                            for character in candidate
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
        heading_move = prepare_general_schedule_heading_move(
            pymupdf, doc, prepared
        )
        moves = (heading_move,) + prepare_following_line_moves(
            pymupdf, doc, prepared
        )
        snapshot = snapshot_document(doc)

        apply_replacements(pymupdf, doc, prepared, moves)
        save_and_validate(pymupdf, doc, output_path, snapshot, prepared, moves)
        image_output_path, image_page1_path, image_page2_path = (
            create_image_output_pdfs(pymupdf, output_path, program_dir)
        )

        before_images: tuple[Path, ...] = ()
        after_images: tuple[Path, ...] = ()
        if args.render_comparison:
            with pymupdf.open(input_path) as input_doc:
                before_images = render_comparison_images(input_doc, program_dir, "変更前")
            with pymupdf.open(output_path) as output_doc:
                after_images = render_comparison_images(output_doc, program_dir, "変更後")

        print(f"出力PDF：{output_path.name}")
        print(f"画像版PDF：{image_output_path.name}")
        print(f"画像版1ページ目PDF：{image_page1_path.name}")
        print(f"画像版2ページ目PDF：{image_page2_path.name}")
        print(f"画像版解像度：{IMAGE_OUTPUT_DPI} DPI")
        print("画像版色空間：RGB")
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
