import os
import re
import time
import threading
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- PORTAL CREDENTIALS & URLS ---
PORTAL_BASE_URL = os.environ.get('PORTAL_URL', 'https://your-sms-portal.com')
PORTAL_LOGIN_URL = f"{PORTAL_BASE_URL}/login"
PORTAL_DATA_URL = f"{PORTAL_BASE_URL}/api/messages"  # Ya portal ka main dashboard page

PORTAL_USERNAME = os.environ.get('PORTAL_USER', 'your_username')
PORTAL_PASSWORD = os.environ.get('PORTAL_PASS', 'your_password')

# Persistent Requests Session (Cookies & Auth Headers auto-manage honge)
portal_session = requests.Session()
portal_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

def login_to_portal():
    """Handles Portal Login and Session Maintenance."""
    try:
        login_payload = {
            'username': PORTAL_USERNAME,
            'password': PORTAL_PASSWORD
        }
        response = portal_session.post(PORTAL_LOGIN_URL, data=login_payload, timeout=15)
        
        if response.status_code == 200 or 'dashboard' in response.url.lower():
            print("[PORTAL] Successfully Authenticated & Logged In!")
            return True
        else:
            print(f"[PORTAL] Login failed with status code: {response.status_code}")
            return False
    except Exception as e:
        print(f"[PORTAL] Login Exception: {e}")
        return False

def fetch_live_portal_messages():
    """Fetches SMS data from portal. Auto re-logins if session expires."""
    try:
        response = portal_session.get(PORTAL_DATA_URL, timeout=15)
        
        # Agar session expire ho jaye (401/403 Error ya login page par redirect)
        if response.status_code in [401, 403] or 'login' in response.url.lower():
            print("[PORTAL] Session expired. Re-authenticating...")
            if login_to_portal():
                response = portal_session.get(PORTAL_DATA_URL, timeout=15)

        if response.status_code == 200:
            # Agar Portal JSON API return karta hai:
            return response.json()
            
            # Agar HTML return karta hai, toh BeautifulSoup se parse kar sakte hain:
            # soup = BeautifulSoup(response.text, 'html.parser')
            # return parse_html_table(soup)
            
    except Exception as e:
        print(f"[PORTAL] Fetch error: {e}")
    return []

# Initial Boot-up Login
login_to_portal()
