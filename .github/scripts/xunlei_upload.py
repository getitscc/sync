#!/usr/bin/env python3
# =============================================================
# xunlei_upload.py —— GitHub Actions 直传迅雷云盘并生成【永久】分享链接
#
# 用法：
#   python3 xunlei_upload.py <本地文件路径> [网盘文件名]
#   python3 xunlei_upload.py --refresh-share <file_id> [网盘文件名]
#
# 第二种模式用于「仅补链」：文件已在迅雷云盘，只按 file_id 重新生成分享链接，
# 不重新上传，供 R2 仍有效但迅雷链接失效时快速补齐。
#
# 必填环境变量：
#   XUNLEI_REFRESH_TOKEN   迅雷云盘 refresh_token（与网盘账号一致）
# 可选环境变量：
#   XUNLEI_CLIENT_ID       默认使用迅雷网页端 client_id
#   XUNLEI_DEVICE_ID       不填则按 refresh_token 派生稳定设备号
#   XUNLEI_PARENT_ID       上传目录 ID，默认空（根目录）
#
# 成功时 stdout 只输出一行 JSON：
#   {"share_url":"...","extract_code":"","remote_file_id":"...","remote_path":"/xxx.tar.gz"}
# 日志全部写 stderr，便于 Actions 里 `RESULT=$(...)` 取值。
#
# 说明：迅雷分享需要文件已经在云盘里。这里先创建可续传上传任务，
#       用接口返回的临时 S3 凭证直传，再按 file_id 建永久分享。
# =============================================================

import asyncio
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from pan_common import Skipped, run

AUTH_BASE = "https://xluser-ssl.xunlei.com"
API_BASE = "https://api-pan.xunlei.com/drive/v1"
DEFAULT_CLIENT_ID = "Xqp0kJBXWhwaTpB6"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class XunleiError(RuntimeError):
    pass


class Xunlei:
    def __init__(self) -> None:
        self.refresh_token = (os.getenv("XUNLEI_REFRESH_TOKEN") or "").strip()
        if not self.refresh_token:
            raise XunleiError("缺少环境变量 XUNLEI_REFRESH_TOKEN")
        self.client_id = (os.getenv("XUNLEI_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
        self.device_id = (os.getenv("XUNLEI_DEVICE_ID") or "").strip() or hashlib.md5(
            f"{self.refresh_token}xlrefresh".encode()
        ).hexdigest()
        self.parent_id = (os.getenv("XUNLEI_PARENT_ID") or "").strip()
        self._access_token = ""
        self._access_exp = 0.0
        self._captcha: dict[str, tuple[str, float]] = {}
        self.client = httpx.Client(timeout=60, follow_redirects=True)

    # ---------- 鉴权 ----------
    def access_token(self) -> str:
        if self._access_token and self._access_exp > time.time():
            return self._access_token
        resp = self.client.post(
            f"{AUTH_BASE}/v1/auth/token",
            json={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )
        data = resp.json() if resp.status_code < 400 else {}
        # 迅雷有两种返回结构：直接平铺 {access_token,...} 或包一层 {data:{...}}
        info = data.get("data") if isinstance(data.get("data"), dict) else data
        token = info.get("access_token")
        if not token:
            # 不要把 access_token / refresh_token 打进日志
            safe = {k: data.get(k) for k in ("error", "error_description", "message", "code") if data.get(k)}
            raise XunleiError(
                f"refresh_token 换取 access_token 失败（HTTP {resp.status_code}）: {safe or '响应无 access_token'}"
            )
        new_refresh = info.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            log("[XUNLEI] 注意：refresh_token 已轮换；若下次运行鉴权失败，请重新从浏览器获取并更新 GitHub Secret")
            self.refresh_token = new_refresh
        self._access_token = token
        self._access_exp = time.time() + max(60, int(info.get("expires_in", 3600)) - 60)
        return token

    def captcha_token(self, action: str) -> str:
        cached = self._captcha.get(action)
        if cached and cached[1] > time.time():
            return cached[0]
        resp = self.client.post(
            f"{AUTH_BASE}/v1/shield/captcha/init",
            json={
                "client_id": self.client_id,
                "action": action,
                "device_id": self.device_id,
                "meta": {
                    "username": "",
                    "phone_number": "",
                    "email": "",
                    "package_name": "pan.xunlei.com",
                    "client_version": "1.45.0",
                    "captcha_sign": "1.fe2108ad808a74c9ac0243309242726c",
                    "timestamp": "1645241033384",
                    "user_id": "0",
                },
            },
        )
        data = resp.json() if resp.status_code < 400 else {}
        info = data.get("data") or {}
        token = info.get("captcha_token")
        if not token:
            raise XunleiError(f"领取 captcha_token 失败: {data.get('error') or data}")
        self._captcha[action] = (token, time.time() + max(60, int(info.get("expires_in", 300)) - 10))
        return token

    def request(
        self,
        method: str,
        path: str,
        action: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": "https://pan.xunlei.com",
            "Referer": "https://pan.xunlei.com/",
            "x-client-id": self.client_id,
            "x-device-id": self.device_id,
            "Authorization": f"Bearer {self.access_token()}",
            "x-captcha-token": self.captcha_token(action),
        }
        resp = self.client.request(
            method, f"{API_BASE}{path}", params=params, json=body, headers=headers
        )
        if resp.status_code >= 400:
            raise XunleiError(f"迅雷接口 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("error_code"):
            raise XunleiError(
                f"迅雷接口错误 {data.get('error_code')}: "
                f"{data.get('error_description') or data.get('error') or ''}"
            )
        return data

    # ---------- 上传 ----------
    def create_upload(self, name: str, size: int, gcid: str) -> tuple[str, dict]:
        data = self.request(
            "POST",
            "/files",
            "POST:/drive/v1/files",
            body={
                "kind": "drive#file",
                "parent_id": self.parent_id,
                "name": name,
                "size": size,
                "hash": gcid,
                "upload_type": "UPLOAD_TYPE_RESUMABLE",
                "space": "",
            },
        )
        file_id = (data.get("file") or {}).get("id") or data.get("id")
        params = ((data.get("resumable") or {}).get("params")) or {}
        if not file_id:
            raise XunleiError(f"创建上传任务未返回 fileId: {json.dumps(data)[:300]}")
        if not params.get("endpoint") or not params.get("key"):
            # 命中秒传 / 云端已存在同 hash 文件时不会返回可续传参数，直接去出链
            log("[XL] 未返回可续传参数，按秒传处理，跳过上传")
            return str(file_id), {}
        return str(file_id), params

    def put_s3(self, file_path: str, params: dict) -> None:
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config
        except ImportError as e:  # pragma: no cover
            raise XunleiError("缺少 boto3，请在 workflow 中 pip install boto3") from e

        bucket = params["bucket"]
        key = params["key"]
        endpoint = normalize_endpoint(params["endpoint"], bucket)
        log(f"[S3] endpoint={endpoint} bucket={bucket} key={key}")

        last_err: Exception | None = None
        for style in ("virtual", "path"):
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=params["access_key_id"],
                aws_secret_access_key=params["access_key_secret"],
                aws_session_token=params.get("security_token") or None,
                region_name="xunlei",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": style},
                    retries={"max_attempts": 5, "mode": "standard"},
                    connect_timeout=30,
                    read_timeout=300,
                ),
            )
            try:
                log(f"[S3] 开始上传（{style} addressing）…")
                client.upload_file(
                    file_path,
                    bucket,
                    key,
                    Config=TransferConfig(
                        multipart_threshold=16 * 1024 * 1024,
                        multipart_chunksize=16 * 1024 * 1024,
                        max_concurrency=4,
                    ),
                )
                log("[S3] 上传完成")
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"[S3] {style} 方式失败: {e}")
        raise XunleiError(f"S3 上传失败: {last_err}")

    def create_share(self, file_id: str, title: str) -> dict:
        last_err: Exception | None = None
        for attempt in range(1, 7):
            try:
                return self.request(
                    "POST",
                    "/share",
                    "POST:/drive/v1/share",
                    body={
                        "file_ids": [file_id],
                        "share_to": "copy",
                        "params": {"subscribe_push": "false", "WithPassCodeInLink": "true"},
                        "title": title,
                        "restore_limit": "-1",
                        # 0 = 永久分享
                        "expiration_days": "0",
                    },
                )
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"[SHARE] 第 {attempt}/6 次失败: {e}")
                time.sleep(5)
        raise XunleiError(f"创建分享失败: {last_err}")


def normalize_endpoint(raw: str, bucket: str) -> str:
    """把迅雷返回的 endpoint 规范成 boto3 可用的 bucket 无关 endpoint。"""
    ep = (raw or "").strip()
    if not ep:
        raise XunleiError("上传参数缺少 endpoint")
    if not ep.startswith("http"):
        ep = "https://" + ep
    parts = urlsplit(ep)
    host = parts.netloc
    if host.startswith(f"{bucket}."):
        host = host[len(bucket) + 1 :]
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def compute_gcid(path: str, size: int) -> str:
    """迅雷 GCID：按块 SHA1 后再整体 SHA1。"""
    psize = 0x40000
    while size / psize > 0x200 and psize < 0x200000:
        psize <<= 1
    outer = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            block = f.read(psize)
            if not block:
                break
            outer.update(hashlib.sha1(block).digest())
    return outer.hexdigest()


def _resolve_folder(xl: "Xunlei", folder: str) -> str:
    """按 /images/{registry}/{image} 路径逐级查找或创建迅雷目录，返回 folder id。"""
    parent_id = ""
    for part in [p for p in folder.strip("/").split("/") if p]:
        data = xl.request(
            "GET",
            "/files",
            "GET:/drive/v1/files",
            params={"parent_id": parent_id, "limit": 200, "with_audit": "true"},
        )
        files = data.get("files") or []
        hit = next(
            (
                f
                for f in files
                if f.get("name") == part
                and f.get("kind") in ("drive#folder", "drive#dir")
            ),
            None,
        )
        if hit:
            parent_id = str(hit.get("id"))
            continue
        created = xl.request(
            "POST",
            "/files",
            "POST:/drive/v1/files",
            body={"kind": "drive#folder", "parent_id": parent_id, "name": part},
        )
        fid = (created.get("file") or {}).get("id") or created.get("id")
        if not fid:
            raise XunleiError(f"迅雷创建目录失败: {part}")
        parent_id = str(fid)
    return parent_id


async def upload_xunlei(args) -> None:
    token = (os.getenv("XUNLEI_REFRESH_TOKEN") or "").strip()
    if not token:
        raise Skipped("未配置 XUNLEI_REFRESH_TOKEN")

    def work() -> None:
        xl = Xunlei()
        # 新布局必须逐级解析 /images/{registry}/{image}，不能使用固定 parent_id
        xl.parent_id = _resolve_folder(xl, args.dir)
        size = os.path.getsize(args.file)
        gcid = compute_gcid(args.file, size)
        file_id, params = xl.create_upload(args.name, size, gcid)
        if params:
            xl.put_s3(args.file, params)
        log(f"[XUNLEI] 上传完成: {args.name} (file_id={file_id})")

    await asyncio.to_thread(work)


if __name__ == "__main__":
    run("xunlei", upload_xunlei)
