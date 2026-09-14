#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram App-Builder Bot (نسخة Pydroid3 + اشتراك إجباري + أكواد)
-- نسخة v2: بنبني التطبيق على مرحلتين:
   1) نطلب من AI Selva خطة مختصرة + قائمة الملفات المطلوبة (رد صغير جدًا).
   2) نطلب محتوى كل ملف على حدة (base64) في نداء مستقل.
   كل رد بقى صغير ومركّز، وده بيمنع مشكلة "الرد اتقطع" اللي كانت بتحصل
   لما نطلب كل التطبيق في رد واحد ضخم.
"""

import json, os, re, shutil, zipfile, asyncio, tempfile, subprocess, base64
import urllib.request, urllib.error
import random, string, time
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import Forbidden, BadRequest
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                          ContextTypes, ConversationHandler)

# ===== قيمك =====
# تحذير أمني: متسيبش المفاتيح دي مكتوبة صريحة في الكود لو هتشارك السكريبت مع
# حد أو ترفعه على GitHub. الأفضل تستخدم متغيرات بيئة (os.environ).
AI_SELVA_API_KEY = "AQ.Ab8RN6K7jj3TUqsU4Wwny-HPfZoL8t3BjN0j8rMeym9fsgQG0A"
TELEGRAM_TOKEN = "8830996414:AAFA1bD-QNTWAQPkxxBvMEMxmP8CzTLTiIE"
# =================

ADMIN_ID = 8273076051
CHANNELS = ["@selva_card", "@ie_zy"]

DURATIONS = {
    "⏱ نص ساعة (30 دقيقة)": 1800,
    "🕐 ساعة": 3600,
    "📅 يوم": 86400,
    "🗓 أسبوع": 604800,
    "📆 شهر": 2592000,
}

DB_USERS = "users_db.json"
DB_CODES = "codes_db.json"

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"

MAX_FILES = 18  # سقف لعدد الملفات عشان ميطلعش تطبيق ضخم يتاخر أو يكلف كتير

PLAN_PROMPT = """You are a senior full-stack developer planning an application based on this idea:

{idea}

Return ONLY a raw JSON object (no markdown fences, no commentary), in this exact shape:
{{"app_name": "short-kebab-case-name", "description": "a concise technical plan (5-10 sentences): architecture, key modules, how files relate, tech stack", "files": ["relative/path1", "relative/path2", ...]}}

Rules:
- List every file needed: source code, README.md, and requirements.txt or package.json if needed.
- Keep the file list practical: {max_files} files maximum, prefer fewer well-organized files over many tiny ones.
- Do NOT include file contents here, only the plan and the file list."""

FILE_PROMPT = """You are a senior full-stack developer building this application:

IDEA: {idea}

ARCHITECTURE PLAN: {description}

FULL FILE LIST (for context/consistency): {file_list}

Now write the COMPLETE, production-quality content for ONLY this one file:
{path}

Return ONLY a raw JSON object (no markdown fences, no commentary), in this exact shape:
{{"content_base64": "BASE64_ENCODED_FULL_FILE_CONTENT"}}

The file content must be base64-encoded (standard base64, no line breaks in the encoded string),
complete, and consistent with the other files in the list and the architecture plan.{extra_instruction}"""

# نبني عدة ملفات في نداء واحد عشان نقلل عدد الطلبات لـ AI Selva (مهم جدًا مع
# حدود الكوتا المحدودة في الباقة المجانية).
BATCH_SIZE = 3

BATCH_FILE_PROMPT = """You are a senior full-stack developer building this application:

IDEA: {idea}

ARCHITECTURE PLAN: {description}

FULL FILE LIST (for context/consistency): {file_list}

Now write the COMPLETE, production-quality content for ONLY these files:
{paths_list}

Return ONLY a raw JSON object (no markdown fences, no commentary), in this exact shape:
{{"files": {{"exact/path/one": "BASE64_ENCODED_CONTENT", "exact/path/two": "BASE64_ENCODED_CONTENT"}}}}

Rules:
- Include EVERY file listed above, using the exact path as the key.
- Each value must be base64-encoded (standard base64, no line breaks in the encoded string).
- Content must be complete and consistent with the other files in the list and the architecture plan.{extra_instruction}"""


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def safe_edit(msg, text):
    """يحاول يعدّل الرسالة، وبيبتلع أخطاء Forbidden (المستخدم عمل بلوك للبوت)
    و BadRequest (زي الرسالة طويلة جدًا أو مفيش تغيير) بدل ما يوقّع البوت."""
    try:
        await msg.edit_text(text[:4090])
    except (Forbidden, BadRequest):
        pass
    except Exception:
        pass


async def error_handler(update, ctx: ContextTypes.DEFAULT_TYPE):
    """معالج أخطاء عام: يمنع الـ traceback الضخم من الظهور في اللوج لكل
    استثناء غير متوقع، ويكتب سطر مختصر بدله."""
    print(f"⚠️ Unhandled error: {ctx.error!r}")


users_db = load_json(DB_USERS, {})
codes_db = load_json(DB_CODES, {})


def gen_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def fmt_remaining(seconds):
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, _ = divmod(seconds, 60)
    parts = []
    if d: parts.append(f"{d} يوم")
    if h: parts.append(f"{h} ساعة")
    if m: parts.append(f"{m} دقيقة")
    return " و".join(parts) if parts else "أقل من دقيقة"


def subscription_status(user_id):
    exp = users_db.get(str(user_id))
    if exp is None:
        return True, 0
    remaining = exp - time.time()
    return remaining <= 0, max(0, remaining)


async def is_member(bot, user_id):
    for ch in CHANNELS:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True


def redeem_code(user_id, text):
    code = text.strip().upper()
    c = codes_db.get(code)
    if not c:
        return False, "❌ الكود ده غير صحيح. جرّب تاني أو تواصل مع الأدمن."
    uid = str(user_id)
    if uid in c["used_by"]:
        return False, "❌ استخدمت الكود ده قبل كده. كل كود ليك مرة واحدة."
    if len(c["used_by"]) >= c["max"]:
        return False, "❌ الكود خلص عدد استخداماته. هات كود جديد من الأدمن."
    c["used_by"].append(uid)
    now = time.time()
    base = max(now, users_db.get(uid, 0))
    users_db[uid] = base + c["duration"]
    save_json(DB_CODES, codes_db)
    save_json(DB_USERS, users_db)
    return True, fmt_remaining(users_db[uid] - now)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    expired, remaining = subscription_status(uid)
    chans = "\n".join(f"• {c}" for c in CHANNELS)
    if uid == ADMIN_ID:
        await update.message.reply_text(
            "👋 أهلاً بالأدمن!\nأرسل فكرة تطبيق عشان أبنيه، أو اكتب /code لعمل كود اشتراك.")
        return
    if expired:
        await update.message.reply_text(
            "👋 أهلاً بيك في بوت بناء التطبيقات!\n\n"
            f"1️⃣ اشترك في القنوات دي:\n{chans}\n\n"
            "2️⃣ بعدها ابعت كود الاشتراك اللي خدته من الأدمن.\n\n"
            "⏳ لو الكود خلصت مدهته هيطلب منك كود جديد تلقائياً.")
    else:
        await update.message.reply_text(
            f"✅ اشتراكك شغال! المتبقي: {fmt_remaining(remaining)}\nأرسل فكرة تطبيق وأنا أبنيها لك 📦")


DUR, USES = 1, 2


async def code_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    kb = [[d] for d in DURATIONS.keys()] + [["❌ إلغاء"]]
    await update.message.reply_text(
        "🔑 عمل كود اشتراك جديد\n\nاختر مدة الكود:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    return DUR


async def code_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in DURATIONS:
        await update.message.reply_text("⚠️ اختر مدة من القائمة اللي ظاهرة.")
        return DUR
    ctx.user_data["duration"] = DURATIONS[text]
    await update.message.reply_text(
        "👥 الكود ده متاح لكم مستخدم؟ اكتب رقم (مثال: 10)",
        reply_markup=ReplyKeyboardRemove())
    return USES


async def code_uses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if n <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ اكتب رقم صحيح أكبر من صفر.")
        return USES
    code = gen_code()
    codes_db[code] = {
        "duration": ctx.user_data["duration"],
        "max": n,
        "used_by": [],
        "created": time.time(),
    }
    save_json(DB_CODES, codes_db)
    await update.message.reply_text(
        f"✅ تم إنشاء الكود!\n\n🔑 الكود: <code>{code}</code>\n"
        f"⏱ المدة: {fmt_remaining(ctx.user_data['duration'])}\n"
        f"👥 عدد المستخدمين: {n}\n\nابعته للمستخدمين.",
        reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    return ConversationHandler.END


async def code_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء ❌", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def my_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not codes_db:
        await update.message.reply_text("مفيش أكواد لسه. اكتب /code")
        return
    lines = []
    for c, v in codes_db.items():
        lines.append(
            f"🔑 <code>{c}</code> | {fmt_remaining(v['duration'])} | "
            f"{len(v['used_by'])}/{v['max']} مستخدم")
    await update.message.reply_text("الأكواد:\n" + "\n".join(lines), parse_mode="HTML")


# ---------------- AI Selva calls ----------------

def _call_ai_selva(prompt_text: str, max_output_tokens: int) -> dict:
    """يبعت طلب لـ AI Selva ويرجّع (candidate content text)، مع إعادة محاولة
    وفحص finishReason عشان نمسك التقطيع بدري."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "maxOutputTokens": max_output_tokens,
        },
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-goog-api-key": AI_SELVA_API_KEY},
        method="POST")

    last_err = ""
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode())
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason", "")
            if finish_reason == "MAX_TOKENS":
                raise RuntimeError("TRUNCATED")
            return candidate["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()[:800]
            except Exception:
                detail = ""
            last_err = f"HTTP {e.code}: {detail}"
            if e.code == 429:
                # جوجل بترجع "Please retry in X.Xs." — نستنى بالمدة الفعلية دي
                m = re.search(r"retry in ([\d.]+)s", detail)
                wait = float(m.group(1)) + 2 if m else 20
                wait = min(wait, 60)
                if attempt < 4:
                    time.sleep(wait)
                    continue
                last_err = ("تجاوزت الحد المسموح (Quota) لمفتاح AI Selva الحالي. "
                            "ده حد من جوجل نفسها (الباقة المجانية)، لازم تنتظر أو تفعّل "
                            "billing على المفتاح من https://ai.dev/rate-limit")
                break
            if e.code in (500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            break
        except RuntimeError as e:
            if str(e) == "TRUNCATED":
                raise
            last_err = str(e)
            time.sleep(5)
        except Exception as e:
            last_err = str(e)
            time.sleep(5)

    raise RuntimeError(last_err)


def _parse_json_relaxed(text: str) -> dict:
    """يحاول يفكّ رد AI Selva كـ JSON، مع تنظيف أي fences أو نص زيادة حواليه."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group(0), strict=False)
        raise


def get_app_plan(idea: str) -> dict:
    """المرحلة 1: خطة مختصرة + قائمة الملفات فقط (رد صغير، بعيد عن التقطيع)."""
    prompt = PLAN_PROMPT.format(idea=idea, max_files=MAX_FILES)
    raw = _call_ai_selva(prompt, max_output_tokens=4096)
    plan = _parse_json_relaxed(raw)
    files = plan.get("files", [])
    if not files:
        raise ValueError("AI Selva مرجّعش قائمة ملفات. جرّب تاني.")
    if len(files) > MAX_FILES:
        files = files[:MAX_FILES]
    plan["files"] = files
    return plan


def get_file_content(idea: str, description: str, file_list, path: str,
                      extra_instruction: str = "") -> str:
    """المرحلة 2 (نسخة ملف واحد): يبني محتوى ملف واحد فقط، مُرجّع كـ base64."""
    prompt = FILE_PROMPT.format(
        idea=idea, description=description,
        file_list=", ".join(file_list), path=path,
        extra_instruction=extra_instruction,
    )
    raw = _call_ai_selva(prompt, max_output_tokens=24576)
    data = _parse_json_relaxed(raw)
    b64 = data.get("content_base64", "")
    try:
        return base64.b64decode(b64, validate=False).decode("utf-8")
    except Exception:
        return b64


def get_files_batch(idea: str, description: str, file_list, batch_paths,
                     extra_instruction: str = "") -> dict:
    """المرحلة 2 (نسخة batch): يبني عدة ملفات في نداء واحد لتقليل عدد الطلبات
    لـ AI Selva (مفيد جدًا مع حدود الكوتا). يرجّع dict {path: content}."""
    prompt = BATCH_FILE_PROMPT.format(
        idea=idea, description=description,
        file_list=", ".join(file_list),
        paths_list="\n".join(f"- {p}" for p in batch_paths),
        extra_instruction=extra_instruction,
    )
    raw = _call_ai_selva(prompt, max_output_tokens=24576)
    data = _parse_json_relaxed(raw)
    files_b64 = data.get("files", {})
    result = {}
    for p in batch_paths:
        b64 = files_b64.get(p)
        if not b64:
            continue
        try:
            result[p] = base64.b64decode(b64, validate=False).decode("utf-8")
        except Exception:
            result[p] = b64
    return result


# ---------------- Bot flow ----------------

async def build_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return  # تحديثات بدون مستخدم/رسالة (مثل بوستات القنوات) - نتجاهلها

    uid = update.effective_user.id
    text = update.message.text

    if uid != ADMIN_ID:
        if not await is_member(ctx.bot, uid):
            chans = "\n".join(f"• {c}" for c in CHANNELS)
            try:
                await update.message.reply_text(
                    f"⚠️ لازم تشترك في القنوات دي الأول:\n{chans}\n\n"
                    "بعد الاشتراك ابعت رسالتك تاني.")
            except (Forbidden, BadRequest):
                pass
            return

        expired, remaining = subscription_status(uid)
        if expired:
            ok, res = redeem_code(uid, text)
            try:
                if ok:
                    await update.message.reply_text(
                        f"🎉 تم تفعيل اشتراكك!\n⏱ المدة المتاحة: {res}\n\nدلوقتي ابعت فكرة التطبيق.")
                else:
                    await update.message.reply_text(
                        f"⚠️ {res}\n\nاشترك في القنوات وبعدها ابعت كود الاشتراك هنا.")
            except (Forbidden, BadRequest):
                pass
            return

    try:
        msg = await update.message.reply_text("🤖 جاري التخطيط للتطبيق...")
    except (Forbidden, BadRequest):
        return  # المستخدم عمل بلوك للبوت - مفيش داعي نكمل

    # المرحلة 1: خطة + قائمة ملفات
    try:
        plan = await asyncio.to_thread(get_app_plan, text)
    except Exception as e:
        err = str(e)
        if err == "TRUNCATED":
            err = "الخطة نفسها اتقطعت! جرّب فكرة أقصر شوية."
        await safe_edit(msg, f"❌ خطأ في التخطيط:\n{err[:800]}")
        return

    file_list = plan["files"]
    description = plan.get("description", "")
    appname = re.sub(r"[^\w\-]", "", plan.get("app_name") or
                      (text.split()[0] if text.split() else "app"))[:30] or "app"

    workdir = tempfile.mkdtemp(prefix=f"user{uid}_")
    appdir = os.path.join(workdir, appname)
    os.makedirs(appdir, exist_ok=True)

    # نقسم قائمة الملفات لمجموعات (batches) عشان نقلل عدد الطلبات لـ AI Selva
    batches = [file_list[i:i + BATCH_SIZE] for i in range(0, len(file_list), BATCH_SIZE)]
    built_files = {}
    failed_files = []

    for bi, batch_paths in enumerate(batches, start=1):
        names_preview = "\n".join(f"📄 {p}" for p in batch_paths)
        await safe_edit(msg, f"🤖 بناء المجموعة {bi}/{len(batches)}:\n{names_preview}")

        batch_result = {}
        last_reason = ""
        attempts = 3
        for attempt in range(attempts):
            extra = ""
            if attempt > 0:
                extra = ("\nIMPORTANT: the previous attempt was too long and got cut off "
                         "or invalid. Keep the code more concise: minimize comments and "
                         "boilerplate, while keeping every file fully functional.")
            try:
                batch_result = await asyncio.to_thread(
                    get_files_batch, text, description, file_list, batch_paths, extra)
                if batch_result:
                    break
            except Exception as e:
                last_reason = str(e)
                if last_reason == "TRUNCATED":
                    last_reason = "الرد اتقطع (المجموعة كبيرة)"
                await asyncio.sleep(2)

        for path in batch_paths:
            content = batch_result.get(path)
            if content is None:
                failed_files.append((path, last_reason or "لم يترجع في الرد"))
                continue
            norm_path = os.path.normpath(path)
            if norm_path.startswith("..") or os.path.isabs(norm_path):
                continue
            full = os.path.join(appdir, norm_path)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            built_files[path] = content

    if not built_files:
        await safe_edit(msg, "❌ فشل بناء كل الملفات. جرّب فكرة أبسط.")
        shutil.rmtree(workdir, ignore_errors=True)
        return

    for cmd in (["git", "init"], ["git", "add", "."],
                ["git", "-c", "user.email=bot@builder.local", "-c", "user.name=AppBuilderBot",
                 "commit", "-m", "Initial commit generated by AI Selva"]):
        try:
            await asyncio.to_thread(subprocess.run, cmd, cwd=appdir,
                                    capture_output=True, timeout=30)
        except Exception:
            pass

    zip_path = os.path.join(workdir, f"{appname}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, names in os.walk(appdir):
            for n in names:
                full = os.path.join(root, n)
                z.write(full, os.path.relpath(full, workdir))

    file_list_str = "\n".join(f"📄 {p}" for p in sorted(built_files))
    fail_str = ("\n\n⚠️ فشل بناء:\n" + "\n".join(f"❌ {p} — {r}" for p, r in failed_files)) if failed_files else ""
    _, remaining = subscription_status(uid)
    footer = f"\n⏱ المتبقي في اشتراكك: {fmt_remaining(remaining)}" if uid != ADMIN_ID else ""

    header = f"✅ تم! {len(built_files)} ملف:\n"
    # حد تليجرام لطول الرسالة 4096 حرف - بنسيب هامش أمان ونقص كل جزء بنسبة
    budget = 3500
    body = f"{header}{file_list_str}{fail_str}{footer}"
    if len(body) > budget:
        # نقص قائمة الملفات الأول، والفشل تاني، مع الحفاظ على الفوتر
        remaining_budget = budget - len(header) - len(footer) - 50
        fl = file_list_str[:max(remaining_budget, 200)]
        fs = fail_str[:max(remaining_budget - len(fl), 0)]
        body = f"{header}{fl}\n…(القائمة اتقصت){fs}{footer}"

    await safe_edit(msg, body)
    try:
        await update.message.reply_document(
            document=open(zip_path, "rb"),
            filename=f"{appname}.zip",
            caption=(f"🚀 مشروع «{appname}» جاهز.{footer}\n"
                     "بعد فك الضغط داخل مجلد المشروع:\n"
                     "git remote add origin <رابط_repo>\n"
                     "git push -u origin main")[:1024])
    except (Forbidden, BadRequest):
        pass
    shutil.rmtree(workdir, ignore_errors=True)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("codes", my_codes))

    conv = ConversationHandler(
        entry_points=[CommandHandler("code", code_start)],
        states={
            DUR: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_duration)],
            USES: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_uses)],
        },
        fallbacks=[MessageHandler(filters.TEXT & ~filters.COMMAND, code_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, build_app))
    app.add_error_handler(error_handler)
    print("🤖 Bot is running... أرسل /start لبوتك في التليجرام")
    app.run_polling()


if __name__ == "__main__":
    main()
