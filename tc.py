import threading
import paho.mqtt.client as mqtt
import requests, time, json, re, os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
import google.generativeai as genai

# ====================== CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro' # <--- Bắt buộc 

# TÀI KHOẢN (Bạn có thể sửa pass ở đây)
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},  # Chủ vườn
    'khach': {'pass': '1111',      'role': 'VIEWER'} # Khách xem
}

# CONFIG BACKEND
GEMINI_API_KEY = os.getenv("GEMINI_KEY", "AIzaSyDnmQNHRgXXPgl-ZhK-Et8EiAW9MjTh-5s").strip()
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

genai.configure(api_key=GEMINI_API_KEY)
try:
    # Dùng bản 2.5 Flash: Nhanh, nhẹ, phù hợp IoT
    model = genai.GenerativeModel('gemini-2.5-flash')
    print("--- AI GEMINI 2.5 FLASH READY ---")
except Exception as e:
    model = None
    print(f"Lỗi khởi tạo AI: {e}")

# BIẾN TOÀN CỤC
CRITICAL_LEVEL = 26 
FLOOD_LEVEL = 90
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68)}, # (Rút gọn cho ngắn)
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang định vị...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_reason': "...", 'pump': False, 'warning': "", 'last_ai_call': 0
}
mqtt_client = mqtt.Client()

# ====================== ROUTE WEB (Flask) ======================

@app.route('/', methods=['GET', 'POST'])
def home():
    # 1. Nếu chưa đăng nhập -> Trả về Login HTML
    if 'user' not in session:
        error = None
        if request.method == 'POST':
            u = request.form.get('username')
            p = request.form.get('password')
            if u in USERS and USERS[u]['pass'] == p:
                session['user'] = u
                session['role'] = USERS[u]['role']
                return redirect('/')
            else:
                error = "Sai tên hoặc mật khẩu!"
        return render_template('login.html', error=error)
    
    # 2. Nếu đã đăng nhập -> Trả về Dashboard HTML kèm Role
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/api/history')
def get_history():
    date_str = request.args.get('date')
    if db_collection is None: return jsonify([]) # Đã fix lỗi is None
    logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
    return jsonify(logs)

# ====================== LOGIC MQTT & AI ======================

def log_event(action, detail):
    if db_collection is None: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        record = {
            "date": now_vn.strftime("%Y-%m-%d"),
            "time": now_vn.strftime("%H:%M:%S"),
            "action": action, "detail": detail, "soil": state['soil'],
            "created_at": now_vn
        }
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
            if "(Thủ công)" not in state['location']: state['location'] = f"{r.get('name')} (GPS)"
            if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

def ask_gemini(force=False):
    if state['mode'] != 'AUTO' or not model: return 
    now = time.time()
    is_emergency = state['soil'] < CRITICAL_LEVEL
    if not force:
        if is_emergency and (now - state['last_ai_call'] < 15): return
        if not is_emergency and (now - state['last_ai_call'] < 120): return

    state['warning'] = "KHẨN CẤP: ĐẤT QUÁ KHÔ!" if is_emergency else ("CẢNH BÁO: NGẬP!" if state['soil'] >= FLOOD_LEVEL else "")
    broadcast()

    prompt = f"Đóng vai kỹ sư nông nghiệp. Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm. Trả về JSON: {{ 'decision': 'ON/OFF', 'timing': '...', 'reason': '...' }}"
    try:
        res = model.generate_content(prompt)
        match = re.search(r'\{.*\}', res.text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            dec = data.get('decision', 'OFF').upper()
            state['ai_timing'] = data.get('timing', '...'); state['ai_reason'] = data.get('reason', '...')
            state['last_ai_call'] = now 
            log_event("AI_AUTO", f"Quyết định: {dec}. {state['ai_reason']}")
            control_pump(dec == 'ON', "AI Logic")
    except: pass
    broadcast()

def control_pump(on, source="System"):
    if state['step'] != 2 and on: on = False
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
    broadcast()

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            val = int(payload.split("H:")[1].split()[0])
            state['soil'] = max(0, min(100, val))
            if state['mode'] == 'AUTO' and state['soil'] < CRITICAL_LEVEL: 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
            broadcast()
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            if evt == 'select_region':
                state['region'] = data['region']; state['step'] = 1
            elif evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển chế độ {state['mode']}")
                if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
            elif evt == 'exit_dashboard':
                state['step'] = 1; state['mode'] = 'NONE'; control_pump(False)
            elif evt == 'set_city':
                city = data.get('city')
                if city in ALL_CITIES:
                    state['lat'], state['lon'] = ALL_CITIES[city]
                    state['location'] = f"{city} (Thủ công)"
                    threading.Thread(target=update_weather, daemon=True).start()
            elif evt == 'user_control' and state['mode'] == 'MANUAL':
                control_pump(bool(data['pump']), "Người dùng bấm")
            broadcast()
    except: pass

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe([ (PREFIX+"esp/data",0), (PREFIX+"events",0) ]), print("MQTT READY"))
    mqtt_client.on_message = on_message
    try: mqtt_client.connect(BROKER, 1883, 60); mqtt_client.loop_forever()
    except: print("Lỗi MQTT")

# --- QUAN TRỌNG: CHẠY MQTT RIÊNG RA KHỎI APP.RUN ---
threading.Thread(target=run_mqtt, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
