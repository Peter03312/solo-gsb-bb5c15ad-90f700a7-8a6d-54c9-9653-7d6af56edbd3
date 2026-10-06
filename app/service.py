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
            "SELECT COUNT(*) AS c FROM dependencies WHERE drill_id = ?", (drill_id,)
        ).fetchone()["c"]
    open_gaps = [g for g in gaps if g["state"] == "missing"]
    return {
        "drill": _drill_dict(drill),
        "terminal_count": len(terminals),
        "record_count": len(rows),
        "open_gap_count": len(open_gaps),
        "open_gaps": open_gaps,
        "dependency_count": dep_count,
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

def _resolve_dependency_endpoint(conn: sqlite3.Connection, drill_id: str,
                                 endpoint: Any, label: str) -> sqlite3.Row:
    """把 {terminal_code, seq} 解析为本演练内的现存记录。

    只有现存记录能作为依赖端点：缺号无记录；同终端同序号内容冲突时，
    系统只留存首报记录与冲突证据，冲突证据本身不是记录，同样不能作为端点。
    """
    if not isinstance(endpoint, dict):
        raise APIError(f"{label} 必须是包含 terminal_code 与 seq 的对象",
                       code="invalid_dependency_endpoint")
    code = endpoint.get("terminal_code")
    if not isinstance(code, str) or not code.strip():
        raise APIError(f"{label} 的终端标识 terminal_code 必须是非空字符串",
                       code="invalid_terminal_code")
    code = code.strip()
    seq = _validate_seq(endpoint.get("seq"))
    terminal = _require_terminal(conn, drill_id, code)
    row = conn.execute(
        "SELECT r.*, t.code AS terminal_code FROM records r "
        "JOIN terminals t ON t.id = r.terminal_id "
        "WHERE r.drill_id = ? AND r.terminal_id = ? AND r.seq = ?",
        (drill_id, terminal["id"], seq),
    ).fetchone()
    if row is None:
        # 区分缺号与“仅有冲突证据”两种情况，给出可操作的提示
        gap = conn.execute(
            "SELECT 1 FROM lost_gaps WHERE terminal_id = ? AND seq = ?",
            (terminal["id"], seq),
        ).fetchone()
        if gap is not None:
            hint = "该序号是缺号（已确认丢失），缺号不能作为处置依赖端点"
        else:
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE terminal_id = ? AND seq = ? LIMIT 1",
                (terminal["id"], seq),
            ).fetchone()
            if conflict is not None:
                hint = ("该序号只有冲突上报证据、没有现存记录，"
                        "冲突证据不能作为处置依赖端点")
            else:
                hint = "该序号尚未上报，缺号不能作为处置依赖端点"
        raise NotFoundError(
            f"{label} 终端 {code} 序号 {seq} 不存在现存记录：{hint}",
            code="endpoint_record_not_found",
        )
    return row


def _dependency_ref(row: sqlite3.Row) -> dict:
    """依赖边在响应中的对外表示（以终端标识+序号标识端点身份）。"""
    return {
        "dependency_id": row["id"],
        "predecessor": {
            "record_id": row["predecessor_record_id"],
            "terminal_code": row["pre_terminal_code"],
            "seq": row["pre_seq"],
        },
        "successor": {
            "record_id": row["successor_record_id"],
            "terminal_code": row["succ_terminal_code"],
            "seq": row["succ_seq"],
        },
        "basis": row["basis"],
        "created_at": row["created_at"],
    }


def _get_dependency_edge(conn: sqlite3.Connection, predecessor_id: str,
                         successor_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT d.*, "
        "tp.code AS pre_terminal_code, rp.seq AS pre_seq, "
        "ts.code AS succ_terminal_code, rs.seq AS succ_seq "
        "FROM dependencies d "
        "JOIN records rp ON rp.id = d.predecessor_record_id "
        "JOIN terminals tp ON tp.id = rp.terminal_id "
        "JOIN records rs ON rs.id = d.successor_record_id "
        "JOIN terminals ts ON ts.id = rs.terminal_id "
        "WHERE d.predecessor_record_id = ? AND d.successor_record_id = ?",
        (predecessor_id, successor_id),
    ).fetchone()


def _successor_path(conn: sqlite3.Connection, drill_id: str, start_id: str,
                    target_id: str) -> Optional[list[sqlite3.Row]]:
    """在本演练的依赖图内，沿“前置 -> 后续”方向查找 start 到 target 的一条路径。

    若存在则返回路径上的边列表（含 start==target 的空路径），不存在返回 None。
    用于提交 predecessor -> successor 前的环检测：
    若 successor 已能沿边到达 predecessor，再加边就会成环。
    """
    if start_id == target_id:
        return []
    edges = conn.execute(
        "SELECT d.*, "
        "tp.code AS pre_terminal_code, rp.seq AS pre_seq, "
        "ts.code AS succ_terminal_code, rs.seq AS succ_seq "
        "FROM dependencies d "
        "JOIN records rp ON rp.id = d.predecessor_record_id "
        "JOIN terminals tp ON tp.id = rp.terminal_id "
        "JOIN records rs ON rs.id = d.successor_record_id "
        "JOIN terminals ts ON ts.id = rs.terminal_id "
        "WHERE d.drill_id = ?",
        (drill_id,),
    ).fetchall()
    adjacency: dict[str, list[sqlite3.Row]] = {}
    for e in edges:
        adjacency.setdefault(e["predecessor_record_id"], []).append(e)
    queue: list[str] = [start_id]
    parents: dict[str, tuple[str, sqlite3.Row]] = {}
    seen = {start_id}
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        for edge in adjacency.get(cur, []):
            nxt = edge["successor_record_id"]
            if nxt in seen:
                continue
            seen.add(nxt)
            parents[nxt] = (cur, edge)
            if nxt == target_id:
                # 回溯路径
                path: list[sqlite3.Row] = []
                node = nxt
                while node != start_id:
                    prev, pe = parents[node]
                    path.append(pe)
                    node = prev
                path.reverse()
                return path
            queue.append(nxt)
    return None


def create_dependency(conn: sqlite3.Connection, drill_id: str,
                      predecessor: dict, successor: dict, basis: Any) -> dict:
    """标记一条处置依赖：successor（后续）的处置依赖 predecessor（前置）。

    幂等：同一条边重复提交直接返回既有边（保留首次依据），不新增数据。
    """
    if not isinstance(basis, str) or not basis.strip():
        raise APIError("处置依据 basis 必须是非空字符串", code="invalid_basis")
    basis = basis.strip()

    with transaction(conn):
        _require_drill_open(conn, drill_id)
        pre = _resolve_dependency_endpoint(conn, drill_id, predecessor, "前置记录")
        succ = _resolve_dependency_endpoint(conn, drill_id, successor, "后续记录")
        if pre["id"] == succ["id"]:
            raise ConflictError(
                "前置记录与后续记录必须是两条不同的现存记录，不能对自身建立依赖",
                code="self_dependency",
            )

        existing = _get_dependency_edge(conn, pre["id"], succ["id"])
        already_exists = existing is not None
        if existing is None:
            # 环检测：若后续记录已能沿“前置->后续”方向到达前置记录，则成环
            path = _successor_path(conn, drill_id, succ["id"], pre["id"])
            if path is not None:
                chain = [f"{e['succ_terminal_code']}#{e['succ_seq']}" for e in path]
                raise ConflictError(
                    "该依赖会形成环：后续记录 "
                    f"{succ['terminal_code']}#{succ['seq']} 已（直接或间接）依赖于前置记录 "
                    f"{pre['terminal_code']}#{pre['seq']}"
                    + (f"（现有路径：{' -> '.join(chain)}）" if chain else ""),
                    code="dependency_cycle",
                    details={
                        "existing_path": [
                            {"terminal_code": e["succ_terminal_code"], "seq": e["succ_seq"]}
                            for e in path
                        ],
                    },
                )
            dep_id = _new_id("dep")
            conn.execute(
                "INSERT INTO dependencies (id, drill_id, predecessor_record_id, "
                "successor_record_id, basis, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dep_id, drill_id, pre["id"], succ["id"], basis, now_iso()),
            )
            existing = _get_dependency_edge(conn, pre["id"], succ["id"])

    body = _dependency_ref(existing)
    body["drill_id"] = drill_id
    body["already_exists"] = already_exists
    return body


def revoke_dependency(conn: sqlite3.Connection, drill_id: str,
                      predecessor: dict, successor: dict) -> dict:
    """撤销一条处置依赖。幂等：边不存在时 deleted=false，不报错。"""
    with transaction(conn):
        _require_drill_open(conn, drill_id)
        pre = _resolve_dependency_endpoint(conn, drill_id, predecessor, "前置记录")
        succ = _resolve_dependency_endpoint(conn, drill_id, successor, "后续记录")
        existing = _get_dependency_edge(conn, pre["id"], succ["id"])
        if existing is None:
            deleted = False
            basis = None
            created_at = None
        else:
            conn.execute("DELETE FROM dependencies WHERE id = ?", (existing["id"],))
            deleted = True
            basis = existing["basis"]
            created_at = existing["created_at"]

    return {
        "drill_id": drill_id,
        "deleted": deleted,
        "predecessor": {"terminal_code": pre["terminal_code"], "seq": pre["seq"]},
        "successor": {"terminal_code": succ["terminal_code"], "seq": succ["seq"]},
        "basis": basis,
        "created_at": created_at,
    }


def _neighbor_record_dict(row: sqlite3.Row) -> dict:
    """依赖邻居（直接前置/直接后续）的记录表示，附带所属依赖边信息。"""
    body = _record_dict(row)
    body["dependency"] = {
        "dependency_id": row["dep_id"],
        "basis": row["dep_basis"],
        "created_at": row["dep_created_at"],
    }
    return body


def _predecessor_chain(conn: sqlite3.Connection, drill_id: str,
                       record_id: str) -> list[dict]:
    """沿前置方向展开完整链：返回全部（直接与间接）前置记录。

    无环图下每个祖先只出现一次，distance 为沿边到达该祖先的最短条数
    （直接前置 distance=1）；结果按终端标识、序号稳定排序。
    """
    edges = conn.execute(
        "SELECT predecessor_record_id, successor_record_id FROM dependencies "
        "WHERE drill_id = ?",
        (drill_id,),
    ).fetchall()
    preds: dict[str, list[str]] = {}
    for e in edges:
        preds.setdefault(e["successor_record_id"], []).append(e["predecessor_record_id"])

    distance = {record_id: 0}
    queue = [record_id]
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        for pid in preds.get(cur, []):
            nd = distance[cur] + 1
            if pid not in distance or nd < distance[pid]:
                distance[pid] = nd
                queue.append(pid)

    ancestor_ids = [rid for rid in distance if rid != record_id]
    if not ancestor_ids:
        return []
    marks = ",".join("?" for _ in ancestor_ids)
    rows = conn.execute(
        f"SELECT r.*, t.code AS terminal_code FROM records r "
        f"JOIN terminals t ON t.id = r.terminal_id WHERE r.id IN ({marks})",
        ancestor_ids,
    ).fetchall()
    chain = []
    for r in rows:
        item = _record_dict(r)
        item["distance"] = distance[r["id"]]
        chain.append(item)
    chain.sort(key=lambda x: (x["terminal_code"], x["seq"]))
    return chain


def get_dependencies(conn: sqlite3.Connection, drill_id: str,
                     terminal_code: str, seq: Any) -> dict:
    """查询指定记录的处置依赖：直接前置、直接后续与沿前置方向的完整链。"""
    with transaction(conn, immediate=False):
        _require_drill(conn, drill_id)
        terminal = _require_terminal(conn, drill_id, terminal_code)
        seq = _validate_seq(seq)
        record = conn.execute(
            "SELECT r.*, t.code AS terminal_code FROM records r "
            "JOIN terminals t ON t.id = r.terminal_id "
            "WHERE r.drill_id = ? AND r.terminal_id = ? AND r.seq = ?",
            (drill_id, terminal["id"], seq),
        ).fetchone()
        if record is None:
            raise NotFoundError(
                f"终端 {terminal_code} 序号 {seq} 的记录不存在，无法查询依赖",
                code="record_not_found",
            )
        pre_rows = conn.execute(
            "SELECT r.*, t.code AS terminal_code, "
            "d.id AS dep_id, d.basis AS dep_basis, d.created_at AS dep_created_at "
            "FROM dependencies d "
            "JOIN records r ON r.id = d.predecessor_record_id "
            "JOIN terminals t ON t.id = r.terminal_id "
            "WHERE d.successor_record_id = ? "
            "ORDER BY t.code ASC, r.seq ASC",
            (record["id"],),
        ).fetchall()
        succ_rows = conn.execute(
            "SELECT r.*, t.code AS terminal_code, "
            "d.id AS dep_id, d.basis AS dep_basis, d.created_at AS dep_created_at "
            "FROM dependencies d "
            "JOIN records r ON r.id = d.successor_record_id "
            "JOIN terminals t ON t.id = r.terminal_id "
            "WHERE d.predecessor_record_id = ? "
            "ORDER BY t.code ASC, r.seq ASC",
            (record["id"],),
        ).fetchall()
        chain = _predecessor_chain(conn, drill_id, record["id"])

    return {
        "drill_id": drill_id,
        "record": _record_dict(record),
        "direct_predecessors": [_neighbor_record_dict(r) for r in pre_rows],
        "direct_successors": [_neighbor_record_dict(r) for r in succ_rows],
        "predecessor_chain": chain,
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
    # 处置依赖快照：封存当时的每条依赖边及其非空依据（身份以记录主键固化）
    dep_rows = conn.execute(
        "SELECT d.*, "
        "rp.terminal_id AS pre_terminal_id, tp.code AS pre_terminal_code, rp.seq AS pre_seq, "
        "rs.terminal_id AS succ_terminal_id, ts.code AS succ_terminal_code, rs.seq AS succ_seq "
        "FROM dependencies d "
        "JOIN records rp ON rp.id = d.predecessor_record_id "
        "JOIN terminals tp ON tp.id = rp.terminal_id "
        "JOIN records rs ON rs.id = d.successor_record_id "
        "JOIN terminals ts ON ts.id = rs.terminal_id "
        "WHERE d.drill_id = ? "
        "ORDER BY tp.code, rp.seq, ts.code, rs.seq",
        (drill_id,),
    ).fetchall()
    for d in dep_rows:
        conn.execute(
            "INSERT INTO seal_dependencies (seal_id, dependency_id, predecessor_record_id, "
            "successor_record_id, predecessor_terminal_id, predecessor_terminal_code, "
            "predecessor_seq, successor_terminal_id, successor_terminal_code, successor_seq, "
            "basis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seal_id, d["id"], d["predecessor_record_id"], d["successor_record_id"],
             d["pre_terminal_id"], d["pre_terminal_code"], d["pre_seq"],
             d["succ_terminal_id"], d["succ_terminal_code"], d["succ_seq"],
             d["basis"], d["created_at"]),
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
            dep_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM seal_dependencies WHERE seal_id = ?", (s["id"],)
            ).fetchone()["c"]
            out.append({
                "seal_id": s["id"],
                "version": s["version"],
                "note": s["note"],
                "created_at": s["created_at"],
                "record_count": cnt,
                "dependency_count": dep_cnt,
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
            "ORDER BY predecessor_terminal_code, predecessor_seq, "
            "successor_terminal_code, successor_seq",
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
                "dependency_id": d["dependency_id"],
                "predecessor": {
                    "record_id": d["predecessor_record_id"],
                    "terminal_code": d["predecessor_terminal_code"],
                    "seq": d["predecessor_seq"],
                },
                "successor": {
                    "record_id": d["successor_record_id"],
                    "terminal_code": d["successor_terminal_code"],
                    "seq": d["successor_seq"],
                },
                "basis": d["basis"],
                "created_at": d["created_at"],
            }
            for d in dep_rows
        ],
    }
