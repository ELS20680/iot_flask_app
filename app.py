# app.py
import os
import requests
import psycopg2
from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv

# Load environment variables from .env file
# NOTE: Ensure you have a .env file with AIO_USERNAME, AIO_KEY, and NEON_DATABASE_URL
load_dotenv()

# --- CONFIGURATION ---
app = Flask(__name__)
# Load credentials from .env
AIO_USERNAME = os.getenv("AIO_USERNAME")
AIO_KEY = os.getenv("AIO_KEY")
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")

# Base URL for Adafruit IO API
AIO_HEADERS = {"X-AIO-Key": AIO_KEY}

# Feeds used by the application 
FEEDS = {
    "temperature": "home/temperature",
    "humidity": "home/humidity",
    "motion": "home/motion",
    "ctrl_light": "home/control/light",
    # RENAMED FAN CONTROL TO LCD SCREEN TEXT CONTROL
    "ctrl_lcd_text": "home/control/lcd_message", 
    "ctrl_mode": "home/control/mode",
    "last_image": "home/last_image_ts" 
}

# Database sensor and column mapping (Must match the table created on Neon)
DB_SENSOR_MAP = {
    'temperature': 'temp_c',
    'humidity': 'humidity_pct',
}

# --- HELPER FUNCTIONS: Adafruit IO Interactions ---

def fetch_live_data():
    """Fetches the latest value for Temperature, Humidity, and Motion from Adafruit IO via HTTP."""
    data = {}
    for feed_key in ["temperature", "humidity", "motion"]:
        feed_name = FEEDS.get(feed_key)
        if not feed_name: continue
        
        url = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds/{feed_name}/data/last"
        
        try:
            response = requests.get(url, headers=AIO_HEADERS, timeout=5)
            response.raise_for_status()
            data[feed_key] = response.json().get('value', 'N/A')
        except requests.exceptions.RequestException as e:
            print(f"Error fetching live data for {feed_key}: {e}")
            data[feed_key] = 'Error'
    return data

def send_control_command(feed_key, value):
    """Sends a control command to an actuator feed on Adafruit IO via HTTP POST."""
    feed_name = FEEDS.get(feed_key)
    if not feed_name:
        return False, "Invalid feed key"

    url = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds/{feed_name}/data"
    payload = {"value": value}
    
    try:
        response = requests.post(url, headers=AIO_HEADERS, json=payload, timeout=5)
        response.raise_for_status()
        return True, "Success"
    except requests.exceptions.RequestException as e:
        print(f"Error sending control command to {feed_key}: {e}")
        return False, f"Error: {e}"

# --- HELPER FUNCTIONS: NEON Database Interactions ---

def connect_to_neon():
    """Establishes a connection to the Neon PostgreSQL database."""
    try:
        return psycopg2.connect(NEON_DATABASE_URL)
    except psycopg2.Error as e:
        print(f"DATABASE CONNECTION ERROR: {e}")
        return None

def fetch_historical_data(date, sensor):
    """Fetches historical sensor data for a specific date from NEON (Cloud DB)."""
    conn = connect_to_neon()
    if not conn:
        return None, "Failed to connect to the cloud database."
        
    data = None
    error = None
    
    try:
        cursor = conn.cursor()
        
        if sensor not in DB_SENSOR_MAP:
             error = "Invalid sensor requested."
             return None, error
        
        db_column = DB_SENSOR_MAP[sensor]
        
        # SQL query to retrieve data for the selected date
        query = f"""
        SELECT ts_iso, {db_column} 
        FROM sensor_data 
        WHERE DATE(ts_iso) = %s 
        ORDER BY ts_iso;
        """
        
        cursor.execute(query, (date,))
        results = cursor.fetchall()
        
        # Prepare data for Chart.js
        labels = [row[0].strftime('%H:%M') for row in results] 
        values = [float(row[1]) for row in results] 
        
        data = {
            "labels": labels,
            "datasets": [{
                "label": sensor.capitalize(),
                "data": values,
                "borderColor": "#3498db",
                "tension": 0.3 
            }]
        }
        
    except psycopg2.Error as e:
        print(f"Database query error: {e}")
        error = f"Database Query Error: {e}"
    finally:
        conn.close()
            
    return data, error

def fetch_intrusion_logs(date):
    """Fetches records where motion was detected for a specific date from NEON (Cloud DB)."""
    conn = connect_to_neon()
    if not conn:
        return [], "Failed to connect to the cloud database."
        
    logs = []
    error = None
    
    try:
        cursor = conn.cursor()
        
        query = """
        SELECT ts_iso, image_path
        FROM sensor_data 
        WHERE DATE(ts_iso) = %s AND (motion = TRUE OR motion = 1) AND image_path IS NOT NULL 
        ORDER BY ts_iso DESC;
        """
        
        cursor.execute(query, (date,))
        results = cursor.fetchall()
        
        for ts, path in results:
             logs.append({
                 'timestamp': ts.strftime('%Y-%m-%d %H:%M:%S'),
                 'image_path': path or "No image recorded" 
             })
        
    except psycopg2.Error as e:
        print(f"Database query error: {e}")
        error = f"Database Query Error: {e}"
    finally:
        conn.close()
            
    return logs, error 


# --- ROUTES: The 5 Required Pages ---

@app.route('/')
def home():
    """Route 1: Home page/Main Dashboard. Shows live data."""
    live_data = fetch_live_data()
    return render_template('home.html', live_data=live_data)

@app.route('/about')
def about():
    """Route 2: About page."""
    return render_template('about.html')

@app.route('/environmental', methods=['GET', 'POST'])
def environmental_data():
    """Route 3: Environmental Data page. Handles historical data selection and plotting."""
    chart_data_json = None
    error = None
    selected_date = None
    selected_sensor = None
    
    if request.method == 'POST':
        selected_date = request.form.get('date')
        selected_sensor = request.form.get('sensor')
        
        if selected_date and selected_sensor:
            chart_data, error = fetch_historical_data(selected_date, selected_sensor)
            
            if chart_data:
                chart_data_json = jsonify(chart_data).get_data(as_text=True)
            
    return render_template('environmental.html', 
                           chart_data=chart_data_json, 
                           error=error,
                           selected_date=selected_date,
                           selected_sensor=selected_sensor)


@app.route('/manage-security', methods=['GET', 'POST'])
def manage_security():
    """Route 4: Manage Security page. Handles arm/disarm and intrusion log fetching."""
    status_msg = None
    intrusion_logs = None
    log_error = None
    selected_log_date = None
    
    if request.method == 'POST':
        action = request.form.get('action') 
        
        if action == 'arm':
            success, msg = send_control_command('ctrl_mode', 'ARMED')
            status_msg = f"Security System: {'ARMED' if success else 'Failed to Arm'}. Message: {msg}"
        elif action == 'disarm':
            success, msg = send_control_command('ctrl_mode', 'DISARMED')
            status_msg = f"Security System: {'DISARMED' if success else 'Failed to Disarm'}. Message: {msg}"
        elif action == 'get_logs':
            selected_log_date = request.form.get('log_date')
            if selected_log_date:
                intrusion_logs, log_error = fetch_intrusion_logs(selected_log_date)
            else:
                 log_error = "Please select a date to fetch logs."
    
    if intrusion_logs is None:
        intrusion_logs = []

    return render_template('manage_security.html', 
                           status_msg=status_msg, 
                           intrusion_logs=intrusion_logs,
                           log_error=log_error,
                           selected_log_date=selected_log_date)


@app.route('/device-control')
def device_control():
    """Route 5: Device Control page. Allows controlling 3+ devices."""
    return render_template('device_control.html')

# --- API ENDPOINT for Device Control (Called by JS in device_control.html) ---

@app.route('/api/control/<device>/<value>', methods=['POST'])
def control_device_api(device, value):
    """API: Sends command to the specified device/state/value via Adafruit IO."""
    
    feed_key_map = {
        'light': 'ctrl_light',
        'lcd_text': 'ctrl_lcd_text', # New feed map
        'mode': 'ctrl_mode', 
    }
    
    feed_key = feed_key_map.get(device)
    
    if not feed_key:
        return jsonify({"success": False, "message": "Invalid device"}), 400

    # Handle LCD Text input validation
    if device == 'lcd_text':
        MAX_LENGTH = 32
        if len(value) > MAX_LENGTH:
             return jsonify({"success": False, "message": f"Text must be {MAX_LENGTH} characters or less."}), 400
        # For LCD, the value is the text itself
        control_value = value
        
    elif device == 'mode':
        control_value = 'ARMED' if value.lower() == 'on' else 'DISARMED'
        
    else: # Light (ON/OFF)
        control_value = value.upper()

    success, msg = send_control_command(feed_key, control_value)
    
    if success:
        return jsonify({"success": True, "message": f"{device.replace('_', ' ').capitalize()} set to '{control_value}'" if device == 'lcd_text' else f"{device.capitalize()} set to {control_value}"})
    else:
        return jsonify({"success": False, "message": f"Failed to control {device}: {msg}"}), 500

# --- MAIN RUN BLOCK ---

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')