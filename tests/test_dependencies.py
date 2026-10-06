"""处置依赖（record dependencies）端到端测试。

语义要点：
- 建立依赖需提交前置/后续记录的终端标识、序号及非空依据；
- 端点必须是同一演练内两条不同的已上报记录：缺号、仅有冲突证据的序号不能作为端点，
  不允许跨演练，不允许成环；
- 重复提交（同端点同依据）与重复撤销均幂等；
- 查询单条记录返回直接前置、直接后续与沿前置方向展开的完整链，按终端标识+序号稳定排序；
- 更正不改变依赖所指身份；封存版本保存当时依赖及依据，后续增删只影响当前视图；
- 完成后拒绝依赖变更，历史版本照常可读。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def _create_drill(client, terminals):
    r = client.post("/api/drills", json={"name": "依赖测试演练", "terminals": terminals})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _report(client, drill, term, seq, content, ts="2026-10-05T09:00:00Z"):
    r = client.post(
        f"/api/drills/{drill}/terminals/{term}/reports",
        json={"entries": [{"seq": seq, "occurred_at": ts, "event_type": "evt",
                           "content": content}]},
    )
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def _create_dep(client, drill, pred, succ, reason="先处置前置，后续方可采信", by="现场指挥"):
    return client.post(
        f"/api/drills/{drill}/dependencies",
        json={"predecessor": {"terminal_code": pred[0], "seq": pred[1]},
              "successor": {"terminal_code": succ[0], "seq": succ[1]},
              "reason": reason, "created_by": by},
    )


def _revoke_dep(client, drill, pred, succ):
    return client.request(
        "DELETE",
        f"/api/drills/{drill}/dependencies",
        json={"predecessor": {"terminal_code": pred[0], "seq": pred[1]},
              "successor": {"terminal_code": succ[0], "seq": succ[1]}},
    )


def _dep_keys(body):
    return [(d["predecessor"]["terminal_code"], d["predecessor"]["seq"],
             d["successor"]["terminal_code"], d["successor"]["seq"])
            for d in body["dependencies"]]


def test_dependency_create_idempotent_and_query_chain(client):
    drill = _create_drill(client, ["T1", "T2"])
    for seq in (1, 2, 3):
        _report(client, drill, "T1", seq, f"T1内容{seq}")
    _report(client, drill, "T2", 1, "T2内容1")

    # 建立三条依赖：T1#1->T1#2、T1#2->T1#3、T2#1->T1#3（跨终端同演练允许）
    r = _create_dep(client, drill, ("T1", 1), ("T1", 2))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["already_exists"] is False
    assert body["predecessor"] == {"record_id": body["predecessor"]["record_id"],
                                   "terminal_code": "T1", "seq": 1}
    assert body["successor"]["terminal_code"] == "T1" and body["successor"]["seq"] == 2
    assert body["reason"] == "先处置前置，后续方可采信"
    dep1_id = body["id"]
    assert _create_dep(client, drill, ("T1", 2), ("T1", 3)).status_code == 201
    assert _create_dep(client, drill, ("T2", 1), ("T1", 3)).status_code == 201

    # 幂等：同端点同依据重复提交 => 200，返回同一依赖，不产生新数据
    r = _create_dep(client, drill, ("T1", 1), ("T1", 2))
    assert r.status_code == 200, r.text
    assert r.json()["already_exists"] is True
    assert r.json()["id"] == dep1_id

    # 同端点但依据不同 => 409，不覆盖既有依赖
    r = _create_dep(client, drill, ("T1", 1), ("T1", 2), reason="另一条依据")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "dependency_exists"

    # 列表按 前置终端+序号 -> 后续终端+序号 稳定排序
    deps = client.get(f"/api/drills/{drill}/dependencies").json()
    assert _dep_keys(deps) == [
        ("T1", 1, "T1", 2),
        ("T1", 2, "T1", 3),
        ("T2", 1, "T1", 3),
    ]

    # 单记录视图：T1#3 的直接前置 T1#2、T2#1（按终端标识+序号排序），无后续；
    # 沿前置方向完整链 = T1#1(depth 2)、T1#2(depth 1)、T2#1(depth 1)
    r = client.get(f"/api/drills/{drill}/records/T1/3/dependencies")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["terminal_code"] == "T1" and view["seq"] == 3
    assert [(p["terminal_code"], p["seq"]) for p in view["predecessors"]] == [("T1", 2), ("T2", 1)]
    assert view["predecessors"][0]["reason"] == "先处置前置，后续方可采信"
    assert view["successors"] == []
    assert [(c["terminal_code"], c["seq"], c["depth"]) for c in view["predecessor_chain"]] == [
        ("T1", 1, 2), ("T1", 2, 1), ("T2", 1, 1),
    ]

    # T1#1：直接后续 T1#2，链为空
    view = client.get(f"/api/drills/{drill}/records/T1/1/dependencies").json()
    assert view["predecessors"] == []
    assert [(s["terminal_code"], s["seq"]) for s in view["successors"]] == [("T1", 2)]
    assert view["predecessor_chain"] == []

    # 演练状态带依赖计数
    status = client.get(f"/api/drills/{drill}").json()
    assert status["dependency_count"] == 3


def test_dependency_revoke_idempotent(client):
    drill = _create_drill(client, ["R1"])
    _report(client, drill, "R1", 1, "一")
    _report(client, drill, "R1", 2, "二")
    assert _create_dep(client, drill, ("R1", 1), ("R1", 2)).status_code == 201

    r = _revoke_dep(client, drill, ("R1", 1), ("R1", 2))
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True and r.json()["already_revoked"] is False

    # 重复撤销幂等：不报错，标记 already_revoked
    r = _revoke_dep(client, drill, ("R1", 1), ("R1", 2))
    assert r.status_code == 200
    assert r.json()["revoked"] is False and r.json()["already_revoked"] is True

    # 当前视图已移除
    assert client.get(f"/api/drills/{drill}/dependencies").json()["dependencies"] == []
    view = client.get(f"/api/drills/{drill}/records/R1/2/dependencies").json()
    assert view["predecessors"] == [] and view["predecessor_chain"] == []

    # 撤销后可重建（新依赖 id）
    r = _create_dep(client, drill, ("R1", 1), ("R1", 2), reason="重建依据")
    assert r.status_code == 201 and r.json()["reason"] == "重建依据"


def test_dependency_validation_errors(client):
    drill = _create_drill(client, ["V1"])
    other = _create_drill(client, ["V1"])  # 另一演练的同标识终端
    _report(client, drill, "V1", 2, "二")  # 故意缺 1
    _report(client, drill, "V1", 3, "三")

    # 依据为空 / 缺失
    r = _create_dep(client, drill, ("V1", 2), ("V1", 3), reason="   ")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_reason"
    r = client.post(f"/api/drills/{drill}/dependencies",
                    json={"predecessor": {"terminal_code": "V1", "seq": 2},
                          "successor": {"terminal_code": "V1", "seq": 3}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_reason"

    # 非法序号 / 空终端标识
    r = _create_dep(client, drill, ("V1", 0), ("V1", 3))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_seq"
    r = _create_dep(client, drill, ("  ", 2), ("V1", 3))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_terminal_code"

    # 前置与后续是同一条记录
    r = _create_dep(client, drill, ("V1", 2), ("V1", 2))
    assert r.status_code == 400 and r.json()["error"]["code"] == "self_dependency"

    # 缺号不能作为端点（V1#1 未上报）
    r = _create_dep(client, drill, ("V1", 1), ("V1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"

    # 仅有冲突证据的序号不能作为端点：V1#4 只有非法/未接受提交，无记录
    r = _create_dep(client, drill, ("V1", 4), ("V1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"

    # 未登记终端 / 跨演练终端 / 演练不存在
    r = _create_dep(client, drill, ("GHOST", 1), ("V1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "terminal_not_registered"
    r = client.post(f"/api/drills/{other}/dependencies",
                    json={"predecessor": {"terminal_code": "V1", "seq": 1},
                          "successor": {"terminal_code": "V1", "seq": 2},
                          "reason": "跨演练"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"
    r = _create_dep(client, "drill_nope", ("V1", 2), ("V1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "drill_not_found"

    # 撤销同样校验端点
    r = _revoke_dep(client, drill, ("V1", 1), ("V1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"

    # 查询不存在记录的依赖视图
    r = client.get(f"/api/drills/{drill}/records/V1/9/dependencies")
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"


def test_dependency_conflict_only_entry_has_no_record(client):
    """冲突证据不等于记录：冲突发生在既有记录上，未接受的迟到内容不产生新端点。"""
    drill = _create_drill(client, ["K1"])
    _report(client, drill, "K1", 1, "原始")
    _report(client, drill, "K1", 2, "二号")
    # 同序号不同内容 => 仅存冲突证据，不产生新记录
    r = _report(client, drill, "K1", 2, "被篡改")
    assert r["status"] == "conflict"
    # 端点解析到的是既有记录本身（身份不变），依赖可正常建立
    assert _create_dep(client, drill, ("K1", 1), ("K1", 2)).status_code == 201
    # 而从未被接受的序号（无任何记录）依旧不能作为端点
    r = _create_dep(client, drill, ("K1", 1), ("K1", 3))
    assert r.status_code == 404 and r.json()["error"]["code"] == "record_not_found"


def test_dependency_cycle_rejected(client):
    drill = _create_drill(client, ["C1", "C2"])
    for seq in (1, 2, 3):
        _report(client, drill, "C1", seq, f"内容{seq}")
    _report(client, drill, "C2", 1, "另一终端")

    assert _create_dep(client, drill, ("C1", 1), ("C1", 2)).status_code == 201
    assert _create_dep(client, drill, ("C1", 2), ("C1", 3)).status_code == 201
    assert _create_dep(client, drill, ("C1", 3), ("C2", 1)).status_code == 201

    # 直接成环：C2#1 -> C1#1（C1#1 的前置链已含 C2#1）
    r = _create_dep(client, drill, ("C2", 1), ("C1", 1))
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_cycle"
    # 间接成环：C1#3 -> C1#1
    r = _create_dep(client, drill, ("C1", 3), ("C1", 1))
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_cycle"
    # 反向边同样成环：C1#3 -> C1#2（2->3 已存在）
    r = _create_dep(client, drill, ("C1", 3), ("C1", 2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "dependency_cycle"

    # 撤销 C1#2 -> C1#3 后，C1#3 -> C1#2 不再成环，可以建立
    assert _revoke_dep(client, drill, ("C1", 2), ("C1", 3)).json()["revoked"] is True
    assert _create_dep(client, drill, ("C1", 3), ("C1", 2)).status_code == 201


def test_correction_preserves_dependency_identity(client):
    drill = _create_drill(client, ["M1"])
    _report(client, drill, "M1", 1, "原始一")
    _report(client, drill, "M1", 2, "原始二")
    r = _create_dep(client, drill, ("M1", 1), ("M1", 2))
    dep = r.json()
    pred_record_id = dep["predecessor"]["record_id"]

    # 更正前置记录内容：记录身份（record_id / 终端标识 / 序号）不变，依赖保持有效
    r = client.put(f"/api/drills/{drill}/records/M1/1",
                   json={"content": "更正后的一", "reason": "笔误"})
    assert r.status_code == 200 and r.json()["id"] == pred_record_id

    deps = client.get(f"/api/drills/{drill}/dependencies").json()["dependencies"]
    assert len(deps) == 1
    assert deps[0]["id"] == dep["id"]
    assert deps[0]["predecessor"]["record_id"] == pred_record_id
    view = client.get(f"/api/drills/{drill}/records/M1/2/dependencies").json()
    assert [(p["terminal_code"], p["seq"]) for p in view["predecessors"]] == [("M1", 1)]
    assert [(c["terminal_code"], c["seq"]) for c in view["predecessor_chain"]] == [("M1", 1)]


def test_seal_captures_dependencies_snapshot(client):
    drill = _create_drill(client, ["S1"])
    for seq in (1, 2, 3):
        _report(client, drill, "S1", seq, f"内容{seq}")
    _create_dep(client, drill, ("S1", 1), ("S1", 2), reason="依据甲")
    _create_dep(client, drill, ("S1", 2), ("S1", 3), reason="依据乙")

    # 封存 v1 含当时全部依赖及依据
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "首封"})
    assert r.status_code == 201, r.text
    sealed = r.json()["dependencies"]
    assert [(d["predecessor"]["seq"], d["successor"]["seq"], d["reason"])
            for d in sealed] == [(1, 2, "依据甲"), (2, 3, "依据乙")]

    # 封存后增删只影响当前视图：撤销 2->3，新建 1->3
    assert _revoke_dep(client, drill, ("S1", 2), ("S1", 3)).json()["revoked"] is True
    _create_dep(client, drill, ("S1", 1), ("S1", 3), reason="依据丙")
    current = client.get(f"/api/drills/{drill}/dependencies").json()
    assert _dep_keys(current) == [("S1", 1, "S1", 2), ("S1", 1, "S1", 3)]

    # 封存版本 1 保持不变
    v1 = client.get(f"/api/drills/{drill}/seals/1").json()
    assert [(d["predecessor"]["seq"], d["successor"]["seq"]) for d in v1["dependencies"]] == \
        [(1, 2), (2, 3)]

    # 再次封存生成 v2，反映当前视图；v1 依旧不变
    r = client.post(f"/api/drills/{drill}/seals", json={"note": "第二封"})
    assert r.status_code == 201
    assert [(d["predecessor"]["seq"], d["successor"]["seq"], d["reason"])
            for d in r.json()["dependencies"]] == [(1, 2, "依据甲"), (1, 3, "依据丙")]
    v1 = client.get(f"/api/drills/{drill}/seals/1").json()
    assert len(v1["dependencies"]) == 2


def test_complete_final_seal_includes_dependencies_then_rejects_changes(client):
    drill = _create_drill(client, ["F1"])
    _report(client, drill, "F1", 1, "一")
    _report(client, drill, "F1", 2, "二")
    _create_dep(client, drill, ("F1", 1), ("F1", 2), reason="收官依据")

    # 完成：最终封存版本与状态翻转原子生效，依赖随最终版本固化
    r = client.post(f"/api/drills/{drill}/complete",
                    json={"note": "收官", "completed_by": "张指挥"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed"
    sealed = body["seal"]["dependencies"]
    assert [(d["predecessor"]["seq"], d["successor"]["seq"], d["reason"])
            for d in sealed] == [(1, 2, "收官依据")]

    # 完成后拒绝依赖变更
    r = _create_dep(client, drill, ("F1", 2), ("F1", 1))
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"
    r = _revoke_dep(client, drill, ("F1", 1), ("F1", 2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "drill_completed"

    # 历史读取照常：当前依赖、单记录视图、最终封存版本
    deps = client.get(f"/api/drills/{drill}/dependencies").json()
    assert _dep_keys(deps) == [("F1", 1, "F1", 2)]
    view = client.get(f"/api/drills/{drill}/records/F1/2/dependencies")
    assert view.status_code == 200
    assert [(p["terminal_code"], p["seq"]) for p in view.json()["predecessors"]] == [("F1", 1)]
    seal = client.get(f"/api/drills/{drill}/seals/1").json()
    assert seal["immutable"] is True and len(seal["dependencies"]) == 1


def test_concurrent_create_same_dependency_idempotent(client):
    drill = _create_drill(client, ["P1"])
    _report(client, drill, "P1", 1, "一")
    _report(client, drill, "P1", 2, "二")

    def create(_):
        return client.post(
            f"/api/drills/{drill}/dependencies",
            json={"predecessor": {"terminal_code": "P1", "seq": 1},
                  "successor": {"terminal_code": "P1", "seq": 2},
                  "reason": "并发依据"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(create, range(12)))

    assert all(r.status_code in (200, 201) for r in responses)
    bodies = [r.json() for r in responses]
    # 恰好一个请求真正建立，其余幂等命中同一依赖
    assert sum(1 for b in bodies if not b["already_exists"]) == 1
    assert len({b["id"] for b in bodies}) == 1
    deps = client.get(f"/api/drills/{drill}/dependencies").json()["dependencies"]
    assert len(deps) == 1
