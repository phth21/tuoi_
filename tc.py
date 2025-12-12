import threading
import paho.mqtt.client as mqtt
import requests, time, json, re, os
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify
from pymongo import MongoClient

# ====== THƯ VIỆN AI ======
import google.generativeai as genai

# ====================== 0. CẤU HÌNH WEB FLASK ======================
app = Flask(__name__)

# Nhúng nội dung HTML vào biến Python
# Đã xóa Firebase SDK, thay bằng fetch API gọi về Server
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Garden (MongoDB Edition)</title>
<script src="https://unpkg.com/mqtt/dist/mqtt.min.js"></script>

<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;700;900&display=swap" rel="stylesheet">
<style>
  :root { --bg: linear-gradient(135deg, #00b09b 0%, #96c93d 100%); }
  body { margin:0; font-family: 'Nunito', sans-serif; background: var(--bg); min-height: 100vh; display: flex; justify-content: center; align-items: center; color: #2d3436; padding: 10px; }
  
  .screen { position: fixed; top:0; left:0; width:100%; height:100%; z-index: 99; display:flex; flex-direction:column; justify-content:center; align-items:center; background: rgba(255,255,255,0.99); transition: 0.3s; }
  
  .btn { padding: 18px 40px; border:none; border-radius:15px; font-weight:bold; cursor:pointer; color:white; margin: 10px; font-size: 1.2rem; box-shadow: 0 5px 15px rgba(0,0,0,0.1); }
  .n { background: #6c5ce7; } .c { background: #0984e3; } .s { background: #d63031; }

  #mainApp { display: none; width: 100%; max-width: 1000px; }
  .card { background: rgba(255,255,255,0.96); border-radius: 30px; padding: 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); position: relative; }
  .back-btn { position: absolute; top: 20px; left: 20px; background: #ff7675; color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; z-index: 10; }

  .layout { display: grid; grid-template-columns: 300px 1fr; gap: 40px; margin-top: 40px; }
  
  .soil-circle { width: 240px; height: 240px; border-radius: 50%; border: 15px solid #dfe6e9; display: flex; flex-direction: column; justify-content: center; align-items: center; margin: 0 auto; background: white; transition: border-color 0.3s; }
  .soil-num { font-size: 4.5rem; font-weight: 900; color: #00b894; line-height: 1; }

  .stats-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; margin-bottom: 25px; }
  .stat-box { background: #f1f2f6; padding: 15px; border-radius: 12px; text-align: center; font-weight: bold; color: #555; }
  .stat-val { display: block; font-size: 1.3rem; color: #0984e3; }

  .mode-container { display: flex; gap: 20px; }
  .mode-card { background:white; padding:40px; border-radius:20px; cursor:pointer; text-align:center; box-shadow:0 10px 30px rgba(0,0,0,0.1); width:200px; border:3px solid transparent; }
  .mode-card:hover { border-color: #00b894; transform: translateY(-5px); }
  
  #panelAuto, #panelManual { display: none; animation: fadeIn 0.5s; }
  .manual-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .p-btn { padding: 30px; border:none; border-radius:15px; font-weight:bold; color:white; font-size:1.3rem; cursor:pointer; width: 100%; }
  .bon { background: #00b894; } .boff { background: #636e72; }

  /* --- POPUP STYLES --- */
  #cityPopup, #historyPopup { display: none; position: fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:999; justify-content:center; align-items:center; }
  .popup-box { background:white; padding:25px; border-radius:20px; width:300px; text-align:center; max-height:80vh; overflow-y:auto; box-shadow: 0 10px 40px rgba(0,0,0,0.2); }
  .city-opt { display:block; width:100%; padding:12px; border:1px solid #eee; background:white; margin-bottom:5px; border-radius:8px; cursor:pointer; font-weight:bold; transition: 0.2s; }
  .city-opt:hover { background:#f0f0f0; color:#0984e3; transform: translateX(5px); }
  
  /* --- HISTORY STYLES --- */
  .hist-btn { display:inline-block; margin-top:10px; background:#2d3436; color:white; padding:10px 20px; border-radius:8px; font-weight:bold; border:none; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0, 0.3); transition: 0.2s; }
  .hist-btn:hover { background:#000; transform: translateY(-2px); }
  #dateInput { padding: 10px; border: 2px solid #dfe6e9; border-radius: 8px; font-family: 'Nunito', sans-serif; font-weight: bold; color: #2d3436; width: 100%; box-sizing: border-box; margin-bottom: 15px; }
  .table-container { max-height: 350px; overflow-y: auto; border-radius: 8px; border: 1px solid #eee; }
  .hist-table { width: 100%; font-size: 0.9rem; border-collapse: collapse; text-align: left; }
  .hist-table th { position: sticky; top: 0; background: #f1f2f6; color: #636e72; padding: 12px; font-weight: 800; z-index: 1; }
  .hist-table td { padding: 12px; border-bottom: 1px solid #eee; color: #2d3436; vertical-align: middle; }
  .badge { padding: 5px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 800; display: inline-block; min-width: 60px; text-align: center; }
  .bg-green { background: #e3f9e5; color: #00b894; border: 1px solid #00b894; }
  .bg-red { background: #ffebeb; color: #d63031; border: 1px solid #d63031; }
  .bg-blue { background: #e3f2fd; color: #0984e3; border: 1px solid #0984e3; }
  .bg-purple { background: #f3e5f5; color: #9c27b0; border: 1px solid #9c27b0; }

  @keyframes fadeIn { from{opacity:0} to{opacity:1} }
  .emergency { animation: blink 1s infinite; border-color: red !important; } @keyframes blink { 50% { opacity: 0.5; } }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } .mode-container { flex-direction:column; } }
</style>
</head>
<body>

<div id="screenRegion" class="screen">
  <h1>🌱 Smart Garden (Mongo)</h1>
  <p>Bước 1: Chọn khu vực</p>
  <div>
    <button class="btn n" onclick="emit('select_region', {region:'NORTH'})">Miền Bắc</button>
    <button class="btn c" onclick="emit('select_region', {region:'CENTRAL'})">Miền Trung</button>
    <button class="btn s" onclick="emit('select_region', {region:'SOUTH'})">Miền Nam</button>
  </div>
</div>

<div id="screenMode" class="screen" style="display:none">
  <h1>Chọn Chế Độ</h1>
  <div class="mode-container">
    <div class="mode-card" onclick="emit('enter_mode', {mode:'AUTO'})">
        <div style="font-size:3rem">🤖</div>
        <h3>AI AUTO</h3>
        <p>Tự động 100%</p>
    </div>
    <div class="mode-card" onclick="emit('enter_mode', {mode:'MANUAL'})">
        <div style="font-size:3rem">🎮</div>
        <h3>THỦ CÔNG</h3>
        <p>Tự bấm nút</p>
    </div>
  </div>
</div>

<div id="mainApp">
  <div class="card">
    <button class="back-btn" onclick="exit()">⬅ Menu</button>
    <div class="layout">
      <div style="text-align:center">
        <div class="soil-circle" id="soilBox">
          <span class="soil-num" id="soil">--</span><span>% ĐẤT</span>
        </div>
        <div style="margin-top:20px; font-weight:bold; font-size:1.4rem" id="pumpSt">BƠM TẮT</div>
        <div id="warn" style="color:red; font-weight:bold; margin-top:10px; min-height:20px"></div>
        <button onclick="openHistory()" class="hist-btn">📅 Lịch sử (MongoDB)</button>
      </div>

      <div>
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:15px">
             <h2 style="margin:0" id="loc">...</h2>
             <button onclick="openCityPopup()" style="background:#eee; border:none; padding:8px 15px; border-radius:20px; cursor:pointer;">✎ Đổi chỗ</button>
        </div>
        <div class="stats-grid">
           <div class="stat-box"><span class="stat-val" id="temp">--</span>Nhiệt độ</div>
           <div class="stat-box"><span class="stat-val" id="hum">--</span>Độ ẩm</div>
           <div class="stat-box"><span class="stat-val" id="rain">--</span>Mưa</div>
        </div>
        <div id="panelAuto">
           <div style="background:#e3f2fd; padding:25px; border-radius:20px; border-left:6px solid #0984e3">
               <h3>🤖 AI Control</h3>
               <p>🕒 <b>Lần tưới kế:</b> <span id="aiTime" style="color:#d63031; font-weight:bold">...</span></p>
               <p>💡 <i><span id="aiReason">...</span></i></p>
           </div>
        </div>
        <div id="panelManual">
           <div class="manual-grid">
              <button class="p-btn bon" onclick="pump(true)">BẬT BƠM</button>
              <button class="p-btn boff" onclick="pump(false)">TẮT BƠM</button>
           </div>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="cityPopup" onclick="if(event.target==this) this.style.display='none'">
    <div class="popup-box">
        <h3>Chọn Tỉnh Thành</h3>
        <div id="cityList"></div>
        <button onclick="document.getElementById('cityPopup').style.display='none'" style="margin-top:15px; padding:10px;">Đóng</button>
    </div>
</div>

<div id="historyPopup" onclick="if(event.target==this) this.style.display='none'">
    <div class="popup-box" style="width: 600px; max-width: 95%;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px">
            <h3 style="margin:0">📜 Nhật Ký Hoạt Động</h3>
            <button onclick="document.getElementById('historyPopup').style.display='none'" style="border:none; background:none; font-size:1.2rem; cursor:pointer">✖</button>
        </div>
        <input type="date" id="dateInput" onchange="loadHistory()">
        <div class="table-container">
            <table class="hist-table">
                <thead><tr><th>Giờ</th><th>Hành động</th><th>Chi tiết</th></tr></thead>
                <tbody id="histBody"></tbody>
            </table>
        </div>
    </div>
</div>

<script>
  // CONFIG MQTT
  const BROKER_URL = 'wss://broker.hivemq.com:8884/mqtt';
  const PREFIX = "thaocute_smartgarden/";
  const client = mqtt.connect(BROKER_URL);

  const REGIONAL_CITIES = {
    'NORTH': ["Hà Nội", "Hải Phòng", "Bắc Ninh", "Hưng Yên", "Nam Định", "Lào Cai"],
    'CENTRAL': ["Đà Nẵng", "Huế", "Nha Trang", "Vinh", "Đà Lạt"],
    'SOUTH': ["TP.HCM", "Cần Thơ", "Vũng Tàu", "Bình Dương", "Cà Mau"]
  };
  let currentRegion = 'NORTH';

  client.on('connect', () => { console.log("MQTT Connected"); client.subscribe(PREFIX + 'update'); });
  client.on('message', (topic, msg) => { if (topic === PREFIX + 'update') { updateUI(JSON.parse(msg.toString())); } });

  function updateUI(d) {
      currentRegion = d.region; 
      document.getElementById('screenRegion').style.display = (d.step === 0) ? 'flex' : 'none';
      document.getElementById('screenMode').style.display   = (d.step === 1) ? 'flex' : 'none';
      document.getElementById('mainApp').style.display      = (d.step === 2) ? 'block' : 'none';
      document.getElementById('loc').innerText = d.location;
      document.getElementById('temp').innerText = d.temp + "°";
      document.getElementById('hum').innerText = d.humidity + "%";
      document.getElementById('rain').innerText = d.rain;
      document.getElementById('soil').innerText = d.soil;
      document.getElementById('warn').innerText = d.warning;
      if (d.step === 2) {
          document.getElementById('panelAuto').style.display = (d.mode === 'AUTO') ? 'block' : 'none';
          document.getElementById('panelManual').style.display = (d.mode === 'MANUAL') ? 'block' : 'none';
          if(d.mode === 'AUTO') {
             document.getElementById('aiTime').innerText = d.ai_timing;
             document.getElementById('aiReason').innerText = d.ai_reason;
          }
      }
      const box = document.getElementById('soilBox');
      if(d.soil < 26) { box.classList.add('emergency'); box.style.borderColor='red'; }
      else { box.classList.remove('emergency'); box.style.borderColor='#dfe6e9'; }
      const pst = document.getElementById('pumpSt');
      if(d.pump) { pst.innerText = "ĐANG BƠM..."; pst.style.color="#00b894"; }
      else { pst.innerText = "MÁY BƠM TẮT"; pst.style.color="#636e72"; }
  }

  function emit(ev, d) { client.publish(PREFIX + 'events', JSON.stringify({event: ev, data: d})); }
  function pump(st) { emit('user_control', {pump: st}); }
  function exit() { if(confirm("Thoát? Bơm sẽ TẮT.")) emit('exit_dashboard', {}); }
  
  function openCityPopup() {
     const list = document.getElementById('cityList'); list.innerHTML = "";
     (REGIONAL_CITIES[currentRegion] || []).forEach(c => {
         let btn = document.createElement('button'); btn.className = 'city-opt'; btn.innerText = c;
         btn.onclick = () => { emit('set_city', {city: c}); document.getElementById('cityPopup').style.display = 'none'; };
         list.appendChild(btn);
     });
     document.getElementById('cityPopup').style.display = 'flex';
  }

  function openHistory() { 
      document.getElementById('historyPopup').style.display = 'flex'; 
      document.getElementById('dateInput').valueAsDate = new Date(); 
      loadHistory(); 
  }

  // --- HÀM LẤY DỮ LIỆU TỪ MONGODB QUA API PYTHON ---
  function loadHistory() {
      const date = document.getElementById('dateInput').value; 
      const tbody = document.getElementById('histBody');
      tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding:20px; color:#888">⏳ Đang tải từ MongoDB...</td></tr>';
      
      // Gọi API Flask thay vì Firebase
      fetch('/api/history?date=' + date)
        .then(response => response.json())
        .then(data => {
          tbody.innerHTML = '';
          if(!data || data.length === 0) { 
              tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding:20px">📭 Không có dữ liệu hôm nay</td></tr>'; 
              return; 
          }
          data.forEach(item => {
              let badgeClass = 'bg-blue';
              if(item.action.includes('ON') || item.action.includes('BẬT')) badgeClass = 'bg-green';
              else if(item.action.includes('OFF') || item.action.includes('TẮT')) badgeClass = 'bg-red';
              else if(item.action.includes('MODE')) badgeClass = 'bg-purple';
              
              tbody.innerHTML += `<tr><td><span style="font-weight:700; color:#555">${item.time}</span></td>
                <td><span class="badge ${badgeClass}">${item.action}</span></td>
                <td><div style="font-weight:bold; color:#2d3436">${item.detail}</div><div style="font-size:0.8rem; color:#888">Độ ẩm đất: ${item.soil}%</div></td></tr>`;
          });
        })
        .catch(err => {
            console.error(err);
            tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color:red">Lỗi kết nối Server</td></tr>';
        });
  }
</script>
</body>
</html>
"""

# ====================== 1. CẤU HÌNH BACKEND ======================
GEMINI_API_KEY = os.getenv("GEMINI_KEY", "AIzaSyDnmQNHRgXXPgl-ZhK-Et8EiAW9MjTh-5s").strip()
OPENWEATHER_KEY = os.getenv("OWM_KEY", "5803b3e6056e6886cfa874414788f232")
MONGO_URI = os.getenv("MONGO_URI") # Lấy chuỗi kết nối từ Render

# KẾT NỐI MONGODB
db_collection = None
try:
    if MONGO_URI:
        # Kết nối tới MongoDB Atlas
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_database("smart_garden_db") # Tên Database
        db_collection = db.history # Tên Collection (Bảng)
        print("--- MONGODB ATLAS CONNECTED ---")
    else:
        print("⚠️ CHƯA CÓ MONGO_URI. Lịch sử sẽ không được lưu.")
except Exception as e:
    print(f"❌ Lỗi MongoDB: {e}")

genai.configure(api_key=GEMINI_API_KEY)
try:
    model = genai.GenerativeModel('gemini-1.5-flash')
except: model = None

# CÁC BIẾN LOGIC
CRITICAL_LEVEL = 26 
FLOOD_LEVEL = 90
REGIONAL_DB = {
    'NORTH': {"Hà Nội":(21.02,105.85), "Hải Phòng":(20.86,106.68), "Bắc Ninh":(21.18,106.07), "Hưng Yên":(20.65,106.05), "Nam Định":(20.42,106.16), "Lào Cai":(22.48,103.97)},
    'CENTRAL': {"Đà Nẵng":(16.05,108.20), "Huế":(16.46,107.59), "Nha Trang":(12.23,109.19), "Vinh":(18.67,105.68), "Đà Lạt":(11.94,108.43)},
    'SOUTH': {"TP.HCM":(10.82,106.62), "Cần Thơ":(10.04,105.74), "Vũng Tàu":(10.34,107.08), "Bình Dương":(11.30,106.48), "Cà Mau":(9.17,105.15)}
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

# ====================== CÁC HÀM LOGIC ======================

# HÀM LƯU LỊCH SỬ VÀO MONGODB
def log_event(action, detail):
    if db_collection is None: return
    try:
        # UTC+7 cho Việt Nam (Render server chạy UTC gốc)
        now_vn = datetime.utcnow() + timedelta(hours=7)
        record = {
            "date": now_vn.strftime("%Y-%m-%d"),
            "time": now_vn.strftime("%H:%M:%S"),
            "action": action,
            "detail": detail,
            "soil": state['soil'],
            "created_at": now_vn # Dùng để sort
        }
        db_collection.insert_one(record)
        # print("Đã lưu log vào Mongo")
    except Exception as e:
        print(f"Lỗi lưu log Mongo: {e}")

# API CHO FRONTEND LẤY LỊCH SỬ
@app.route('/api/history')
def get_history():
    date_str = request.args.get('date') # YYYY-MM-DD
    if db_collection is None: return jsonify([])
    
    # Tìm trong MongoDB theo ngày, ẩn trường _id (vì không convert sang JSON được)
    logs = list(db_collection.find(
        {"date": date_str}, 
        {'_id': 0} # Không lấy ID
    ).sort("created_at", -1)) # Sắp xếp mới nhất lên đầu
    
    return jsonify(logs)

def broadcast():
    try: mqtt_client.publish(PREFIX + "update", json.dumps(state, ensure_ascii=False))
    except: pass

def update_weather():
    if not state['lat']: return
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={state['lat']}&lon={state['lon']}&units=metric&appid={OPENWEATHER_KEY}"
        r = requests.get(url, timeout=3).json()
        if r.get('cod') == 200:
            state['temp'] = r['main']['temp']
            state['humidity'] = r['main']['humidity']
            state['rain'] = r.get('rain', {}).get('1h', 0.0)
            if "(Thủ công)" not in state['location'] and "(IP)" not in state['location']:
                 state['location'] = f"{r.get('name', 'Unknown')} (GPS)"
            if state['mode'] == 'AUTO':
                threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
    except: pass
    broadcast()

def auto_locate_ip():
    try:
        r = requests.get('http://ip-api.com/json/', timeout=5).json()
        if r['status'] == 'success':
            state['lat'], state['lon'] = r['lat'], r['lon']
            state['location'] = f"{r.get('city', 'Unknown')} (IP)"
            update_weather()
    except: state['location'] = "Định vị lỗi"
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

    prompt = f"""
    Đóng vai kỹ sư nông nghiệp.
    Dữ liệu: Đất {state['soil']}%, Nhiệt {state['temp']}C, Mưa {state['rain']}mm.
    Khẩn cấp (<26%): {is_emergency}.
    Trả về JSON: {{ "decision": "ON hoặc OFF", "timing": "...", "reason": "..." }}
    """
    try:
        res = model.generate_content(prompt)
        match = re.search(r'\{.*\}', res.text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            dec = data.get('decision', 'OFF').upper()
            state['ai_timing'] = data.get('timing', '...')
            state['ai_reason'] = data.get('reason', '...')
            state['last_ai_call'] = now 
            log_event("AI_AUTO", f"Quyết định: {dec}. {state['ai_reason']}")
            control_pump(dec == 'ON', "AI Logic")
    except Exception as e: print(f"AI Error: {e}")
    broadcast()

def control_pump(on, source="System"):
    if state['step'] != 2 and on: on = False
    if state['soil'] >= FLOOD_LEVEL and on: on = False
    
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
            try:
                val = int(payload.split("H:")[1].split()[0])
                state['soil'] = max(0, min(100, val))
                if state['mode'] == 'AUTO' and state['soil'] < CRITICAL_LEVEL: 
                    threading.Thread(target=ask_gemini, kwargs={'force': False}, daemon=True).start()
                broadcast()
            except: pass
        elif msg.topic == PREFIX + "events":
            d = json.loads(payload); evt = d.get('event'); data = d.get('data', {})
            if evt == 'select_region':
                state['region'] = data['region']; state['step'] = 1
                threading.Thread(target=auto_locate_ip, daemon=True).start()
            elif evt == 'enter_mode':
                state['mode'] = data['mode']; state['step'] = 2
                log_event("MODE_CHANGE", f"Chuyển sang chế độ {state['mode']}")
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

@app.route('/')
def index(): return render_template_string(HTML_CONTENT)

def run_mqtt():
    mqtt_client.on_connect = lambda c,u,f,rc: (c.subscribe(PREFIX + "esp/data"), c.subscribe(PREFIX + "events"), print("--- MQTT READY ---"))
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(BROKER, 1883, 60)
        mqtt_client.loop_forever()
    except: print("Lỗi MQTT")

# 1. Chạy MQTT ngay khi file được load (để Gunicorn cũng chạy được)
threading.Thread(target=run_mqtt, daemon=True).start()

# 2. Block này chỉ dành cho khi chạy thử dưới máy local
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

