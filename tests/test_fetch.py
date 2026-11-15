"""tools/odoo/fetch.sh against an upstream repository in a temp dir (no network)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from odoo_typing.compat import SUPPORTED_VERSIONS

TOOLS = Path(__file__).parents[1] / "tools"
FETCH = TOOLS / "odoo" / "fetch.sh"
PINS = TOOLS / "odoo" / "commits.txt"
LAYOUT_LINE = re.search(r"^LAYOUT=(\d+)$", FETCH.read_text(), re.MULTILINE)
assert LAYOUT_LINE is not None
LAYOUT = LAYOUT_LINE.group(1)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or shutil.which("git") is None
    or shutil.which("bash") is None,
    reason="needs Linux (GNU cp -al), git and bash",
)

# git without the user's configuration (signing, hooks, URL rewrites) and with no
# transport but local files: a test can never reach the network.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "file",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
# What git exports to its hooks (a test run from one) to point at the developer's own
# repository and index: never let the temp repositories' commands use them.
GIT_REPOSITORY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


def _git_env() -> dict[str, str]:
    """This process's environment without the repository variables, plus ``GIT_ENV``."""
    env = {name: value for name, value in os.environ.items() if name not in GIT_REPOSITORY_ENV}
    return {**env, **GIT_ENV}


def _git(cwd: Path, *args: str) -> str:
    command = ["git", "-C", str(cwd), *args]
    result = subprocess.run(command, check=True, capture_output=True, text=True, env=_git_env())
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    """Write ``files`` into ``repo``, commit everything and return the commit."""
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, list[str]]:
    """A repository with an Odoo-like layout on branch 19.0, and its two commits."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "19.0")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    commits = []
    for number in (1, 2):
        files = {
            "odoo/release.py": "version_info = (19, 0, 0, 'final', 0, '')\n",
            "odoo/addons/base/__init__.py": "",
            "odoo/addons/base/static/a.js": "",
            "addons/crm/__init__.py": f"crm = {number}\n",
            "addons/crm/i18n/fr.po": "",
            "doc/index.rst": "",
        }
        commits.append(_commit(repo, files, f"commit {number}"))
    return repo, commits


def _env(tmp_path: Path, repo: Path, commit: str | None) -> dict[str, str]:
    """fetch.sh's environment: ``repo`` upstream, a cache in ``tmp_path``, ``commit`` pinned."""
    env = {
        **_git_env(),
        "ODOO_TYPING_ODOO_REPO": repo.as_uri(),
        "ODOO_TYPING_ODOO_CACHE": str(tmp_path / "cache"),
        "ODOO_TYPING_FETCH_RETRY_DELAY": "0",
    }
    env.pop("ODOO_TYPING_ODOO_COMMIT", None)
    if commit is not None:
        env["ODOO_TYPING_ODOO_COMMIT"] = commit
    return env


def _fetch(
    tmp_path: Path, repo: Path, *args: str, commit: str | None = None, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run fetch.sh 19.0 (``path``: another ``PATH``)."""
    env = _env(tmp_path, repo, commit)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(FETCH), "19.0", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_fetch_pinned_commit_is_checked_out_without_static_files(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    odoo = Path(result.stdout.strip())
    assert odoo == tmp_path / "cache" / "19.0" / "root" / "odoo"
    assert (odoo / "addons" / "crm" / "__init__.py").read_text() == "crm = 1\n"
    assert (odoo / "release.py").is_file()
    assert (odoo / "addons" / "base" / "__init__.py").is_file()
    assert not (odoo / "addons" / "base" / "static").exists()
    assert not (odoo / "addons" / "crm" / "i18n").exists()
    assert not (tmp_path / "cache" / "19.0" / "src" / "doc").exists()


def test_fetch_at_the_pin_needs_no_network_and_keeps_the_root(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream
    odoo = Path(_fetch(tmp_path, repo, commit=first).stdout.strip())
    marker = odoo.parent / "marker"
    marker.write_text("")
    repo.rename(tmp_path / "gone")

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    assert (result.stdout.strip(), marker.exists()) == (str(odoo), True)


def test_fetch_moved_pin_rebuilds_the_root(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, second) = upstream
    odoo = Path(_fetch(tmp_path, repo, commit=first).stdout.strip())
    (odoo.parent / "marker").write_text("")

    result = _fetch(tmp_path, repo, commit=second)

    assert result.returncode == 0, result.stderr
    assert (odoo / "addons" / "crm" / "__init__.py").read_text() == "crm = 2\n"
    assert not (odoo.parent / "marker").exists()
    assert (tmp_path / "cache" / "19.0" / "root.commit").read_text() == f"{second} {LAYOUT}\n"


def test_fetch_moved_pin_is_fetched_shallow(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, second) = upstream
    _fetch(tmp_path, repo, commit=first)

    result = _fetch(tmp_path, repo, commit=second)

    # not lazily as a missing object of the partial clone, which brings its history
    src = tmp_path / "cache" / "19.0" / "src"
    assert (result.returncode, _git(src, "rev-list", "--count", "HEAD")) == (0, "1")


@pytest.mark.parametrize("old_stamp", ["{commit}\n", "{commit} 1\n"], ids=["sha", "layout-1"])
def test_fetch_root_of_another_layout_is_rebuilt(
    tmp_path: Path, upstream: tuple[Path, list[str]], old_stamp: str
) -> None:
    repo, (first, _) = upstream
    odoo = Path(_fetch(tmp_path, repo, commit=first).stdout.strip())
    marker = odoo.parent / "marker"
    marker.write_text("")
    root_commit = tmp_path / "cache" / "19.0" / "root.commit"
    root_commit.write_text(old_stamp.format(commit=first))  # what an older fetch.sh wrote

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    assert (marker.exists(), root_commit.read_text()) == (False, f"{first} {LAYOUT}\n")


def test_fetch_checkout_with_other_sparse_patterns_gets_the_current_ones(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream
    odoo = Path(_fetch(tmp_path, repo, commit=first).stdout.strip())
    src = tmp_path / "cache" / "19.0" / "src"
    _git(src, "sparse-checkout", "set", "--no-cone", "/*")  # an older fetch.sh: everything
    marker = odoo.parent / "marker"
    marker.write_text("")

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    assert not (src / "doc").exists()
    assert not (src / "addons" / "crm" / "i18n").exists()
    assert not marker.exists()  # root/ is rebuilt from the new checkout


def test_fetch_checkout_failing_once_is_retried(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream
    real_git = shutil.which("git")
    flaky = tmp_path / "bin" / "git"  # the blob download of the first checkout breaks
    flaky.parent.mkdir()
    flaky.write_text(
        "#!/bin/sh\n"
        'case "$*" in *"checkout --quiet --force --detach FETCH_HEAD"*)\n'
        f'  [ -e "{tmp_path}/failed" ] || {{ touch "{tmp_path}/failed"; exit 128; }} ;;\n'
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    flaky.chmod(0o755)

    result = _fetch(tmp_path, repo, commit=first, path=f"{flaky.parent}:{os.environ['PATH']}")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "failed").exists()
    assert (Path(result.stdout.strip()) / "addons/crm/__init__.py").read_text() == "crm = 1\n"


def test_fetch_back_at_the_pin_after_latest_needs_no_network(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream
    _fetch(tmp_path, repo, commit=first)
    _fetch(tmp_path, repo, "--latest")
    repo.rename(tmp_path / "gone")

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    assert (Path(result.stdout.strip()) / "addons/crm/__init__.py").read_text() == "crm = 1\n"


@pytest.mark.parametrize(
    "release",
    ["version_info = (18, 0, 0, 'final', 0, '')\n", "version_info = ('saas~18', 3)\n"],
    ids=["final", "saas"],
)
def test_fetch_commit_of_another_major_version_exits_1_and_builds_no_root(
    tmp_path: Path, upstream: tuple[Path, list[str]], release: str
) -> None:
    repo, _ = upstream
    other = _commit(repo, {"odoo/release.py": release}, "another version")  # a wrong pin

    result = _fetch(tmp_path, repo, commit=other)

    assert (result.returncode, result.stdout) == (1, "")
    assert f"{other} is Odoo 18 (odoo/release.py), not 19.0" in result.stderr
    assert not (tmp_path / "cache" / "19.0" / "root").exists()


def test_fetch_git_variables_of_a_hook_are_ignored(
    tmp_path: Path, upstream: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, (first, _) = upstream
    hook = tmp_path / "hook"  # what git exports to a hook of the developer's repository
    for name in GIT_REPOSITORY_ENV:
        monkeypatch.setenv(name, str(hook / name.lower()))

    result = _fetch(tmp_path, repo, commit=first)

    assert result.returncode == 0, result.stderr
    assert not hook.exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_fetch_concurrent_calls_all_print_the_root(
    tmp_path: Path, upstream: tuple[Path, list[str]]
) -> None:
    repo, (first, _) = upstream
    command = ["bash", str(FETCH), "19.0"]
    env = _env(tmp_path, repo, first)
    processes = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for _ in range(3)
    ]

    outputs = [process.communicate()[0] for process in processes]

    root = f"{tmp_path / 'cache' / '19.0' / 'root' / 'odoo'}\n".encode()
    assert ([process.returncode for process in processes], outputs) == ([0] * 3, [root] * 3)


@pytest.mark.parametrize("option", ["--latest", "--update"])
def test_fetch_latest_takes_the_branch_tip(
    tmp_path: Path, upstream: tuple[Path, list[str]], option: str
) -> None:
    repo, (first, second) = upstream
    _fetch(tmp_path, repo, commit=first)

    result = _fetch(tmp_path, repo, option, commit=first)  # the pin does not apply

    assert result.returncode == 0, result.stderr
    assert second in result.stderr
    assert (Path(result.stdout.strip()) / "addons/crm/__init__.py").read_text() == "crm = 2\n"


def test_fetch_short_pin_exits_2(tmp_path: Path, upstream: tuple[Path, list[str]]) -> None:
    repo, (first, _) = upstream

    result = _fetch(tmp_path, repo, commit=first[:12])

    assert (result.returncode, result.stdout) == (2, "")
    assert "not a full sha" in result.stderr


def test_fetch_unreachable_upstream_exits_1_and_prints_no_path(tmp_path: Path) -> None:
    result = _fetch(tmp_path, tmp_path / "missing", commit="0" * 40)

    assert (result.returncode, result.stdout) == (1, "")
    assert "cannot fetch" in result.stderr


def test_commits_pin_every_supported_version_to_a_full_sha() -> None:
    lines = [line.split() for line in PINS.read_text().splitlines()]
    pins = {words[0]: words[1] for words in lines if words and not words[0].startswith("#")}

    assert set(pins) == {f"{version}.0" for version in SUPPORTED_VERSIONS}
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in pins.values()), pins
