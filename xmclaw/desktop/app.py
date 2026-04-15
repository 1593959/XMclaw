"""Desktop application entry point."""
import sys
from PySide6.QtWidgets import QApplication
from xmclaw.desktop.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
