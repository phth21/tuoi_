import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient

# 🔥 SDK MỚI (GOOGLE GENAI v1.0+)
from google import genai
from google.genai import types

# ====================== 1. CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

# 🔒 KHÓA LUỒNG
ai_lock = threading.Lock()

# TÀI KHOẢN
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',      'role': 'VIEWER'}
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

# ====================== KHỞI TẠO AI (AUTO FALLBACK) ======================
ai_client = None

# [QUAN TRỌNG] Danh sách ưu tiên Model. 
# Nếu cái đầu lỗi 429, nó sẽ tự nhảy sang cái sau.
AI_MODELS_PRIORITY = ["gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"]

if GEMINI_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_KEY)
        print("✅ AI READY")
    except Exception as e:
        print(f"❌ Lỗi khởi tạo AI: {e}")

# ====================== BIẾN TOÀN CỤC ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 25  # Mức báo động khô cần tưới gấp

# Biến theo dõi logic bơm khẩn cấp 15s/60s
last_emergency_pump_time = 0 
EMERGENCY_COOLDOWN = 300  # 5 phút

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

# ====================== FLASK ROUTES ======================
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

# --- 🔥 HÀM HỎI AI THÔNG MINH (TỰ FIX LỖI 429) ---
def ask_gemini(force=False):
    if ai_lock.locked(): return 
    
    with ai_lock:
        if state['mode'] != 'AUTO': return
        if not ai_client: return

        now = time.time()
        elapsed = now - state['last_ai_call']
        is_emergency = state['soil'] < EMERGENCY_LEVEL
        
        # Nếu đang khẩn cấp, hỏi thường xuyên hơn
        cooldown_time = 30 if is_emergency else 120
        if not force and elapsed < cooldown_time: return

        # Prompt chuẩn theo yêu cầu
        prompt = f"""
        Role: Hệ thống tưới cây IoT.
        Input: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
        Trạng thái khẩn cấp: {"CÓ" if is_emergency else "KHÔNG"}.
        
        Output JSON Only:
        1. "action": "TƯỚI" hoặc "KHÔNG".
        2. "target": (int) Độ ẩm mục tiêu để dừng bơm (VD: 75).
        3. "timing": (string) Bao giờ tưới tiếp (VD: "1 giờ nữa", "KHẨN CẤP").
        4. "reason": (string) Lý do ngắn gọn.
        """

        # --- CƠ CHẾ TỰ ĐỘNG CHỌN MODEL (AUTO SWITCH) ---
        success = False
        for model_name in AI_MODELS_PRIORITY:
            if success: break
            try:
                print(f"\n--- 🤖 AI Trying: {model_name} ---")
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", 
                        temperature=0.4
                    )
                )

                if response and response.text:
                    data = json.loads(response.text)

                    action = data.get("action", "KHÔNG").upper()
                    target = int(data.get("target", 75))
                    timing = data.get("timing", "...")
                    reason = data.get("reason", "...")

                    state['ai_target'] = target; state['ai_timing'] = timing; state['ai_reason'] = reason
                    state['last_ai_call'] = time.time()

                    print(f"🎯 AI ({model_name}) → {action} | {reason}")
                    log_event(f"AI_{model_name}", f"{action} - {reason}")
                    
                    if not is_emergency: 
                        if action == "TƯỚI": control_pump(True, "AI Decision")
                        else: control_pump(False, "AI Decision")
                    
                    broadcast()
                    success = True # Đánh dấu đã thành công để thoát vòng lặp

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"⚠️ Model {model_name} hết quota. Đang chuyển model khác...")
                    continue # Thử model tiếp theo trong danh sách
                else:
                    print(f"❌ AI Error ({model_name}): {e}")
                    break # Lỗi khác thì dừng luôn

# ====================== ĐIỀU KHIỂN BƠM ======================
def control_pump(on, source="System"):
    if on and state['soil'] >= FLOOD_LEVEL:
        on = False; state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
    if state['step'] != 2 and on: on = False 
    
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 PUMP {cmd} ({source})")
    
    if not on and "NGẬP" in state['warning']: state['warning'] = ""
    broadcast()

def delayed_pump_off(duration):
    time.sleep(duration)
    if state['pump']: 
        print(f"⏳ Auto Stop sau {duration}s")
        control_pump(False, f"Auto Stop ({duration}s)")

# ====================== MQTT HANDLE ======================
def on_message(client, userdata, msg):
    global last_emergency_pump_time
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # --- 🔥 LOGIC KHẨN CẤP 15s/60s (Giữ nguyên) ---
                if state['soil'] < EMERGENCY_LEVEL:
                    state['warning'] = "🔥 KHẨN CẤP: ĐẤT QUÁ KHÔ!"
                    
                    if state['mode'] == 'AUTO' and not state['pump']:
                        current_ts = time.time()
                        
                        # Logic 60s nếu quá gần, 15s nếu bình thường
                        if (current_ts - last_emergency_pump_time) < EMERGENCY_COOLDOWN:
                            pump_duration = 60
                            log_msg = "Khẩn cấp dồn dập (60s)"
                        else:
                            pump_duration = 15
                            log_msg = "Khẩn cấp thường (15s)"
                        
                        last_emergency_pump_time = current_ts
                        control_pump(True, log_msg)
                        threading.Thread(target=delayed_pump_off, args=(pump_duration,), daemon=True).start()

                elif state['soil'] >= FLOOD_LEVEL:
                    state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
                else:
                    state['warning'] = "" 
                
                # --- LOGIC CẮT BƠM AN TOÀN ---
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                elif state['mode'] == 'AUTO':
                    if state['pump'] and state['soil'] >= state['ai_target']:
                        control_pump(False, f"Đạt mục tiêu {state['ai_target']}%")
                    
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                
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
                if state['mode'] == 'AUTO': 
                    threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
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
