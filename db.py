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
                "INSERT INTO expense_requests(public_id,user_id,status,version_no,mosque_name,wilaya,mission_start_date,mission_end_date,duration_text,amount_requested,currency,additional_details) "
                "VALUES('PENDING',%s,'submitted',1,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (user_id, validated.mosque_name, validated.wilaya, validated.mission_start_date, validated.mission_end_date, validated.duration_text, validated.amount_requested, validated.currency, validated.additional_details),
            )
            row = dict(cur.fetchone())
            public_id = f"EXP-{datetime.now(timezone.utc).year}-{row['id']:06d}"
            cur.execute("UPDATE expense_requests SET public_id=%s WHERE id=%s RETURNING *", (public_id, row["id"]))
            row = dict(cur.fetchone())
            cur.execute(
                "INSERT INTO request_versions(request_id,version_no,snapshot,created_by,source) VALUES(%s,1,%s,%s,'user')",
                (row["id"], Json(snapshot), user_id),
            )
            for attachment in attachments or []:
                cur.execute(
                    "INSERT INTO attachments(request_id,version_no,telegram_file_id,telegram_file_unique_id,media_type,original_name) VALUES(%s,1,%s,%s,%s,%s)",
                    (row["id"], attachment["file_id"], attachment.get("file_unique_id"), attachment["media_type"], attachment.get("original_name")),
                )
            self._event(cur, row["id"], "request_submitted", "draft", "submitted", user_id, "المستخدم", 1)
            conn.commit()
            return row

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT r.*,u.full_name,u.username,u.profile_link FROM expense_requests r JOIN users u ON u.telegram_user_id=r.user_id WHERE r.id=%s",
                (request_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_request_by_public_id(self, public_id: str) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT r.*,u.full_name,u.username,u.profile_link FROM expense_requests r JOIN users u ON u.telegram_user_id=r.user_id WHERE r.public_id=%s",
                (public_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

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
