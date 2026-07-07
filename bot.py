"""
بوت تليجرام لتنبيهات التداول
- ينبّه قبل الأخبار الاقتصادية المهمة
- ينبّه لما شرط استراتيجية EMA/RSI يتحقق على EUR/USD
"""

import os
import json
from datetime import datetime, timezone

import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "state.json"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SYMBOL = "EURUSD=X"

WATCH_CURRENCIES = {"USD", "EUR"}
IMPACT_LEVELS = {"High"}
NEWS_LEAD_MINUTES = 30          # نبّه قبل الخبر بـ 30 دقيقة
NEAR_EMA_THRESHOLD = 0.0007     # هامش القرب من EMA20 (٠.٠٧٪ من السعر)


# ---------- إدارة الحالة (عشان منبعتش نفس التنبيه أكتر من مرة) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified_events": [], "last_signal": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- تليجرام ----------

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=15)
        if not r.ok:
            print("فشل إرسال رسالة تليجرام:", r.text)
    except Exception as e:
        print("خطأ في الاتصال بتليجرام:", e)


# ---------- التقويم الاقتصادي ----------

def check_news(state):
    try:
        resp = requests.get(CALENDAR_URL, timeout=15)
        events = resp.json()
    except Exception as e:
        print("تعذر جلب التقويم الاقتصادي:", e)
        return

    now = datetime.now(timezone.utc)

    for event in events:
        currency = event.get("country")
        impact = event.get("impact")
        if currency not in WATCH_CURRENCIES or impact not in IMPACT_LEVELS:
            continue

        event_time_str = event.get("date")
        if not event_time_str:
            continue
        try:
            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
        except Exception:
            continue

        event_id = f"{event.get('title')}_{event_time_str}"
        minutes_until = (event_time - now).total_seconds() / 60

        if 0 <= minutes_until <= NEWS_LEAD_MINUTES and event_id not in state["notified_events"]:
            send_telegram(
                "⚠️ خبر اقتصادي مهم بعد {m} دقيقة\n"
                "العملة: {c}\n"
                "الحدث: {t}\n"
                "التوقع: {f}\n"
                "السابق: {p}\n\n"
                "تجنب فتح صفقات جديدة خلال ٣٠ دقيقة قبل وبعد الخبر."
                .format(
                    m=int(minutes_until),
                    c=currency,
                    t=event.get("title"),
                    f=event.get("forecast", "-"),
                    p=event.get("previous", "-"),
                )
            )
            state["notified_events"].append(event_id)

    # الاحتفاظ بآخر 200 حدث بس عشان الملف يفضل صغير
    state["notified_events"] = state["notified_events"][-200:]


# ---------- المؤشرات الفنية ----------

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def check_strategy(state):
    try:
        df15 = yf.download(SYMBOL, period="5d", interval="15m", progress=False)
        df5 = yf.download(SYMBOL, period="2d", interval="5m", progress=False)
    except Exception as e:
        print("تعذر جلب بيانات السعر:", e)
        return

    if df15.empty or df5.empty or len(df5) < 25:
        return

    # نسخ حديثة من yfinance بترجع أعمدة متعددة المستويات (MultiIndex)
    # حتى مع رمز واحد بس - نبسّطها هنا عشان نتجنب مشاكل النوع
    if hasattr(df15.columns, "levels"):
        df15.columns = df15.columns.get_level_values(0)
    if hasattr(df5.columns, "levels"):
        df5.columns = df5.columns.get_level_values(0)

    df15["ema50"] = ema(df15["Close"], 50)
    df5["ema20"] = ema(df5["Close"], 20)
    df5["rsi"] = rsi(df5["Close"], 14)

    trend_up = float(df15["Close"].iloc[-1]) > float(df15["ema50"].iloc[-1])
    trend_down = float(df15["Close"].iloc[-1]) < float(df15["ema50"].iloc[-1])

    price = float(df5["Close"].iloc[-1])
    ema20 = float(df5["ema20"].iloc[-1])
    rsi_now = float(df5["rsi"].iloc[-1])
    rsi_prev = float(df5["rsi"].iloc[-2])

    near_ema = abs(price - ema20) / price < NEAR_EMA_THRESHOLD

    signal = None
    if trend_up and near_ema and rsi_prev < 40 <= rsi_now:
        signal = "شراء (Long)"
    elif trend_down and near_ema and rsi_prev > 60 >= rsi_now:
        signal = "بيع (Short)"

    now_hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
    signal_key = f"{signal}_{now_hour_key}" if signal else None

    if signal and state.get("last_signal") != signal_key:
        send_telegram(
            "📊 إشارة استراتيجية على EUR/USD\n"
            "النوع: {s}\n"
            "السعر الحالي: {p:.5f}\n"
            "EMA20 (M5): {e:.5f}\n"
            "RSI (M5): {r:.1f}\n"
            "الاتجاه العام (M15): {trend}\n\n"
            "ذكّر نفسك: حدد وقف خسارة وهدف ربح قبل الدخول. دي إشارة آلية مش توصية استثمارية."
            .format(
                s=signal,
                p=price,
                e=ema20,
                r=rsi_now,
                trend="صاعد" if trend_up else "هابط",
            )
        )
        state["last_signal"] = signal_key


def main():
    state = load_state()
    check_news(state)
    check_strategy(state)
    save_state(state)


if __name__ == "__main__":
    main()
