"""核心业务规则：领取、提交、上传改写、农户撤回、列表查询。

这些函数都不自己开事务，由调用方（路由或测试）放进 db.write_tx() 里执行，
保证"检查 + 写入"在同一事务内完成，跨进程并发也不会破坏约束。

农户许可撤回规则（与上传规则联动）：
- 撤回按农户号执行：同事务删除该农户的全部记录、领取与标签，并写入三张封禁表；
- 之后任何含封禁农户号的 CSV 整批拒绝（一行不落库）；
- 撤回时快照记录号与描述指纹：同一记录号不得改绑其他农户，原描述也不得换号重传；
- 旧库中无农户号的记录 farmer_no 为 NULL，标记 UNKNOWN_SOURCE，上传不得给它指定农户。
"""
import csv
import hashlib
import io
import uuid

LABELS = ("病害", "虫害", "无异常")
ANNOTATORS = {"ann1": "标注员甲", "ann2": "标注员乙", "ann3": "标注员丙"}

# 旧库遗留记录的来源标记：不擅自归属任何农户
UNKNOWN_SOURCE = "来源未知"

MAX_CSV_BYTES = 5 * 1024 * 1024


class LogicError(Exception):
    """业务规则拒绝（过期、改版、重复领取等），消息可直接展示给用户。"""


def desc_hash(description: str) -> str:
    """田间描述的指纹：撤回后用于识别换记录号/换农户号的原描述重传。"""
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 领取

def claim_record(conn, record_no: str, annotator: str, now: float, ttl: int) -> dict:
    """领取一条记录，返回新凭证。

    - 已标过当前版本 / 记录已集齐两票 / 两席被他人有效领取占满 -> 拒绝；
    - 重复领取（含过期后重领）会轮换 token，旧页面凭证立即失效。
    """
    cur = conn.execute(
        "SELECT * FROM records_current WHERE record_no = ?", (record_no,)
    ).fetchone()
    if cur is None:
        raise LogicError("记录不存在，可能尚未上传或其农户已撤回许可")
    version = cur["version"]

    if conn.execute(
        "SELECT 1 FROM labels WHERE record_no=? AND version=? AND annotator=?",
        (record_no, version, annotator),
    ).fetchone():
        raise LogicError("您已标注过该记录的当前版本")

    votes = conn.execute(
        "SELECT COUNT(*) AS c FROM labels WHERE record_no=? AND version=?",
        (record_no, version),
    ).fetchone()["c"]
    if votes >= 2:
        raise LogicError("该记录已完成双人标注")

    # 他人有效领取占位：已提交者的领取视为已消费，不再占位
    others_active = conn.execute(
        """
        SELECT COUNT(*) AS c FROM claims cl
        WHERE cl.record_no=? AND cl.version=? AND cl.annotator<>? AND cl.expires_at>?
          AND NOT EXISTS (SELECT 1 FROM labels l
                          WHERE l.record_no=cl.record_no AND l.version=cl.version
                            AND l.annotator=cl.annotator)
        """,
        (record_no, version, annotator, now),
    ).fetchone()["c"]
    if votes + others_active >= 2:
        raise LogicError("该记录正在被其他标注员处理，请稍后再试")

    token = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO claims(token, record_no, version, annotator, expires_at, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(record_no, version, annotator)
        DO UPDATE SET token=excluded.token,
                      expires_at=excluded.expires_at,
                      created_at=excluded.created_at
        """,
        (token, record_no, version, annotator, now + ttl, now),
    )
    return {
        "token": token,
        "record_no": record_no,
        "version": version,
        "description": cur["description"],
        "expires_at": now + ttl,
    }


# ---------------------------------------------------------------- 提交

def submit_label(conn, token: str, annotator: str, label: str, now: float) -> dict:
    """凭领取 token 提交标签。

    拒绝顺序：凭证无效（含重领/改写/撤回后旧页） -> 版本不符 -> 已过期；
    同人重复提交不报错也不加票。农户撤回会在同事务删领取，旧领取无法回写。
    """
    if label not in LABELS:
        raise LogicError("非法标签")

    cl = conn.execute("SELECT * FROM claims WHERE token=?", (token,)).fetchone()
    if cl is None or cl["annotator"] != annotator:
        raise LogicError(
            "领取凭证无效：该记录可能已被改写、其农户已撤回许可、被重新领取，或页面已过期。"
            "请回到待标列表重新领取。"
        )

    cur = conn.execute(
        "SELECT * FROM records_current WHERE record_no=?", (cl["record_no"],)
    ).fetchone()
    if cur is None or cur["version"] != cl["version"]:
        raise LogicError("该记录描述已被改写或已随农户撤回移除，旧版本页面提交被拒绝")

    if now > cl["expires_at"]:
        raise LogicError("领取已过期（超过有效期），本次提交被拒绝，请重新领取")

    record_no, version = cl["record_no"], cl["version"]
    if conn.execute(
        "SELECT 1 FROM labels WHERE record_no=? AND version=? AND annotator=?",
        (record_no, version, annotator),
    ).fetchone():
        return {"duplicated": True, "record_no": record_no, "version": version}

    votes = conn.execute(
        "SELECT COUNT(*) AS c FROM labels WHERE record_no=? AND version=?",
        (record_no, version),
    ).fetchone()["c"]
    if votes >= 2:
        raise LogicError("该记录已集齐两票，提交被拒绝")

    conn.execute(
        "INSERT INTO labels(record_no, version, annotator, label, created_at) "
        "VALUES (?,?,?,?,?)",
        (record_no, version, annotator, label, now),
    )
    return {"duplicated": False, "record_no": record_no, "version": version}


# ---------------------------------------------------------------- 上传 / 改写

def parse_csv_bytes(raw: bytes):
    """解析上传的 CSV。

    新流程要求每行带农户号，表头必须为
    record_no,description,farmer_no（或 记录号,描述,农户号）。
    返回 [(record_no, description, farmer_no), ...]，任一缺列即整体拒绝。
    """
    if not raw:
        raise LogicError("文件为空")
    if len(raw) > MAX_CSV_BYTES:
        raise LogicError("CSV 文件过大（限 5MB）")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LogicError("CSV 必须是 UTF-8 编码")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise LogicError("CSV 缺少表头")
    reader.fieldnames = [h.strip() for h in reader.fieldnames if h]
    headers = set(reader.fieldnames)
    no_key = "record_no" if "record_no" in headers else ("记录号" if "记录号" in headers else None)
    desc_key = "description" if "description" in headers else ("描述" if "描述" in headers else None)
    farmer_key = "farmer_no" if "farmer_no" in headers else ("农户号" if "农户号" in headers else None)
    if not no_key or not desc_key or not farmer_key:
        raise LogicError(
            "表头需包含 record_no,description,farmer_no（或 记录号,描述,农户号）；"
            "新上传的 CSV 每行必须带农户号"
        )
    rows = []
    for i, r in enumerate(reader, start=2):
        no = (r.get(no_key) or "").strip()
        desc = (r.get(desc_key) or "").strip()
        farmer = (r.get(farmer_key) or "").strip()
        if not no and not desc and not farmer:
            continue
        if not no or not desc or not farmer:
            raise LogicError(f"第 {i} 行缺少记录号、描述或农户号（三者均为必填）")
        rows.append((no, desc, farmer))
    if not rows:
        raise LogicError("CSV 中没有有效数据行")
    return rows


def _preflight_upload(conn, rows):
    """上传前的整批校验：任何一行违规都抛 LogicError，整批拒绝、一行不落库。"""
    # 1) 文件内部同一记录号不得对应不同农户号（含同号重复行）
    owner_in_file = {}
    for no, desc, farmer in rows:
        prev = owner_in_file.get(no)
        if prev is not None and prev != farmer:
            raise LogicError(
                f"整批拒绝：记录号 {no} 在本文件中同时绑定农户 {prev} 与 {farmer}，"
                "同一记录号不得改绑农户"
            )
        owner_in_file[no] = farmer

    banned_farmers = {
        r["farmer_no"] for r in conn.execute("SELECT farmer_no FROM banned_farmers")
    }
    banned_records = {
        r["record_no"]: r["farmer_no"]
        for r in conn.execute("SELECT record_no, farmer_no FROM banned_farmer_records")
    }
    banned_hashes = {
        r["desc_hash"]: r["source_farmer"]
        for r in conn.execute("SELECT desc_hash, source_farmer FROM banned_descriptions")
    }

    # 库里现有记录号 -> 当前农户号（NULL 为旧库「来源未知」）
    existing = {
        r["record_no"]: r["farmer_no"]
        for r in conn.execute("SELECT record_no, farmer_no FROM records_current")
    }

    for no, desc, farmer in rows:
        # 2) 封禁农户号：整批拒绝
        if farmer in banned_farmers:
            raise LogicError(
                f"整批拒绝：农户 {farmer} 已撤回使用许可并被持久封禁，"
                "含该农户号的 CSV 一律不得导入"
            )
        # 3) 撤回时该记录号属于被撤农户：禁止改绑其他农户（也禁止同农户复用）
        if no in banned_records:
            raise LogicError(
                f"整批拒绝：记录号 {no} 原属已撤回许可的农户 {banned_records[no]}，"
                "该记录号不得重新上传或改绑其他农户"
            )
        # 4) 原描述换记录号/换农户号重传
        h = desc_hash(desc)
        if h in banned_hashes:
            raise LogicError(
                f"整批拒绝：记录号 {no} 的描述来自已撤回许可的农户 "
                f"{banned_hashes[h]}，撤回农户的田间描述不得以任何记录号重新进入训练集"
            )
        # 5) 记录号已在库中：农户号必须与现有绑定完全一致；
        #    尤其不得把旧库「来源未知」记录擅自归到某农户名下
        if no in existing:
            cur_farmer = existing[no]
            if cur_farmer != farmer:
                who = UNKNOWN_SOURCE if cur_farmer is None else cur_farmer
                raise LogicError(
                    f"整批拒绝：记录号 {no} 当前归属为「{who}」，"
                    f"不得改绑到农户 {farmer}（同一记录号的农户绑定不可变更）"
                )


def upsert_rows(conn, rows, now: float) -> dict:
    """按记录号 upsert：新增 / 改写（清旧领取与标签、版本+1）/ 未变（保留进度）。

    先做整批预检，任一违规即抛 LogicError，整个事务回滚，一行都不会写入。
    """
    _preflight_upload(conn, rows)
    added = updated = kept = 0
    for record_no, desc, farmer in rows:
        cur = conn.execute(
            "SELECT * FROM records_current WHERE record_no=?", (record_no,)
        ).fetchone()
        if cur is None:
            conn.execute(
                "INSERT INTO records_current(record_no, version, description, farmer_no, updated_at) "
                "VALUES (?,?,?,?,?)",
                (record_no, 1, desc, farmer, now),
            )
            added += 1
        elif cur["description"] == desc:
            kept += 1  # 未改写：领取与标签全部保留（预检已保证农户号一致）
        else:
            # 改写：同事务清除旧领取与旧标签，迟到的旧版提交随后会被版本校验挡下
            conn.execute("DELETE FROM claims WHERE record_no=?", (record_no,))
            conn.execute("DELETE FROM labels WHERE record_no=?", (record_no,))
            conn.execute(
                "UPDATE records_current SET version=?, description=?, updated_at=? "
                "WHERE record_no=?",
                (cur["version"] + 1, desc, now, record_no),
            )
            updated += 1
    return {"added": added, "updated": updated, "kept": kept}


# ---------------------------------------------------------------- 农户撤回

def withdraw_farmer(conn, farmer_no: str, now: float, reason: str = "农户撤回使用许可") -> dict:
    """按农户号撤回许可：清除记录/领取/标签并持久封禁，全部在同一事务内。

    任何一步失败由外层 write_tx 整体回滚，原状态保持不变。
    重复撤回同一农户号幂等成功（封禁本身持久）。
    """
    farmer_no = (farmer_no or "").strip()
    if not farmer_no:
        raise LogicError("农户号不能为空")

    recs = conn.execute(
        "SELECT record_no, description FROM records_current WHERE farmer_no=?",
        (farmer_no,),
    ).fetchall()
    record_nos = [r["record_no"] for r in recs]

    # 快照记录号与描述指纹，封堵换号重传与改绑重传
    for r in recs:
        conn.execute(
            "INSERT OR IGNORE INTO banned_farmer_records(record_no, farmer_no, banned_at) "
            "VALUES (?,?,?)",
            (r["record_no"], farmer_no, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO banned_descriptions(desc_hash, source_farmer, description, banned_at) "
            "VALUES (?,?,?,?)",
            (desc_hash(r["description"]), farmer_no, r["description"], now),
        )

    # 分批删除，避免 SQLite 单条 SQL 变量上限；记录、领取、标签全部清除
    for i in range(0, len(record_nos), 400):
        chunk = record_nos[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM labels WHERE record_no IN ({placeholders})", chunk
        )
        conn.execute(
            f"DELETE FROM claims WHERE record_no IN ({placeholders})", chunk
        )
        conn.execute(
            f"DELETE FROM records_current WHERE record_no IN ({placeholders})", chunk
        )

    conn.execute(
        "INSERT INTO banned_farmers(farmer_no, reason, banned_at) VALUES (?,?,?) "
        "ON CONFLICT(farmer_no) DO UPDATE SET reason=excluded.reason",
        (farmer_no, reason, now),
    )
    return {"farmer_no": farmer_no, "removed": len(record_nos)}


def list_banned_farmers(conn):
    """封禁名单页：封禁农户及其被清除的记录号（审计用）。"""
    farmers = [
        dict(r)
        for r in conn.execute(
            "SELECT farmer_no, reason, banned_at FROM banned_farmers ORDER BY banned_at DESC"
        ).fetchall()
    ]
    for f in farmers:
        f["records"] = [
            r["record_no"]
            for r in conn.execute(
                "SELECT record_no FROM banned_farmer_records WHERE farmer_no=? ORDER BY record_no",
                (f["farmer_no"],),
            ).fetchall()
        ]
    return farmers


# ---------------------------------------------------------------- 列表查询

def _farmer_label(row) -> str:
    return row["farmer_no"] if row["farmer_no"] else UNKNOWN_SOURCE


def list_pending(conn, annotator: str, now: float):
    """当前版本票数 < 2 的记录，附带当前标注员的领取/标注状态。

    注意：不返回任何他人标签内容（双盲），只返回票数。
    """
    rows = conn.execute(
        """
        SELECT c.record_no, c.version, c.description, c.farmer_no,
          (SELECT COUNT(*) FROM labels l
            WHERE l.record_no=c.record_no AND l.version=c.version) AS votes,
          EXISTS(SELECT 1 FROM labels l
            WHERE l.record_no=c.record_no AND l.version=c.version
              AND l.annotator=:a) AS i_labeled,
          (SELECT COUNT(*) FROM claims cl
            WHERE cl.record_no=c.record_no AND cl.version=c.version
              AND cl.annotator<>:a AND cl.expires_at>:now
              AND NOT EXISTS (SELECT 1 FROM labels l2
                              WHERE l2.record_no=cl.record_no AND l2.version=cl.version
                                AND l2.annotator=cl.annotator)) AS others_active,
          (SELECT cl.token FROM claims cl
            WHERE cl.record_no=c.record_no AND cl.version=c.version
              AND cl.annotator=:a AND cl.expires_at>:now) AS my_token,
          (SELECT cl.expires_at FROM claims cl
            WHERE cl.record_no=c.record_no AND cl.version=c.version
              AND cl.annotator=:a AND cl.expires_at>:now) AS my_expires
        FROM records_current c
        WHERE (SELECT COUNT(*) FROM labels l
                WHERE l.record_no=c.record_no AND l.version=c.version) < 2
        ORDER BY c.record_no
        """,
        {"a": annotator, "now": now},
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["farmer_label"] = _farmer_label(r)
        d["remaining"] = max(0, int(d["my_expires"] - now)) if d["my_expires"] else 0
        if d["i_labeled"]:
            d["state"] = "done_by_me"
        elif d["my_token"]:
            d["state"] = "claimed_by_me"
        elif d["votes"] + d["others_active"] >= 2:
            d["state"] = "busy"
        else:
            d["state"] = "open"
        out.append(d)
    return out


def list_disagreed(conn):
    """两票不一致的当前版本记录（含双方标签，用于质检复核）。"""
    recs = conn.execute(
        """
        SELECT c.record_no, c.version, c.description, c.farmer_no
        FROM records_current c
        WHERE (SELECT COUNT(*) FROM labels l
                WHERE l.record_no=c.record_no AND l.version=c.version) = 2
          AND (SELECT COUNT(DISTINCT l.label) FROM labels l
                WHERE l.record_no=c.record_no AND l.version=c.version) = 2
        ORDER BY c.record_no
        """
    ).fetchall()
    out = []
    for r in recs:
        labs = conn.execute(
            "SELECT annotator, label FROM labels "
            "WHERE record_no=? AND version=? ORDER BY annotator",
            (r["record_no"], r["version"]),
        ).fetchall()
        out.append(
            {
                "record_no": r["record_no"],
                "version": r["version"],
                "description": r["description"],
                "farmer_label": _farmer_label(r),
                "labels": [
                    {"name": ANNOTATORS.get(l["annotator"], l["annotator"]), "label": l["label"]}
                    for l in labs
                ],
            }
        )
    return out


def list_usable(conn):
    """两票一致的当前版本记录，即可下载的训练数据。

    撤回农户的记录已在 withdraw_farmer 中删除，天然不会出现在这里；
    旧库「来源未知」记录若已双票一致，仍保留可下载并在页面标明来源未知。
    """
    return [
        {**dict(r), "farmer_label": r["farmer_no"] if r["farmer_no"] else UNKNOWN_SOURCE}
        for r in conn.execute(
            """
            SELECT c.record_no, c.version, c.description, c.farmer_no,
              (SELECT l.label FROM labels l
                WHERE l.record_no=c.record_no AND l.version=c.version LIMIT 1) AS label
            FROM records_current c
            WHERE (SELECT COUNT(*) FROM labels l
                    WHERE l.record_no=c.record_no AND l.version=c.version) = 2
              AND (SELECT COUNT(DISTINCT l.label) FROM labels l
                    WHERE l.record_no=c.record_no AND l.version=c.version) = 1
            ORDER BY c.record_no
            """
        ).fetchall()
    ]


def stats(conn):
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN votes < 2 THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN votes = 2 AND distincts = 2 THEN 1 ELSE 0 END) AS disagreed,
          SUM(CASE WHEN votes = 2 AND distincts = 1 THEN 1 ELSE 0 END) AS usable
        FROM (
          SELECT c.record_no,
            (SELECT COUNT(*) FROM labels l
              WHERE l.record_no=c.record_no AND l.version=c.version) AS votes,
            (SELECT COUNT(DISTINCT l.label) FROM labels l
              WHERE l.record_no=c.record_no AND l.version=c.version) AS distincts
          FROM records_current c
        )
        """
    ).fetchone()
    out = {k: (row[k] or 0) for k in ("pending", "disagreed", "usable")}
    out["banned"] = conn.execute("SELECT COUNT(*) AS c FROM banned_farmers").fetchone()["c"]
    out["unknown"] = conn.execute(
        "SELECT COUNT(*) AS c FROM records_current WHERE farmer_no IS NULL"
    ).fetchone()["c"]
    return out
