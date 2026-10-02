import os
import re
import time
import threading
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- ANTI-SLEEP PING ENGINE FOR RENDER ---
RENDER_APP_URL = os.environ.get('RENDER_EXTERNAL_URL', 'http://127.0.0.1:5000')

def keep_alive():
    """Background thread that pings server every 4 minutes (240s) to prevent Render sleep."""
    while True:
        time.sleep(240)
        try:
            ping_url = f"{RENDER_APP_URL.rstrip('/')}/ping"
            response = requests.get(ping_url, timeout=10)
            print(f"[PINGER] Keep-alive ping sent to {ping_url} | Status: {response.status_code}")
        except Exception as e:
            print(f"[PINGER] Ping failed: {e}")

# Start anti-sleep thread automatically on startup
ping_thread = threading.Thread(target=keep_alive, daemon=True)
ping_thread.start()

# --- CLI PATTERNS MAPPING ---
CLI_PATTERNS = {
    'Microsoft': [r'microsoft', r'msft', r'azure', r'outlook'],
    'Royal Canin': [r'royal\s*canin', r'canin'],
    'Ticketmaster': [r'ticketmaster', r'tkmst'],
    'Google': [r'google', r'g-', r me', r'gmail'],
    'WhatsApp': [r'whatsapp', r'wa-'],
    'Telegram': [r'telegram', r't\.me'],
    'Facebook': [r'facebook', r'fb-'],
    'Amazon': [r'amazon', r'amzn'],
    'Uber': [r'uber'],
    'Apple': [r'apple']
}

def detect_cli(text, raw_sender=''):
    combined = f"{raw_sender} {text}".lower()
    for cli_name, patterns in CLI_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, combined):
                return cli_name
    if raw_sender and raw_sender.strip():
        return raw_sender.strip().title()
    return 'Other'

def extract_code(text):
    match = re.search(r'\b\d{4,8}\b', text)
    return match.group(0) if match else ''

# --- MOCK DATA SOURCE (Connect to your DB / Portal API here) ---
RAW_MESSAGES = [
    {"id": 1, "number": "+923000000001", "sender": "Microsoft", "text": "Your Microsoft verification code is 482019", "timestamp": "2026-10-02 14:20:00"},
    {"id": 2, "number": "+923000000002", "sender": "RoyalCanin", "text": "Royal Canin login OTP is 918234", "timestamp": "2026-10-02 15:10:00"},
    {"id": 3, "number": "+923000000003", "sender": "TicketMaster", "text": "Ticketmaster security code: 334102", "timestamp": "2026-09-15 10:00:00"},
    {"id": 4, "number": "+923000000001", "sender": "Google", "text": "G-554123 is your Google verification code", "timestamp": "2026-09-01 11:30:00"}
]

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "alive", "message": "Server is active"}), 200

@app.route('/api/messages', methods=['GET', 'POST'])
def get_messages():
    params = request.args if request.method == 'GET' else (request.get_json() or {})
    
    cli_filter = params.get('cli', '').strip().lower()
    number_filter = params.get('number', '').strip()
    date_filter = params.get('date', '').strip()
    month_filter = params.get('month', '').strip()
    start_date = params.get('start_date', '').strip()
    end_date = params.get('end_date', '').strip()

    filtered_list = []
    for item in RAW_MESSAGES:
        cli = detect_cli(item.get('text', ''), item.get('sender', ''))
        code = extract_code(item.get('text', ''))
        number = item.get('number', '')
        ts_str = item.get('timestamp', '')

        if cli_filter and cli_filter != 'all' and cli.lower() != cli_filter:
            continue
        if number_filter and number_filter not in number:
            continue
        if date_filter and not ts_str.startswith(date_filter):
            continue
        if month_filter and not ts_str.startswith(month_filter):
            continue
        if start_date and end_date and ts_str[:10]:
            if not (start_date <= ts_str[:10] <= end_date):
                continue

        filtered_list.append({
            "id": item.get('id'),
            "cli": cli,
            "number": number,
            "code": code,
            "full_text": item.get('text'),
            "timestamp": ts_str
        })

    return jsonify({
        "status": "success",
        "total": len(filtered_list),
        "data": filtered_list
    })

@app.route('/api/numbers', methods=['GET', 'POST'])
def get_numbers_range():
    """Generates / retrieves numbers based on range."""
    params = request.args if request.method == 'GET' else (request.get_json() or {})
    start_num = params.get('start', '').strip()
    end_num = params.get('end', '').strip()

    result_numbers = []
    
    if start_num and end_num:
        try:
            # Numeric range extraction
            s_val = int(re.sub(r'\D', '', start_num))
            e_val = int(re.sub(r'\D', '', end_num))
            
            # Limit max range return to prevent memory overload
            count = 0
            for curr in range(s_val, e_val + 1):
                if count >= 100:
                    break
                num_str = f"+{curr}"
                # Find matching service activity if available
                active_msg = next((m for m in RAW_MESSAGES if m['number'] == num_str), None)
                status = f"Active - {detect_cli(active_msg['text'], active_msg['sender'])}" if active_msg else "Idle"
                
                result_numbers.append({
                    "number": num_str,
                    "status": status
                })
                count += 1
        except Exception:
            pass

    if not result_numbers:
        # Default active numbers response
        for item in RAW_MESSAGES:
            cli = detect_cli(item.get('text'), item.get('sender'))
            result_numbers.append({
                "number": item.get('number'),
                "status": f"Active - {cli}"
            })

    return jsonify({
        "status": "success",
        "total": len(result_numbers),
        "numbers": result_numbers
    })

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Returns SMS Statistics."""
    total_sms = len(RAW_MESSAGES)
    cli_counts = {}
    for item in RAW_MESSAGES:
        cli = detect_cli(item.get('text'), item.get('sender'))
        cli_counts[cli] = cli_counts.get(cli, 0) + 1

    return jsonify({
        "status": "success",
        "total_sms": total_sms,
        "today_sms": total_sms,
        "active_clis_count": len(cli_counts),
        "success_rate": "98%",
        "cli_breakdown": cli_counts
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
