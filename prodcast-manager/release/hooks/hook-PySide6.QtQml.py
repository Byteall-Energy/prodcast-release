"""Keep WebEngine's QtQml dependencies, not the unused QML application plugins.

Manager renders bundled HTML in QWebEngineView. It does not load QQmlEngine,
QML files, Qt Quick Controls, 3D scenes or the QML designer tooling.
"""
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
