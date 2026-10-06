"""处置依赖（disposition dependencies）端到端测试。

语义要点：
- 同一演练内，把一条后续记录标记为依赖于一条前置记录，必须给出非空依据；
- 端点必须是两条不同的现存记录：缺号（missing / lost_confirmed）不能作为端点，
  冲突证据本身也不是记录、不可作为端点；不得跨演练；不得成环；
- 重复提交 / 重复撤销幂等；未完成演练可撤销；
- 查询返回直接前置、直接后续与沿前置方向展开的完整链，按终端标识、序号稳定排序；
- 记录更正不改变依赖所指身份（依赖以记录主键关联）；
- 封存版本固化当时的依赖与依据，之后增删只影响当前视图；
- 完成时最终封存与状态翻转原子生效；完成后拒绝依赖变更，历史版本照常可读。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def _create_drill(client, terminals):
    r = client.post("/api/drills", json={"name": "依赖测试演练", "terminals": terminals})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _entry(seq, content, ts=None, etype="evt"):
    return {
        "seq": seq,
        "occurred_at": ts or f"2026-10-05T09:{seq:02d}:00Z",
        "event_type": etype,
        "content": content,
    }


def _report(client, drill, term, entries):
    r = client.post(f"/api/drills/{drill}/terminals/{term}/reports",
                    json={"entries": entries})
    assert r.status_code == 200, r.text
    return r.json()


def _ep(term, seq):
    return {"terminal_code": term, "seq": seq}


def _deps_url(drill):
    return f"/api/drills/{drill}/dependencies"


def _add(client, drill, pre, succ, basis, **kw):
    return client.put(_deps_url(drill),
                      json={"predecessor": pre, "successor": succ, "basis": basis}, **kw)


def _remove(client, drill, pre, succ):
    return client.request("DELETE", _deps_url(drill),
                          json={"predecessor": pre, "successor": succ})


def test_create_query_and_stable_sorting(client):
    drill = _create_drill(client, ["A", "B", "C"])
    _report(client, drill, "A", [_entry(1, "A1"), _entry(2, "A2")])
    _report(client, drill, "B", [_entry(1, "B1")])
    _report(client, drill, "C", [_entry(1, "C1")])

    # B#1 依赖 A#1；A#2 依赖 B#1（构成 A#1 -> B#1 -> A#2 链）
    r = _add(client, drill, _ep("A", 1), _ep("B", 1), "先完成A1才能处置B1")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["already_exists"] is False
    assert first["predecessor"]["terminal_code"] == "A" and first["predecessor"]["seq"] == 1
    assert first["predecessor"]["record_id"]
    assert first["successor"]["terminal_code"] == "B" and first["successor"]["seq"] == 1
    assert first["basis"] == "先完成A1才能处置B1"
    dep_b1 = first["dependency_id"]

    r = _add(client, drill, _ep("B", 1), _ep("A", 2), "A2依赖B1")
    assert r.status_code == 201, r.text

    # C#1 也依赖 A#1（菱形分支，用于验证后续排序与最短距离）
    r = _add(client, drill, _ep("A", 1), _ep("C", 1), "C1依赖A1")
    assert r.status_code == 201, r.text

    # 查询 A#2：直接前置=[B#1]；完整链含 B#1(dist1) 与 A#1(dist2)，按终端+序号排序
    r = client.get(f"/api/drills/{drill}/records/A/2/dependencies")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["terminal_code"], p["seq"]) for p in body["direct_predecessors"]] == [("B", 1)]
    assert body["direct_predecessors"][0]["dependency"]["basis"] == "A2依赖B1"
    assert body["direct_successors"] == []
    chain = [(c["terminal_code"], c["seq"], c["distance"]) for c in body["predecessor_chain"]]
    assert chain == [("A", 1, 2), ("B", 1, 1)]

    # 查询 A#1：无前置；直接后续=[B#1, C#1]（终端标识+序号稳定排序），A#2 为间接后续
    r = client.get(f"/api/drills/{drill}/records/A/1/dependencies")
    body = r.json()
    assert body["direct_predecessors"] == []
    assert [(s["terminal_code"], s["seq"]) for s in body["direct_successors"]] == [
        ("B", 1), ("C", 1)
    ]
    # 直接后续记录携带所属边的依据与边 id
    succ_b1 = next(s for s in body["direct_successors"] if s["terminal_code"] == "B")
    assert succ_b1["dependency"]["basis"] == "先完成A1才能处置B1"
    assert succ_b1["id"]
    assert body["predecessor_chain"] == []

    # B#1 的完整链：只有 A#1（dist1）
    r = client.get(f"/api/drills/{drill}/records/B/1/dependencies")
    body = r.json()
    assert [(c["terminal_code"], c["seq"], c["distance"])
            for c in body["predecessor_chain"]] == [("A", 1, 1)]
    assert dep_b1  # 边 id 存在


def test_resubmit_is_idempotent_and_keeps_first_basis(client):
    drill = _create_drill(client, ["P"])
    _report(client, drill, "P", [_entry(1, "P1"), _entry(2, "P2")])

    r1 = _add(client, drill, _ep("P", 1), _ep("P", 2), "首次依据")
    assert r1.status_code == 201 and r1.json()["already_exists"] is False
    dep_id = r1.json()["dependency_id"]

    # 重复提交（即便依据不同）：200 幂等，保留首次依据，不产生新边
    r2 = _add(client, drill, _ep("P", 1), _ep("P", 2), "另一个依据")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["already_exists"] is True
    assert body["dependency_id"] == dep_id
    assert body["basis"] == "首次依据"

    status = client.get(f"/api/drills/{drill}").json()
    assert status["dependency_count"] == 1


def test_revoke_and_idempotent_revoke(client):
    drill = _create_drill(client, ["R"])
    _report(client, drill, "R", [_entry(1, "R1"), _entry(2, "R2")])
    _add(client, drill, _ep("R", 1), _ep("R", 2), "依据")

    r = _remove(client, drill, _ep("R", 1), _ep("R", 2))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert r.json()["basis"] == "依据"

    # 撤销后查询，不再有边
    body = client.get(f"/api/drills/{drill}/records/R/2/dependencies").json()
    assert body["direct_predecessors"] == [] and body["predecessor_chain"] == []

    # 重复撤销幂等：deleted=false，不报错
    r = _remove(client, drill, _ep("R", 1), _ep("R", 2))
    assert r.status_code == 200
    assert r.json()["deleted"] is False

    # 撤销后可重新建立
    r = _add(client, drill, _ep("R", 1), _ep("R", 2), "重建依据")
    assert r.status_code == 201 and r.json()["already_exists"] is False


def test_self_dependency_rejected(client):
    drill = _create_drill(client, ["S"])
    _report(client, drill, "S", [_entry(1, "S1")])
    r = _add(client, drill, _ep("S", 1), _ep("S", 1), "自依赖")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "self_dependency"


def test_direct_and_indirect_cycle_rejected(client):
    drill = _create_drill(client, ["Y"])
    _report(client, drill, "Y", [_entry(1, "Y1"), _entry(2, "Y2"), _entry(3, "Y3")])
    # 链：Y#1 -> Y#2 -> Y#3（#2 依赖 #1，#3 依赖 #2）
    assert _add(client, drill, _ep("Y", 1), _ep("Y", 2), "b").status_code == 201
    assert _add(client, drill, _ep("Y", 2), _ep("Y", 3), "b").status_code == 201

    # 直接成环：#1 反过来依赖 #2
    r = _add(client, drill, _ep("Y", 2), _ep("Y", 1), "环")
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_cycle"

    # 间接成环：#1 依赖 #3（#1 已可达 #3：#1->#2->#3）
    r = _add(client, drill, _ep("Y", 3), _ep("Y", 1), "大环")
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_cycle"
    # 未写入任何边
    assert client.get(f"/api/drills/{drill}").json()["dependency_count"] == 2


def test_gap_endpoints_rejected_missing_and_lost(client):
    drill = _create_drill(client, ["G"])
    _report(client, drill, "G", [_entry(1, "G1"), _entry(3, "G3")])  # 缺 2

    # 缺号（missing）作为前置 => 404
    r = _add(client, drill, _ep("G", 2), _ep("G", 3), "缺号当前置")
    assert r.status_code == 404 and r.json()["error"]["code"] == "endpoint_record_not_found"
    # 缺号作为后续同样 404
    r = _add(client, drill, _ep("G", 1), _ep("G", 2), "缺号当后续")
    assert r.status_code == 404 and r.json()["error"]["code"] == "endpoint_record_not_found"

    # 逐号确认丢失后，lost_confirmed 仍不是现存记录，依旧不能作为端点
    r = client.post(f"/api/drills/{drill}/terminals/G/gaps/lost", json={"seq": 2, "reason": "丢"})
    assert r.status_code == 200
    r = _add(client, drill, _ep("G", 2), _ep("G", 3), "已确认丢失当端点")
    assert r.status_code == 404 and r.json()["error"]["code"] == "endpoint_record_not_found"

    # 撤销不存在的边但端点是缺号 => 404（端点先校验）
    r = _remove(client, drill, _ep("G", 2), _ep("G", 3))
    assert r.status_code == 404


def test_conflicting_record_resolves_to_original_evidence_not_addressable(client):
    """同序号冲突时，现存的是首报记录；冲突证据本身没有独立序号身份，

    无法被当作端点（端点只能解析到 records 表）。首报记录仍是合法现存记录。"""
    drill = _create_drill(client, ["K"])
    _report(client, drill, "K", [_entry(1, "原始内容")])
    # 制造冲突证据
    r = _report(client, drill, "K", [_entry(1, "冲突内容")])
    assert r["counts"]["conflict"] == 1

    # 端点 (K,1) 解析到现存的首报记录，可正常建依赖（依据落在首报记录上）
    _report(client, drill, "K", [_entry(2, "K2")])
    r = _add(client, drill, _ep("K", 1), _ep("K", 2), "首报记录为前置")
    assert r.status_code == 201, r.text
    body = client.get(f"/api/drills/{drill}/records/K/2/dependencies").json()
    assert body["direct_predecessors"][0]["content"] == "原始内容"


def test_cross_drill_rejected(client):
    d1 = _create_drill(client, ["X"])
    d2 = _create_drill(client, ["Z"])
    _report(client, d1, "X", [_entry(1, "d1-X1")])
    _report(client, d2, "Z", [_entry(1, "d2-Z1")])

    # 终端不属于本演练 => 明确跨演练错误
    r = _add(client, d2, _ep("X", 1), _ep("Z", 1), "跨演练")
    assert r.status_code == 404 and r.json()["error"]["code"] == "cross_drill_terminal"

    # 两演练登记同标识终端后，d2 下无该序号记录仍被拒（资源属于 d1）
    client.post(f"/api/drills/{d2}/terminals", json={"terminals": [{"code": "X"}]})
    r = _add(client, d2, _ep("X", 1), _ep("Z", 1), "跨演练")
    assert r.status_code == 404 and r.json()["error"]["code"] == "endpoint_record_not_found"

    # d1 数据未受影响
    assert client.get(f"/api/drills/{d1}").json()["dependency_count"] == 0


def test_invalid_basis_and_endpoint_fields(client):
    drill = _create_drill(client, ["V"])
    _report(client, drill, "V", [_entry(1, "V1"), _entry(2, "V2")])

    # 空依据 / 纯空白依据
    r = _add(client, drill, _ep("V", 1), _ep("V", 2), "   ")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_basis"
    r = _add(client, drill, _ep("V", 1), _ep("V", 2), None)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_basis"

    # 终端标识缺失/非字符串
    r = client.put(_deps_url(drill), json={
        "predecessor": {"terminal_code": "  ", "seq": 1},
        "successor": _ep("V", 2), "basis": "b"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_terminal_code"

    # 序号非法
    r = _add(client, drill, {"terminal_code": "V", "seq": 0}, _ep("V", 2), "b")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_seq"
    r = _add(client, drill, {"terminal_code": "V", "seq": "1"}, _ep("V", 2), "b")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_seq"

    # 未登记终端
    r = _add(client, drill, _ep("GHOST", 1), _ep("V", 2), "b")
    assert r.status_code == 404 and r.json()["error"]["code"] == "terminal_not_registered"

    # 查询不存在的记录
    r = client.get(f"/api/drills/{drill}/records/V/9/dependencies")
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"


def test_correction_keeps_dependency_identity(client):
    drill = _create_drill(client, ["M"])
    _report(client, drill, "M", [_entry(1, "M1原文"), _entry(2, "M2")])
    created = _add(client, drill, _ep("M", 1), _ep("M", 2), "依据").json()
    pre_record_id = created["predecessor"]["record_id"]

    # 更正前置记录（产生 rev=1，原地更新）
    r = client.put(f"/api/drills/{drill}/records/M/1",
                   json={"content": "M1更正后", "reason": "笔误"})
    assert r.status_code == 200 and r.json()["rev"] == 1

    # 依赖仍指向同一条记录（record_id 不变），查询链上内容为更正后的当前值
    body = client.get(f"/api/drills/{drill}/records/M/2/dependencies").json()
    pred = body["direct_predecessors"][0]
    assert pred["id"] == pre_record_id
    assert pred["content"] == "M1更正后"
    assert body["predecessor_chain"][0]["id"] == pre_record_id
    assert client.get(f"/api/drills/{drill}").json()["dependency_count"] == 1


def test_seal_snapshots_dependencies_and_later_changes_affect_current_only(client):
    drill = _create_drill(client, ["E"])
    _report(client, drill, "E", [_entry(1, "E1"), _entry(2, "E2"), _entry(3, "E3")])
    _add(client, drill, _ep("E", 1), _ep("E", 2), "v1依据")

    # 封存 v1：固化当时依赖与依据
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "v1"})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert len(v1["dependencies"]) == 1
    dep = v1["dependencies"][0]
    assert (dep["predecessor"]["terminal_code"], dep["predecessor"]["seq"]) == ("E", 1)
    assert (dep["successor"]["terminal_code"], dep["successor"]["seq"]) == ("E", 2)
    assert dep["basis"] == "v1依据"

    # 封存之后：新增一条依赖、撤销旧依赖 —— 只影响当前视图
    _add(client, drill, _ep("E", 2), _ep("E", 3), "v2新增依据")
    assert _remove(client, drill, _ep("E", 1), _ep("E", 2)).json()["deleted"] is True

    # 当前视图：旧边已撤，新边存在
    body = client.get(f"/api/drills/{drill}/records/E/3/dependencies").json()
    assert [(p["terminal_code"], p["seq"]) for p in body["direct_predecessors"]] == [("E", 2)]

    # 封存 v2 反映新状态
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "v2"})
    v2 = r.json()
    assert [(d["successor"]["seq"], d["basis"]) for d in v2["dependencies"]] == [
        (3, "v2新增依据")
    ]

    # 封存 v1 不变：旧依赖与依据照常可读
    old = client.get(f"/api/drills/{drill}/seals/1").json()
    assert [(d["successor"]["seq"], d["basis"]) for d in old["dependencies"]] == [
        (2, "v1依据")
    ]
    # 版本列表带依赖计数
    seals = client.get(f"/api/drills/{drill}/seals").json()["seals"]
    assert {s["version"]: s["dependency_count"] for s in seals} == {1: 1, 2: 1}


def test_complete_seals_dependencies_and_blocks_later_changes(client):
    drill = _create_drill(client, ["F"])
    _report(client, drill, "F", [_entry(1, "F1"), _entry(2, "F2")])
    _add(client, drill, _ep("F", 1), _ep("F", 2), "收官依据")

    # 完成：最终封存含当时依赖，状态原子翻转
    r = client.post(f"/api/drills/{drill}/complete", json={"completed_by": "指挥"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert len(body["seal"]["dependencies"]) == 1
    assert body["seal"]["dependencies"][0]["basis"] == "收官依据"

    # 完成后：新增 / 撤销依赖均被拒
    r = _add(client, drill, _ep("F", 1), _ep("F", 2), "重复提交")
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"
    r = _remove(client, drill, _ep("F", 1), _ep("F", 2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"

    # 历史读取不受影响：依赖查询、最终封存版本照常可读
    r = client.get(f"/api/drills/{drill}/records/F/2/dependencies")
    assert r.status_code == 200
    assert [(p["terminal_code"], p["seq"]) for p in r.json()["direct_predecessors"]] == [
        ("F", 1)
    ]
    final = client.get(f"/api/drills/{drill}/seals/1").json()
    assert final["immutable"] is True and len(final["dependencies"]) == 1


def test_concurrent_create_same_edge_idempotent(client):
    drill = _create_drill(client, ["J"])
    _report(client, drill, "J", [_entry(1, "J1"), _entry(2, "J2")])
    payload = {"predecessor": _ep("J", 1), "successor": _ep("J", 2), "basis": "并发依据"}
    url = _deps_url(drill)

    def submit(_):
        return client.put(url, json=payload)

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(submit, range(20)))

    created = [r for r in responses if r.status_code == 201]
    existing = [r for r in responses if r.status_code == 200]
    assert len(created) == 1 and len(existing) == 19
    assert len({r.json()["dependency_id"] for r in responses}) == 1
    assert client.get(f"/api/drills/{drill}").json()["dependency_count"] == 1
