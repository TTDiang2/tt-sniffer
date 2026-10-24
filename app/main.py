"""入口：python -m app.main（或双击 run.bat）。"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

import uvicorn

from .config import CONFIG, data_dir
from .pipeline import Pipeline
from .server import create_app
from .store import Store


def setup_logging():
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    try:
        handler = RotatingFileHandler(
            data_dir() / "server.log", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8")
    except OSError as e:
        logging.getLogger("main").warning("无法创建轮转日志: %s", e)
    else:
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)


async def amain():
    setup_logging()
    store = Store()
    pipeline = Pipeline(store)
    app = create_app(store, pipeline)

    srv_cfg = CONFIG["server"]
    server = uvicorn.Server(uvicorn.Config(
        app, host=srv_cfg["host"], port=srv_cfg["port"], log_level="warning"))
    # 2026-10-16 防僵尸：端口被占（bind 失败）等场景下 server 退出而旧实现
    # 的 gather 会让 pipeline 无头续跑——现场曾积 3 个双采集僵尸进程写同一
    # 库 12 天。任一协程先退即整体退出（异常向上传播，进程可被自启脚本重拉）。
    server_task = asyncio.create_task(server.serve())
    pipe_task = asyncio.create_task(pipeline.run())
    done, pending = await asyncio.wait(
        {server_task, pipe_task}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        t.result()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
