#!/usr/bin/env python3
"""百度网盘上传（开放平台 API，需要 BAIDU_ACCESS_TOKEN）。"""

import asyncio
import hashlib
import json
import os

import httpx

from pan_common import Skipped, log, run

BLOCK_SIZE = 4 * 1024 * 1024
BAIDU_API = "https://pan.baidu.com/rest/2.0/xpan/file"
BAIDU_PCS = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    params: dict,
    data: dict,
    allow_errno: tuple[int, ...] = (),
) -> dict:
    resp = await client.request(method, url, params=params, data=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"百度接口 HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    errno = payload.get("errno", 0)
    if errno not in (0, *allow_errno):
        raise RuntimeError(
            f"百度接口 errno={errno}: "
            f"{payload.get('errmsg') or payload.get('request_id') or ''}"
        )
    return payload


def _md5_blocks(path: str) -> list[str]:
    result: list[str] = []
    with open(path, "rb") as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            result.append(hashlib.md5(block).hexdigest())
    return result


async def upload_baidu(args) -> None:
    token = (os.getenv("BAIDU_ACCESS_TOKEN") or "").strip()
    if not token:
        raise Skipped("未配置 BAIDU_ACCESS_TOKEN")

    size = os.path.getsize(args.file)
    folder = "/" + args.dir.strip("/") if args.dir.strip("/") else ""
    target = f"{folder}/{args.name}"
    headers = {"User-Agent": "pan.baidu.com"}

    async with httpx.AsyncClient(timeout=3600, headers=headers) as client:
        # 逐级创建目录；errno=-8 表示目录已存在
        current = ""
        for part in [p for p in folder.strip("/").split("/") if p]:
            current += f"/{part}"
            await _request(
                client,
                "POST",
                BAIDU_API,
                {"method": "create", "access_token": token},
                {"path": current, "isdir": "1", "size": "0", "block_list": "[]"},
                allow_errno=(-8,),
            )

        block_list = await asyncio.to_thread(_md5_blocks, args.file)
        pre = await _request(
            client,
            "POST",
            BAIDU_API,
            {"method": "precreate", "access_token": token},
            {
                "path": target,
                "size": str(size),
                "isdir": "0",
                "autoinit": "1",
                "block_list": json.dumps(block_list),
            },
        )
        if pre.get("return_type") == 2:
            log(f"[BAIDU] 秒传命中: {args.name}")
            return

        upload_id = pre.get("uploadid")
        if not upload_id:
            raise RuntimeError(f"百度预上传未返回 uploadid: {pre}")

        sem = asyncio.Semaphore(4)

        async def upload_part(seq: int, chunk: bytes) -> None:
            async with sem:
                resp = await client.post(
                    BAIDU_PCS,
                    params={
                        "method": "upload",
                        "access_token": token,
                        "type": "tmpfile",
                        "path": target,
                        "uploadid": upload_id,
                        "partseq": str(seq),
                    },
                    files={"file": ("part", chunk, "application/octet-stream")},
                )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"百度分片 {seq} HTTP {resp.status_code}: {resp.text[:160]}"
                    )
                payload = resp.json()
                if payload.get("errno"):
                    raise RuntimeError(
                        f"百度分片 {seq} errno={payload.get('errno')}"
                    )

        tasks: set[asyncio.Task] = set()
        with open(args.file, "rb") as f:
            seq = 0
            while True:
                chunk = f.read(BLOCK_SIZE)
                if not chunk:
                    break
                tasks.add(asyncio.create_task(upload_part(seq, chunk)))
                seq += 1
                if len(tasks) >= 4:
                    done, tasks = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
        if tasks:
            await asyncio.gather(*tasks)

        await _request(
            client,
            "POST",
            BAIDU_API,
            {"method": "create", "access_token": token},
            {
                "path": target,
                "size": str(size),
                "isdir": "0",
                "uploadid": upload_id,
                "block_list": json.dumps(block_list),
            },
        )
        log(f"[BAIDU] 上传完成: {args.name}")


if __name__ == "__main__":
    run("baidu", upload_baidu)
