"""Keep functional validation tied to the source pair that is packaged."""
from pathlib import Path


def test_functional_gate_uses_frozen_sources_and_blocks_assembly():
    workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/release.yml').read_text()
    functional = workflow.split('\n  functional:\n', 1)[1].split('\n  app:\n', 1)[0]
    assert '    needs: resolve\n' in functional
    assemble = workflow.split('\n  assemble:\n', 1)[1].split('\n  publish:\n', 1)[0]
    assert 'needs: [resolve, functional, app, agent, worker, ai, manager]' in assemble
    for component in ('app', 'worker'):
        checkout = functional.split('repository: Byteall-Energy/prodcast-' + component, 1)[1].split('persist-credentials:', 1)[0]
        assert 'ref: ${{ needs.resolve.outputs.' + component + '_sha }}' in checkout
        assert 'path: ' + component in checkout
    environment = functional.split('\n    env:\n', 1)[1].split('\n    steps:\n', 1)[0]
    assert '      PRODCAST_WORKER_SOURCE: ${{ github.workspace }}/worker' in environment.splitlines()
    assert "python -m unittest discover -s app/release -p 'test_worker_roundtrip.py' -v" in functional
    assert 'test apiapp.test_platform_functional apiapp.test_output_save_mode --noinput' in functional
    assert 'exit 1' in functional
    assert 'POSTGRES_DB: prodcast_functional' in functional
    report = functional.split('uses: actions/upload-artifact@v4', 1)[1]
    assert 'if: always()' in report
    assert 'name: functional-test-report' in report
