import threading
import paho.mqtt.client as mqtt
import requests, time, json, re, os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
import google.generativeai as genai

# ====================== CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro' 

# TÀI KHOẢN
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},  
    'khach': {'pass': '1111',       'role': 'VIEWER'} 
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
    else:
        print("⚠️ Cảnh báo: Chưa có MONGO_URI, lịch sử sẽ không được lưu.")
except Exception as e: print(f"❌ Lỗi MongoDB: {e}")

# AI CONNECT
genai.configure(api_key=GEMINI_API_KEY)
try:
    model = genai.GenerativeModel('gemini-1.5-flash')
    print("--- AI GEMINI READY ---")
except Exception as e:
    model = None
    print(f"Lỗi khởi tạo AI: {e}")

# BIẾN TOÀN CỤC
CRITICAL_LEVEL = 26 
FLOOD_LEVEL = 90
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Cà Mau":(9.17,105.15)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

# State khởi tạo (Thêm last_ai_call để logic cooldown hoạt động)
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': "...", 'ai_reason': "...",
    'pump': False, 'warning': "", 'last_ai_call': 0
}

mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

# ====================== ROUTE WEB (Flask) ======================

@app.route('/', methods=['GET', 'POST'])
def home():
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
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/api/history')
def get_history():
    date_str = request.args.get('date')
    if db_collection is None: return jsonify([])
    logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
    return jsonify(logs)

# ====================== LOGIC HỆ THỐNG ======================

def log_event(action, detail):
    """Ghi log vào MongoDB"""
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
    """Gửi cập nhật xuống Web"""
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

def update_weather():
    """Gọi API OpenWeatherMap"""
    if not state['lat']: return
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={state['lat']}&lon={state['lon']}&units=metric&appid={OPENWEATHER_KEY}"
        r = requests.get(url, timeout=3).json()
        if r.get('cod') == 200:
            state['temp'] = r['main']['temp']; state['humidity'] = r['main']['humidity']
            state['rain'] = r.get('rain', {}).get('1h', 0.0)
            
            # Cập nhật tên địa điểm
            if "Thủ công" not in state['location']: 
                state['location'] = r.get('name') + " (VN)"
            
            if state['mode'] == 'AUTO': 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

# --- LOGIC AI MỚI (CHÍNH XÁC NHƯ BẠN YÊU CẦU) ---
def ask_gemini(force=False):
    if state['mode'] != 'AUTO': return 
    
    # Check nhanh ngập úng (Ưu tiên cao nhất - Không cần AI)
    if state['soil'] >= FLOOD_LEVEL:
        control_pump(False, "Safety Check")
        return

    if not model: 
        print("Lỗi: Model AI chưa sẵn sàng")
        return

    now = time.time()
    is_emergency = state['soil'] < CRITICAL_LEVEL
    
    # === LOGIC COOLDOWN (QUAN TRỌNG) ===
    time_diff = now - state['last_ai_call']
    
    if force:
        pass # Cho qua
    elif is_emergency:
        if time_diff < 15: return # Chống spam API khi khẩn cấp
    else:
        if time_diff < 120: return # Chờ 2 phút

    # Cập nhật cảnh báo hiển thị
    if is_emergency: state['warning'] = "KHẨN CẤP: ĐẤT QUÁ KHÔ!"
    elif state['soil'] >= FLOOD_LEVEL: state['warning'] = f"CẢNH BÁO: NGẬP ÚNG (>{FLOOD_LEVEL}%)!"
    else: state['warning'] = "" 

    broadcast()

    # Prompt ngắn gọn, tiết kiệm Token
    prompt = f"""
    Đóng vai kỹ sư nông nghiệp.
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
    Khẩn cấp (<26%): {is_emergency}.
    
    Yêu cầu trả về đúng định dạng JSON: 
    {{ "decision": "ON hoặc OFF", "timing": "...", "target": "XX%", "reason": "..." }}
    
    Lưu ý:
    - "target": Độ ẩm mục tiêu để dừng bơm (bạn phải tự dự đoán).
    - "timing": Mô tả ngắn gọn bao giờ tưới(bắt buộc phải có thời gian nhất định) và độ ẩm dự đoán là bao nhiêu.
    - "reason": Lý do ngắn gọn giải thích tại sao tưới đến độ ẩm đấy.
    """
    try:
        print(f"📡 Đang gọi Gemini... (Soil: {state['soil']}%)")
        res = model.generate_content(prompt)
        match = re.search(r'\{.*\}', res.text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            dec = data.get('decision', 'OFF').upper()
            state['ai_timing'] = data.get('timing', '...')
            state['ai_target'] = data.get('target', '...')
            state['ai_reason'] = data.get('reason', '...')
            
            # Cập nhật thời gian gọi thành công
            state['last_ai_call'] = now 
            
            log_event("AI_DECISION", f"AI: {dec} ({state['ai_reason']})")
            control_pump(dec == 'ON', "AI Logic")
            print(f"✅ AI Done: {dec} | {state['ai_reason']}")
    except Exception as e:
        print(f"❌ AI Error: {e}")
    broadcast()

def control_pump(on, source="System"):
    if on and state['soil'] >= FLOOD_LEVEL:
        on = False
        state['warning'] = "NGẬP ÚNG! TỪ CHỐI BƠM"

    if state['step'] != 2 and on: on = False 
    
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 PUMP {cmd} ({source})")
    
    if not on and state['warning'] == "NGẬP ÚNG! TỪ CHỐI BƠM": state['warning'] = ""
    broadcast()

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU TỪ ESP ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                elif state['mode'] == 'AUTO':
                    if state['soil'] < CRITICAL_LEVEL: 
                        threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                    
                    if state['pump']:
                        nums = re.findall(r'\d+', str(state['ai_target']))
                        if nums:
                            target_val = int(nums[0])
                            if state['soil'] >= (target_val + 3):
                                control_pump(False, "AI Target Reached")
                broadcast()
            except: pass

        # --- 2. NHẬN SỰ KIỆN TỪ WEB ---
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            
            if evt == 'select_region':
                state['region'] = data['region']
                state['step'] = 1
                broadcast()
                
            elif evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển chế độ {state['mode']}")
                if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                broadcast()

            elif evt == 'exit_dashboard':
                state['step'] = 1; state['mode'] = 'NONE'; control_pump(False)
            
            elif evt == 'set_city':
                city = data.get('city')
                if city in ALL_CITIES:
                    state['lat'], state['lon'] = ALL_CITIES[city]
                    state['location'] = f"{city} (Thủ công)"
                    threading.Thread(target=update_weather, daemon=True).start()

            elif evt == 'set_gps':
                state['lat'] = data['lat']
                state['lon'] = data['lon']
                state['location'] = "📍 Đang lấy tên..."
                broadcast()
                print(f"🌍 Nhận GPS: {state['lat']}, {state['lon']}")
                threading.Thread(target=update_weather, daemon=True).start()
            
            elif evt == 'user_control' and state['mode'] == 'MANUAL':
                control_pump(bool(data['pump']), "Người dùng bấm")
            broadcast()
    except: pass

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe([ (PREFIX+"esp/data",0), (PREFIX+"events",0) ]), print("✅ MQTT CONNECTED"))
    mqtt_client.on_message = on_message
    try: 
        mqtt_client.connect(BROKER, 1883, 60)
        mqtt_client.loop_start() # Dùng loop_start cho Render
    except Exception as e: print(f"❌ Lỗi MQTT: {e}")

# --- KHỞI ĐỘNG MQTT Ở GLOBAL SCOPE CHO RENDER ---
try:
    run_mqtt()
    print("--- Background Thread Started ---")
except: pass

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
