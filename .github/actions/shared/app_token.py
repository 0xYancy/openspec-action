#!/usr/bin/env python3
"""按组织换取只读 token。

用法：python3 app_token.py <org> [<org> ...]

凭据来源（环境变量，二选一或并用，后者优先）：
  APP_ID + APP_PRIVATE_KEY   只读 GitHub App：为每个组织的安装换取临时 token，
                             权限只申请 contents / pull_requests / metadata 的读取
  READ_TOKENS                JSON 对象 {"<org>": "<token>"}：过渡期每个组织的只读细粒度 PAT

输出（stdout）：JSON {"tokens": {"<org>": "<token>"}, "skipped": {"<org>": "<原因>"}}
每个 token 都会以 ::add-mask:: 写入日志遮罩，原因中不包含凭据。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
READ_PERMISSIONS = {"contents": "read", "pull_requests": "read", "metadata": "read"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as key_file:
        key_file.write(private_key)
        key_path = key_file.name
    try:
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.unlink(key_path)
    return f"{header}.{payload}.{b64url(signature)}"


def request(method: str, path: str, token: str, body: dict | None = None) -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "null")


def mask(token: str) -> None:
    print(f"::add-mask::{token}", file=sys.stderr)


def main(orgs: list[str]) -> dict:
    tokens: dict[str, str] = {}
    skipped: dict[str, str] = {}
    app_id = os.environ.get("APP_ID", "").strip()
    private_key = os.environ.get("APP_PRIVATE_KEY", "").strip()
    installations: dict[str, int] = {}
    if app_id and private_key:
        try:
            jwt = app_jwt(app_id, private_key)
            for item in request("GET", "/app/installations?per_page=100", jwt) or []:
                installations[item["account"]["login"].lower()] = item["id"]
            for org in orgs:
                inst = installations.get(org.lower())
                if inst is None:
                    continue
                result = request("POST", f"/app/installations/{inst}/access_tokens", jwt, {"permissions": READ_PERMISSIONS})
                tokens[org] = result["token"]
                mask(tokens[org])
        except (urllib.error.URLError, subprocess.CalledProcessError, KeyError, ValueError) as error:
            reason = f"GitHub App 换取 token 失败：{type(error).__name__}"
            for org in orgs:
                skipped.setdefault(org, reason)
    try:
        pats = json.loads(os.environ.get("READ_TOKENS") or "{}")
    except ValueError:
        pats = {}
    for org, token in (pats or {}).items():
        if org in orgs and token and org not in tokens:
            tokens[org] = token
            mask(token)
    for org in orgs:
        if org in tokens:
            skipped.pop(org, None)
        elif org not in skipped:
            skipped[org] = "未安装只读 GitHub App，也未配置该组织的只读凭据"
    return {"tokens": tokens, "skipped": skipped}


if __name__ == "__main__":
    print(json.dumps(main(sorted(set(sys.argv[1:]))), ensure_ascii=False))
