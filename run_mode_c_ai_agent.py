import os
import json
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
    client = genai.Client()
    
    # 【致命錯誤已在此行修正】：必須是 ensure_ascii=False
    tasks_str = json.dumps(payload_data.get("tasks", []), indent=2, ensure_ascii=False)
    
    prompt = f"""
    你現在是華爾街頂級買方資深 PM，嚴格執行【模式C：三階段雙殺模型】。
    
    以下是 Quant Engine 剛產出的初步數據與需要你聯網核對的任務清單(Payload)：
    {tasks_str}
    
    請啟動 Google Search 聯網工具，針對清單中的每檔標的，執行 Layer 3 聯網審查與 Layer 4 買方決策：
    1. 根據 'must_verify' 欄位，核對該公司過去 30 天內最新 10-K/10-Q 的 footnotes，抓出 non-recurring / restructuring 等 EBITDA 隱蔽調整項，挑戰 'numbers_to_challenge' 中的數據是否乾淨。
    2. 如果標的涉及半導體或 AI 供應鏈，強制對齊最新台積電(TSMC)先進製程產能利用率、ASML EUV 交期以及四大 CSP 的最新 CapEx 指引，驗證其隱含 CAGR 是否撞上物理產能硬限制。
    3. 交叉比對最新官方短倉數據，覆核 Short Interest > 15% 與 Days to Cover > 5 天的軋空禁制。
    
    輸出要求：
    - 直接破題，拒絕客套廢話與機器翻譯感，使用精準華爾街買方繁體中文。
    - 逐檔給出最終操盤論點，明確將標的分流為【實質防禦】、【價值陷阱】或【博弈泡沫】。
    - 每項結論必須附帶聯網查證到的實證數據或具體事件。
    """
    
    print("[+] 正在啟動 Gemini 3.5 Web Evidence & PM Decision Agent (2026 Live Mode)...")
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1 
        )
    )
    return response.text

def send_final_decision_email(final_memo):
    user_email = os.environ.get("USER_EMAIL")
    sender_email = os.environ.get("EMAIL_SENDER")
    sender_pwd = os.environ.get("EMAIL_PASSWORD")
    
    if not all([user_email, sender_email, sender_pwd]):
        print("[-] 郵件環境變數不完整，略過最終報告寄送。")
        return
        
    msg = EmailMessage()
    msg["Subject"] = f"【Mode C 買方實戰決策書】AI Agent 聯網審查日報 - {datetime.now().strftime('%Y-%m-%d')}"
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
        final_memo_report = execute_pm_agent_reasoning(payload)
        
        with open("Mode_C_Final_Decision_Memo.md", "w", encoding="utf-8") as f:
            f.write(final_memo_report)
            
        send_final_decision_email(final_memo_report)
    else:
        print("[*] 今日無標的通過初選，或初選池為空，無需執行 AI 審查。")
