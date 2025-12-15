import threading
import paho.mqtt.client as mqtt
import requests, time, json, re, os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
import google.generativeai as genai

# ====================== 1. CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro' 

# --- CẤU HÌNH TÀI KHOẢN ---
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},  
    'khach': {'pass': '1111',       'role': 'VIEWER'} 
}

# --- CẤU HÌNH API & DATABASE ---
# ⚠️ QUAN TRỌNG: Thay API Key thật của bạn vào đây nếu chưa set biến môi trường
GEMINI_API_KEY = os.getenv("GEMINI_KEY", "Dien_API_Key_Gemini_Cua_Ban_Vao_Day").strip()
OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232") # Key mẫu (nên thay bằng key riêng)
MONGO_URI = os.getenv("MONGO_URI") # Ví dụ: "mongodb+srv://..."

# KẾT NỐI MONGODB
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_database("smart_garden_db")
        db_collection = db.history
        print("✅ MONGODB ATLAS CONNECTED")
    else:
        print("⚠️ Cảnh báo: Chưa có MONGO_URI, lịch sử sẽ không được lưu.")
except Exception as e: print(f"❌ Lỗi MongoDB: {e}")

# KẾT NỐI AI GEMINI
genai.configure(api_key=GEMINI_API_KEY)
model = None
try:
    model = genai.GenerativeModel('gemini-1.5-flash')
    print("✅ AI GEMINI READY")
except Exception as e:
    print(f"❌ Lỗi khởi tạo AI: {e}")

# ====================== 2. BIẾN TOÀN CỤC & TRẠNG THÁI ======================
CRITICAL_LEVEL = 30  # Đất dưới mức này là khô hạn
FLOOD_LEVEL = 90     # Đất trên mức này là ngập úng
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Cà Mau":(9.17,105.15)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

# State hệ thống
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': "...", 'ai_reason': "Đang chờ dữ liệu...",
    'pump': False, 'warning': "", 'last_ai_call': 0
}

mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

# ====================== 3. CÁC ROUTE FLASK (WEB) ======================

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
    # Lấy dữ liệu history từ Mongo
    try:
        logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
        return jsonify(logs)
    except: return jsonify([])

# ====================== 4. LOGIC HỆ THỐNG (CORE) ======================

def log_event(action, detail):
    """Ghi log hành động vào MongoDB"""
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
    """Gửi toàn bộ state xuống Web qua MQTT"""
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

def update_weather():
    """Lấy thời tiết từ OpenWeatherMap"""
    if not state['lat']: return
    try:
        url = f"[https://api.openweathermap.org/data/2.5/weather?lat=](https://api.openweathermap.org/data/2.5/weather?lat=){state['lat']}&lon={state['lon']}&units=metric&appid={OPENWEATHER_KEY}"
        r = requests.get(url, timeout=3).json()
        if r.get('cod') == 200:
            state['temp'] = r['main']['temp']
            state['humidity'] = r['main']['humidity']
            state['rain'] = r.get('rain', {}).get('1h', 0.0)
            
            if "Thủ công" not in state['location'] and "Đang lấy" not in state['location']: 
                state['location'] = r.get('name') + " (VN)"
            
            # Sau khi có thời tiết, nếu đang AUTO thì gọi AI cập nhật chiến thuật
            if state['mode'] == 'AUTO': 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except Exception as e: print(f"Weather Error: {e}")
    broadcast()

# --- [QUAN TRỌNG] HÀM GỌI AI ĐÃ SỬA LỖI ---
def ask_gemini(force=False):
    if state['mode'] != 'AUTO': return 
    
    # 1. Kiểm tra an toàn (Ngập úng -> Tắt ngay)
    if state['soil'] >= FLOOD_LEVEL:
        control_pump(False, "Safety Check (Flood)")
        state['warning'] = f"CẢNH BÁO: NGẬP ÚNG (>{FLOOD_LEVEL}%)"
        broadcast()
        return

    if not model: 
        print("⚠️ Lỗi: Chưa có model AI (Kiểm tra API Key)")
        return

    now = time.time()
    is_emergency = state['soil'] < CRITICAL_LEVEL
    
    # 2. Logic Cooldown (Chống spam API)
    # Nếu khẩn cấp (đất khô): chờ 15s. Nếu bình thường: chờ 60s.
    cooldown = 15 if is_emergency else 60 
    time_diff = now - state['last_ai_call']
    
    if not force and time_diff < cooldown:
        # Chưa hết thời gian chờ -> bỏ qua
        return

    # Cập nhật cảnh báo trên giao diện
    if is_emergency: state['warning'] = "KHẨN CẤP: ĐẤT QUÁ KHÔ!"
    elif state['soil'] >= FLOOD_LEVEL: state['warning'] = "NGẬP ÚNG!"
    else: state['warning'] = "" 
    broadcast()

    # 3. Tạo Prompt (Yêu cầu JSON chuẩn)
    prompt = f"""
    Đóng vai kỹ sư nông nghiệp IoT.
    Dữ liệu hiện tại: 
    - Độ ẩm đất: {state['soil']}%
    - Nhiệt độ: {state['temp']}°C
    - Lượng mưa 1h: {state['rain']}mm
    
    Quy tắc:
    - Nếu đất < 30%: Cân nhắc BẬT bơm (ON).
    - Nếu đất > 70%: TẮT bơm (OFF).
    - Nếu trời mưa (>0.5mm): Ưu tiên TẮT.
    
    Trả về định dạng JSON DUY NHẤT (không giải thích thêm, không markdown):
    {{ "decision": "ON hoặc OFF", "timing": "Mô tả bao giờ tưới", "target": "XX%", "reason": "Lý do ngắn gọn < 15 từ" }}
    """
    
    try:
        print(f"📡 Đang gọi Gemini... (Soil: {state['soil']}%)")
        res = model.generate_content(prompt)
        
        # [FIX] Làm sạch chuỗi JSON (Xóa ```json và ```)
        clean_text = res.text.replace("```json", "").replace("```", "").strip()
        
        # Tìm chuỗi JSON trong phản hồi
        match = re.search(r'\{.*\}', clean_text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            dec = data.get('decision', 'OFF').upper()
            
            state['ai_timing'] = data.get('timing', '...')
            state['ai_target'] = data.get('target', '...')
            state['ai_reason'] = data.get('reason', '...')
            
            # Cập nhật thời gian gọi thành công
            state['last_ai_call'] = now 
            
            # Ghi log và điều khiển
            log_event("AI_DECISION", f"AI: {dec} | {state['ai_reason']}")
            
            # Chỉ gửi lệnh bơm nếu trạng thái thay đổi
            if (dec == 'ON') != state['pump']:
                control_pump(dec == 'ON', "AI Logic")
                
            print(f"✅ AI Done: {dec} | {state['ai_reason']}")
        else:
            print(f"⚠️ AI phản hồi không đúng format JSON: {clean_text}")

    except Exception as e:
        print(f"❌ AI Error: {e}")
    broadcast()

def control_pump(on, source="System"):
    """Hàm điều khiển bơm trung tâm"""
    # Safety: Nếu ngập thì luôn tắt
    if on and state['soil'] >= FLOOD_LEVEL:
        on = False
        state['warning'] = "NGẬP ÚNG! TỪ CHỐI BƠM"

    # Safety: Chỉ được bơm ở Step 2 (Màn hình chính)
    if state['step'] != 2 and on: on = False 
    
    # Chỉ gửi lệnh MQTT nếu trạng thái thay đổi
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 PUMP {cmd} ({source})")
    
    if not on and state['warning'] == "NGẬP ÚNG! TỪ CHỐI BƠM": state['warning'] = ""
    broadcast()

# ====================== 5. XỬ LÝ MQTT (EVENTS) ======================

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # --- A. NHẬN SỐ LIỆU TỪ ESP (Cảm biến) ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                # Payload mẫu: "H: 45 T: 30" -> lấy số 45
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # 1. Kiểm tra an toàn tức thì
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                # 2. Logic AUTO
                elif state['mode'] == 'AUTO':
                    # [FIX] Luôn gọi hàm AI, hàm đó sẽ tự quyết định có chạy hay không dựa vào thời gian
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                    
                    # Logic ngắt bơm theo Target mà AI đã đề ra trước đó (nếu có)
                    if state['pump']:
                        nums = re.findall(r'\d+', str(state['ai_target']))
                        if nums:
                            target_val = int(nums[0])
                            # Nếu đất ẩm hơn mục tiêu -> Tắt bơm
                            if state['soil'] >= target_val:
                                control_pump(False, "AI Target Reached")
                                
                broadcast()
            except Exception as e: print(f"Parse ESP Error: {e}")

        # --- B. NHẬN SỰ KIỆN TỪ WEB (Nút bấm) ---
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            
            if evt == 'select_region':
                state['region'] = data['region']
                state['step'] = 1
                broadcast()
                
            elif evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển chế độ {state['mode']}")
                if state['mode'] == 'AUTO': 
                    # Khi vừa vào Auto, ép AI chạy ngay lập tức
                    threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
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
                threading.Thread(target=update_weather, daemon=True).start()
            
            elif evt == 'user_control' and state['mode'] == 'MANUAL':
                control_pump(bool(data['pump']), "Người dùng bấm")
            
            broadcast()
    except Exception as e: print(f"MQTT Msg Error: {e}")

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe([ (PREFIX+"esp/data",0), (PREFIX+"events",0) ]), print("✅ MQTT CONNECTED"))
    mqtt_client.on_message = on_message
    try: 
        mqtt_client.connect(BROKER, 1883, 60)
        mqtt_client.loop_start() 
    except Exception as e: print(f"❌ Lỗi MQTT: {e}")

# ====================== 6. CHẠY APP ======================
try:
    run_mqtt()
    print("--- SERVER STARTED ---")
except: pass

if __name__ == '__main__':
    # Chạy trên mọi IP để thiết bị khác truy cập được
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
