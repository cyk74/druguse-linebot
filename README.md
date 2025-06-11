# 💊 DRUGUSE LINEBOT 藥品小幫手

這是一個整合 **LINE Messaging API**、**Google Gemini AI**、**Google Maps API** 與 **SQLite** 的聊天機器人，用於提供：

- ✅ 用藥提醒設定與自動推播
- 🧠 藥品查詢(名稱、適應症、副作用)
- 📷 圖片藥品辨識(名稱、適應症、副作用)
- 🏥 附近藥局查詢（含地圖與電話）

---

## 📦 專案結構

```bash
.
├── app.py              # 主程式
├── linebot.db          # SQLite 資料庫（執行後產生）
├── requirements.txt    # Python 套件清單
├── Dockerfile          # Docker 容器設定
└── README.md           # 專案說明文件
```
---

## ⚙️ 安裝方式

1. 建立虛擬環境並安裝依賴
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. 設定環境變數：

| 變數名稱                  | 說明                                 |
|---------------------------|--------------------------------------|
| `YOUR_CHANNEL_SECRET`     | LINE Bot Webhook 驗證碼              |
| `YOUR_CHANNEL_ACCESS_TOKEN` | LINE Bot Access Token             |
| `GOOGLE_API_KEY`          | Google Gemini API 金鑰               |
| `GOOGLE_MAP_API_KEY`      | Google Maps API 金鑰（查詢藥局）     |

3. 啟動伺服器
```bash
python app.py
```

預設會開在 `https://kyle9574-linebot.hf.space/callback`。

---

## 🧠 功能說明

| 功能             | 說明                                                                 |
|------------------|----------------------------------------------------------------------|
| `用藥提醒`       | 啟動互動式提醒設定流程                                               |
| `修改用藥提醒`   | 顯示已有提醒並可修改開始/結束日與時間                              |
| `查詢藥品`       | 輸入藥品名稱或點選查詢功能，回覆藥名、適應症、副作用                |
| `圖片查詢`       | 上傳藥品圖片，由 Gemini 模型辨識與補充資訊                          |
| `查詢藥局`       | 傳送位置，回傳附近藥局（名稱、地址、距離、導航按鈕）               |

---

## 🔄 用藥提醒排程邏輯

- 使用 `APScheduler` 每 **20 秒** 檢查是否需提醒
- 若符合條件（時間到但尚未提醒），會使用 LINE API 推播訊息給對應用戶
- 推播訊息範例：`⏰ 用藥提醒：該服用「XXX」囉！`
- 發送後會寫入 `reminders_log` 防止重複推送

---

## 🗄 資料表說明（SQLite）

### `reminders`
| 欄位         | 說明                       |
|--------------|----------------------------|
| `id`         | 主鍵，自動遞增             |
| `user_id`    | LINE 使用者 ID             |
| `medicine`   | 藥品名稱                   |
| `start_date` | 開始日期 (YYYY-MM-DD)     |
| `end_date`   | 結束日期 (YYYY-MM-DD)     |
| `times`      | JSON 格式時間陣列 (HH:MM) |
| `sent`       | 是否已發送（備用欄位）     |

### `reminders_log`
| 欄位           | 說明                  |
|----------------|-----------------------|
| `id`           | 主鍵，自動遞增        |
| `reminder_id`  | 對應提醒的 ID         |
| `date`         | 提醒日期              |
| `time`         | 提醒時間              |

### `drugs`
| 欄位       | 說明       |
|------------|------------|
| 中文品名   | 藥品中文名 |
| 英文品名   | 藥品英文名 |
| 適應症     | 藥品用途   |

---

## 🧪 測試 API 路由

| 路徑              | 方法 | 功能               |
|-------------------|------|--------------------|
| `/`               | GET  | 健康檢查訊息       |
| `/callback`       | POST | LINE Webhook 接收  |
| `/images/<name>`  | GET  | 顯示暫存圖片       |
| `/show_reminders` | GET  | 顯示提醒資料表內容 |

---

## ⚠️ 注意事項

- 本專案使用 Gemini 模型回覆藥品內容，**不具診斷或處方建議效力**
- 用藥資料僅限參考，建議用戶仍以藥師或官方醫療資訊為準

---
