import json
import os
import random
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage

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
    """對長期價值研究候選做可審計的二次查核。"""
    client = genai.Client()
    tasks = payload_data.get("tasks", [])
    limits = payload_data.get("portfolio_limits", {})
    tasks_str = json.dumps(tasks, indent=2, ensure_ascii=False)
    limits_str = json.dumps(limits, indent=2, ensure_ascii=False)
    grounding_state = "已啟用 Google Search grounding" if ENABLE_GEMINI_SEARCH else "未啟用 Google Search grounding，僅做非連網邏輯覆核"

    prompt = f"""
你是協助年輕投資人的長期價值研究員。投資組合採 70% ETF 核心、最多 30% 主動個股；只做多，不使用槓桿、期權或放空。Quant 模型只提供研究漏斗，不能直接下買入指令。

【執行狀態】{grounding_state}
【模型】{GEMINI_MODEL}
【投資組合限制】
{limits_str}

【Quant 候選與查核任務】
{tasks_str}

請逐檔輸出，嚴格遵守：
1. 先列出三句話投資論點、最強反方論點、論點失效條件；無法驗證時標記「待查」，不可假裝確認。
2. 建立悲觀/基準/樂觀三情境，說明營收、毛利、自由現金流與估值倍數假設，不提供精確目標價幻覺。
3. 驗證最新 10-K/10-Q footnotes、管理層資本配置、股數稀釋、產業瓶頸與主要競爭風險。
4. 檢查候選和常見大盤/科技 ETF 的個股與產業重疊；重疊過高時建議降低主動部位，而不是把同一風險買兩次。
5. 高 Short Interest 只作波動風險，不得變成事件交易、放空或期權建議。
6. 分流為【研究優先】、【觀察】、【排除】或【資料不足】；說明最關鍵的升級/降級條件。
7. 即使列為研究優先，也只能建議完成研究後的小額起始部位，且不得突破 payload 的權重上限。

輸出：繁體中文、直接、可審計；避免「保證」、「必漲」、「完美」等語言，並在最後提醒這不是個人化投資建議。
"""

    config = build_generation_config()
    last_error = ""
    max_retries = max(1, GEMINI_MAX_RETRIES)
    for attempt in range(max_retries):
        try:
            print(f"[+] 啟動長期價值 Agent 二審：model={GEMINI_MODEL}, search={ENABLE_GEMINI_SEARCH}, 嘗試 {attempt + 1}/{max_retries}...")
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
        except Exception as exc:
            last_error = str(exc)
            if is_retryable_error(last_error):
                if attempt < max_retries - 1:
                    delay = retry_delay_seconds(attempt)
                    print(f"[-] Gemini API 暫時性錯誤/限流，冷卻 {delay:.1f} 秒後重試。錯誤摘要：{last_error[:180]}")
                    time.sleep(delay)
                    continue
                return f"【系統警告】Gemini API 已重試 {max_retries} 次仍失敗；本批標的未完成二審。錯誤摘要：{last_error[:240]}\n"
            raise


def send_final_decision_email(final_memo):
    user_email = os.environ.get("USER_EMAIL")
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    if not all([user_email, sender_email, sender_pwd]):
        print("[-] 郵件環境變數不完整，略過報告寄送。")
        return

    msg = EmailMessage()
    msg["Subject"] = f"【Mode C 長期價值研究】候選標的二審 - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender_email
    msg["To"] = user_email
    msg.set_content(final_memo)

    csv_path = "mode_c_shortlist.csv"
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_pwd)
        server.send_message(msg)
        print("[+] 長期價值研究報告已寄出。")


if __name__ == "__main__":
    payload = load_quant_payload()
    if payload and payload.get("tasks"):
        all_tasks = payload.get("tasks", [])
        target_tasks = all_tasks[:5]
        final_memo_report = ""
        batch_size = int(os.environ.get("GEMINI_BATCH_SIZE", "1"))
        total_batches = (len(target_tasks) + batch_size - 1) // batch_size

        for i in range(0, len(target_tasks), batch_size):
            current_batch_idx = (i // batch_size) + 1
            batch_data = {
                "tasks": target_tasks[i:i + batch_size],
                "portfolio_limits": payload.get("portfolio_limits", {}),
            }
            print(f"\n[+] 正在執行第 {current_batch_idx}/{total_batches} 批長期研究覆核...")
            memo = execute_pm_agent_reasoning(batch_data)
            final_memo_report += memo + "\n\n"
            if current_batch_idx < total_batches:
                print(f"[!] 本批完成，冷卻 {GEMINI_BETWEEN_TASK_SECONDS:.1f} 秒以降低 API 限流機率...")
                time.sleep(GEMINI_BETWEEN_TASK_SECONDS)

        with open("Mode_C_Final_Decision_Memo.md", "w", encoding="utf-8") as f:
            f.write(final_memo_report)
        print("[+] Mode_C_Final_Decision_Memo.md 已整合完畢。")
        send_final_decision_email(final_memo_report)
    else:
        print("[*] 本次沒有通過長期持有門檻的候選，不啟動 AI 二審。")
