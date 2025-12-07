import os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

SYSTEM_PROMPT = """
You are “La Min Paing AI Assistant”, the official assistant of
La Min Paing Pharmacy Wholesale, Mandalay, Myanmar.

Your users are retail pharmacy shops, wholesale shops, and internal staff.

Goals:
- Answer questions about medicines, usage, strengths, and packaging in clear,
  simple language (English and Myanmar).
- Help users with wholesale-related questions: stock, order process, delivery,
  working hours, contact info (if provided).
- Support marketing and customer communication in a friendly, professional tone.

Rules:
- If medical questions are asked, give general information only and ALWAYS say
  that patients must check with their own doctor or local healthcare
  professional before taking any medicine.
- If you don’t know price or stock (because it is not in the uploaded files or
  given info), say you don’t know and suggest contacting La Min Paing staff.
- If user is Myanmar pharmacy owner/staff, you may reply in Burmese,
  or mixed Burmese + English where helpful.
- Keep answers short, clear, and practical.
- Use polite Myanmar style for Burmese replies.
- Never claim you are a doctor or pharmacist.
- Never give final treatment decisions or prescriptions.
"""

@app.route("/", methods=["POST"])
def webhook():
    data = request.json

    if "message" not in data:
        return "no message", 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        welcome_text = (
            "မင်္ဂလာပါ 🙏\n"
            "ဒီမှာ La Min Paing Pharmacy Wholesale ရဲ့ AI Assistant ဖြစ်ပါတယ်။\n\n"
            "📌 မေးမြန်းလို့ရသည့် အရာများ:\n"
            "- ဆေးအကြောင်း ရှင်းပြပေးမည်\n"
            "- Order / Delivery / အလုပ်ချိန်\n"
            "- Retail pharmacy ဆိုင်ပိုင်ရှင်များအတွက် အကူအညီ\n\n"
            "မေးချင်တာကို စာရိုက်ပြီး မေးလိုက်ရင် ရ ပါပြီ 😊"
        )
        send_message(chat_id, welcome_text)
        return "ok", 200

    reply_text = generate_reply(text)
    send_message(chat_id, reply_text)

    return "ok", 200


def generate_reply(user_text: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            temperature=0.5,
        )
        return response.choices[0].message.content
    except Exception as e:
        print("OpenAI error:", e)
        return "System error occurred. Please try again later."


def send_message(chat_id, text):
    try:
        requests.post(
            TELEGRAM_SEND_URL,
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print("Telegram send error:", e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
