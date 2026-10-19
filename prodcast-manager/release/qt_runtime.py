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


def desktop_resources(entries):
    """Exclude unused browser debugging packs and non-EN/RU translations.

    Keep normal rendering resources, ICU, the helper process and GPU/software
    fallbacks. HTML content and application translations are unchanged.
    """
    result=[]
    for entry in entries:
        path=entry[0].replace('\\','/').lower()
        name=path.rsplit('/',1)[-1]
        if path.startswith('pyside6/resources/'):
            if name.endswith('.debug.pak') or name.startswith('qtwebengine_devtools_resources'):
                continue
        if path.startswith('pyside6/translations/'):
            if name.endswith('.pak') and name not in ('en-us.pak','en-gb.pak','ru.pak'):
                continue
            if name.endswith('.qm') and not name.endswith(('_en.qm','_en_us.qm','_en_gb.qm','_ru.qm')):
                continue
        result.append(entry)
    return result
