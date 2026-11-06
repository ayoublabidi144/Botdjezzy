# -*- coding: utf-8 -*-
"""
Djezzy Assistant Telegram Bot (Local Polling Edition - Pro UI)
"""

import os
import re
import uuid
import logging
from datetime import date
from urllib.parse import quote

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

BASE = "https://apim.djezzy.dz/mobile-api"
API = BASE + "/api/v1"
CONTENT_API = BASE + "/content/api/v1"

BOT_TOKEN = "9204415595:AAFqLv1EUEt9mLbe22K0xhM6mraR4sDwals"
ADMIN_ID = None

DJZ_CLIENT_ID     = "87pIExRhxBb3_wGsA5eSEfyATloa"
DJZ_CLIENT_SECRET = "uf82p68Bgisp8Yg1Uz8Pf6_v1XYa"
DJZ_URL_SUBS      = "https://apim.djezzy.dz/mobile-api/api/v1/subscribers/connected-products-balances/{msisdn}"
DJZ_PKG_CODE      = "GIFTWALKWIN2GO"
DJZ_COOLDOWN      = 7 * 24 * 3600

APP_CLIENT_ID = os.getenv("APP_CLIENT_ID", "87pIExRhxBb3_wGsA5eSEfyATloa").strip()
APP_CLIENT_SECRET = os.getenv("APP_CLIENT_SECRET", "uf82p68Bgisp8Yg1Uz8Pf6_v1XYa").strip()

UA = "MobileApp/3.0.0"
TIMEOUT = 25
MAX_INVITES_PER_DAY = 5

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("djezzy_bot")

sessions = {}
pending_actions = {}
daily_usage = {"date": date.today(), "count": 0}


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("0") and len(digits) == 10:
        digits = "213" + digits[1:]
    elif len(digits) == 9 and digits[0] in "567":
        digits = "213" + digits
    if not re.fullmatch(r"213[567]\d{8}", digits):
        raise ValueError("أدخل رقم جازي صحيحاً، مثال: 0770123456")
    return digits


def form_headers(token=None):
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def json_headers(token):
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "ar",
        "Authorization": f"Bearer {token}",
    }


def request_otp(phone):
    if not APP_CLIENT_ID:
        raise RuntimeError("APP_CLIENT_ID غير مضبوط.")
    params = {
        "msisdn": phone,
        "client_id": APP_CLIENT_ID,
        "scope": "smsotp",
    }
    consent = {
        "consent-agreement": [{"marketing-notifications": False}],
        "is-consent": True,
    }
    headers = {
        "User-Agent": "MobileApp/3.0.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "accept-language": "fr",
    }
    try:
        response = requests.post(
            BASE + "/oauth2/registration",
            params=params,
            json=consent,
            headers=headers,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"تعذر الاتصال بخادم جازي: {exc}") from exc

    if response.status_code not in (200, 201, 202):
        detail = response.text[:400]
        raise RuntimeError(f"فشل طلب رمز التحقق (HTTP {response.status_code}): {detail}")
    return None


def verify_otp(phone, otp, token=None):
    if not APP_CLIENT_ID or not APP_CLIENT_SECRET:
        raise RuntimeError("إعدادات APP_CLIENT_ID أو APP_CLIENT_SECRET ناقصة.")
    r = requests.post(
        BASE + "/oauth2/token",
        headers=form_headers(),
        data={
            "grant_type": "mobile",
            "mobileNumber": phone,
            "otp": otp,
            "client_id": APP_CLIENT_ID,
            "client_secret": APP_CLIENT_SECRET,
            "scope": "djezzyAppV2",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def refresh_session(user):
    if not user.get("refresh_token"):
        return False
    r = requests.post(
        BASE + "/oauth2/token",
        headers=form_headers(),
        data={
            "grant_type": "refresh_token",
            "refresh_token": user["refresh_token"],
            "client_id": APP_CLIENT_ID,
            "client_secret": APP_CLIENT_SECRET,
            "scope": "djezzyAppV2",
        },
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return False
    data = r.json()
    user["access_token"] = data["access_token"]
    user["refresh_token"] = data.get("refresh_token", user["refresh_token"])
    return True


def api_request(user, method, path, *, content=False, **kwargs):
    root = CONTENT_API if content else API
    url = root + path
    r = requests.request(
        method, url, headers=json_headers(user["access_token"]),
        timeout=TIMEOUT, **kwargs
    )
    if r.status_code == 401 and refresh_session(user):
        r = requests.request(
            method, url, headers=json_headers(user["access_token"]),
            timeout=TIMEOUT, **kwargs
        )
    if r.status_code == 402 and "/services/shake/" in path:
        try:
            return r.json()
        except ValueError:
            return {"status": 402, "message": "تم قبول طلب التفعيل وفق استجابة جازي."}
    r.raise_for_status()
    if not r.text.strip():
        return {}
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:1000]}


def data_of(result):
    return result.get("data", {}) if isinstance(result, dict) else {}


def lang_name(value):
    if isinstance(value, dict):
        return value.get("ar") or value.get("fr") or value.get("en") or "غير متوفر"
    return value if value is not None else "غير متوفر"


def get_user(update):
    return sessions.get(update.effective_user.id)


async def send_error(target, exc):
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 429:
            text = "⚠️ الخادم يطلب الانتظار بسبب كثرة الطلبات (429)."
        elif code == 401:
            text = "🔐 انتهت الجلسة أو تعذر تجديدها. يرجى تسجيل الدخول من جديد."
        elif code == 403:
            text = "⛔ جازي رفض الطلب (403)."
        else:
            text = f"❌ رفض خادم جازي الطلب. HTTP {code}."
    elif isinstance(exc, requests.RequestException):
        text = "📡 تعذر الاتصال بخادم جازي. تحقق من الإنترنت وحاول لاحقاً."
    else:
        log.exception("Unhandled error", exc_info=exc)
        text = f"❌ حدث خطأ غير متوقع."
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])
    if hasattr(target, "reply_text"):
        await target.reply_text(text, reply_markup=kb)
    elif hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=kb)


def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 تسجيل الدخول", callback_data="btn_login"), InlineKeyboardButton("💰 الرصيد والخط", callback_data="btn_balance")],
        [InlineKeyboardButton("📦 الباقات النشطة", callback_data="btn_packages"), InlineKeyboardButton("📜 سجل الاشتراكات", callback_data="btn_history")],
        [InlineKeyboardButton("🎲 فحص هز واربح", callback_data="btn_shake"), InlineKeyboardButton("🚶‍♂️ فحص امشي واربح", callback_data="btn_walk")],
        [InlineKeyboardButton("📨 إرسال دعوة", callback_data="btn_invite"), InlineKeyboardButton("📋 سجل الدعوات", callback_data="btn_invitations")],
        [InlineKeyboardButton("🎁 كتالوج الباقات", callback_data="btn_catalog"), InlineKeyboardButton("🧩 الخدمات الرقمية", callback_data="btn_services")],
        [InlineKeyboardButton("🚪 تسجيل الخروج", callback_data="btn_logout")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 **مساعد جازي الاحترافي**\n\n"
        "مرحباً بك في بوت إدارة خدمات وحصص جازي.\n"
        "اختر أحد الخيارات أدناه للبدء:"
    )
    markup = get_main_menu_keyboard()
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    value = (update.message.text or "").strip()
    uid = update.effective_user.id

    if state == "phone":
        try:
            phone = normalize_phone(value)
            token = request_otp(phone)
            context.user_data.update({"login_phone": phone, "public_token": token, "state": "otp"})
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")]])
            await update.message.reply_text(
                "📩 **تم إرسال رمز التحقق (OTP) إلى رقمك.**\nيرجى إرسال الرمز هنا الآن لإتمام التسجيل:",
                reply_markup=kb, parse_mode="Markdown"
            )
        except Exception as exc:
            await send_error(update.message, exc)
        return

    if state == "otp":
        if not re.fullmatch(r"\d{4,8}", value):
            await update.message.reply_text("⚠️ رمز OTP يجب أن يتكون من أرقام صحيحة.")
            return
        try:
            phone = context.user_data["login_phone"]
            tokens = verify_otp(phone, value, context.user_data["public_token"])
            sessions[uid] = {
                "phone": phone,
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
            }
            context.user_data.clear()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])
            await update.message.reply_text("✅ **تم تسجيل الدخول بنجاح!**", reply_markup=kb, parse_mode="Markdown")
        except Exception as exc:
            context.user_data.clear()
            await send_error(update.message, exc)
        return

    if state == "invite_phone":
        try:
            receiver = normalize_phone(value)
            sender = sessions[uid]["phone"]
            if receiver == sender:
                await update.message.reply_text("⚠️ لا يمكنك إرسال دعوة إلى رقمك نفسه.")
                return
            pending_actions[uid] = {"kind": "invite", "receiver": receiver}
            context.user_data.pop("state", None)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأكيد الدعوة", callback_data="confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")]
            ])
            await update.message.reply_text(
                f"📨 إرسال دعوة إلى الرقم: `{receiver}`؟\nتأكد من موافقة صاحب الرقم أولاً.",
                reply_markup=kb, parse_mode="Markdown"
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])
    await update.message.reply_text("اختر من الأوامر أو اضغط القائمة الرئيسية:", reply_markup=kb)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data

    if data == "main_menu":
        await start(update, context)
        return
    elif data == "btn_cancel":
        pending_actions.pop(uid, None)
        context.user_data.clear()
        await q.edit_message_text("❌ تم الإلغاء بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        return
    elif data == "btn_login":
        context.user_data.clear()
        context.user_data["state"] = "phone"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")]])
        await q.edit_message_text("📱 **أرسل رقم جازي الخاص بك (مثال: 0770123456):**", reply_markup=kb, parse_mode="Markdown")
        return
    elif data == "btn_logout":
        sessions.pop(uid, None)
        context.user_data.clear()
        pending_actions.pop(uid, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        await q.edit_message_text("🚪 **تم تسجيل الخروج ومسح الجلسة بنجاح.**", reply_markup=kb, parse_mode="Markdown")
        return
    elif data == "btn_invite":
        if uid not in sessions:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔑 تسجيل الدخول", callback_data="btn_login"), InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])
            await q.edit_message_text("⚠️ يرجى تسجيل الدخول أولاً لاستعمال هذه الميزة.", reply_markup=kb)
            return
        context.user_data["state"] = "invite_phone"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")]])
        await q.edit_message_text("📨 **أرسل رقم الشخص المراد إرسال الدعوة إليه:**", reply_markup=kb, parse_mode="Markdown")
        return

    # دو جلب البيانات
    user = sessions.get(uid)
    if not user:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔑 تسجيل الدخول", callback_data="btn_login"), InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])
        await q.edit_message_text("⚠️ يرجى تسجيل الدخول أولاً باستعمال زر تسجيل الدخول.", reply_markup=kb)
        return

    try:
        if data == "btn_balance":
            res = data_of(api_request(user, "GET", f"/subscribers/main-balance/{user['phone']}"))
            info = res.get("customerInformations", {})
            text = (
                "💰 **معلومات الرصيد والخط**\n\n"
                f"• الرصيد الرئيسي: `{res.get('mainBalance', 'غير متوفر')} دج`\n"
                f"• المستحقات: `{res.get('due', 'غير متوفر')}`\n"
                f"• حالة الخط: `{info.get('status', 'غير متوفر')}`\n"
                f"• نوع الاتصال: `{info.get('connectionType', 'غير متوفر')}`\n"
                f"• نوع الدفع: `{info.get('paymentType', 'غير متوفر')}`"
            )
            await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

        elif data == "btn_packages":
            res = data_of(api_request(user, "GET", f"/subscribers/connected-products-balances/{user['phone']}"))
            items = res.get("products", [])
            lines = [
                "📦 **الباقات والحصص النشطة**",
                f"الرصيد الرئيسي: `{res.get('mainBalance', 'غير متوفر')} دج`\n"
            ]
            if not items:
                lines.append("لا توجد باقات نشطة حالياً.")
            for item in items[:25]:
                # تم إخفاء الرموز والأكواد التقنية المعقدة تماماً والاكتفاء باسم الباقة وتفاصيلها النظيفة
                p_name = lang_name(item.get('commercialName'))
                lines.append(f"• **{p_name}**")
                for bal in item.get("balances", [])[:3]:
                    lines.append(f"  - المتبقي: `{bal.get('remaining', '?')} / {bal.get('totalAmount', '?')}`\n  - الانتهاء: `{bal.get('expiryAt', 'غير متوفر')}`")
            
            await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

        elif data == "btn_history":
            res = data_of(api_request(user, "GET", f"/subscribers/subscription-history/{user['phone']}"))
            if isinstance(res, dict): res = res.get("history", [])
            lines = ["📜 **سجل الاشتراكات الأخيرة:**\n"]
            for item in res[:15] if isinstance(res, list) else []:
                lines.append(f"• `{lang_name(item.get('commercialName'))}` | `{item.get('packageFee', '?')} دج` | الحالة: `{item.get('status', '?')}`")
            if len(lines) == 1: lines.append("لا يوجد سجل اشتراكات متاح.")
            await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

        elif data == "btn_shake":
            res = data_of(api_request(user, "GET", f"/services/shake/{user['phone']}"))
            code = res.get("code")
            if not code:
                await q.edit_message_text("⚠️ لم يرجع الخادم عرض هز واربح متاحاً لحسابك حالياً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
                return
            name = lang_name(res.get("commercialName"))
            desc = lang_name(res.get("mediumDescription"))
            price = res.get("price", "0")
            pending_actions[uid] = {"kind": "shake", "code": code}
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تفعيل العرض", callback_data="confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
            ])
            await q.edit_message_text(
                f"🎲 **عروض هز واربح المتاحة:**\n\n• **{name}**\n- الوصف: {desc}\n- السعر: `{price} دج`",
                reply_markup=kb, parse_mode="Markdown"
            )

        elif data == "btn_walk":
            balances = data_of(api_request(user, "GET", f"/subscribers/connected-products-balances/{user['phone']}"))
            active_walk = [p for p in balances.get("products", []) if str(p.get("code", "")).startswith("WAW_")]
            if active_walk:
                p = active_walk[0]
                expiry = p.get("expiryAt")
                for b in p.get("balances", []):
                    expiry = b.get("expiryAt", expiry)
                await q.edit_message_text(
                    f"🚶‍♂️ **مكافأة المشي مفعلة حالياً:**\n- انتهاء الصلاحية: `{expiry or 'غير متوفر'}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown"
                )
                return
            res = data_of(api_request(user, "GET", f"/services/walk/campaign/{user['phone']}"))
            levels = res.get("wawLevels", [])
            lines = ["🚶‍♂️ **حالة امشي واربح:**\n"]
            reward_code = None
            for level in levels:
                reward = level.get("reward")
                if isinstance(reward, dict):
                    reward_code = reward.get("packageCode") or reward.get("code")
                elif isinstance(reward, str):
                    reward_code = reward
                lines.append(f"• الخطوات: `{level.get('steps', '?')}` — المكافأة متاحة")
            if reward_code:
                pending_actions[uid] = {"kind": "walk", "code": reward_code}
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تفعيل المكافأة", callback_data="confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="btn_cancel")],
                    [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
                ])
                await q.edit_message_text("\n".join(lines) + "\n\nهل تريد تفعيل المكافأة الآن؟", reply_markup=kb, parse_mode="Markdown")
            else:
                await q.edit_message_text("⚠️ لا توجد مكافأة مشي جاهزة للتفعيل حالياً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

        elif data == "btn_invitations":
            res = data_of(api_request(user, "GET", f"/services/mgm/invitations/{user['phone']}"))
            items = res.get("invitations", [])
            lines = ["📋 **سجل الدعوات:**\n"]
            for item in items[:20]:
                lines.append(f"• `{item.get('msisdnReceiver', '?')}` — الحالة: `{item.get('status', '?')}`")
            if len(lines) == 1: lines.append("لا توجد دعوات مسجلة.")
            await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

        elif data == "btn_catalog":
            res = data_of(api_request(user, "GET", f"/subscribers/customer-offers/{user['phone']}", params={"tags": "STANDARD,SURPRISE,DIGITAL,FLEXY"}))
            lines = ["🎁 **كتالوج العروض المتاحة (عرض فقط):**\n"]
            for category in res if isinstance(res, list) else []:
                lines.append(f"\n📂 **{lang_name(category.get('title'))}**")
                for sub in category.get("subcategories", []):
                    lines.append(f"  ▪️ {lang_name(sub.get('title'))}")
                    for p in sub.get("packages", [])[:5]:
                        # تم إخفاء الرموز والأكواد التقنية المعقدة في الكتالوج وعرض الاسم والسعر بأسلوب نظيف
                        lines.append(f"    - {lang_name(p.get('commercialName'))} | `{p.get('price', '?')} دج`")
            await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

        elif data == "btn_services":
            res = api_request(user, "GET", f"/subscribers/{user['phone']}", content=True, params={"include": "content-available-services"})
            included = res.get("included", []) if isinstance(res, dict) else []
            lines = ["🧩 **الخدمات الرقمية المتاحة:**\n"]
            for item in included:
                if item.get("type") == "content-services":
                    attrs = item.get("attributes", {})
                    lines.append(f"• {attrs.get('category', '?')} — {lang_name(attrs.get('commercial-name', {}).get('text', {}))}")
            if len(lines) == 1: lines.append("لا توجد خدمات متاحة حالياً.")
            await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]), parse_mode="Markdown")

    except Exception as exc:
        await send_error(q, exc)


async def confirm_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    action = pending_actions.get(uid)
    user = sessions.get(uid)
    
    if not action or not user:
        await q.edit_message_text("⚠️ انتهت صلاحية الطلب أو الجلسة. أعد المحاولة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        return
    
    kind = action["kind"]
    try:
        if kind == "invite":
            today = date.today()
            if daily_usage["date"] != today:
                daily_usage.update({"date": today, "count": 0})
            if daily_usage["count"] >= MAX_INVITES_PER_DAY:
                await q.edit_message_text(f"⚠️ وصلت إلى الحد اليومي للدعوات ({MAX_INVITES_PER_DAY}).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
                pending_actions.pop(uid, None)
                return
            api_request(user, "POST", f"/services/mgm/send-invitation/{user['phone']}", json={"msisdnReceiver": action["receiver"]})
            daily_usage["count"] += 1
            await q.edit_message_text("✅ تم إرسال طلب الدعوة بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        
        elif kind == "shake":
            api_request(user, "POST", f"/services/shake/{user['phone']}", json={"packageCode": action["code"]})
            await q.edit_message_text("✅ تم إرسال طلب تفعيل عرض الهز بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
        
        elif kind == "walk":
            api_request(user, "POST", f"/services/walk/activate-reward/{user['phone']}", json={"packageCode": action["code"]})
            await q.edit_message_text("✅ تم إرسال طلب تفعيل مكافأة المشي بنجاح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
            
        pending_actions.pop(uid, None)
    except Exception as exc:
        pending_actions.pop(uid, None)
        await send_error(q, exc)


def main():
    if not BOT_TOKEN:
        raise SystemExit("يجب ضبط BOT_TOKEN أولاً.")
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(confirm_action, pattern=r"^confirm$"))
    app.add_handler(CallbackQueryHandler(callback_router, pattern=r"^(main_menu|btn_|cancel)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("Djezzy Pro Bot (UI Enhanced) is running locally...")
    app.run_polling()


if __name__ == "__main__":
    main()
