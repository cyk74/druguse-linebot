import os
import sqlite3
import requests
import tempfile
import logging
from io import BytesIO

from flask import Flask, request, abort, send_from_directory
from PIL import Image

from linebot.v3.webhook import WebhookParser, WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from linebot.v3.messaging import MessagingApi, Configuration, ApiClient, MessagingApiBlob
from linebot.v3.messaging.models import (
    TextMessage, ReplyMessageRequest, PushMessageRequest,
    FlexMessage, FlexBubble, FlexBox, FlexText, FlexButton, URIAction,
    QuickReply, QuickReplyItem, LocationAction, ImageMessage, DatetimePickerAction,
    MessageAction
)
from linebot.v3.exceptions import InvalidSignatureError

import google.generativeai as genai
import json
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

CHANNEL_SECRET = os.environ.get("YOUR_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("YOUR_CHANNEL_ACCESS_TOKEN")
GOOGLE_MAP_API_KEY = os.environ.get("GOOGLE_MAP_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
base_url = os.environ.get("HF_SPACE_URL", "localhost")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN or not GOOGLE_API_KEY:
    raise RuntimeError("Missing essential environment variables")

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "linebot.db")

print("目前資料庫路徑：", DB_PATH)
print("資料庫檔案是否存在：", os.path.exists(DB_PATH))
try:
    with open(DB_PATH, "ab") as f:
        f.write(b"")
    print("✅ 資料庫有寫入權限")
except Exception as e:
    print("❌ 資料庫無法寫入：", e)

static_tmp_path = "/tmp"
os.makedirs(static_tmp_path, exist_ok=True)

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
parser = WebhookParser(CHANNEL_SECRET)
handler = WebhookHandler(CHANNEL_SECRET)

genai.configure(api_key=GOOGLE_API_KEY)
chat = genai.GenerativeModel(model_name="gemini-1.5-flash")
text_system_prompt = "你是一個專業的中文藥物安全衛教AI，運行於Linebot平台，負責為台灣用戶提供用藥查詢、衛教提醒、藥品辨識與互動諮詢。所有回應必須以繁體中文呈現，語氣需保持專業、中立、清晰，嚴禁使用非正式語彙或網路用語。你的回答僅限於台灣現行合法藥品、常見用藥安全及一般衛教知識，絕不涉及診斷、處方或違法用途。遇重要藥品資訊或警語時，務必標示資料來源（如衛福部、健保署或官方藥物資料庫）；無法查證時，需說明資訊有限並提醒用戶諮詢藥師。遇到模糊、非藥物相關、或疑似緊急情境（如中毒、嚴重過敏），請直接回覆：「請儘速就醫或聯絡藥師，Linebot無法提供緊急醫療協助。」回答時，優先給出簡明結論，再補充必要說明，遇複雜內容可分點陳述，藥品名稱、注意事項及用法用量需明顯標註。若用戶詢問非本功能範圍問題，請回覆：「本Linebot僅提供藥物安全與衛生教育資訊。」並簡要列舉可查詢主題（如用藥禁忌、藥物交互作用、藥品保存方式等）。所有資訊僅反映截至2025年6月之官方資料，若遇新藥、召回或重大警訊，應提醒用戶查閱衛福部或官方藥事機構。"

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

user_states = {}

def init_reminders_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        medicine TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        times TEXT NOT NULL,
        sent INTEGER DEFAULT 0
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id INTEGER,
        date TEXT,
        time TEXT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drugs (
        中文品名 TEXT,
        英文品名 TEXT,
        適應症 TEXT
    );
    """)
    conn.commit()
    conn.close()
init_reminders_table()

def add_reminder(user_id, medicine, start_date, end_date, times):
    print("[DEBUG] add_reminder 被呼叫")
    print(f"[DEBUG] 嘗試寫入提醒：{user_id}, {medicine}, {start_date}, {end_date}, {times}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO reminders (user_id, medicine, start_date, end_date, times, sent) VALUES (?, ?, ?, ?, ?, 0)",
        (user_id, medicine, start_date, end_date, json.dumps(times))
    )
    conn.commit()
    cursor.execute("SELECT * FROM reminders")
    print("[DEBUG] reminders 資料表內容：", cursor.fetchall())
    conn.close()
    print("[DEBUG] ✅ 寫入 reminders 成功")

def check_and_send_reminders():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, medicine, start_date, end_date, times FROM reminders")
    rows = cursor.fetchall()
    for rid, user_id, medicine, start_date, end_date, times_json in rows:
        if start_date <= today <= end_date:
            times = json.loads(times_json)
            for t in times:
                cursor.execute("SELECT COUNT(*) FROM reminders_log WHERE reminder_id=? AND date=? AND time=?", (rid, today, t))
                if now_time == t and cursor.fetchone()[0] == 0:
                    print(f"[DEBUG] 發送提醒給 {user_id}：{medicine} @ {t}")
                    with ApiClient(configuration) as api_client:
                        messaging_api = MessagingApi(api_client)
                        messaging_api.push_message(
                            push_message_request=PushMessageRequest(
                                to=user_id,
                                messages=[TextMessage(text=f"⏰ 用藥提醒：該服用「{medicine}」囉！")]
                            )
                        )
                    cursor.execute("INSERT INTO reminders_log (reminder_id, date, time) VALUES (?, ?, ?)", (rid, today, t))
    conn.commit()
    conn.close()

if not hasattr(app, "reminder_scheduler_started"):
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_send_reminders, 'interval', seconds=20)
    scheduler.start()
    app.reminder_scheduler_started = True

@app.route("/images/<filename>")
def serve_image(filename):
    return send_from_directory(static_tmp_path, filename)

@app.route("/")
def home():
    return {"message": "Line Webhook Server"}

@app.route("/show_reminders")
def show_reminders():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reminders")
    rows = cursor.fetchall()
    conn.close()
    print("[DEBUG] /show_reminders 查詢結果：", rows)
    return {"reminders": rows}

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    print(f"[DEBUG] 收到 callback 請求，body={body}")

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        print("[DEBUG] InvalidSignatureError")
        abort(400)
    except Exception as e:
        print("[DEBUG] Webhook parse error:", e)
        abort(400)

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        blob_api = MessagingApiBlob(api_client)

        for event in events:
            print(f"[DEBUG] event.type={event.type}, event={event}")
            # ====== 用藥提醒對話流程 ======
            if event.type == "message" and event.message.type == "text":
                user_id = event.source.user_id
                user_input = event.message.text.strip()
                print(f"[DEBUG] user_input: {user_input}, user_states: {user_states.get(user_id)}")
                # 修改用藥提醒選單
                if user_input == "修改用藥提醒":
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("SELECT DISTINCT medicine FROM reminders WHERE user_id=?", (user_id,))
                    medicines = [row[0] for row in cursor.fetchall()]
                    conn.close()
                    if not medicines:
                        reply_text = "你還沒有設定過任何藥物提醒。"
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        return "OK"
                    quick_reply = QuickReply(
                        items=[QuickReplyItem(action=MessageAction(label=med, text=med)) for med in medicines]
                    )
                    reply_text = "請選擇你要修改的藥品："
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    user_states[user_id] = {'step': 'edit_medicine'}
                    return "OK"
                elif user_input == "用藥提醒":
                    user_states[user_id] = {'step': 'ask_medicine'}
                    print(f"[DEBUG] 進入 ask_medicine, user_id={user_id}")
                    reply_text = "請輸入要提醒的藥品名稱："
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    return "OK"
                elif user_id in user_states:
                    state = user_states[user_id]
                    print(f"[DEBUG] user_states[{user_id}] = {state}")
                    if state.get('step') == 'ask_medicine':
                        state['medicine'] = user_input
                        state['step'] = 'ask_start'
                        print(f"[DEBUG] 進入 ask_start, user_id={user_id}, medicine={user_input}")
                        quick_reply = QuickReply(
                            items=[
                                QuickReplyItem(
                                    action=DatetimePickerAction(
                                        label="選擇開始日期",
                                        data="start_date",
                                        mode="date"
                                    )
                                )
                            ]
                        )
                        reply_text = "請選擇提醒開始日期："
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        return "OK"
                    elif state.get('step') == 'ask_times':
                        print(f"[DEBUG] 進入 ask_times, user_id={user_id}, state={state}")
                        times = [t.strip() for t in user_input.split(",") if t.strip()]
                        # 檢查每個時間格式是否為 HH:MM
                        import re
                        valid = True
                        for t in times:
                            if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", t):
                                valid = False
                                break
                        if not times or not valid:
                            reply_text = "時間格式錯誤，請重新輸入（24小時制，如 08:00,12:00,18:00）："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        # 時間格式正確才繼續
                        add_reminder(user_id, state['medicine'], state['start_date'], state['end_date'], times)
                        reply_text = f"已設定提醒：{state['medicine']}\n從 {state['start_date']} 到 {state['end_date']}\n每天：{', '.join(times)}"
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        user_states.pop(user_id, None)
                        print(f"[DEBUG] 完成提醒流程，user_states 移除 {user_id}")
                        return "OK"
                    # ====== 修改用藥提醒流程 ======
                    elif state.get('step') == 'edit_medicine':
                        selected_medicine = user_input
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT id, start_date, end_date, times FROM reminders WHERE user_id=? AND medicine=? ORDER BY id DESC LIMIT 1",
                            (user_id, selected_medicine)
                        )
                        row = cursor.fetchone()
                        conn.close()
                        if not row:
                            reply_text = "查無此藥品提醒資料。"
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            user_states.pop(user_id, None)
                            return "OK"
                        reminder_id, start_date, end_date, times_json = row
                        times = ','.join(json.loads(times_json))
                        reply_text = (
                            f"你目前的提醒設定：\n"
                            f"藥品：{selected_medicine}\n"
                            f"開始：{start_date}\n"
                            f"結束：{end_date}\n"
                            f"時間：{times}\n"
                            "請選擇要修改的欄位，或輸入 完成 結束："
                        )
                        quick_reply = QuickReply(
                            items=[
                                QuickReplyItem(action=MessageAction(label="開始日期", text="開始日期")),
                                QuickReplyItem(action=MessageAction(label="結束日期", text="結束日期")),
                                QuickReplyItem(action=MessageAction(label="提醒時間", text="提醒時間")),
                                QuickReplyItem(action=MessageAction(label="完成", text="完成")),
                            ]
                        )
                        state['step'] = 'edit_field'
                        state['reminder_id'] = reminder_id
                        state['medicine'] = selected_medicine
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        return "OK"
                    elif state.get('step') == 'edit_field':
                        field = user_input.strip()
                        if field == "開始日期":
                            state['step'] = 'edit_start_date'
                            quick_reply = QuickReply(
                                items=[
                                    QuickReplyItem(
                                        action=DatetimePickerAction(
                                            label="選擇開始日期",
                                            data="edit_start_date",
                                            mode="date"
                                        )
                                    )
                                ]
                            )
                            reply_text = "請選擇新的開始日期："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        elif field == "結束日期":
                            state['step'] = 'edit_end_date'
                            quick_reply = QuickReply(
                                items=[
                                    QuickReplyItem(
                                        action=DatetimePickerAction(
                                            label="選擇結束日期",
                                            data="edit_end_date",
                                            mode="date"
                                        )
                                    )
                                ]
                            )
                            reply_text = "請選擇新的結束日期："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        elif field == "提醒時間":
                            state['step'] = 'edit_times'
                            reply_text = "請輸入新的提醒時間（24小時制，用逗號分隔）："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        elif field.lower() == "完成":
                            reply_text = "已結束修改。"
                            user_states.pop(user_id, None)
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        else:
                            # 再次顯示選單
                            quick_reply = QuickReply(
                                items=[
                                    QuickReplyItem(action=MessageAction(label="開始日期", text="開始日期")),
                                    QuickReplyItem(action=MessageAction(label="結束日期", text="結束日期")),
                                    QuickReplyItem(action=MessageAction(label="提醒時間", text="提醒時間")),
                                    QuickReplyItem(action=MessageAction(label="完成", text="完成")),
                                ]
                            )
                            reply_text = "請選擇要修改的欄位，或輸入 完成 結束："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                    elif state.get('step') == 'edit_times':
                        import re
                        times = [t.strip() for t in user_input.split(",") if t.strip()]
                        valid = all(re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", t) for t in times)
                        if not times or not valid:
                            reply_text = "時間格式錯誤，請重新輸入（24小時制，如 08:00,12:00,18:00）："
                            reply_request = ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=reply_text)]
                            )
                            messaging_api.reply_message(reply_message_request=reply_request)
                            return "OK"
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute("UPDATE reminders SET times=? WHERE id=?", (json.dumps(times), state['reminder_id']))
                        conn.commit()
                        conn.close()
                        reply_text = "提醒時間已更新！"
                        # 修改完繼續顯示選單
                        quick_reply = QuickReply(
                            items=[
                                QuickReplyItem(action=MessageAction(label="開始日期", text="開始日期")),
                                QuickReplyItem(action=MessageAction(label="結束日期", text="結束日期")),
                                QuickReplyItem(action=MessageAction(label="提醒時間", text="提醒時間")),
                                QuickReplyItem(action=MessageAction(label="完成", text="完成")),
                            ]
                        )
                        reply_text += "\n請選擇要繼續修改的欄位，或輸入 完成 結束："
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text, quick_reply=quick_reply)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        state['step'] = 'edit_field'
                        return "OK"

                # ====== 其他功能區塊（查詢藥品、AI、藥局、圖片） ======
                user_input = event.message.text.strip()
                print("[DEBUG] 進入原有功能區塊，收到訊息：", user_input)

                # AI 問答
                if user_input.startswith("AI "):
                    prompt = "你是一個中文的AI助手，請用繁體中文回答。\n" + user_input[3:].strip()
                    try:
                        response = chat.generate_content(prompt)
                        reply_text = response.text
                    except Exception as e:
                        logging.exception("AI 問答發生錯誤")
                        reply_text = "⚠️ AI 回答失敗，請稍後再試"
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        return "OK"

                # 查詢藥品
                elif user_input == "查詢藥品":
                    try:
                        # 這裡應該要有 medicine_name 的來源，通常是 user_states 或請用戶再輸入
                        medicine_name = user_states.get(user_id, {}).get('medicine')
                        if not medicine_name:
                            reply_text = "請輸入要查詢的藥品名稱:"
                        else:
                            medicine_name = medicine_name.strip().lower()
                            conn = sqlite3.connect(DB_PATH)
                            cursor = conn.cursor()
                            query = """
                                SELECT DISTINCT 中文品名, 英文品名, 適應症
                                FROM drugs
                                WHERE LOWER(中文品名) = ? OR LOWER(英文品名) = ?
                                LIMIT 1
                            """
                            cursor.execute(query, (medicine_name, medicine_name))
                            row = cursor.fetchone()
                            conn.close()
                            print(f"[DEBUG] 查詢 drugs 結果：{row}")

                            if row:
                                zh_name, en_name, indication = row
                                # 副作用由 AI 產生
                                prompt = (
                                    f"請只用簡短條列式（每點用-開頭，不要用*），僅列出副作用，"
                                    f"針對藥品「{zh_name}」(英文名：{en_name})，"
                                    "請用繁體中文回答，不要加任何說明、警語或強調語句。"
                                )
                                try:
                                    ai_resp = chat.generate_content(prompt)
                                    side_effects = ai_resp.text.strip()
                                except Exception as e:
                                    logging.exception("AI 產生副作用失敗")
                                    side_effects = f"AI 回答失敗：{e}"
                                reply_text = (
                                    f"🔹 中文品名：{zh_name}\n"
                                    f"📌 英文品名：{en_name}\n"
                                    f"📄 適應症：{indication}\n"
                                    f"⚠️ 副作用：\n{side_effects}"
                                )
                            else:
                                reply_text = "未找到相關藥品，請重新輸入"
                    except Exception as e:
                        logging.exception("查詢資料時發生錯誤")
                        reply_text = f"⚠️ 查詢資料時發生錯誤，請稍後再試"

                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text.strip())]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)

                #圖片查詢
                elif user_input == "圖片查詢":
                    reply_text = "請傳送藥品圖片:"
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    return "OK"
                
                # 查詢藥局
                elif "查詢藥局" in user_input:
                    try:
                        quick_reply = QuickReply(
                            items=[QuickReplyItem(action=LocationAction(label="傳送我的位置"))]
                        )
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="請點選下方按鈕傳送你的位置，我才能幫你找附近藥局喔～", quick_reply=quick_reply)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                    except Exception as e:
                        logging.exception("查詢藥局發生錯誤")
                        reply_text = "⚠️ 查詢藥局失敗，請稍後再試"
                        reply_request = ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_text)]
                        )
                        messaging_api.reply_message(reply_message_request=reply_request)
                        return "OK"
                else:
                    try:
                        medicine_name = user_input
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        query = """
                        SELECT DISTINCT 中文品名, 英文品名, 適應症
                        FROM drugs
                        WHERE 中文品名 LIKE ? OR 英文品名 LIKE ?
                        LIMIT 1
                        """
                        like_param = f'%{medicine_name}%'
                        cursor.execute(query, (like_param, like_param))
                        row = cursor.fetchone()
                        conn.close()
                        print(f"[DEBUG] 查詢 drugs 結果：{row}")

                        if row:
                            zh_name, en_name, indication = row
                            # 副作用由 AI 產生
                            prompt = (
                                f"請只用簡短條列式（每點用-開頭，不要用*），僅列出副作用，"
                                f"針對藥品「{zh_name}」(英文名：{en_name})，"
                                "請用繁體中文回答，不要加任何說明、警語或強調語句。"
                            )
                            try:
                                ai_resp = chat.generate_content(prompt)
                                side_effects = ai_resp.text.strip()
                            except Exception as e:
                                side_effects = f"AI 回答失敗：{e}"
                            reply_text = (
                                f"🔹 中文品名：{zh_name}\n"
                                f"📌 英文品名：{en_name}\n"
                                f"📄 適應症：{indication}\n"
                                f"⚠️ 副作用：\n{side_effects}"
                            )
                        else:
                            prompt = (
                                f"請用以下格式，幫我介紹藥品「{medicine_name}」，"
                                "只要條列資料本身，不要加任何說明、警語或強調語句：\n"
                                "🔹 中文品名：\n"
                                "📌 英文品名：\n"
                                "📄 適應症：\n"
                                "⚠️ 副作用：\n（請用-開頭條列，不要用*）"
                            )
                            try:
                                ai_resp = chat.generate_content(prompt)
                                reply_text = ai_resp.text
                            except Exception as e:
                                reply_text = f"AI 回答失敗：{e}"

                    except Exception as e:
                        reply_text = f"⚠️ 查詢資料時發生錯誤：{str(e)}"

                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text.strip())]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)

            elif event.type == "message" and event.message.type == "location":
                print("[DEBUG] 收到位置訊息")
                user_lat = event.message.latitude
                user_lng = event.message.longitude

                nearby_url = (
                    f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?"
                    f"location={user_lat},{user_lng}&radius=1000&type=pharmacy&language=zh-TW&key={GOOGLE_MAP_API_KEY}"
                )
                nearby_res = requests.get(nearby_url).json()
                print(f"[DEBUG] nearby_res: {nearby_res}")

                if not nearby_res.get('results'):
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="附近找不到藥局")]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    return "OK"

                bubbles = []
                for place in nearby_res['results'][:3]:
                    place_id = place['place_id']
                    name = place.get('name', '藥局名稱未知')
                    address = place.get('vicinity', '地址不詳')
                    location = place['geometry']['location']
                    dest_lat, dest_lng = location['lat'], location['lng']

                    # 取得電話
                    details_url = (
                        f"https://maps.googleapis.com/maps/api/place/details/json?"
                        f"place_id={place_id}&fields=name,formatted_phone_number&key={GOOGLE_MAP_API_KEY}"
                    )
                    details_res = requests.get(details_url).json()
                    phone = details_res.get('result', {}).get('formatted_phone_number', '電話不詳')

                    # 取得距離
                    dist_url = (
                        f"https://maps.googleapis.com/maps/api/distancematrix/json?"
                        f"origins={user_lat},{user_lng}&destinations={dest_lat},{dest_lng}&key={GOOGLE_MAP_API_KEY}"
                    )
                    dist_res = requests.get(dist_url).json()
                    distance = dist_res['rows'][0]['elements'][0]['distance']['text']

                    map_url = f"https://www.google.com/maps/search/?api=1&query={dest_lat},{dest_lng}"

                    bubble = FlexBubble(
                        body=FlexBox(
                            layout="vertical",
                            contents=[
                                FlexText(text=name, weight="bold", size="lg"),
                                FlexText(text=f"地址：{address}", size="sm", color="#555555", wrap=True),
                                FlexText(text=f"電話：{phone}", size="sm", color="#555555"),
                                FlexText(text=f"距離：{distance}", size="sm", color="#777777"),
                            ],
                        ),
                        footer=FlexBox(
                            layout="vertical",
                            contents=[
                                FlexButton(
                                    style="link",
                                    height="sm",
                                    action=URIAction(label="地圖導航", uri=map_url),
                                )
                            ],
                        ),
                    )
                    bubbles.append(bubble)

                from linebot.v3.messaging.models import FlexCarousel, FlexMessage

                carousel = FlexCarousel(contents=bubbles)
                flex_message = FlexMessage(
                    alt_text="附近藥局推薦",
                    contents=carousel
                )

                reply_request = ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[flex_message]
                )
                messaging_api.reply_message(reply_message_request=reply_request)
                return "OK"
            elif event.type == "message" and event.message.type == "image":
                print("[DEBUG] 收到圖片訊息")
                try:
                    content = blob_api.get_message_content(message_id=event.message.id)
                    with tempfile.NamedTemporaryFile(dir=static_tmp_path, suffix=".jpg", delete=False) as tf:
                        tf.write(content)
                        filename = os.path.basename(tf.name)
                    image = Image.open(tf.name)

                    prompt = (
                        "請根據這張圖片判斷藥品資訊，若圖片無法判斷適應症或副作用，請根據藥品名稱推測並補充，"
                        "只要條列資料本身，不要加任何說明、警語或強調語句，也不要加**：\n"
                        "🔹 中文品名：\n"
                        "📌 英文品名：\n"
                        "📄 適應症：\n"
                        "⚠️ 副作用：\n（請用-開頭條列，不要用*）"
                    )

                    response = chat.generate_content([image, prompt])
                    description = response.text

                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=description.strip())]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                except Exception as e:
                    logging.exception("圖片處理發生錯誤")
                    reply_text = "⚠️ 圖片處理失敗，請稍後再試"
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    return "OK"

            elif event.type == "postback":
                user_id = event.source.user_id
                data = event.postback.data
                print(f"[DEBUG] postback data: {data}, user_states: {user_states.get(user_id)}")
                # 用藥提醒步驟分開訊息
                if data == "start_date":
                    user_states[user_id]['start_date'] = event.postback.params['date']
                    user_states[user_id]['step'] = 'ask_end'
                    # 先回覆
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"你選擇的開始日期為：{event.postback.params['date']}")]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    # 再推送下一步
                    quick_reply = QuickReply(
                        items=[
                            QuickReplyItem(
                                action=DatetimePickerAction(
                                    label="選擇結束日期",
                                    data="end_date",
                                    mode="date"
                                )
                            )
                        ]
                    )
                    messaging_api.push_message(
                        push_message_request=PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text="請選擇提醒結束日期：", quick_reply=quick_reply)]
                        )
                    )
                    return "OK"
                elif data == "end_date":
                    user_states[user_id]['end_date'] = event.postback.params['date']
                    user_states[user_id]['step'] = 'ask_times'
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"你選擇的結束日期為：{event.postback.params['date']}")]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    messaging_api.push_message(
                        push_message_request=PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text="請輸入每天要提醒的時間（24小時制，可多個，用逗號分隔，如 08:00,12:00,18:00）：")]
                        )
                    )
                    return "OK"
                # 修改用藥提醒步驟分開訊息
                elif data == "edit_start_date":
                    user_states[user_id]['step'] = 'edit_field'
                    new_start = event.postback.params['date']
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE reminders SET start_date=? WHERE id=?", (new_start, user_states[user_id]['reminder_id']))
                    conn.commit()
                    conn.close()
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"開始日期已更新為：{new_start}")]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    quick_reply = QuickReply(
                        items=[
                            QuickReplyItem(action=MessageAction(label="開始日期", text="開始日期")),
                            QuickReplyItem(action=MessageAction(label="結束日期", text="結束日期")),
                            QuickReplyItem(action=MessageAction(label="提醒時間", text="提醒時間")),
                            QuickReplyItem(action=MessageAction(label="完成", text="完成")),
                        ]
                    )
                    messaging_api.push_message(
                        push_message_request=PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text="請選擇要繼續修改的欄位，或輸入 完成 結束：", quick_reply=quick_reply)]
                        )
                    )
                    return "OK"
                elif data == "edit_end_date":
                    user_states[user_id]['step'] = 'edit_field'
                    new_end = event.postback.params['date']
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE reminders SET end_date=? WHERE id=?", (new_end, user_states[user_id]['reminder_id']))
                    conn.commit()
                    conn.close()
                    reply_request = ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"結束日期已更新為：{new_end}")]
                    )
                    messaging_api.reply_message(reply_message_request=reply_request)
                    quick_reply = QuickReply(
                        items=[
                            QuickReplyItem(action=MessageAction(label="開始日期", text="開始日期")),
                            QuickReplyItem(action=MessageAction(label="結束日期", text="結束日期")),
                            QuickReplyItem(action=MessageAction(label="提醒時間", text="提醒時間")),
                            QuickReplyItem(action=MessageAction(label="完成", text="完成")),
                        ]
                    )
                    messaging_api.push_message(
                        push_message_request=PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text="請選擇要繼續修改的欄位，或輸入 完成 結束：", quick_reply=quick_reply)]
                        )
                    )
                    return "OK"

    print("[DEBUG] callback 執行結束")
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860)