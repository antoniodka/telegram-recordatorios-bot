import os
import re
import sqlite3
import calendar
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import dateparser
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

# ================== CONFIG ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TZ = os.getenv("TZ", "America/Bogota").strip()
DEFAULT_HOUR = os.getenv("DEFAULT_HOUR", "09:00").strip()
RETRY_EVERY_MINUTES = int(os.getenv("RETRY_EVERY_MINUTES", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "24"))

DB_PATH = "/data/reminders.db"
LOCAL_TZ = ZoneInfo(TZ)


# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      task TEXT NOT NULL,
      due_utc TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',       -- pending|done|deleted
      amount_cop INTEGER,                          -- opcional
      category TEXT DEFAULT 'general',              -- general|pago
      created_utc TEXT,
      completed_utc TEXT,
      retry_count INTEGER NOT NULL DEFAULT 0,
      last_sent_utc TEXT
    )
    """)
    return conn


def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("PRAGMA table_info(reminders)")
    cols = {row[1] for row in cur.fetchall()}

    def add(col_sql: str):
        conn.execute(f"ALTER TABLE reminders ADD COLUMN {col_sql}")

    if "amount_cop" not in cols:
        add("amount_cop INTEGER")
    if "category" not in cols:
        add("category TEXT DEFAULT 'general'")
    if "created_utc" not in cols:
        add("created_utc TEXT")
    if "completed_utc" not in cols:
        add("completed_utc TEXT")
    if "retry_count" not in cols:
        add("retry_count INTEGER NOT NULL DEFAULT 0")
    if "last_sent_utc" not in cols:
        add("last_sent_utc TEXT")

    conn.commit()
    conn.close()


def now_local():
    return datetime.now(LOCAL_TZ)


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_utc_iso(iso_utc: str) -> datetime:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def pretty_dt_local(dt_local: datetime) -> str:
    return dt_local.strftime("%Y-%m-%d %H:%M")


def pretty_money(n: int) -> str:
    s = f"{n:,}".replace(",", ".")
    return f"${s} COP"


def insert_reminder(chat_id: int, task: str, due_local: datetime, amount_cop: int | None, category: str):
    conn = db()
    created = utc_iso(utc_now())
    due_utc = utc_iso(due_local)
    cur = conn.execute(
        "INSERT INTO reminders(chat_id, task, due_utc, status, amount_cop, category, created_utc) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
        (chat_id, task, due_utc, amount_cop, category, created)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid, due_utc


def update_reminder_time(chat_id: int, rid: int, new_due_local: datetime):
    conn = db()
    due_utc = utc_iso(new_due_local)
    cur = conn.execute(
        "SELECT id FROM reminders WHERE chat_id=? AND id=? AND status!='deleted' LIMIT 1",
        (chat_id, rid)
    )
    if not cur.fetchone():
        conn.close()
        return None

    conn.execute(
        "UPDATE reminders SET due_utc=?, status='pending', retry_count=0, last_sent_utc=NULL WHERE chat_id=? AND id=?",
        (due_utc, chat_id, rid)
    )
    conn.commit()
    conn.close()
    return due_utc


def fetch_reminders(chat_id: int, status: str | None = None, limit: int = 200):
    conn = db()
    if status:
        cur = conn.execute(
            "SELECT id, task, due_utc, status, amount_cop, category FROM reminders "
            "WHERE chat_id=? AND status=? ORDER BY due_utc ASC LIMIT ?",
            (chat_id, status, limit)
        )
    else:
        cur = conn.execute(
            "SELECT id, task, due_utc, status, amount_cop, category FROM reminders "
            "WHERE chat_id=? AND status!='deleted' ORDER BY due_utc ASC LIMIT ?",
            (chat_id, limit)
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_range(chat_id: int, start_local: datetime, end_local: datetime, category: str | None = None):
    start_utc = utc_iso(start_local)
    end_utc = utc_iso(end_local)
    conn = db()
    if category:
        cur = conn.execute(
            "SELECT id, task, due_utc, status, amount_cop, category FROM reminders "
            "WHERE chat_id=? AND status!='deleted' AND category=? AND due_utc>=? AND due_utc<=? "
            "ORDER BY due_utc ASC",
            (chat_id, category, start_utc, end_utc)
        )
    else:
        cur = conn.execute(
            "SELECT id, task, due_utc, status, amount_cop, category FROM reminders "
            "WHERE chat_id=? AND status!='deleted' AND due_utc>=? AND due_utc<=? "
            "ORDER BY due_utc ASC",
            (chat_id, start_utc, end_utc)
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_done(chat_id: int, rid: int | None = None):
    conn = db()
    if rid is None:
        cur = conn.execute(
            "SELECT id, task FROM reminders WHERE chat_id=? AND status='pending' ORDER BY due_utc ASC LIMIT 1",
            (chat_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        rid, task = row
    else:
        cur = conn.execute(
            "SELECT id, task FROM reminders WHERE chat_id=? AND id=? AND status='pending' LIMIT 1",
            (chat_id, rid)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        rid, task = row

    conn.execute(
        "UPDATE reminders SET status='done', completed_utc=? WHERE chat_id=? AND id=?",
        (utc_iso(utc_now()), chat_id, rid)
    )
    conn.commit()
    conn.close()
    return rid, task


def delete_reminder(chat_id: int, rid: int):
    conn = db()
    cur = conn.execute(
        "SELECT id, task FROM reminders WHERE chat_id=? AND id=? AND status!='deleted' LIMIT 1",
        (chat_id, rid)
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE reminders SET status='deleted' WHERE chat_id=? AND id=?", (chat_id, rid))
    conn.commit()
    conn.close()
    return row[0], row[1]


# ================== Interpreter ==================
CONFIRM_WORDS = [
    "ya", "listo", "hecho", "ok", "okey", "confirmo", "pagado",
    "realizado", "ya lo hice", "ya lo pagué", "ya lo pague"
]

MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12
}

@dataclass
class ParsedCreate:
    task: str
    due_local: datetime | None
    amount_cop: int | None
    category: str  # general|pago


def looks_like_confirm(text: str) -> bool:
    t = text.strip().lower()
    return any(t == w or w in t for w in CONFIRM_WORDS)


def text_has_time_hints(text: str) -> bool:
    tl = text.lower()
    if ":" in tl:
        return True
    if re.search(r"\b\d{1,2}\s*(am|pm)\b", tl):
        return True
    if "a las" in tl:
        return True
    return False


def strip_money_candidates(text: str) -> str:
    """
    Quita números grandes (>=4 dígitos) para ayudar a parsear fechas/horas.
    NO toca horas porque incluyen ":".
    """
    def repl(m):
        token = m.group(0)
        if ":" in token:
            return token
        digits = re.sub(r"[^\d]", "", token)
        if len(digits) >= 4:
            return " "
        return token

    return re.sub(r"\$?\s*\d{1,3}(?:[.,]\d{3})+|\$?\s*\d{4,}", repl, text)


def parse_amount_cop_strict(text: str) -> int | None:
    """
    Dinero SOLO si:
    - contexto de dinero (pagar/pesos/cop/$/debo)
    - número con >=4 dígitos (quitando puntos/commas)
    - NO es hora (no tiene ':')
    """
    tl = text.lower()
    money_context = any(w in tl for w in ["pagar", "pago", "pesos", "cop", "$", "debo"])
    if not money_context:
        return None

    candidates = re.findall(r"\$?\s*\d{1,3}(?:[.,]\d{3})+|\$?\s*\d+", text)
    best = None

    for c in candidates:
        if ":" in c:
            continue
        digits = re.sub(r"[^\d]", "", c)
        if len(digits) < 4:
            continue
        try:
            val = int(digits)
            if val <= 0:
                continue
            if best is None or val > best:
                best = val
        except ValueError:
            continue

    return best


def parse_date_time_from_text(text: str) -> datetime | None:
    cleaned = strip_money_candidates(text)
    tl = cleaned.lower()

    has_time = text_has_time_hints(cleaned) or bool(re.search(r"\b(\d{1,2})(:\d{2})?\b", tl))
    mentions_day = any(k in tl for k in [
        "hoy", "mañana", "manana",
        "lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo",
        "próximo", "proximo",
        "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "setiembre",
        "octubre", "noviembre", "diciembre",
        "en ", "dentro", "esta tarde", "esta noche"
    ])

    augmented = cleaned
    if mentions_day and not has_time:
        augmented = f"{cleaned} {DEFAULT_HOUR}"

    dt = dateparser.parse(
        augmented,
        languages=["es"],
        settings={
            "TIMEZONE": TZ,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now_local()
        }
    )
    return dt


def parse_create_intent(text: str) -> ParsedCreate | None:
    t = text.strip()
    tl = t.lower()

    is_reminder = any(k in tl for k in ["recuérdame", "recuerdame", "recordame"])
    is_pay_word = any(k in tl for k in ["pagar", "pago", "debo pagar", "tengo que pagar", "debo"])

    if not (is_reminder or is_pay_word):
        return None

    quoted = re.search(r'"([^"]+)"', t)
    task = quoted.group(1).strip() if quoted else ""

    amount = parse_amount_cop_strict(t)
    category = "pago" if (amount is not None or is_pay_word) else "general"

    # Relativo: "en X minutos/horas"
    rel = re.search(r"\ben\s+(\d+)\s*(min|minuto|minutos|m|hora|horas|h)\b", tl)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2)
        mins = n if unit in ["min", "minuto", "minutos", "m"] else n * 60
        due_local = now_local() + timedelta(minutes=mins)

        if not task:
            tmp = re.sub(r'^(recuérdame|recuerdame|recordame)\s+', '', t, flags=re.IGNORECASE).strip()
            tmp = re.sub(r'\ben\s+\d+\s*(min|minuto|minutos|m|hora|horas|h)\b', '', tmp, flags=re.IGNORECASE).strip()
            task = tmp.strip(" ,.-") or "Recordatorio"

        return ParsedCreate(task=task, due_local=due_local, amount_cop=amount, category=category)

    # Absoluto/natural
    cleaned = re.sub(r'^(recuérdame|recuerdame|recordame)\s+', '', t, flags=re.IGNORECASE).strip()
    due_local = parse_date_time_from_text(cleaned)

    if not task:
        tmp = cleaned
        tmp = re.sub(r"\b(hoy|mañana|manana|el|este|esta|próximo|proximo|cada)\b", " ", tmp, flags=re.IGNORECASE)
        tmp = re.sub(r"\b(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\b", " ", tmp, flags=re.IGNORECASE)
        tmp = re.sub(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\b", " ", tmp, flags=re.IGNORECASE)
        tmp = re.sub(r"\b\d{1,2}(:\d{2})\s*(am|pm)?\b", " ", tmp, flags=re.IGNORECASE)
        tmp = re.sub(r"\b\d{1,2}\s*(am|pm)\b", " ", tmp, flags=re.IGNORECASE)
        tmp = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", tmp)
        tmp = re.sub(r"\b\d{1,2}\b", " ", tmp)
        tmp = strip_money_candidates(tmp)
        tmp = re.sub(r"\s+", " ", tmp).strip(" ,.-")

        if category == "pago" and "pagar" not in tmp.lower():
            tmp = ("pagar " + tmp).strip()

        task = tmp if len(tmp) >= 3 else cleaned

    return ParsedCreate(task=task, due_local=due_local, amount_cop=amount, category=category)


# ================== Quincena ==================
def quincena_range(year: int, month: int, which: int) -> tuple[datetime, datetime]:
    if which == 1:
        start = datetime(year, month, 1, 0, 0, tzinfo=LOCAL_TZ)
        end = datetime(year, month, 15, 23, 59, tzinfo=LOCAL_TZ)
    else:
        last_day = calendar.monthrange(year, month)[1]
        start = datetime(year, month, 16, 0, 0, tzinfo=LOCAL_TZ)
        end = datetime(year, month, last_day, 23, 59, tzinfo=LOCAL_TZ)
    return start, end


def current_quincena_range() -> tuple[datetime, datetime, str]:
    today = now_local().date()
    which = 1 if today.day <= 15 else 2
    start, end = quincena_range(today.year, today.month, which)
    label = "esta primera quincena" if which == 1 else "esta segunda quincena"
    return start, end, label


def parse_quincena_query(text: str) -> tuple[datetime, datetime, str] | None:
    tl = text.lower()

    if "esta quincena" in tl:
        start, end, label = current_quincena_range()
        return start, end, label

    which = None
    if "primera quincena" in tl:
        which = 1
    elif "segunda quincena" in tl:
        which = 2

    month = None
    month_name = None
    for mname, mnum in MONTHS_ES.items():
        if re.search(rf"\b{mname}\b", tl):
            month = mnum
            month_name = mname
            break
    if month is None:
        return None

    ym = re.search(r"\b(20\d{2})\b", tl)
    y = int(ym.group(1)) if ym else now_local().year

    if which is None:
        if month == now_local().month and y == now_local().year:
            which = 1 if now_local().day <= 15 else 2
        else:
            which = 1

    start, end = quincena_range(y, month, which)
    label = f"{'primera' if which==1 else 'segunda'} quincena de {month_name} {y}"
    return start, end, label


# ================== Formatting ==================
def format_list(rows, title="🗓️ Lista"):
    if not rows:
        return "✅ No hay recordatorios para mostrar."
    lines = [title]
    for rid, task, due_utc, status, amount, category in rows:
        dt = from_utc_iso(due_utc)
        money = f" — {pretty_money(amount)}" if amount else ""
        st = "✅" if status == "done" else "⏳"
        cat = "💸" if category == "pago" else "🔔"
        lines.append(f"{st} {cat} #{rid} — {pretty_dt_local(dt)} — {task}{money}")
    return "\n".join(lines)


def format_pay_sum(rows, label: str):
    pays = [r for r in rows if (r[4] is not None) or (r[5] == "pago")]
    if not pays:
        return f"✅ No veo pagos en {label}."
    total = sum((r[4] or 0) for r in pays)
    msg = [f"💸 Pagos en {label}:", ""]
    for rid, task, due_utc, status, amount, category in pays:
        dt = from_utc_iso(due_utc)
        amt = pretty_money(amount or 0)
        st = "✅" if status == "done" else "⏳"
        msg.append(f"{st} #{rid} — {pretty_dt_local(dt)} — {amt} — {task}")
    msg.append("")
    msg.append(f"🔢 Total: {pretty_money(total)}")
    return "\n".join(msg)


# ================== Natural language features ==================
def parse_delete_request(text: str) -> list[int] | None:
    tl = text.strip().lower()
    if not re.match(r"^(borrar|borra|eliminar|elimina)\b", tl):
        return None
    nums = re.findall(r"#?(\d+)", tl)
    if not nums:
        return None
    ids = []
    for n in nums:
        try:
            ids.append(int(n))
        except ValueError:
            pass
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out if out else None


def is_pending_list_request(text: str) -> bool:
    tl = text.strip().lower()
    return tl in {"lista", "generar lista", "generar la lista", "genera lista", "genera la lista"}


def is_full_list_request(text: str) -> bool:
    tl = text.strip().lower()
    return tl in {"genera toda la lista", "generar toda la lista", "toda la lista", "lista completa"}


def is_quincena_list_request(text: str) -> bool:
    tl = text.strip().lower()
    return tl in {"genera toda la de la quincena", "generar toda la de la quincena", "lista de la quincena", "toda la quincena"}


def is_commands_request(text: str) -> bool:
    tl = text.strip().lower()
    return tl in {"generar lista de comandos", "genera comando", "comandos", "comandos disponibles", "ayuda"}


# ================== Interactive state ==================
def set_awaiting_datetime_for_new(context: ContextTypes.DEFAULT_TYPE, payload: dict):
    context.user_data["awaiting_datetime_for_new"] = payload


def pop_awaiting_datetime_for_new(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.pop("awaiting_datetime_for_new", None)


def is_awaiting_datetime_for_new(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "awaiting_datetime_for_new" in context.user_data


def set_awaiting_datetime_for_reschedule(context: ContextTypes.DEFAULT_TYPE, payload: dict):
    context.user_data["awaiting_datetime_for_reschedule"] = payload


def pop_awaiting_datetime_for_reschedule(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.pop("awaiting_datetime_for_reschedule", None)


def is_awaiting_datetime_for_reschedule(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return "awaiting_datetime_for_reschedule" in context.user_data


# ================== Keyboard ==================
def reminder_keyboard(rid: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("✅ Ya lo hice", callback_data=f"done:{rid}"),
            InlineKeyboardButton("🔁 Recuérdame otra vez", callback_data=f"resched:{rid}")
        ],
        [
            InlineKeyboardButton("🕐 En 1 hora", callback_data=f"snooze1h:{rid}")
        ]
    ]
    return InlineKeyboardMarkup(kb)


# ================== Commands (slash) ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot SIN IA listo.\n\n"
        "Ejemplos:\n"
        '• Recuérdame "botar basura" en 10 minutos\n'
        "• Recuérdame pagar 1.000 pesos a Daniela el 1 de febrero\n\n"
        "Frases útiles:\n"
        "• lista  (pendientes)\n"
        "• toda la lista  (todo)\n"
        "• lista de la quincena\n"
        "• borrar 4,5,6\n"
        "• comandos\n"
        "• sumame cuanto debo pagar esta quincena\n"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = fetch_reminders(chat_id, status="pending", limit=200)
    await update.message.reply_text(format_list(rows, "🗓️ Pendientes:"))


async def listall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = fetch_reminders(chat_id, status=None, limit=200)
    await update.message.reply_text(format_list(rows, "🗂️ Lista completa:"))


async def sumq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    start, end, label = current_quincena_range()
    rows = fetch_range(chat_id, start, end, category=None)
    await update.message.reply_text(format_pay_sum(rows, label))


# ================== Callback buttons ==================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    data = query.data or ""

    if data.startswith("done:"):
        rid = int(data.split(":")[1])
        result = mark_done(chat_id, rid)
        if not result:
            await query.edit_message_text("✅ Ese recordatorio ya no está pendiente.")
            return
        rid2, task = result
        await query.edit_message_text(f"✅ Listo, marcado como hecho: #{rid2} — {task}")
        return

    if data.startswith("resched:"):
        rid = int(data.split(":")[1])
        set_awaiting_datetime_for_reschedule(context, {"rid": rid})
        await query.message.reply_text(
            "⏳ ¿Cuándo te lo recuerdo otra vez?\n"
            "Ejemplos:\n"
            "• en 10 minutos\n"
            "• hoy 5:30pm\n"
            "• mañana 7pm\n"
            "• 2026-02-03 07:00"
        )
        return

    if data.startswith("snooze1h:"):
        rid = int(data.split(":")[1])
        due_utc = update_reminder_time(chat_id, rid, now_local() + timedelta(hours=1))
        if not due_utc:
            await query.message.reply_text("No encontré ese recordatorio. Puede que ya esté eliminado.")
            return
        await query.message.reply_text(
            f"🕐 Listo. Reprogramado #{rid} para dentro de 1 hora.\n⏰ {pretty_dt_local(from_utc_iso(due_utc))}"
        )
        return


# ================== Text interpreter ==================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    tl = text.lower()

    # 0) borrar 4,5,6
    ids = parse_delete_request(text)
    if ids:
        deleted = []
        not_found = []
        for rid in ids:
            res = delete_reminder(chat_id, rid)
            if res:
                deleted.append(rid)
            else:
                not_found.append(rid)

        msg = []
        if deleted:
            msg.append("🗑️ Eliminados: " + ", ".join(f"#{x}" for x in deleted))
        if not_found:
            msg.append("⚠️ No encontrados: " + ", ".join(f"#{x}" for x in not_found))

        await update.message.reply_text("\n".join(msg) if msg else "No pude borrar nada.")
        return

    # 0.1) comandos/ayuda
    if is_commands_request(text):
        await update.message.reply_text(
            "📌 Comandos disponibles (sin slash):\n\n"
            "🗓️ Listas:\n"
            "• lista / generar lista / genera la lista  → pendientes\n"
            "• toda la lista / lista completa → todo\n"
            "• lista de la quincena / toda la quincena → todo en la quincena actual\n\n"
            "🗑️ Borrar:\n"
            "• borrar 4\n"
            "• borrar 4,5,6\n\n"
            "💸 Sumas:\n"
            "• sumame cuanto debo pagar esta quincena\n"
            "• suma segunda quincena de febrero\n\n"
            "⏰ Recordatorios:\n"
            "• Recuérdame \"botar basura\" en 10 minutos\n"
            "• Recuérdame pagar 1.000 pesos a Daniela el 1 de febrero"
        )
        return

    # 0.2) lista pendientes
    if is_pending_list_request(text):
        rows = fetch_reminders(chat_id, status="pending", limit=200)
        await update.message.reply_text(format_list(rows, "🗓️ Pendientes:"))
        return

    # 0.3) lista completa
    if is_full_list_request(text):
        rows = fetch_reminders(chat_id, status=None, limit=200)
        await update.message.reply_text(format_list(rows, "🗂️ Lista completa:"))
        return

    # 0.4) lista de la quincena actual (incluye done/pending en el rango)
    if is_quincena_list_request(text):
        start, end, label = current_quincena_range()
        rows = fetch_range(chat_id, start, end, category=None)
        await update.message.reply_text(format_list(rows, f"🗓️ Lista en {label}:"))
        return

    # 1) esperando fecha/hora para reprogramar
    if is_awaiting_datetime_for_reschedule(context):
        payload = pop_awaiting_datetime_for_reschedule(context)
        rid = int(payload["rid"])
        dt = parse_date_time_from_text(text)
        if not dt:
            set_awaiting_datetime_for_reschedule(context, payload)
            await update.message.reply_text(
                "No entendí esa fecha/hora 😅\n"
                "Ej: en 10 minutos / mañana 7pm / hoy 5:30pm / 2026-02-03 07:00"
            )
            return

        due_utc = update_reminder_time(chat_id, rid, dt)
        if not due_utc:
            await update.message.reply_text("No encontré ese recordatorio. Puede que ya esté eliminado.")
            return

        await update.message.reply_text(f"🔁 Listo. Reprogramado #{rid}\n⏰ {pretty_dt_local(from_utc_iso(due_utc))}")
        return

    # 2) esperando fecha/hora para crear nuevo
    if is_awaiting_datetime_for_new(context):
        payload = pop_awaiting_datetime_for_new(context)
        dt = parse_date_time_from_text(text)
        if not dt:
            set_awaiting_datetime_for_new(context, payload)
            await update.message.reply_text(
                "No entendí esa fecha/hora 😅\n"
                "Ej: en 10 minutos / mañana 7pm / hoy 5:30pm / 2026-02-03 07:00"
            )
            return

        rid, due_utc = insert_reminder(
            chat_id=chat_id,
            task=payload["task"],
            due_local=dt,
            amount_cop=payload.get("amount_cop"),
            category=payload.get("category", "general")
        )
        msg = f"📌 Guardado: #{rid}\n⏰ {pretty_dt_local(from_utc_iso(due_utc))}\n🧾 {payload['task']}"
        if payload.get("amount_cop"):
            msg += f"\n💸 Monto: {pretty_money(payload['amount_cop'])}"
        await update.message.reply_text(msg)
        return

    # 3) confirmación rápida
    if looks_like_confirm(text):
        result = mark_done(chat_id, None)
        if not result:
            await update.message.reply_text("✅ No veo pendientes para marcar.")
            return
        rid, task = result
        await update.message.reply_text(f"✅ Hecho: #{rid} — {task}")
        return

    # 4) sumas por quincena (natural)
    if "quincena" in tl and any(k in tl for k in ["suma", "sumame", "sumar", "cuanto debo", "cuánto debo", "cuanto tengo", "total"]):
        q = parse_quincena_query(text)
        if not q:
            start, end, label = current_quincena_range()
        else:
            start, end, label = q

        rows = fetch_range(chat_id, start, end, category=None)
        await update.message.reply_text(format_pay_sum(rows, label))
        return

    # 5) crear recordatorio/pago
    parsed = parse_create_intent(text)
    if parsed:
        if parsed.due_local is None:
            set_awaiting_datetime_for_new(context, {
                "task": parsed.task,
                "amount_cop": parsed.amount_cop,
                "category": parsed.category
            })
            ask = "¿Para cuándo te lo recuerdo? (en 10 minutos / mañana 7pm / 2026-02-03 07:00)"
            if parsed.amount_cop:
                await update.message.reply_text(
                    f"Entendí esto:\n🧾 {parsed.task}\n💸 {pretty_money(parsed.amount_cop)}\n\n{ask}"
                )
            else:
                await update.message.reply_text(f"Entendí esto:\n🧾 {parsed.task}\n\n{ask}")
            return

        rid, due_utc = insert_reminder(chat_id, parsed.task, parsed.due_local, parsed.amount_cop, parsed.category)
        msg = f"📌 Guardado: #{rid}\n⏰ {pretty_dt_local(from_utc_iso(due_utc))}\n🧾 {parsed.task}"
        if parsed.amount_cop:
            msg += f"\n💸 Monto: {pretty_money(parsed.amount_cop)}"
        await update.message.reply_text(msg)
        return

    # fallback
    await update.message.reply_text(
        "Te leo 👀\n"
        "Prueba:\n"
        "• lista\n"
        "• toda la lista\n"
        "• lista de la quincena\n"
        "• borrar 4,5,6\n"
        "• comandos\n"
        '• Recuérdame "botar basura" en 10 minutos\n'
        "• Recuérdame pagar 1.000 pesos a Daniela el 1 de febrero\n"
        "• Sumame cuanto debo pagar esta quincena"
    )


# ================== Scheduler ==================
async def tick_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.execute(
        "SELECT id, chat_id, task, due_utc, retry_count, last_sent_utc "
        "FROM reminders WHERE status='pending'"
    )
    rows = cur.fetchall()

    now = utc_now()

    for rid, chat_id, task, due_utc, retry_count, last_sent_utc in rows:
        due_dt = datetime.fromisoformat(due_utc.replace("Z", "+00:00"))

        if now < due_dt:
            continue

        # Reintentos si no respondes
        if retry_count >= MAX_RETRIES:
            conn.execute("UPDATE reminders SET status='done', completed_utc=? WHERE id=?", (utc_iso(now), rid))
            conn.commit()
            continue

        if last_sent_utc:
            last = datetime.fromisoformat(last_sent_utc.replace("Z", "+00:00"))
            mins = (now - last).total_seconds() / 60.0
            if mins < RETRY_EVERY_MINUTES:
                continue

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ Recordatorio:\n{task}\n\n¿Qué hiciste?",
                reply_markup=reminder_keyboard(rid)
            )
            conn.execute(
                "UPDATE reminders SET retry_count=retry_count+1, last_sent_utc=? WHERE id=?",
                (utc_iso(now), rid)
            )
            conn.commit()
        except Exception:
            pass

    conn.close()


# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN en el .env")

    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()

    # Slash (opcionales)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("listall", listall_cmd))
    app.add_handler(CommandHandler("sumq", sumq_cmd))

    # Botones inline
    app.add_handler(CallbackQueryHandler(on_callback))

    # Texto normal
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Checker
    app.job_queue.run_repeating(tick_job, interval=30, first=5)

    print("🤖 Bot SIN IA (todo integrado) corriendo... (CTRL+C para parar)")
    app.run_polling()


if __name__ == "__main__":
    main()

