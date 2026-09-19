"""ORM 模型层的公共基座。

目前只有一件事：统一的时间戳工厂 `utc_now`。

为什么它要单独一个模块：这个函数原先住在 `app/models/repository.py` 里
（因为 Repository 模型是最早写的），结果**每一个**业务模型都要
`from app.models.repository import utc_now`——包括跟"仓库解析"毫无关系的
错题本、注释记录、文件台账。删掉开源贡献端时这层耦合立刻变成拦路虎：
只要还有一个模型引用它，`repository.py` 就删不掉。

时间戳工厂和"某个具体业务表"没有关系，放在公共基座里才是它的位置。
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """统一的 UTC 时间戳工厂。

    用 UTC 而不是本地时间：数据库里只存一种时间基准，
    展示时再按浏览器时区换算（前端 `new Date(iso).toLocaleString('zh-CN')`）。
    混存本地时间会导致"换台电脑看历史记录，时间对不上"。
    """
    return datetime.now(UTC)


__all__ = ["utc_now"]
