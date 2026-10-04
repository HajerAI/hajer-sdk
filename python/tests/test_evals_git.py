"""`hajer.evals._git`: what an eval run can say about its source, and what it must never say.

The repository cases use the real `git` on a repository made under `tmp_path`: local, no socket. The
provider cases hand in a plain dictionary where the whitelisted snapshot would go, and a runner that
refuses every command, so they are pure. Every wire assertion is on the whole dict, because the shape
is what the platform reads.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from hajer.evals._git import GitContext, GitRunner, git_context, strip_credentials

SHA = re.compile(r"[0-9a-f]{40}")


def _git(repo: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    completed = subprocess.run(  # noqa: S603 - the real git, on a repository this test made
        [executable, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One empty commit on `main`, with an `origin` whose URL carries a credential."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "one",
    )
    _git(tmp_path, "remote", "add", "origin", "https://user:secret@example.com/org/repo.git")
    return tmp_path


def _refusing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """A git that declines every question: not a repository, no such remote."""
    return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: not a git repository")


def _missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """No git on PATH."""
    raise FileNotFoundError("git")


def _hanging(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """A git that never came back."""
    raise subprocess.TimeoutExpired(cmd=["git"], timeout=5.0)


# ── a repository, through the real git ───────────────────────────────────────────────────────────


def test_a_repository_reports_its_commit_branch_cleanliness_and_a_stripped_remote(repo: Path) -> None:
    context = git_context(repo, {})
    assert context is not None
    assert context.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert SHA.fullmatch(context.commit_sha or "")
    assert context.branch == _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert context.dirty is False
    assert context.remote_url == "https://example.com/org/repo.git"
    assert "secret" not in (context.remote_url or "")
    assert context.ci is None
    assert context.to_wire() == {
        "commitSha": context.commit_sha,
        "branch": "main",
        "dirty": False,
        "remoteUrl": "https://example.com/org/repo.git",
        "ci": None,
    }


def test_a_staged_change_is_dirty_and_an_untracked_file_is_not(repo: Path) -> None:
    (repo / "note.txt").write_text("hello", encoding="utf-8")
    untracked = git_context(repo, {})
    assert untracked is not None
    assert untracked.dirty is False, "an untracked file is not a change to what was committed"

    _git(repo, "add", "note.txt")
    staged = git_context(repo, {})
    assert staged is not None
    assert staged.dirty is True


def test_a_detached_head_has_no_branch_but_still_a_commit(repo: Path) -> None:
    _git(repo, "checkout", "-q", "--detach")
    context = git_context(repo, {})
    assert context is not None
    assert context.branch is None
    assert SHA.fullmatch(context.commit_sha or "")


def test_a_repository_without_an_origin_has_no_remote(repo: Path) -> None:
    _git(repo, "remote", "remove", "origin")
    context = git_context(repo, {})
    assert context is not None
    assert context.remote_url is None
    assert SHA.fullmatch(context.commit_sha or "")


# ── not a repository ─────────────────────────────────────────────────────────────────────────────


def test_outside_a_repository_with_no_ci_there_is_nothing_to_say(tmp_path: Path) -> None:
    assert git_context(tmp_path, {}) is None


def test_outside_a_repository_under_generic_ci_the_job_is_all_that_is_known(tmp_path: Path) -> None:
    context = git_context(tmp_path, {"CI": "true"})
    assert context is not None
    assert context.ci is not None
    assert context.ci.provider == "generic"
    assert context.to_wire() == {
        "commitSha": None,
        "branch": None,
        "dirty": None,
        "remoteUrl": None,
        "ci": {
            "provider": "generic",
            "runId": None,
            "prNumber": None,
            "baseRef": None,
            "headRef": None,
            "repository": None,
        },
    }


# ── a git that cannot answer ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("runner", [_refusing, _missing, _hanging], ids=["non-zero", "oserror", "timeout"])
def test_a_git_that_cannot_answer_leaves_nulls_and_never_raises(tmp_path: Path, runner: GitRunner) -> None:
    context = git_context(tmp_path, {"CI": "true"}, run=runner)
    assert context is not None
    assert (context.commit_sha, context.branch, context.dirty, context.remote_url) == (None, None, None, None)
    assert context.ci is not None
    assert context.ci.provider == "generic"


@pytest.mark.parametrize("runner", [_refusing, _missing, _hanging], ids=["non-zero", "oserror", "timeout"])
def test_a_git_that_cannot_answer_and_no_ci_is_none(tmp_path: Path, runner: GitRunner) -> None:
    assert git_context(tmp_path, {}, run=runner) is None


def test_the_runner_is_asked_with_a_fixed_vector_and_no_shell(tmp_path: Path) -> None:
    seen: list[tuple[list[str], Path, float]] = []

    def recording(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = kwargs["cwd"]
        timeout = kwargs["timeout"]
        assert isinstance(cwd, Path)
        assert isinstance(timeout, float)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        seen.append((argv, cwd, timeout))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="true\n", stderr="")

    git_context(tmp_path, {}, run=recording, timeout_s=1.5)
    assert seen[0] == (["git", "rev-parse", "--is-inside-work-tree"], tmp_path, 1.5)
    assert [argv for argv, _, _ in seen] == [
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "config", "--get", "remote.origin.url"],
    ]


# ── one case per provider, on the wire ───────────────────────────────────────────────────────────


def test_github_actions_pull_request(tmp_path: Path) -> None:
    context = git_context(
        tmp_path,
        {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "GITHUB_SHA": "b" * 40,
            "GITHUB_REF": "refs/pull/42/merge",
            "GITHUB_REF_NAME": "42/merge",
            "GITHUB_HEAD_REF": "feature/x",
            "GITHUB_BASE_REF": "main",
            "GITHUB_RUN_ID": "123456",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_SERVER_URL": "https://github.com",
        },
        run=_refusing,
    )
    assert context is not None
    assert context.to_wire() == {
        "commitSha": "b" * 40,
        "branch": "feature/x",
        "dirty": None,
        "remoteUrl": None,
        "ci": {
            "provider": "github-actions",
            "runId": "123456",
            "prNumber": 42,
            "baseRef": "main",
            "headRef": "feature/x",
            "repository": "org/repo",
        },
    }


def test_github_actions_push_uses_the_ref_name_and_has_no_pull_request(tmp_path: Path) -> None:
    context = git_context(
        tmp_path,
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_SHA": "b" * 40,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main",
            "GITHUB_BASE_REF": "",
            "GITHUB_RUN_ID": "7",
            "GITHUB_REPOSITORY": "org/repo",
        },
        run=_refusing,
    )
    assert context is not None
    assert context.ci is not None
    assert (context.branch, context.ci.head_ref, context.ci.base_ref, context.ci.pr_number) == (
        "main",
        "main",
        None,
        None,
    )


def test_gitlab_merge_request(tmp_path: Path) -> None:
    context = git_context(
        tmp_path,
        {
            "CI": "true",
            "GITLAB_CI": "true",
            "CI_COMMIT_SHA": "c" * 40,
            "CI_COMMIT_REF_NAME": "refs/merge-requests/7/head",
            "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature/y",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main",
            "CI_MERGE_REQUEST_IID": "7",
            "CI_PIPELINE_ID": "999",
            "CI_PROJECT_URL": "https://gitlab.example.com/org/repo",
        },
        run=_refusing,
    )
    assert context is not None
    assert context.to_wire() == {
        "commitSha": "c" * 40,
        "branch": "feature/y",
        "dirty": None,
        "remoteUrl": None,
        "ci": {
            "provider": "gitlab-ci",
            "runId": "999",
            "prNumber": 7,
            "baseRef": "main",
            "headRef": "feature/y",
            "repository": "https://gitlab.example.com/org/repo",
        },
    }


def test_circleci_pull_request_number_is_the_url_tail(tmp_path: Path) -> None:
    context = git_context(
        tmp_path,
        {
            "CI": "true",
            "CIRCLECI": "true",
            "CIRCLE_SHA1": "d" * 40,
            "CIRCLE_BRANCH": "feature/z",
            "CIRCLE_PULL_REQUEST": "https://github.com/org/repo/pull/15",
            "CIRCLE_BUILD_NUM": "77",
            "CIRCLE_REPOSITORY_URL": "git@github.com:org/repo.git",
        },
        run=_refusing,
    )
    assert context is not None
    assert context.to_wire() == {
        "commitSha": "d" * 40,
        "branch": "feature/z",
        "dirty": None,
        "remoteUrl": None,
        "ci": {
            "provider": "circleci",
            "runId": "77",
            "prNumber": 15,
            "baseRef": None,
            "headRef": "feature/z",
            "repository": "git@github.com:org/repo.git",
        },
    }


def test_buildkite_spells_no_pull_request_as_false_and_its_repo_loses_its_token(tmp_path: Path) -> None:
    context = git_context(
        tmp_path,
        {
            "CI": "true",
            "BUILDKITE": "true",
            "BUILDKITE_COMMIT": "e" * 40,
            "BUILDKITE_BRANCH": "feature/w",
            "BUILDKITE_PULL_REQUEST": "false",
            "BUILDKITE_PULL_REQUEST_BASE_BRANCH": "",
            "BUILDKITE_BUILD_ID": "0190a0b0-c0d0-7000-8000-000000000001",
            "BUILDKITE_REPO": "https://x-access-token@github.com/org/repo.git",
        },
        run=_refusing,
    )
    assert context is not None
    assert context.to_wire() == {
        "commitSha": "e" * 40,
        "branch": "feature/w",
        "dirty": None,
        "remoteUrl": None,
        "ci": {
            "provider": "buildkite",
            "runId": "0190a0b0-c0d0-7000-8000-000000000001",
            "prNumber": None,
            "baseRef": None,
            "headRef": "feature/w",
            "repository": "https://github.com/org/repo.git",
        },
    }


def test_a_provider_flag_outranks_the_generic_ci_flag(tmp_path: Path) -> None:
    context = git_context(tmp_path, {"CI": "true", "CIRCLECI": "true"}, run=_refusing)
    assert context is not None
    assert context.ci is not None
    assert context.ci.provider == "circleci"


def test_ci_sha_and_branch_override_the_local_read_but_dirty_and_remote_stay_local(repo: Path) -> None:
    context = git_context(
        repo,
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF": "refs/pull/3/merge",
            "GITHUB_HEAD_REF": "feature/x",
            "GITHUB_BASE_REF": "main",
        },
    )
    assert context is not None
    assert context.commit_sha == "a" * 40 != _git(repo, "rev-parse", "HEAD")
    assert context.branch == "feature/x"
    assert context.dirty is False
    assert context.remote_url == "https://example.com/org/repo.git"


def test_a_ci_that_names_no_sha_falls_back_to_the_local_one(repo: Path) -> None:
    context = git_context(repo, {"CI": "true"})
    assert context is not None
    assert context.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert context.branch == "main"
    assert context.ci is not None
    assert context.ci.provider == "generic"


def test_the_models_are_frozen_and_closed() -> None:
    context = GitContext(commit_sha="f" * 40)
    with pytest.raises(ValidationError, match="frozen"):
        context.commit_sha = None
    with pytest.raises(ValidationError, match="Extra inputs"):
        GitContext.model_validate({"commit_sha": None, "author": "nobody"})


# ── credentials never leave ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:token@host/org/repo.git", "https://host/org/repo.git"),
        ("https://token@host/org/repo.git", "https://host/org/repo.git"),
        ("http://user:pw@host:8080/org/repo.git", "http://host:8080/org/repo.git"),
        ("https://host/org/repo.git", "https://host/org/repo.git"),
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        ("ssh://git@host/org/repo.git", "ssh://git@host/org/repo.git"),
        ("ssh://git:secret@host:2222/org/repo.git", "ssh://git@host:2222/org/repo.git"),
        ("/srv/git/repo.git", "/srv/git/repo.git"),
        ("file:///srv/git/repo.git", "file:///srv/git/repo.git"),
        ("", ""),
    ],
)
def test_strip_credentials(url: str, expected: str) -> None:
    assert strip_credentials(url) == expected
