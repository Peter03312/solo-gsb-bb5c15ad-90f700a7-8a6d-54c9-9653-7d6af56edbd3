"""业务逻辑层：纯函数 + sqlite3，不依赖 FastAPI，便于直接测试。

关键并发语义：所有写操作在 `BEGIN IMMEDIATE` 事务内完成“先查后写”，
配合 busy_timeout，同一终端同一序号的并发提交被串行化，
从而保证：完全相同 => 幂等 accepted/duplicate；内容不同 => 且仅留存一次冲突。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Optional

from .db import transaction
from .errors import APIError, ConflictError, NotFoundError
from .timeutil import compute_fingerprint, normalize_occurred_at, now_iso


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------- 序列化

def _drill_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "status": row["status"],
        "completed_at": row["completed_at"],
        "completed_by": row["completed_by"],
        "final_seal_version": row["final_seal_version"],
        "created_at": row["created_at"],
    }


def _terminal_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "drill_id": row["drill_id"],
        "code": row["code"],
        "name": row["name"],
        "created_at": row["created_at"],
    }


def _record_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "drill_id": row["drill_id"],
        "terminal_id": row["terminal_id"],
        "terminal_code": row["terminal_code"],
        "seq": row["seq"],
        "occurred_at": row["occurred_at"],
        "event_type": row["event_type"],
        "content": row["content"],
        "rev": row["rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _revision_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "record_id": row["record_id"],
        "rev": row["rev"],
        "occurred_at": row["occurred_at"],
        "event_type": row["event_type"],
        "content": row["content"],
        "reason": row["reason"],
        "revised_by": row["revised_by"],
        "created_at": row["created_at"],
        "is_original": row["rev"] == 0,
    }


# ---------------------------------------------------------------- 实体查找

def _require_drill(conn: sqlite3.Connection, drill_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM drills WHERE id = ?", (drill_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"演练不存在: {drill_id}", code="drill_not_found")
    return row


def _require_drill_open(conn: sqlite3.Connection, drill_id: str) -> sqlite3.Row:
    """要求演练存在且未完成；已完成的演练拒绝一切变更类操作。"""
    drill = _require_drill(conn, drill_id)
    if drill["status"] == "completed":
        raise ConflictError(
            f"演练 {drill_id} 已完成（最终封存版本 "
            f"v{drill['final_seal_version']}），拒绝变更",
            code="drill_completed",
            details={
                "status": "completed",
                "final_seal_version": drill["final_seal_version"],
                "completed_at": drill["completed_at"],
            },
        )
    return drill


def _terminal_in_drill(conn: sqlite3.Connection, drill_id: str, terminal_code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM terminals WHERE drill_id = ? AND code = ?",
        (drill_id, terminal_code),
    ).fetchone()


def _require_terminal(conn: sqlite3.Connection, drill_id: str, terminal_code: str) -> sqlite3.Row:
    row = _terminal_in_drill(conn, drill_id, terminal_code)
    if row is not None:
        return row
    # 终端可能在别的演练登记过：给出明确的跨演练提示
    other = conn.execute(
        "SELECT d.id FROM terminals t JOIN drills d ON d.id = t.drill_id "
        "WHERE t.code = ? AND t.drill_id != ? LIMIT 1",
        (terminal_code, drill_id),
    ).fetchone()
    if other is not None:
        raise NotFoundError(
            f"终端 {terminal_code!r} 不属于演练 {drill_id}（它登记在演练 {other['id']} 下），"
            "终端不可跨演练使用",
            code="cross_drill_terminal",
        )
    raise NotFoundError(
        f"终端未在演练 {drill_id} 登记: {terminal_code}", code="terminal_not_registered"
    )


# ---------------------------------------------------------------- 演练 / 终端

def create_drill(conn: sqlite3.Connection, name: Optional[str], note: Optional[str],
                terminal_codes: list[str]) -> dict:
    with transaction(conn):
        drill_id = _new_id("drill")
        ts = now_iso()
        conn.execute(
            "INSERT INTO drills (id, name, note, created_at) VALUES (?, ?, ?, ?)",
            (drill_id, name, note, ts),
        )
        registered = []
        for code in terminal_codes:
            registered.append(_insert_terminal(conn, drill_id, code, None))
    body = _drill_dict(conn.execute("SELECT * FROM drills WHERE id = ?", (drill_id,)).fetchone())
    body["terminals"] = registered
    return body


def list_drills(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM drills ORDER BY created_at, id").fetchall()
    return [_drill_dict(r) for r in rows]


def register_terminals(conn: sqlite3.Connection, drill_id: str, items: list[dict]) -> dict:
    with transaction(conn):
        _require_drill_open(conn, drill_id)
        # 同一批内重复也要报冲突
        seen: set[str] = set()
        duplicates = []
        registered = []
        for item in items:
            code = item["code"]
            if code in seen:
                duplicates.append({"code": code, "reason": "duplicate_in_batch"})
                continue
            seen.add(code)
            existing = _terminal_in_drill(conn, drill_id, code)
            if existing is not None:
                duplicates.append({"code": code, "reason": "already_registered"})
                continue
            registered.append(_insert_terminal(conn, drill_id, code, item.get("name")))
    return {"drill_id": drill_id, "registered": registered, "duplicates": duplicates}


def _insert_terminal(conn: sqlite3.Connection, drill_id: str, code: str,
                     name: Optional[str]) -> dict:
    if not isinstance(code, str) or not code.strip():
        raise APIError("终端标识 code 必须是非空字符串", code="invalid_terminal_code")
    terminal_id = _new_id("term")
    conn.execute(
        "INSERT INTO terminals (id, drill_id, code, name, created_at) VALUES (?, ?, ?, ?, ?)",
        (terminal_id, drill_id, code.strip(), name, now_iso()),
    )
    row = conn.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
    return _terminal_dict(row)


def list_terminals(conn: sqlite3.Connection, drill_id: str) -> list[dict]:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        rows = conn.execute(
            "SELECT * FROM terminals WHERE drill_id = ? ORDER BY created_at, code", (drill_id,)
        ).fetchall()
    return [_terminal_dict(r) for r in rows]


# ---------------------------------------------------------------- 上报

def _validate_seq(value: Any) -> int:
    # bool 是 int 的子类，须显式排除
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise APIError("序号 seq 必须是 >= 1 的整数", code="invalid_seq")
    return value


def submit_reports(conn: sqlite3.Connection, drill_id: str, terminal_code: str,
                   entries: list[dict]) -> dict:
    with transaction(conn):
        drill = _require_drill(conn, drill_id)
        completed = drill["status"] == "completed"
        terminal = _require_terminal(conn, drill_id, terminal_code)
        terminal_id = terminal["id"]
        results: list[dict] = []
        counts = {"accepted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}

        for idx, entry in enumerate(entries):
            result: dict[str, Any] = {"index": idx, "seq": entry.get("seq")}
            try:
                seq = _validate_seq(entry.get("seq"))
                occurred_at = normalize_occurred_at(entry.get("occurred_at"))
                event_type = entry.get("event_type")
                if not isinstance(event_type, str) or not event_type.strip():
                    raise APIError("event_type 必须是非空字符串", code="invalid_event_type")
                event_type = event_type.strip()
                content = entry.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise APIError("content 必须是非空字符串", code="invalid_content")
                fingerprint = compute_fingerprint(occurred_at, event_type, content)
            except APIError as exc:
                result.update(status="rejected", error={"code": exc.code, "message": exc.message})
                counts["rejected"] += 1
                results.append(result)
                continue

            existing = conn.execute(
                "SELECT * FROM records WHERE terminal_id = ? AND seq = ?",
                (terminal_id, seq),
            ).fetchone()

            if existing is None:
                if completed:
                    # 演练已完成：非重复上报一律拒绝，不写入任何数据
                    result.update(status="rejected", error={
                        "code": "drill_completed",
                        "message": "演练已完成，非重复上报被拒绝",
                    })
                    counts["rejected"] += 1
                    results.append(result)
                    continue
                record_id = _new_id("rec")
                ts = now_iso()
                conn.execute(
                    "INSERT INTO records (id, drill_id, terminal_id, seq, occurred_at, "
                    "event_type, content, fingerprint, rev, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (record_id, drill_id, terminal_id, seq, occurred_at, event_type,
                     content, fingerprint, ts, ts),
                )
                conn.execute(
                    "INSERT INTO record_revisions (id, record_id, drill_id, rev, occurred_at, "
                    "event_type, content, fingerprint, reason, revised_by, created_at) "
                    "VALUES (?, ?, ?, 0, ?, ?, ?, ?, NULL, NULL, ?)",
                    (_new_id("rev"), record_id, drill_id, occurred_at, event_type,
                     content, fingerprint, ts),
                )
                # 迟到上报恰好补上了此前“确认丢失”的缺号：把该缺号标记为已闭合
                conn.execute(
                    "UPDATE lost_gaps SET resolved = 1, resolved_at = ? "
                    "WHERE terminal_id = ? AND seq = ? AND resolved = 0",
                    (ts, terminal_id, seq),
                )
                result.update(status="accepted", record_id=record_id)
                counts["accepted"] += 1
            else:
                result["record_id"] = existing["id"]
                if existing["fingerprint"] == fingerprint:
                    # 完整内容完全一致：幂等返回，不留任何新数据（完成后亦然）
                    result.update(status="duplicate")
                    counts["duplicate"] += 1
                elif completed:
                    # 演练已完成：冲突内容不得新增冲突证据，直接拒绝
                    result.update(status="rejected", error={
                        "code": "drill_completed",
                        "message": "演练已完成，冲突内容被拒绝且不再留存冲突证据",
                    })
                    counts["rejected"] += 1
                else:
                    conflict_id = _new_id("cfl")
                    incoming = _entry_json(occurred_at, event_type, content)
                    existing_body = _entry_json(
                        existing["occurred_at"], existing["event_type"], existing["content"]
                    )
                    conn.execute(
                        "INSERT INTO conflicts (id, drill_id, terminal_id, record_id, seq, "
                        "incoming, existing, incoming_fingerprint, existing_fingerprint, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (conflict_id, drill_id, terminal_id, existing["id"], seq,
                         incoming, existing_body, fingerprint, existing["fingerprint"], now_iso()),
                    )
                    result.update(status="conflict", conflict_id=conflict_id)
                    counts["conflict"] += 1
            results.append(result)

    return {
        "drill_id": drill_id,
        "terminal_code": terminal_code,
        "counts": counts,
        "results": results,
    }


def _entry_json(occurred_at: str, event_type: str, content: str) -> str:
    import json
    return json.dumps(
        {"occurred_at": occurred_at, "event_type": event_type, "content": content},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- 时间线与缺号

def _timeline_rows(conn: sqlite3.Connection, drill_id: str) -> list[sqlite3.Row]:
    # 稳定排序：发生时间 → 终端标识 → 序号
    return conn.execute(
        "SELECT r.*, t.code AS terminal_code FROM records r "
        "JOIN terminals t ON t.id = r.terminal_id "
        "WHERE r.drill_id = ? "
        "ORDER BY r.occurred_at ASC, t.code ASC, r.seq ASC",
        (drill_id,),
    ).fetchall()


def _gaps_for(conn: sqlite3.Connection, drill_id: str) -> tuple[list[dict], list[sqlite3.Row]]:
    """返回 (显式缺号列表, 终端行)。缺号状态为 missing / lost_confirmed。"""
    terminals = conn.execute(
        "SELECT * FROM terminals WHERE drill_id = ? ORDER BY code", (drill_id,)
    ).fetchall()
    gaps: list[dict] = []
    for term in terminals:
        seq_rows = conn.execute(
            "SELECT seq FROM records WHERE terminal_id = ?", (term["id"],)
        ).fetchall()
        seqs = {r["seq"] for r in seq_rows}
        if not seqs:
            continue
        lost_rows = conn.execute(
            "SELECT * FROM lost_gaps WHERE terminal_id = ? AND resolved = 0", (term["id"],)
        ).fetchall()
        lost = {r["seq"]: r for r in lost_rows}
        for seq in range(1, max(seqs) + 1):
            if seq in seqs:
                continue
            if seq in lost:
                lr = lost[seq]
                gaps.append({
                    "terminal_id": term["id"],
                    "terminal_code": term["code"],
                    "seq": seq,
                    "state": "lost_confirmed",
                    "reason": lr["reason"],
                    "confirmed_by": lr["confirmed_by"],
                    "confirmed_at": lr["created_at"],
                })
            else:
                gaps.append({
                    "terminal_id": term["id"],
                    "terminal_code": term["code"],
                    "seq": seq,
                    "state": "missing",
                })
    gaps.sort(key=lambda g: (g["terminal_code"], g["seq"]))
    return gaps, terminals


def get_timeline(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        rows = _timeline_rows(conn, drill_id)
        gaps, _ = _gaps_for(conn, drill_id)
    open_gaps = [g for g in gaps if g["state"] == "missing"]
    return {
        "drill_id": drill_id,
        "records": [_record_dict(r) for r in rows],
        "gaps": gaps,
        "open_gap_count": len(open_gaps),
        "record_count": len(rows),
    }


def list_gaps(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        gaps, _ = _gaps_for(conn, drill_id)
    return {
        "drill_id": drill_id,
        "gaps": gaps,
        "open_gaps": [g for g in gaps if g["state"] == "missing"],
        "confirmed_lost": [g for g in gaps if g["state"] == "lost_confirmed"],
    }


def get_drill_status(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        drill = _require_drill(conn, drill_id)
        rows = _timeline_rows(conn, drill_id)
        gaps, terminals = _gaps_for(conn, drill_id)
        seal_rows = conn.execute(
            "SELECT * FROM seals WHERE drill_id = ? ORDER BY version", (drill_id,)
        ).fetchall()
        dep_count = conn.execute(
            "SELECT COUNT(*) AS c FROM record_dependencies WHERE drill_id = ?", (drill_id,)
        ).fetchone()["c"]
    open_gaps = [g for g in gaps if g["state"] == "missing"]
    return {
        "drill": _drill_dict(drill),
        "terminal_count": len(terminals),
        "record_count": len(rows),
        "dependency_count": dep_count,
        "open_gap_count": len(open_gaps),
        "open_gaps": open_gaps,
        "sealed": len(seal_rows) > 0,
        "seal_versions": [s["version"] for s in seal_rows],
    }


# ---------------------------------------------------------------- 确认丢失

def mark_lost(conn: sqlite3.Connection, drill_id: str, terminal_code: str,
              seq: Any, reason: Optional[str], confirmed_by: Optional[str]) -> dict:
    with transaction(conn):
        _require_drill_open(conn, drill_id)
        terminal = _require_terminal(conn, drill_id, terminal_code)
        seq = _validate_seq(seq)

        record = conn.execute(
            "SELECT id FROM records WHERE terminal_id = ? AND seq = ?",
            (terminal["id"], seq),
        ).fetchone()
        if record is not None:
            raise ConflictError(
                f"终端 {terminal_code} 序号 {seq} 已上报，不是缺号，无需确认丢失",
                code="gap_already_filled",
            )

        existing = conn.execute(
            "SELECT * FROM lost_gaps WHERE terminal_id = ? AND seq = ?",
            (terminal["id"], seq),
        ).fetchone()
        if existing is not None:
            if existing["resolved"]:
                raise ConflictError(
                    f"序号 {seq} 的丢失确认已被迟到上报补齐", code="gap_already_filled"
                )
            # 幂等：重复确认直接返回既有确认
            return {
                "drill_id": drill_id,
                "terminal_code": terminal_code,
                "seq": seq,
                "state": "lost_confirmed",
                "reason": existing["reason"],
                "confirmed_by": existing["confirmed_by"],
                "confirmed_at": existing["created_at"],
                "already_confirmed": True,
            }

        max_row = conn.execute(
            "SELECT MAX(seq) AS m FROM records WHERE terminal_id = ?", (terminal["id"],)
        ).fetchone()
        max_seq = max_row["m"]
        if max_seq is None or seq > max_seq:
            raise ConflictError(
                f"终端 {terminal_code} 当前不存在序号 {seq} 对应的缺号"
                "（只能确认已上报最大序号之内的缺号）",
                code="not_a_gap",
            )

        gap_id = _new_id("gap")
        ts = now_iso()
        conn.execute(
            "INSERT INTO lost_gaps (id, drill_id, terminal_id, seq, reason, confirmed_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gap_id, drill_id, terminal["id"], seq, reason, confirmed_by, ts),
        )
    return {
        "drill_id": drill_id,
        "terminal_code": terminal_code,
        "seq": seq,
        "state": "lost_confirmed",
        "reason": reason,
        "confirmed_by": confirmed_by,
        "confirmed_at": ts,
        "already_confirmed": False,
    }


# ---------------------------------------------------------------- 更正与历史

def _find_record(conn: sqlite3.Connection, drill_id: str,
                 terminal: sqlite3.Row, seq: Any) -> sqlite3.Row:
    seq = _validate_seq(seq)
    row = conn.execute(
        "SELECT * FROM records WHERE terminal_id = ? AND seq = ?",
        (terminal["id"], seq),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"终端 {terminal['code']} 序号 {seq} 的记录不存在，无法更正",
            code="record_not_found",
        )
    return row


def correct_record(conn: sqlite3.Connection, drill_id: str, terminal_code: str,
                   seq: Any, payload: dict) -> dict:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise APIError("更正理由 reason 必须是非空字符串", code="invalid_reason")

    with transaction(conn):
        _require_drill_open(conn, drill_id)
        terminal = _require_terminal(conn, drill_id, terminal_code)
        record = _find_record(conn, drill_id, terminal, seq)

        occurred_at = record["occurred_at"]
        if payload.get("occurred_at") is not None:
            occurred_at = normalize_occurred_at(payload["occurred_at"])
        event_type = record["event_type"]
        if payload.get("event_type") is not None:
            event_type = payload["event_type"]
            if not isinstance(event_type, str) or not event_type.strip():
                raise APIError("event_type 必须是非空字符串", code="invalid_event_type")
            event_type = event_type.strip()
        content = record["content"]
        if payload.get("content") is not None:
            content = payload["content"]
            if not isinstance(content, str) or not content.strip():
                raise APIError("content 必须是非空字符串", code="invalid_content")

        fingerprint = compute_fingerprint(occurred_at, event_type, content)
        if fingerprint == record["fingerprint"]:
            raise APIError(
                "更正内容与当前记录完全一致，未产生新版本", code="no_change"
            )

        new_rev = record["rev"] + 1
        ts = now_iso()
        conn.execute(
            "INSERT INTO record_revisions (id, record_id, drill_id, rev, occurred_at, "
            "event_type, content, fingerprint, reason, revised_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("rev"), record["id"], drill_id, new_rev, occurred_at, event_type,
             content, fingerprint, reason.strip(), payload.get("revised_by"), ts),
        )
        conn.execute(
            "UPDATE records SET occurred_at = ?, event_type = ?, content = ?, "
            "fingerprint = ?, rev = ?, updated_at = ? WHERE id = ?",
            (occurred_at, event_type, content, fingerprint, new_rev, ts, record["id"]),
        )
        updated = conn.execute(
            "SELECT r.*, t.code AS terminal_code FROM records r "
            "JOIN terminals t ON t.id = r.terminal_id WHERE r.id = ?",
            (record["id"],),
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM record_revisions WHERE record_id = ? ORDER BY rev", (record["id"],)
        ).fetchall()

    body = _record_dict(updated)
    body["terminal_code"] = terminal_code
    body["revisions"] = [_revision_dict(r) for r in history]
    body["note"] = "封存版本中的原文不受影响；仅当前时间线产生新版本"
    return body


def get_record(conn: sqlite3.Connection, drill_id: str, terminal_code: str, seq: Any) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        terminal = _require_terminal(conn, drill_id, terminal_code)
        seq = _validate_seq(seq)
        row = conn.execute(
            "SELECT r.*, t.code AS terminal_code FROM records r "
            "JOIN terminals t ON t.id = r.terminal_id "
            "WHERE r.terminal_id = ? AND r.seq = ?",
            (terminal["id"], seq),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"终端 {terminal_code} 序号 {seq} 的记录不存在", code="record_not_found"
            )
        history = conn.execute(
            "SELECT * FROM record_revisions WHERE record_id = ? ORDER BY rev", (row["id"],)
        ).fetchall()
    body = _record_dict(row)
    body["revisions"] = [_revision_dict(r) for r in history]
    return body


# ---------------------------------------------------------------- 冲突证据

def list_conflicts(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        rows = conn.execute(
            "SELECT c.*, t.code AS terminal_code FROM conflicts c "
            "JOIN terminals t ON t.id = c.terminal_id "
            "WHERE c.drill_id = ? ORDER BY c.created_at, t.code, c.seq",
            (drill_id,),
        ).fetchall()
    return {
        "drill_id": drill_id,
        "conflicts": [
            {
                "id": r["id"],
                "terminal_code": r["terminal_code"],
                "record_id": r["record_id"],
                "seq": r["seq"],
                "incoming": r["incoming"],
                "existing": r["existing"],
                "incoming_fingerprint": r["incoming_fingerprint"],
                "existing_fingerprint": r["existing_fingerprint"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 处置依赖

def _dependency_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "drill_id": row["drill_id"],
        "predecessor": {
            "record_id": row["pred_record_id"],
            "terminal_code": row["pred_terminal_code"],
            "seq": row["pred_seq"],
        },
        "successor": {
            "record_id": row["succ_record_id"],
            "terminal_code": row["succ_terminal_code"],
            "seq": row["succ_seq"],
        },
        "reason": row["reason"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _resolve_dependency_endpoint(conn: sqlite3.Connection, drill_id: str,
                                 endpoint: dict) -> sqlite3.Row:
    """把 {terminal_code, seq} 解析为本演练内一条已上报记录。

    缺号、从未上报或仅有冲突证据的序号都没有记录，不能作为依赖端点。
    """
    code = endpoint.get("terminal_code")
    if not isinstance(code, str) or not code.strip():
        raise APIError("终端标识 terminal_code 必须是非空字符串", code="invalid_terminal_code")
    terminal = _require_terminal(conn, drill_id, code.strip())
    seq = _validate_seq(endpoint.get("seq"))
    row = conn.execute(
        "SELECT r.*, t.code AS terminal_code FROM records r "
        "JOIN terminals t ON t.id = r.terminal_id "
        "WHERE r.terminal_id = ? AND r.seq = ?",
        (terminal["id"], seq),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"终端 {code.strip()} 序号 {seq} 没有已上报记录"
            "（缺号或仅有冲突证据的条目不能作为依赖端点）",
            code="record_not_found",
        )
    return row


def _predecessor_chain(conn: sqlite3.Connection, record_id: str) -> dict[str, int]:
    """沿前置方向 BFS 展开完整链，返回 {祖先 record_id: 最小深度}（不含起点自身）。"""
    depths: dict[str, int] = {}
    frontier = [record_id]
    depth = 0
    while frontier:
        depth += 1
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"SELECT DISTINCT pred_record_id FROM record_dependencies "
            f"WHERE succ_record_id IN ({placeholders})",
            frontier,
        ).fetchall()
        nxt: list[str] = []
        for r in rows:
            pid = r["pred_record_id"]
            if pid == record_id or pid in depths:
                continue
            depths[pid] = depth
            nxt.append(pid)
        frontier = nxt
    return depths


def create_dependency(conn: sqlite3.Connection, drill_id: str, predecessor: dict,
                      successor: dict, reason: Any, created_by: Optional[str]) -> dict:
    """建立处置依赖（前置 -> 后续）。重复提交同一对端点且依据一致时幂等返回。"""
    if not isinstance(reason, str) or not reason.strip():
        raise APIError("依赖依据 reason 必须是非空字符串", code="invalid_reason")
    reason = reason.strip()

    with transaction(conn):
        _require_drill_open(conn, drill_id)
        pred = _resolve_dependency_endpoint(conn, drill_id, predecessor)
        succ = _resolve_dependency_endpoint(conn, drill_id, successor)
        if pred["id"] == succ["id"]:
            raise APIError("前置与后续必须是两条不同的记录", code="self_dependency")

        existing = conn.execute(
            "SELECT * FROM record_dependencies WHERE pred_record_id = ? AND succ_record_id = ?",
            (pred["id"], succ["id"]),
        ).fetchone()
        if existing is not None:
            if existing["reason"] == reason:
                # 幂等：同一对端点、同一依据的重复提交直接返回既有依赖
                body = _dependency_dict(existing)
                body["already_exists"] = True
                return body
            raise ConflictError(
                "该前置/后续记录对之间已存在依据不同的依赖；如需变更请先撤销再重建",
                code="dependency_exists",
                details={"dependency": _dependency_dict(existing)},
            )

        # 环检测：若“后续记录”已位于“前置记录”的前置链上，则 pred->succ 会闭合出环
        if succ["id"] in _predecessor_chain(conn, pred["id"]):
            raise ConflictError(
                f"依赖 {pred['terminal_code']}#{pred['seq']} -> "
                f"{succ['terminal_code']}#{succ['seq']} 会与既有依赖形成环，已拒绝",
                code="dependency_cycle",
                details={
                    "predecessor": {"terminal_code": pred["terminal_code"], "seq": pred["seq"]},
                    "successor": {"terminal_code": succ["terminal_code"], "seq": succ["seq"]},
                },
            )

        dep_id = _new_id("dep")
        ts = now_iso()
        conn.execute(
            "INSERT INTO record_dependencies (id, drill_id, pred_record_id, succ_record_id, "
            "pred_terminal_code, pred_seq, succ_terminal_code, succ_seq, reason, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dep_id, drill_id, pred["id"], succ["id"], pred["terminal_code"], pred["seq"],
             succ["terminal_code"], succ["seq"], reason, created_by, ts),
        )
        row = conn.execute(
            "SELECT * FROM record_dependencies WHERE id = ?", (dep_id,)
        ).fetchone()
    body = _dependency_dict(row)
    body["already_exists"] = False
    return body


def revoke_dependency(conn: sqlite3.Connection, drill_id: str, predecessor: dict,
                      successor: dict) -> dict:
    """撤销处置依赖。重复撤销幂等：依赖不存在时返回 revoked=False。"""
    with transaction(conn):
        _require_drill_open(conn, drill_id)
        pred = _resolve_dependency_endpoint(conn, drill_id, predecessor)
        succ = _resolve_dependency_endpoint(conn, drill_id, successor)
        row = conn.execute(
            "SELECT * FROM record_dependencies WHERE pred_record_id = ? AND succ_record_id = ?",
            (pred["id"], succ["id"]),
        ).fetchone()
        revoked = row is not None
        if revoked:
            conn.execute("DELETE FROM record_dependencies WHERE id = ?", (row["id"],))
    return {
        "drill_id": drill_id,
        "predecessor": {"record_id": pred["id"], "terminal_code": pred["terminal_code"],
                        "seq": pred["seq"]},
        "successor": {"record_id": succ["id"], "terminal_code": succ["terminal_code"],
                      "seq": succ["seq"]},
        "revoked": revoked,
        "already_revoked": not revoked,
    }


def list_dependencies(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        rows = conn.execute(
            "SELECT * FROM record_dependencies WHERE drill_id = ? "
            "ORDER BY pred_terminal_code, pred_seq, succ_terminal_code, succ_seq",
            (drill_id,),
        ).fetchall()
    return {"drill_id": drill_id,
            "dependencies": [_dependency_dict(r) for r in rows]}


def get_record_dependencies(conn: sqlite3.Connection, drill_id: str,
                            terminal_code: str, seq: Any) -> dict:
    """单条记录的依赖视图：直接前置、直接后续、沿前置方向展开的完整链。

    三个列表均按终端标识 + 序号稳定排序。
    """
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        terminal = _require_terminal(conn, drill_id, terminal_code)
        seq = _validate_seq(seq)
        record = conn.execute(
            "SELECT * FROM records WHERE terminal_id = ? AND seq = ?",
            (terminal["id"], seq),
        ).fetchone()
        if record is None:
            raise NotFoundError(
                f"终端 {terminal_code} 序号 {seq} 的记录不存在", code="record_not_found"
            )
        pred_rows = conn.execute(
            "SELECT * FROM record_dependencies WHERE succ_record_id = ? "
            "ORDER BY pred_terminal_code, pred_seq",
            (record["id"],),
        ).fetchall()
        succ_rows = conn.execute(
            "SELECT * FROM record_dependencies WHERE pred_record_id = ? "
            "ORDER BY succ_terminal_code, succ_seq",
            (record["id"],),
        ).fetchall()
        chain = _predecessor_chain(conn, record["id"])
        chain_records: list[dict] = []
        if chain:
            placeholders = ",".join("?" for _ in chain)
            rows = conn.execute(
                f"SELECT r.id, r.seq, t.code AS terminal_code FROM records r "
                f"JOIN terminals t ON t.id = r.terminal_id WHERE r.id IN ({placeholders})",
                list(chain),
            ).fetchall()
            chain_records = sorted(
                (
                    {
                        "record_id": r["id"],
                        "terminal_code": r["terminal_code"],
                        "seq": r["seq"],
                        "depth": chain[r["id"]],
                    }
                    for r in rows
                ),
                key=lambda x: (x["terminal_code"], x["seq"]),
            )

    def _edge(row: sqlite3.Row, direction: str) -> dict:
        endpoint = row["predecessor"] if direction == "predecessor" else row["successor"]
        return {
            "dependency_id": row["id"],
            "record_id": endpoint["record_id"],
            "terminal_code": endpoint["terminal_code"],
            "seq": endpoint["seq"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    return {
        "drill_id": drill_id,
        "record_id": record["id"],
        "terminal_code": terminal["code"],
        "seq": seq,
        "predecessors": [_edge(_dependency_dict(r), "predecessor") for r in pred_rows],
        "successors": [_edge(_dependency_dict(r), "successor") for r in succ_rows],
        "predecessor_chain": chain_records,
    }


# ---------------------------------------------------------------- 封存

def _create_seal_snapshot(conn: sqlite3.Connection, drill_id: str,
                          note: Optional[str]) -> int:
    """在当前事务内生成一个只追加的不可变封存版本，返回版本号。

    调用方必须已确认缺号全部闭合；本函数与调用方共享同一事务，
    保证“校验缺号 + 写快照（+ 完成状态翻转）”原子生效。
    """
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM seals WHERE drill_id = ?", (drill_id,)
    ).fetchone()
    version = version_row["v"] + 1
    seal_id = _new_id("seal")
    ts = now_iso()
    conn.execute(
        "INSERT INTO seals (id, drill_id, version, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (seal_id, drill_id, version, note, ts),
    )

    # 时间线快照（封存时刻的当前值）
    for r in _timeline_rows(conn, drill_id):
        conn.execute(
            "INSERT INTO seal_records (seal_id, drill_id, record_id, terminal_id, "
            "terminal_code, seq, occurred_at, event_type, content, rev) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seal_id, drill_id, r["id"], r["terminal_id"], r["terminal_code"],
             r["seq"], r["occurred_at"], r["event_type"], r["content"], r["rev"]),
        )
    # 历次修订原文快照（封存版本可完整追溯）
    rev_rows = conn.execute(
        "SELECT vr.* FROM record_revisions vr JOIN records r ON r.id = vr.record_id "
        "WHERE r.drill_id = ? ORDER BY vr.record_id, vr.rev",
        (drill_id,),
    ).fetchall()
    for vr in rev_rows:
        conn.execute(
            "INSERT INTO seal_revisions (seal_id, record_id, rev, occurred_at, event_type, "
            "content, reason, revised_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seal_id, vr["record_id"], vr["rev"], vr["occurred_at"], vr["event_type"],
             vr["content"], vr["reason"], vr["revised_by"], vr["created_at"]),
        )
    # 闭合证据：当时确认丢失的缺号，以及曾经确认丢失、后被迟到上报补齐的缺号
    gap_rows = conn.execute(
        "SELECT lg.*, t.code AS terminal_code FROM lost_gaps lg "
        "JOIN terminals t ON t.id = lg.terminal_id WHERE lg.drill_id = ?",
        (drill_id,),
    ).fetchall()
    for g in gap_rows:
        conn.execute(
            "INSERT INTO seal_gaps (seal_id, terminal_id, terminal_code, seq, closed_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (seal_id, g["terminal_id"], g["terminal_code"], g["seq"],
             "filled" if g["resolved"] else "lost"),
        )
    # 处置依赖快照：封存时刻的依赖边及依据；后续增删只影响当前视图
    dep_rows = conn.execute(
        "SELECT * FROM record_dependencies WHERE drill_id = ? "
        "ORDER BY pred_terminal_code, pred_seq, succ_terminal_code, succ_seq",
        (drill_id,),
    ).fetchall()
    for d in dep_rows:
        conn.execute(
            "INSERT INTO seal_dependencies (seal_id, pred_terminal_code, pred_seq, "
            "succ_terminal_code, succ_seq, reason, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seal_id, d["pred_terminal_code"], d["pred_seq"], d["succ_terminal_code"],
             d["succ_seq"], d["reason"], d["created_by"], d["created_at"]),
        )
    return version


def _open_gaps_or_raise(conn: sqlite3.Connection, drill_id: str) -> None:
    gaps, _ = _gaps_for(conn, drill_id)
    open_gaps = [g for g in gaps if g["state"] == "missing"]
    if open_gaps:
        raise ConflictError(
            "仍有缺号未闭合（未补齐也未逐号确认丢失），不能封存",
            code="unresolved_gaps",
            details={"open_gaps": open_gaps},
        )


def seal_drill(conn: sqlite3.Connection, drill_id: str, note: Optional[str]) -> dict:
    with transaction(conn):
        _require_drill_open(conn, drill_id)
        _open_gaps_or_raise(conn, drill_id)
        version = _create_seal_snapshot(conn, drill_id, note)
    return get_seal(conn, drill_id, version)


# ---------------------------------------------------------------- 完成演练

def complete_drill(conn: sqlite3.Connection, drill_id: str, note: Optional[str],
                   completed_by: Optional[str]) -> dict:
    """主动完成演练：缺号全部闭合时，原子生成最终封存版本并转为已完成。

    - 条件不满足：409 unresolved_gaps，不生成任何封存版本；
    - 幂等：已完成的演练重复调用返回同一最终版本，不产生新版本。
    """
    with transaction(conn):
        drill = _require_drill(conn, drill_id)
        if drill["status"] == "completed":
            # 重复完成：直接返回已生成的最终版本，忽略本次 note/completed_by
            version = drill["final_seal_version"]
            already_completed = True
        else:
            _open_gaps_or_raise(conn, drill_id)
            version = _create_seal_snapshot(conn, drill_id, note)
            conn.execute(
                "UPDATE drills SET status = 'completed', completed_at = ?, "
                "completed_by = ?, final_seal_version = ? WHERE id = ?",
                (now_iso(), completed_by, version, drill_id),
            )
            already_completed = False

    seal = get_seal(conn, drill_id, version)
    with transaction(conn, immediate=False):
        drill = _require_drill(conn, drill_id)
    return {
        "drill_id": drill_id,
        "status": "completed",
        "already_completed": already_completed,
        "completed_at": drill["completed_at"],
        "completed_by": drill["completed_by"],
        "final_seal_version": version,
        "seal": seal,
    }


def list_seals(conn: sqlite3.Connection, drill_id: str) -> dict:
    with transaction(conn, immediate=False):
        drill = _require_drill(conn, drill_id)
        rows = conn.execute(
            "SELECT * FROM seals WHERE drill_id = ? ORDER BY version", (drill_id,)
        ).fetchall()
        out = []
        for s in rows:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM seal_records WHERE seal_id = ?", (s["id"],)
            ).fetchone()["c"]
            out.append({
                "seal_id": s["id"],
                "version": s["version"],
                "note": s["note"],
                "created_at": s["created_at"],
                "record_count": cnt,
                "is_final": drill["final_seal_version"] == s["version"],
            })
    return {"drill_id": drill_id, "seals": out}


def _require_seal(conn: sqlite3.Connection, drill_id: str, version: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM seals WHERE drill_id = ? AND version = ?", (drill_id, version)
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"演练 {drill_id} 不存在封存版本 {version}", code="seal_version_not_found"
        )
    return row


def get_seal(conn: sqlite3.Connection, drill_id: str, version: int) -> dict:
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        seal = _require_seal(conn, drill_id, version)
        rec_rows = conn.execute(
            "SELECT * FROM seal_records WHERE seal_id = ? "
            "ORDER BY occurred_at, terminal_code, seq",
            (seal["id"],),
        ).fetchall()
        rev_rows = conn.execute(
            "SELECT * FROM seal_revisions WHERE seal_id = ? ORDER BY record_id, rev",
            (seal["id"],),
        ).fetchall()
        gap_rows = conn.execute(
            "SELECT * FROM seal_gaps WHERE seal_id = ? ORDER BY terminal_code, seq",
            (seal["id"],),
        ).fetchall()
        dep_rows = conn.execute(
            "SELECT * FROM seal_dependencies WHERE seal_id = ? "
            "ORDER BY pred_terminal_code, pred_seq, succ_terminal_code, succ_seq",
            (seal["id"],),
        ).fetchall()

    revs_by_record: dict[str, list[dict]] = {}
    for vr in rev_rows:
        revs_by_record.setdefault(vr["record_id"], []).append({
            "rev": vr["rev"],
            "occurred_at": vr["occurred_at"],
            "event_type": vr["event_type"],
            "content": vr["content"],
            "reason": vr["reason"],
            "revised_by": vr["revised_by"],
            "created_at": vr["created_at"],
            "is_original": vr["rev"] == 0,
        })

    records = []
    for r in rec_rows:
        records.append({
            "record_id": r["record_id"],
            "terminal_id": r["terminal_id"],
            "terminal_code": r["terminal_code"],
            "seq": r["seq"],
            "occurred_at": r["occurred_at"],
            "event_type": r["event_type"],
            "content": r["content"],
            "rev_at_seal": r["rev"],
            "revisions": revs_by_record.get(r["record_id"], []),
        })

    return {
        "drill_id": drill_id,
        "seal_id": seal["id"],
        "version": seal["version"],
        "note": seal["note"],
        "created_at": seal["created_at"],
        "immutable": True,
        "records": records,
        "closed_gaps": [
            {
                "terminal_code": g["terminal_code"],
                "seq": g["seq"],
                "closed_by": g["closed_by"],
            }
            for g in gap_rows
        ],
        "dependencies": [
            {
                "predecessor": {"terminal_code": d["pred_terminal_code"], "seq": d["pred_seq"]},
                "successor": {"terminal_code": d["succ_terminal_code"], "seq": d["succ_seq"]},
                "reason": d["reason"],
                "created_by": d["created_by"],
                "created_at": d["created_at"],
            }
            for d in dep_rows
        ],
    }
