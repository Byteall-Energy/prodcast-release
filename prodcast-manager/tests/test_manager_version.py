import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'release'))
from manager_version import normalize,stamp


@pytest.mark.parametrize('value',['v0.6.8','0.6.8'])
def test_stamp_release_version(tmp_path,value):
    path=tmp_path/'__init__.py';path.write_text("__version__ = '0.6.4'\n")
    stamp(path,value)
    assert path.read_text()=="__version__ = '0.6.8'\n"


@pytest.mark.parametrize('value',['v0.6','v0.6.8-rc.1','bad','v0.6.8\n'])
def test_invalid_version(value):
    with pytest.raises(ValueError):normalize(value)


def test_missing_declaration_preserved(tmp_path):
    path=tmp_path/'__init__.py';path.write_text('# absent\n')
    with pytest.raises(ValueError):stamp(path,'v0.6.8')
    assert path.read_text()=='# absent\n'


def test_workflow_stamps_before_build_and_verifies_before_rename():
    workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/release.yml').read_text()
    assert workflow.index('Set internal Manager version from release input')<workflow.index('name: Test Manager')<workflow.index('Build and smoke-test one-file Manager')
    assert './build.ps1 -Python python -TestsOnly' in workflow
    assert './build.ps1 -Python python -Output dist -SkipTests' in workflow
    assert workflow.index('--verify-exe prodcast-manager/dist/ProdCast-Manager.exe')<workflow.index('Move-Item -LiteralPath prodcast-manager/dist/ProdCast-Manager.exe')
    assert 'path: prodcast-manager/dist/ProdCast-Manager-${{ env.RELEASE_VERSION }}.exe' in workflow


def test_latest_workflow_resolves_and_verifies_version_before_upload():
    workflow=(Path(__file__).resolve().parents[2]/'.github/workflows/build-latest-manager.yml').read_text()
    assert 'REQUESTED_MANAGER_VERSION: ${{ inputs.version }}' in workflow
    assert workflow.index('Resolve internal Manager version')<workflow.index('name: Test Manager')<workflow.index('Build and smoke-test Manager')
    assert './build.ps1 -Python python -TestsOnly' in workflow
    assert './build.ps1 -Python python -Output dist -SkipTests' in workflow
    assert workflow.index('--verify-exe dist/ProdCast-Manager.exe')<workflow.index('Move-Item -LiteralPath dist/ProdCast-Manager.exe')
    assert 'path: prodcast-manager/dist/ProdCast-Manager-v${{ env.MANAGER_VERSION }}.exe' in workflow
    assert 'path: prodcast-manager/dist/*.exe' not in workflow
    assert workflow.index('Verify and stage versioned executable')<workflow.index('Upload latest Manager executable files')
