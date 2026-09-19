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
--  另加 6 个工程字段：knowledge_points_json / filename / source /
--      record_id / model / trace_id，理由见下面的逐列注释。
--
--  与 tutor_records 表的分工：
--      tutor_records       流水账：每次操作都记（包含"没错误"的那些）
--      student_error_logs  错题本：只记真正出错的，字段面向复习与聚合
--  合并成一张表的代价：查「最近 5-10 条错题」要按状态过滤流水账，
--  历史列表会被"无错误"记录稀释，按知识点聚合也要全表扫。所以分开建两张。
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
    -- 关联的流水账记录 id，便于"从错题跳回当时那份代码"
    record_id               INTEGER,
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
    record_id               BIGINT       DEFAULT NULL,
    model                   VARCHAR(128) DEFAULT NULL,
    trace_id                VARCHAR(64)  DEFAULT NULL,
    created_at              DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_student_error_logs_student_created (student_id, created_at),
    KEY ix_student_error_logs_student_kp      (student_id, knowledge_point),
    KEY ix_student_error_logs_error_type      (error_type),
    CONSTRAINT fk_error_log_record
        FOREIGN KEY (record_id) REFERENCES tutor_records (id) ON DELETE SET NULL
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
