#!/usr/bin/env python3
"""阿里云盘上传（aligo，需要 ALIYUN_REFRESH_TOKEN）。"""

import asyncio
import logging
import os

from pan_common import Skipped, log, run


async def upload_aliyun(args) -> None:
    token = (os.getenv("ALIYUN_REFRESH_TOKEN") or "").strip()
    if not token:
        raise Skipped("未配置 ALIYUN_REFRESH_TOKEN")

    def work() -> None:
        from aligo import Aligo

        try:
            ali = Aligo(refresh_token=token, re_login=False, level=logging.WARNING)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"阿里云盘 refresh_token 无效（当前长度 {len(token)}，"
                f"应只填 refresh_token 字段值）: {e}"
            ) from e

        # 逐级创建 /images/{registry}/{image}
        parent = "root"
        target = None
        current_path = ""
        for part in [p for p in args.dir.strip("/").split("/") if p]:
            current_path = f"{current_path}/{part}".strip("/")
            target = ali.get_file_by_path(current_path)
            if target is None:
                target = ali.create_folder(name=part, parent_file_id=parent)
            parent = target.file_id
        if target is None:
            raise RuntimeError(f"阿里云盘目录创建失败: {args.dir}")

        ali.upload_file(args.file, parent_file_id=target.file_id, name=args.name)

    await asyncio.to_thread(work)
    log(f"[ALIYUN] 上传完成: {args.name}")


if __name__ == "__main__":
    run("aliyun", upload_aliyun)
