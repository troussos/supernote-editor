"""Entry point: python -m supernote_editor [file.note]"""
import os
import sys

# allow running from the project directory without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from PySide6.QtWidgets import QApplication

    from supernote_editor.mainwindow import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Supernote Editor")
    app.setOrganizationName("SupernoteEditor")
    window = MainWindow()
    window.show()
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".note") and os.path.exists(arg):
            window.open_path(arg)
            break
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
