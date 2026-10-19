# Offline Tkinter onefile build. No Qt or browser runtime is included.
from pathlib import Path
project=Path(SPECPATH).parent
a=Analysis([str(project/'manager.py')],pathex=[str(project)],
           binaries=[],datas=[(str(project/'prodcast_manager/resources'),'prodcast_manager/resources')],
           hiddenimports=[],hookspath=[],runtime_hooks=[],
           excludes=['PySide6','shiboken6','PyQt6','PyQt5','prodcast_manager.qt_ui'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='ProdCast-Manager',
        debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,
        icon=str(project/'prodcast_manager/resources/prodcast-manager.ico'))
