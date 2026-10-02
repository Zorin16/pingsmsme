import os
import re
import sys
import json
import time
import threading
from datetime import datetime, timedelta
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------- CONFIG
PORTAL_BASE_URL = "http://135.125.222.224/ints"
LOGIN_URL = f"{PORTAL_BASE_URL}/login"
INBOX_URL = f"{PORTAL_BASE_URL}/client/SMSCDRStats"
DATA_URL = f"{PORTAL_BASE_URL}/client/res/data_smscdr.php"

# Credentials must be set via Koyeb Environment Variables
USERNAME = os.environ.get("PORTAL_USER", "").strip()
PASSWORD = os.environ.get("PORTAL_PASS", "").strip()
APP_URL = os.environ.get("APP_URL", "").strip()  # e.g., https://your-app.koyeb.app

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
})

portal_lock = threading.Lock()
served_rows = {}
portal_info = {"total": None}
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")

# ---------------------------------------------------------------- KEEP ALIVE PINGER
def start_keep_alive():
    """Pings the server every 4 minutes to prevent Koyeb scale-to-zero sleep."""
    if not APP_URL:
        print("[Keep-Alive] APP_URL not set in environment variables. Self-ping disabled.")
        return

    health_url = f"{APP_URL.rstrip('/')}/health"
    print(f"[Keep-Alive] Started ping loop targeting: {health_url}")

    def ping_loop():
        while True:
            time.sleep(240)  # Ping every 4 minutes (240 seconds)
            try:
                res = requests.get(health_url, timeout=10)
                print(f"[Keep-Alive] Self-ping status: {res.status_code}")
            except Exception as err:
                print(f"[Keep-Alive] Ping failed: {err}")

    t = threading.Thread(target=ping_loop, daemon=True)
    t.start()

# ---------------------------------------------------------------- LOGIN & HELPERS
def solve_math_captcha(text):
    match = re.search(r"(\d{1,2})\s*\+\s*(\d{1,2})", text)
    if match:
        return int(match.group(1)) + int(match.group(2))
    return None

def build_login_request(html):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return None, None
    captcha = solve_math_captcha(form.get_text(" ")) or solve_math_captcha(soup.get_text(" "))
    if captcha is None:
        return None, None
    payload = {}
    username_set = False
    for inp in form.find_all("input"):
        name = inp.get("name")
        itype = (inp.get("type") or "text").lower()
        if not name or itype in ("submit", "button", "image", "checkbox", "radio"):
            continue
        lname = name.lower()
        if itype == "password":
            payload[name] = PASSWORD
        elif itype == "hidden":
            payload[name] = inp.get("value", "")
        elif any(k in lname for k in ("capt", "answer", "math")):
            payload[name] = str(captcha)
        elif not username_set:
            payload[name] = USERNAME
            username_set = True
        else:
            payload[name] = str(captcha)
    action = urljoin(LOGIN_URL, form.get("action") or LOGIN_URL)
    return action, payload

def is_logged_in():
    try:
        res = session.get(INBOX_URL, timeout=10)
    except requests.RequestException:
        return False
    if res.status_code in (401, 403) or "login" in res.url.lower():
        return False
    page = BeautifulSoup(res.text, "html.parser")
    if page.find("input", {"type": "password"}):
        return False
    return True

def login_to_portal():
    if not USERNAME or not PASSWORD:
        print("[ERROR] PORTAL_USER or PORTAL_PASS environment variables are missing.")
        return False
    try:
        response = session.get(LOGIN_URL, timeout=10)
        if response.status_code != 200:
            print("Failed to load login page")
            return False
        action, payload = build_login_request(response.text)
        if not payload:
            print("Could not read login form or solve CAPTCHA")
            return False
        session.post(action, data=payload, timeout=10)
        ok = is_logged_in()
        print("Login successful!" if ok else "Login failed (credentials/CAPTCHA issue)")
        return ok
    except Exception as e:
        print(f"Login Exception: {e}")
        return False

# ---------------------------------------------------------------- DATA FETCH
def build_params():
    now = datetime.now()
    d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    d2 = (now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")
    params = {
        "fdate1": d1, "fdate2": d2,
        "frange": "", "fnum": "", "fcli": "",
        "fgdate": "", "fgmonth": "", "fgrange": "",
        "fgnumber": "", "fgcli": "", "fg": 0,
        "sEcho": 1, "iColumns": 7, "sColumns": ",,,,,,",
        "iDisplayStart": 0, "iDisplayLength": 500,
        "sSearch": "", "bRegex": "false",
        "iSortCol_0": 0, "sSortDir_0": "desc", "iSortingCols": 1,
        "_": int(now.timestamp() * 1000),
    }
    for i in range(7):
        params.update({
            f"mDataProp_{i}": i, f"sSearch_{i}": "",
            f"bRegex_{i}": "false", f"bSearchable_{i}": "true",
            f"bSortable_{i}": "true",
        })
    return params

def fetch_rows():
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": INBOX_URL,
        "Accept": "application/json, text/javascript, */*; q=0.01"
    }
    with portal_lock:
        for attempt in range(3):
            try:
                session.get(INBOX_URL, timeout=10)
                r = session.get(DATA_URL, params=build_params(), headers=headers, timeout=15)
                if "login" in r.url.lower() or r.text.strip().startswith("<!DOCTYPE") or r.text.strip().startswith("<html"):
                    raise ValueError("Session expired, got HTML")
                data = r.json()
                rows = data.get("aaData", [])
                portal_info["total"] = data.get("iTotalRecords")
                return rows
            except Exception as e:
                print(f"[ERROR] Fetch failed (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    if not login_to_portal():
                        return None
                else:
                    return None
        return None

# ---------------------------------------------------------------- OTP PARSING
ALNUM_TOKEN = re.compile(r"(?<![\w/@.])([A-Za-z0-9]{4,8})(?![\w@/]|\.\w)")
OTP_KEYWORD = re.compile(r"otp|code|pin|verification|password|passcode", re.I)

def extract_alnum_otp(message):
    cands = [(m.start(), m.group(1)) for m in ALNUM_TOKEN.finditer(message)]
    mixed = [(p, t) for p, t in cands if re.search(r"\d", t) and re.search(r"[A-Za-z]", t)]
    if not mixed:
        return None
    kw = OTP_KEYWORD.search(message)
    if kw:
        for p, t in cands:
            if p >= kw.end() and re.search(r"\d", t):
                return t
    return mixed[0][1]

def extract_otp(message, alnum=False):
    if alnum:
        found = extract_alnum_otp(message)
        if found:
            return found
    keyword = re.search(r"(?:otp|code|pin|verification)\D{0,25}(\d{4,8})", message, re.I)
    if keyword:
        return keyword.group(1)
    reverse = re.search(r"(\d{4,8})\D{0,25}(?:is your|otp|code)", message, re.I)
    if reverse:
        return reverse.group(1)
    dashed = re.search(r"\b(\d{3})-(\d{3})\b", message)
    if dashed:
        return dashed.group(1) + dashed.group(2)
    plain = re.search(r"\b\d{4,6}\b", message)
    return plain.group(0) if plain else None

def ph_national(number):
    d = re.sub(r"\D", "", str(number))
    if d.startswith("63") and len(d) >= 12:
        d = d[2:]
    elif d.startswith("0") and len(d) >= 11:
        d = d[1:]
    return d[-10:] if len(d) > 10 else d

def row_matches_phone(cells, target):
    for cell in cells:
        token = re.sub(r"[\s+\-().]", "", cell)
        if re.fullmatch(r"[\d*xX#]{9,15}", token):
            if token.startswith("63") and len(token) >= 12:
                token = token[2:]
            elif token.startswith("0") and len(token) >= 11:
                token = token[1:]
            token = token[-10:]
            if len(token) == len(target):
                real = [(a, b) for a, b in zip(token, target) if a not in "*xX#"]
                if len(real) >= 6 and all(a == b for a, b in real):
                    return True
    return target in re.sub(r"\D", "", " ".join(cells))

def find_new_otp(rows, target_digits, mark_only=False):
    seen = served_rows.setdefault(target_digits, set())
    candidates = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        cells = [BeautifulSoup(str(c), "html.parser").get_text(" ", strip=True) for c in row]
        row_text = " ".join(cells)
        if not row_matches_phone(cells, target_digits):
            continue
        row_key = row_text
        if mark_only:
            seen.add(row_key)
            continue
        if row_key in seen:
            continue
        stamp = TIMESTAMP_RE.search(row_text)
        message = max(cells, key=len)
        otp = extract_otp(message)
        if otp:
            candidates.append((stamp.group(0) if stamp else "", otp, row_key))
    if not candidates:
        return None
    best = max(enumerate(candidates), key=lambda x: (x[1][0], -x[0]))[1]
    seen.add(best[2])
    return best[1]

# ---------------------------------------------------------------- CORS & ROUTES
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200

@app.route("/get-otp", methods=["GET"])
def get_otp():
    phone_param = request.args.get("phone", "").strip()
    target_digits = ph_national(phone_param)
    if not target_digits:
        return jsonify({"error": "Phone parameter required"}), 400
    rows = fetch_rows()
    if rows is None:
        return jsonify({"error": "Failed to log into SMS portal"}), 500
    try:
        otp = find_new_otp(rows, target_digits)
    except Exception as e:
        return jsonify({"error": f"Error parsing rows: {e}"}), 500
    return jsonify({"phone": phone_param, "otp": otp, "rows_seen": len(rows)})

@app.route("/mark-seen", methods=["GET"])
def mark_seen():
    phone_param = request.args.get("phone", "").strip()
    target_digits = ph_national(phone_param)
    if not target_digits:
        return jsonify({"error": "Phone parameter required"}), 400
    rows = fetch_rows()
    if rows is None:
        return jsonify({"error": "Failed to log into SMS portal"}), 500
    find_new_otp(rows, target_digits, mark_only=True)
    return jsonify({"phone": phone_param, "marked": True})

def extract_number(cells):
    for cell in cells:
        token = re.sub(r"[\s+\-().]", "", cell)
        if re.fullmatch(r"[\d*xX#]{9,15}", token):
            if token.startswith("63") and len(token) >= 12:
                token = token[2:]
            elif token.startswith("0") and len(token) >= 11:
                token = token[1:]
            return "+63" + token[-10:]
    return None

TZ_OFFSET_HOURS = float(os.environ.get("PORTAL_TZ_OFFSET_HOURS", "0"))

def compute_stats(rows, alnum=False):
    today = (datetime.now() + timedelta(hours=TZ_OFFSET_HOURS)).strftime("%Y-%m-%d")
    data = {}
    total = 0
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        cells = [BeautifulSoup(str(c), "html.parser").get_text(" ", strip=True) for c in row]
        stamp_m = TIMESTAMP_RE.search(" ".join(cells))
        stamp = stamp_m.group(0) if stamp_m else ""
        if stamp and stamp[:10] != today:
            continue
        total += 1
        number = extract_number(cells)
        if not number:
            continue
        entry = data.setdefault(number, [])
        texts = [c for c in cells if not TIMESTAMP_RE.fullmatch(c)] or cells
        otp = extract_otp(max(texts, key=len), alnum)
        if otp:
            entry.append({"otp": otp, "time": stamp[11:19] if stamp else ""})

    numbers = []
    for n, otps in sorted(data.items(), key=lambda kv: -len(kv[1])):
        otps = sorted(otps, key=lambda o: o["time"], reverse=True)
        numbers.append({
            "number": n,
            "country_code": n[:3],
            "local": n[3:],
            "otp_count": len(otps),
            "last_otp": otps[0]["otp"] if otps else None,
            "last_otp_time": otps[0]["time"] if otps else "",
            "otps": otps,
        })
    return {"total_sms": total, "date": today, "alnum": alnum, "numbers": numbers}

@app.route("/stats", methods=["GET"])
def stats():
    rows = fetch_rows()
    if rows is None:
        return jsonify({"error": "Failed to log into SMS portal"}), 500
    return jsonify(compute_stats(rows, request.args.get("alnum") == "1"))

@app.route("/restart", methods=["GET"])
def restart():
    with portal_lock:
        served_rows.clear()
        session.cookies.clear()
        ok = login_to_portal()
    if not ok:
        return jsonify({"error": "Re-login to portal failed"}), 500
    return stats()

if __name__ == "__main__":
    login_to_portal()
    start_keep_alive()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
