# tc.py — SERVER TRUNG TÂM (FINAL VERSION)
import threading, time, json, os, requests 
import paho.mqtt.client as mqtt
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient
from google import genai
from google.genai import types

# --- CẤU HÌNH ---
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',     'role': 'VIEWER'}
}

# Lấy Key từ biến môi trường (Hoặc điền trực tiếp vào đây nếu test nhanh)
GEMINI_KEY = os.getenv("GEMINI_KEY") 
MONGO_URI = os.getenv("MONGO_URI")

# --- KẾT NỐI DATABASE (Nếu có) ---
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db_collection = mongo_client.get_database("smart_garden_db").history
        print("✅ MongoDB Connected")
    else: print("⚠️ Chưa có MONGO_URI (Chạy chế độ không lưu lịch sử)")
except: pass

# --- KẾT NỐI AI GEMINI ---
ai_client = None; CURRENT_MODEL = "gemini-1.5-flash"
if GEMINI_KEY: 
    ai_client = genai.Client(api_key=GEMINI_KEY)
    print("✅ AI Gemini Ready")
else: print("⚠️ Chưa có GEMINI_KEY")

# --- TRẠNG THÁI HỆ THỐNG ---
CRITICAL_LEVEL = 26; FLOOD_LEVEL = 90
BROKER = "broker.hivemq.com"; PREFIX = "thaocute_smartgarden/"
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 
    'location': "Đang định vị...", 'lat': 21.02, 'lon': 105.83,
    'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': 75, 'ai_reason': "Đang chờ dữ liệu...", 
    'pump': False, 'warning': "", 'last_ai_call': 0
}
mqtt_client = mqtt.Client(client_id=f"Srv_{int(time.time())}")

# --- LOGIC ĐỊNH VỊ TỰ ĐỘNG ---
def auto_locate():
    try:
        # Chỉ tự định vị nếu người dùng chưa chỉnh tay
        if "(Thủ công)" not in state['location']:
            print("📍 Đang dò tìm vị trí qua IP...")
            r = requests.get('http://ip-api.com/json', timeout=5)
            if r.status_code == 200:
                data = r.json()
                state['location'] = f"{data.get('city', 'Unknown')}, {data.get('countryCode', 'VN')}"
                state['lat'] = data.get('lat', 21.02)
                state['lon'] = data.get('lon', 105.83)
                print(f"✅ Đã định vị: {state['location']}")
                broadcast()
    except Exception as e: print(f"❌ Lỗi định vị: {e}")

# --- WEB SERVER (FLASK) ---
@app.route('/', methods=['GET', 'POST'])
def home():
    if 'user' not in session:
        if request.method == 'POST':
            u = request.form.get('username'); p = request.form.get('password')
            if u in USERS and USERS[u]['pass'] == p: 
                session['user'] = u; session['role'] = USERS[u]['role']
                return redirect('/')
            return render_template('login.html', error="Sai mật khẩu!")
        return render_template('login.html')
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/api/history')
def get_history():
    if db_collection is None: return jsonify([])
    try:
        logs = list(db_collection.find({}, {'_id': 0}).sort("created_at", -1).limit(20))
        return jsonify(logs)
    except: return jsonify([])

# --- HÀM HỖ TRỢ ---
def log_event(action, detail):
    if db_collection is None: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        db_collection.insert_one({
            "date": now_vn.strftime("%Y-%m-%d"), "time": now_vn.strftime("%H:%M:%S"),
            "action": action, "detail": detail, "soil": state['soil'], "created_at": now_vn
        })
    except: pass

def broadcast():
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

# --- LOGIC AI ---
def ask_gemini(force=False):
    if state['mode'] != 'AUTO' or not ai_client: return
    
    now = time.time(); is_emergency = state['soil'] < CRITICAL_LEVEL
    # Logic: Chỉ gọi khi (Người dùng ép buộc) HOẶC (Khẩn cấp) HOẶC (Đã quá 2 phút từ lần gọi trước)
    if not force and not is_emergency and (now - state['last_ai_call'] < 120): return

    print("🤖 Đang gọi Gemini...")
    prompt = f"""Bạn là Kỹ sư nông nghiệp AI. 
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Vùng {state['region']}.
    Khẩn cấp (Đất < {CRITICAL_LEVEL}%): {is_emergency}.
    Yêu cầu: Trả về JSON (không markdown).
    Format: {{ "decision": "ON" hoặc "OFF", "timing": "bao lâu", "target": số_nguyên_độ_ẩm_mục_tiêu, "reason": "lý do ngắn gọn" }}"""
    
    try:
        res = ai_client.models.generate_content(
            model=CURRENT_MODEL, contents=prompt, 
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(res.text.replace("```json","").replace("```","").strip())
        
        decision = data.get("decision", "OFF").upper()
        state['ai_target'] = int(data.get("target", 75))
        state['ai_reason'] = data.get("reason", "...")
        state['last_ai_call'] = now
        
        print(f"🤖 AI Quyết định: {decision} (Mục tiêu: {state['ai_target']}%)")
        control_pump(decision == "ON", "AI Gemini")
    except Exception as e: print(f"❌ AI Lỗi: {e}")

# --- ĐIỀU KHIỂN BƠM ---
def control_pump(on, source):
    # LỚP BẢO VỆ 1: CHỐNG NGẬP
    if state['soil'] >= FLOOD_LEVEL and on: 
        on = False; state['warning'] = "NGẬP ÚNG! NGẮT BƠM"
    else:
        state['warning'] = ""

    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💧 Bơm {cmd} (Nguồn: {source})")
    
    broadcast()

# --- XỬ LÝ MQTT ---
# Trong file tc.py
def on_message(c, u, msg):
    try:
        payload = msg.payload.decode()
        
        # 1. NHẬN DỮ LIỆU CẢM BIẾN
        if "esp/data" in msg.topic and "H:" in payload:
            parts = payload.split()
            for p in parts:
                if "H:" in p: state['soil'] = int(p.replace("H:",""))
                if "T:" in p: state['temp'] = float(p.replace("T:",""))
            
            if state['mode'] == 'AUTO':
                if state['soil'] < CRITICAL_LEVEL: 
                    threading.Thread(target=ask_gemini, kwargs={'force':False}).start()
                if state['pump'] and state['soil'] >= (state['ai_target'] + 3): 
                    control_pump(False, "Auto Target Reached")
            broadcast()

        # 2. NHẬN SỰ KIỆN TỪ WEB
        elif "events" in msg.topic:
            d = json.loads(payload); evt = d.get('event')
            
            if evt == 'enter_mode': 
                state['mode'] = d['data']['mode']; state['step'] = 2
                if state['mode'] == 'AUTO': 
                    threading.Thread(target=ask_gemini, kwargs={'force':True}).start()
                broadcast() # <--- QUAN TRỌNG: Gửi xác nhận để Web vào màn hình chính
            
            elif evt == 'user_control': 
                control_pump(bool(d['data']['pump']), "User Remote")
            
            elif evt == 'get_status': 
                broadcast()
            
            elif evt == 'force_locate': 
                threading.Thread(target=auto_locate).start()
                
            elif evt == 'select_region': 
                state['region'] = d['data']['region']; state['step'] = 1
                broadcast() # <--- QUAN TRỌNG: Gửi xác nhận để Web sang màn hình chọn Mode
                
            elif evt == 'set_city': 
                state['location'] = d['data']['city'] + " (Thủ công)"
                broadcast()
            
            elif evt == 'exit_dashboard':
                state['step'] = 0; state['mode'] = 'NONE'
                broadcast() # <--- QUAN TRỌNG: Để Web quay về màn hình đầu

    except Exception as e: print(f"MQTT Error: {e}")

if __name__ == '__main__':
    # Setup MQTT
    mqtt_client.on_message = on_message
    mqtt_client.on_connect = lambda c,u,f,rc: c.subscribe([(PREFIX+"esp/data",0), (PREFIX+"events",0)])
    mqtt_client.connect(BROKER, 1883, 60)
    mqtt_client.loop_start()
    
    # Chạy server
    print("🚀 Server đang chạy tại http://localhost:5000")
    # Tự động định vị lần đầu khi server bật
    threading.Thread(target=auto_locate).start()
    app.run(host='0.0.0.0', port=5000, use_reloader=False)

