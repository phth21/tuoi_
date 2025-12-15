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
GEMINI_API_KEY = os.getenv("GEMINI_KEY")
if not GEMINI_API_KEY:
    if not GEMINI_API_KEY:
        raise RuntimeError("❌ GEMINI_KEY environment variable not set")

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

# ====================== AI AUTO-DISCOVERY (THỬ SAI TRỰC TIẾP) ======================
genai.configure(api_key=GEMINI_API_KEY)
model = None

def init_gemini_model():
    """
    Hàm khởi tạo AI theo kiểu 'Thử Sai'. 
    Nó sẽ gửi thử 1 tin nhắn 'test' tới Google. 
    Cái nào không lỗi 404 thì lấy cái đó.
    """
    global model
    print("\n🔍 Đang dò tìm model AI phù hợp...")
    
    # Danh sách các tên model có thể dùng (Ưu tiên Flash -> Pro -> Cũ)
    candidates = [
        "gemini-1.5-flash",          # Bản chuẩn, nhanh, free
        "gemini-1.5-flash-latest",   # Bản mới nhất của Flash
        "gemini-1.5-pro",            # Bản Pro (thông minh hơn)
        "gemini-1.5-pro-latest",     # Bản Pro mới nhất
        "gemini-1.0-pro",            # Bản ổn định đời cũ
    ]
    
    for name in candidates:
        try:
            print(f"   👉 Đang thử: {name}...", end=" ")
            temp_model = genai.GenerativeModel(name)
            
            # QUAN TRỌNG: Gọi thử 1 lệnh giả để xem có bị lỗi 404 không
            temp_model.generate_content("Test") 
            
            print("✅ KẾT NỐI THÀNH CÔNG!")
            return temp_model
        except Exception as e:
            # Nếu lỗi, in ra ngắn gọn rồi thử cái tiếp theo
            err_msg = str(e)
            if "404" in err_msg or "not found" in err_msg:
                print("❌ (Không tìm thấy/Lỗi model)")
            else:
                print(f"❌ (Lỗi khác: {err_msg[:30]}...)")
            continue

    print("\n⚠️ CẢNH BÁO: Không model nào chạy được. Đang ép dùng 'gemini-1.5-flash'...")
    return genai.GenerativeModel("gemini-1.5-flash")

# Khởi tạo lần đầu
try:
    model = init_gemini_model()
    print("--- AI SYSTEM READY ---")
except Exception as e:
    print(f"❌ Lỗi khởi tạo AI Fatal: {e}")

# ====================== BIẾN TOÀN CỤC & HẰNG SỐ ======================
FLOOD_LEVEL = 90
EMERGENCY_LEVEL = 25 # Dưới mức này là QUÁ KHÔ -> Gọi AI gấp

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
    logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
    return jsonify(logs)

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
            if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

# --- LOGIC AI MỚI: 120s THƯỜNG / 30s KHẨN CẤP ---
def ask_gemini(force=False):
    global model
    
    # 1. Xác định thời gian chờ (Cooldown)
    is_emergency = state['soil'] < EMERGENCY_LEVEL
    cooldown_time = 30 if is_emergency else 120  # Khô quá thì 30s, thường thì 120s
    
    elapsed = time.time() - state['last_ai_call']
    
    # 2. Kiểm tra điều kiện chạy
    if state['mode'] != 'AUTO': return
    
    if not force and elapsed < cooldown_time:
        # Chỉ in log cooldown nếu là trường hợp khẩn cấp để debug
        if is_emergency: print(f"⏳ Đất khô ({state['soil']}%) - Chờ {cooldown_time}s (Mới {int(elapsed)}s)")
        return

    print(f"\n--- 🤖 AI CHECK | Soil={state['soil']}% | Mode={'KHẨN CẤP' if is_emergency else 'ĐỊNH KỲ'} ---")

    # Đảm bảo model tồn tại trước khi gọi
    if model is None:
        model = init_gemini_model()
        if model is None: return

    # 3. Prompt thông minh
    urgent_note = ""
    if is_emergency:
        urgent_note = "CẢNH BÁO: ĐẤT ĐANG RẤT KHÔ! HÃY ƯU TIÊN TƯỚI NGAY LẬP TỨC!"

    prompt = f"""
    Độ ẩm đất: {state['soil']}%. Nhiệt độ: {state['temp']}C. Mưa 1h: {state['rain']}mm.
    {urgent_note}
    
    Bạn là hệ thống tưới cây thông minh.
    Trả lời DUY NHẤT JSON:
    {{
      "action": "TƯỚI" hoặc "KHÔNG",
      "target": Độ ẩm mục tiêu để dừng bơm (bạn phải tự dự đoán),
      "timing": Mô tả ngắn gọn bao giờ tưới(bắt buộc phải có thời gian nhất định) và độ ẩm dự đoán là bao nhiêu,
      "reason": Lý do ngắn gọn giải thích tại sao tưới đến độ ẩm đấy
    }}
    """

    try:
        # --- THỰC HIỆN GỌI AI ---
        # Thêm cơ chế: Nếu lỗi model thì tự đổi và gọi lại ngay lập tức (Retry logic)
        try:
            response = model.generate_content(prompt)
        except Exception as e:
            if "404" in str(e) or "not found" in str(e):
                print("🔄 Model hiện tại bị lỗi 404. Đang đổi model khác và THỬ LẠI NGAY...")
                model = init_gemini_model() # Tìm model mới
                if model:
                    response = model.generate_content(prompt) # Gọi lại lần 2
                else:
                    return # Chịu thua
            else:
                raise e # Nếu lỗi khác (mạng rớt...) thì ném ra ngoài để log

        # 4. Xử lý kết quả (Parse JSON)
        raw = response.text.strip()
        # print("📝 AI RAW:", raw) # Bật dòng này nếu muốn debug xem AI trả lời gì
        
        text = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)

        action = data.get("action", "KHÔNG")
        target = int(data.get("target", state['soil']))
        timing = data.get("timing", "...")
        reason = data.get("reason", "...")

        state['ai_target'] = target; state['ai_timing'] = timing; state['ai_reason'] = reason
        state['last_ai_call'] = time.time() # Cập nhật thời gian gọi cuối

        print(f"🎯 AI → {action} | Target={target}% | {timing}")

        if action == "TƯỚI": control_pump(True, "AI Decision")
        else: control_pump(False, "AI Decision")

        broadcast()

    except Exception as e:
        print("❌ AI ERROR:", e)
        # Vẫn giữ dòng này để phòng hờ các lỗi khác làm hỏng model
        if "404" in str(e) or "not found" in str(e): 
            model = init_gemini_model()

def control_pump(on, source="System"):
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

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        
        # --- 1. NHẬN SỐ LIỆU TỪ ESP (CẢM BIẾN) ---
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            try:
                # Lấy độ ẩm hiện tại
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                
                # A. AN TOÀN TUYỆT ĐỐI: Ngập úng (90%) là tắt bơm bất chấp mọi thứ
                if state['soil'] >= FLOOD_LEVEL and state['pump']:
                    control_pump(False, "Safety Cutoff")
                
                # B. LOGIC AUTO (AI)
                elif state['mode'] == 'AUTO':
                    # 1. Gửi dữ liệu cho AI (để nó quyết định tưới hay chờ)
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                    
                    # 2. 🔴 LOGIC NGẮT BƠM THEO MỤC TIÊU CỦA AI 🔴
                    # Chỉ kiểm tra khi bơm đang BẬT
                    if state['pump']:
                        try:
                            # Lấy con số mục tiêu AI đã đặt ra (ví dụ: 75)
                            # (Code AI ở trên đã lưu số này vào state['ai_target'])
                            target_val = int(state['ai_target'])
                            
                            # So sánh: Nếu độ ẩm hiện tại >= Mục tiêu AI
                            # (Ví dụ: Đất 76% >= Mục tiêu 75% -> TẮT)
                            if state['soil'] >= target_val:
                                control_pump(False, f"Đạt mục tiêu AI ({target_val}%)")
                                print(f"✅ Đã tưới xong! Đất đạt {state['soil']}% (Mục tiêu: {target_val}%)")
                        except:
                            pass # Nếu lỗi đọc số mục tiêu thì bỏ qua

                broadcast() # Gửi dữ liệu mới nhất xuống Web
            except: pass

        # --- 2. NHẬN SỰ KIỆN TỪ WEB (Giữ nguyên không đổi) ---
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

