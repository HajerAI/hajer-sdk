"""Where an eval run came from: the commit, the branch, the CI job — read without ever raising.

A run report that cannot say which commit it graded is a report nobody can act on, so `hajer eval` asks
git and the CI environment before it starts. Two rules shape how it asks. First, **a missing answer is a
null, never a failure of the run**: no git on `PATH`, a directory that is not a repository, a command
that hangs — each leaves its field `None` and the run goes on. Second, **nothing read here can carry a
secret out of the process**: the CI variables arrive as the whitelisted snapshot
(`hajer._settings.ci_environment_snapshot`), so a job's tokens are never in reach, and every repository
URL — the local `origin` or a provider's — has its userinfo removed before it is kept, because a clone
URL with a token in it is how a CI job authenticates, and an eval upload must not become its exfiltration.

A CI-provided sha and branch win over the local read, because CI checks out a detached merge commit
whose `HEAD` says nothing useful; `dirty` and the remote still come from git, which is the only place
they exist.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from hajer._json import JsonObject

#: The one shape of runner this module spawns git through: `subprocess.run`, or a test's stand-in.
GitRunner = Callable[..., subprocess.CompletedProcess[str]]

#: `refs/pull/<n>/merge` — the only place GitHub Actions states the pull request number for a PR event.
_GITHUB_PULL_REF: Final[re.Pattern[str]] = re.compile(r"refs/pull/(\d+)/", flags=re.ASCII)
#: Schemes whose userinfo is removed whole. A token travels as the user name (`https://token@host`) as
#: often as the password, so for a web URL nothing before the `@` is kept; an SSH URL's user name is the
#: account the key is for (`git@`), not a secret, so there only a password is removed.
_WEB_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
#: What `git rev-parse --abbrev-ref HEAD` prints when there is no branch to name.
_DETACHED: Final[str] = "HEAD"


class CiContext(BaseModel):
    """The CI job an eval ran under, as far as its provider states it: nulls for what it does not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    run_id: str | None = None
    pr_number: int | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    repository: str | None = None

    def to_wire(self) -> JsonObject:
        """The platform's camelCase; a null is sent as a null so the server can tell "unknown" from "absent"."""
        return {
            "provider": self.provider,
            "runId": self.run_id,
            "prNumber": self.pr_number,
            "baseRef": self.base_ref,
            "headRef": self.head_ref,
            "repository": self.repository,
        }


class GitContext(BaseModel):
    """What the run can say about its source: each field `None` where git or CI could not answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    remote_url: str | None = None
    ci: CiContext | None = None

    def to_wire(self) -> JsonObject:
        """The platform's camelCase, nulls included."""
        return {
            "commitSha": self.commit_sha,
            "branch": self.branch,
            "dirty": self.dirty,
            "remoteUrl": self.remote_url,
            "ci": None if self.ci is None else self.ci.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class _CiRead:
    """A provider's answer: the job, plus the sha and branch it states, which outrank the local read."""

    context: CiContext
    commit_sha: str | None
    branch: str | None


def git_context(
    root: Path,
    ci: Mapping[str, str],
    *,
    run: GitRunner = subprocess.run,
    # The one literal bound in this module, and only because the signature is callable on its own: the
    # runner passes the setting it reads, and a git that has not answered in five seconds is a git that
    # is not going to (a network-mounted checkout, a credential helper waiting on a terminal).
    timeout_s: float = 5.0,
) -> GitContext | None:
    """The source of an eval run under `root`, or `None` when there is nothing at all to say.

    `None` only when `root` is not inside a git work tree *and* `ci` names no provider — a run from an
    unpacked tarball on a laptop. Anything else is a context with what could be learned and nulls for
    the rest. `ci` is the whitelisted snapshot, never the raw environment: this module reads no variable
    that is not in `CI_VARIABLES`.
    """
    read = _read_ci(ci)
    inside = _first_line(_git(root, ("rev-parse", "--is-inside-work-tree"), run, timeout_s)) == "true"
    if not inside and read is None:
        return None

    local_sha: str | None = None
    local_branch: str | None = None
    dirty: bool | None = None
    remote_url: str | None = None
    if inside:
        local_sha = _first_line(_git(root, ("rev-parse", "HEAD"), run, timeout_s))
        named = _first_line(_git(root, ("rev-parse", "--abbrev-ref", "HEAD"), run, timeout_s))
        local_branch = None if named == _DETACHED else named
        status = _git(root, ("status", "--porcelain", "--untracked-files=no"), run, timeout_s)
        dirty = None if status is None else bool(status.strip())
        origin = _first_line(_git(root, ("config", "--get", "remote.origin.url"), run, timeout_s))
        remote_url = None if origin is None else strip_credentials(origin)

    if read is None:
        return GitContext(commit_sha=local_sha, branch=local_branch, dirty=dirty, remote_url=remote_url, ci=None)
    return GitContext(
        commit_sha=read.commit_sha or local_sha,
        branch=read.branch or local_branch,
        dirty=dirty,
        remote_url=remote_url,
        ci=read.context,
    )


def strip_credentials(url: str) -> str:
    """The URL without what authenticates it.

    `https://user:token@host/…` and `https://token@host/…` both become `https://host/…`; an SSH URL keeps
    its user name (`ssh://git@host/…` is the account, not a secret) and loses only a password; an scp-like
    `git@host:org/repo.git` has no userinfo a URL parser can see and is returned as it came, as is a local
    path. The parse never raises on a string: `urlsplit` accepts anything.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    netloc = host if parts.scheme in _WEB_SCHEMES else f"{userinfo.split(':', 1)[0]}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ── local reads ──────────────────────────────────────────────────────────────────────────────────


def _git(root: Path, args: tuple[str, ...], run: GitRunner, timeout_s: float) -> str | None:
    """git's stdout for one fixed argument vector, or `None` for any way it could fail to answer.

    No shell, no prompt: `capture_output` keeps a credential helper from reaching a terminal, the timeout
    keeps a hung filesystem from holding the run, and a non-zero exit is a question git declined, which
    is an answer of `None` and not an error.
    """
    try:
        completed = run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        # OSError: no git on PATH, or no such directory. TimeoutExpired: it hung. ValueError: an argument
        # the platform refuses (a NUL in the path). Each is "git could not answer", which is a null.
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _first_line(output: str | None) -> str | None:
    """The one value a `rev-parse` or `config --get` prints, or `None` for no output at all."""
    if output is None:
        return None
    line = output.strip().splitlines()[0].strip() if output.strip() else ""
    return line or None


# ── CI providers ─────────────────────────────────────────────────────────────────────────────────


def _github(ci: Mapping[str, str]) -> _CiRead:
    # For a pull request event `GITHUB_REF_NAME` is `<n>/merge`, the synthetic merge ref; the branch the
    # author pushed is `GITHUB_HEAD_REF`, which is set only then.
    head = _value(ci, "GITHUB_HEAD_REF") or _value(ci, "GITHUB_REF_NAME")
    return _CiRead(
        context=CiContext(
            provider="github-actions",
            run_id=_value(ci, "GITHUB_RUN_ID"),
            pr_number=_pull_request_from_ref(_value(ci, "GITHUB_REF")),
            base_ref=_value(ci, "GITHUB_BASE_REF"),
            head_ref=head,
            repository=_repository(_value(ci, "GITHUB_REPOSITORY")),
        ),
        commit_sha=_value(ci, "GITHUB_SHA"),
        branch=head,
    )


def _gitlab(ci: Mapping[str, str]) -> _CiRead:
    # In a merge request pipeline `CI_COMMIT_REF_NAME` may be the merge ref rather than the source branch;
    # the source branch variable is set only then, so it is preferred.
    head = _value(ci, "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME") or _value(ci, "CI_COMMIT_REF_NAME")
    return _CiRead(
        context=CiContext(
            provider="gitlab-ci",
            run_id=_value(ci, "CI_PIPELINE_ID"),
            pr_number=_number(_value(ci, "CI_MERGE_REQUEST_IID")),
            base_ref=_value(ci, "CI_MERGE_REQUEST_TARGET_BRANCH_NAME"),
            head_ref=head,
            repository=_repository(_value(ci, "CI_PROJECT_URL")),
        ),
        commit_sha=_value(ci, "CI_COMMIT_SHA"),
        branch=head,
    )


def _circleci(ci: Mapping[str, str]) -> _CiRead:
    branch = _value(ci, "CIRCLE_BRANCH")
    return _CiRead(
        context=CiContext(
            provider="circleci",
            run_id=_value(ci, "CIRCLE_BUILD_NUM"),
            pr_number=_pull_request_from_url(_value(ci, "CIRCLE_PULL_REQUEST")),
            base_ref=None,
            head_ref=branch,
            repository=_repository(_value(ci, "CIRCLE_REPOSITORY_URL")),
        ),
        commit_sha=_value(ci, "CIRCLE_SHA1"),
        branch=branch,
    )


def _buildkite(ci: Mapping[str, str]) -> _CiRead:
    branch = _value(ci, "BUILDKITE_BRANCH")
    pull_request = _value(ci, "BUILDKITE_PULL_REQUEST")
    return _CiRead(
        context=CiContext(
            provider="buildkite",
            run_id=_value(ci, "BUILDKITE_BUILD_ID"),
            # Buildkite spells "not a pull request" as the string `false`, which `_number` reads as none.
            pr_number=_number(pull_request),
            base_ref=_value(ci, "BUILDKITE_PULL_REQUEST_BASE_BRANCH"),
            head_ref=branch,
            repository=_repository(_value(ci, "BUILDKITE_REPO")),
        ),
        commit_sha=_value(ci, "BUILDKITE_COMMIT"),
        branch=branch,
    )


def _generic(ci: Mapping[str, str]) -> _CiRead:  # noqa: ARG001 - the generic flag states nothing but itself
    return _CiRead(context=CiContext(provider="generic"), commit_sha=None, branch=None)


#: Detection order. A provider's own flag before the generic `CI`, which every one of them also sets.
_PROVIDERS: Final[tuple[tuple[str, Callable[[Mapping[str, str]], _CiRead]], ...]] = (
    ("GITHUB_ACTIONS", _github),
    ("GITLAB_CI", _gitlab),
    ("CIRCLECI", _circleci),
    ("BUILDKITE", _buildkite),
    ("CI", _generic),
)


def _read_ci(ci: Mapping[str, str]) -> _CiRead | None:
    for flag, read in _PROVIDERS:
        if _value(ci, flag) is not None:
            return read(ci)
    return None


def _value(ci: Mapping[str, str], name: str) -> str | None:
    """A variable's value, or `None` for absent or blank: a provider that sets `GITHUB_BASE_REF=` means none."""
    raw = ci.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _number(value: str | None) -> int | None:
    """A positive integer, or `None`: `false`, blank and anything else a provider spells "none" as."""
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _pull_request_from_ref(ref: str | None) -> int | None:
    if ref is None:
        return None
    matched = _GITHUB_PULL_REF.match(ref)
    return None if matched is None else _number(matched.group(1))


def _pull_request_from_url(url: str | None) -> int | None:
    """The trailing path segment of a pull request URL (`…/pull/15`), which is the number."""
    if url is None:
        return None
    return _number(url.rstrip("/").rsplit("/", 1)[-1])


def _repository(value: str | None) -> str | None:
    """A provider's repository as given, minus any credential: an `org/repo` has none and passes through."""
    return None if value is None else strip_credentials(value)
