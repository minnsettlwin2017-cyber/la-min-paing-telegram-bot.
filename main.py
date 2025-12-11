import os
import csv
import difflib
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# NEW: web scraping imports
import requests
from bs4 import BeautifulSoup

# ---------- LOAD ENV ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

# ---------- LOAD OPENAI ----------
from openai import OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- ORDER STATES ----------
NAME, PHONE, ADDRESS, ITEMS, CONFIRM = range(5)

# ---------- CSV / PRICELIST ----------
ORDERS_CSV = "orders.csv"

# အခုက PRICELIST_CSV မသုံးတော့ပေမယ့် ကုန်ပစ္စည်း structure အတွက်အရန်ထားရင်လဲရ
PRICELIST_CSV = "pricelist.csv"

PRICELIST = []

# NEW: Online pricelist URL (IT STAR website)
PRICE_URL = (
    "https://laminpaing.itstar.io/product/price"
    "?bid=00000000-0000-0000-0000-000000000001"
    "&img=0&pid=1&exp=NjM5MDEwOTQ0MDAwMDAwMDAw&sort=price"
)


# ==============================================
#  PRICE LIST FUNCTIONS (ONLINE)
# ==============================================
def load_pricelist():
    """
    Website (PRICE_URL) ထဲက HTML table ကို parse လုပ်ပြီး
    PRICELIST list of dicts ထဲ data ထည့်ပေးမယ်।
    """
    global PRICELIST
    PRICELIST = []

    try:
        print("[INFO] Fetching pricelist from website...")
        r = requests.get(PRICE_URL, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("[ERROR] Failed to fetch pricelist:", e)
        return

    soup = BeautifulSoup(r.text, "html.parser")

    # table structure ကို auto-detect (header row ကိုလိုက်ကြည့်)
    table = soup.find("table")
    if not table:
        print("[ERROR] No <table> found in pricelist page")
        return

    rows = table.find_all("tr")
    if not rows:
        print("[ERROR] No <tr> rows in pricelist table")
        return

    # header row
    header_cells = rows[0].find_all(["th", "td"])
    headers = [h.get_text(strip=True).lower() for h in header_cells]

    def find_index(keywords, default=None):
        for i, h in enumerate(headers):
            for kw in keywords:
                if kw in h:
                    return i
        return default

    idx_name = find_index(["name", "item"])
    idx_price = find_index(["sale price", "price"])
    idx_unit = find_index(["unit"])
    idx_exp = find_index(["exp"])

    count = 0
    for tr in rows[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue
        texts = [c.get_text(strip=True) for c in cells]

        def safe_get(idx_):
            if idx_ is None:
                return ""
            if idx_ < len(texts):
                return texts[idx_]
            return ""

        name = safe_get(idx_name)
        price = safe_get(idx_price)
        unit = safe_get(idx_unit)
        exp = safe_get(idx_exp)

        if not name:
            continue

        PRICELIST.append({
            "Name": name,
            "Price": price,
            "Unit": unit,
            "Exp Date": exp,
        })
        count += 1

    print(f"[INFO] Loaded {count} products from ONLINE pricelist")


def search_items_substring(query, limit=10):
    q = query.lower()
    matches = []
    for row in PRICELIST:
        name = row.get("Name", "")
        if q in name.lower():
            matches.append(row)
        if len(matches) >= limit:
            break
    return matches


def search_items_fuzzy(query, limit=5, cutoff=0.5):
    """Spelling mistake allowed fuzzy match"""
    if not PRICELIST:
        return []

    q = query.lower()
    names = [row.get("Name", "") for row in PRICELIST]

    close = difflib.get_close_matches(q, [n.lower() for n in names], n=limit, cutoff=cutoff)

    results = []
    for c in close:
        for row in PRICELIST:
            if row.get("Name", "").lower() == c:
                results.append(row)
                break
    return results


def format_item(row):
    name = row.get("Name", "")
    price = row.get("Price", "")
    unit = row.get("Unit", "")
    exp = row.get("Exp Date", "") or row.get("Exp", "")

    text = f"📦 {name}\n💰 Price: {price}"
    if unit:
        text += f"\n📦 Unit: {unit}"
    if exp:
        text += f"\n⌛ Exp: {exp}"
    return text


def parse_items_and_total(items_text: str):
    """Auto total calculator (online pricelist data ကိုသုံးမယ်)"""
    total = 0.0
    detail = []
    unknown = []

    for raw in items_text.splitlines():
        line = raw.strip()
        if not line:
            continue

        lower = line.lower()
        if " x " in lower:
            parts = lower.split(" x ")
        elif "×" in lower:
            parts = lower.split("×")
        else:
            parts = [lower, "1"]

        name_part = parts[0].strip()
        qty_str = parts[1].strip().split()[0] if len(parts) > 1 else "1"

        try:
            qty = float(qty_str)
        except:
            qty = 1.0

        matches = search_items_substring(name_part, 1)
        if not matches:
            matches = search_items_fuzzy(name_part, 1, 0.5)

        if not matches:
            unknown.append(raw)
            continue

        price_str = matches[0].get("Price", "0").replace(",", "")
        try:
            price = float(price_str)
        except:
            unknown.append(raw)
            continue

        line_total = price * qty
        total += line_total
        detail.append(f"{raw} → {price:,.0f} x {qty:g} = {line_total:,.0f} Ks")

    return total, detail, unknown


# ==============================================
#  CHATGPT HANDLER
# ==============================================
async def ask_chatgpt(prompt: str) -> str:
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": "You are AI assistant for La Min Paing Pharmacy. Reply briefly in Burmese unless user uses English."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print("GPT ERROR:", e)
        return "❌ ChatGPT ကို ချိတ်ဆက်ရာမှာ ပြဿနာတစ်ခု ဖြစ်နေပါတယ်။"


# ==============================================
#  ORDER HANDLERS
# ==============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[KeyboardButton("🛒 Order တင်မယ်")]]
    rm = ReplyKeyboardMarkup(kb, resize_keyboard=True)

    await update.message.reply_text(
        "မင်္ဂလာပါ 🙏\n"
        "La Min Paing Pharmacy Wholesale AI Bot မှကြိုဆိုပါတယ်ခင်ဗျာ\n\n"
        "🛒 Order → 'Order တင်မယ်'\n"
        "💰 Price → /p name (ဥပမာ /p amlodipine)\n"
        "🤖 Q&A → တစ်ခြားမေးချင်တာ မြန်မာလို/English လို မေးပါ",
        reply_markup=rm
    )
    return ConversationHandler.END


async def order_start(update, context):
    # order တင်မယ်ဆိုတာနဲ့လည်း pricelist မရှိသေးရင် load လုပ်ပေးမယ်
    if not PRICELIST:
        load_pricelist()
    await update.message.reply_text("👤 Customer Name :")
    return NAME


async def get_name(update, context):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("📞 Phone :")
    return PHONE


async def get_phone(update, context):
    context.user_data["phone"] = update.message.text
    await update.message.reply_text("📍 Address ('မလို' ဆိုလည်းရ):")
    return ADDRESS


async def get_address(update, context):
    context.user_data["address"] = update.message.text
    await update.message.reply_text(
        "🧾 Items list ထည့်ပါ:\n"
        "- Amlodipine 5mg x 10 box\n"
        "- Euroamlo 10mg x 5 box"
    )
    return ITEMS


async def get_items(update, context):
    items = update.message.text
    context.user_data["items"] = items

    total, detail, unknown = parse_items_and_total(items)
    context.user_data["total"] = f"{total:,.0f}"

    name = context.user_data["name"]
    phone = context.user_data["phone"]
    address = context.user_data["address"]

    breakdown = ""
    if detail:
        breakdown += "💰 Price breakdown:\n" + "\n".join(detail) + "\n\n"
    if unknown:
        breakdown += "⚠️ pricelist ထဲမတွေ့တဲ့ items:\n" + "\n".join(unknown) + "\n\n"

    total_line = f"💵 စုစုပေါင်း: {total:,.0f} Ks\n\n"

    summary = (
        "📋 **Order Summary**\n"
        "------------------------\n"
        f"👤 {name}\n📞 {phone}\n📍 {address}\n\n"
        f"🧾 {items}\n\n"
        f"{breakdown}{total_line}"
        "စျေးနှုန်းများ အချိန်နှင့်အမျှ အပြောင်းအလဲ ရှိနိုင်ပါသည် သက်ဆိုင်ရာ way သမားမှ ပြန်လည်ဆက်သွယ် ပေးပါမည်ခင်ဗျာ\n"
        "ဘောက်ချာ ထွက်မှသာလျှင် စျေးနှုန်းနှင့် ရနိုင်မယ့် ပစ္စည်းအတည်ဖြစ်ပါမည်ခင်ဗျာ\n"
        "ဝယ်ယူအားပေးမှုအတွက် ကျေးဇူးအထူးတင်ရှိပါသည်ခင်ဗျာ\n"
        "Confirm လုပ်မလား?\n"
        "✅ Confirm     ❌ Cancel"
    )

    kb = [[KeyboardButton("✅ Confirm")], [KeyboardButton("❌ Cancel")]]
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return CONFIRM


async def confirm(update, context):
    text = update.message.text

    if text == "❌ Cancel":
        context.user_data.clear()
        await update.message.reply_text("❌ အော်ဒါ Cancel လုပ်ခဲ့သည်။")
        return ConversationHandler.END

    # Save CSV
    exists = os.path.isfile(ORDERS_CSV)
    with open(ORDERS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "name", "phone", "address", "items", "total"])
        if not exists:
            writer.writeheader()
        writer.writerow({
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": context.user_data["name"],
            "phone": context.user_data["phone"],
            "address": context.user_data["address"],
            "items": context.user_data["items"],
            "total": context.user_data["total"],
        })

    # Admin notify
    if ADMIN_CHAT_ID != 0:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 NEW ORDER\n👤 {context.user_data['name']}\n"
            f"📞 {context.user_data['phone']}\n📍 {context.user_data['address']}\n"
            f"🧾 {context.user_data['items']}\n"
            f"💵 Total {context.user_data['total']} Ks"
        )

    await update.message.reply_text(
        "✅ အော်ဒါ လက်ခံပြီးပါပြီ 🙏",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🛒 Order တင်မယ်")]], resize_keyboard=True)
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Order flow cancelled.")
    return ConversationHandler.END


# ==============================================
#  PRICE COMMAND /p
# ==============================================
async def price_command(update, context):
    if not PRICELIST:
        load_pricelist()

    if not PRICELIST:
        await update.message.reply_text("❌ Online pricelist ကို load မရနိုင်သေးပါ")
        return

    if not context.args:
        await update.message.reply_text("Usage: /p amlodipine")
        return

    query = " ".join(context.args)

    matches = search_items_substring(query, 5)
    fuzzy = False
    if not matches:
        matches = search_items_fuzzy(query, 5, 0.5)
        fuzzy = True

    if not matches:
        await update.message.reply_text(f"'{query}' ကို မတွေ့ပါ ❌")
        return

    if len(matches) == 1:
        await update.message.reply_text(format_item(matches[0]))
        return

    lines = []
    if fuzzy:
        lines.append(f"✏️ '{query}' နဲ့ အနီးစပ်ဆုံး results:")
    else:
        lines.append(f"'{query}' results:")

    for i, row in enumerate(matches, 1):
        lines.append(f"{i}. {row['Name']} – {row['Price']} Ks")

    await update.message.reply_text("\n".join(lines))


# ==============================================
#  GENERAL CHATGPT HANDLER
# ==============================================
async def general_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    reply = await ask_chatgpt(text)
    await update.message.reply_text(reply)


# ==============================================
#  MAIN
# ==============================================
def main():
    # bot start 때 online pricelist ကို တစ်ကြိမ် load လိုက်မယ်
    load_pricelist()

    app = ApplicationBuilder().token(TOKEN).build()

    # ORDER FLOW
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🛒 Order တင်မယ်$"), order_start)],
        states={
            NAME: [MessageHandler(filters.TEXT, get_name)],
            PHONE: [MessageHandler(filters.TEXT, get_phone)],
            ADDRESS: [MessageHandler(filters.TEXT, get_address)],
            ITEMS: [MessageHandler(filters.TEXT, get_items)],
            CONFIRM: [MessageHandler(filters.Regex("^(✅ Confirm|❌ Cancel)$"), confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Handlers priority order
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["p", "price"], price_command))
    app.add_handler(conv)

    # FINAL FALLBACK → ChatGPT Q&A
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, general_chat)
    )

    print("BOT RUNNING…")
    app.run_polling()


if __name__ == "__main__":
    main()
