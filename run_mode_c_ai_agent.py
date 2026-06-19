import json
import os
import random
import re
import smtplib
import time
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from google import genai
from google.genai import types


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "4"))
GEMINI_RETRY_BASE_SECONDS = float(os.environ.get("GEMINI_RETRY_BASE_SECONDS", "30"))
GEMINI_RETRY_MAX_SECONDS = float(os.environ.get("GEMINI_RETRY_MAX_SECONDS", "180"))
GEMINI_BETWEEN_TASK_SECONDS = float(os.environ.get("GEMINI_BETWEEN_TASK_SECONDS", "20"))
GEMINI_REPORT_LIMIT = int(os.environ.get("GEMINI_REPORT_LIMIT", "5"))
ENABLE_GEMINI_SEARCH = os.environ.get("ENABLE_GEMINI_SEARCH", "1") == "1"
STATE_DIR = Path(os.environ.get("MODE_C_STATE_DIR", ".mode_c_state"))
HISTORY_PATH = STATE_DIR / "ai_report_history.json"

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
    with open(payload_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def iso_week_key(now=None) -> str:
    current = now or datetime.now(timezone.utc)
    year, week, _ = current.isocalendar()
    return f"{year}-W{week:02d}"


def load_weekly_history(path=HISTORY_PATH, now=None) -> dict:
    week = iso_week_key(now)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if payload.get("week") != week or not isinstance(payload.get("tickers"), list):
        return {"week": week, "tickers": []}
    tickers = list(dict.fromkeys(str(ticker).upper() for ticker in payload["tickers"] if ticker))
    return {"week": week, "tickers": tickers}


def save_weekly_history(history: dict, path=HISTORY_PATH) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, destination)


def select_weekly_tasks(tasks, history: dict, limit: int = GEMINI_REPORT_LIMIT):
    already_reviewed = {str(ticker).upper() for ticker in history.get("tickers", [])}
    selected = []
    for task in tasks:
        ticker = str(task.get("ticker") or "").upper()
        if not ticker or ticker in already_reviewed:
            continue
        selected.append(task)
        if len(selected) >= max(1, limit):
            break
    return selected


def mark_tasks_reviewed(history: dict, tasks) -> dict:
    tickers = list(history.get("tickers", []))
    seen = {str(ticker).upper() for ticker in tickers}
    for task in tasks:
        ticker = str(task.get("ticker") or "").upper()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return {"week": history.get("week") or iso_week_key(), "tickers": tickers}


def sanitize_report_text(text: str) -> str:
    """Remove emoji, invalid Unicode controls and decorative output noise."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\ufffd", "")
    cleaned = []
    for char in normalized:
        if char in "\n\t":
            cleaned.append(char)
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs", "Co", "Cn", "So"}:
            continue
        cleaned.append(char)
    output = "".join(cleaned)
    output = re.sub(r"[ \t]+", " ", output)
    output = re.sub(r" *\n *", "\n", output)
    output = re.sub(r"\n{3,}", "\n\n", output)
    return output.strip()


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

執行狀態：{grounding_state}
模型：{GEMINI_MODEL}
投資組合限制：
{limits_str}

Quant 候選與查核任務：
{tasks_str}

請逐檔輸出，嚴格遵守：
1. 使用純文字繁體中文，不使用 emoji、圖示、特殊裝飾符號、Markdown 表格或花式分隔線。
2. 固定使用下列小標：公司、結論、三句話投資論點、最強反方論點、論點失效條件、三情境、待查資料。
3. 無法驗證時寫「待查」，不可把待查內容寫成已確認。
4. 建立悲觀、基準、樂觀三情境，說明營收、毛利、自由現金流與估值倍數假設，不製造精確目標價幻覺。
5. 驗證最新 10-K 或 10-Q footnotes、管理層資本配置、股數稀釋、產業瓶頸與主要競爭風險。
6. 檢查候選和常見大盤或科技 ETF 的個股與產業重疊；重疊過高時建議降低主動部位。
7. 高 Short Interest 只作波動風險，不得變成事件交易、放空或期權建議。
8. 結論只能是研究優先、觀察、排除或資料不足，並說明最關鍵的升級或降級條件。
9. 即使列為研究優先，也只能建議完成研究後的小額起始部位，且不得突破 payload 的權重上限。
10. 避免保證、必漲、完美等語言，最後提醒這不是個人化投資建議。
"""

    config = build_generation_config()
    max_retries = max(1, GEMINI_MAX_RETRIES)
    for attempt in range(max_retries):
        try:
            print(f"[+] 啟動長期價值 Agent 二審：model={GEMINI_MODEL}, search={ENABLE_GEMINI_SEARCH}, 嘗試 {attempt + 1}/{max_retries}...")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            memo = sanitize_report_text(response.text or "")
            if not memo:
                return "系統警告：Gemini 回傳空內容；本批標的未完成二審。"
            footer = (
                f"\n\nAgent 二審設定：model={GEMINI_MODEL}；"
                f"Google Search grounding={'ON' if ENABLE_GEMINI_SEARCH else 'OFF'}"
            )
            return sanitize_report_text(memo + footer)
        except Exception as exc:
            error_message = str(exc)
            if is_retryable_error(error_message):
                if attempt < max_retries - 1:
                    delay = retry_delay_seconds(attempt)
                    print(f"[-] Gemini API 暫時性錯誤或限流，冷卻 {delay:.1f} 秒後重試。錯誤摘要：{error_message[:180]}")
                    time.sleep(delay)
                    continue
                return sanitize_report_text(
                    f"系統警告：Gemini API 已重試 {max_retries} 次仍失敗；本批標的未完成二審。錯誤摘要：{error_message[:240]}"
                )
            raise


def send_final_decision_email(final_memo):
    user_email = os.environ.get("USER_EMAIL")
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    if not all([user_email, sender_email, sender_pwd]):
        print("[-] 郵件環境變數不完整，略過報告寄送。")
        return

    msg = EmailMessage()
    msg["Subject"] = f"Mode C 長期價值研究 - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender_email
    msg["To"] = user_email
    msg.set_content(sanitize_report_text(final_memo))

    csv_path = "mode_c_shortlist.csv"
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as handle:
            msg.add_attachment(handle.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_pwd)
        server.send_message(msg)
        print("[+] 長期價值研究報告已寄出。")


def main():
    payload = load_quant_payload()
    output_path = Path("Mode_C_Final_Decision_Memo.md")
    if not payload or not payload.get("tasks"):
        output_path.write_text("本次沒有通過長期持有門檻的候選。\n", encoding="utf-8")
        print("[*] 本次沒有通過長期持有門檻的候選，不啟動 AI 二審。")
        return

    all_tasks = payload.get("tasks", [])
    history = load_weekly_history()
    target_tasks = select_weekly_tasks(all_tasks, history, GEMINI_REPORT_LIMIT)
    if not target_tasks:
        message = f"{history['week']} 的候選本週均已分析，本次不重複產生個股報告。\n"
        output_path.write_text(message, encoding="utf-8")
        print("[*] 本週沒有尚未分析的新候選；略過寄信。")
        return

    final_sections = []
    batch_size = max(1, int(os.environ.get("GEMINI_BATCH_SIZE", "1")))
    total_batches = (len(target_tasks) + batch_size - 1) // batch_size
    successful_tasks = []

    for index in range(0, len(target_tasks), batch_size):
        current_batch = target_tasks[index:index + batch_size]
        current_batch_idx = (index // batch_size) + 1
        batch_data = {
            "tasks": current_batch,
            "portfolio_limits": payload.get("portfolio_limits", {}),
        }
        tickers = ", ".join(str(task.get("ticker") or "") for task in current_batch)
        print(f"[+] 正在執行第 {current_batch_idx}/{total_batches} 批研究覆核：{tickers}")
        memo = execute_pm_agent_reasoning(batch_data)
        final_sections.append(memo)
        if not memo.startswith("系統警告："):
            successful_tasks.extend(current_batch)
            history = mark_tasks_reviewed(history, current_batch)
            save_weekly_history(history)

        if current_batch_idx < total_batches:
            print(f"[!] 本批完成，冷卻 {GEMINI_BETWEEN_TASK_SECONDS:.1f} 秒以降低 API 限流機率。")
            time.sleep(GEMINI_BETWEEN_TASK_SECONDS)

    final_memo_report = sanitize_report_text("\n\n".join(final_sections)) + "\n"
    output_path.write_text(final_memo_report, encoding="utf-8")
    print(f"[+] 報告完成；本週新增分析 {len(successful_tasks)} 檔，累計 {len(history.get('tickers', []))} 檔。")
    if successful_tasks:
        send_final_decision_email(final_memo_report)


if __name__ == "__main__":
    main()
