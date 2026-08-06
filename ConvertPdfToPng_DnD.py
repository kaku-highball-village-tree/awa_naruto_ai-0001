# -*- coding: utf-8 -*-
###############################################################
#
# ConvertPdfToPng_DnD.py
#
# Windows上でPDFファイルをドラッグ＆ドロップで受け付け、同じ場所にある
# ConvertPdfToPng_Cmd.pyを現在のPythonで呼び出すフロントエンドです。
#
###############################################################

import os
import subprocess
import sys

import win32api
import win32con
import win32gui


pszApplicationTitle: str = "ConvertPdfToPng_DnD"


###############################################################
# 指定された文字列を情報メッセージボックスに表示します。
###############################################################
def show_message_box(pszMessage: str, pszTitle: str) -> None:
    win32gui.MessageBox(
        0,
        pszMessage,
        pszTitle,
        win32con.MB_OK | win32con.MB_ICONINFORMATION,
    )


###############################################################
# 指定された文字列をエラーメッセージボックスに表示します。
###############################################################
def show_error_message_box(pszMessage: str, pszTitle: str) -> None:
    win32gui.MessageBox(
        0,
        pszMessage,
        pszTitle,
        win32con.MB_OK | win32con.MB_ICONERROR,
    )


###############################################################
# 変換用スクリプトへPDFのフルパスを渡し、実行結果を表示します。
###############################################################
def run_convert_pdf_to_png(pszInputFileFullPath: str) -> None:
    pszCurrentDirectoryFullPath: str = os.path.dirname(os.path.abspath(__file__))
    pszScriptFileName: str = "ConvertPdfToPng_Cmd.py"
    pszScriptFileFullPath: str = os.path.join(
        pszCurrentDirectoryFullPath, pszScriptFileName
    )

    if not os.path.isfile(pszScriptFileFullPath):
        show_error_message_box(
            "Error: ConvertPdfToPng_Cmd.py not found.\n\nPath:\n"
            + pszScriptFileFullPath,
            pszApplicationTitle,
        )
        return

    # PYTHONUTF8により、日本語を含む標準出力・標準エラーもUTF-8で受け取ります。
    objEnvironment: dict[str, str] = os.environ.copy()
    objEnvironment["PYTHONUTF8"] = "1"
    try:
        objCompletedProcess: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, pszScriptFileFullPath, pszInputFileFullPath],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=objEnvironment,
        )
    except Exception as objException:
        show_error_message_box(
            "Error: unexpected exception while running ConvertPdfToPng_Cmd.py."
            "\n\nPath:\n"
            + pszInputFileFullPath
            + "\n\nDetail:\n"
            + str(objException),
            pszApplicationTitle,
        )
        return

    pszStdOut: str = objCompletedProcess.stdout.strip()
    pszStdErr: str = objCompletedProcess.stderr.strip()
    if objCompletedProcess.returncode != 0:
        pszErrorMessage: str = (
            "Error: ConvertPdfToPng_Cmd.py exited with non-zero return code."
            "\n\nReturn code:\n"
            + str(objCompletedProcess.returncode)
            + "\n\nstderr:\n"
            + (pszStdErr or "(no stderr output)")
        )
        if pszStdOut:
            pszErrorMessage += "\n\nstdout:\n" + pszStdOut
        pszErrorMessage += "\n\nInput PDF:\n" + pszInputFileFullPath
        show_error_message_box(pszErrorMessage, pszApplicationTitle)
        return

    show_message_box(
        pszStdOut or "ConvertPdfToPng_Cmd.py finished successfully.",
        pszApplicationTitle,
    )


###############################################################
# ウィンドウ内に操作方法と出力先の説明を描画します。
###############################################################
def draw_instruction_text(iWindowHandle: int) -> None:
    iDeviceContextHandle, objPaintStruct = win32gui.BeginPaint(iWindowHandle)
    objClientRect = win32gui.GetClientRect(iWindowHandle)
    iMargin: int = 10
    objClientRect = (
        objClientRect[0] + iMargin,
        objClientRect[1] + iMargin,
        objClientRect[2] - iMargin,
        objClientRect[3] - iMargin,
    )
    pszInstructionText: str = (
        "PDFファイルをこのウィンドウにドラッグ＆ドロップしてください。\n\n"
        "PDFの各ページをPNGファイルへ変換します。\n"
        "変換したPNGファイルは、PDFファイルと同じフォルダ内の\n"
        "「PDFファイル名_png」フォルダへ保存します。"
    )
    win32gui.DrawText(
        iDeviceContextHandle,
        pszInstructionText,
        -1,
        objClientRect,
        win32con.DT_LEFT | win32con.DT_TOP | win32con.DT_WORDBREAK,
    )
    win32gui.EndPaint(iWindowHandle, objPaintStruct)


###############################################################
# ドロップ、再描画、終了などのWindowsメッセージを処理します。
###############################################################
def window_proc(
    iWindowHandle: int, iMessage: int, iWparam: int, iLparam: int
) -> int:
    if iMessage == win32con.WM_CREATE:
        win32gui.DragAcceptFiles(iWindowHandle, True)
        return 0

    if iMessage == win32con.WM_DROPFILES:
        iDropHandle: int = iWparam
        try:
            iFileCount: int = win32api.DragQueryFile(iDropHandle, -1)
            if iFileCount != 1:
                show_error_message_box(
                    "Error: please drag and drop exactly one PDF file at a time.",
                    pszApplicationTitle,
                )
                return 0
            pszDroppedFilePath: str = win32api.DragQueryFile(iDropHandle, 0)
        finally:
            # どの分岐でもHDROPハンドルを確実に解放します。
            win32api.DragFinish(iDropHandle)

        if os.path.splitext(pszDroppedFilePath)[1].lower() != ".pdf":
            show_error_message_box(
                "Error: the dropped file is not a PDF file.\n\nPath:\n"
                + pszDroppedFilePath,
                pszApplicationTitle,
            )
            return 0
        run_convert_pdf_to_png(os.path.abspath(pszDroppedFilePath))
        return 0

    if iMessage == win32con.WM_PAINT:
        draw_instruction_text(iWindowHandle)
        return 0
    if iMessage == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(
        iWindowHandle, iMessage, iWparam, iLparam
    )


###############################################################
# メインウィンドウで使用するWindowsクラスを登録します。
###############################################################
def register_window_class(pszWindowClassName: str) -> int:
    iInstanceHandle: int = win32api.GetModuleHandle(None)
    objWndClass = win32gui.WNDCLASS()
    objWndClass.hInstance = iInstanceHandle
    objWndClass.lpszClassName = pszWindowClassName
    objWndClass.lpfnWndProc = window_proc
    objWndClass.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
    objWndClass.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    objWndClass.hbrBackground = win32con.COLOR_WINDOW + 1
    return win32gui.RegisterClass(objWndClass)


###############################################################
# 常に最前面となるドラッグ＆ドロップ用ウィンドウを作成します。
###############################################################
def create_main_window(
    pszWindowClassName: str, pszWindowTitle: str
) -> int:
    iInstanceHandle: int = win32api.GetModuleHandle(None)
    iWindowStyle: int = (
        win32con.WS_OVERLAPPED
        | win32con.WS_CAPTION
        | win32con.WS_SYSMENU
        | win32con.WS_MINIMIZEBOX
    )
    iWindowHeight: int = 260
    iWindowWidth: int = int(iWindowHeight * 1.618)
    iWindowHandle: int = win32gui.CreateWindowEx(
        win32con.WS_EX_ACCEPTFILES,
        pszWindowClassName,
        pszWindowTitle,
        iWindowStyle,
        win32con.CW_USEDEFAULT,
        win32con.CW_USEDEFAULT,
        iWindowWidth,
        iWindowHeight,
        0,
        0,
        iInstanceHandle,
        None,
    )
    win32gui.ShowWindow(iWindowHandle, win32con.SW_SHOWNORMAL)
    win32gui.UpdateWindow(iWindowHandle)
    win32gui.SetWindowPos(
        iWindowHandle,
        win32con.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
    )
    win32gui.DragAcceptFiles(iWindowHandle, True)
    return iWindowHandle


###############################################################
# ウィンドウを準備し、ドラッグ＆ドロップを待ち受けます。
###############################################################
def main() -> None:
    pszWindowClassName: str = "ConvertPdfToPngDndWindowClass"
    pszWindowTitle: str = "ConvertPdfToPng (Drag & Drop)"
    try:
        iClassAtom: int = register_window_class(pszWindowClassName)
        iWindowHandle: int = create_main_window(
            pszWindowClassName, pszWindowTitle
        )
        # 参照を保つとともに、登録・作成に成功したことを明確にします。
        del iClassAtom, iWindowHandle
        win32gui.PumpMessages()
    except Exception as objException:
        show_error_message_box(
            "Error: failed to run the drag-and-drop window.\n\nDetail:\n"
            + str(objException),
            pszApplicationTitle,
        )


if __name__ == "__main__":
    main()
