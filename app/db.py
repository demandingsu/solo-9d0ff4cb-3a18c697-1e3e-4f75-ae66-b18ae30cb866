"""SQLite 数据访问层。

两个应用进程共享同一个 SQLite 数据库文件：
- WAL 模式 + busy_timeout，读写不互相阻塞，写者按 BEGIN IMMEDIATE 串行化；
- 所有多步写入都包在 write_tx() 的 BEGIN IMMEDIATE 事务里，
  因此跨进程的并发领取 / 提交 / 改写 / 撤回要么全部落库，要么整体回滚；
- 领取期限以绝对时间戳落库，进程重启后仍然有效。

农户许可撤回相关表：
- banned_farmers         被持久封禁的农户号（撤回即写入，重启不丢）
- banned_farmer_records  撤回时该农户名下的记录号快照（禁止同一记录号改绑他人重传）
- banned_descriptions    撤回时田间描述的哈希快照（禁止原描述换记录号/换农户号重传）

旧库升级：
- 早期 records_current 没有 farmer_no 列，init_db() 自动 ALTER 补列；
- 补列后旧行 farmer_no 为 NULL，应用层一律展示为「来源未知」，
  旧记录与其标签原样保留，任何上传都不得把它们归到某个农户名下。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "annotate.db"
DB_PATH = os.environ.get("DB_PATH", str(_DEFAULT_DB))

SCHEMA = """
CREATE TABLE IF NOT EXISTS records_current (
    record_no   TEXT PRIMARY KEY,      -- 记录号
    version     INTEGER NOT NULL,      -- 描述每改写一次 +1
    description TEXT NOT NULL,
    farmer_no   TEXT,                  -- 农户号；NULL 表示旧库遗留的「来源未知」记录
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    token      TEXT PRIMARY KEY,       -- 领取凭证，提交时校验
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,       -- 领取时的记录版本
    annotator  TEXT NOT NULL,
    expires_at REAL NOT NULL,          -- 绝对过期时间（epoch 秒）
    created_at REAL NOT NULL,
    UNIQUE (record_no, version, annotator)   -- 同一标注员对同一版本只有一条领取
);

CREATE TABLE IF NOT EXISTS labels (
    record_no  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    annotator  TEXT NOT NULL,
    label      TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
    created_at REAL NOT NULL,
    PRIMARY KEY (record_no, version, annotator)  -- 同人同版本至多一票，重复提交不产生新票
);

-- 撤回使用许可的农户：持久封禁，重启后仍然有效
CREATE TABLE IF NOT EXISTS banned_farmers (
    farmer_no  TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '农户撤回使用许可',
    banned_at  REAL NOT NULL
);

-- 撤回时名下记录号快照：同一记录号永远不得改绑其他农户重传
CREATE TABLE IF NOT EXISTS banned_farmer_records (
    record_no  TEXT PRIMARY KEY,
    farmer_no  TEXT NOT NULL,
    banned_at  REAL NOT NULL
);

-- 撤回时描述指纹快照：原描述换号/换农户重传可被识别并整批拒绝
CREATE TABLE IF NOT EXISTS banned_descriptions (
    desc_hash      TEXT PRIMARY KEY,   -- sha256(描述原文)
    source_farmer  TEXT NOT NULL,
    description    TEXT NOT NULL,      -- 留档便于审计与页面展示
    banned_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_rv ON claims(record_no, version);
CREATE INDEX IF NOT EXISTS idx_labels_rv ON labels(record_no, version);
CREATE INDEX IF NOT EXISTS idx_banned_records_farmer ON banned_farmer_records(farmer_no);
"""


def connect() -> sqlite3.Connection:
    path = DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：自动提交，事务由我们显式 BEGIN IMMEDIATE 控制
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """建表并把旧版库平滑升级到带农户号的新结构。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # 旧库迁移：补 farmer_no 列，旧行保持 NULL = 来源未知（标签与领取原样保留）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(records_current)")}
        if "farmer_no" not in cols:
            conn.execute("ALTER TABLE records_current ADD COLUMN farmer_no TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_farmer ON records_current(farmer_no)"
        )
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写事务：BEGIN IMMEDIATE 先拿写锁，避免并发下锁升级失败。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
