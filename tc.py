# tc.py — SMART GARDEN HYBRID (FLASK + MONGODB + GEMINI SDK 1.0)
import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient

# 🔥 SDK AI MỚI (GOOGLE GENAI v1.0)
from google import genai
from google.genai import types

# ====================== 1. CẤU HÌNH SERVER & DATABASE ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

# TÀI KHOẢN
USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',     'role': 'VIEWER'}
}

# CONFIG KEYS
GEMINI_KEY = os.getenv("GEMINI_KEY")
OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232")
MONGO_URI = os.getenv("MONGO_URI")

# MONGODB CONNECT (Thay thế Firebase)
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.server_info() # Check kết nối
        db = mongo_client.get_database("smart_garden_db")
        db_collection = db.history
        print("--- MONGODB ATLAS CONNECTED ---")
    else:
        print("⚠️ Cảnh báo: Chưa có MONGO_URI")
except Exception as e:
    print(f"❌ Lỗi MongoDB: {e}")
    db_collection = None

# ====================== 2. CẤU HÌNH AI (SDK MỚI) ======================
ai_client = None
CURRENT_MODEL = "gemini-1.5-flash"

if GEMINI_KEY:
    ai_client = genai.Client(api_key=GEMINI_KEY)

def find_working_model():
    """Dò tìm model AI còn sống (Logic giữ nguyên vì rất tốt)"""
    global CURRENT_MODEL
    print("\n🔍 Đang dò tìm model AI...")
    candidates = ["gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"]
    
    for name in candidates:
        try:
            ai_client.models.generate_content(
                model=name, contents="Test", 
                config=types.GenerateContentConfig(max_output_tokens=5)
            )
            CURRENT_MODEL = name
            print(f"✅ Đã chọn model: {CURRENT_MODEL}")
            return True
        except: continue
    return False

if ai_client: find_working_model()

# ====================== 3. THAM SỐ HỆ THỐNG (TỪ CODE MẪU) ======================
CRITICAL_LEVEL = 26  # Đất khô khẩn cấp
FLOOD_LEVEL    = 90  # Ngập úng tuyệt đối

BROKER = "broker.hivemq.com"
PREFIX = "thaocute_smartgarden/"

# DATABASE TỈNH THÀNH
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Cà Mau":(9.17,105.15)}
}
ALL_CITIES = {}
for r in REGIONAL_DB.values(): ALL_CITIES.update(r)

state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': 75, 'ai_reason': "...", 
    'pump': False, 'warning': "", 'last_ai_call': 0
}

mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

# ====================== 4. FLASK ROUTES ======================
@app.route('/', methods=['GET', 'POST'])
def home():
    if 'user' not in session:
        if request.method == 'POST':
            u = request.form.get('username'); p = request.form.get('password')
            if u in USERS and USERS[u]['pass'] == p:
                session['user'] = u; session['role'] = USERS[u]['role']
                return redirect('/')
        return render_template('login.html')
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/api/history')
def get_history():
    date_str = request.args.get('date')
    if not db_collection: return jsonify([])
    try:
        logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
        return jsonify(logs)
    except: return jsonify([])

# ====================== 5. HÀM LOGIC (GHÉP TỪ CODE MẪU) ======================
def log_event(action, detail):
    """Ghi log vào MongoDB"""
    if not db_collection: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        record = {
            "date": now_vn.strftime("%Y-%m-%d"), 
            "time": now_vn.strftime("%H:%M:%S"),
            "action": action, "detail": detail, 
            "soil": state['soil'], "created_at": now_vn
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
            if "Thủ công" not in state['location']: state['location'] = r.get('name') + " (VN)"
            
            # Chỉ gọi AI khi có thời tiết mới VÀ đang AUTO
            if state['mode'] == 'AUTO':
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

# --- 🔥 TRÁI TIM CỦA HỆ THỐNG: AI LOGIC (ĐÃ GHÉP) ---
def ask_gemini(force=False):
    global CURRENT_MODEL
    
    # 1. Check an toàn trước
    if state['soil'] >= FLOOD_LEVEL:
        control_pump(False, "Safety Check (Ngập)")
        return

    if state['mode'] != 'AUTO' or not ai_client: return

    now = time.time()
    is_emergency = state['soil'] < CRITICAL_LEVEL
    time_diff = now - state['last_ai_call']

    # 2. Logic Cooldown (Giống code mẫu)
    # - Nếu Force (ép buộc): Chạy luôn
    # - Nếu Khẩn cấp: Chờ tối thiểu 15s (Chống spam)
    # - Bình thường: Chờ 120s (Tiết kiệm tiền/quota)
    if force:
        pass 
    elif is_emergency:
        if time_diff < 15: return 
    else:
        if time_diff < 120: return

    # Cập nhật warning để hiển thị Web
    if is_emergency: state['warning'] = "KHẨN CẤP: ĐẤT QUÁ KHÔ!"
    else: state['warning'] = ""

    print(f"\n📡 Đang gọi Gemini... (Soil: {state['soil']}%)")

    # 3. Prompt (Giống code mẫu - Kỹ sư nông nghiệp)
    prompt = f"""
    Đóng vai kỹ sư nông nghiệp.
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
    Khẩn cấp (<{CRITICAL_LEVEL}%): {is_emergency}.
    
    Yêu cầu trả về đúng định dạng JSON: 
    {{ "decision": "ON hoặc OFF", "timing": "...", "target": số_nguyên, "reason": "..." }}
    
    Lưu ý:
    - "target": Độ ẩm mục tiêu để dừng bơm (VD: 75).
    - "timing": Mô tả ngắn gọn bao giờ tưới.
    - "reason": Lý do ngắn gọn.
    """

    try:
        # Gọi SDK Mới
        response = None
        try:
            response = ai_client.models.generate_content(
                model=CURRENT_MODEL, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.5)
            )
        except:
            # Retry logic
            if find_working_model():
                response = ai_client.models.generate_content(
                    model=CURRENT_MODEL, contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
            else: return

        # Xử lý kết quả
        if response and response.text:
            # Lọc markdown nếu AI lỡ tay thêm vào
            raw = response.text.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)

            decision = data.get("decision", "OFF").upper() # ON/OFF
            target   = int(data.get("target", 75))
            timing   = data.get("timing", "...")
            reason   = data.get("reason", "...")

            state['ai_target'] = target
            state['ai_timing'] = timing
            state['ai_reason'] = reason
            state['last_ai_call'] = now

            print(f"🎯 AI → {decision} | Target={target}% | {reason}")
            log_event("AI_DECISION", f"AI: {decision} ({reason})")

            control_pump(decision == "ON", "AI Logic")
            broadcast()

    except Exception as e:
        print(f"❌ AI Error: {e}")

# ====================== ĐIỀU KHIỂN BƠM (GHÉP LOGIC AN TOÀN) ======================
def control_pump(on, source="System"):
    # 1. Chỉ cho phép bơm ở Step 2 (Mode AUTO/MANUAL)
    if state['step'] != 2 and on: on = False

    # 2. LOGIC CHỐNG NGẬP TUYỆT ĐỐI (>= 90%)
    if state['soil'] >= FLOOD_LEVEL and on:
        on = False
        state['warning'] = f"NGẬP ÚNG! CẤM BƠM (>{FLOOD_LEVEL}%)"
        print(f"⚠️ [SAFETY] Đất {state['soil']}% -> Block bơm!")

    # Xóa cảnh báo nếu đã an toàn
    if not on and CRITICAL_LEVEL <= state['soil'] < FLOOD_LEVEL:
        state['warning'] = ""

    # Gửi lệnh MQTT
    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
        print(f"💦 PUMP {cmd} ({source})")

    # Dự phòng: Luôn gửi OFF nếu đất đang ngập (đề phòng gói tin trước bị mất)
    elif on == False and state['soil'] >= FLOOD_LEVEL:
        mqtt_client.publish(PREFIX + "cmd", "OFF")

    broadcast()

# ====================== MQTT HANDLE ======================
def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # A. AN TOÀN (Ngập là cắt ngay lập tức)
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Auto-Cutoff (Sensor)")
                
                # B. LOGIC AUTO
                elif state['mode'] == 'AUTO' and state['step'] == 2:
                    # Nếu đất khô khẩn cấp -> Gọi AI ngay (bỏ qua cooldown 2 phút)
                    if state['soil'] < CRITICAL_LEVEL:
                         threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                    
                    # 🔴 LOGIC TỰ NGẮT THÔNG MINH (Target + 3%)
                    # Nếu đang bơm mà đất đã ẩm hơn mục tiêu AI đề ra 3% -> Tắt
                    if state['pump']:
                        try:
                            target_val = int(state['ai_target'])
                            # Bù 3% cho quán tính nước thấm
                            if state['soil'] >= (target_val + 3):
                                control_pump(False, f"Đạt mục tiêu {target_val}% (+3%)")
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
                # Vào Auto -> Gọi AI ngay (Force)
                if state['mode'] == 'AUTO': 
                    threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                else:
                    control_pump(False, "Init Manual")
                broadcast()
            elif evt == 'exit_dashboard':
                state['step'] = 0; state['mode'] = 'NONE'; control_pump(False); broadcast()
            elif evt == 'set_city':
                city = data.get('city')
                if city in ALL_CITIES:
                    state['lat'], state['lon'] = ALL_CITIES[city]
                    state['location'] = f"{city} (Thủ công)"
                    threading.Thread(target=update_weather, daemon=True).start()
            elif evt == 'user_control' and state['mode'] == 'MANUAL':
                control_pump(bool(data['pump']), "Người dùng bấm")
            
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
    app.run(host='0.0.0.0', port=port, use_reloader=False)
