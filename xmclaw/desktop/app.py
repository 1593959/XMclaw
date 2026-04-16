"""Desktop application entry point."""
import os
import sys

# Force software rendering before any Qt imports
# These MUST be set before QApplication is created
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox --use-gl=swiftshader")
os.environ.setdefault("QTWEBENGINE_DISABLE_GPU", "1")

from PySide6.QtWidgets import QApplication
from xmclaw.desktop.main_window import MainWindow


def main():
    if "--disable-gpu" not in sys.argv:
        sys.argv.append("--disable-gpu")
    if "--no-sandbox" not in sys.argv:
        sys.argv.append("--no-sandbox")

    print("[Desktop] QT_QPA_PLATFORM =", os.environ.get("QT_QPA_PLATFORM"), file=sys.stderr)
    print("[Desktop] Creating QApplication...", file=sys.stderr)
    app = QApplication(sys.argv)
    print("[Desktop] QApplication created", file=sys.stderr)
    app.setQuitOnLastWindowClosed(False)

    try:
        window = MainWindow()
    except Exception as e:
        import traceback
        print(f"[Desktop FATAL] MainWindow.__init__ crashed: {e}", file=sys.stderr)
        traceback.print_exc()
        input("按回车退出...")
        return

    window.show()
    window.raise_()
    window.activateWindow()
    print("[Desktop] Entering event loop...", file=sys.stderr)
    result = app.exec()
    print(f"[Desktop] Event loop exited with code {result}", file=sys.stderr)
    sys.exit(result)


if __name__ == "__main__":
    main()
