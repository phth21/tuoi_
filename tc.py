import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient

# 🔥 SDK MỚI (GOOGLE GENAI v1.0+)
from google import genai
from google.genai import types

# ====================== 1. CẤU HÌNH SERVER & KHÓA AN TOÀN ======================
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

# ====================== KHỞI TẠO AI (AUTO SELECT MODEL) ======================
ai_client = None

# [MỚI] Danh sách Model để tự động lựa chọn
AVAILABLE_MODELS = ["gemini-2.0-flash-exp", "gemini-1.5-flash", "gemini-1.5-pro"]
CURRENT_MODEL = AVAILABLE_MODELS[0] # Mặc định dùng cái mới nhất

def auto_select_model(is_urgent=False):
    """
    [MỚI] Tự động chọn model AI hợp lý.
    - Nếu khẩn cấp: Dùng Flash (nhanh).
    - Nếu bình thường: Có thể dùng Pro (thông minh hơn) hoặc giữ nguyên Flash.
    """
    global CURRENT_MODEL
    if is_urgent:
        # Ưu tiên tốc độ khi khẩn cấp
        CURRENT_MODEL = "gemini-2.0-flash-exp"
    else:
        # Logic mặc định
        CURRENT_MODEL = "gemini-2.0-flash-exp"
    return CURRENT_MODEL

if GEMINI_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_KEY)
        print(f"✅ AI READY: Đang khởi chạy với {CURRENT_MODEL}")
    except Exception as e:
        print(f"❌ Lỗi khởi tạo AI: {e}")

# ====================== BIẾN TOÀN CỤC ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 25  # Mức báo động khô cần tưới gấp

# [MỚI] Biến theo dõi logic bơm khẩn cấp 15s/60s
last_emergency_pump_time = 0 
EMERGENCY_COOLDOWN = 300  # 5 phút (nếu bị lại trong 5p thì bơm 60s)

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

# --- 🔥 HÀM HỎI AI (ĐÃ CHỈNH SỬA PROMPT & OUTPUT) ---
def ask_gemini(force=False):
    if ai_lock.locked(): return 
    
    with ai_lock:
        if state['mode'] != 'AUTO': return
        if not ai_client: return

        now = time.time()
        elapsed = now - state['last_ai_call']
        is_emergency = state['soil'] < EMERGENCY_LEVEL
        
        # [MỚI] Tự động chọn model dựa trên tình huống
        model_to_use = auto_select_model(is_emergency)

        # Nếu đang khẩn cấp, hỏi thường xuyên hơn
        cooldown_time = 30 if is_emergency else 120
        if not force and elapsed < cooldown_time: return

        print(f"\n--- 🤖 AI CHECK ({model_to_use}) | Soil={state['soil']}% ---")

        # [MỚI] Cập nhật Prompt theo yêu cầu Target/Timing/Reason
        prompt = f"""
        Role: Hệ thống điều khiển tưới thông minh.
        Input: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
        Trạng thái khẩn cấp: {"CÓ (Đất < 25%)" if is_emergency else "KHÔNG"}.
        
        Nhiệm vụ: Trả về JSON với các trường sau:
        1. "action": "TƯỚI" hoặc "KHÔNG".
        2. "target": (int) Độ ẩm mục tiêu để dừng bơm (VD: 75). Ý nghĩa: lần tưới tiếp theo sẽ dừng ở mức này.
        3. "timing": (string) Khi nào cần tưới tiếp (VD: "1 giờ nữa", "KHẨN CẤP", "Ngày mai"). Dựa vào độ ẩm hiện tại.
        4. "reason": (string) Lý do ngắn gọn (VD: "Đất khô, trời nắng", "Đang mưa").
        """

        try:
            response = ai_client.models.generate_content(
                model=model_to_use,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    temperature=0.4
                )
            )

            if response and response.text:
                data = json.loads(response.text)

                action = data.get("action", "KHÔNG").upper()
                target = int(data.get("target", 75)) # Default an toàn
                timing = data.get("timing", "Chờ cập nhật")
                reason = data.get("reason", "Đang phân tích...")

                state['ai_target'] = target; state['ai_timing'] = timing; state['ai_reason'] = reason
                state['last_ai_call'] = time.time()

                print(f"🎯 AI → {action} | Target: {target}% | Time: {timing} | Reason: {reason}")
                log_event("AI_DECISION", f"{action} - {reason}")

                # Lưu ý: Việc điều khiển bơm khẩn cấp (15s/60s) sẽ được xử lý ưu tiên ở on_message
                # AI chỉ đóng vai trò tư vấn và điều khiển tưới bổ sung thông thường
                if not is_emergency: 
                    if action == "TƯỚI": control_pump(True, "AI Decision")
                    else: control_pump(False, "AI Decision")
                
                broadcast()

        except Exception as e:
            print(f"❌ AI ERROR: {e}")

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

# [MỚI] Hàm hỗ trợ tắt bơm sau X giây (cho logic khẩn cấp)
def delayed_pump_off(duration):
    time.sleep(duration)
    if state['pump']: # Chỉ tắt nếu nó đang bật
        print(f"⏳ Hết thời gian bơm khẩn cấp ({duration}s). Tắt bơm.")
        control_pump(False, f"Auto Stop ({duration}s)")

# ====================== MQTT HANDLE (LOGIC 15s/60s MỚI) ======================
def on_message(client, userdata, msg):
    global last_emergency_pump_time # Sử dụng biến toàn cục
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # --- 🔥 LOGIC KHẨN CẤP (15s vs 60s) ---
                if state['soil'] < EMERGENCY_LEVEL:
                    state['warning'] = "🔥 KHẨN CẤP: ĐẤT QUÁ KHÔ!"
                    
                    # Nếu đang ở chế độ AUTO và bơm chưa bật (để tránh spam lệnh ON)
                    if state['mode'] == 'AUTO' and not state['pump']:
                        current_ts = time.time()
                        
                        # Kiểm tra xem lần khẩn cấp trước có quá gần không (Emergency Cooldown)
                        if (current_ts - last_emergency_pump_time) < EMERGENCY_COOLDOWN:
                            pump_duration = 60
                            log_msg = "Khẩn cấp dồn dập -> Bơm 60s"
                        else:
                            pump_duration = 15
                            log_msg = "Khẩn cấp thường -> Bơm 15s"
                        
                        # Cập nhật thời gian và thực hiện bơm
                        last_emergency_pump_time = current_ts
                        control_pump(True, log_msg)
                        
                        # Tạo luồng riêng để tắt bơm sau thời gian định sẵn
                        threading.Thread(target=delayed_pump_off, args=(pump_duration,), daemon=True).start()

                elif state['soil'] >= FLOOD_LEVEL:
                    state['warning'] = "⛔ NGUY HIỂM: NGẬP ÚNG!"
                else:
                    state['warning'] = "" 
                
                # --- LOGIC CẮT BƠM AN TOÀN ---
                # A. Ngập là cắt
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                # B. Logic Auto thường (Dừng khi đạt target của AI)
                elif state['mode'] == 'AUTO':
                    if state['pump'] and state['soil'] >= state['ai_target']:
                        # Chỉ tắt nếu không phải đang trong chu trình khẩn cấp (logic timer sẽ lo khẩn cấp)
                        # Ở đây kiểm tra đơn giản: nếu độ ẩm đã ngon thì tắt
                        control_pump(False, f"Đạt mục tiêu {state['ai_target']}%")
                    
                    # Vẫn gọi AI để cập nhật trạng thái UI (Target/Timing/Reason)
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                
                broadcast() 
            except: pass

        # --- 2. NHẬN SỰ KIỆN TỪ WEB (GIỮ NGUYÊN) ---
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
