import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests 
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient

# ====================== 1. CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

# 🔒 KHÓA LUỒNG
ai_lock = threading.Lock()

# TÀI KHOẢN
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',       'role': 'VIEWER'}
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

# ====================== BIẾN TOÀN CỤC ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 20 
EMERGENCY_COOLDOWN = 300
last_emergency_pump_time = 0 

# CONFIG MODEL AI
AI_MODELS_PRIORITY = [
    "gemini-2.5-flash",            # Stable, rất nhanh & rẻ - lựa chọn lý tưởng thứ 2
    "gemini-2.5-flash-lite",       # Siêu rẻ & nhanh cho high-frequency (nếu bạn gọi AI thường xuyên)
    "gemini-2.5-pro",              # Dự phòng mạnh hơn nếu cần reasoning phức tạp hơn
    "gemini-2.0-flash"             # Stable cũ nhưng vẫn chạy tốt làm fallback cuối
]

REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Cà Mau":(9.17,105.15)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

# --- STATE UPDATE: ĐỒNG BỘ TÊN BIẾN VỚI HTML ---
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    
    # AI Config
    'ai_timing': "...", 'ai_target': "...", 'ai_reason': "...", 
    'pump': False, 'warning': "", 'last_ai_call': 0,
    'ai_initialized': False,
    
    # NEW: Cấu hình Auto (Đã sửa tên cho khớp HTML)
    'auto_strategy': 'AI',       # 'AI' hoặc 'CUSTOM'
    'custom_min': 30,            # Ngưỡng bật bơm (Tự đặt)
    'custom_max': 80             # Ngưỡng tắt bơm (Tự đặt)
}

mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

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

def log_event(action, detail):
    if db_collection is None: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        # Lưu thêm soil vào history
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
            
            # Chỉ gọi AI nếu đang mode AUTO và Sub-mode là AI
            if state['mode'] == 'AUTO' and state['auto_strategy'] == 'AI' and state['ai_initialized']: 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

def call_gemini_api_direct(model_name, prompt_text):
    if not GEMINI_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{"parts": [{"text": prompt_text}] }],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.4 }
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"⚠️ API Error ({model_name}): {response.status_code}")
            return None 
        result = response.json()
        raw_text = result['candidates'][0]['content']['parts'][0]['text']
        clean_text = re.sub(r"```json|```", "", raw_text).strip()
        match = re.search(r'\{.*\}', clean_text, re.DOTALL)
        if match: return json.loads(match.group())
        else: return json.loads(clean_text)
    except Exception as e:
        print(f"❌ Gemini Exception ({model_name}): {e}")
        return None

def ask_gemini(force=False):
    if ai_lock.locked(): return    
    with ai_lock:
        if state['mode'] != 'AUTO' or state['auto_strategy'] != 'AI': return # Chỉ chạy khi đúng mode AI
        if not GEMINI_KEY: return
        
        if state['ai_initialized']:
            now = time.time()
            elapsed = now - state['last_ai_call']
            is_emergency = state['soil'] < EMERGENCY_LEVEL
            cooldown_time = 30 if is_emergency else 120
            if not force and elapsed < cooldown_time: return

        # --- ĐÃ SỬA: Dùng {{ và }} cho JSON ---
        prompt = f"""
        Role: Hệ thống tưới cây IoT.
        Input: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
        Trạng thái khẩn cấp: {"CÓ" if state['soil'] < EMERGENCY_LEVEL else "KHÔNG"}.        
        Output JSON Only:
        {{   
            "action": "TƯỚI" hoặc "KHÔNG",
            "target": "Xem xét để phù hợp với thời tiết lượng mưa và đưa ra độ ẩm cần tưới phù hợp (ví dụ 70%, 75% )",
            "timing": "Mô tả bao giờ tưới(ngay bây giờ nếu đất quá khô và bị cảnh báo; hoặc 2 tiếng nữa, 5 tiếng nữa,...)",
            "reason": "Lý do ngắn gọn dựa vào thời tiết, lượng mưa, miền, mùa,... để giải thích tại sao chọn độ ẩm và thời gian đấy"         
        }}""" 
        # --- Lưu ý: Dòng trên phải là }}""" (2 dấu ngoặc) ---
        
        success = False
        for model_name in AI_MODELS_PRIORITY:
            if success: break          
            print(f"\n--- 🤖 AI Direct Call: {model_name} ---")
            data = call_gemini_api_direct(model_name, prompt)            
            if data:
                action = data.get("action", "KHÔNG").upper()
                try:
                    target_raw = data.get("target", 75)
                    target = int(re.search(r'\d+', str(target_raw)).group())
                except: target = 75
                
                state['ai_target'] = target
                state['ai_timing'] = data.get("timing", "...")
                state['ai_reason'] = data.get("reason", "...")
                state['last_ai_call'] = time.time()              
                state['ai_initialized'] = True 
                
                print(f"🎯 AI Success ({model_name}): {action} | {state['ai_reason']}")
                log_event(f"AI_{model_name}", f"{action} - {state['ai_reason']}")            
                
                if action == "TƯỚI": control_pump(True, "AI Start")
                else: control_pump(False, "AI Stop")                
                
                broadcast()
                success = True
            else:
                print(f"⚠️ Model {model_name} thất bại. Đang thử model khác...")

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

def on_message(client, userdata, msg):
    global last_emergency_pump_time
    try:
        payload = msg.payload.decode()
        
        # --- XỬ LÝ CẢM BIẾN ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                if state['mode'] == 'AUTO' and not state['ai_initialized'] and state['auto_strategy'] == 'AI':
                    broadcast(); return 

                # 1. KIỂM TRA KHẨN CẤP (Ưu tiên số 1 - Bảo vệ cây)
                if state['soil'] < EMERGENCY_LEVEL:
                    state['warning'] = "🔥 KHẨN CẤP: ĐẤT QUÁ KHÔ!"
                    if state['mode'] == 'AUTO' and not state['pump']:
                        current_ts = time.time()
                        pump_duration = 60 if (current_ts - last_emergency_pump_time) < EMERGENCY_COOLDOWN else 15
                        last_emergency_pump_time = current_ts
                        control_pump(True, "Emergency Pump")
                        threading.Thread(target=delayed_pump_off, args=(pump_duration,), daemon=True).start()
                elif state['soil'] >= FLOOD_LEVEL:
                    state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
                    if state['pump']: control_pump(False, "Flood Safety")
                
                # 2. XỬ LÝ LOGIC TỰ ĐỘNG (Nếu không có khẩn cấp)
                else:
                    state['warning'] = ""
                    if state['mode'] == 'AUTO':
                        # >>> LOGIC MỚI: TÁCH BIỆT AI VÀ TỰ ĐẶT <<<
                        if state['auto_strategy'] == 'AI':
                            # --- LOGIC AI ---
                            if state['pump'] and state['soil'] >= state['ai_target']:
                                control_pump(False, f"Target AI {state['ai_target']}% OK")                    
                            threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()           
                        
                        elif state['auto_strategy'] == 'CUSTOM':
                            # --- LOGIC NGƯỜI DÙNG TỰ ĐẶT ---
                            # Nếu đất khô hơn mức TƯỚI (custom_min) -> Bật bơm
                            if state['soil'] <= state['custom_min'] and not state['pump']:
                                control_pump(True, f"User Set (<={state['custom_min']}%)")
                            # Nếu đất ẩm hơn mức NGẮT (custom_max) -> Tắt bơm
                            elif state['soil'] >= state['custom_max'] and state['pump']:
                                control_pump(False, f"User Set (>={state['custom_max']}%)")

                broadcast()
            except: pass

        # --- XỬ LÝ SỰ KIỆN TỪ WEB (ĐÃ SỬA ĐỂ KHỚP HTML) ---
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})            
            
            if evt == 'get_status':
                broadcast()
            
            elif evt == 'select_region':
                state['region'] = data['region']; state['step'] = 1; broadcast()        
            
            elif evt == 'enter_mode':
                control_pump(False, "Mode Switch")               
                state['mode'] = data['mode']; state['step'] = 2                
                
                if state['mode'] == 'AUTO':
                    # Mặc định vào AI trước
                    state['auto_strategy'] = 'AI'
                    state['ai_initialized'] = False 
                    state['ai_reason'] = "Đang kết nối vệ tinh AI..."
                    state['ai_timing'] = "Vui lòng đợi..."
                    threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                
                log_event("MODE_CHANGE", f"Vào {state['mode']}")
                broadcast()                
            
            # --- EVENT MỚI: CHỌN CHIẾN THUẬT (AI hay CUSTOM) ---
            elif evt == 'set_strategy':
                new_strat = data.get('strategy', 'AI')
                if state['auto_strategy'] != new_strat:
                    state['auto_strategy'] = new_strat
                    log_event("CONFIG_CHANGE", f"Chuyển sang {new_strat}")
                    
                    if new_strat == 'AI':
                        threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                    else:
                        # Nếu chuyển sang Custom, kiểm tra ngay ngưỡng tưới
                        broadcast() # Cập nhật UI ngay
                broadcast()

            # --- EVENT MỚI: NHẬN NGƯỠNG TỰ ĐẶT TỪ THANH KÉO ---
            elif evt == 'set_thresholds':
                state['custom_min'] = int(data.get('min', 30))
                state['custom_max'] = int(data.get('max', 80))
                # log_event("USER_SET", f"Min:{state['custom_min']} - Max:{state['custom_max']}")
                # Kiểm tra logic ngay lập tức khi thay đổi số
                if state['mode'] == 'AUTO' and state['auto_strategy'] == 'CUSTOM':
                    if state['soil'] <= state['custom_min'] and not state['pump']:
                        control_pump(True, "User Set Update")
                    elif state['soil'] >= state['custom_max'] and state['pump']:
                        control_pump(False, "User Set Update")
                broadcast()

            elif evt == 'exit_dashboard':
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

