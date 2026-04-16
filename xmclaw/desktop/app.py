"""Desktop application entry point."""
import sys
from PySide6.QtWidgets import QApplication
from xmclaw.desktop.main_window import MainWindow


def main():
    # Disable GPU acceleration to prevent crashes on some systems
    if "--disable-gpu" not in sys.argv:
        sys.argv.append("--disable-gpu")
    if "--no-sandbox" not in sys.argv:
        sys.argv.append("--no-sandbox")

    app = QApplication(sys.argv)
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
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
