"""本轮功能测试: 普通分配锁定表 (会务方按论文锁定 0~2 名评审人)。

覆盖需求:
- 会务方凭密钥提交完整锁定表 (携带所见资料修订号); 空表清除全部锁定;
  未知论文/评审人 404、重复锁定或每篇超 2 名 422、过期/超前修订号 409,
  被拒请求一律不改动当前锁定表、不推进修订号;
- 相同锁定表重试幂等 (changed=false, 不推进修订号); 有效变更推进修订号,
  写入响应返回修订号与是否变更; 会务方可查看当前锁定表;
- 普通预演与发布必须保留锁定槽位; 剩余位置遵守既有机构冲突/主题专长/回避/
  资格/容量约束与优化次序 (先最小化最高容量比例, 再全局字典序最小);
  单人锁定时专长要求可由另一人满足;
- 锁定人随后被删除/停用/回避或因资料与机构归并而失格时: 预演说明具体冲突并
  标为不可完整分配, 发布拒绝 (422) 且不改变当前发布版, 锁定不被悄悄释放;
- 锁定表仅约束普通分配, 不改变补位的确认槽位规则。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-locks-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "reviewer_status_changes", "institution_merges",
            "review_corrections", "assignment_locks",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield
    # 测试进程共享同一数据库连接, 其他测试文件的夹具不知道锁定表:
    # 每个用例结束后清空锁定表, 避免残留锁定影响其他文件的求解
    db.reset_for_tests()
    with db.write_txn() as conn:
        conn.execute("DELETE FROM assignment_locks")


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid, topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def put_locks(client, locks, revision=None):
    if revision is None:
        revision = current_revision(client)
    return client.put(
        "/assignment/locks",
        headers=ORG,
        json={"base_revision": revision, "locks": locks},
    )


def get_locks(client):
    return client.get("/assignment/locks", headers=ORG).json()


def standard_setup(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1")


def setup_locked_publish(client):
    """3 名评审人 + P1, 锁定 R3 后发布; 返回发布序号。发布方案 P1 = (R1, R3)。"""
    standard_setup(client)
    assert put_locks(client, {"P1": ["R3"]}).json()["changed"] is True
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200
    assert r.json()["plan"]["P1"] == ["R1", "R3"]
    return r.json()["serial"]


# ------------------------------------------------------------ 鉴权与写入校验

def test_locks_require_organizer_key(client):
    assert client.get("/assignment/locks").status_code == 401
    assert client.get("/assignment/locks", headers={"X-Organizer-Key": "wrong"}).status_code == 401
    assert client.put("/assignment/locks", json={"base_revision": 0, "locks": {}}).status_code == 401


def test_put_and_get_locks(client):
    standard_setup(client)
    rev = current_revision(client)
    r = put_locks(client, {"P1": ["R3", "R2"]})  # 顺序无关, 存储按编号升序
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["revision"] == rev + 1
    assert body["locks"] == {"P1": ["R2", "R3"]}
    assert current_revision(client) == rev + 1
    view = get_locks(client)
    assert view["locks"] == {"P1": ["R2", "R3"]}
    assert view["revision"] == rev + 1


def test_same_lock_table_retry_is_idempotent(client):
    standard_setup(client)
    assert put_locks(client, {"P1": ["R1"]}).json()["changed"] is True
    rev = current_revision(client)
    r = put_locks(client, {"P1": ["R1"]})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["revision"] == rev
    assert current_revision(client) == rev  # 不推进修订号


def test_empty_table_clears_locks(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R1", "R2"]})
    r = put_locks(client, {})
    assert r.json()["changed"] is True
    assert get_locks(client)["locks"] == {}
    r = put_locks(client, {})  # 已是空表: 再清除幂等
    assert r.json()["changed"] is False


def test_unknown_paper_or_reviewer_rejected_without_change(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R1"]})
    rev = current_revision(client)
    r = put_locks(client, {"P1": ["R1"], "GHOST-P": ["R2"]})
    assert r.status_code == 404
    r = put_locks(client, {"P1": ["R1", "GHOST-R"]})
    assert r.status_code == 404
    # 被拒请求不改表、不推进修订号
    assert get_locks(client)["locks"] == {"P1": ["R1"]}
    assert current_revision(client) == rev


def test_duplicate_and_oversized_locks_rejected(client):
    standard_setup(client)
    rev = current_revision(client)
    r = put_locks(client, {"P1": ["R1", "R1"]})
    assert r.status_code == 422
    r = put_locks(client, {"P1": ["R1", "R2", "R3"]})
    assert r.status_code == 422
    assert get_locks(client)["locks"] == {}
    assert current_revision(client) == rev


def test_stale_or_future_revision_rejected(client):
    standard_setup(client)
    rev = current_revision(client)
    r = put_locks(client, {"P1": ["R1"]}, revision=rev + 5)  # 超前
    assert r.status_code == 409
    put_locks(client, {"P1": ["R1"]})
    r = put_locks(client, {"P1": ["R2"]}, revision=rev)  # 过期
    assert r.status_code == 409
    assert get_locks(client)["locks"] == {"P1": ["R1"]}


# ------------------------------------------------------------ 预演/发布保留锁定槽位

def test_dry_run_keys_present_without_locks(client):
    standard_setup(client)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["locks"] == {}
    assert body["lock_problems"] == {}


def test_dry_run_preserves_single_lock(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R3"]})
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    # 无锁定时字典序最小为 (R1, R2); 锁定 R3 后必须保留 R3, 搭档取字典序最小的 R1
    assert body["plan"]["P1"] == ["R1", "R3"]
    assert body["locks"] == {"P1": ["R3"]}
    assert body["lock_problems"] == {}
    assert body["papers"]["P1"]["locked"] == ["R3"]


def test_dry_run_preserves_double_lock(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R2", "R3"]})
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["plan"]["P1"] == ["R2", "R3"]
    assert body["papers"]["P1"]["locked"] == ["R2", "R3"]


def test_single_lock_expertise_satisfied_by_partner(client):
    add_reviewer(client, "R1", "Inst-A", topics=["AI"])
    add_reviewer(client, "R2", "Inst-B", topics=["DB"])
    add_reviewer(client, "R3", "Inst-C", topics=["DB"])
    add_paper(client, "P1", topics=["AI"])
    put_locks(client, {"P1": ["R3"]})  # R3 不擅长 AI, 专长要求由搭档满足
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["plan"]["P1"] == ["R1", "R3"]  # (R2, R3) 无人擅长 AI, 不合法


def test_locked_loads_count_toward_capacity(client):
    add_reviewer(client, "R1", "Inst-A", capacity=1)
    add_reviewer(client, "R2", "Inst-B", capacity=2)
    add_reviewer(client, "R3", "Inst-C", capacity=2)
    add_paper(client, "P1")
    add_paper(client, "P2")
    put_locks(client, {"P1": ["R1"]})
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    # R1 锁定在 P1 且容量为 1, P2 只能由 R2/R3 组成
    assert body["plan"] == {"P1": ["R1", "R2"], "P2": ["R2", "R3"]}
    assert body["loads"]["R1"] == 1


def test_locks_respect_existing_constraints(client):
    # R2 与作者同机构被排除; 锁定 R1 后搭档只能是 R3
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Author-P1")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1", institutions=["Author-P1"])
    put_locks(client, {"P1": ["R1"]})
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["plan"]["P1"] == ["R1", "R3"]


def test_publish_preserves_locks(client):
    serial = setup_locked_publish(client)
    pub = client.get("/assignment", headers=ORG).json()
    assert pub["serial"] == serial
    assert pub["plan"]["P1"] == ["R1", "R3"]
    assert pub["papers"]["P1"]["locked"] == ["R3"]


def test_lock_write_bumps_revision_and_invalidates_publish(client):
    standard_setup(client)
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    put_locks(client, {"P1": ["R1"]})  # 锁定表写入推进修订号
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 409


def test_single_lock_without_valid_partner(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-A")  # 与 R1 同机构, 不能配对
    add_paper(client, "P1")
    put_locks(client, {"P1": ["R1"]})
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["unassigned"] == ["P1"]
    assert body["lock_problems"] == {}  # 锁定本身有效, 只是不存在合法搭档
    assert "no_valid_pair_containing_locked_reviewer" in body["diagnostics"]["P1"]["reasons"]


# ------------------------------------------------------------ 锁定人失格: 不悄悄释放

def test_locked_reviewer_deleted_blocks_completion(client):
    serial = setup_locked_publish(client)
    client.delete("/reviewers/R3", headers=ORG)
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["unassigned"] == ["P1"]
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_not_found"}
    ]
    assert body["diagnostics"]["P1"]["reasons"] == ["locked_reviewer_not_found"]
    # 发布拒绝且不改变当前发布版
    rev = current_revision(client)
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 422
    assert r.json()["detail"]["lock_problems"]["P1"][0]["reason"] == "locked_reviewer_not_found"
    pub = client.get("/assignment", headers=ORG).json()
    assert pub["serial"] == serial
    assert pub["plan"]["P1"] == ["R1", "R3"]
    # 锁定未被悄悄释放
    assert get_locks(client)["locks"] == {"P1": ["R3"]}


def test_locked_reviewer_disabled_blocks_completion(client):
    setup_locked_publish(client)
    rev = current_revision(client)
    r = client.post(
        "/reviewers/R3/status",
        headers=ORG,
        json={"reviewer_id": "R3", "base_revision": rev, "active": False,
              "reason": "长期休假暂停本轮评审"},
    )
    assert r.status_code == 200
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_disabled"}
    ]
    # 重新启用后恢复可行
    rev = current_revision(client)
    client.post(
        "/reviewers/R3/status",
        headers=ORG,
        json={"reviewer_id": "R3", "base_revision": rev, "active": True,
              "reason": "假期结束恢复评审"},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["plan"]["P1"] == ["R1", "R3"]


def test_locked_reviewer_hard_recusal_blocks_completion(client):
    serial = setup_locked_publish(client)
    r = client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers("R3"),
        json={"paper_id": "P1", "decision": "recuse", "reason": "近三年与作者有合作"},
    )
    assert r.status_code == 200
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_hard_recusal"}
    ]
    rev = current_revision(client)
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 422
    assert client.get("/assignment", headers=ORG).json()["serial"] == serial


def test_locked_reviewer_avoid_list_blocks_completion(client):
    setup_locked_publish(client)
    client.put(
        "/reviewers/R3",
        headers=ORG,
        json={"credential": "cred-R3", "topics": ["AI"], "institution": "Inst-C",
              "capacity": 3, "avoid_papers": ["P1"]},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_on_avoid_list"}
    ]


def test_locked_reviewer_institution_change_blocks_completion(client):
    setup_locked_publish(client)
    client.put(
        "/reviewers/R3",
        headers=ORG,
        json={"credential": "cred-R3", "topics": ["AI"], "institution": "Author-P1",
              "capacity": 3, "avoid_papers": []},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_same_institution_as_author"}
    ]


def test_locked_reviewer_merged_with_author_institution(client):
    setup_locked_publish(client)
    rev = current_revision(client)
    r = client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Inst-C", "name_b": "Author-P1", "base_revision": rev},
    )
    assert r.json()["changed"] is True
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_reviewer_same_institution_as_author_via_merge"}
    ]


def test_locked_pair_merged_into_same_institution(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R2", "R3"]})
    assert client.post("/assignment/dry-run", headers=ORG).json()["feasible"] is True
    rev = current_revision(client)
    client.post(
        "/institutions/merge-groups",
        headers=ORG,
        json={"name_a": "Inst-B", "name_b": "Inst-C", "base_revision": rev},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_pair_same_institution_via_merge"}
    ]


def test_locked_pair_without_expert(client):
    add_reviewer(client, "R1", "Inst-A", topics=["AI"])
    add_reviewer(client, "R2", "Inst-B", topics=["DB"])
    add_reviewer(client, "R3", "Inst-C", topics=["DB"])
    add_paper(client, "P1", topics=["AI"])
    put_locks(client, {"P1": ["R2", "R3"]})  # 两人均不擅长 AI, 无第三人可满足
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    assert body["lock_problems"]["P1"] == [
        {"reviewer_id": "R3", "reason": "locked_pair_no_expert"}
    ]


def test_locked_assignments_exceed_capacity(client):
    add_reviewer(client, "R1", "Inst-A", capacity=2)
    add_reviewer(client, "R2", "Inst-B", capacity=2)
    add_reviewer(client, "R3", "Inst-C", capacity=2)
    add_paper(client, "P1")
    add_paper(client, "P2")
    put_locks(client, {"P1": ["R1"], "P2": ["R1"]})
    assert client.post("/assignment/dry-run", headers=ORG).json()["feasible"] is True
    client.put(
        "/reviewers/R1",
        headers=ORG,
        json={"credential": "cred-R1", "topics": ["AI"], "institution": "Inst-A",
              "capacity": 1, "avoid_papers": []},
    )
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    # 确定性地在编号最大的论文上标记超额; P1 的锁定仍照常兑现
    assert body["lock_problems"] == {
        "P2": [{"reviewer_id": "R1", "reason": "locked_assignments_exceed_capacity"}]
    }
    assert body["plan"]["P1"] == ["R1", "R2"]
    assert body["unassigned"] == ["P2"]


def test_lock_on_deleted_paper_is_dormant_but_kept(client):
    standard_setup(client)
    add_paper(client, "P2")
    put_locks(client, {"P1": ["R1"], "P2": ["R2"]})
    client.delete("/papers/P1", headers=ORG)
    # 已删除论文的锁定无可约束对象, 不参与求解; 其余锁定照常兑现
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert "P1" not in body["plan"]
    assert body["plan"]["P2"] == ["R1", "R2"]
    assert body["locks"] == {"P2": ["R2"]}
    # 锁定行保留, 会务方可查看并清除
    assert get_locks(client)["locks"] == {"P1": ["R1"], "P2": ["R2"]}


# ------------------------------------------------------------ 补位不受锁定约束

def test_locks_do_not_constrain_backfill(client):
    standard_setup(client)
    put_locks(client, {"P1": ["R3"]})
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    # 普通发布保留锁定: P1 = (R1, R3)
    assert client.get("/assignment", headers=ORG).json()["plan"]["P1"] == ["R1", "R3"]
    # R1 确认, R3 保持 pending
    r = client.post(
        "/reviewer/assignments/P1/decision",
        headers=reviewer_headers("R1"),
        json={"paper_id": "P1", "decision": "confirm"},
    )
    assert r.status_code == 200
    # 补位只固定已确认槽位 (R1), 锁定表不约束补位: 搭档按优化次序取 R2 而非锁定的 R3
    body = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["fixed"]["P1"] == ["R1"]
    assert body["plan"]["P1"] == ["R1", "R2"]
