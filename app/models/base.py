"""ORM 模型层的公共基座：统一的时间戳工厂与时间戳列类型。

这里有**两件**互相关联的东西，改的时候要一起看：

1. `utc_now()` —— 写入时用的时间戳工厂（统一产生 UTC 时间）；
2. `UTCDateTime` —— 时间戳列类型（保证**读出来也带 UTC 时区**）。

## 为什么需要 UTCDateTime（这是一个真实踩过的坑）

SQLite 的 DATETIME **不保存时区**。`DateTime(timezone=True)` 在 SQLite 上
只是把 UTC 的"墙上时间"存成字符串，读回来是 **naive**（`tzinfo=None`）的。
于是 Pydantic 把它序列化成 `"2026-09-19T14:35:12"`——没有 `Z`、也没有偏移量。
浏览器 `new Date("2026-09-19T14:35:12")` 对"不带时区的日期时间"按**本地时间**
解析（ES2016 之后的规范如此），于是接口给的是 UTC、页面当成北京时间显示，
**在中国正好差 8 小时**。用户看到的就是"右下角的时间和电脑时间不一样"。

修法不是在界面上减 8 小时，而是让接口**把时区说清楚**：
`UTCDateTime` 在读取时给 naive 值补上 UTC，序列化结果就带上了 `Z`，
浏览器自己会换算成当地时区——换任何时区都对，夏令时也对。

## 对历史数据的影响

老数据库里存的本来就是 UTC 墙上时间（只是读出来没带时区），
所以"读取时补 UTC"正好把它们解释对，**不需要迁移数据**。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """统一的时间戳工厂：产生带 UTC 时区的当前时间。

    用 UTC 而不是本地时间：数据库里只存一种时间基准，
    展示时再按浏览器时区换算。混存本地时间会导致
    "换台电脑看同一条记录，时间对不上"。
    """
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """始终以 UTC 存取的时间戳列。

    写入：统一转成 UTC 再落库（SQLite 存不了时区，所以存 naive 的 UTC 墙上时间）。
    读取：把 naive 的值补上 UTC 时区，让上层拿到的永远是 tz-aware 的 UTC。

    这样序列化出来的 ISO 串一定带 `Z`/`+00:00`，
    前端 `new Date(...)` 能正确换算到用户所在时区。

    换到 PostgreSQL 时也不用改：它的 `timestamptz` 本来就会回带时区的值，
    这里再 `astimezone(UTC)` 一次是幂等的。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        # naive 的输入一律**当作 UTC**（老数据就是这样存的），而不是当本地时间——
        # 后者会在每次写入时引入一个时区偏移，越存越偏。
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        # 落库前去掉 tzinfo：SQLite 存不了它，留着只会让 SQLAlchemy 报错或存出带偏移的串
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = ["UTCDateTime", "utc_now"]
