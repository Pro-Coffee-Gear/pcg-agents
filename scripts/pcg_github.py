#!/usr/bin/env python3
"""Constrained Composio CLI gateway for the PCG GitHub repository."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

OWNER = "WWWPCG"
REPO = "pcg-agents"
REPO_API = f"/repos/{OWNER}/{REPO}"
SNAPSHOT_API = f"{REPO_API}/contents/deliverables.toml"
DEFAULT_TIMEOUT = 30
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitHubError(RuntimeError):
    """A sanitized GitHub or broker failure."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _env_file_values(home: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = home / ".env"
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise GitHubError("unable to read GitHub gateway configuration") from None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _configured_value(values: dict[str, str], *names: str) -> str:
    """Honor caller name priority; for each name, file precedes process env."""
    for name in names:
        if values.get(name):
            return values[name]
        if os.environ.get(name):
            return os.environ[name]
    return ""


class ComposioGitHub:
    """Minimal GitHub adapter with repository and method allowlists."""

    def __init__(
        self,
        account: str,
        cli_config: str | os.PathLike[str],
        allow_snapshot_write: bool = False,
        timeout: int | float = DEFAULT_TIMEOUT,
        runner=subprocess.run,
    ) -> None:
        if not isinstance(account, str) or not account.strip():
            raise ValueError("Composio GitHub account selector is required")
        if any(ord(char) < 32 for char in account) or account.startswith("-"):
            raise ValueError("Composio GitHub account selector is invalid")
        cli = Path(cli_config) if cli_config else None
        if cli is None or not cli.is_absolute():
            raise ValueError("PCG_COMPOSIO_CLI must be an absolute executable path")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("Composio timeout must be positive")
        self.account = account
        self.cli_config = str(cli)
        self.allow_snapshot_write = bool(allow_snapshot_write)
        self.timeout = timeout
        self._runner = runner
        self.owner = OWNER
        self.repo = REPO

    @classmethod
    def from_environment(cls, home: str | os.PathLike[str] | None = None, **kwargs: Any) -> "ComposioGitHub":
        home_path = Path(home) if home is not None else Path(os.environ.get("HERMES_HOME", "/opt/data"))
        values = _env_file_values(home_path)
        account = _configured_value(values, "PCG_GITHUB_ACCOUNT")
        executable = _configured_value(values, "PCG_COMPOSIO_CLI")
        if not executable:
            executable = str(Path.home() / ".composio" / "composio")
        allow = _configured_value(values, "PCG_GITHUB_ALLOW_SNAPSHOT_WRITE").lower() == "true"
        if not account:
            raise ValueError("PCG_GITHUB_ACCOUNT is required for GitHub access")
        return cls(account, executable, allow_snapshot_write=allow, **kwargs)

    def safe_repo_path(self, repo_spec: str) -> bool:
        """Compatibility helper; request methods enforce the stronger path policy."""
        return repo_spec == f"{OWNER}/{REPO}"

    @staticmethod
    def _validate_segments(path: str) -> list[str]:
        if not path.startswith("/") or "\\" in path or "%" in path:
            raise ValueError("GitHub API path is not allowed")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("GitHub API path is not allowed")
        segments = parsed.path.split("/")[1:]
        if any(not segment or segment in {".", ".."} for segment in segments):
            raise ValueError("GitHub API path is not allowed")
        return segments

    @classmethod
    def _validate_get_path(cls, api_path: str) -> None:
        if not isinstance(api_path, str):
            raise ValueError("GitHub API path is not allowed")
        segments = cls._validate_segments(api_path)
        parsed = urlsplit(api_path)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
        prefix = ["repos", OWNER, REPO]
        if segments == prefix:
            if query:
                raise ValueError("GitHub API query is not allowed")
            return
        rest = segments[len(prefix):] if segments[:len(prefix)] == prefix else []
        if rest and rest[0] == "contents":
            if set(query) - {"ref"} or any(len(values) != 1 or not values[0] for values in query.values()):
                raise ValueError("GitHub API query is not allowed")
            return
        if rest == ["git", "ref", "heads", "main"]:
            if query:
                raise ValueError("GitHub API query is not allowed")
            return
        if len(rest) == 3 and rest[:2] == ["git", "trees"]:
            ref = rest[2]
            if not ref or set(query) - {"recursive"} or query.get("recursive") != ["1"]:
                raise ValueError("GitHub API tree request is not allowed")
            return
        raise ValueError("GitHub API path is not allowed")

    @staticmethod
    def _validate_put_path(api_path: str, body: dict[str, Any]) -> None:
        ComposioGitHub._validate_segments(api_path)
        if urlsplit(api_path).query or api_path != SNAPSHOT_API:
            raise ValueError("GitHub PUT path is not allowed")
        if body.get("branch") != "main":
            raise ValueError("GitHub PUT must target branch main")

    def parse_response(self, output: str, context: str = "GitHub request") -> dict[str, Any] | list[Any]:
        """Parse the real proxy contract: raw GitHub JSON dict or list."""
        if not isinstance(output, str) or not output.strip():
            raise GitHubError(f"{context} returned no JSON")
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            raise GitHubError(f"{context} returned malformed JSON") from None
        if not isinstance(data, (dict, list)):
            raise GitHubError(f"{context} returned malformed JSON")
        if isinstance(data, dict):
            if data.get("successful") is False:
                raise GitHubError(f"{context} failed at the GitHub broker")
            status: int | None = None
            raw_status = data.get("status")
            if isinstance(raw_status, int) and not isinstance(raw_status, bool):
                status = raw_status
            elif isinstance(raw_status, str) and raw_status.isdigit():
                status = int(raw_status)
            if status is not None and status >= 400:
                raise GitHubError(f"{context} failed with GitHub status {status}", status=status)
        return data

    def _run_proxy(self, method: str, api_path: str, json_input: dict[str, Any] | None = None):
        if method == "GET":
            self._validate_get_path(api_path)
        elif method == "PUT":
            if not self.allow_snapshot_write:
                raise GitHubError("GitHub snapshot writes are not enabled")
            if json_input is None:
                raise ValueError("GitHub PUT body is required")
            self._validate_put_path(api_path, json_input)
        else:
            raise ValueError("GitHub method is not allowed")

        command = [
            self.cli_config,
            "proxy",
            "https://api.github.com" + api_path,
            "--toolkit", "github",
            "--account", self.account,
            "-X", method,
        ]
        input_text = ""
        if json_input is not None:
            command.extend(["-H", "Content-Type: application/json", "-d", "-"])
            input_text = json.dumps(json_input, separators=(",", ":"))
        try:
            result = self._runner(
                command,
                input=input_text,
                capture_output=True,
                timeout=self.timeout,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitHubError("Composio GitHub request timed out") from None
        except (OSError, subprocess.SubprocessError):
            raise GitHubError("Composio GitHub request could not start") from None
        except Exception:
            raise GitHubError("Composio GitHub request failed") from None
        if result.returncode != 0:
            raise GitHubError("Composio GitHub request failed")
        return self.parse_response(result.stdout, context=f"GitHub {method}")

    def get(self, api_path: str):
        return self._run_proxy("GET", api_path)

    def get_contents(self, repo_path: str = "", ref: str | None = None):
        if not isinstance(repo_path, str) or repo_path.startswith("/"):
            raise ValueError("repository content path is not allowed")
        api_path = f"{REPO_API}/contents"
        if repo_path:
            api_path += "/" + repo_path
        if ref is not None:
            if not isinstance(ref, str) or not ref or any(char in ref for char in "%?#&"):
                raise ValueError("GitHub ref is not allowed")
            api_path += "?ref=" + ref
        return self.get(api_path)

    def get_contents_list(self, api_path: str) -> list[dict[str, Any]]:
        data = self.get(api_path)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise GitHubError("GitHub contents listing was malformed")
        return data

    def read_file(self, repo_path: str, ref: str | None = None) -> bytes:
        data = self.get_contents(repo_path, ref=ref)
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitHubError("GitHub file response was malformed")
        if data.get("encoding") != "base64":
            raise GitHubError("GitHub file content was unavailable")
        if "truncated" in data and data["truncated"] is not False:
            raise GitHubError("GitHub file content was unavailable")
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise GitHubError("GitHub file response was malformed")
        try:
            decoded = base64.b64decode(encoded.replace("\n", "").replace("\r", ""), validate=True)
        except (binascii.Error, ValueError):
            raise GitHubError("GitHub file response contained invalid base64") from None
        size = data.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or len(decoded) != size
        ):
            raise GitHubError("GitHub file response had invalid size metadata")
        return decoded

    @staticmethod
    def encode_for_write(content: bytes | str) -> str:
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise TypeError("GitHub content must be bytes or text")
        return base64.b64encode(content).decode("ascii")

    def put_contents(
        self,
        api_path: str,
        content: bytes | str,
        branch: str,
        message: str,
        sha: str | None,
    ):
        if branch != "main":
            raise ValueError("GitHub PUT must target branch main")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("GitHub commit message is required")
        body: dict[str, Any] = {
            "message": message,
            "content": self.encode_for_write(content),
            "branch": branch,
        }
        if sha is not None:
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                raise ValueError("GitHub file SHA is invalid")
            body["sha"] = sha
        return self._run_proxy("PUT", api_path, json_input=body)


def read_config(home: str | os.PathLike[str] | None = None) -> tuple[str, str, bool]:
    """Compatibility factory values: executable, account selector, write gate."""
    adapter = ComposioGitHub.from_environment(home=home)
    return adapter.cli_config, adapter.account, adapter.allow_snapshot_write


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PCG Composio GitHub gateway check")
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
