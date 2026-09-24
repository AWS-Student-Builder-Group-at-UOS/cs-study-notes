import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib import error, request


class SnapshotError(RuntimeError):
    pass


def _git(
    root: Path, *args: str, purpose: str, allowed_codes: Tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        raise SnapshotError("Git is unavailable. Install Git before running the study script.") from None
    except subprocess.TimeoutExpired:
        raise SnapshotError("Git timed out while reading the study history. Retry the run.") from None
    except OSError:
        raise SnapshotError("Cannot start Git. Check the checkout directory and permissions.") from None
    if result.returncode not in allowed_codes:
        raise SnapshotError(purpose) from None
    return result


def github_repository(root: Path) -> Optional[str]:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        remote = _git(root, "remote", "get-url", "origin", purpose="Cannot read origin.",
                      allowed_codes=(0, 2, 128)).stdout.decode().strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", remote)
        repository = match.group(1) if match else None
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise SnapshotError("GitHub repository name is invalid.")
    if os.environ.get("GITHUB_ACTIONS") == "true" and not repository:
        raise SnapshotError("GitHub repository is required to verify the submission deadline.")
    return repository


def github_revision(root: Path, repository: str, cutoff: datetime) -> str:
    endpoint = f"https://api.github.com/repos/{repository}/activity"
    url = endpoint + "?ref=refs%2Fheads%2Fmain&direction=desc&per_page=100"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for page in range(50):
        try:
            with request.urlopen(request.Request(url, headers=headers), timeout=20) as response:
                records = json.load(response)
                links = response.headers.get("Link", "")
            if not isinstance(records, list):
                raise ValueError
            if page == 0 and records:
                latest = records[0]["after"]
                if not isinstance(latest, str) or not re.fullmatch(r"[0-9a-f]{40}", latest):
                    raise ValueError
                baseline = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main",
                                purpose="Cannot read origin/main.", allowed_codes=(0, 128))
                if baseline.returncode == 0 and latest != baseline.stdout.decode().strip():
                    behind = _git(root, "merge-base", "--is-ancestor", latest, "refs/remotes/origin/main",
                                  purpose="Cannot verify GitHub activity freshness.", allowed_codes=(0, 1, 128))
                    if behind.returncode == 0:
                        raise SnapshotError("GitHub 활동 기록이 아직 최신 main을 반영하지 않았습니다. 잠시 후 다시 실행하세요.")
            for record in records:
                stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or record["ref"] != "refs/heads/main":
                    raise ValueError
                if stamp >= cutoff:
                    continue
                revision = record["after"]
                if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision == "0" * 40:
                    raise ValueError
                present = _git(root, "cat-file", "-e", revision + "^{commit}",
                               purpose="Cannot inspect deadline commit.", allowed_codes=(0, 1, 128))
                if present.returncode:
                    _git(root, "fetch", "origin", revision, purpose="Cannot fetch deadline commit.")
                return revision
        except (error.URLError, OSError, ValueError, KeyError, TypeError):
            raise SnapshotError("GitHub 활동 기록을 확인하지 못했습니다. 마감 결과를 확정하지 않았습니다.") from None
        next_link = re.search(r'<([^>]+)>; rel="next"', links)
        if not next_link:
            break
        url = next_link.group(1)
        if not url.startswith(endpoint + "?"):
            raise SnapshotError("GitHub activity pagination URL is invalid.")
    raise SnapshotError("마감 이전 main의 GitHub 활동 기록이 없습니다. 결과를 수동으로 확인해 주세요.")


def snapshot_revision(root: Path, cutoff: datetime) -> Optional[str]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise SnapshotError("The snapshot cutoff must include a timezone (for example Asia/Seoul).")
    root = Path(root).resolve()
    top_level = _git(
        root,
        "rev-parse",
        "--show-toplevel",
        purpose="Cannot read the repository. Run from a valid, non-bare Git checkout.",
    ).stdout.rstrip(b"\r\n")
    if Path(os.fsdecode(top_level)).resolve() != root:
        raise SnapshotError("The snapshot root must be the repository's top-level directory.")
    shallow = _git(
        root,
        "rev-parse",
        "--is-shallow-repository",
        purpose="Cannot determine whether Git history is complete. Fetch the full repository history.",
    ).stdout.strip()
    if shallow != b"false":
        raise SnapshotError("A full Git history is required. Set actions/checkout fetch-depth to 0.")

    repository = github_repository(root)
    if repository:
        return github_revision(root, repository, cutoff)

    head_result = _git(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD",
        purpose="Cannot resolve HEAD. Restore a valid branch checkout.",
        allowed_codes=(0, 1),
    )
    if head_result.returncode == 1:
        branch = _git(
            root,
            "symbolic-ref",
            "--quiet",
            "HEAD",
            purpose="Cannot resolve HEAD. Restore a valid branch checkout.",
        ).stdout.strip()
        refs = _git(
            root,
            "for-each-ref",
            "--format=%(refname)",
            os.fsdecode(branch),
            purpose="Cannot read branch references. Restore a valid branch checkout.",
        ).stdout.splitlines()
        if branch in refs:
            raise SnapshotError("The current branch's HEAD is invalid. Restore the repository history.") from None
        return None
    if not head_result.stdout.strip():
        raise SnapshotError("Cannot resolve HEAD. Restore a valid branch checkout.")

    history = _git(
        root,
        "log",
        "--first-parent",
        "--format=%H %ct",
        "HEAD",
        "--",
        purpose="Cannot read complete commit history. Fetch all history and retry.",
    ).stdout.splitlines()
    cutoff_timestamp = cutoff.timestamp()
    for entry in history:
        try:
            revision, timestamp = entry.split(b" ", 1)
            commit_timestamp = int(timestamp)
            revision_text = revision.decode("ascii")
            if len(revision_text) not in (40, 64) or any(
                character not in "0123456789abcdef" for character in revision_text
            ):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise SnapshotError("Git returned an invalid commit record. Check repository integrity.") from None
        if commit_timestamp < cutoff_timestamp:
            return revision_text
    return None


def _materialize(root: Path, revision: str, destination: Path) -> None:
    tree = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
        purpose="Cannot read the deadline commit's files. Fetch full history and retry.",
    ).stdout
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ")
        except ValueError:
            raise SnapshotError("Git returned an invalid file record. Check repository integrity.") from None
        if mode not in (b"100644", b"100755") or object_type != b"blob":
            continue
        parts = raw_path.split(b"/")
        if any(part in (b"", b".", b"..") or part.lower() == b".git" for part in parts):
            raise SnapshotError("The deadline commit contains an unsafe path. Repair the repository tree.")
        target = destination.joinpath(*(os.fsdecode(part) for part in parts))
        try:
            target.resolve().relative_to(destination.resolve())
        except (OSError, ValueError):
            raise SnapshotError("The deadline commit contains an unsafe path. Repair the repository tree.") from None
        try:
            blob_id = object_id.decode("ascii")
        except UnicodeDecodeError:
            raise SnapshotError("Git returned an invalid object identifier. Check repository integrity.") from None
        data = _git(
            root,
            "cat-file",
            "blob",
            blob_id,
            purpose="Cannot read a submission file from Git. Fetch complete history and retry.",
        ).stdout
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            raise SnapshotError("Cannot create the temporary snapshot. Check free space and file permissions.") from None


@contextmanager
def snapshot_at(root: Path, cutoff: datetime) -> Iterator[Path]:
    root = Path(root).resolve()
    revision = snapshot_revision(root, cutoff)
    with tempfile.TemporaryDirectory(prefix="cs-study-snapshot-") as temporary:
        destination = Path(temporary)
        if revision is not None:
            _materialize(root, revision, destination)
        yield destination
