import os
import json
import time
import random
import smtplib
from email.message import EmailMessage
from datetime import datetime
from google import genai
from google.genai import types


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))
GEMINI_RETRY_BASE_SECONDS = float(os.environ.get("GEMINI_RETRY_BASE_SECONDS", "30"))
GEMINI_RETRY_MAX_SECONDS = float(os.environ.get("GEMINI_RETRY_MAX_SECONDS", "180"))
GEMINI_BETWEEN_TASK_SECONDS = float(os.environ.get("GEMINI_BETWEEN_TASK_SECONDS", "20"))
ENABLE_GEMINI_SEARCH = os.environ.get("ENABLE_GEMINI_SEARCH", "1") == "1"

RETRYABLE_ERROR_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "rate limit", "quota",
    "500", "502", "504", "DEADLINE_EXCEEDED", "timeout", "temporarily unavailable",
    "overloaded", "internal error",
)
FATAL_ERROR_MARKERS = (
    "API key not valid", "PERMISSION_DENIED", "not found for API version",
    "INVALID_ARGUMENT", "model not found", "does not exist", "authentication",
)


def is_retryable_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    if any(marker.lower() in msg for marker in FATAL_ERROR_MARKERS):
        return False
    return any(marker.lower() in msg for marker in RETRYABLE_ERROR_MARKERS)


def retry_delay_seconds(attempt: int) -> float:
    base = min(GEMINI_RETRY_MAX_SECONDS, GEMINI_RETRY_BASE_SECONDS * (2 ** attempt))
    jitter = random.uniform(0, max(1.0, base * 0.25))
    return min(GEMINI_RETRY_MAX_SECONDS, base + jitter)


def build_generation_config() -> types.GenerateContentConfig:
    kwargs = {"temperature": 0.1}
    if ENABLE_GEMINI_SEARCH:
        try:
            kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        except Exception as exc:
            print(f"[-] 無法啟用 Google Search grounding，改用非連網推理：{exc}")
    return types.GenerateContentConfig(**kwargs)

def load_quant_payload():
    payload_path = "mode_c_agent_payload.json"
    if not os.path.exists(payload_path):
        print(f"[-] 找不到 {payload_path}，中止 AI Agent 推理。")
        return None
    with open(payload_path, "r", encoding="utf-8") as f:
        return json.load(f)
def execute_pm_agent_reasoning(payload_data):
    """
    買方二審推理 + API 暫時性錯誤退避。

    修正重點：
    - 僅依 payload 的 must_verify / physical_check 執行，不把 TSMC/ASML/CSP 套到每檔股票。
    - 若 ENABLE_GEMINI_SEARCH=1，啟用 Google Search grounding；否則明確標註未連網。
    - 503/429/5xx 採 exponential backoff + jitter；認證/模型錯誤不盲目重試。
    """
    client = genai.Client()
    tasks = payload_data.get("tasks", [])
    tasks_str = json.dumps(tasks, indent=2, ensure_ascii=False)
    grounding_state = "已啟用 Google Search grounding" if ENABLE_GEMINI_SEARCH else "未啟用 Google Search grounding，僅做非連網邏輯覆核"

    prompt = f"""
你現在是買方資深 PM，執行【模式C：三階段雙殺模型】Layer 3/4 二審。

【執行狀態】{grounding_state}
【模型】{GEMINI_MODEL}

以下是 Quant Engine 產出的候選標的任務包：
{tasks_str}

請逐檔輸出結論，且嚴格遵守：
1. 只根據每檔的 `must_verify` 與 `physical_check` 做查核；不得把半導體/AI/CSP 的 TSMC/ASML/CSP 檢查泛化到所有股票。
2. 若 `physical_check` 寫明「不套用 TSMC/ASML」，請改查該產業真正瓶頸，例如需求、產能、庫存、價格、融資、法規、臨床/FDA、信用損失、客流、訂單/backlog 等。
3. 對每檔明確標示：`適用查核`、`不適用查核`、`仍需補查`。不要把「待查」寫成「已驗證」。
4. 若未啟用 Google Search grounding，凡涉及最新 CapEx、交期、短倉、財報日期、法說與新聞者，必須標示「未連網，待查」。
5. 最終仍需分流為【實質防禦】、【價值陷阱】、【博弈泡沫】或【資料不足/待查】，並指出最關鍵的降級條件。

輸出風格：繁體中文、直接、可審計；避免誇張保證語，例如「完美」、「鐵一般證明」、「終極武器」。
"""

    config = build_generation_config()
    last_error = ""
    max_retries = max(1, GEMINI_MAX_RETRIES)

    for attempt in range(max_retries):
        try:
            print(f"[+] 啟動 Mode-C Agent 二審：model={GEMINI_MODEL}, search={ENABLE_GEMINI_SEARCH}, 嘗試 {attempt + 1}/{max_retries}...")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            memo = (response.text or "").strip()
            if not memo:
                return "【系統警告】Gemini 回傳空內容；本批標的未完成二審。\n"
            footer = f"\n\n*(Agent 二審設定：model={GEMINI_MODEL}; Google Search grounding={'ON' if ENABLE_GEMINI_SEARCH else 'OFF'})*"
            return memo + footer

        except Exception as e:
            last_error = str(e)
            if is_retryable_error(last_error):
                if attempt < max_retries - 1:
                    delay = retry_delay_seconds(attempt)
                    print(f"[-] Gemini API 暫時性錯誤/限流，冷卻 {delay:.1f} 秒後重試。錯誤摘要：{last_error[:180]}")
                    time.sleep(delay)
                    continue
                print(f"[-] 連續 {max_retries} 次 API 暫時性錯誤，本批標的標記為待查。")
                return f"【系統警告】Gemini API 暫時性錯誤或限流，已重試 {max_retries} 次仍失敗；本批標的未完成二審。錯誤摘要：{last_error[:240]}\n"

            # 認證、模型不存在、參數錯誤等通常不是等待能解決；讓 workflow 明確失敗。
            raise e


def send_final_decision_email(final_memo):
    user_email = os.environ.get("USER_EMAIL")
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    
    if not all([user_email, sender_email, sender_pwd]):
        print("[-] 郵件環境變數不完整，略過報告寄送。")
        return
        
    msg = EmailMessage()
    msg["Subject"] = f"【Mode C 買方二審決策書】核心標的Agent審查日報 - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender_email
    msg["To"] = user_email
    msg.set_content(final_memo)
    
    csv_path = "mode_c_screen.csv"
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))
            
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_pwd)
        server.send_message(msg)
        print("[+] 頂級買方實戰決策書已成功送達指定郵箱。")

if __name__ == "__main__":
    payload = load_quant_payload()
    if payload and payload.get("tasks"):
        all_tasks = payload.get("tasks", [])
        
        # 🔪【買方戰略切片】：AI 深度推理只咬死最頂級的前 5 檔
        target_tasks = all_tasks[:5]
        final_memo_report = ""
        
        # 🎯【單發點射戰術】：維持一檔一檔發送，精確掌控日誌
        batch_size = int(os.environ.get("GEMINI_BATCH_SIZE", "1"))
        total_batches = (len(target_tasks) + batch_size - 1) // batch_size
        
        for i in range(0, len(target_tasks), batch_size):
            current_batch_idx = (i // batch_size) + 1
            batch_data = {"tasks": target_tasks[i : i + batch_size]}
            
            print(f"\n[+] 正在執行第 {current_batch_idx}/{total_batches} 檔核心標的推理...")
            memo = execute_pm_agent_reasoning(batch_data)
            final_memo_report += memo + "\n\n"
            
            if current_batch_idx < total_batches:
                print(f"[!] 本批推理完成，冷卻 {GEMINI_BETWEEN_TASK_SECONDS:.1f} 秒以降低 API 限流機率...")
                time.sleep(GEMINI_BETWEEN_TASK_SECONDS)
                
        with open("Mode_C_Final_Decision_Memo.md", "w", encoding="utf-8") as f:
            f.write(final_memo_report)
        print("[+] 最終 Mode_C_Final_Decision_Memo.md 報告已整合完畢。")
            
        send_final_decision_email(final_memo_report)
    else:
        print("[*] 今日初選底池為空，AI Agent 無法發動推理。")
