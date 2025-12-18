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
except Exception as e: print(f"❌ Lỗi MongoDB: {e}")

# ====================== KHỞI TẠO AI ======================
ai_client = None
AI_MODELS_PRIORITY = ["gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"]

if GEMINI_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_KEY)
        print("✅ AI READY")
    except Exception as e: print(f"❌ Lỗi khởi tạo AI: {e}")

# ====================== BIẾN TOÀN CỤC ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 25 
EMERGENCY_COOLDOWN = 300
last_emergency_pump_time = 0 

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
    'pump': False, 'warning': "", 'last_ai_call': 0,
    'ai_initialized': False  # 🔥 Cờ mới: Kiểm tra AI đã dự đoán lần đầu chưa
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
            # Chỉ gọi AI ở đây nếu AI đã khởi tạo rồi (để cập nhật định kỳ), còn lần đầu thì gọi ở enter_mode
            if state['mode'] == 'AUTO' and state['ai_initialized']: 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

# --- HÀM HỎI AI ---
def ask_gemini(force=False):
    if ai_lock.locked(): return 
    
    with ai_lock:
        if state['mode'] != 'AUTO': return
        if not ai_client: return

        # Nếu chưa initialize (lần đầu vào Auto), bỏ qua check thời gian -> Chạy luôn
        if state['ai_initialized']:
            now = time.time()
            elapsed = now - state['last_ai_call']
            is_emergency = state['soil'] < EMERGENCY_LEVEL
            cooldown_time = 30 if is_emergency else 120
            if not force and elapsed < cooldown_time: return

        prompt = f"""
        Role: Hệ thống tưới cây IoT.
        Input: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
        Trạng thái khẩn cấp: {"CÓ" if state['soil'] < EMERGENCY_LEVEL else "KHÔNG"}.
        
        Output JSON Only:
        1. "action": "TƯỚI" hoặc "KHÔNG".
        2. "target": (int) Độ ẩm mục tiêu để dừng bơm (VD: 75).
        3. "timing": (string) Bao giờ tưới tiếp (VD: "1 giờ nữa", "KHẨN CẤP").
        4. "reason": (string) Lý do ngắn gọn.
        """

        success = False
        for model_name in AI_MODELS_PRIORITY:
            if success: break
            try:
                print(f"\n--- 🤖 AI Computing ({model_name})... ---")
                response = ai_client.models.generate_content(
                    model=model_name, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4)
                )

                if response and response.text:
                    data = json.loads(response.text)
                    action = data.get("action", "KHÔNG").upper()
                    target = int(data.get("target", 75))
                    timing = data.get("timing", "...")
                    reason = data.get("reason", "...")

                    state['ai_target'] = target; state['ai_timing'] = timing; state['ai_reason'] = reason
                    state['last_ai_call'] = time.time()
                    
                    # 🔥 QUAN TRỌNG: Đánh dấu AI đã hoạt động -> Cho phép hệ thống chạy tiếp
                    state['ai_initialized'] = True 

                    print(f"🎯 AI Result: {action} | {reason}")
                    log_event(f"AI_{model_name}", f"{action} - {reason}")
                    
                    # AI ra lệnh thì thực thi ngay
                    if action == "TƯỚI": control_pump(True, "AI Start")
                    else: control_pump(False, "AI Stop")
                    
                    broadcast()
                    success = True
            except Exception as e:
                if "429" in str(e): continue
                else: break

# ====================== ĐIỀU KHIỂN BƠM ======================
def control_pump(on, source="System"):
    # An toàn tuyệt đối: Đất quá ẩm thì cấm bơm (trừ khi đang tắt bơm)
    if on and state['soil'] >= FLOOD_LEVEL:
        on = False; state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
    
    # Nếu đang không ở Dashboard (step != 2) thì cấm bật
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

                # 🔥 LOGIC MỚI: NẾU AUTO MÀ AI CHƯA CHẠY -> IM LẶNG TUYỆT ĐỐI
                if state['mode'] == 'AUTO' and not state['ai_initialized']:
                    # Không làm gì cả, chờ AI ở thread khác quyết định
                    # Nhưng nếu quá lâu AI chưa chạy (do mạng lag), ta vẫn có thể trigger lại ở đây
                    broadcast()
                    return 

                # --- Logic bên dưới chỉ chạy khi: Mode là MANUAL hoặc (AUTO đã có AI_Initialized) ---
                
                # Logic tưới khẩn cấp (Chỉ chạy khi AI đã Initialized hoặc Manual)
                if state['soil'] < EMERGENCY_LEVEL:
                    state['warning'] = "🔥 KHẨN CẤP: ĐẤT QUÁ KHÔ!"
                    # Chỉ tự tưới khẩn cấp nếu đang AUTO (và AI đã OK)
                    if state['mode'] == 'AUTO' and not state['pump']:
                        current_ts = time.time()
                        pump_duration = 60 if (current_ts - last_emergency_pump_time) < EMERGENCY_COOLDOWN else 15
                        last_emergency_pump_time = current_ts
                        control_pump(True, "Emergency Pump")
                        threading.Thread(target=delayed_pump_off, args=(pump_duration,), daemon=True).start()

                elif state['soil'] >= FLOOD_LEVEL:
                    state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
                    if state['pump']: control_pump(False, "Flood Safety")
                else:
                    state['warning'] = ""
                    # Logic cắt bơm thông thường khi đạt target AI
                    if state['mode'] == 'AUTO' and state['pump'] and state['soil'] >= state['ai_target']:
                        control_pump(False, f"Target {state['ai_target']}% OK")
                    
                    # Trigger định kỳ
                    if state['mode'] == 'AUTO':
                        threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                
                broadcast()
            except: pass

        # --- 2. NHẬN SỰ KIỆN TỪ WEB ---
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            
            if evt == 'get_status':
                broadcast()
            elif evt == 'select_region':
                state['region'] = data['region']; state['step'] = 1; broadcast()
            
            elif evt == 'enter_mode':
                # 🔥 QUAN TRỌNG: Tắt bơm ngay lập tức khi đổi chế độ
                control_pump(False, "Mode Switch")
                
                state['mode'] = data['mode']; state['step'] = 2
                
                if state['mode'] == 'AUTO':
                    state['ai_initialized'] = False # Reset cờ: Bắt buộc chờ AI
                    state['ai_reason'] = "Đang kết nối vệ tinh AI..."
                    state['ai_timing'] = "Vui lòng đợi..."
                    # Gọi AI ngay lập tức
                    threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                
                log_event("MODE_CHANGE", f"Vào {state['mode']}")
                broadcast()
                
            elif evt == 'exit_dashboard':
                # 🔥 QUAN TRỌNG: Thoát ra là tắt bơm
                control_pump(False, "User Exit")
                state['step'] = 0; state['mode'] = 'NONE'
                broadcast()
                
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
                control_pump(bool(data['pump']), "User Click")
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
