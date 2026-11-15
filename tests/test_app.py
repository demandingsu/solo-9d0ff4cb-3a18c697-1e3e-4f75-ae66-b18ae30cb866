"""业务规则与页面流程测试。

时间相关用例直接调用 logic 层并注入 now，无需真实等待 60 秒。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

# 新流程：每行必须带农户号
CSV1 = (
    "record_no,description,farmer_no\n"
    "R1,玉米叶片出现病斑,F1\n"
    "R2,稻穗颈部枯死,F2\n"
    "R3,植株生长正常,F1\n"
)
CSV1_CHANGED = (
    "record_no,description,farmer_no\n"
    "R1,玉米叶片出现大面积褐色病斑（改写）,F1\n"
    "R2,稻穗颈部枯死,F2\n"
    "R3,植株生长正常,F1\n"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEED_ON_EMPTY", "0")
    import app.db as db
    importlib.reload(db)
    import app.logic as logic
    importlib.reload(logic)
    import app.main as main
    importlib.reload(main)
    db.init_db()
    with TestClient(main.app) as client:
        yield db, logic, main, client


def upload(client, text=CSV1):
    return client.post(
        "/upload", files={"file": ("a.csv", text.encode("utf-8"), "text/csv")}
    )


def claim_token(client, record_no, annotator):
    client.cookies.set("annotator", annotator)
    r = client.post("/claim", data={"record_no": record_no}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/work/")[1]


def submit(client, token, annotator, label):
    client.cookies.set("annotator", annotator)
    return client.post("/submit", data={"token": token, "label": label})


# ------------------------------------------------------------- 页面流程

def test_full_flow_to_download(env):
    db, logic, main, client = env
    assert upload(client).status_code == 200

    t1 = claim_token(client, "R1", "ann1")
    r = client.get(f"/work/{t1}")
    assert r.status_code == 200 and "玉米叶片出现病斑" in r.text

    assert submit(client, t1, "ann1", "病害").status_code == 200
    t2 = claim_token(client, "R1", "ann2")
    assert submit(client, t2, "ann2", "病害").status_code == 200

    r = client.get("/usable")
    assert "R1" in r.text and "病害" in r.text and "F1" in r.text

    r = client.get("/download")
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "R1,玉米叶片出现病斑,病害" in body
    assert "R2" not in body  # 未完成的记录不进训练集


def test_blind_pending_hides_labels(env):
    """提交前服务端不返回他人标签：待标页只显示票数。"""
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    submit(client, t1, "ann1", "虫害")
    client.cookies.set("annotator", "ann2")
    r = client.get("/pending")
    assert "R1" in r.text and "1/2" in r.text
    # R1 所在行不得出现他人已提交的标签
    row = r.text.split("R1", 1)[1].split("</tr>", 1)[0]
    assert "虫害" not in row


def test_expired_submit_rejected_and_reclaim(env):
    db, logic, main, client = env
    upload(client)
    conn = db.connect()
    try:
        info = logic.claim_record(conn, "R1", "ann1", now=1000.0, ttl=60)
        with pytest.raises(logic.LogicError, match="过期"):
            logic.submit_label(conn, info["token"], "ann1", "病害", now=1061.0)
        # 过期后可重领：拿到新 token
        info2 = logic.claim_record(conn, "R1", "ann1", now=1062.0, ttl=60)
        assert info2["token"] != info["token"]
        # 旧页（旧 token）即使本人重领后也拒绝
        with pytest.raises(logic.LogicError, match="凭证无效"):
            logic.submit_label(conn, info["token"], "ann1", "病害", now=1063.0)
        # 新页在有效期内可提交
        res = logic.submit_label(conn, info2["token"], "ann1", "病害", now=1064.0)
        assert res["duplicated"] is False
    finally:
        conn.close()


def test_duplicate_submit_adds_no_vote(env):
    db, logic, main, client = env
    upload(client)
    t = claim_token(client, "R1", "ann1")
    assert submit(client, t, "ann1", "病害").status_code == 200
    r = submit(client, t, "ann1", "病害")
    assert r.status_code == 200 and "重复提交" in r.text
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"]
    conn.close()
    assert n == 1


def test_third_annotator_blocked(env):
    db, logic, main, client = env
    upload(client)
    claim_token(client, "R1", "ann1")
    claim_token(client, "R1", "ann2")
    # 两席被占，第三人不能领取
    client.cookies.set("annotator", "ann3")
    r = client.post("/claim", data={"record_no": "R1"}, follow_redirects=False)
    assert "err" in r.headers["location"]
    # 前两票完成后第三人同样不能领取
    t1 = claim_token(client, "R2", "ann1")
    t2 = claim_token(client, "R2", "ann2")
    submit(client, t1, "ann1", "病害")
    submit(client, t2, "ann2", "病害")
    r = client.post("/claim", data={"record_no": "R2"}, follow_redirects=False)
    assert "err" in r.headers["location"]


def test_disagreement_page_and_download_excluded(env):
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    t2 = claim_token(client, "R1", "ann2")
    submit(client, t1, "ann1", "病害")
    submit(client, t2, "ann2", "虫害")
    r = client.get("/disagreed")
    assert "R1" in r.text and "病害" in r.text and "虫害" in r.text
    assert "R1" not in client.get("/download").content.decode("utf-8-sig")


# ------------------------------------------------------------- 改写规则

def test_rewrite_clears_and_blocks_late_submit(env):
    db, logic, main, client = env
    upload(client)
    t1 = claim_token(client, "R1", "ann1")
    submit(client, t1, "ann1", "病害")          # 旧版本一票
    t2 = claim_token(client, "R1", "ann2")      # ann2 持有有效领取

    r = upload(client, CSV1_CHANGED)            # R1 描述改写
    assert "改写 <b>1</b>" in r.text and "未变保留 <b>2</b>" in r.text

    conn = db.connect()
    cur = conn.execute("SELECT * FROM records_current WHERE record_no='R1'").fetchone()
    assert cur["version"] == 2 and cur["farmer_no"] == "F1"
    assert conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM claims WHERE record_no='R1'").fetchone()["c"] == 0
    conn.close()

    # 旧页提交（迟到）不得写入新版
    r = submit(client, t2, "ann2", "虫害")
    assert r.status_code == 400 and "被拒绝" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM labels WHERE record_no='R1'").fetchone()["c"] == 0
    conn.close()

    # 未改写的 R2 进度保留
    t3 = claim_token(client, "R2", "ann1")
    submit(client, t3, "ann1", "病害")
    upload(client, CSV1_CHANGED)  # 再次重传，R2 仍未变
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no='R2' AND version=1"
    ).fetchone()["c"] == 1
    conn.close()


def test_chinese_header_csv(env):
    db, logic, main, client = env
    r = upload(client, "记录号,描述,农户号\nC1,茶园发现茶小绿叶蝉,F9\n")
    assert r.status_code == 200 and "新增 <b>1</b>" in r.text


# ------------------------------------------------------------- 上传必须带农户号

def test_csv_without_farmer_column_rejected_entirely(env):
    db, logic, main, client = env
    # 旧的两列表头不再接受
    r = upload(client, "record_no,description\nX1,旧格式无农户号\n")
    assert r.status_code == 400 and "农户号" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"] == 0
    conn.close()


def test_csv_row_missing_farmer_rejected(env):
    db, logic, main, client = env
    r = upload(client, "record_no,description,farmer_no\nX1,描述,\n")
    assert r.status_code == 400
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"] == 0
    conn.close()


# ------------------------------------------------------------- 农户撤回

def test_withdraw_removes_data_and_blocks_pages_and_download(env):
    db, logic, main, client = env
    upload(client)
    # F1 名下 R1、R3；先给 R1 凑齐两票进入训练集
    t1 = claim_token(client, "R1", "ann1")
    submit(client, t1, "ann1", "病害")
    t2 = claim_token(client, "R1", "ann2")
    submit(client, t2, "ann2", "病害")
    assert "R1" in client.get("/download").content.decode("utf-8-sig")

    # 网页按农户号撤回
    r = client.post("/withdraw", data={"farmer_no": "F1"})
    assert r.status_code == 200 and "F1" in r.text

    conn = db.connect()
    # F1 的记录、领取、标签全部清除；F2 的 R2 保留
    assert conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE farmer_no='F1'"
    ).fetchone()["c"] == 0
    recs = {r["record_no"] for r in conn.execute("SELECT record_no FROM records_current")}
    assert recs == {"R2"}
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no IN ('R1','R3')"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM claims WHERE record_no IN ('R1','R3')"
    ).fetchone()["c"] == 0
    # 持久封禁
    assert conn.execute(
        "SELECT COUNT(*) c FROM banned_farmers WHERE farmer_no='F1'"
    ).fetchone()["c"] == 1
    conn.close()

    # 新页面与训练 CSV 不再含该农户数据
    body = client.get("/download").content.decode("utf-8-sig")
    assert "R1" not in body and "R3" not in body
    pending_text = client.get("/pending").text
    assert "R1" not in pending_text and "R3" not in pending_text and "R2" in pending_text
    assert "F1" in client.get("/banned").text


def test_withdraw_blocks_old_claim_writeback(env):
    """撤回后旧领取不能回写标签。"""
    db, logic, main, client = env
    upload(client)
    token = claim_token(client, "R1", "ann1")  # R1 属于 F1，先领取不提交
    client.post("/withdraw", data={"farmer_no": "F1"})
    # 旧页面提交 -> 凭证随撤回被删，拒绝
    r = submit(client, token, "ann1", "病害")
    assert r.status_code == 400 and "凭证无效" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"] == 0
    conn.close()
    # 工作页也提示凭证无效
    assert client.get(f"/work/{token}").status_code == 400


def test_withdraw_failure_rolls_back_keeps_state(env):
    """撤回事务失败时整体回滚，原状态保持不变。"""
    db, logic, main, client = env
    upload(client)
    conn = db.connect()
    before = conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"]
    conn.close()

    class Boom(Exception):
        pass

    # 模拟撤回过程中的意外故障
    with pytest.raises(Boom):
        with db.write_tx() as conn:
            logic.withdraw_farmer(conn, "F1", 1234.0)
            raise Boom("disk full")

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"] == before
    assert conn.execute("SELECT COUNT(*) c FROM banned_farmers").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM banned_descriptions").fetchone()["c"] == 0
    conn.close()
    # 撤回失败后该农户未被封禁，正常重传（未变）仍然可用
    assert upload(client).status_code == 200


def test_withdraw_empty_farmer_rejected(env):
    db, logic, main, client = env
    upload(client)
    r = client.post("/withdraw", data={"farmer_no": "  "})
    assert r.status_code == 400 and "农户号" in r.text


def test_repeat_withdraw_is_idempotent(env):
    db, logic, main, client = env
    upload(client)
    client.post("/withdraw", data={"farmer_no": "F1"})
    r = client.post("/withdraw", data={"farmer_no": "F1"})
    assert r.status_code == 200
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) c FROM banned_farmers WHERE farmer_no='F1'"
    ).fetchone()["c"] == 1
    conn.close()


# ------------------------------------------------------------- 封禁后的上传拦截

def test_upload_with_banned_farmer_rejected_entire_batch(env):
    db, logic, main, client = env
    upload(client)
    client.post("/withdraw", data={"farmer_no": "F1"})

    # 含封禁农户号：整批拒绝，同批的合法新记录也不得入库
    r = upload(
        client,
        "record_no,description,farmer_no\n"
        "R9,全新农户的全新描述,F8\n"
        "R10,封禁农户换了记录号,F1\n",
    )
    assert r.status_code == 400 and "整批拒绝" in r.text and "F1" in r.text
    conn = db.connect()
    recs = {x["record_no"] for x in conn.execute("SELECT record_no FROM records_current")}
    assert "R9" not in recs and "R10" not in recs
    conn.close()


def test_rebind_banned_record_no_to_other_farmer_rejected(env):
    """同一记录号不得改绑其他农户（撤回记录号快照拦截）。"""
    db, logic, main, client = env
    upload(client)
    client.post("/withdraw", data={"farmer_no": "F1"})  # R1/R3 进快照
    r = upload(client, "record_no,description,farmer_no\nR1,玉米叶片出现病斑,F7\n")
    assert r.status_code == 400 and "整批拒绝" in r.text and "R1" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"] == 1  # 只剩 R2
    conn.close()


def test_reupload_same_description_new_no_rejected(env):
    """原描述换记录号/换农户号重传也要拦截（描述指纹）。"""
    db, logic, main, client = env
    upload(client)
    client.post("/withdraw", data={"farmer_no": "F1"})
    r = upload(
        client,
        "record_no,description,farmer_no\n"
        "NEW-1,玉米叶片出现病斑,F7\n",  # R1 的原描述，换号换农户
    )
    assert r.status_code == 400 and "整批拒绝" in r.text
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM records_current").fetchone()["c"] == 1
    conn.close()


def test_rebind_existing_record_rejected(env):
    """库内现存记录号不得改绑农户号。"""
    db, logic, main, client = env
    upload(client)
    r = upload(client, "record_no,description,farmer_no\nR1,玉米叶片出现病斑,F7\n")
    assert r.status_code == 400 and "整批拒绝" in r.text and "改绑" in r.text
    conn = db.connect()
    cur = conn.execute("SELECT farmer_no FROM records_current WHERE record_no='R1'").fetchone()
    assert cur["farmer_no"] == "F1"  # 原绑定不变
    conn.close()


def test_conflicting_farmer_within_one_file_rejected(env):
    db, logic, main, client = env
    r = upload(
        client,
        "record_no,description,farmer_no\n"
        "R1,同一记录号两个农户,F1\n"
        "R1,同一记录号两个农户,F2\n",
    )
    assert r.status_code == 400 and "整批拒绝" in r.text


# ------------------------------------------------------------- 旧库迁移：来源未知

def test_legacy_records_kept_and_marked_unknown(env):
    """旧库（无 farmer_no 列）升级：旧记录与标签保留，页面标为来源未知，不得归户。"""
    db, logic, main, client = env
    # 直接用旧结构造一个旧库
    import os
    path = os.environ["DB_PATH"]
    os.remove(path)
    conn = sqlite3_connect(path)
    conn.executescript(
        """
        CREATE TABLE records_current (
            record_no TEXT PRIMARY KEY, version INTEGER NOT NULL,
            description TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE claims (
            token TEXT PRIMARY KEY, record_no TEXT NOT NULL, version INTEGER NOT NULL,
            annotator TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
            UNIQUE(record_no, version, annotator));
        CREATE TABLE labels (
            record_no TEXT NOT NULL, version INTEGER NOT NULL, annotator TEXT NOT NULL,
            label TEXT NOT NULL CHECK (label IN ('病害','虫害','无异常')),
            created_at REAL NOT NULL, PRIMARY KEY(record_no, version, annotator));
        INSERT INTO records_current VALUES ('L1', 1, '旧库遗留描述', 1000.0);
        INSERT INTO labels VALUES ('L1', 1, 'ann1', '病害', 1000.0);
        INSERT INTO labels VALUES ('L1', 1, 'ann2', '病害', 1001.0);
        """
    )
    conn.close()

    db.init_db()  # 触发 ALTER 迁移

    conn = db.connect()
    row = conn.execute("SELECT * FROM records_current WHERE record_no='L1'").fetchone()
    assert row["farmer_no"] is None  # 未擅自归户
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no='L1'"
    ).fetchone()["c"] == 2  # 旧标签保留
    conn.close()

    # 页面明确标为来源未知
    assert "来源未知" in client.get("/pending").text or "来源未知" in client.get("/usable").text
    usable = client.get("/usable").text
    assert "L1" in usable and "来源未知" in usable
    # 训练 CSV 仍含旧记录（规则要求保留旧记录和标签）
    assert "L1,旧库遗留描述,病害" in client.get("/download").content.decode("utf-8-sig")

    # 不得把来源未知的旧记录擅自归到某农户
    r = upload(client, "record_no,description,farmer_no\nL1,旧库遗留描述,F1\n")
    assert r.status_code == 400 and "来源未知" in r.text


def sqlite3_connect(path):
    import sqlite3
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c
