"""Optional publishing of a finished report to a GitHub repository.

Kept from the original tool because the shareable link is the point: a report
in `reports/` renders through htmlpreview immediately, without waiting for a
Pages build.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .net import FetchError, fetch, fetch_json

GITHUB_API = "https://api.github.com"


def parse_repo(value: str) -> tuple[str, str]:
    """Accept `owner/repo`, a full URL, or an SSH remote."""
    value = value.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/\s]+)", value)
    if match:
        owner, repo = match.group(1), match.group(2)
    elif value.count("/") == 1:
        owner, repo = value.split("/", 1)
    else:
        raise ValueError(f"Cannot read a repository from {value!r}; use owner/repo")
    return owner, repo.removesuffix(".git")


def _existing_sha(owner: str, repo: str, path: str, token: str, branch: str) -> str | None:
    """GitHub requires the blob sha to overwrite an existing file."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    try:
        data = fetch_json(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
    except FetchError:
        return None
    return data.get("sha") if isinstance(data, dict) else None


def upload_report(
    report_path: Path,
    *,
    token: str,
    repo: str,
    branch: str = "main",
    directory: str = "reports",
) -> dict[str, str]:
    """Upload one report file. Returns a dict of shareable links."""
    owner, repo_name = parse_repo(repo)
    filename = report_path.name
    target = f"{directory.strip('/')}/{filename}" if directory else filename

    content = base64.b64encode(report_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {
        "message": f"Add PageSpeed report: {filename}",
        "content": content,
        "branch": branch,
    }
    sha = _existing_sha(owner, repo_name, target, token, branch)
    if sha:
        body["sha"] = sha

    url = f"{GITHUB_API}/repos/{owner}/{repo_name}/contents/{target}"
    fetch(
        url,
        method="PUT",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        timeout=120,
    )

    raw = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{target}"
    return {
        "raw": raw,
        "preview": f"https://htmlpreview.github.io/?{raw}",
        "pages": f"https://{owner}.github.io/{repo_name}/{target}",
        "repo": f"https://github.com/{owner}/{repo_name}/blob/{branch}/{target}",
    }
