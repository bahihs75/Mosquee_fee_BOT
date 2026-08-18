from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import Database
from domain import DomainError, ExpenseInput, profile_link
from excel_export import build_confirmed_workbook
from formatting import request_summary, status_text, user_summary

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("reimbursement-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0"))
ADMIN_THREAD_ID = int(os.environ.get("ADMIN_THREAD_ID", "0")) or None
CASHIER_CHAT_ID = int(os.environ.get("CASHIER_CHAT_ID", "0")) or None
CASHIER_TARGET_TYPE = os.environ.get("CASHIER_TARGET_TYPE", "user")
ADMIN_PAGE_SIZE = 8

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير مضبوط")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL غير مضبوط")
if not ADMIN_GROUP_ID:
    raise RuntimeError("ADMIN_GROUP_ID غير مضبوط")

DB = Database(DATABASE_URL)

# هذه الحالة مؤقتة لجلسة إدخال واحدة فقط؛ المصدر المالي الدائم هو PostgreSQL.
pending: dict[tuple[int, int], dict[str, Any]] = {}


async def is_admin_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.id != ADMIN_GROUP_ID:
        return False
    try:
        member = await context.bot.get_chat_member(ADMIN_GROUP_ID, user.id)
        return member.status in {"creator", "administrator", "member", "restricted"} and getattr(member, "is_member", True)
    except Exception:
        logger.exception("فشل التحقق من عضو الإدارة")
        return False


async def is_cashier_actor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    configured = DB.get_cashier()
    target_type = configured["target_type"] if configured else CASHIER_TARGET_TYPE
    target_id = int(configured["target_chat_id"]) if configured else CASHIER_CHAT_ID
    if not chat or not user or not target_id:
        return False
    if target_type == "user":
        return chat.type == ChatType.PRIVATE and user.id == target_id
    return chat.id == target_id


def actor_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "غير معروف"
    return " ".join(part for part in [user.first_name, user.last_name] if part).strip() or str(user.id)


def private_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


def key_for(update: Update) -> tuple[int, int]:
    return (update.effective_chat.id, update.effective_user.id)  # type: ignore[union-attr]


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إنشاء طلب استرجاع مصاريف", callback_data="u:new")],
        [InlineKeyboardButton("طلباتي", callback_data="u:list")],
        [InlineKeyboardButton("إعادة التسجيل بمساعدة الإدارة", callback_data="u:help")],
    ])


def admin_request_keyboard(request_id: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in {"submitted", "resubmitted", "reopened", "payment_rejected", "expired"}:
        rows.append([
            InlineKeyboardButton("اعتماد وتحويل للصندوق", callback_data=f"a:approve:{request_id}"),
            InlineKeyboardButton("تعديل مباشر", callback_data=f"a:edit:{request_id}"),
        ])
        rows.append([InlineKeyboardButton("طلب تعديل من المستخدم", callback_data=f"a:chg:{request_id}")])
        rows.append([InlineKeyboardButton("إلغاء الطلب", callback_data=f"a:cancel:{request_id}")])
    rows.append([InlineKeyboardButton("عرض سجل الطلب", callback_data=f"a:events:{request_id}")])
    return InlineKeyboardMarkup(rows)


def cashier_keyboard(request_id: int, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status == "approved_by_admin":
        rows.append([InlineKeyboardButton("بدء مراجعة الصندوق", callback_data=f"c:start:{request_id}")])
    if status in {"approved_by_admin", "cashier_review", "partially_paid"}:
        rows.append([
            InlineKeyboardButton("تأكيد الدفع الكامل", callback_data=f"c:payfull:{request_id}"),
            InlineKeyboardButton("دفع جزئي / استكمال", callback_data=f"c:paypartial:{request_id}"),
        ])
        rows.append([InlineKeyboardButton("رفض / تعليق الدفع", callback_data=f"c:reject:{request_id}")])
    return InlineKeyboardMarkup(rows)


def user_request_keyboard(request_id: int, status: str) -> InlineKeyboardMarkup | None:
    if status == "changes_requested":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("تعديل وإعادة الإرسال", callback_data=f"u:edit:{request_id}")],
            [InlineKeyboardButton("إلغاء الطلب", callback_data=f"u:cancel:{request_id}")],
        ])
    if status in {"submitted", "resubmitted"}:
        return InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء الطلب", callback_data=f"u:cancel:{request_id}")]])
    return None


async def deliver_text(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    chat_id: int,
    request_id: int | None,
    recipient_user_id: int | None = None,
    channel: str = "private",
    message_kind: str = "notification",
    reply_markup: InlineKeyboardMarkup | None = None,
    thread_id: int | None = None,
) -> bool:
    delivery_id = DB.record_delivery(request_id, recipient_user_id, chat_id, channel, message_kind, {"text": text, "request_id": request_id})
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            message_thread_id=thread_id,
        )
        DB.mark_delivery(delivery_id, "sent")
        return True
    except Exception as exc:
        logger.warning("فشل إرسال إشعار %s: %s", delivery_id, exc)
        DB.mark_delivery(delivery_id, "failed", str(exc)[:1000])
        return False


async def notify_user(context: ContextTypes.DEFAULT_TYPE, row: dict[str, Any], text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    await deliver_text(context, text, int(row["user_id"]), int(row["id"]), int(row["user_id"]), "private", "user_notification", reply_markup)


async def notify_admin_group(context: ContextTypes.DEFAULT_TYPE, row: dict[str, Any], text: str | None = None) -> None:
    await deliver_text(
        context,
        text or request_summary(row, True),
        ADMIN_GROUP_ID,
        int(row["id"]),
        None,
        "group",
        "admin_request",
        admin_request_keyboard(int(row["id"]), str(row["status"])),
        ADMIN_THREAD_ID,
    )


async def notify_cashier(context: ContextTypes.DEFAULT_TYPE, row: dict[str, Any]) -> None:
    configured = DB.get_cashier()
    cashier_id = int(configured["target_chat_id"]) if configured else CASHIER_CHAT_ID
    target_type = configured["target_type"] if configured else CASHIER_TARGET_TYPE
    if not cashier_id:
        logger.error("لا توجد وجهة للصندوق؛ سيبقى الطلب معتمداً بانتظار ضبط الصندوق")
        return
    await deliver_text(
        context,
        "<b>طلب معتمد من الإدارة ويحتاج تأكيد الصندوق</b>\n\n" + request_summary(row, True),
        cashier_id,
        int(row["id"]),
        cashier_id if target_type == "user" else None,
        "private" if target_type == "user" else "group",
        "cashier_request",
        cashier_keyboard(int(row["id"]), str(row["status"])),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        if await is_admin_member(update, context):
            await update.effective_message.reply_text("أوامر الإدارة متاحة عبر /users و /export و /help_admin.")
        return
    user = update.effective_user
    assert user is not None
    existing = DB.get_user(user.id)
    if not existing:
        pending[key_for(update)] = {"mode": "register"}
        await update.effective_message.reply_text(
            "مرحباً. هذا بوت خاص لتسجيل طلبات استرجاع مصاريف المهام.\n\n"
            "أرسل اسمك الرسمي كما يجب أن يظهر في السجلات. بعد التسجيل لا يمكنك تغييره بنفسك، ويستطيع تعديلَه أعضاء مجموعة الإدارة فقط."
        )
        return
    DB.touch_user(user.id, user.username, profile_link(user.id, user.username))
    if existing["status"] != "active":
        await update.effective_message.reply_text("حسابك غير نشط حالياً. راجع مجموعة الإدارة.")
        return
    await update.effective_message.reply_text("اختر العملية المطلوبة:", reply_markup=main_menu())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if private_chat(update):
        pending.pop(key_for(update), None)
        await update.effective_message.reply_text("تم إلغاء الإدخال الحالي.", reply_markup=main_menu())


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    user = update.effective_user
    assert user is not None
    db_user = DB.get_user(user.id)
    if not db_user or db_user["status"] != "active":
        await update.effective_message.reply_text("يجب إكمال التسجيل وتفعيل الحساب أولاً. استخدم /start.")
        return
    pending[key_for(update)] = {"mode": "new_request", "step": "mosque", "data": {}}
    await update.effective_message.reply_text("أرسل اسم المسجد أو اسم المهمة التي صُرفت عليها المصاريف.")


async def ask_next_request_field(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> None:
    step = state["step"]
    prompts = {
        "mosque": "أرسل اسم المسجد أو المهمة.",
        "wilaya": "أرسل الولاية.",
        "duration": "أرسل مدة المهمة، مثال: 3 أيام أو من 01 إلى 03 مارس.",
        "amount": "أرسل المبلغ المطلوب بالدينار الجزائري، مثال: 12500.00.",
        "details": "أرسل أي ملاحظات إضافية، أو أرسل /skip إذا لا توجد ملاحظات.",
        "attachment": "أرسل صورة أو ملفاً يثبت المصروف، أو أرسل /skip إذا لا يوجد مرفق.",
    }
    await update.effective_message.reply_text(prompts[step])


async def finalize_request(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], request_id: int | None = None) -> None:
    data = state["data"]
    try:
        expense = ExpenseInput(
            mosque_name=data["mosque_name"],
            wilaya=data["wilaya"],
            duration_text=data["duration_text"],
            amount_requested=Decimal(data["amount_requested"]),
            currency="DZD",
            additional_details=data.get("additional_details", ""),
        )
        user_id = update.effective_user.id  # type: ignore[union-attr]
        attachments = data.get("attachments", [])
        if request_id:
            row = DB.user_resubmit(request_id, user_id, expense, attachments)
        else:
            row = DB.create_request(user_id, expense, attachments)
        full_row = DB.get_request(int(row["id"]))
        assert full_row is not None
        pending.pop(key_for(update), None)
        await update.effective_message.reply_text(
            "تم إرسال طلبك إلى الإدارة بنجاح. رقم الطلب: " + str(full_row["public_id"]) + "\nستصلك رسالة عند طلب تعديل أو إلغاء أو تأكيد الدفع."
        )
        await notify_admin_group(context, full_row)
    except (DomainError, KeyError, ValueError) as exc:
        await update.effective_message.reply_text(f"لم يتم حفظ الطلب: {exc}\nأرسل القيمة الصحيحة أو استخدم /cancel.")


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    state = pending.get(key_for(update))
    if not state:
        return
    if state.get("mode") in {"admin_rename", "admin_edit", "admin_reason", "cashier_reason", "cashier_payment"}:
        await group_text(update, context)
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if state["mode"] == "register":
        try:
            user = update.effective_user
            assert user is not None
            DB.register_user(user.id, text, user.username, profile_link(user.id, user.username))
            pending.pop(key_for(update), None)
            await message.reply_text("تم تسجيل اسمك بنجاح. يمكنك الآن إنشاء طلب استرجاع مصاريف.", reply_markup=main_menu())
        except DomainError as exc:
            await message.reply_text(str(exc))
        return
    if state["mode"] not in {"new_request", "resubmit"}:
        return
    step = state["step"]
    if step == "mosque":
        state["data"]["mosque_name"] = text
        state["step"] = "wilaya"
    elif step == "wilaya":
        state["data"]["wilaya"] = text
        state["step"] = "duration"
    elif step == "duration":
        state["data"]["duration_text"] = text
        state["step"] = "amount"
    elif step == "amount":
        try:
            state["data"]["amount_requested"] = str(Decimal(text.replace(",", ".")))
        except Exception:
            await message.reply_text("أرسل المبلغ كرقم فقط، مثال: 12500.00")
            return
        state["step"] = "details"
    elif step == "details":
        state["data"]["additional_details"] = "" if text == "/skip" else text
        state["step"] = "attachment"
    elif step == "attachment":
        if text != "/skip":
            await message.reply_text("أرسل صورة أو ملفاً، أو /skip للمتابعة دون مرفق.")
            return
        request_id = state.get("request_id")
        await finalize_request(update, context, state, request_id)
        return
    await ask_next_request_field(update, context, state)


async def private_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private_chat(update):
        return
    state = pending.get(key_for(update))
    if not state or state.get("step") != "attachment" or state.get("mode") not in {"new_request", "resubmit"}:
        return
    message = update.effective_message
    if message.photo:
        photo = message.photo[-1]
        state["data"].setdefault("attachments", []).append({"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "media_type": "photo"})
    elif message.document:
        state["data"].setdefault("attachments", []).append({"file_id": message.document.file_id, "file_unique_id": message.document.file_unique_id, "media_type": "document", "original_name": message.document.file_name})
    else:
        return
    request_id = state.get("request_id")
    await finalize_request(update, context, state, request_id)


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_member(update, context):
        return
    users, total = DB.list_users(0, ADMIN_PAGE_SIZE)
    if not users:
        await update.effective_message.reply_text("لا يوجد مستخدمون مسجلون بعد.")
        return
    text = f"<b>المستخدمون المسجلون</b> (الإجمالي: {total})\n\n" + "\n\n".join(user_summary(row) for row in users)
    buttons = [[InlineKeyboardButton(f"تغيير اسم {row['full_name'][:18]}", callback_data=f"a:rename:{row['telegram_user_id']}")] for row in users]
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_member(update, context):
        return
    rows = DB.confirmed_requests()
    workbook = build_confirmed_workbook(rows)
    await update.effective_message.reply_document(document=workbook, filename=f"confirmed_expenses_{date.today().isoformat()}.xlsx", caption=f"تم تصدير {len(rows)} طلباً مؤكداً.")


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_member(update, context):
        return
    await update.effective_message.reply_text(
        "<b>واجهة الإدارة</b>\n\n"
        "/users — عرض المستخدمين المسجلين وروابطهم وتغيير أسمائهم.\n"
        "/export — تصدير الطلبات المؤكدة إلى Excel.\n"
        "/set_cashier user <telegram_id> — ضبط الصندوق كمستخدم خاص.\n"
        "/set_cashier group <chat_id> — ضبط الصندوق كمجموعة.\n"
        "/cancel — إلغاء إدخال سبب جارٍ.\n\n"
        "أعضاء المجموعة متساوون؛ كل إجراء يعتمد على عضوية المجموعة ويُسجّل بمعرف واسم المنفذ.", parse_mode=ParseMode.HTML
    )


async def set_cashier_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_member(update, context):
        return
    args = context.args
    if len(args) != 2 or args[0] not in {"user", "group"}:
        await update.effective_message.reply_text("الصيغة: /set_cashier user <telegram_id> أو /set_cashier group <chat_id>")
        return
    try:
        target_id = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("المعرف يجب أن يكون رقماً.")
        return
    DB.set_cashier(args[0], target_id, update.effective_user.id, actor_name(update))  # type: ignore[union-attr]
    await update.effective_message.reply_text("تم حفظ وجهة الصندوق. يجب أن يكون البوت قادراً على مراسلة المستخدم أو موجوداً في المجموعة.")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 2:
        return
    scope, action = parts[0], parts[1]
    try:
        if scope == "u":
            await user_callback(query, context, action, parts[2:] if len(parts) > 2 else [])
        elif scope == "a":
            await admin_callback(query, context, action, parts[2:] if len(parts) > 2 else [])
        elif scope == "c":
            await cashier_callback(query, context, action, parts[2:] if len(parts) > 2 else [])
    except DomainError as exc:
        await query.message.reply_text(str(exc))
    except Exception:
        logger.exception("فشل تنفيذ callback %s", query.data)
        await query.message.reply_text("حدث خطأ غير متوقع. لم يتم تغيير حالة الطلب.")


async def user_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str, args: list[str]) -> None:
    user = query.from_user
    chat = query.message.chat
    if chat.type != ChatType.PRIVATE:
        return
    if action == "new":
        db_user = DB.get_user(user.id)
        if not db_user or db_user["status"] != "active":
            await query.message.reply_text("يجب إكمال التسجيل وتفعيل الحساب أولاً. استخدم /start.")
            return
        pending[(chat.id, user.id)] = {"mode": "new_request", "step": "mosque", "data": {}}
        await query.message.reply_text("أرسل اسم المسجد أو اسم المهمة التي صُرفت عليها المصاريف.")
        return
    if action == "list":
        rows = DB.get_user_requests(user.id)
        if not rows:
            await query.message.reply_text("لا توجد طلبات مسجلة لك.")
        else:
            await query.message.reply_text("\n\n".join(request_summary({**row, "full_name": DB.get_user(user.id)["full_name"], "username": DB.get_user(user.id).get("username")}) for row in rows), parse_mode=ParseMode.HTML)
        return
    if action == "help":
        await query.message.reply_text("لا يمكنك تعديل الاسم بنفسك. اطلب من عضو الإدارة تنفيذ ذلك من مجموعة الإدارة.")
        return
    if not args:
        return
    request_id = int(args[0])
    row = DB.get_request(request_id)
    if not row or int(row["user_id"]) != user.id:
        raise DomainError("الطلب غير موجود أو لا يخصك.")
    if action == "cancel":
        updated = DB.user_cancel(request_id, user.id)
        full = DB.get_request(request_id)
        await query.message.reply_text("تم إلغاء الطلب.")
        if full:
            await notify_admin_group(context, full, "<b>أُبلغت الإدارة بإلغاء المستخدم للطلب</b>\n\n" + request_summary(full))
        return
    if action == "edit":
        pending[(chat.id, user.id)] = {"mode": "resubmit", "request_id": request_id, "step": "mosque", "data": {}}
        await query.message.reply_text("ابدأ تعديل الطلب بإرسال اسم المسجد أو اسم المهمة الجديد.")


async def admin_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str, args: list[str]) -> None:
    fake_update = Update(update_id=0, callback_query=query)
    if not await is_admin_member(fake_update, context):
        raise DomainError("هذا الإجراء متاح لأعضاء مجموعة الإدارة فقط.")
    if action == "users":
        return
    if action == "export":
        return
    if action == "rename":
        if not args:
            return
        target_id = int(args[0])
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "admin_rename", "target_user_id": target_id}
        await query.message.reply_text("أرسل الاسم الرسمي الجديد للمستخدم. لن يتم تغييره إلا بعد استلام هذه الرسالة.")
        return
    if not args:
        return
    request_id = int(args[0])
    row = DB.get_request(request_id)
    if not row:
        raise DomainError("الطلب غير موجود.")
    if action == "approve":
        updated = DB.admin_approve(request_id, query.from_user.id, actor_name_from_user(query.from_user))
        full = DB.get_request(request_id)
        if full:
            await notify_cashier(context, full)
            await query.message.reply_text("تم اعتماد الطلب وتحويله إلى الصندوق.")
        return
    if action == "edit":
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "admin_edit", "request_id": request_id}
        await query.message.reply_text("أرسل التعديل بهذا الترتيب مفصولاً بعلامة |:\nاسم المسجد | الولاية | مدة المهمة | المبلغ | الملاحظات الاختيارية")
        return
    if action == "chg":
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "admin_reason", "action": "changes", "request_id": request_id}
        await query.message.reply_text("أرسل سبب طلب التعديل. السبب إلزامي وسيصل إلى المستخدم.")
        return
    if action == "cancel":
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "admin_reason", "action": "cancel", "request_id": request_id}
        await query.message.reply_text("أرسل سبب إلغاء الطلب. السبب إلزامي وسيصل إلى المستخدم.")
        return
    if action == "events":
        events = DB.request_events(request_id)
        if not events:
            await query.message.reply_text("لا يوجد سجل أحداث لهذا الطلب.")
            return
        lines = [f"<b>سجل {row['public_id']}</b>"]
        for event in events:
            lines.append(f"{event['created_at']} — {event['event_type']} — {event.get('actor_name') or 'النظام'} — {event.get('reason') or ''}")
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def actor_name_from_user(user) -> str:
    return " ".join(part for part in [user.first_name, user.last_name] if part).strip() or str(user.id)


async def cashier_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str, args: list[str]) -> None:
    fake_update = Update(update_id=0, callback_query=query)
    if not await is_cashier_actor(fake_update, context):
        raise DomainError("هذا الإجراء متاح للصندوق المحدد فقط.")
    if not args:
        return
    request_id = int(args[0])
    row = DB.get_request(request_id)
    if not row:
        raise DomainError("الطلب غير موجود.")
    cashier_name = actor_name_from_user(query.from_user)
    if action == "start":
        DB.cashier_start_review(request_id, query.from_user.id, cashier_name)
        await query.message.reply_text("تم فتح الطلب للمراجعة.")
        return
    if action in {"payfull", "paypartial"}:
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "cashier_payment", "request_id": request_id, "partial": action == "paypartial"}
        prompt = "أرسل: المبلغ | طريقة الدفع | ملاحظة اختيارية\nمثال: 12500 | نقداً | تم التسليم يداً بيد"
        await query.message.reply_text(prompt)
        return
    if action == "reject":
        pending[(query.message.chat.id, query.from_user.id)] = {"mode": "cashier_reason", "request_id": request_id}
        await query.message.reply_text("أرسل سبب رفض أو تعليق الدفع.")


async def group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    state = pending.get(key_for(update))
    if not state:
        return
    text = (update.effective_message.text or "").strip()
    chat_id, user_id = key_for(update)
    try:
        if state["mode"] == "admin_rename":
            if not await is_admin_member(update, context):
                return
            DB.update_user_name(int(state["target_user_id"]), text, user_id, actor_name(update))
            pending.pop((chat_id, user_id), None)
            await update.effective_message.reply_text("تم تعديل الاسم وتسجيل العملية في سجل التدقيق.")
            return
        if state["mode"] == "admin_edit":
            if not await is_admin_member(update, context):
                return
            parts = [part.strip() for part in text.split("|", 4)]
            if len(parts) < 4:
                await update.effective_message.reply_text("الصيغة المطلوبة: اسم المسجد | الولاية | المدة | المبلغ | الملاحظات الاختيارية")
                return
            expense = ExpenseInput(
                mosque_name=parts[0],
                wilaya=parts[1],
                duration_text=parts[2],
                amount_requested=Decimal(parts[3].replace(",", ".")),
                currency="DZD",
                additional_details=parts[4] if len(parts) == 5 else "",
            )
            request_id = int(state["request_id"])
            DB.admin_edit(request_id, expense, user_id, actor_name(update), "تعديل مباشر من الإدارة")
            pending.pop((chat_id, user_id), None)
            row = DB.get_request(request_id)
            if row:
                await notify_user(context, row, "<b>عدّلت الإدارة بيانات طلبك</b>\n\n" + request_summary(row))
                await update.effective_message.reply_text("تم حفظ تعديل الإدارة. يجب اعتماد النسخة المعدلة قبل تحويلها إلى الصندوق.")
            return
        if state["mode"] == "admin_reason":
            if not await is_admin_member(update, context):
                return
            request_id = int(state["request_id"])
            if state["action"] == "changes":
                DB.admin_request_changes(request_id, user_id, actor_name(update), text)
            else:
                DB.admin_cancel(request_id, user_id, actor_name(update), text)
            pending.pop((chat_id, user_id), None)
            row = DB.get_request(request_id)
            if row:
                await notify_user(context, row, "<b>تحديث على طلبك</b>\n\n" + request_summary(row), user_request_keyboard(request_id, row["status"]))
            await update.effective_message.reply_text("تم حفظ الإجراء وإبلاغ المستخدم.")
            return
        if state["mode"] == "cashier_reason":
            if not await is_cashier_actor(update, context):
                return
            request_id = int(state["request_id"])
            DB.cashier_reject(request_id, user_id, actor_name(update), text)
            pending.pop((chat_id, user_id), None)
            row = DB.get_request(request_id)
            if row:
                await notify_user(context, row, "<b>تحديث على طلب الدفع</b>\n\n" + request_summary(row, True), user_request_keyboard(request_id, row["status"]))
                await notify_admin_group(context, row, "<b>الصندوق علّق أو رفض الدفع</b>\n\n" + request_summary(row))
            await update.effective_message.reply_text("تم تسجيل سبب الرفض أو التعليق وإبلاغ الأطراف.")
            return
        if state["mode"] == "cashier_payment":
            if not await is_cashier_actor(update, context):
                return
            parts = [part.strip() for part in text.split("|", 2)]
            if len(parts) < 2:
                await update.effective_message.reply_text("الصيغة المطلوبة: المبلغ | طريقة الدفع | ملاحظة اختيارية")
                return
            paid_amount = Decimal(parts[0].replace(",", "."))
            method = parts[1]
            note = parts[2] if len(parts) == 3 else ""
            request_id = int(state["request_id"])
            allow_partial = DB.get_setting("allow_partial_payment", "false") == "true"
            DB.cashier_payment(request_id, user_id, actor_name(update), paid_amount, method, note, allow_partial)
            pending.pop((chat_id, user_id), None)
            row = DB.get_request(request_id)
            if row:
                await notify_user(context, row, "<b>تم تأكيد استلامك للمبلغ</b>\n\n" + request_summary(row))
                await notify_admin_group(context, row, "<b>تم تأكيد الدفع من الصندوق</b>\n\n" + request_summary(row))
            await update.effective_message.reply_text("تم تسجيل الدفع وإبلاغ الإدارة والمستخدم.")
    except (DomainError, ValueError) as exc:
        await update.effective_message.reply_text(f"لم يتم حفظ العملية: {exc}")


async def retry_deliveries(context: ContextTypes.DEFAULT_TYPE) -> None:
    for delivery in DB.pending_deliveries(20):
        payload = delivery.get("payload") or {}
        text = payload.get("text")
        request_id = payload.get("request_id")
        if not text:
            continue
        reply_markup = None
        row = DB.get_request(int(request_id)) if request_id else None
        if row and delivery.get("message_kind") == "admin_request":
            reply_markup = admin_request_keyboard(int(row["id"]), str(row["status"]))
        elif row and delivery.get("message_kind") == "cashier_request":
            reply_markup = cashier_keyboard(int(row["id"]), str(row["status"]))
        elif row and delivery.get("message_kind") == "user_notification":
            reply_markup = user_request_keyboard(int(row["id"]), str(row["status"]))
        try:
            await context.bot.send_message(chat_id=int(delivery["recipient_chat_id"]), text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            DB.mark_delivery(int(delivery["id"]), "sent")
        except Exception as exc:
            DB.mark_delivery(int(delivery["id"]), "failed", str(exc)[:1000])


async def expire_stale(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        stale_hours = int(DB.get_setting("stale_hours", "72") or "72")
    except ValueError:
        stale_hours = 72
    for row in DB.pending_expired_requests(stale_hours):
        # لا نغير الحالة تلقائياً كي لا نغلق طلباً مالياً دون رؤية بشرية؛ نرسل تنبيهاً للإدارة.
        await notify_admin_group(context, row, "<b>تنبيه: طلب متأخر عن المعالجة</b>\n\n" + request_summary(row))


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-reimbursement-bot"})


async def telegram_webhook(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise web.HTTPUnauthorized(text="invalid webhook secret")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.json_response({"ok": True})


async def on_startup(app: web.Application) -> None:
    await application.initialize()
    await application.start()
    if PUBLIC_URL:
        await application.bot.set_webhook(url=f"{PUBLIC_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET or None, allowed_updates=Update.ALL_TYPES)
    logger.info("bot started")


async def on_cleanup(app: web.Application) -> None:
    if PUBLIC_URL:
        await application.bot.delete_webhook(drop_pending_updates=False)
    await application.stop()
    await application.shutdown()
    DB.close()


def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("new_request", new_request))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("help_admin", admin_help))
    app.add_handler(CommandHandler("set_cashier", set_cashier_command))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^(u|a|c):"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL), private_media))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text))
    app.job_queue.run_repeating(retry_deliveries, interval=60, first=30, name="retry-deliveries")
    app.job_queue.run_repeating(expire_stale, interval=3600, first=600, name="stale-notifications")
    return app


application = build_application()


async def run() -> None:
    await asyncio.to_thread(DB.open)
    if PUBLIC_URL:
        aio = web.Application()
        aio.router.add_get("/health", health)
        aio.router.add_post("/telegram/webhook", telegram_webhook)
        aio.on_startup.append(on_startup)
        aio.on_cleanup.append(on_cleanup)
        runner = web.AppRunner(aio)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("HTTP server listening on %s", PORT)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    else:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            DB.close()


if __name__ == "__main__":
    asyncio.run(run())
