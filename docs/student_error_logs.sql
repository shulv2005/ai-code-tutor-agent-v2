-- ============================================================================
--  错题本表 student_error_logs —— 建表 SQL
-- ============================================================================
--
--  用途：每次 AI 诊断判定「代码有错」时，把这次错误按**知识点**归档到学生名下，
--        供下一次诊断时回查历史、聚合薄弱点，并据此出「举一反三」的新题目。
--
--  这份文件是**手工建表 / 换库时用的参考 DDL**。
--  本项目的日常运行**不需要**手工执行它：SQLAlchemy 会在服务启动时按
--  `app/models/error_log.py` 里的模型定义自动建表（见 app/core/database.py 的 init_db）。
--  两者必须保持一致——改了一边就要同步改另一边，所以把 DDL 和模型放在一起维护。
--
--  字段与需求文档的对应关系（需求点名 8 个字段，这里全部具备）：
--      id / student_id / code_snippet / language /
--      error_type / knowledge_point / ai_feedback / created_at
--  另加 5 个工程字段：knowledge_points_json / filename / source /
--      model / trace_id，理由见下面的逐列注释。
--
--  为什么这个错题本**自带全部信息**、不依赖任何别的表：
--  它存了当时的代码片段（code_snippet）、知识点、错误类型与 AI 反馈，
--  因此查回来就是一个完整的复习条目。原先还有一个 record_id 指向
--  tutor_records 流水账（"这次错误是哪一次提交产生的"），
--  但那张表的唯一读取方是页面左下角的「历史记录」列表——
--  该功能已删除，流水账与这个外键一并移除。
-- ============================================================================


-- ============================================================================
--  一、SQLite（本项目开发/教室环境的默认库，零配置、单文件）
-- ============================================================================
--  为什么本项目默认选 SQLite 而不是 MySQL：
--    1. 教室场景没有运维：双击 start.bat 就能跑，不需要学生装数据库服务；
--    2. 数据量小：一个班的错题记录量级在万行以内，SQLite 绰绰有余；
--    3. 迁移零成本：SQLAlchemy 2.0 抽象掉了方言差异，将来换成 MySQL/PostgreSQL
--       只需要改 .env 里的 DATABASE__URL_OVERRIDE，业务代码一行都不用动。
-- ============================================================================

CREATE TABLE IF NOT EXISTS student_error_logs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ---------- 谁错的 ----------
    -- 用字符串而不是外键：本项目学生端不做登录，学号由页面输入框提供。
    -- 将来接入统一认证时，把这里换成指向用户表的整数外键即可
    -- （SQLite 里 TEXT 列能直接放 UUID / 学号两种形态，迁移期不用改类型）。
    student_id              VARCHAR(64)  NOT NULL,

    -- ---------- 错在什么代码上 ----------
    code_snippet            TEXT         NOT NULL,
    language                VARCHAR(32)  NOT NULL,
    -- 原始文件名。可为空：粘贴进来的代码没有文件名。
    filename                VARCHAR(255),

    -- ---------- 错在哪 ----------
    -- 语法错误 / 逻辑错误 / 运行时错误 / 规范问题 …（由模型判定）
    error_type              VARCHAR(32)  NOT NULL,
    -- 主知识点：取模型返回 knowledge_points 的第一条。
    -- 单独存一列是为了「按知识点聚合统计」——
    -- 在 JSON 列上做 GROUP BY 需要数据库的 JSON 函数，SQLite / MySQL 写法还不同，
    -- 冗余这一列之后，聚合就是一条最普通的 GROUP BY，两个库都能跑。
    knowledge_point         VARCHAR(255) NOT NULL,
    -- 完整知识点列表的 JSON 数组字符串，例如 ["循环边界","数组下标"]。
    -- 解析失败时前端退回只显示 knowledge_point，不影响页面。
    knowledge_points_json   TEXT,

    -- ---------- AI 说了什么 ----------
    -- 本次诊断给出的具体分析（对应提示词里的 analysis.message）
    ai_feedback             TEXT         NOT NULL,

    -- ---------- 工程字段 ----------
    -- 这条错题是哪次操作产生的：diagnose（新接口）/ check（旧的 AI 检测）
    source                  VARCHAR(16)  NOT NULL DEFAULT 'diagnose',
    model                   VARCHAR(128),
    trace_id                VARCHAR(64),

    created_at              DATETIME     NOT NULL
);

-- 主力查询：「某学生最近 N 条错题」（按学生过滤 + 时间倒序）
CREATE INDEX IF NOT EXISTS ix_student_error_logs_student_created
    ON student_error_logs (student_id, created_at);

-- 次要查询：「某学生最常错的知识点」（按学生 + 知识点分组计数）
CREATE INDEX IF NOT EXISTS ix_student_error_logs_student_kp
    ON student_error_logs (student_id, knowledge_point);

-- 单列索引：SQLAlchemy 的 index=True 会额外建这三个。
-- 上面的两条组合索引已经能覆盖绝大多数查询，但按 language / error_type
-- 单独筛选（例如"看看所有语法错误"）时用得上。
CREATE INDEX IF NOT EXISTS ix_student_error_logs_student_id ON student_error_logs (student_id);
CREATE INDEX IF NOT EXISTS ix_student_error_logs_language   ON student_error_logs (language);
CREATE INDEX IF NOT EXISTS ix_student_error_logs_error_type ON student_error_logs (error_type);
CREATE INDEX IF NOT EXISTS ix_student_error_logs_kp         ON student_error_logs (knowledge_point);
CREATE INDEX IF NOT EXISTS ix_student_error_logs_created_at ON student_error_logs (created_at);


-- ============================================================================
--  二、MySQL 8.0（一个班 / 一个学校集中部署时用这个）
-- ============================================================================
--  切换方式：.env 里设 DATABASE__URL_OVERRIDE=mysql+asyncmy://user:pwd@host:3306/dbname
--  （注意：项目依赖里目前装的是 aiosqlite，换 MySQL 还得装对应驱动）
--
--  与 SQLite 版的差异只有三处，都是方言要求：
--    1. AUTO_INCREMENT 而不是 AUTOINCREMENT；
--    2. 明确的 InnoDB + utf8mb4（要存中文与 emoji，utf8 三字节不够）；
--    3. DATETIME(6) 保留微秒——诊断是"秒级并发"的场景，
--       同一秒内可能写入多条（学生连点、批量导入），精确到秒会让排序不稳定。
--
--  关于 student_id 的长度与索引：utf8mb4 下 VARCHAR(64) 的索引占 256 字节，
--  远低于 InnoDB 单列索引 3072 字节的上限，不需要前缀索引。
-- ============================================================================

/*
CREATE TABLE IF NOT EXISTS student_error_logs (
    id                      BIGINT       NOT NULL AUTO_INCREMENT,
    student_id              VARCHAR(64)  NOT NULL,
    code_snippet            MEDIUMTEXT   NOT NULL,
    language                VARCHAR(32)  NOT NULL,
    filename                VARCHAR(255) DEFAULT NULL,
    error_type              VARCHAR(32)  NOT NULL,
    knowledge_point         VARCHAR(255) NOT NULL,
    knowledge_points_json   TEXT,
    ai_feedback             MEDIUMTEXT   NOT NULL,
    source                  VARCHAR(16)  NOT NULL DEFAULT 'diagnose',
    model                   VARCHAR(128) DEFAULT NULL,
    trace_id                VARCHAR(64)  DEFAULT NULL,
    created_at              DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_student_error_logs_student_created (student_id, created_at),
    KEY ix_student_error_logs_student_kp      (student_id, knowledge_point),
    KEY ix_student_error_logs_error_type      (error_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
*/


-- ============================================================================
--  三、常用查询（前端两个面板就是靠这两条撑起来的）
-- ============================================================================

-- 1) 某学生最近的错题——诊断前捞出来喂给大模型的"历史错题记录"
--    SELECT * FROM student_error_logs
--     WHERE student_id = ?
--     ORDER BY created_at DESC, id DESC
--     LIMIT 8;

-- 2) 某学生的易错知识点 Top 5——下半屏那排"条形图"
--    SELECT knowledge_point, COUNT(*) AS count
--      FROM student_error_logs
--     WHERE student_id = ?
--     GROUP BY knowledge_point
--     ORDER BY count DESC
--     LIMIT 5;

-- 3) 某学生的错题总数——下半屏右上角的"N 条"
--    SELECT COUNT(*) FROM student_error_logs WHERE student_id = ?;

-- 4) 删除一条错题（**必须带 student_id**，否则任何学生都能凭 id 删别人的错题）
--    DELETE FROM student_error_logs WHERE id = ? AND student_id = ?;


-- ============================================================================
--  四、使用时长表 usage_sessions —— 建表 SQL
-- ============================================================================
--
--  用途：记录"这个学生用了多久"。每次双击 start.bat 启动服务时写一条，
--        前端页面每隔 60 秒发一次心跳往前推有效时长；老师看的学习报告
--        里"累计有效使用时长"就是这个表的汇总。
--
--  字段解释（与 app/models/usage.py 一一对应）：
--      started_at       服务启动时间 = 学生双击 start.bat 的时间
--      last_seen_at     最后一次收到前端心跳的时间
--      active_seconds   ★ 累计有效秒数：每次心跳把「距上次心跳的间隔」截断后
--                       累加进来。单段最多计 300 秒（MAX_HEARTBEAT_GAP_SECONDS）
--      ended_at         正常关闭时写入；进程被强杀 / 断电时为 NULL
--      pid / app_version  排查用：哪一次是哪个进程、哪个版本产生的
--
--  为什么时长要"累加"而不是每次现算 (last_seen_at - started_at)：
--  浏览器标签页会被挂起（省电模式、合上笔记本盖），恢复后第一个心跳的间隔
--  可能是几小时。现算会把这几个小时算成学习时长，而老师正是拿这个数字判断
--  学生有没有真的在练。累加 + 单段截断把这类误差封顶在 5 分钟——
--  "宁可少算，不能多算"。
--
--  只有一张索引：主力查询是「某学生最近 N 次使用」（按学生过滤 + 时间倒序）。
-- ============================================================================

CREATE TABLE IF NOT EXISTS usage_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id      VARCHAR(64) NOT NULL,
    started_at      DATETIME    NOT NULL,
    last_seen_at    DATETIME    NOT NULL,
    active_seconds  INTEGER     NOT NULL DEFAULT 0,
    ended_at        DATETIME    DEFAULT NULL,
    pid             INTEGER     DEFAULT NULL,
    app_version     VARCHAR(32) DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS ix_usage_sessions_student_started
    ON usage_sessions (student_id, started_at);

-- MySQL 版（与上面等价，只是方言不同；注意 active_seconds 不是保留字，可直接用）
/*
CREATE TABLE IF NOT EXISTS usage_sessions (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    student_id      VARCHAR(64) NOT NULL,
    started_at      DATETIME(6) NOT NULL,
    last_seen_at    DATETIME(6) NOT NULL,
    active_seconds  INT         NOT NULL DEFAULT 0,
    ended_at        DATETIME(6) DEFAULT NULL,
    pid             INT         DEFAULT NULL,
    app_version     VARCHAR(32) DEFAULT NULL,
    PRIMARY KEY (id),
    KEY ix_usage_sessions_student_started (student_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
*/

-- 5) 某学生区间内的总有效使用时长（学习报告「累计有效使用时长」）
--    SELECT COALESCE(SUM(active_seconds), 0) FROM usage_sessions
--     WHERE student_id = ? AND started_at >= ?;

-- 6) 某学生用过哪几天（学习报告「使用天数」；按本地日期去重，跨零点算两天）
--    SELECT DATE(started_at, 'localtime') AS day, SUM(active_seconds)
--      FROM usage_sessions
--     WHERE student_id = ?
--     GROUP BY day
--     ORDER BY day DESC;
