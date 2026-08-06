# -*- coding: utf-8 -*-
###############################################################
#
# ConvertPdfToPng_Cmd.py
#
# コマンドラインで指定されたPDFファイルを開き、各ページを
# 300 DPIの白背景PNGファイルへ変換するスクリプトです。
#
###############################################################

import os
import re
import sys

import fitz


# PDFの標準解像度（72 DPI）を300 DPIへ拡大するために使用します。
iOutputDpi: int = 300


###############################################################
# 標準エラー出力へ、処理内容と対象を含むエラーを表示します。
###############################################################
def write_error_message(
    pszOperation: str,
    pszInputPdfFileFullPath: str,
    objException: object | None = None,
    iPageNumber: int | None = None,
) -> None:
    pszMessage: str = "Error: " + pszOperation
    if pszInputPdfFileFullPath:
        pszMessage += "\nInput PDF:\n" + pszInputPdfFileFullPath
    if iPageNumber is not None:
        pszMessage += "\nPage number:\n" + str(iPageNumber)
    if objException is not None:
        pszMessage += "\nDetail:\n" + str(objException)
    print(pszMessage, file=sys.stderr)


###############################################################
# 入力パスが、存在するPDFファイルを示すことを確認します。
###############################################################
def validate_input_pdf_file(pszInputPdfFileFullPath: str) -> bool:
    if not os.path.exists(pszInputPdfFileFullPath):
        write_error_message("input PDF file does not exist.", pszInputPdfFileFullPath)
        return False
    if not os.path.isfile(pszInputPdfFileFullPath):
        write_error_message("input PDF path is not a file.", pszInputPdfFileFullPath)
        return False
    if os.path.splitext(pszInputPdfFileFullPath)[1].lower() != ".pdf":
        write_error_message("input file is not a PDF file.", pszInputPdfFileFullPath)
        return False
    return True


###############################################################
# PDFと同じ場所に作成する「PDFファイル名_png」のパスを返します。
###############################################################
def make_output_directory_path(pszInputPdfFileFullPath: str) -> str:
    pszInputDirectoryFullPath: str = os.path.dirname(pszInputPdfFileFullPath)
    pszPdfFileBaseName: str = os.path.splitext(
        os.path.basename(pszInputPdfFileFullPath)
    )[0]
    return os.path.join(pszInputDirectoryFullPath, pszPdfFileBaseName + "_png")


###############################################################
# 本プログラムの命名規則に一致する古いPNGだけを削除します。
###############################################################
def remove_old_generated_png_files(
    pszOutputDirectoryFullPath: str,
    pszPdfFileBaseName: str,
) -> None:
    objGeneratedFilePattern: re.Pattern[str] = re.compile(
        r"^" + re.escape(pszPdfFileBaseName) + r"_page_\d{3,}\.png$",
        re.IGNORECASE,
    )
    for pszFileName in os.listdir(pszOutputDirectoryFullPath):
        if objGeneratedFilePattern.fullmatch(pszFileName):
            pszFileFullPath: str = os.path.join(
                pszOutputDirectoryFullPath, pszFileName
            )
            if os.path.isfile(pszFileFullPath):
                os.remove(pszFileFullPath)


###############################################################
# PDFを開き、先頭から順に全ページをPNGへ変換します。
###############################################################
def convert_pdf_pages_to_png_files(
    pszInputPdfFileFullPath: str,
    pszOutputDirectoryFullPath: str,
) -> int:
    pszPdfFileBaseName: str = os.path.splitext(
        os.path.basename(pszInputPdfFileFullPath)
    )[0]
    fZoom: float = iOutputDpi / 72.0
    objMatrix: fitz.Matrix = fitz.Matrix(fZoom, fZoom)

    try:
        with fitz.open(pszInputPdfFileFullPath) as objDocument:
            if objDocument.needs_pass:
                raise PermissionError(
                    "the PDF is encrypted and requires a password."
                )

            iPageCount: int = objDocument.page_count
            if iPageCount <= 0:
                raise ValueError("the PDF contains no pages.")

            print("Input PDF: " + pszInputPdfFileFullPath)
            print("Output directory: " + pszOutputDirectoryFullPath)
            print("Page count: " + str(iPageCount))
            print("Output DPI: " + str(iOutputDpi))

            # 3桁を最低幅とし、1000ページ以上では桁数を自動拡張します。
            iPageNumberWidth: int = max(3, len(str(iPageCount)))
            for iPageIndex in range(iPageCount):
                iPageNumber: int = iPageIndex + 1
                pszOutputPngFileName: str = (
                    pszPdfFileBaseName
                    + "_page_"
                    + str(iPageNumber).zfill(iPageNumberWidth)
                    + ".png"
                )
                pszOutputPngFileFullPath: str = os.path.join(
                    pszOutputDirectoryFullPath, pszOutputPngFileName
                )
                try:
                    objPage: fitz.Page = objDocument.load_page(iPageIndex)
                    # alpha=Falseは透明領域を白で初期化し、一般的なRGB PNGにします。
                    objPixmap: fitz.Pixmap = objPage.get_pixmap(
                        matrix=objMatrix,
                        colorspace=fitz.csRGB,
                        alpha=False,
                    )
                    objPixmap.save(pszOutputPngFileFullPath)
                except Exception as objException:
                    write_error_message(
                        "failed to convert or save a PDF page.",
                        pszInputPdfFileFullPath,
                        objException,
                        iPageNumber,
                    )
                    return 1

                print(
                    "Page "
                    + str(iPageNumber)
                    + "/"
                    + str(iPageCount)
                    + ": "
                    + pszOutputPngFileFullPath
                )
    except Exception as objException:
        write_error_message(
            "failed to open or read the PDF file.",
            pszInputPdfFileFullPath,
            objException,
        )
        return 1

    print("\nCompleted successfully.")
    print("\nInput PDF:\n" + pszInputPdfFileFullPath)
    print("\nOutput directory:\n" + pszOutputDirectoryFullPath)
    print("\nConverted pages:\n" + str(iPageCount))
    return 0


###############################################################
# 引数検証、出力先準備、および変換処理を順番に実行します。
###############################################################
def main() -> int:
    # Windowsの日本語パスを親プロセスへ確実に渡すためUTF-8を使用します。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("Error: input PDF file path is not specified.", file=sys.stderr)
        print(
            "Usage: python ConvertPdfToPng_Cmd.py <input_pdf_file_path>",
            file=sys.stderr,
        )
        return 1

    pszInputPdfFileFullPath: str = os.path.abspath(sys.argv[1])
    if not validate_input_pdf_file(pszInputPdfFileFullPath):
        return 1

    pszOutputDirectoryFullPath: str = make_output_directory_path(
        pszInputPdfFileFullPath
    )
    pszPdfFileBaseName: str = os.path.splitext(
        os.path.basename(pszInputPdfFileFullPath)
    )[0]
    try:
        os.makedirs(pszOutputDirectoryFullPath, exist_ok=True)
        remove_old_generated_png_files(
            pszOutputDirectoryFullPath, pszPdfFileBaseName
        )
    except Exception as objException:
        write_error_message(
            "failed to prepare the output directory.",
            pszInputPdfFileFullPath,
            objException,
        )
        return 1

    return convert_pdf_pages_to_png_files(
        pszInputPdfFileFullPath, pszOutputDirectoryFullPath
    )


if __name__ == "__main__":
    sys.exit(main())
