import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient

# 🔥 SDK MỚI (GOOGLE GENAI v1.0+)
from google import genai
from google.genai import types

# ====================== CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

# TÀI KHOẢN
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',     'role': 'VIEWER'}
}

# CONFIG BACKEND
GEMINI_KEY = os.getenv("GEMINI_KEY")
if not GEMINI_KEY:
    print("⚠️ CẢNH BÁO: Chưa set GEMINI_KEY! AI sẽ không chạy.")

OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232")
MONGO_URI = os.getenv("MONGO_URI")

# MONGODB CONNECT
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_database("smart_garden_db")
        db_collection = db.history
        print("--- MONGODB ATLAS CONNECTED ---")
    else:
        print("⚠️ Cảnh báo: Chưa có MONGO_URI")
except Exception as e: print(f"❌ Lỗi MongoDB: {e}")

# ====================== AI AUTO-DISCOVERY (SDK MỚI) ======================
ai_client = None
CURRENT_MODEL = "gemini-1.5-flash"

if GEMINI_KEY:
    ai_client = genai.Client(api_key=GEMINI_KEY)

def find_working_model():
    """
    Logic 'Thử Sai' của bạn, nhưng viết lại cho SDK mới.
    Nó sẽ thử từng model, cái nào chạy được thì chốt.
    """
    global CURRENT_MODEL
    print("\n🔍 Đang dò tìm model AI (SDK Mới)...")
    
    candidates = [
        "gemini-2.0-flash-exp",     # Bản 2.0 mới nhất (rất nhanh)
        "gemini-1.5-flash",         # Bản ổn định
        "gemini-1.5-pro",           # Bản thông minh
        "gemini-1.5-flash-8b"       # Bản siêu nhẹ
    ]
    
    for name in candidates:
        try:
            print(f"   👉 Đang thử: {name}...", end=" ")
            # Gọi thử một lệnh test
            ai_client.models.generate_content(
                model=name, contents="Test", 
                config=types.GenerateContentConfig(max_output_tokens=5)
            )
            print("✅ OK!")
            CURRENT_MODEL = name
            return True
        except Exception as e:
            print(f"❌ Lỗi ({e})")
            continue
            
    print("⚠️ Tất cả model đều lỗi. Giữ nguyên model cũ.")
    return False

# Chạy dò model lần đầu
if ai_client: find_working_model()

# ====================== BIẾN TOÀN CỤC ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 25 

REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Cà Mau":(9.17,105.15)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': 100, 'ai_reason': "...", 
    'pump': False, 'warning': "", 'last_ai_call': 0
}

mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

# ====================== FLASK ROUTES (GIỮ NGUYÊN) ======================
@app.route('/', methods=['GET', 'POST'])
def home():
    if 'user' not in session:
        error = None
        if request.method == 'POST':
            u = request.form.get('username'); p = request.form.get('password')
            if u in USERS and USERS[u]['pass'] == p:
                session['user'] = u; session['role'] = USERS[u]['role']
                return redirect('/')
            else: error = "Sai tên hoặc mật khẩu!"
        return render_template('login.html', error=error)
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/api/history')
def get_history():
    date_str = request.args.get('date')
    if db_collection is None: return jsonify([])
    try:
        logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
        return jsonify(logs)
    except: return jsonify([])

# ====================== LOGIC HỆ THỐNG ======================
def log_event(action, detail):
    if db_collection is None: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        record = {"date": now_vn.strftime("%Y-%m-%d"), "time": now_vn.strftime("%H:%M:%S"),
                  "action": action, "detail": detail, "soil": state['soil'], "created_at": now_vn}
        db_collection.insert_one(record)
    except: pass

def broadcast():
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

def update_weather():
    if not state['lat']: return
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={state['lat']}&lon={state['lon']}&units=metric&appid={OPENWEATHER_KEY}"
        r = requests.get(url, timeout=3).json()
        if r.get('cod') == 200:
            state['temp'] = r['main']['temp']; state['humidity'] = r['main']['humidity']
            state['rain'] = r.get('rain', {}).get('1h', 0.0)
            if "Thủ công" not in state['location']: state['location'] = r.get('name') + " (VN)"
            if state['mode'] == 'AUTO': 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

# --- 🔥 HÀM HỎI AI (GIỮ NGUYÊN LOGIC CỦA BẠN - DÙNG SDK MỚI) ---
def ask_gemini(force=False):
    global CURRENT_MODEL
    
    # 1. Logic Cooldown (120s / 30s)
    is_emergency = state['soil'] < EMERGENCY_LEVEL
    cooldown_time = 30 if is_emergency else 120
    elapsed = time.time() - state['last_ai_call']
    
    if state['mode'] != 'AUTO': return
    if not force and elapsed < cooldown_time:
        if is_emergency: print(f"⏳ Đất khô ({state['soil']}%) - Chờ {cooldown_time}s...")
        return

    if not ai_client: return

    print(f"\n--- 🤖 AI CHECK ({CURRENT_MODEL}) | Soil={state['soil']}% ---")

    # 2. Prompt
    urgent_note = "CẢNH BÁO: ĐẤT RẤT KHÔ! ƯU TIÊN TƯỚI NGAY!" if is_emergency else ""
    prompt = f"""
    Độ ẩm đất: {state['soil']}%. Nhiệt độ: {state['temp']}C. Mưa 1h: {state['rain']}mm.
    {urgent_note}
    Bạn là hệ thống tưới cây. Trả lời DUY NHẤT JSON (không markdown):
    {{
      "action": "TƯỚI" hoặc "KHÔNG",
      "target": số_nguyên (ví dụ 75),
      "timing": "...",
      "reason": "..."
    }}
    """

    try:
        # --- LOGIC RETRY (THỬ LẠI NẾU LỖI) ---
        response = None
        try:
            # SDK mới: Ép kiểu JSON ngay tại config -> Không cần regex lọc nữa
            response = ai_client.models.generate_content(
                model=CURRENT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    temperature=0.5
                )
            )
        except Exception as e:
            print(f"⚠️ Model {CURRENT_MODEL} lỗi ({e}). Đang tìm model khác...")
            # Nếu lỗi -> Gọi hàm dò tìm lại ngay lập tức
            if find_working_model():
                # Thử lại lần 2 với model mới tìm được
                response = ai_client.models.generate_content(
                    model=CURRENT_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
            else:
                return # Chịu thua

        # 3. Xử lý kết quả (JSON chuẩn)
        if response and response.text:
            data = json.loads(response.text)

            action = data.get("action", "KHÔNG")
            target = int(data.get("target", 80)) # Default 80
            timing = data.get("timing", "...")
            reason = data.get("reason", "...")

            state['ai_target'] = target; state['ai_timing'] = timing; state['ai_reason'] = reason
            state['last_ai_call'] = time.time()

            print(f"🎯 AI → {action} | Target={target}% | {timing}")

            if action == "TƯỚI": control_pump(True, "AI Decision")
            else: control_pump(False, "AI Decision")
            
            broadcast()

    except Exception as e:
        print(f"❌ AI FATAL ERROR: {e}")

# ====================== ĐIỀU KHIỂN BƠM ======================
def control_pump(on, source="System"):
    # Safety Check (Ngập úng)
    if on and state['soil'] >= FLOOD_LEVEL:
        on = False; state['warning'] = "NGẬP ÚNG! TỪ CHỐI BƠM"
    if state['step'] != 2 and on: on = False 
    
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 PUMP {cmd} ({source})")
    
    if not on and state['warning'] == "NGẬP ÚNG! TỪ CHỐI BƠM": state['warning'] = ""
    broadcast()

# ====================== MQTT HANDLE (GIỮ NGUYÊN) ======================
def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # A. AN TOÀN (Ngập là cắt)
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                # B. LOGIC AUTO
                elif state['mode'] == 'AUTO':
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                    
                    # 🔴 LOGIC TỰ NGẮT THEO TARGET (Của bạn)
                    if state['pump']:
                        try:
                            target_val = int(state['ai_target'])
                            if state['soil'] >= target_val:
                                control_pump(False, f"Đạt mục tiêu {target_val}%")
                        except: pass
                broadcast() 
            except: pass

        # --- 2. NHẬN SỰ KIỆN TỪ WEB ---
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            
            if evt == 'select_region':
                state['region'] = data['region']; state['step'] = 1; broadcast()
            elif evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển chế độ {state['mode']}")
                if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                broadcast()
            elif evt == 'exit_dashboard':
                state['step'] = 0; state['mode'] = 'NONE'; control_pump(False)
            elif evt == 'set_city':
                city = data.get('city')
                if city in ALL_CITIES:
                    state['lat'], state['lon'] = ALL_CITIES[city]
                    state['location'] = f"{city} (Thủ công)"
                    threading.Thread(target=update_weather, daemon=True).start()
            elif evt == 'set_gps':
                state['lat'] = data['lat']; state['lon'] = data['lon']
                state['location'] = "📍 Đang lấy tên..."; broadcast()
                threading.Thread(target=update_weather, daemon=True).start()
            elif evt == 'user_control' and state['mode'] == 'MANUAL':
                control_pump(bool(data['pump']), "Người dùng bấm")
            broadcast()
    except Exception as e: print(f"❌ Lỗi on_message: {e}")

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe([ (PREFIX+"esp/data",0), (PREFIX+"events",0) ]), print("✅ MQTT CONNECTED"))
    mqtt_client.on_message = on_message
    try: mqtt_client.connect(BROKER, 1883, 60); mqtt_client.loop_start()
    except Exception as e: print(f"❌ Lỗi MQTT: {e}")

try: run_mqtt(); print("--- Background Thread Started ---")
except: pass

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
