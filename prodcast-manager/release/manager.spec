# PyInstaller onefile desktop build, including a consistent C++ runtime for Qt.
from pathlib import Path
import sys
import PySide6
from PyInstaller.utils.hooks import copy_metadata

project=Path(SPECPATH).parent
sys.path.insert(0,str(project/'release'))
from qt_runtime import runtime_files,consistent_binaries,desktop_resources

qt=Path(PySide6.__file__).parent
runtimes=runtime_files(qt)
if sys.platform=='win32' and 'vcruntime140.dll' not in runtimes:
    raise RuntimeError('Qt wheel must provide its redistributable C++ runtime')
metadata=[]
for distribution in ('PySide6','PySide6_Essentials','PySide6_Addons','PySide6_WebEngine','PySide6_Pdf','shiboken6'):
    metadata.extend(copy_metadata(distribution))
a=Analysis([str(project/'manager.py')],pathex=[str(project)],
           binaries=[(str(path),'.') for path in runtimes.values()],
           datas=[(str(project/'prodcast_manager/resources'),'prodcast_manager/resources'),*metadata],
           hiddenimports=[],hookspath=[str(project/'release/hooks')],runtime_hooks=[],excludes=[],noarchive=False)
a.binaries=consistent_binaries(a.binaries,runtimes)
a.datas=desktop_resources(a.datas)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='ProdCast-Manager',
        debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,
        icon=str(project/'prodcast_manager/resources/prodcast-manager.ico'))
