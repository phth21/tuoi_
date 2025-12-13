import threading
import paho.mqtt.client as mqtt
import requests, time, json, re, os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from pymongo import MongoClient
import google.generativeai as genai

# ====================== CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro' # <--- Khóa bảo mật session

# TÀI KHOẢN ĐĂNG NHẬP
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},  # Chủ vườn (Full quyền)
    'khach': {'pass': '1111',       'role': 'VIEWER'} # Khách (Chỉ xem)
}

# API KEYS & DATABASE (Nên đưa vào biến môi trường nếu có thể)
GEMINI_API_KEY = os.getenv("GEMINI_KEY", "AIzaSyDnmQNHRgXXPgl-ZhK-Et8EiAW9MjTh-5s").strip()
OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232")
MONGO_URI = os.getenv("MONGO_URI")

# KẾT NỐI MONGODB
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_database("smart_garden_db")
        db_collection = db.history
        print("--- MONGODB ATLAS CONNECTED ---")
except Exception as e: print(f"❌ Lỗi MongoDB: {e}")

# KẾT NỐI AI GEMINI
genai.configure(api_key=GEMINI_API_KEY)
model = None
try:
    model = genai.GenerativeModel('gemini-2.5-flash')
    print("--- AI GEMINI 2.5 FLASH READY ---")
except Exception as e:
    print(f"❌ Lỗi khởi tạo AI: {e}")

# CẤU HÌNH HỆ THỐNG
CRITICAL_LEVEL = 26  # Đất khô dưới mức này là KHẨN CẤP
FLOOD_LEVEL = 90     # Đất ẩm trên mức này là NGẬP
BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

# DỮ LIỆU TỈNH THÀNH (Dùng khi chọn thủ công)
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

# BIẾN TRẠNG THÁI TOÀN CỤC (STATE)
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 
    'location': "Đang định vị...", 
    'lat': None, 'lon': None, # Ban đầu để None, chờ Auto IP hoặc Thủ công
    'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_reason': "...", 'ai_target': "...", 
    'pump': False, 'warning': "", 'last_ai_call': 0
}
mqtt_client = mqtt.Client()

# ====================== ROUTE WEB (FLASK) ======================

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

# ====================== CHỨC NĂNG LOGIC CỐT LÕI ======================

# 1. HÀM TỰ ĐỘNG DÒ VỊ TRÍ (AUTO IP)
def auto_detect_location():
    print("🌍 Đang tự động dò tìm vị trí qua Internet...")
    try:
        # Gọi API miễn phí lấy tọa độ từ IP
        r = requests.get("http://ip-api.com/json/", timeout=5).json()
        if r['status'] == 'success':
            # Chỉ cập nhật nếu người dùng CHƯA chọn thủ công
            if "(Thủ công)" not in state['location']:
                state['lat'] = r['lat']
                state['lon'] = r['lon']
                state['location'] = f"{r['city']} (Tự động)"
                print(f"✅ Đã tìm thấy bạn tại: {r['city']} ({r['lat']}, {r['lon']})")
                # Có vị trí rồi thì lấy thời tiết ngay
                update_weather()
        else:
            print("⚠️ Không dò được vị trí tự động.")
    except Exception as e:
        print(f"❌ Lỗi dò vị trí: {e}")

# 2. HÀM GHI LOG VÀO MONGODB
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

# 3. HÀM GỬI DỮ LIỆU CẬP NHẬT CHO WEB
def broadcast():
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

# 4. HÀM LẤY THỜI TIẾT TỪ OPENWEATHERMAP
def update_weather():
    # Nếu chưa có tọa độ (do Auto lỗi và chưa chọn tay) thì thoát
    if not state['lat']: return 
    
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={state['lat']}&lon={state['lon']}&units=metric&appid={OPENWEATHER_KEY}"
        r = requests.get(url, timeout=3).json()
        if r.get('cod') == 200:
            state['temp'] = r['main']['temp']
            state['humidity'] = r['main']['humidity']
            state['rain'] = r.get('rain', {}).get('1h', 0.0)
            
            # Cập nhật tên địa điểm nếu đang dùng Auto/GPS (không ghi đè nếu là Thủ công)
            if "(Thủ công)" not in state['location']: 
                if "Tự động" not in state['location']: # Nếu chưa có tên thì lấy tên từ Weather
                    state['location'] = f"{r.get('name')} (GPS)"
            
            # Nếu đang AUTO MODE thì gọi AI luôn để check điều kiện mới
            if state['mode'] == 'AUTO': 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
        else:
            print(f"⚠️ Weather API Error: {r.get('message')}")
    except Exception as e: 
        print(f"❌ Update Weather Error: {e}")
    broadcast()

# 5. HÀM GỌI AI GEMINI (ĐÃ FIX LỖI JSON)
def ask_gemini(force=False):
    if state['mode'] != 'AUTO' or not model: return 
    now = time.time()
    is_emergency = state['soil'] < CRITICAL_LEVEL
    
    # Logic tần suất gọi AI (Tiết kiệm tiền)
    if not force:
        # Nếu khẩn cấp: 15s gọi 1 lần. Bình thường: 2 phút gọi 1 lần.
        wait_time = 15 if is_emergency else 120
        if (now - state['last_ai_call'] < wait_time): return

    state['warning'] = "KHẨN CẤP: ĐẤT QUÁ KHÔ!" if is_emergency else ("CẢNH BÁO: NGẬP!" if state['soil'] >= FLOOD_LEVEL else "")
    broadcast()

    prompt = f"""
    Đóng vai kỹ sư nông nghiệp.
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
    Khẩn cấp (<26%): {is_emergency}.
    
    Yêu cầu trả về JSON chuẩn (không markdown): 
    {{ "decision": "ON hoặc OFF", "timing": "...", "target": "XX%", "reason": "..." }}
    
    Lưu ý:
    - "target": Độ ẩm mục tiêu để dừng bơm (VD: 70%).
    - "timing": Bao giờ tưới và dự đoán (VD: Tưới ngay trong 10p).
    - "reason": Lý do ngắn gọn (< 15 từ).
    """
    
    print(f"🤖 Đang hỏi AI... (Đất: {state['soil']}%)")
    try:
        res = model.generate_content(prompt)
        text = res.text
        # [FIX] Xóa ký tự markdown thừa
        text = text.replace("```json", "").replace("```", "").strip()
        
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            dec = data.get('decision', 'OFF').upper()
            
            state['ai_timing'] = data.get('timing', '...')
            state['ai_reason'] = data.get('reason', '...')
            state['ai_target'] = data.get('target', '...')
            
            state['last_ai_call'] = now 
            print(f"✅ AI Trả lời: {dec} | Mục tiêu: {state['ai_target']}")
            
            log_event("AI_AUTO", f"Quyết định: {dec}. Mục tiêu: {state['ai_target']}. {state['ai_reason']}")
            control_pump(dec == 'ON', "AI Logic")
        else:
            print(f"⚠️ Lỗi định dạng JSON từ AI: {text}")
    except Exception as e: 
        print(f"❌ Lỗi kết nối AI: {e}")
    broadcast()

# 6. HÀM ĐIỀU KHIỂN BƠM (Gửi lệnh MQTT)
def control_pump(on, source="System"):
    # Nếu chưa vào chế độ điều khiển (Step 2) thì không được bật bơm
    if state['step'] != 2 and on: on = False
    
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 MÁY BƠM: {cmd} ({source})")
    broadcast()

# 7. XỬ LÝ TIN NHẮN MQTT ĐẾN
def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # A. Nhận dữ liệu cảm biến từ ESP
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            val = int(payload.split("H:")[1].split()[0])
            state['soil'] = max(0, min(100, val))
            
            # [AN TOÀN] Chống ngập cấp tốc (Ngắt cứng nếu > 90%)
            if state['soil'] >= FLOOD_LEVEL and state['pump']:
                control_pump(False, "BẢO VỆ CHỐNG NGẬP")

            # Nếu Đất khô khẩn cấp khi đang Auto -> Gọi AI ngay lập tức
            if state['mode'] == 'AUTO' and state['soil'] < CRITICAL_LEVEL: 
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
            broadcast()
            
        # B. Nhận sự kiện điều khiển từ Web
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            
            if evt == 'select_region': # Chọn vùng
                state['region'] = data['region']; state['step'] = 1
                # Nếu người dùng muốn reset vị trí theo vùng (Bỏ comment nếu muốn)
                # if data['region'] == 'NORTH': state['lat'], state['lon'] = 21.02, 105.85 ...

            elif evt == 'enter_mode': # Chọn chế độ
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển chế độ {state['mode']}")
                if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
            
            elif evt == 'exit_dashboard': # Thoát
                state['step'] = 1; state['mode'] = 'NONE'; control_pump(False)
            
            elif evt == 'set_city': # [QUAN TRỌNG] Chọn địa điểm thủ công
                city = data.get('city')
                if city in ALL_CITIES:
                    state['lat'], state['lon'] = ALL_CITIES[city]
                    state['location'] = f"{city} (Thủ công)" # Ghi đè lên Auto
                    threading.Thread(target=update_weather, daemon=True).start()
            
            elif evt == 'user_control' and state['mode'] == 'MANUAL': # Bấm nút
                control_pump(bool(data['pump']), "Người dùng bấm")
            broadcast()
    except Exception as e: print(f"MQTT Handle Error: {e}")

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe([ (PREFIX+"esp/data",0), (PREFIX+"events",0) ]), print("📡 MQTT CONNECTED"))
    mqtt_client.on_message = on_message
    try: mqtt_client.connect(BROKER, 1883, 60); mqtt_client.loop_forever()
    except: print("❌ Lỗi kết nối Broker MQTT")

# ====================== KHỞI ĐỘNG SERVER ======================
if __name__ == '__main__':
    # 1. Chạy luồng MQTT
    threading.Thread(target=run_mqtt, daemon=True).start()
    
    # 2. Tự động dò vị trí ngay khi bật (Chỉ chạy 1 lần đầu)
    threading.Thread(target=auto_detect_location, daemon=True).start()

    print("🚀 SERVER ĐANG CHẠY TẠI http://localhost:5000")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
