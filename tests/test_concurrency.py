"""并发压力测试：多线程共用同一 SQLite 文件，模拟两个应用进程下的
并发领取/提交/改写/撤回/上传。

不变量：
- 同一 (record_no, version) 至多两票，且来自不同标注员；
- 重复提交不产生额外票；
- 改写后旧 token 一律无法提交；
- 撤回成功后：该农户记录/领取/标签全部消失，训练集不再含其数据，旧领取无法回写；
- 封禁后含封禁农户号的上传整批失败，库内一行不多。
"""
import importlib
import threading
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "c.db"))
    import app.db as db
    importlib.reload(db)
    import app.logic as logic
    importlib.reload(logic)
    db.init_db()
    with db.write_tx() as conn:
        logic.upsert_rows(
            conn,
            [(f"R{i}", f"描述{i}", f"F{i % 3}") for i in range(5)],
            time.time(),
        )
    return db, logic


def test_concurrent_claim_and_submit(env):
    db, logic = env
    annotators = ["ann1", "ann2", "ann3"]
    errors, tokens = [], []
    lock = threading.Lock()

    def claim_all(a):
        for i in range(5):
            try:
                with db.write_tx() as conn:
                    info = logic.claim_record(conn, f"R{i}", a, time.time(), 60)
                with lock:
                    tokens.append((f"R{i}", a, info["token"]))
            except logic.LogicError:
                pass

    threads = [threading.Thread(target=claim_all, args=(a,)) for a in annotators for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 每条记录有效领取（不同标注员）不超过 2 个
    by_rec = {}
    for rec, a, _ in tokens:
        by_rec.setdefault(rec, set()).add(a)
    for rec, holders in by_rec.items():
        assert len(holders) <= 2, f"{rec} 被 {holders} 同时领取"

    def submit_all(rec, a, token):
        try:
            with db.write_tx() as conn:
                logic.submit_label(conn, token, a, "病害", time.time())
        except logic.LogicError:
            pass
        except Exception as e:  # 不允许出现非业务异常（如 database is locked）
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=submit_all, args=x) for x in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    rows = conn.execute(
        "SELECT record_no, version, COUNT(*) c, COUNT(DISTINCT annotator) d "
        "FROM labels GROUP BY record_no, version"
    ).fetchall()
    conn.close()
    for r in rows:
        assert r["c"] <= 2 and r["c"] == r["d"], dict(r)


def test_concurrent_rewrite_vs_submit(env):
    """提交与改写并发：要么提交先于改写（随后被清除），要么被挡下；绝不能写进新版本。"""
    db, logic = env
    conn = db.connect()
    info = logic.claim_record(conn, "R0", "ann1", time.time(), 60)
    conn.close()
    token = info["token"]
    done = threading.Event()

    def rewriter():
        for i in range(30):
            with db.write_tx() as c:
                logic.upsert_rows(c, [("R0", f"改写版本{i}", "F0")], time.time())
        done.set()

    def submitter():
        while not done.is_set():
            try:
                with db.write_tx() as c:
                    logic.submit_label(c, token, "ann1", "病害", time.time())
            except logic.LogicError:
                pass

    ts = [threading.Thread(target=rewriter), threading.Thread(target=submitter)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    conn = db.connect()
    cur = conn.execute("SELECT version FROM records_current WHERE record_no='R0'").fetchone()
    n = conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no='R0' AND version=?",
        (cur["version"],),
    ).fetchone()["c"]
    conn.close()
    assert n == 0, "旧版提交写进了新版本"


def test_concurrent_withdraw_vs_claims_and_submits(env):
    """两个进程交错：撤回与领取/提交并发，撤回成功后数据彻底消失且旧领取无法回写。"""
    db, logic = env
    errors = []
    lock = threading.Lock()
    withdrawn = threading.Event()

    # F0 名下 R0、R3；先给两个标注员发领取凭证（撤回前领取）
    conn = db.connect()
    old_claim = logic.claim_record(conn, "R0", "ann1", time.time(), 600)
    conn.close()

    def withdrawer():
        with db.write_tx() as c:
            logic.withdraw_farmer(c, "F0", time.time())
        withdrawn.set()

    def worker(a):
        # 撤回前后都不断尝试领取与提交 F0 / 其他农户的记录
        end = time.time() + 3
        while time.time() < end:
            for rec in ("R0", "R3", "R1"):
                try:
                    with db.write_tx() as c:
                        info = logic.claim_record(c, rec, a, time.time(), 600)
                    with db.write_tx() as c:
                        logic.submit_label(c, info["token"], a, "病害", time.time())
                except logic.LogicError:
                    pass
                except Exception as e:
                    with lock:
                        errors.append(e)

    threads = [
        threading.Thread(target=withdrawer),
        threading.Thread(target=worker, args=("ann1",)),
        threading.Thread(target=worker, args=("ann2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert withdrawn.is_set()

    # 撤回前的旧领取凭证在撤回后绝不允许回写
    with pytest.raises(logic.LogicError, match="凭证无效"):
        with db.write_tx() as c:
            logic.submit_label(c, old_claim["token"], "ann1", "病害", time.time())

    conn = db.connect()
    # F0 的记录、标签、领取全部不存在
    assert conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE record_no IN ('R0','R3')"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no IN ('R0','R3')"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM claims WHERE record_no IN ('R0','R3')"
    ).fetchone()["c"] == 0
    # 其他农户记录保留
    assert conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE farmer_no <> 'F0'"
    ).fetchone()["c"] == 3
    # 封禁落库
    assert conn.execute(
        "SELECT 1 FROM banned_farmers WHERE farmer_no='F0'"
    ).fetchone() is not None
    conn.close()

    # 训练集（双票一致列表）里永远不会再出现 F0 的记录号
    with db.connect() as c:
        usable = logic.list_usable(c)
    assert all(r["record_no"] not in ("R0", "R3") for r in usable)


def test_concurrent_upload_after_ban_never_partial(env):
    """封禁后并发上传含封禁号的 CSV：全部整批拒绝，库内不会出现任何批次的行。"""
    db, logic = env
    with db.write_tx() as c:
        logic.withdraw_farmer(c, "F1", time.time())

    errors = []
    lock = threading.Lock()

    def uploader(i):
        # 一半批次带封禁号 F1（应整批拒绝），一半是干净的新农户数据（可成功）
        if i % 2 == 0:
            rows = [
                (f"OK-{i}", f"干净描述{i}", f"G{i}"),
                (f"BAD-{i}", f"坏批次描述{i}", "F1"),
            ]
            expect_fail = True
        else:
            rows = [(f"OK-{i}", f"干净描述{i}", f"G{i}")]
            expect_fail = False
        try:
            with db.write_tx() as c:
                logic.upsert_rows(c, rows, time.time())
            assert not expect_fail
        except logic.LogicError:
            assert expect_fail
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=uploader, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    # 坏批次的任何行都不得入库
    bad = conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE record_no LIKE 'BAD-%'"
    ).fetchone()["c"]
    assert bad == 0
    # 干净批次正常入库
    ok = conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE record_no LIKE 'OK-%'"
    ).fetchone()["c"]
    assert ok == 4
    # F1 原有且未被撤回波及的记录……F1 名下 R1/R4 已随撤回删除
    assert conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE farmer_no='F1'"
    ).fetchone()["c"] == 0
    conn.close()


def test_concurrent_double_withdraw_same_farmer(env):
    """两个进程同时撤回同一农户：结果等价于一次撤回，不报错且封禁只落一条。"""
    db, logic = env
    errors = []
    lock = threading.Lock()

    def wd():
        try:
            with db.write_tx() as c:
                logic.withdraw_farmer(c, "F2", time.time())
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=wd) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) c FROM banned_farmers WHERE farmer_no='F2'"
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM records_current WHERE farmer_no='F2'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM labels WHERE record_no IN "
        "(SELECT record_no FROM banned_farmer_records WHERE farmer_no='F2')"
    ).fetchone()["c"] == 0
    conn.close()
