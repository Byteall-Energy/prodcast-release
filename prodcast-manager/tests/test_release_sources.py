from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from release_sources import find_base, source_ci, validate_version


class FakeGitHub:
    def __init__(self, releases):
        self.releases = releases

    def request(self, method, path, body=None, missing_ok=False):
        assert method == "GET" and path.endswith("/releases?per_page=100")
        return self.releases


def release(tag, *, draft=False, prerelease=False, complete=True):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": f"ProdCast-{tag}-complete.zip"}] if complete else [],
    }


def test_version_is_stable_only():
    assert validate_version("v12.3.4") == (12, 3, 4)
    for invalid in ("0.6.1", "v0.6", "v0.6.1-rc.1", "v0.6.1+build"):
        with pytest.raises(ValueError):
            validate_version(invalid)


def test_base_is_latest_earlier_published_stable_complete():
    api = FakeGitHub([
        release("v0.6.0"), release("v0.5.0"), release("v0.6.1", draft=True),
        release("v0.5.9", prerelease=True), release("v0.5.8", complete=False),
    ])
    assert find_base(api, "v0.6.1", None) == ("v0.6.0", "ProdCast-v0.6.0-complete.zip")
    assert find_base(api, "v0.6.1", "v0.5.0") == ("v0.5.0", "ProdCast-v0.5.0-complete.zip")


def test_invalid_base_override_fails():
    with pytest.raises(RuntimeError):
        find_base(FakeGitHub([release("v0.6.0", draft=True)]), "v0.6.1", "v0.6.0")


def test_release_repository_does_not_gate_on_running_release_workflow():
    class UnexpectedGitHubCall:
        def request(self, *args, **kwargs):
            raise AssertionError("release repository CI must not query its own running checks")

    result = source_ci(
        UnexpectedGitHubCall(),
        "release",
        "Byteall-Energy/prodcast-release",
        "a" * 40,
        True,
    )
    assert result["state"] == "not-applicable"
