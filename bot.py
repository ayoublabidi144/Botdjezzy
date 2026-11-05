# -*- coding: utf-8 -*-
"""
Djezzy Assistant Telegram Bot (Local Polling Edition)
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

# تم وضع التوكن الجديد هنا
BOT_TOKEN = "9204415595:AAFqLv1EUEt9mLbe22K0xhM6mraR4sDwals"
ADMIN_ID = None  # تم إلغاء تقييد الأدمين لكي لا يظهر أي تقييد

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
        raise RuntimeError(
            f"فشل طلب رمز التحقق (HTTP {response.status_code}): {detail}"
        )
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


async def send_error(message, exc):
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 429:
            text = "الخادم يطلب الانتظار بسبب كثرة الطلبات (429). انتظر قبل إعادة المحاولة."
        elif code == 401:
            text = "انتهت الجلسة أو تعذر تجديدها. سجّل الدخول من جديد بـ /login."
        elif code == 403:
            text = "جازي رفض الطلب (403). قد يكون المسار أو صلاحية الحساب غير مقبول."
        else:
            text = f"رفض خادم جازي الطلب. HTTP {code}."
    elif isinstance(exc, requests.RequestException):
        text = "تعذر الاتصال بخادم جازي. تحقق من الإنترنت وحاول لاحقاً."
    else:
        log.exception("Unhandled error", exc_info=exc)
        text = f"حدث خطأ: {type(exc).__name__}"
    await message.reply_text(text)


def menu_text():
    return (
        "🤖 مساعد جازي\n\n"
        "🔐 /login — تسجيل الدخول برقمك\n"
        "💰 /balance — الرصيد ومعلومات الخط\n"
        "📦 /packages — الباقات النشطة\n"
        "🧾 /history — سجل الاشتراكات\n"
        "🎲 /shake — فحص عرض هز واربح\n"
        "🚶 /walk — فحص امشي واربح\n"
        "📨 /invite — إرسال دعوة بموافقة المستلم\n"
        "📋 /invitations — سجل الدعوات\n"
        "🎁 /catalog — عرض كتالوج الباقات (بدون شراء)\n"
        "🧩 /services — الخدمات الرقمية المتاحة\n"
        "🚪 /logout — تسجيل الخروج\n\n"
        "ملاحظة: توفر العروض والمكافآت يحدده حسابك وخادم جازي."
    )


async def start(update, context):
    await update.message.reply_text(menu_text())


async def login(update, context):
    context.user_data.clear()
    context.user_data["state"] = "phone"
    await update.message.reply_text("أرسل رقم جازي الخاص بك، مثال: 0770123456")


async def logout(update, context):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    pending_actions.pop(update.effective_user.id, None)
    await update.message.reply_text("تم تسجيل الخروج ومسح الجلسة من ذاكرة البوت.")


async def handle_text(update, context):
    state = context.user_data.get("state")
    value = (update.message.text or "").strip()
    uid = update.effective_user.id

    if state == "phone":
        try:
            phone = normalize_phone(value)
            token = request_otp(phone)
            context.user_data.update({"login_phone": phone, "public_token": token, "state": "otp"})
            await update.message.reply_text(
                "📩 طلبت رمز التحقق لرقمك. أرسل OTP هنا.\n"
                "لا ترسل الرمز لأي شخص، ولا تشاركه خارج محادثتك الخاصة مع البوت."
            )
        except Exception as exc:
            await send_error(update.message, exc)
        return

    if state == "otp":
        if not re.fullmatch(r"\d{4,8}", value):
            await update.message.reply_text("رمز OTP يجب أن يتكون من 4 إلى 8 أرقام.")
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
            await update.message.reply_text("✅ تم تسجيل الدخول. اكتب /start لعرض الخدمات.")
        except Exception as exc:
            context.user_data.clear()
            await send_error(update.message, exc)
        return

    if state == "invite_phone":
        try:
            receiver = normalize_phone(value)
            sender = sessions[uid]["phone"]
            if receiver == sender:
                await update.message.reply_text("لا يمكنك إرسال دعوة إلى رقمك نفسه.")
                return
            pending_actions[uid] = {"kind": "invite", "receiver": receiver}
            context.user_data.pop("state", None)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأكيد الدعوة", callback_data="confirm"),
                InlineKeyboardButton("إلغاء", callback_data="cancel"),
            ]])
            await update.message.reply_text(
                f"إرسال دعوة إلى {receiver}؟\nلا ترسل الدعوة إلا إذا وافق صاحب الرقم.",
                reply_markup=kb,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
        return

    await update.message.reply_text("اكتب /start لعرض الأوامر.")


async def need_user(update):
    user = get_user(update)
    if not user:
        await update.effective_message.reply_text("سجّل الدخول أولاً باستعمال /login.")
        return None
    return user


async def balance(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(user, "GET", f"/subscribers/main-balance/{user['phone']}"))
        info = result.get("customerInformations", {})
        await update.message.reply_text(
            "💰 معلومات الرصيد والخط\n"
            f"الرصيد: {result.get('mainBalance', 'غير متوفر')} دج\n"
            f"المستحقات: {result.get('due', 'غير متوفر')}\n"
            f"حالة الخط: {info.get('status', 'غير متوفر')}\n"
            f"نوع الاتصال: {info.get('connectionType', 'غير متوفر')}\n"
            f"نوع الدفع: {info.get('paymentType', 'غير متوفر')}"
        )
    except Exception as exc: await send_error(update.message, exc)


async def packages(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(user, "GET", f"/subscribers/connected-products-balances/{user['phone']}"))
        items = result.get("products", [])
        lines = [
            "📦 الباقات والحصص النشطة",
            f"الرصيد الرئيسي: {result.get('mainBalance', 'غير متوفر')} دج",
        ]
        if not items:
            lines.append("لا توجد باقات ظاهرة في الاستجابة.")
        for item in items[:25]:
            lines.append(
                f"• {lang_name(item.get('commercialName'))} "
                f"— {item.get('code', 'بدون كود')}"
            )
            for bal in item.get("balances", [])[:3]:
                lines.append(
                    f"   المتبقي: {bal.get('remaining', '?')} / "
                    f"{bal.get('totalAmount', '?')} — الانتهاء: {bal.get('expiryAt', '?')}"
                )
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc: await send_error(update.message, exc)


async def history(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(user, "GET", f"/subscribers/subscription-history/{user['phone']}"))
        if isinstance(result, dict): result = result.get("history", [])
        lines = ["🧾 آخر الاشتراكات:"]
        for item in result[:15] if isinstance(result, list) else []:
            lines.append(
                f"• {lang_name(item.get('commercialName'))} | "
                f"{item.get('packageFee', '?')} دج | {item.get('status', '?')} | "
                f"{item.get('subscriptionDateTime', '?')}"
            )
        if len(lines) == 1: lines.append("لا يوجد سجل أو أن صيغة الاستجابة مختلفة.")
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc: await send_error(update.message, exc)


async def shake(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(user, "GET", f"/services/shake/{user['phone']}"))
        code = result.get("code")
        if not code:
            await update.message.reply_text("لم يرجع الخادم عرض هز واربح متاحاً لحسابك حالياً.")
            return
        name = lang_name(result.get("commercialName"))
        desc = lang_name(result.get("mediumDescription"))
        price = result.get("price", "غير معروف")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("تفعيل العرض الظاهر", callback_data="confirm"),
            InlineKeyboardButton("إلغاء", callback_data="cancel"),
        ]])
        pending_actions[update.effective_user.id] = {
            "kind": "shake", "code": code, "name": name,
            "price": price, "description": desc,
        }
        await update.message.reply_text(
            f"🎲 العرض الذي أعاده خادم جازي:\n{name}\n"
            f"الحجم/الوصف: {desc}\nالسعر: {price} دج\n"
            f"كود العرض: {code}\n\nلن يتم التفعيل إلا بعد تأكيدك.",
            reply_markup=kb,
        )
    except Exception as exc: await send_error(update.message, exc)


async def walk(update, context):
    user = await need_user(update)
    if not user: return
    try:
        balances = data_of(api_request(user, "GET", f"/subscribers/connected-products-balances/{user['phone']}"))
        active_walk = []
        for p in balances.get("products", []):
            if str(p.get("code", "")).startswith("WAW_"):
                active_walk.append(p)
        if active_walk:
            p = active_walk[0]
            expiry = p.get("expiryAt")
            for b in p.get("balances", []):
                expiry = b.get("expiryAt", expiry)
            await update.message.reply_text(
                f"🚶 لديك مكافأة مشي مسجلة ضمن الباقات.\n"
                f"الكود: {p.get('code', '?')}\n"
                f"انتهاء الصلاحية: {expiry or 'غير متوفر'}"
            )
            return
        result = data_of(api_request(user, "GET", f"/services/walk/campaign/{user['phone']}"))
        levels = result.get("wawLevels", [])
        lines = ["🚶 حالة امشي واربح:"]
        reward_code = None
        for level in levels:
            reward = level.get("reward")
            if isinstance(reward, dict):
                reward_code = reward.get("packageCode") or reward.get("code")
            elif isinstance(reward, str):
                reward_code = reward
            lines.append(f"• المستوى: {level.get('steps', '?')} خطوة — المكافأة: {reward_code or reward or 'غير جاهزة'}")
        if reward_code:
            pending_actions[update.effective_user.id] = {"kind": "walk", "code": reward_code}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تفعيل المكافأة المتاحة", callback_data="confirm"),
                InlineKeyboardButton("إلغاء", callback_data="cancel"),
            ]])
            await update.message.reply_text("\n".join(lines) + "\nالخادم أعاد مكافأة قابلة للطلب:", reply_markup=kb)
        else:
            await update.message.reply_text(
                "\n".join(lines) + "\n\nلم يرجع الخادم packageCode جاهزاً. "
                "لا يمكن للبوت إنشاء مكافأة أو تخطي شروط الخطوات."
            )
    except Exception as exc: await send_error(update.message, exc)


async def invite(update, context):
    if not await need_user(update): return
    context.user_data["state"] = "invite_phone"
    await update.message.reply_text("أرسل رقم الشخص الذي وافق على استقبال دعوة جازي.")


async def invitations(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(user, "GET", f"/services/mgm/invitations/{user['phone']}"))
        items = result.get("invitations", [])
        lines = ["📨 سجل الدعوات:"]
        for item in items[:20]:
            lines.append(
                f"• {item.get('msisdnReceiver', '?')} — {item.get('status', '?')} "
                f"(تنتهي: {item.get('expireAt', '?')})"
            )
        if len(lines) == 1: lines.append("لا توجد دعوات في السجل.")
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc: await send_error(update.message, exc)


async def catalog(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = data_of(api_request(
            user, "GET",
            f"/subscribers/customer-offers/{user['phone']}",
            params={"tags": "STANDARD,SURPRISE,DIGITAL,FLEXY"},
        ))
        lines = ["🎁 كتالوج العروض المتاحة (عرض فقط):"]
        for category in result if isinstance(result, list) else []:
            lines.append(f"\n【{lang_name(category.get('title'))}】")
            for sub in category.get("subcategories", []):
                lines.append(f"  {lang_name(sub.get('title'))}")
                for p in sub.get("packages", [])[:8]:
                    lines.append(
                        f"   • {lang_name(p.get('commercialName'))} — "
                        f"{p.get('price', '?')} دج — code: {p.get('packageCode', '?')}"
                    )
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc: await send_error(update.message, exc)


async def services(update, context):
    user = await need_user(update)
    if not user: return
    try:
        result = api_request(
            user, "GET", f"/subscribers/{user['phone']}",
            content=True, params={"include": "content-available-services"},
        )
        included = result.get("included", []) if isinstance(result, dict) else []
        lines = ["🧩 الخدمات الرقمية المتاحة:"]
        for item in included:
            if item.get("type") == "content-services":
                attrs = item.get("attributes", {})
                lines.append(f"• {attrs.get('category', '?')} — {lang_name(attrs.get('commercial-name', {}).get('text', {}))}")
        if len(lines) == 1: lines.append("لم تظهر خدمات في الاستجابة.")
        await update.message.reply_text("\n".join(lines))
    except Exception as exc: await send_error(update.message, exc)


async def callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if q.data == "cancel":
        pending_actions.pop(uid, None)
        await q.edit_message_text("تم الإلغاء. لم يتم إرسال أي طلب تفعيل.")
        return
    action = pending_actions.get(uid)
    user = sessions.get(uid)
    if not action or not user:
        await q.edit_message_text("انتهى الطلب أو الجلسة. أعد المحاولة.")
        return
    kind = action["kind"]
    try:
        if kind == "invite":
            today = date.today()
            if daily_usage["date"] != today:
                daily_usage.update({"date": today, "count": 0})
            if daily_usage["count"] >= MAX_INVITES_PER_DAY:
                await q.edit_message_text(f"وصلت إلى الحد الوقائي اليومي ({MAX_INVITES_PER_DAY}).")
                pending_actions.pop(uid, None)
                return
            api_request(
                user, "POST", f"/services/mgm/send-invitation/{user['phone']}",
                json={"msisdnReceiver": action["receiver"]},
            )
            daily_usage["count"] += 1
            await q.edit_message_text("✅ أرسل خادم جازي طلب الدعوة. راجع /invitations لاحقاً.")
        elif kind == "shake":
            api_request(
                user, "POST", f"/services/shake/{user['phone']}",
                json={"packageCode": action["code"]},
            )
            await q.edit_message_text("تم إرسال طلب تفعيل عرض الهز إلى جازي. افحص /packages للتأكد من النتيجة.")
        elif kind == "walk":
            api_request(
                user, "POST", f"/services/walk/activate-reward/{user['phone']}",
                json={"packageCode": action["code"]},
            )
            await q.edit_message_text("تم إرسال طلب تفعيل مكافأة المشي. افحص /packages للتأكد.")
        pending_actions.pop(uid, None)
    except Exception as exc:
        pending_actions.pop(uid, None)
        if isinstance(exc, requests.HTTPError):
            code = exc.response.status_code if exc.response is not None else "?"
            await q.edit_message_text(f"لم يكتمل الطلب. رد جازي HTTP {code}.")
        else:
            await q.edit_message_text("تعذر تنفيذ الطلب حالياً. حاول لاحقاً.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("يجب ضبط BOT_TOKEN أولاً.")
    if not all([APP_CLIENT_ID, APP_CLIENT_SECRET]):
        log.warning("APP_CLIENT_ID وAPP_CLIENT_SECRET غير مضبوطين؛ تسجيل الدخول لن يعمل حتى تضبطهما.")
    app = Application.builder().token(BOT_TOKEN).build()
    commands = [
        ("start", start), ("login", login), ("logout", logout),
        ("balance", balance), ("packages", packages), ("history", history),
        ("shake", shake), ("walk", walk), ("invite", invite),
        ("invitations", invitations), ("catalog", catalog), ("services", services),
    ]
    for name, handler in commands:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^(confirm|cancel)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Djezzy Telegram bot is running locally...")
    app.run_polling()


if __name__ == "__main__":
    main()
