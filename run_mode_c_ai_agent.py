import os
import json
import time
import smtplib
from email.message import EmailMessage
from datetime import datetime
from google import genai
from google.genai import types

def load_quant_payload():
    payload_path = "mode_c_agent_payload.json"
    if not os.path.exists(payload_path):
        print(f"[-] 找不到 {payload_path}，中止 AI Agent 推理。")
        return None
    with open(payload_path, "r", encoding="utf-8") as f:
        return json.load(f)
def execute_pm_agent_reasoning(payload_data):
    """
    🎯 買方純邏輯高階推理 + 503 伺服器壅塞避震器
    遇到 Google 伺服器當機自動退避 30 秒，不浪費配額，確保管線不死。
    """
    client = genai.Client()
    tasks_str = json.dumps(payload_data.get("tasks", []), indent=2, ensure_ascii=False)
    
    prompt = f"""
    你現在是華爾街頂級買方資深 PM，嚴格執行【模式C：三階段雙雙殺模型】。
    
    以下是 Quant Engine 剛產出的高信念標的數據（已通過規模、便宜度、未來增長綜合篩選）：
    {tasks_str}
    
    請針對清單中的每檔黃金標的，執行 Layer 3 數據審查與 Layer 4 買方決策：
    1. 根據 'must_verify' 欄位，運用你內置的 2026 頂級知識庫與第一原理，審查其 footnotes，抓出 non-recurring / restructuring 等 EBITDA 隱蔽調整項，挑戰數據。
    2. 如果標的涉及半導體或 AI 供應鏈，強制對齊最新台積電(TSMC)先進製程產能利用率、ASML EUV 交期以及四大 CSP 的最新 CapEx 指引，驗證其隱含 CAGR 是否撞上物理產能硬限制。
    3. 交叉比對最新官方短倉數據，覆核 Short Interest > 15% 與 Days to Cover > 5 天的軋空禁制。
    
    輸出要求：
    - 直接破題，拒絕客套廢話，使用精準華爾街買方繁體中文。
    - 逐檔給出最終操盤論點，明確將標的分流為【實質防禦】、【價值陷阱】或【博弈泡沫】。
    """
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[+] 啟動買方純邏輯高階推理 (嘗試 {attempt + 1}/{max_retries})...")
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1
                )
            )
            return response.text + "\n\n*(註：本標的已啟用買方純邏輯推理方案)*"
            
        except Exception as e:
            error_msg = str(e)
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                if attempt < max_retries - 1:
                    print(f"[-] Google 伺服器滿載 (503 UNAVAILABLE)，進入強制冷卻退避 30 秒...")
                    time.sleep(30)
                else:
                    print(f"[-] 連續 {max_retries} 次撞擊 503 壅塞，跳過此標的。")
                    return f"【系統警告】此標的因 Google 伺服器滿載 (503) 無法完成推理。\n"
            else:
                # 遇到非 503 的其他致命錯誤，直接拋出讓程式停止
                raise e


def send_final_decision_email(final_memo):
    user_email = os.environ.get("USER_EMAIL")
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    
    if not all([user_email, sender_email, sender_pwd]):
        print("[-] 郵件環境變數不完整，略過報告寄送。")
        return
        
    msg = EmailMessage()
    msg["Subject"] = f"【Mode C 頂級買方實戰決策書】核心標的聯網審查日報 - {datetime.now().strftime('%Y-%m-%d')}"
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
        batch_size = 1
        total_batches = len(target_tasks)
        
        for i in range(0, len(target_tasks), batch_size):
            current_batch_idx = i + 1
            batch_data = {"tasks": target_tasks[i : i + batch_size]}
            
            print(f"\n[+] 正在執行第 {current_batch_idx}/{total_batches} 檔核心標的推理...")
            memo = execute_pm_agent_reasoning(batch_data)
            final_memo_report += memo + "\n\n"
            
            if current_batch_idx < total_batches:
                print("[!] 單發點射完畢，冷卻 15 秒以符合 API 速率監控...")
                time.sleep(15)
                
        with open("Mode_C_Final_Decision_Memo.md", "w", encoding="utf-8") as f:
            f.write(final_memo_report)
        print("[+] 最終 Mode_C_Final_Decision_Memo.md 報告已整合完畢。")
            
        send_final_decision_email(final_memo_report)
    else:
        print("[*] 今日初選底池為空，AI Agent 無法發動推理。")
