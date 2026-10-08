import sys

from PyQt6.QtWidgets import QApplication

from vibe_coder.gui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VibeCoder")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
