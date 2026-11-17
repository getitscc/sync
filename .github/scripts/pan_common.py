#!/usr/bin/env python3
"""网盘上传脚本公共工具：参数解析、结果文件、进度回调。

每个网盘脚本独立运行，约定：
- 成功：退出码 0，结果文件 {"provider": ..., "status": "success", "error": null}
- 缺凭证：status = skipped，error 写明缺少的变量名
- 失败/超时：status = failed，error 写明原因
无论成功失败都退出码 0，由 workflow 汇总 archive_done 决定任务终态。
"""

import argparse
import asyncio
import json
import sys

import httpx


class Skipped(Exception):
    """缺少凭证等可跳过原因。"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True, help="本地待上传文件")
    p.add_argument("--name", required=True, help="网盘内文件名")
    p.add_argument("--dir", required=True, help="网盘目标目录，如 /images/Docker Hub/nginx")
    p.add_argument("--version", default="", help="镜像版本/tag，用于 latest 等移动标签的覆盖处理")
    p.add_argument("--output", required=True, help="结果 JSON 输出路径")
    p.add_argument("--callback-url", default="", help="Worker 回调地址")
    p.add_argument("--callback-secret", default="")
    p.add_argument("--task-id", default="")
    p.add_argument("--timeout", type=int, default=1800, help="单网盘超时（秒）")
    return p


async def _post_callback(
    url: str, secret: str, task_id: str, result: dict
) -> None:
    payload = {
        "taskId": task_id,
        "status": "pan_result",
        "panResult": result,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            url, json=payload, headers={"X-Callback-Secret": secret}
        )
        resp.raise_for_status()


def emit(args: argparse.Namespace, provider: str, status: str, error) -> None:
    result = {"provider": provider, "status": status, "error": error}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False), flush=True)

    if args.callback_url and args.task_id:
        try:
            asyncio.run(
                _post_callback(
                    args.callback_url, args.callback_secret, args.task_id, result
                )
            )
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] pan_result 回调失败: {e}")


def run(provider: str, upload) -> None:
    """执行 upload(args)，把结果写入 output 并回调；始终退出码 0。"""
    args = build_parser().parse_args()

    async def _run() -> None:
        await asyncio.wait_for(upload(args), timeout=args.timeout)

    try:
        asyncio.run(_run())
        emit(args, provider, "success", None)
    except Skipped as e:
        emit(args, provider, "skipped", str(e))
    except asyncio.TimeoutError:
        emit(args, provider, "failed", f"上传超时（{args.timeout} 秒）")
    except Exception as e:  # noqa: BLE001
        log(f"[{provider.upper()}] 失败: {e}")
        emit(args, provider, "failed", str(e)[:500])
