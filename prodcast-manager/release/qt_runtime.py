"""Use Qt's bundled C++ runtime consistently, including at onefile startup."""
from pathlib import Path


def runtime_files(qt_directory):
    return {path.name.lower():path for path in Path(qt_directory).iterdir()
            if path.is_file() and path.suffix.lower()=='.dll' and path.name.lower().startswith(('vcruntime140','msvcp140'))}


def consistent_binaries(binaries, runtimes):
    result=[]
    for destination,source,kind in binaries:
        name=destination.replace('\\','/').rsplit('/',1)[-1].lower()
        # Qt uses Windows' unsuffixed ICU API. A PATH entry (e.g. PDF tools)
        # can make dependency discovery collect an incompatible suffixed ICU.
        if name in ('icuuc.dll','icuin.dll','icudt.dll'):
            continue
        result.append((destination,str(runtimes[name]) if name in runtimes else source,kind))
    return result
