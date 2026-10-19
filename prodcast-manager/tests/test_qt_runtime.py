import importlib.util
from pathlib import Path


def test_old_python_runtime_is_replaced_in_root_and_nested_locations(tmp_path):
    spec=importlib.util.spec_from_file_location('qt_runtime',Path(__file__).resolve().parents[1]/'release/qt_runtime.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for name in ('vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll','Qt6Core.dll'):(tmp_path/name).touch()
    runtimes=module.runtime_files(tmp_path)
    assert 'qt6core.dll' not in runtimes
    binaries=[('VCRUNTIME140.dll','old/python/runtime.dll','BINARY'),
              ('shiboken6/MSVCP140.dll','old/other/runtime.dll','BINARY'),
              ('PySide6/Qt6Core.dll','correct/Qt6Core.dll','BINARY')]
    replaced=module.consistent_binaries(binaries,runtimes)
    assert replaced[0][1]==str(tmp_path/'vcruntime140.dll')
    assert replaced[1][1]==str(tmp_path/'msvcp140.dll')
    assert replaced[2]==binaries[2]
    assert module.consistent_binaries([('icuuc.dll','pdf-tools/icuuc.dll','BINARY')],runtimes)==[]


def test_desktop_resource_filter_preserves_rendering_and_supported_languages():
    spec=importlib.util.spec_from_file_location('qt_runtime',Path(__file__).resolve().parents[1]/'release/qt_runtime.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    keep=['PySide6/resources/qtwebengine_resources.pak',
          'PySide6/resources/qtwebengine_resources_100p.pak',
          'PySide6/resources/qtwebengine_resources_200p.pak',
          'PySide6/resources/icudtl.dat',
          'PySide6/translations/qtwebengine_locales/en-US.pak',
          'PySide6/translations/qtwebengine_locales/en-GB.pak',
          'PySide6/translations/qtwebengine_locales/ru.pak',
          'PySide6/translations/qtbase_ru.qm',
          'PySide6/opengl32sw.dll', 'prodcast_manager/resources/webui/app.js']
    drop=['PySide6/resources/qtwebengine_resources.debug.pak',
          'PySide6/resources/qtwebengine_devtools_resources.pak',
          'PySide6/resources/qtwebengine_devtools_resources.debug.pak',
          'PySide6/translations/qtwebengine_locales/fr.pak',
          'PySide6/translations/qtbase_zh_CN.qm']
    rows=[(name.replace('/','\\'),'source','DATA') for name in keep+drop]
    assert module.desktop_resources(rows)==rows[:len(keep)]
