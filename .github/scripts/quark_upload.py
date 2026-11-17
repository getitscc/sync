#!/usr/bin/env python3
"""夸克网盘上传：通过临时 AList（Quark 驱动）完成。

夸克官方上传接口已改为 task_id + file/update/hash + OSS 分片签名，
直连实现容易随接口变动失效；这里复用 AList 的 Quark 驱动：
启动容器 → 用 QUARK_COOKIE 挂载 → /api/fs/put 上传 → 删除容器。
"""

import asyncio
import json
import os
import secrets
import subprocess
import time

import httpx

from pan_common import Skipped, log, run

ALIST_IMAGE = os.getenv("ALIST_IMAGE", "xhofe/alist:latest")
CONTAINER = "jishi-alist-quark"
BASE = "http://127.0.0.1:5244"
MOUNT = "/pan"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _wait_alist(timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{BASE}/api/public/settings", timeout=3)
            if resp.status_code < 500:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise RuntimeError("AList 启动超时")


def _start_alist() -> None:
    _docker("rm", "-f", CONTAINER, check=False)
    try:
        _docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            "127.0.0.1:5244:5244",
            ALIST_IMAGE,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"AList 启动失败: {(e.stderr or '')[:300]}") from e
    _wait_alist()


def _login() -> str:
    password = secrets.token_urlsafe(12)
    proc = _docker(
        "exec", CONTAINER, "./alist", "admin", "set", password, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"AList 设置密码失败: {(proc.stderr or '')[:300]}")

    resp = httpx.post(
        f"{BASE}/api/auth/login",
        json={"username": "admin", "password": password},
        timeout=20,
    )
    token = (resp.json().get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"AList 登录失败: {resp.text[:300]}")
    return token


def _mount_quark(token: str, cookie: str) -> None:
    addition = {"cookie": cookie, "root_folder_id": "0"}
    body = {
        "mount_path": MOUNT,
        "order": 0,
        "remark": "jishi-temp",
        "driver": "Quark",
        "addition": json.dumps(addition, ensure_ascii=False),
    }
    resp = httpx.post(
        f"{BASE}/api/admin/storage/create",
        headers={"Authorization": token},
        json=body,
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(
            f"AList 挂载夸克失败: {data.get('message') or resp.text[:300]}"
        )
    # 给驱动一点初始化时间
    time.sleep(3)


def _upload(token: str, path: str, name: str, folder: str) -> None:
    dest_dir = f"{MOUNT}/{folder.strip('/')}"
    httpx.post(
        f"{BASE}/api/fs/mkdir",
        headers={"Authorization": token},
        json={"path": dest_dir},
        timeout=30,
    )

    last = ""
    for _ in range(3):
        try:
            with open(path, "rb") as f:
                resp = httpx.put(
                    f"{BASE}/api/fs/put",
                    headers={
                        "Authorization": token,
                        "File-Path": f"{dest_dir}/{name}",
                        "As-Task": "false",
                    },
                    content=f,
                    timeout=httpx.Timeout(3600.0, connect=30.0),
                )
            data = resp.json()
            if data.get("code") == 200:
                return
            last = data.get("message") or resp.text[:300]
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(5)
    raise RuntimeError(f"AList 上传失败: {last}")


async def upload_quark(args) -> None:
    cookie = (os.getenv("QUARK_COOKIE") or "").strip()
    if not cookie:
        raise Skipped("未配置 QUARK_COOKIE")

    def work() -> None:
        _start_alist()
        try:
            token = _login()
            _mount_quark(token, cookie)
            log(f"[QUARK] 上传: {args.name} -> {args.dir}")
            _upload(token, args.file, args.name, args.dir)
            log(f"[QUARK] 上传完成: {args.name}")
        finally:
            _docker("rm", "-f", CONTAINER, check=False)

    await asyncio.to_thread(work)


if __name__ == "__main__":
    run("quark", upload_quark)
