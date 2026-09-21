#!/usr/bin/env python3
"""GitHub business adapter over the generic centrally authorized Composio client."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pcg_composio import ComposioClient, ComposioError

OWNER = "WWWPCG"
REPO = "pcg-agents"
REPO_API = f"/repos/{OWNER}/{REPO}"
SNAPSHOT_API = f"{REPO_API}/contents/deliverables.toml"
DEFAULT_TIMEOUT = 30
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class GitHubError(RuntimeError):
    """A sanitized GitHub, session, or transport failure."""

    def __init__(self, message: str, status: int | None = None, *, kind: str = "github"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def _sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise GitHubError(f"{context} response was malformed")
    return value


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0%s" % (len(content), content)).hexdigest()


def _repo_path(path: object, *, allow_empty: bool = False) -> str:
    if not isinstance(path, str) or (not path and not allow_empty):
        raise ValueError("repository path is invalid")
    if path.startswith(("/", "\\")) or "\\" in path or "%" in path:
        raise ValueError("repository path is invalid")
    if any(ord(character) < 32 for character in path):
        raise ValueError("repository path is invalid")
    if path:
        parts = PurePosixPath(path).parts
        if any(part in {"", ".", ".."} for part in path.split("/")) or any(
            part in {"", ".", ".."} for part in parts
        ):
            raise ValueError("repository path is invalid")
    return path


def _target(owner: object, repo: object) -> tuple[str, str]:
    if not isinstance(owner, str) or not _NAME_RE.fullmatch(owner):
        raise ValueError("GitHub repository owner is invalid")
    if not isinstance(repo, str) or not _NAME_RE.fullmatch(repo):
        raise ValueError("GitHub repository name is invalid")
    return owner, repo


def _branch(branch: object) -> str:
    if (
        not isinstance(branch, str)
        or not _BRANCH_RE.fullmatch(branch)
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ValueError("GitHub branch is invalid")
    return branch


class ComposioGitHub:
    """Compatibility translator; Composio, not this wrapper, authorizes requests."""

    def __init__(
        self,
        client: ComposioClient | Any,
        *,
        owner: str = OWNER,
        repo: str = REPO,
    ) -> None:
        if client is None or not callable(getattr(client, "execute", None)):
            raise ValueError("a Composio named-tool client is required")
        self.client = client
        self.owner, self.repo = _target(owner, repo)

    @classmethod
    def from_environment(cls, home: object = None, **kwargs: Any) -> "ComposioGitHub":
        """The compatibility home argument is intentionally ignored; no .env is read."""
        owner = kwargs.pop("owner", OWNER)
        repo = kwargs.pop("repo", REPO)
        return cls(ComposioClient.from_environment(**kwargs), owner=owner, repo=repo)

    def _execute(self, tool_slug: str, arguments: dict[str, Any]) -> Any:
        try:
            return self.client.execute(tool_slug, arguments)
        except ComposioError as exc:
            if exc.kind == "session_denied":
                message = "GitHub request was denied by the provisioned Composio session"
            elif exc.kind == "provisioning":
                message = f"required direct tool {tool_slug} is not exposed by the Composio session"
            else:
                message = "GitHub named-tool request failed"
            raise GitHubError(message, status=exc.status, kind=exc.kind) from None

    def safe_repo_path(self, repo_spec: str) -> bool:
        """Compatibility syntax check only; this is not an authorization decision."""
        if not isinstance(repo_spec, str) or repo_spec.count("/") != 1:
            return False
        try:
            _target(*repo_spec.split("/", 1))
        except ValueError:
            return False
        return True

    @staticmethod
    def _route(api_path: str) -> tuple[str, dict[str, Any]]:
        if not isinstance(api_path, str) or not api_path.startswith("/") or "\\" in api_path or "%" in api_path:
            raise ValueError("GitHub API path is unsupported")
        parsed = urlsplit(api_path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("GitHub API path is unsupported")
        segments = parsed.path.split("/")[1:]
        if len(segments) < 3 or segments[0] != "repos":
            raise ValueError("GitHub API path is unsupported")
        owner, repo = _target(segments[1], segments[2])
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
        rest = segments[3:]
        common = {"owner": owner, "repo": repo}
        if not rest and not query:
            return "GITHUB_GET_A_REPOSITORY", common
        if rest and rest[0] == "contents":
            if set(query) - {"ref"} or any(len(values) != 1 or not values[0] for values in query.values()):
                raise ValueError("GitHub API path is unsupported")
            path = "/".join(rest[1:])
            try:
                _repo_path(path, allow_empty=True)
            except ValueError:
                raise ValueError("GitHub API path is unsupported") from None
            arguments = {**common, "path": path}
            if "ref" in query:
                arguments["ref"] = query["ref"][0]
            return "GITHUB_GET_REPOSITORY_CONTENT", arguments
        if len(rest) >= 3 and rest[:2] == ["git", "ref"] and not query:
            ref = "/".join(rest[2:])
            if not ref.startswith(("heads/", "tags/", "pull/")):
                raise ValueError("GitHub API path is unsupported")
            return "GITHUB_GET_A_REFERENCE", {**common, "ref": ref}
        if len(rest) == 3 and rest[:2] == ["git", "trees"]:
            if query != {"recursive": ["1"]}:
                raise ValueError("GitHub API path is unsupported")
            return "GITHUB_GET_A_TREE", {
                **common,
                "tree_sha": rest[2],
                "recursive": True,
            }
        raise ValueError("GitHub API path is unsupported")

    def get(self, api_path: str) -> Any:
        tool, arguments = self._route(api_path)
        result = self._execute(tool, arguments)
        if tool == "GITHUB_GET_REPOSITORY_CONTENT":
            if not isinstance(result, dict) or "content" not in result:
                raise GitHubError("GitHub content response was malformed")
            return result["content"]
        return result

    def get_contents(self, repo_path: str = "", ref: str | None = None) -> Any:
        path = _repo_path(repo_path, allow_empty=True)
        arguments: dict[str, Any] = {"owner": self.owner, "repo": self.repo, "path": path}
        if ref is not None:
            if not isinstance(ref, str) or not ref or any(character in ref for character in "%?#&"):
                raise ValueError("GitHub ref is invalid")
            arguments["ref"] = ref
        result = self._execute("GITHUB_GET_REPOSITORY_CONTENT", arguments)
        if not isinstance(result, dict) or "content" not in result:
            raise GitHubError("GitHub content response was malformed")
        return result["content"]

    def get_contents_list(self, api_path: str) -> list[dict[str, Any]]:
        data = self.get(api_path)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise GitHubError("GitHub contents listing was malformed")
        return data

    @staticmethod
    def _decode_file(data: object, context: str = "GitHub file") -> tuple[bytes, str | None]:
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubError(f"{context} response was malformed")
        if data.get("encoding") != "base64":
            raise GitHubError(f"{context} content was unavailable")
        if "truncated" in data and data["truncated"] is not False:
            raise GitHubError(f"{context} content was unavailable")
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise GitHubError(f"{context} response was malformed")
        try:
            decoded = base64.b64decode(encoded.replace("\n", "").replace("\r", ""), validate=True)
        except (binascii.Error, ValueError):
            raise GitHubError(f"{context} response contained invalid base64") from None
        size = data.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or len(decoded) != size:
            raise GitHubError(f"{context} response had invalid size metadata")
        raw_sha = data.get("sha")
        if raw_sha is not None:
            file_sha = _sha(raw_sha, context)
            if _git_blob_sha(decoded) != file_sha:
                raise GitHubError(f"{context} content did not match its blob SHA")
        else:
            file_sha = None
        return decoded, file_sha

    def read_file(self, repo_path: str, ref: str | None = None) -> bytes:
        decoded, _ = self._decode_file(self.get_contents(repo_path, ref=ref))
        return decoded

    @staticmethod
    def encode_for_write(content: bytes | str) -> str:
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise TypeError("GitHub content must be bytes or text")
        return base64.b64encode(content).decode("ascii")

    @staticmethod
    def _validate_tree(data: object, expected_sha: str | None = None) -> dict[str, dict[str, Any]]:
        if not isinstance(data, dict) or data.get("truncated") is not False:
            raise GitHubError("GitHub tree was incomplete or malformed")
        if expected_sha is not None and _sha(data.get("sha"), "GitHub tree") != expected_sha:
            raise GitHubError("GitHub tree did not match the requested object")
        items = data.get("tree")
        if not isinstance(items, list):
            raise GitHubError("GitHub tree response was malformed")
        tree: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise GitHubError("GitHub tree response was malformed")
            path = _repo_path(item.get("path"))
            if path in tree or item.get("type") not in {"blob", "tree", "commit"}:
                raise GitHubError("GitHub tree response was malformed")
            _sha(item.get("sha"), "GitHub tree")
            tree[path] = {
                key: item[key]
                for key in ("path", "mode", "type", "sha")
                if key in item
            }
        return tree

    def _read_blob(self, sha: str) -> bytes:
        result = self._execute("GITHUB_GET_A_BLOB", {
            "owner": self.owner,
            "repo": self.repo,
            "file_sha": sha,
        })
        if not isinstance(result, dict) or result.get("encoding") != "base64":
            raise GitHubError("GitHub blob response was malformed")
        if _sha(result.get("sha"), "GitHub blob") != sha:
            raise GitHubError("GitHub blob response did not match the requested object")
        encoded = result.get("content")
        if not isinstance(encoded, str):
            raise GitHubError("GitHub blob response was malformed")
        try:
            content = base64.b64decode(encoded.replace("\n", "").replace("\r", ""), validate=True)
        except (binascii.Error, ValueError):
            raise GitHubError("GitHub blob response contained invalid base64") from None
        size = result.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size != len(content):
            raise GitHubError("GitHub blob response had invalid size metadata")
        if _git_blob_sha(content) != sha:
            raise GitHubError("GitHub blob content did not match its SHA")
        return content

    @staticmethod
    def _parse_contents_api(api_path: str) -> tuple[str, str, str]:
        if not isinstance(api_path, str):
            raise ValueError("GitHub contents API path is invalid")
        parsed = urlsplit(api_path)
        if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
            raise ValueError("GitHub contents API path is invalid")
        parts = parsed.path.split("/")
        if len(parts) < 6 or parts[1] != "repos" or parts[4] != "contents":
            raise ValueError("GitHub contents API path is invalid")
        owner, repo = _target(parts[2], parts[3])
        path = _repo_path("/".join(parts[5:]))
        return owner, repo, path

    def put_contents(
        self,
        api_path: str,
        content: bytes | str,
        branch: str,
        message: str,
        sha: str | None,
    ) -> dict[str, Any]:
        """Create one non-retrying fast-forward commit from a verified branch snapshot.

        Failed ref updates can leave unreachable blobs, trees, or commits; those immutable
        objects are harmless and this method never retries, rebases, or force-updates.
        """
        owner, repo, path = self._parse_contents_api(api_path)
        if (owner, repo) != (self.owner, self.repo):
            raise ValueError("GitHub API target does not match this adapter")
        branch = _branch(branch)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("GitHub commit message is required")
        if sha is not None:
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                raise ValueError("expected GitHub file SHA is invalid")
        if isinstance(content, str):
            raw = content.encode("utf-8")
        elif isinstance(content, bytes):
            raw = content
        else:
            raise TypeError("GitHub content must be bytes or text")
        desired_sha = _git_blob_sha(raw)
        common = {"owner": owner, "repo": repo}

        # Resolve the branch exactly once. A missing branch is fatal; there is no
        # fallback to the repository default branch.
        ref = self._execute("GITHUB_GET_A_REFERENCE", {**common, "ref": f"heads/{branch}"})
        try:
            head = _sha(ref["object"]["sha"], "GitHub branch")
        except (KeyError, TypeError):
            raise GitHubError("GitHub branch response was malformed") from None
        if ref.get("ref") not in (None, f"refs/heads/{branch}"):
            raise GitHubError("GitHub branch response was malformed")

        # Independently verify the expected file state at immutable H.
        try:
            current = self.get_contents(path, ref=head)
        except GitHubError as exc:
            if sha is not None or exc.status != 404 or exc.kind == "session_denied":
                raise
            current = None
        if sha is None:
            if current is not None:
                raise GitHubError("GitHub file already exists; snapshot is stale", status=409)
        else:
            if current is None:
                raise GitHubError("GitHub file disappeared; snapshot is stale", status=409)
            _, current_sha = self._decode_file(current, "GitHub current file")
            if current_sha != sha:
                raise GitHubError("GitHub file changed; snapshot is stale", status=409)

        commit = self._execute("GITHUB_GET_COMMIT_OBJECT", {**common, "commit_sha": head})
        if not isinstance(commit, dict) or _sha(commit.get("sha"), "GitHub commit") != head:
            raise GitHubError("GitHub commit response was malformed")
        try:
            base_tree = _sha(commit["tree"]["sha"], "GitHub commit tree")
        except (KeyError, TypeError):
            raise GitHubError("GitHub commit response was malformed") from None

        old_tree_result = self._execute("GITHUB_GET_A_TREE", {
            **common,
            "tree_sha": head,
            "recursive": True,
        })
        old_tree = self._validate_tree(old_tree_result, expected_sha=head)
        old_entry = old_tree.get(path)
        if sha is None:
            if old_entry is not None:
                raise GitHubError("GitHub tree shows the file already exists; snapshot is stale", status=409)
        elif old_entry is None or old_entry.get("type") != "blob" or old_entry.get("sha") != sha:
            raise GitHubError("GitHub tree did not match the expected file snapshot", status=409)

        created_blob = self._execute("GITHUB_CREATE_A_BLOB", {
            **common,
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        })
        if not isinstance(created_blob, dict):
            raise GitHubError("GitHub blob creation response was malformed")
        blob_sha = _sha(created_blob.get("sha"), "GitHub created blob")
        if blob_sha != desired_sha:
            raise GitHubError("GitHub created an unexpected blob; outcome requires review")
        if self._read_blob(blob_sha) != raw:
            raise GitHubError("GitHub created blob could not be verified; outcome requires review")

        created_tree = self._execute("GITHUB_CREATE_A_TREE", {
            **common,
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}],
        })
        if not isinstance(created_tree, dict):
            raise GitHubError("GitHub tree creation response was malformed")
        tree_sha = _sha(created_tree.get("sha"), "GitHub created tree")
        new_tree_result = self._execute("GITHUB_GET_A_TREE", {
            **common,
            "tree_sha": tree_sha,
            "recursive": True,
        })
        new_tree = self._validate_tree(new_tree_result, expected_sha=tree_sha)
        expected_tree = dict(old_tree)
        expected_tree[path] = {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        if new_tree != expected_tree:
            raise GitHubError("GitHub created tree changed unexpected paths; outcome requires review")

        created_commit = self._execute("GITHUB_CREATE_A_COMMIT", {
            **common,
            "message": message,
            "tree": tree_sha,
            "parents": [head],
        })
        if not isinstance(created_commit, dict):
            raise GitHubError("GitHub commit creation response was malformed")
        commit_sha = _sha(created_commit.get("sha"), "GitHub created commit")
        verified_commit = self._execute("GITHUB_GET_COMMIT_OBJECT", {
            **common,
            "commit_sha": commit_sha,
        })
        try:
            verified_sha = _sha(verified_commit["sha"], "GitHub created commit")
            verified_tree = _sha(verified_commit["tree"]["sha"], "GitHub created commit tree")
            parents = verified_commit["parents"]
            parent_shas = [_sha(parent["sha"], "GitHub created commit parent") for parent in parents]
        except (KeyError, TypeError):
            raise GitHubError("GitHub created commit response was malformed") from None
        if verified_sha != commit_sha or verified_tree != tree_sha or parent_shas != [head]:
            raise GitHubError("GitHub created commit did not match its requested parent and tree")

        updated = self._execute("GITHUB_UPDATE_A_REFERENCE", {
            **common,
            "ref": f"heads/{branch}",
            "sha": commit_sha,
            "force": False,
        })
        try:
            updated_sha = _sha(updated["object"]["sha"], "GitHub updated reference")
        except (KeyError, TypeError):
            raise GitHubError("GitHub reference update response was malformed; outcome requires review") from None
        if updated_sha != commit_sha:
            raise GitHubError("GitHub reference update returned an unexpected commit; outcome requires review")

        final_ref = self._execute("GITHUB_GET_A_REFERENCE", {**common, "ref": f"heads/{branch}"})
        try:
            final_sha = _sha(final_ref["object"]["sha"], "GitHub verified reference")
        except (KeyError, TypeError):
            raise GitHubError("GitHub reference verification was malformed; outcome requires review") from None
        if final_sha != commit_sha:
            raise GitHubError("GitHub branch moved before verification; outcome requires review", status=409)
        final_file = self.get_contents(path, ref=commit_sha)
        final_content, final_blob = self._decode_file(final_file, "GitHub written file")
        if final_blob != blob_sha or final_content != raw:
            raise GitHubError("GitHub written file could not be verified; outcome requires review")

        return {
            "commit": {
                "sha": commit_sha,
                "html_url": f"https://github.com/{owner}/{repo}/commit/{commit_sha}",
            },
            "content": {"sha": blob_sha},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PCG Composio GitHub session check")
    parser.add_argument("--check", action="store_true", help="run a read-only private repository check")
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("--check is required")
    try:
        adapter = ComposioGitHub.from_environment()
        repo = adapter.get(REPO_API)
        if not isinstance(repo, dict) or repo.get("full_name") != f"{OWNER}/{REPO}":
            raise GitHubError("GitHub repository probe was malformed")
        adapter.read_file("health.toml")
    except (GitHubError, ValueError) as exc:
        print(f"GitHub check failed: {exc}")
        return 1
    print("GitHub check passed: repository and private file are readable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
