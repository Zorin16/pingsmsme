import os
import re
import json
from datetime import datetime, timedelta
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

app = Flask(__name__)

PORTAL_BASE_URL = "http://135.125.222.224/ints"
LOGIN_URL = f"{PORTAL_BASE_URL}/login"
INBOX_URL = f"{PORTAL_BASE_URL}/client/SMSCDRStats"
DATA_URL = f"{PORTAL_BASE_URL}/client/res/data_smscdr.php"

TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
ALNUM_TOKEN = re.compile(r"(?<![\w/@.])([A-Za-z0-9]{4,8})(?![\w@/]|\.\w)")
OTP_KEYWORD = re.compile(r"otp|code|pin|verification|password|passcode", re.I)


def solve_math_captcha(text):
    match = re.search(r"(\d{1,2})\s*\+\s*(\d{1,2})", text)
    if match:
        return int(match.group(1)) + int(match.group(2))
    return None


def build_login_request(html, username, password):
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
            payload[name] = password
        elif itype == "hidden":
            payload[name] = inp.get("value", "")
        elif any(k in lname for k in ("capt", "answer", "math")):
            payload[name] = str(captcha)
        elif not username_set:
            payload[name] = username
            username_set = True
        else:
            payload[name] = str(captcha)
    action = urljoin(LOGIN_URL, form.get("action") or LOGIN_URL)
    return action, payload


def create_portal_session(username, password):
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })
    try:
        res = session.get(LOGIN_URL, timeout=10)
        if res.status_code != 200:
            return None, "SMS Portal unreachable"
        action, payload = build_login_request(res.text, username, password)
        if not payload:
            return None, "Could not solve portal CAPTCHA"
        
        session.post(action, data=payload, timeout=10)
        
        inbox_res = session.get(INBOX_URL, timeout=10)
        if inbox_res.status_code in (401, 403) or "login" in inbox_res.url.lower():
            return None, "Invalid username or password"
        if BeautifulSoup(inbox_res.text, "html.parser").find("input", {"type": "password"}):
            return None, "Invalid username or password"
            
        return session, None
    except Exception as e:
        return None, f"Portal Connection Error: {str(e)}"


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


def fetch_user_rows(session):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": INBOX_URL,
        "Accept": "application/json, text/javascript, */*; q=0.01"
    }
    try:
        session.get(INBOX_URL, timeout=10)
        r = session.get(DATA_URL, params=build_params(), headers=headers, timeout=15)
        if "login" in r.url.lower() or r.text.strip().startswith("<!DOCTYPE"):
            return None
        return r.json().get("aaData", [])
    except Exception:
        return None


def extract_otp(message, alnum=False):
    if alnum:
        cands = [(m.start(), m.group(1)) for m in ALNUM_TOKEN.finditer(message)]
        mixed = [(p, t) for p, t in cands if re.search(r"\d", t) and re.search(r"[A-Za-z]", t)]
        if mixed:
            return mixed[0][1]
    keyword = re.search(r"(?:otp|code|pin|verification)\D{0,25}(\d{4,8})", message, re.I)
    if keyword:
        return keyword.group(1)
    plain = re.search(r"\b\d{4,6}\b", message)
    return plain.group(0) if plain else None


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


def compute_stats(rows, alnum=False):
    today = datetime.now().strftime("%Y-%m-%d")
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
            "number": n, "country_code": n[:3], "local": n[3:],
            "otp_count": len(otps), "last_otp": otps[0]["otp"] if otps else None,
            "last_otp_time": otps[0]["time"] if otps else "", "otps": otps,
        })
    return {"total_sms": total, "date": today, "alnum": alnum, "numbers": numbers}


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET"])
@app.route("/stats", methods=["GET"])
def stats():
    username = request.headers.get("X-Portal-User")
    password = request.headers.get("X-Portal-Pass")

    if not username or not password:
        return jsonify({"error": "Portal credentials required. Please enter username and password in extension settings."}), 400

    session, error_msg = create_portal_session(username, password)
    if not session:
        return jsonify({"error": error_msg or "Invalid username or password"}), 401

    rows = fetch_user_rows(session)
    if rows is None:
        return jsonify({"error": "Failed to retrieve data from SMS portal"}), 500

    return jsonify(compute_stats(rows, request.args.get("alnum") == "1"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)