from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor

from domain import DomainError, ExpenseInput, Status, snapshot_from_input, transition, validate_cumulative_payment, validate_expense, validate_payment


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"


class Database:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ["DATABASE_URL"]
        self.sslmode = os.getenv("DB_SSLMODE", "require")
        self.pool: pool.ThreadedConnectionPool | None = None

    def open(self) -> None:
        if self.pool is None:
            self.pool = pool.ThreadedConnectionPool(
                int(os.getenv("DB_MIN_CONNECTIONS", "1")),
                int(os.getenv("DB_MAX_CONNECTIONS", "8")),
                self.database_url,
                sslmode=self.sslmode,
            )
        self.execute_schema()

    def close(self) -> None:
        if self.pool:
            self.pool.closeall()
            self.pool = None

    @contextmanager
    def connection(self):
        if not self.pool:
            self.open()
        assert self.pool is not None
        conn = self.pool.getconn()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            self.pool.putconn(conn)

    def execute_schema(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(schema)
            conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(%s,%s,NOW()) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (key, value),
            )
            conn.commit()

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_user_id = %s", (user_id,))
            return dict(cur.fetchone()) if cur.rowcount else None

    def touch_user(self, user_id: int, username: str | None, profile_link: str) -> None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET username=%s, profile_link=%s, last_seen_at=NOW() WHERE telegram_user_id=%s",
                (username, profile_link, user_id),
            )
            conn.commit()

    def register_user(self, user_id: int, full_name: str, username: str | None, profile_link: str) -> dict[str, Any]:
        full_name = " ".join(full_name.strip().split())
        if len(full_name) < 2 or len(full_name) > 200:
            raise DomainError("الاسم الرسمي يجب أن يتكون من حرفين على الأقل وألا يتجاوز 200 حرف.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_user_id=%s FOR UPDATE", (user_id,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE users SET username=%s, profile_link=%s, last_seen_at=NOW() WHERE telegram_user_id=%s RETURNING *",
                    (username, profile_link, user_id),
                )
            else:
                cur.execute(
                    "INSERT INTO users(telegram_user_id,full_name,username,profile_link) VALUES(%s,%s,%s,%s) RETURNING *",
                    (user_id, full_name, username, profile_link),
                )
            row = dict(cur.fetchone())
            conn.commit()
            return row

    def update_user_name(self, user_id: int, new_name: str, actor_id: int, actor_name: str) -> dict[str, Any]:
        new_name = " ".join(new_name.strip().split())
        if len(new_name) < 2 or len(new_name) > 200:
            raise DomainError("الاسم الجديد غير صالح.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise DomainError("المستخدم غير موجود.")
            old_name = row["full_name"]
            cur.execute(
                "UPDATE users SET full_name=%s,name_updated_at=NOW(),name_updated_by=%s WHERE telegram_user_id=%s RETURNING *",
                (new_name, actor_id, user_id),
            )
            updated = dict(cur.fetchone())
            cur.execute(
                "INSERT INTO workflow_events(request_id,target_user_id,event_type,actor_id,actor_name,version_no,reason,metadata) "
                "VALUES(NULL,%s,'user_name_updated',%s,%s,1,%s,%s)",
                (user_id, actor_id, actor_name, f"{old_name} -> {new_name}", Json({"user_id": user_id, "old_name": old_name, "new_name": new_name})),
            )
            conn.commit()
            return updated

    def set_user_status(self, user_id: int, status: str, actor_id: int, actor_name: str) -> None:
        if status not in {"active", "suspended", "inactive"}:
            raise DomainError("حالة المستخدم غير صالحة.")
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET status=%s WHERE telegram_user_id=%s", (status, user_id))
            if cur.rowcount != 1:
                raise DomainError("المستخدم غير موجود.")
            conn.commit()

    def list_users(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS count FROM users")
            total = int(cur.fetchone()["count"])
            cur.execute(
                "SELECT * FROM users ORDER BY full_name, telegram_user_id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return [dict(row) for row in cur.fetchall()], total

    def get_user_requests(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM expense_requests WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def set_admin_group(self, chat_id: int, thread_id: int | None, actor_id: int) -> None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_group_config(id,chat_id,thread_id,configured_by) VALUES(1,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET chat_id=EXCLUDED.chat_id,thread_id=EXCLUDED.thread_id,configured_by=EXCLUDED.configured_by,configured_at=NOW()",
                (chat_id, thread_id, actor_id),
            )
            conn.commit()

    def get_admin_group(self) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM admin_group_config WHERE id=1")
            row = cur.fetchone()
            return dict(row) if row else None

    def set_cashier(self, target_type: str, chat_id: int, actor_id: int, label: str = "الصندوق") -> None:
        if target_type not in {"user", "group"}:
            raise DomainError("نوع وجهة الصندوق غير صالح.")
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cashier_config(id,target_type,target_chat_id,label,configured_by) VALUES(1,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET target_type=EXCLUDED.target_type,target_chat_id=EXCLUDED.target_chat_id,label=EXCLUDED.label,enabled=TRUE,configured_by=EXCLUDED.configured_by,configured_at=NOW()",
                (target_type, chat_id, label, actor_id),
            )
            conn.commit()

    def get_cashier(self) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM cashier_config WHERE id=1 AND enabled=TRUE")
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key, value in list(result.items()):
            if isinstance(value, (date, datetime)):
                result[key] = value.isoformat()
            elif isinstance(value, Decimal):
                result[key] = str(value)
        return result

    def _event(self, cur, request_id: int, event_type: str, from_status: str | None, to_status: str | None,
               actor_id: int | None, actor_name: str, version_no: int, reason: str = "", metadata: dict[str, Any] | None = None) -> None:
        cur.execute(
            "INSERT INTO workflow_events(request_id,event_type,from_status,to_status,actor_id,actor_name,version_no,reason,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (request_id, event_type, from_status, to_status, actor_id, actor_name, version_no, reason, Json(metadata or {})),
        )

    def create_request(self, user_id: int, data: ExpenseInput, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        validated = validate_expense(data)
        snapshot = snapshot_from_input(validated)
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM users WHERE telegram_user_id=%s AND status='active'", (user_id,))
            if not cur.fetchone():
                raise DomainError("يجب تسجيل المستخدم وتفعيله قبل إنشاء الطلب.")
            cur.execute(
                "INSERT INTO expense_requests(public_id,user_id,status,version_no,mosque_name,wilaya,baladiya,mission_start_date,mission_end_date,duration_text,responsable,carpet_type,carpet_area,has_feutre,carpet_rate,carpet_amount,approval_stage,amount_requested,currency,additional_details) "
                "VALUES('PENDING',%s,'submitted',1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s) RETURNING *",
                (user_id, validated.mosque_name, validated.wilaya, validated.baladiya, validated.mission_start_date, validated.mission_end_date, validated.duration_text, validated.responsable, validated.carpet_type, validated.carpet_area, validated.has_feutre, validated.carpet_rate, validated.carpet_amount, validated.amount_requested, validated.currency, validated.additional_details),
            )
            row = dict(cur.fetchone())
            public_id = f"EXP-{datetime.now(timezone.utc).year}-{row['id']:06d}"
            cur.execute("UPDATE expense_requests SET public_id=%s WHERE id=%s RETURNING *", (public_id, row["id"]))
            row = dict(cur.fetchone())
            cur.execute(
                "INSERT INTO request_versions(request_id,version_no,snapshot,created_by,source) VALUES(%s,1,%s,%s,'user')",
                (row["id"], Json(snapshot), user_id),
            )
            self._insert_items(cur, row["id"], 1, validated.mission_expenses)
            for attachment in attachments or []:
                cur.execute(
                    "INSERT INTO attachments(request_id,version_no,telegram_file_id,telegram_file_unique_id,media_type,original_name) VALUES(%s,1,%s,%s,%s,%s)",
                    (row["id"], attachment["file_id"], attachment.get("file_unique_id"), attachment["media_type"], attachment.get("original_name")),
                )
            self._event(cur, row["id"], "request_submitted", "draft", "submitted", user_id, "المستخدم", 1)
            conn.commit()
            return row

    def get_responsables(self) -> list[str]:
        raw = self.get_setting("responsables", "Ammar redouan|ahmed lasaakeur") or ""
        return [item.strip() for item in raw.split("|") if item.strip()]

    def set_responsables(self, names: list[str], actor_id: int, actor_name: str) -> None:
        cleaned = [" ".join(name.strip().split()) for name in names if name.strip()]
        if not cleaned:
            raise DomainError("يجب أن تحتوي قائمة المسؤولين على اسم واحد على الأقل.")
        if any(len(name) < 2 or len(name) > 200 for name in cleaned):
            raise DomainError("اسم المسؤول غير صالح.")
        self.set_setting("responsables", "|".join(dict.fromkeys(cleaned)))

    def get_carpet_rates(self) -> tuple[Decimal, Decimal]:
        without = Decimal(self.get_setting("carpet_rate_without_feutre", "15") or "15")
        with_feutre = Decimal(self.get_setting("carpet_rate_with_feutre", "20") or "20")
        return without, with_feutre

    def set_carpet_rates(self, without_feutre: Decimal, with_feutre: Decimal) -> None:
        if without_feutre <= 0 or with_feutre <= 0:
            raise DomainError("سعر المتر يجب أن يكون أكبر من الصفر.")
        self.set_setting("carpet_rate_without_feutre", str(without_feutre))
        self.set_setting("carpet_rate_with_feutre", str(with_feutre))

    def get_expense_items(self, request_id: int, version_no: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if version_no is None:
                cur.execute("SELECT * FROM expense_items WHERE request_id=%s ORDER BY id", (request_id,))
            else:
                cur.execute("SELECT * FROM expense_items WHERE request_id=%s AND version_no=%s ORDER BY id", (request_id, version_no))
            return [dict(row) for row in cur.fetchall()]

    def _insert_items(self, cur, request_id: int, version_no: int, items: Iterable[dict[str, Any]]) -> None:
        for item in items:
            cur.execute(
                "INSERT INTO expense_items(request_id,version_no,item_type,description,amount,currency) VALUES(%s,%s,'mission',%s,%s,%s)",
                (request_id, version_no, item["description"], item["amount"], item.get("currency", "DZD")),
            )

    def _request_snapshot_with_items(self, row: dict[str, Any], cur) -> dict[str, Any]:
        snapshot = self._snapshot(row)
        cur.execute("SELECT description,amount,currency,item_type FROM expense_items WHERE request_id=%s AND version_no=%s ORDER BY id", (row["id"], row["version_no"]))
        snapshot["mission_expenses"] = [self._snapshot(dict(item)) for item in cur.fetchall()]
        return snapshot

    def _recalculate_total(self, cur, row: dict[str, Any]) -> Decimal:
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM expense_items WHERE request_id=%s AND version_no=%s", (row["id"], row["version_no"]))
        items_total = Decimal(cur.fetchone()["total"] or 0)
        carpet_amount = Decimal(row.get("carpet_amount") or 0)
        total = (carpet_amount + items_total).quantize(Decimal("0.01"))
        cur.execute("UPDATE expense_requests SET amount_requested=%s,updated_at=NOW() WHERE id=%s RETURNING *", (total, row["id"]))
        return total

    def admin_approve_stage(self, request_id: int, stage: int, actor_id: int, actor_name: str) -> dict[str, Any]:
        if stage not in {1, 2, 3}:
            raise DomainError("مرحلة الاعتماد غير صالحة.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["status"] not in {Status.SUBMITTED.value, Status.RESUBMITTED.value, Status.REOPENED.value}:
                raise DomainError("لا يمكن اعتماد هذا الطلب في حالته الحالية.")
            expected = int(row.get("approval_stage") or 0) + 1
            if stage != expected:
                raise DomainError(f"يجب اعتماد المرحلة السابقة أولاً. المرحلة المطلوبة الآن: {expected}.")
            fields = {1: ("space_approved_by", "space_approved_at", "space_approved"), 2: ("expenses_approved_by", "expenses_approved_at", "expenses_approved"), 3: ("total_approved_by", "total_approved_at", "admin_approved")}[stage]
            status = Status.APPROVED_BY_ADMIN.value if stage == 3 else row["status"]
            cur.execute(
                f"UPDATE expense_requests SET approval_stage=%s,{fields[0]}=%s,{fields[1]}=NOW(),status=%s,approved_by=CASE WHEN %s=3 THEN %s ELSE approved_by END,approved_at=CASE WHEN %s=3 THEN NOW() ELSE approved_at END,approved_version_no=CASE WHEN %s=3 THEN version_no ELSE approved_version_no END,updated_at=NOW() WHERE id=%s AND approval_stage=%s AND version_no=%s RETURNING *",
                (stage, actor_id, status, stage, actor_id, stage, stage, row["id"], stage - 1, row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تغير الطلب قبل اعتماد المرحلة. أعد تحميله.")
            updated = dict(updated)
            self._event(cur, row["id"], fields[2], row["status"], status, actor_id, actor_name, row["version_no"], f"اعتماد المرحلة {stage}", {"stage": stage})
            conn.commit()
            return updated

    def admin_edit_field(self, request_id: int, field: str, value: Any, actor_id: int, actor_name: str, reason: str = "") -> dict[str, Any]:
        allowed = {"mosque_name", "wilaya", "baladiya", "duration_text", "responsable", "carpet_type", "carpet_area", "has_feutre", "carpet_rate", "carpet_amount"}
        if field not in allowed:
            raise DomainError("هذا الحقل لا يدعم التعديل المباشر.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["status"] not in {Status.SUBMITTED.value, Status.RESUBMITTED.value, Status.REOPENED.value} or int(row.get("approval_stage") or 0) >= 3:
                raise DomainError("لا يمكن تعديل الطلب بعد الاعتماد النهائي.")
            value = str(value).strip() if field not in {"carpet_area", "carpet_rate", "carpet_amount", "has_feutre"} else value
            if field in {"mosque_name", "wilaya", "baladiya", "duration_text", "responsable", "carpet_type"}:
                if len(str(value).strip()) < 1:
                    raise DomainError("القيمة الجديدة لا يمكن أن تكون فارغة.")
            if field in {"carpet_area", "carpet_rate", "carpet_amount"}:
                value = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
                if value <= 0:
                    raise DomainError("القيمة يجب أن تكون أكبر من الصفر.")
            if field == "has_feutre":
                value = bool(value)
            updates = {field: value}
            if field == "carpet_area":
                updates["carpet_amount"] = (value * Decimal(row.get("carpet_rate") or (20 if row.get("has_feutre") else 15))).quantize(Decimal("0.01"))
            elif field == "has_feutre" and row.get("carpet_area"):
                updates["carpet_rate"] = Decimal("20" if value else "15")
                updates["carpet_amount"] = (Decimal(row["carpet_area"]) * updates["carpet_rate"]).quantize(Decimal("0.01"))
            elif field == "carpet_rate" and row.get("carpet_area"):
                updates["carpet_amount"] = (Decimal(row["carpet_area"]) * value).quantize(Decimal("0.01"))
            assignments = ",".join(f"{key}=%s" for key in updates)
            params = list(updates.values())
            params.extend([row["id"]])
            assignments += ",approval_stage=0,space_approved_by=NULL,space_approved_at=NULL,expenses_approved_by=NULL,expenses_approved_at=NULL,total_approved_by=NULL,total_approved_at=NULL,approved_by=NULL,approved_at=NULL,approved_version_no=NULL"
            cur.execute(f"UPDATE expense_requests SET {assignments},updated_at=NOW() WHERE id=%s RETURNING *", params)
            updated = dict(cur.fetchone())
            self._recalculate_total(cur, updated)
            cur.execute("SELECT * FROM expense_requests WHERE id=%s", (request_id,))
            updated = dict(cur.fetchone())
            next_version = int(row["version_no"]) + 1
            cur.execute("UPDATE expense_requests SET version_no=%s WHERE id=%s RETURNING *", (next_version, request_id))
            updated = dict(cur.fetchone())
            cur.execute("INSERT INTO expense_items(request_id,version_no,item_type,description,amount,currency) SELECT request_id,%s,item_type,description,amount,currency FROM expense_items WHERE request_id=%s AND version_no=%s", (next_version, request_id, row["version_no"]))
            snapshot = self._request_snapshot_with_items(updated, cur)
            cur.execute("INSERT INTO request_versions(request_id,version_no,snapshot,created_by,source,change_reason) VALUES(%s,%s,%s,%s,'admin',%s)", (request_id, next_version, Json(snapshot), actor_id, reason or f"تعديل الحقل {field}"))
            self._event(cur, request_id, "admin_field_edited", row["status"], row["status"], actor_id, actor_name, next_version, reason or f"تعديل {field}", {"field": field})
            conn.commit()
            return updated

    def admin_update_expense_item(self, request_id: int, item_id: int, description: str, amount: Decimal, actor_id: int, actor_name: str) -> dict[str, Any]:
        description = " ".join(description.strip().split())
        amount = Decimal(amount).quantize(Decimal("0.01"))
        if len(description) < 2 or amount <= 0:
            raise DomainError("بيانات المصروف التفصيلي غير صالحة.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if int(row.get("approval_stage") or 0) >= 2:
                raise DomainError("لا يمكن تعديل المصاريف بعد اعتماد المرحلة الثانية.")
            cur.execute("UPDATE expense_items SET description=%s,amount=%s WHERE id=%s AND request_id=%s AND version_no=%s RETURNING *", (description, amount, item_id, request_id, row["version_no"]))
            item = cur.fetchone()
            if not item:
                raise DomainError("المصروف غير موجود أو تغيرت نسخة الطلب.")
            self._recalculate_total(cur, row)
            cur.execute("UPDATE expense_requests SET approval_stage=0,space_approved_by=NULL,space_approved_at=NULL,expenses_approved_by=NULL,expenses_approved_at=NULL,total_approved_by=NULL,total_approved_at=NULL,approved_by=NULL,approved_at=NULL,approved_version_no=NULL,updated_at=NOW() WHERE id=%s", (request_id,))
            self._event(cur, request_id, "mission_expense_edited", row["status"], row["status"], actor_id, actor_name, row["version_no"], "تعديل مصروف تفصيلي", {"item_id": item_id})
            conn.commit()
            return dict(item)

    def admin_remove_expense_item(self, request_id: int, item_id: int, actor_id: int, actor_name: str) -> None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if int(row.get("approval_stage") or 0) >= 2:
                raise DomainError("لا يمكن حذف المصاريف بعد اعتماد المرحلة الثانية.")
            cur.execute("DELETE FROM expense_items WHERE id=%s AND request_id=%s AND version_no=%s RETURNING id", (item_id, request_id, row["version_no"]))
            if not cur.fetchone():
                raise DomainError("المصروف غير موجود أو تغيرت نسخة الطلب.")
            self._recalculate_total(cur, row)
            cur.execute("UPDATE expense_requests SET approval_stage=0,space_approved_by=NULL,space_approved_at=NULL,expenses_approved_by=NULL,expenses_approved_at=NULL,total_approved_by=NULL,total_approved_at=NULL,approved_by=NULL,approved_at=NULL,approved_version_no=NULL,updated_at=NOW() WHERE id=%s", (request_id,))
            self._event(cur, request_id, "mission_expense_removed", row["status"], row["status"], actor_id, actor_name, row["version_no"], "حذف مصروف تفصيلي", {"item_id": item_id})
            conn.commit()

    def admin_back_to_edit(self, request_id: int, actor_id: int, actor_name: str) -> dict[str, Any]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["status"] not in {Status.SUBMITTED.value, Status.RESUBMITTED.value, Status.REOPENED.value}:
                raise DomainError("لا يمكن إعادة هذا الطلب للتعديل في حالته الحالية.")
            cur.execute("UPDATE expense_requests SET approval_stage=0,space_approved_by=NULL,space_approved_at=NULL,expenses_approved_by=NULL,expenses_approved_at=NULL,total_approved_by=NULL,total_approved_at=NULL,approved_by=NULL,approved_at=NULL,approved_version_no=NULL,updated_at=NOW() WHERE id=%s RETURNING *", (request_id,))
            updated = dict(cur.fetchone())
            self._event(cur, request_id, "admin_back_to_edit", row["status"], row["status"], actor_id, actor_name, row["version_no"], "إعادة الطلب للتعديل")
            conn.commit()
            return updated

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT r.*,u.full_name,u.username,u.profile_link FROM expense_requests r JOIN users u ON u.telegram_user_id=r.user_id WHERE r.id=%s",
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            cur.execute("SELECT * FROM expense_items WHERE request_id=%s ORDER BY id", (request_id,))
            result["mission_expenses"] = [dict(item) for item in cur.fetchall()]
            return result

    def get_request_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT r.*,u.full_name,u.username,u.profile_link FROM expense_requests r JOIN users u ON u.telegram_user_id=r.user_id WHERE r.public_id=%s",
                (public_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            cur.execute("SELECT * FROM expense_items WHERE request_id=%s ORDER BY id", (result["id"],))
            result["mission_expenses"] = [dict(item) for item in cur.fetchall()]
            return result

    def get_attachments(self, request_id: int, version_no: int | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if version_no is None:
                cur.execute("SELECT * FROM attachments WHERE request_id=%s ORDER BY id", (request_id,))
            else:
                cur.execute("SELECT * FROM attachments WHERE request_id=%s AND version_no=%s ORDER BY id", (request_id, version_no))
            return [dict(row) for row in cur.fetchall()]

    def set_admin_message(self, request_id: int, chat_id: int, thread_id: int | None, message_id: int) -> None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE expense_requests SET admin_chat_id=%s,admin_thread_id=%s,admin_message_id=%s,updated_at=NOW() WHERE id=%s",
                (chat_id, thread_id, message_id, request_id),
            )
            conn.commit()

    def _locked_request(self, cur, request_id: int) -> dict[str, Any]:
        cur.execute("SELECT * FROM expense_requests WHERE id=%s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if not row:
            raise DomainError("الطلب غير موجود.")
        return dict(row)

    def _change_status(self, cur, row: dict[str, Any], target: Status, event_type: str, actor_id: int, actor_name: str, reason: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        cur.execute(
            "UPDATE expense_requests SET status=%s,updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
            (target.value, row["id"], row["status"], row["version_no"]),
        )
        updated = cur.fetchone()
        if not updated:
            raise DomainError("تمت معالجة الطلب من عضو آخر أو تغيرت نسخته. أعد تحميل الطلب.")
        updated = dict(updated)
        self._event(cur, row["id"], event_type, row["status"], target.value, actor_id, actor_name, row["version_no"], reason, metadata)
        return updated

    def admin_request_changes(self, request_id: int, actor_id: int, actor_name: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if len(reason) < 3:
            raise DomainError("سبب طلب التعديل إلزامي.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "request_changes")
            updated = self._change_status(cur, row, tx.to_status, tx.event_type, actor_id, actor_name, reason)
            conn.commit()
            return updated

    def admin_cancel(self, request_id: int, actor_id: int, actor_name: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if len(reason) < 3:
            raise DomainError("سبب الإلغاء إلزامي.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "cancel_admin")
            cur.execute(
                "UPDATE expense_requests SET status=%s,cancel_reason=%s,cancelled_by=%s,cancelled_at=NOW(),updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (tx.to_status.value, reason, actor_id, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تمت معالجة الطلب من عضو آخر. أعد تحميل الطلب.")
            updated = dict(updated)
            self._event(cur, row["id"], tx.event_type, row["status"], tx.to_status.value, actor_id, actor_name, row["version_no"], reason)
            conn.commit()
            return updated

    def user_cancel(self, request_id: int, user_id: int, reason: str = "إلغاء من المستخدم") -> dict[str, Any]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["user_id"] != user_id:
                raise DomainError("لا يمكنك إلغاء طلب مستخدم آخر.")
            tx = transition(row["status"], "cancel_user")
            cur.execute(
                "UPDATE expense_requests SET status=%s,cancel_reason=%s,cancelled_by=%s,cancelled_at=NOW(),updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (tx.to_status.value, reason, user_id, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تمت معالجة الطلب من عضو آخر.")
            updated = dict(updated)
            self._event(cur, row["id"], tx.event_type, row["status"], tx.to_status.value, user_id, "المستخدم", row["version_no"], reason)
            conn.commit()
            return updated

    def admin_approve(self, request_id: int, actor_id: int, actor_name: str) -> dict[str, Any]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "approve_admin")
            cur.execute(
                "UPDATE expense_requests SET status=%s,approved_by=%s,approved_at=NOW(),approved_version_no=version_no,updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (tx.to_status.value, actor_id, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تمت معالجة الطلب من عضو آخر أو تغيرت النسخة.")
            updated = dict(updated)
            self._event(cur, row["id"], tx.event_type, row["status"], tx.to_status.value, actor_id, actor_name, row["version_no"])
            conn.commit()
            return updated

    def admin_edit(self, request_id: int, data: ExpenseInput, actor_id: int, actor_name: str, reason: str) -> dict[str, Any]:
        validated = validate_expense(data)
        snapshot = snapshot_from_input(validated)
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["status"] not in {Status.SUBMITTED.value, Status.RESUBMITTED.value, Status.REOPENED.value}:
                raise DomainError("لا يمكن تعديل الطلب في حالته الحالية.")
            next_version = row["version_no"] + 1
            cur.execute(
                "UPDATE expense_requests SET version_no=%s,mosque_name=%s,wilaya=%s,mission_start_date=%s,mission_end_date=%s,duration_text=%s,amount_requested=%s,currency=%s,additional_details=%s,updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (next_version, validated.mosque_name, validated.wilaya, validated.mission_start_date, validated.mission_end_date, validated.duration_text, validated.amount_requested, validated.currency, validated.additional_details, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تغير الطلب قبل حفظ التعديل. أعد تحميله.")
            updated = dict(updated)
            cur.execute(
                "INSERT INTO request_versions(request_id,version_no,snapshot,created_by,source,change_reason) VALUES(%s,%s,%s,%s,'admin',%s)",
                (row["id"], next_version, Json(snapshot), actor_id, reason.strip()),
            )
            self._event(cur, row["id"], "admin_edited", row["status"], row["status"], actor_id, actor_name, next_version, reason, {"old_version": row["version_no"], "new_version": next_version})
            conn.commit()
            return updated

    def user_resubmit(self, request_id: int, user_id: int, data: ExpenseInput, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        validated = validate_expense(data)
        snapshot = snapshot_from_input(validated)
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            if row["user_id"] != user_id:
                raise DomainError("لا يمكنك تعديل طلب مستخدم آخر.")
            if row["status"] != Status.CHANGES_REQUESTED.value:
                raise DomainError("هذا الطلب لا ينتظر تعديلاً منك.")
            next_version = row["version_no"] + 1
            cur.execute(
                "UPDATE expense_requests SET status='resubmitted',version_no=%s,mosque_name=%s,wilaya=%s,mission_start_date=%s,mission_end_date=%s,duration_text=%s,amount_requested=%s,currency=%s,additional_details=%s,updated_at=NOW() WHERE id=%s AND status='changes_requested' AND version_no=%s RETURNING *",
                (next_version, validated.mosque_name, validated.wilaya, validated.mission_start_date, validated.mission_end_date, validated.duration_text, validated.amount_requested, validated.currency, validated.additional_details, row["id"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تغير الطلب قبل إعادة الإرسال.")
            updated = dict(updated)
            cur.execute(
                "INSERT INTO request_versions(request_id,version_no,snapshot,created_by,source) VALUES(%s,%s,%s,%s,'user')",
                (row["id"], next_version, Json(snapshot), user_id),
            )
            for attachment in attachments or []:
                cur.execute(
                    "INSERT INTO attachments(request_id,version_no,telegram_file_id,telegram_file_unique_id,media_type,original_name) VALUES(%s,%s,%s,%s,%s,%s)",
                    (row["id"], next_version, attachment["file_id"], attachment.get("file_unique_id"), attachment["media_type"], attachment.get("original_name")),
                )
            self._event(cur, row["id"], "request_resubmitted", row["status"], "resubmitted", user_id, "المستخدم", next_version)
            conn.commit()
            return updated

    def cashier_start_review(self, request_id: int, cashier_id: int, cashier_name: str) -> dict[str, Any]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "cashier_review")
            updated = self._change_status(cur, row, tx.to_status, tx.event_type, cashier_id, cashier_name, "")
            conn.commit()
            return updated

    def cashier_payment(self, request_id: int, cashier_id: int, cashier_name: str, paid_amount: Decimal, method: str, note: str, allow_partial: bool) -> dict[str, Any]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            result, total_paid = validate_cumulative_payment(row["amount_requested"], row.get("paid_amount") or Decimal("0"), paid_amount, allow_partial)
            action = "confirm_payment" if result == "full" else "partial_payment"
            tx = transition(row["status"], action)
            paid_amount = Decimal(paid_amount).quantize(Decimal("0.01"))
            cur.execute(
                "UPDATE expense_requests SET status=%s,paid_amount=%s,payment_method=%s,payment_note=%s,cashier_user_id=%s,paid_at=NOW(),updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (tx.to_status.value, total_paid, method.strip()[:100], note.strip()[:2000], cashier_id, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تمت معالجة الدفع من مسؤول آخر أو تغير الطلب.")
            updated = dict(updated)
            self._event(cur, row["id"], tx.event_type, row["status"], tx.to_status.value, cashier_id, cashier_name, row["version_no"], note)
            conn.commit()
            return updated

    def cashier_reject(self, request_id: int, cashier_id: int, cashier_name: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if len(reason) < 3:
            raise DomainError("سبب رفض أو تعليق الدفع إلزامي.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "reject_payment")
            cur.execute(
                "UPDATE expense_requests SET status=%s,rejection_reason=%s,rejected_by=%s,updated_at=NOW() WHERE id=%s AND status=%s AND version_no=%s RETURNING *",
                (tx.to_status.value, reason, cashier_id, row["id"], row["status"], row["version_no"]),
            )
            updated = cur.fetchone()
            if not updated:
                raise DomainError("تمت معالجة الطلب من طرف آخر.")
            updated = dict(updated)
            self._event(cur, row["id"], tx.event_type, row["status"], tx.to_status.value, cashier_id, cashier_name, row["version_no"], reason)
            conn.commit()
            return updated

    def reopen_request(self, request_id: int, actor_id: int, actor_name: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if len(reason) < 3:
            raise DomainError("سبب إعادة الفتح إلزامي.")
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            row = self._locked_request(cur, request_id)
            tx = transition(row["status"], "reopen")
            updated = self._change_status(cur, row, tx.to_status, tx.event_type, actor_id, actor_name, reason)
            conn.commit()
            return updated

    def request_events(self, request_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM workflow_events WHERE request_id=%s ORDER BY created_at DESC LIMIT %s", (request_id, limit))
            return [dict(row) for row in cur.fetchall()]

    def confirmed_requests(self, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        query = "SELECT r.*,u.full_name,u.username,u.profile_link FROM expense_requests r JOIN users u ON u.telegram_user_id=r.user_id WHERE r.status='paid_confirmed'"
        params: list[Any] = []
        if start:
            query += " AND r.paid_at::date >= %s"
            params.append(start)
        if end:
            query += " AND r.paid_at::date <= %s"
            params.append(end)
        query += " ORDER BY r.paid_at, r.id"
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def pending_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM delivery_messages WHERE status IN ('pending','failed') ORDER BY created_at LIMIT %s", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def record_delivery(self, request_id: int | None, recipient_user_id: int | None, recipient_chat_id: int, channel: str, message_kind: str, payload: dict[str, Any]) -> int:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO delivery_messages(request_id,recipient_user_id,recipient_chat_id,channel,message_kind,payload) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (request_id, recipient_user_id, recipient_chat_id, channel, message_kind, Json(payload)),
            )
            delivery_id = int(cur.fetchone()[0])
            conn.commit()
            return delivery_id

    def mark_delivery(self, delivery_id: int, status: str, error: str | None = None) -> None:
        if status not in {"sent", "failed"}:
            raise DomainError("حالة التسليم غير صالحة.")
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE delivery_messages SET status=%s,last_error=%s,attempts=attempts+1,sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END WHERE id=%s",
                (status, error, status, delivery_id),
            )
            conn.commit()

    def pending_expired_requests(self, stale_hours: int) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM expense_requests WHERE status IN ('submitted','resubmitted','payment_rejected') AND updated_at < NOW() - (%s || ' hours')::interval",
                (stale_hours,),
            )
            return [dict(row) for row in cur.fetchall()]
