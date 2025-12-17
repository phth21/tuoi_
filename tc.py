# tc.py — SMART GARDEN FINAL (Đã thêm cột Độ Ẩm vào Lịch sử)
import threading, time, json, re, os
import paho.mqtt.client as mqtt
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect
from pymongo import MongoClient
from google import genai
from google.genai import types

# ====================== CẤU HÌNH SERVER ======================
app = Flask(__name__)
app.secret_key = 'thao_cute_sieu_cap_vipro'

USERS = {
    'admin': {'pass': 'admin123', 'role': 'ADMIN'},
    'khach': {'pass': '1111',     'role': 'VIEWER'}
}

GEMINI_KEY = os.getenv("GEMINI_KEY")
OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232")
MONGO_URI = os.getenv("MONGO_URI")

# MONGODB CONNECT
db_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.server_info()
        db = mongo_client.get_database("smart_garden_db")
        db_collection = db.history
        print("--- MONGODB ATLAS CONNECTED ---")
    else:
        print("⚠️ Chưa có MONGO_URI")
except: db_collection = None

# AI SETUP
ai_client = None
CURRENT_MODEL = "gemini-1.5-flash"
if GEMINI_KEY: ai_client = genai.Client(api_key=GEMINI_KEY)

# HÀM TÌM MODEL
def find_working_model():
    global CURRENT_MODEL
    candidates = ["gemini-1.5-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro"]
    for name in candidates:
        try:
            ai_client.models.generate_content(model=name, contents="Test", config=types.GenerateContentConfig(max_output_tokens=5))
            CURRENT_MODEL = name; return True
        except: continue
    return False
if ai_client: find_working_model()

# SYSTEM STATE
CRITICAL_LEVEL = 26; FLOOD_LEVEL = 90
BROKER = "broker.hivemq.com"; PREFIX = "thaocute_smartgarden/"
state = {
    'step': 0, 'region': 'NORTH', 'mode': 'NONE', 'location': "Đang dò...", 
    'lat': None, 'lon': None, 'soil': 0, 'temp': 25.0, 'humidity': 80, 'rain': 0.0,
    'ai_timing': "...", 'ai_target': 75, 'ai_reason': "...", 
    'pump': False, 'warning': "", 'last_ai_call': 0
}
mqtt_client = mqtt.Client(client_id=f"Render_Server_{int(time.time())}")

# ====================== FLASK ROUTES ======================
@app.route('/', methods=['GET', 'POST'])
def home():
    if 'user' not in session:
        if request.method == 'POST':
            u = request.form.get('username'); p = request.form.get('password')
            if u in USERS and USERS[u]['pass'] == p: session['user'] = u; session['role'] = USERS[u]['role']; return redirect('/')
        return render_template('login.html')
    return render_template('dashboard.html', user=session['user'], role=session['role'])

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/api/history')
def get_history():
    date_str = request.args.get('date')
    if db_collection is None: return jsonify([])
    try:
        # Lấy dữ liệu từ MongoDB
        logs = list(db_collection.find({"date": date_str}, {'_id': 0}).sort("created_at", -1))
        # Chuẩn hóa dữ liệu trả về cho đẹp
        data = []
        for l in logs:
            data.append({
                "time": l.get("time"),
                "action": l.get("action"),
                "detail": l.get("detail"),
                "soil": l.get("soil", 0) # <--- QUAN TRỌNG: Gửi kèm độ ẩm lúc đó
            })
        return jsonify(data)
    except: return jsonify([])

# ====================== LOGIC CODE ======================
def log_event(action, detail):
    if db_collection is None: return
    try:
        now_vn = datetime.utcnow() + timedelta(hours=7)
        record = {
            "date": now_vn.strftime("%Y-%m-%d"), 
            "time": now_vn.strftime("%H:%M:%S"),
            "action": action, "detail": detail, 
            "soil": state['soil'], # Lưu độ ẩm vào DB
            "created_at": now_vn
        }
        db_collection.insert_one(record)
    except: pass

def broadcast():
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

def ask_gemini(force=False):
    global CURRENT_MODEL
    if state['soil'] >= FLOOD_LEVEL: control_pump(False, "Ngập úng"); return
    if state['mode'] != 'AUTO' or not ai_client: return
    
    now = time.time(); is_emergency = state['soil'] < CRITICAL_LEVEL
    time_diff = now - state['last_ai_call']
    
    if force: pass 
    elif is_emergency: 
        if time_diff < 15: return 
    else: 
        if time_diff < 120: return

    prompt = f"""Đóng vai kỹ sư nông nghiệp.
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
    Khẩn cấp (<{CRITICAL_LEVEL}%): {is_emergency}.
    Trả về JSON: {{ "decision": "ON/OFF", "timing": "...", "target": int, "reason": "..." }}"""

    try:
        res = None
        try: res = ai_client.models.generate_content(model=CURRENT_MODEL, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
        except: 
            if find_working_model(): res = ai_client.models.generate_content(model=CURRENT_MODEL, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
        
        if res and res.text:
            raw = res.text.replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            dec = data.get("decision", "OFF").upper()
            state['ai_target'] = int(data.get("target", 75))
            state['ai_timing'] = data.get("timing", "...")
            state['ai_reason'] = data.get("reason", "...")
            state['last_ai_call'] = now
            
            log_event("AI_DECISION", f"AI: {dec} ({state['ai_reason']})")
            control_pump(dec == "ON", "AI Logic")
            broadcast()
    except Exception as e: print(f"AI Error: {e}")

def control_pump(on, source="System"):
    if state['step'] != 2 and on: on = False
    if state['soil'] >= FLOOD_LEVEL and on: on = False; state['warning'] = "NGẬP ÚNG!"
    if not on and CRITICAL_LEVEL <= state['soil'] < FLOOD_LEVEL: state['warning'] = ""

    if state['pump'] != on:
        state['pump'] = on
        cmd = "ON" if on else "OFF"
        mqtt_client.publish(PREFIX + "cmd", cmd)
        log_event(f"PUMP_{cmd}", source)
    elif not on and state['soil'] >= FLOOD_LEVEL: mqtt_client.publish(PREFIX + "cmd", "OFF")
    broadcast()

def on_message(c, u, msg):
    try:
        payload = msg.payload.decode()
        if msg.topic == PREFIX + "esp/data" and "H:" in payload:
            val = int(payload.split("H:")[1].split()[0])
            state['soil'] = max(0, min(100, val))
            
            if state['soil'] >= FLOOD_LEVEL and state['pump']: control_pump(False, "Sensor Cutoff")
            elif state['mode'] == 'AUTO' and state['step'] == 2:
                if state['soil'] < CRITICAL_LEVEL: threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                if state['pump'] and state['soil'] >= (state['ai_target'] + 3): control_pump(False, "Target Reached")
            broadcast()
            
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            if evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                if state['mode'] == 'AUTO': threading.Thread(target=ask_gemini, kwargs={'force': True}, daemon=True).start()
                else: control_pump(False, "Init Manual")
            elif evt == 'user_control' and state['mode'] == 'MANUAL': control_pump(bool(data['pump']), "User")
            elif evt == 'exit_dashboard': state['step'] = 0; state['mode'] = 'NONE'; control_pump(False)
            elif evt == 'select_region': state['region'] = data['region']; state['step'] = 1
            broadcast()
    except: pass

def run_mqtt():
    mqtt_client.on_message = on_message
    mqtt_client.on_connect = lambda c,u,f,rc: c.subscribe([(PREFIX+"esp/data",0), (PREFIX+"events",0)])
    mqtt_client.connect(BROKER, 1883, 60); mqtt_client.loop_start()

try: run_mqtt()
except: pass

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
