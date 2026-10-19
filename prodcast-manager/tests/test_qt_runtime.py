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
