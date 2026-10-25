import os
import sys
import uuid
import json
import base64
import sqlite3
import subprocess
import time
import threading
import socket
import struct
import urllib.parse
import html
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session

try:
    import telebot
except ImportError:
    telebot = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pablo-rail-secret-key-change-me")

ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin")

XRAY_PORT = 10000
XRAY_API_PORT = 10085
FLASK_PORT = 5000

DB_PATH = "users.db"
# Banner shown with the user welcome message (put welcome.jpg next to app.py, or set WELCOME_IMAGE)
WELCOME_IMAGE_PATH = os.environ.get(
    "WELCOME_IMAGE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg")
)
XRAY_CONFIG_PATH = "xray_config.json"
NGINX_CONFIG_PATH = "nginx.conf"

# Telegram bot: channel gate, credits and key settings
TG_CHANNEL = os.environ.get("TG_CHANNEL", "@arsar2027")
TG_CHANNEL_URL = os.environ.get("TG_CHANNEL_URL", "https://t.me/arsar2027")
JOIN_BONUS_CREDITS = 5        # credits for joining the channel
REFERRAL_BONUS_CREDITS = 5    # credits per friend who joins through the invite link
# Key packages: label, credit cost, and one entry per key (size in GB)
KEY_PACKAGES = {
    "p500":   {"label": "500 MB · 1 key",             "cost": 1, "sizes": [0.5]},
    "p1gb":   {"label": "1 GB · 1 key",               "cost": 3, "sizes": [1]},
    "p3gb":   {"label": "3 GB · 1 key",               "cost": 5, "sizes": [3]},
    "p2x250": {"label": "250 MB + 250 MB · 2 keys",   "cost": 5, "sizes": [0.25, 0.25]},
}
PACKAGES_SUMMARY = " · ".join(f"{p['label']} = {p['cost']} credits" for p in KEY_PACKAGES.values())
KEY_COST_CREDITS = min(p["cost"] for p in KEY_PACKAGES.values())  # cheapest package, used for auto-refill
KEY_DAYS = 3                  # validity per generated key
CONFIGS_PER_KEY = 1           # one config per key
AUTO_REFILL_CREDITS = 5       # credits given automatically when a user has run out
AUTO_REFILL_DAYS = 3          # ...this many days after their credits ran out
CHANNEL_CHECK_INTERVAL = 300  # seconds between channel-membership checks of key owners

ONLINE_USERS = {}
ONLINE_THRESHOLD = 90

# Website (Cloudflare Worker) က Key အခြေအနေ ဖတ်ဖို့ သုံးတဲ့ token
# Server မှာ environment variable အဖြစ် PANEL_API_TOKEN သတ်မှတ်ပါ (မသတ်ရင် API ပိတ်ထားမယ်)
PANEL_API_TOKEN = os.environ.get("PANEL_API_TOKEN", "")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            uuid TEXT UNIQUE NOT NULL,
            quota_gb REAL DEFAULT 0,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 30,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            chat_id TEXT PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            credits INTEGER DEFAULT 0,
            bonus_given INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            referred_by TEXT DEFAULT '',
            referral_rewarded INTEGER DEFAULT 0,
            refill_at TEXT DEFAULT '',
            created_at TEXT
        )
    """)

    # Older databases: add the key owner column
    c.execute("PRAGMA table_info(users)")
    if "owner_chat_id" not in [row[1] for row in c.fetchall()]:
        c.execute("ALTER TABLE users ADD COLUMN owner_chat_id TEXT DEFAULT ''")

    c.execute("PRAGMA table_info(users)")
    if "channel_locked" not in [row[1] for row in c.fetchall()]:
        # 1 = key switched off automatically because its owner left the channel
        c.execute("ALTER TABLE users ADD COLUMN channel_locked INTEGER DEFAULT 0")

    c.execute("PRAGMA table_info(bot_users)")
    bot_cols = [row[1] for row in c.fetchall()]
    if "refill_at" not in bot_cols:
        c.execute("ALTER TABLE bot_users ADD COLUMN refill_at TEXT DEFAULT ''")
    if "in_channel" not in bot_cols:
        # -1 = unknown, 0 = left, 1 = joined
        c.execute("ALTER TABLE bot_users ADD COLUMN in_channel INTEGER DEFAULT -1")
    if "channel_checked_at" not in bot_cols:
        c.execute("ALTER TABLE bot_users ADD COLUMN channel_checked_at TEXT DEFAULT ''")

    conn.commit()
    conn.close()

def get_setting(key, default=""):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default

def set_setting(key, value):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("Error saving setting:", e)
        return False

def get_admin_credentials():
    settings_file = "panel_settings.json"
    data = None

    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None

    # A password changed from the panel or the Telegram bot always wins
    if isinstance(data, dict) and data.get("changed"):
        return data.get("username", "admin"), data.get("password", "admin")

    env_user = os.environ.get("ADMIN_USER")
    env_pass = os.environ.get("ADMIN_PASS")
    if env_user is not None and env_pass is not None:
        return env_user, env_pass

    if isinstance(data, dict):
        return data.get("username", "admin"), data.get("password", "admin")

    return "admin", "admin"

def save_admin_credentials(username, password):
    settings_file = "panel_settings.json"

    data = {
        "username": username,
        "password": password,
        "changed": True
    }

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def credentials_version():
    """Changes whenever the admin username/password changes (used to log out old sessions)."""
    username, password = get_admin_credentials()
    return hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()[:16]

@app.before_request
def _invalidate_old_sessions():
    if "admin" in session and session.get("cred_ver") != credentials_version():
        session.clear()

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def is_user_active(user):
    """Check whether the user is active based on traffic, time and overall status"""
    if user["enabled"] == 0:
        return False, "disabled"

    if user.get("channel_locked"):
        return False, "channel_left"

    try:
        created_dt = datetime.fromisoformat(user["created_at"])
        elapsed_days = (datetime.now() - created_dt).days
        if elapsed_days >= user["expire_days"]:
            return False, "expired"
    except Exception:
        pass

    used_gb = user["used_bytes"] / (1024 ** 3)
    if user["quota_gb"] > 0 and used_gb >= user["quota_gb"]:
        return False, "limit_exceeded"

    return True, "active"

def enrich_user(u):
    try:
        created_dt = datetime.fromisoformat(u["created_at"])
        elapsed_days = (datetime.now() - created_dt).days
        days_left = max(0, u["expire_days"] - elapsed_days)
    except Exception:
        days_left = u["expire_days"]

    used_gb = round(u["used_bytes"] / (1024 ** 3), 2)
    quota_gb = round(float(u["quota_gb"]), 2)

    if quota_gb > 0:
        percent = min(100, round((used_gb / quota_gb) * 100, 1))
    else:
        percent = 0

    active_state, reason = is_user_active(u)

    u["used_gb"] = used_gb
    u["days_left"] = days_left
    u["percent"] = percent
    u["is_expired"] = days_left <= 0 or reason == "expired"
    u["is_limit_exceeded"] = reason == "limit_exceeded"
    u["is_active"] = active_state
    u["created_date"] = u["created_at"][:10] if u.get("created_at") else ""
    u["is_online"] = is_user_online(u["name"])

    return u

def is_user_online(name):
    last_seen = ONLINE_USERS.get(name, 0)
    return (time.time() - last_seen) < ONLINE_THRESHOLD

def build_xray_config():
    users = get_all_users()
    clients = []

    for u in users:
        # Only users who are actually active are added to the Xray config
        active, _ = is_user_active(u)
        if active:
            clients.append({
                "id": u["uuid"],
                "email": u["name"],
                "level": 0
            })

    if not clients:
        clients.append({
            "id": str(uuid.uuid4()),
            "email": "default_user",
            "level": 0
        })

    config = {
        "log": {
            "loglevel": "warning"
        },
        "stats": {},
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True
            }
        },
        "inbounds": [
            {
                "tag": "api",
                "port": XRAY_API_PORT,
                "listen": "127.0.0.1",
                "protocol": "dokodemo-door",
                "settings": {
                    "address": "127.0.0.1"
                }
            },
            {
                "tag": "vless-in",
                "port": XRAY_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "settings": {
                    "clients": clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": "/ws"
                    }
                }
            }
        ],
        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "direct"
            }
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api"],
                    "outboundTag": "api"
                }
            ]
        }
    }

    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def is_server_stopped():
    """True when the admin has stopped the whole VPN server from the Telegram bot."""
    return get_setting("server_stopped", "0") == "1"


def is_xray_running():
    try:
        r = subprocess.run(["pgrep", "-f", "xray"], capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def restart_xray():
    build_xray_config()
    try:
        subprocess.run(["pkill", "-9", "-f", "xray"], check=False)
        time.sleep(0.5)
    except Exception:
        pass

    # Admin stopped the server: keep Xray down (also after a redeploy / restart)
    if is_server_stopped():
        print("Xray is stopped by the admin (server_stopped=1), not starting it.")
        return

    try:
        xray_bin = "/usr/local/bin/xray/xray"
        if not os.path.exists(xray_bin):
            xray_bin = "xray"

        subprocess.Popen(
            [xray_bin, "run", "-c", XRAY_CONFIG_PATH],
            stdout=sys.stdout,
            stderr=sys.stderr
        )
    except Exception as e:
        print("Xray start error:", e)

def xray_query_stats():
    stats = {}
    xray_bin = "/usr/local/bin/xray/xray"
    if not os.path.exists(xray_bin):
        xray_bin = "xray"

    try:
        result = subprocess.run(
            [
                xray_bin,
                "api",
                "statsquery",
                f"--server=127.0.0.1:{XRAY_API_PORT}",
                "-pattern=user>>>",
                "-reset=true"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return stats

        data = json.loads(result.stdout)
        stat_list = data.get("stat", [])

        for item in stat_list:
            name = item.get("name", "")
            value = int(item.get("value", 0))

            parts = name.split(">>>")
            if len(parts) >= 4 and parts[0] == "user":
                user_email = parts[1]

                if user_email not in stats:
                    stats[user_email] = 0

                stats[user_email] += value

    except Exception:
        pass

    return stats

def xray_reset_user_stats(user_email):
    xray_bin = "/usr/local/bin/xray/xray"
    if not os.path.exists(xray_bin):
        xray_bin = "xray"

    try:
        for direction in ["uplink", "downlink"]:
            subprocess.run(
                [
                    xray_bin,
                    "api",
                    "statsquery",
                    f"--server=127.0.0.1:{XRAY_API_PORT}",
                    "-reset=true",
                    f"-pattern=user>>>{user_email}>>>traffic>>>{direction}"
                ],
                capture_output=True,
                timeout=3
            )
    except Exception:
        pass

PREVIOUS_STATS = {}

def stats_collector():
    global PREVIOUS_STATS

    while True:
        try:
            time.sleep(15)

            stats = xray_query_stats()

            if not stats:
                continue

            conn = get_db()
            c = conn.cursor()

            need_restart = False

            for user_email, total_bytes in stats.items():
                if total_bytes <= 0:
                    continue

                c.execute(
                    "UPDATE users SET used_bytes = used_bytes + ? WHERE name = ?",
                    (total_bytes, user_email)
                )

                ONLINE_USERS[user_email] = time.time()

                c.execute(
                    "SELECT id, quota_gb, used_bytes, enabled, created_at, expire_days FROM users WHERE name = ?",
                    (user_email,)
                )

                row = c.fetchone()

                if row and row["enabled"] == 1:
                    # We no longer fully deactivate the user in the database (so the subscription is not cut off)
                    # We only evaluate their real active status and, if needed, restart Xray to cut the connection
                    active, reason = is_user_active(dict(row))
                    if not active:
                        need_restart = True
                        print(f"[QUOTA/TIME] User '{user_email}' is no longer active ({reason}). Disabling from Xray.")

            conn.commit()
            conn.close()

            if need_restart:
                restart_xray()

        except Exception as e:
            print("Stats collector error:", e)

def start_stats_collector():
    t = threading.Thread(target=stats_collector, daemon=True)
    t.start()

def start_nginx():
    port = os.environ.get("PORT", "8080")

    nginx_conf = f"""
pid /run/nginx.pid;
error_log /dev/stderr warn;
events {{
    worker_connections 1024;
}}
http {{
    access_log /dev/stdout;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    keepalive_timeout 65;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {port};
        server_name _;
        location ~ ^/ws {{
            proxy_redirect off;
            rewrite ^/ws.*$ /ws break;
            proxy_pass http://127.0.0.1:{XRAY_PORT};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{FLASK_PORT};
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""

    with open(NGINX_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(nginx_conf)

    try:
        subprocess.run(["pkill", "-9", "-f", "nginx"], check=False)
        time.sleep(0.3)
    except Exception:
        pass

    subprocess.Popen([
        "nginx",
        "-c",
        os.path.abspath(NGINX_CONFIG_PATH),
        "-g",
        "daemon off;"
    ])

def make_all_vless_configs(user, host):
    created_dt = datetime.fromisoformat(user["created_at"])
    elapsed_days = (datetime.now() - created_dt).days
    days_left = max(0, user["expire_days"] - elapsed_days)

    used_gb = round(user["used_bytes"] / (1024 ** 3), 2)
    quota_gb = round(float(user["quota_gb"]), 2)
    remaining_gb = max(0.0, round(quota_gb - used_gb, 2))

    u_uuid = user["uuid"]
    name = user["name"]

    def config_remark(number):
        remark_text = (
            f"High Speed Config | "
            f"🌐 𝕋𝕦𝕣𝕓𝕠 𝕋𝕦𝕟𝕟𝕖𝕝 𝕄𝕄 🌐 | "
            f"{number} | "
            f"{name}"
        )
        return urllib.parse.quote(remark_text)

    configs = []

    c1 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(1)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 1",
        "config": c1
    })

    c2 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}%3Fed%3D2560"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(2)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 2",
        "config": c2
    })

    c3 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=h2%2Chttp%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=firefox"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(3)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 3",
        "config": c3
    })

    c4 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(4)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 4",
        "config": c4
    })

    c5 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}%3Fed%3D2048"
        f"&security=tls"
        f"&alpn=h2"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=edge"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(5)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 5",
        "config": c5
    })

    c6 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=opera"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(6)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 6",
        "config": c6
    })

    c7 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}%3Fed%3D2560"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=android"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(7)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 7",
        "config": c7
    })

    c8 = (
        f"vless://{u_uuid}@{host}:80"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=none"
        f"&encryption=none"
        f"&host={host}"
        f"&type=ws"
        f"#{config_remark(8)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 8",
        "config": c8
    })

    c9 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=random"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(9)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 9",
        "config": c9
    })

    c10 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}%3Fhost%3D{host}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{config_remark(10)}"
    )
    configs.append({
        "title": "𝕋𝕦𝕣𝕓𝕠",
        "desc": "𝕋𝕦𝕣𝕓𝕠",
        "tag": "HighSpeed 10",
        "config": c10
    })

    return configs[:CONFIGS_PER_KEY]

def make_fake_config(reason, user, host):
    """Generate a disabled (display-only) connection to notify the end user in their app"""
    u_uuid = str(uuid.uuid4())
    name = user["name"]
    
    if reason == "limit_exceeded":
        remark = f"⛔️ Traffic Limit Exceeded | {name}"
        desc = "Your allowed traffic quota has been used up. Please renew your plan."
    elif reason == "expired":
        remark = f"⛔️ Subscription Expired | {name}"
        desc = "Your subscription time has expired. Please renew your plan."
    elif reason == "channel_left":
        remark = f"⛔️ Join our Telegram channel to unlock | {name}"
        desc = "You left our Telegram channel, so your key was switched off. Join again and it turns back on."
    else:
        remark = f"⛔️ Service Disabled | {name}"
        desc = "Your service has been disabled by the admin."
        
    remark_encoded = urllib.parse.quote(remark)
    
    config_str = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2Fdummy"
        f"&security=tls"
        f"&encryption=none"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&sni={host}"
        f"#{remark_encoded}"
    )
    return [{
        "title": remark,
        "desc": desc,
        "tag": "Alert",
        "config": config_str
    }]

def get_subscription_header(user):
    """Generate the standard usage-monitoring header shown at the top of the user's app"""
    used = int(user["used_bytes"])
    total = int(float(user["quota_gb"]) * (1024 ** 3))
    try:
        created_dt = datetime.fromisoformat(user["created_at"])
        expire_dt = created_dt + timedelta(days=int(user["expire_days"]))
        expire_ts = int(expire_dt.timestamp())
    except Exception:
        expire_ts = 0
    return f"upload=0; download={used}; total={total}; expire={expire_ts}"

@app.route("/")
def home():
    if "admin" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        current_username, current_password = get_admin_credentials()

        if username == current_username and password == current_password:
            session["admin"] = True
            session["cred_ver"] = credentials_version()
            return redirect(url_for("dashboard"))

        return render_template("login.html", error="Incorrect username or password!")

    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "admin" not in session:
        return redirect(url_for("login"))

    users = get_all_users()

    total_gb = sum(u["quota_gb"] for u in users)
    total_used = sum(u["used_bytes"] for u in users) / (1024 ** 3)
    active_count = sum(1 for u in users if is_user_active(u)[0])

    return render_template(
        "dashboard.html",
        users=users,
        total_users=len(users),
        active_users=active_count,
        total_gb=round(total_gb, 2),
        total_used=round(total_used, 2)
    )

@app.route("/users")
def users_page():
    if "admin" not in session:
        return redirect(url_for("login"))

    raw_users = get_all_users()
    users = [enrich_user(u) for u in raw_users]

    total_gb = sum(u["quota_gb"] for u in raw_users)
    total_used = sum(u["used_bytes"] for u in raw_users) / (1024 ** 3)
    active_count = sum(1 for u in users if u["is_active"])
    disabled_count = sum(1 for u in users if u["enabled"] == 0)
    expired_count = sum(1 for u in users if u["is_expired"] or u["is_limit_exceeded"])
    online_count = sum(1 for u in users if u["is_online"])

    return render_template(
        "users.html",
        users=users,
        total_users=len(users),
        active_users=active_count,
        disabled_users=disabled_count,
        expired_users=expired_count,
        online_users=online_count,
        total_gb=round(total_gb, 2),
        total_used=round(total_used, 2)
    )

@app.route("/api/online_users")
def api_online_users():
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    raw_users = get_all_users()
    online = []

    for u in raw_users:
        if is_user_online(u["name"]):
            last_seen = ONLINE_USERS.get(u["name"], 0)
            seconds_ago = int(time.time() - last_seen)

            online.append({
                "id": u["id"],
                "name": u["name"],
                "used_gb": round(u["used_bytes"] / (1024 ** 3), 2),
                "quota_gb": u["quota_gb"],
                "seconds_ago": seconds_ago
            })

    return jsonify({
        "status": "success",
        "count": len(online),
        "users": online
    })

@app.route("/api/public/status")
def api_public_status():
    """Website ကနေ token နဲ့ ခေါ်ပြီး Key တစ်ခုချင်းစီရဲ့ online/data/သက်တမ်း ကိုဖတ်ဖို့"""
    token = request.headers.get("X-Api-Token", "")
    if not PANEL_API_TOKEN or not hmac.compare_digest(token.encode("utf-8"), PANEL_API_TOKEN.encode("utf-8")):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    users = []
    for u in get_all_users():
        e = enrich_user(dict(u))
        users.append({
            "uuid": e["uuid"],
            "online": bool(e["is_online"]),
            "active": bool(e["is_active"]),
            "used_gb": e["used_gb"],
            "quota_gb": e["quota_gb"],
            "days_left": e["days_left"],
        })

    # Server (Xray) အလုပ်လုပ်နေလား - Admin က ရပ်ထားရင်/Xray ပျက်နေရင် False
    server_up = is_xray_running() and not is_server_stopped()

    return jsonify({"status": "success", "xray": server_up, "users": users})

@app.route("/settings")
def settings():
    if "admin" not in session:
        return redirect(url_for("login"))

    username, _ = get_admin_credentials()
    tg_token = get_setting("telegram_token", "")
    tg_admin_id = get_setting("telegram_admin_id", "")

    return render_template(
        "settings.html",
        current_username=username,
        telegram_token=tg_token,
        telegram_admin_id=tg_admin_id
    )

@app.route("/api/settings", methods=["POST"])
def update_settings():
    if "admin" not in session:
        return jsonify({"status": "error", "message": "Unauthorized access"}), 401

    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    if action == "telegram":
        tg_token = data.get("telegram_token", "").strip()
        tg_admin_id = data.get("telegram_admin_id", "").strip()

        try:
            set_setting("telegram_token", tg_token)
            set_setting("telegram_admin_id", tg_admin_id)
            return jsonify({"status": "success", "message": "Telegram bot settings saved successfully"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    elif action == "security":
        new_username = data.get("username", "").strip()
        new_password = data.get("password", "")
        current_password = data.get("current_password", "")

        if not new_username:
            return jsonify({"status": "error", "message": "New username is required"}), 400

        if not new_password:
            return jsonify({"status": "error", "message": "New password is required"}), 400

        username, password = get_admin_credentials()

        if current_password != password:
            return jsonify({"status": "error", "message": "Current password is incorrect"}), 400

        if len(new_username) < 3:
            return jsonify({"status": "error", "message": "Username must be at least 3 characters"}), 400

        if len(new_password) < 4:
            return jsonify({"status": "error", "message": "Password must be at least 4 characters"}), 400

        try:
            save_admin_credentials(new_username, new_password)
            session.pop("admin", None)
            return jsonify({"status": "success", "message": "Login credentials updated"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "error", "message": "Invalid request"}), 400

@app.route("/api/add_user", methods=["POST"])
def add_user():
    if "admin" not in session:
        return jsonify({"status": "error", "message": "Unauthorized access"}), 401

    data = request.json or {}

    name = data.get("name", "").strip()

    try:
        quota = float(data.get("quota", 30))
        days = int(data.get("days", 30))
    except Exception:
        return jsonify({"status": "error", "message": "Invalid volume or number of days"}), 400

    if not name:
        return jsonify({"status": "error", "message": "User name is required"}), 400

    if quota <= 0:
        return jsonify({"status": "error", "message": "Volume must be greater than zero"}), 400

    if days <= 0:
        return jsonify({"status": "error", "message": "Number of days must be greater than zero"}), 400

    user_uuid = str(uuid.uuid4())

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute(
            """
            INSERT INTO users
            (name, uuid, quota_gb, expire_days, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, user_uuid, quota, days, datetime.now().isoformat())
        )

        conn.commit()
        conn.close()

        restart_xray()

        return jsonify({"status": "success", "message": "User created successfully"})

    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "This username already exists"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/edit_user/<int:user_id>", methods=["POST"])
def edit_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    data = request.get_json(silent=True) or {}

    try:
        new_quota = float(data.get("quota_gb", 0))
        new_days = int(data.get("expire_days", 0))
    except Exception:
        return jsonify({"status": "error", "message": "Invalid values"}), 400

    if new_quota <= 0 or new_days <= 0:
        return jsonify({"status": "error", "message": "Volume and days must be greater than zero"}), 400

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute(
            "UPDATE users SET quota_gb=?, expire_days=? WHERE id=?",
            (new_quota, new_days, user_id)
        )

        conn.commit()
        conn.close()

        # Rebuild the core because the edited user may have had their quota renewed and must be re-enabled
        restart_xray()

        return jsonify({"status": "success", "message": "User updated successfully"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/reset_user/<int:user_id>", methods=["POST"])
def reset_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT name FROM users WHERE id=?", (user_id,))
        row = c.fetchone()

        if row:
            xray_reset_user_stats(row["name"])

        c.execute(
            "UPDATE users SET used_bytes=0, created_at=? WHERE id=?",
            (datetime.now().isoformat(), user_id)
        )

        conn.commit()
        conn.close()

        # Rebuild the config file to re-enable the user who was cut off in Xray
        restart_xray()

        return jsonify({"status": "success", "message": "User traffic reset to zero"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/delete_user/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("DELETE FROM users WHERE id=?", (user_id,))

    conn.commit()
    conn.close()

    restart_xray()

    return jsonify({"status": "success", "message": "User deleted successfully"})

@app.route("/api/toggle_user/<int:user_id>", methods=["POST"])
def toggle_user(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT enabled FROM users WHERE id=?", (user_id,))

    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"status": "error", "message": "User not found"}), 404

    new_val = 0 if row[0] == 1 else 1

    c.execute("UPDATE users SET enabled=? WHERE id=?", (new_val, user_id))

    conn.commit()
    conn.close()

    restart_xray()

    return jsonify({"status": "success", "new_state": new_val})

@app.route("/api/user_config/<int:user_id>")
def user_config(user_id):
    if "admin" not in session:
        return jsonify({"status": "error"}), 401

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = c.fetchone()

    conn.close()

    if not user:
        return jsonify({"status": "error", "message": "User not found"}), 404

    host = request.host.split(":")[0]

    configs = make_all_vless_configs(dict(user), host)

    sub_link = f"{request.host_url}sub/{user['uuid']}"

    return jsonify({
        "status": "success",
        "configs": configs,
        "sub": sub_link,
        "user": dict(user)
    })

@app.route("/sub/<user_uuid>")
def subscription(user_uuid):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE uuid=?", (user_uuid,))
    user = c.fetchone()

    conn.close()

    # If the user was not in the database at all, it raises an error
    if not user:
        return ("User not found", 404)

    user_dict = dict(user)
    ua = request.headers.get("User-Agent", "").lower()

    client_keywords = [
        "v2ray", "clash", "sing-box", "hiddify",
        "nekobox", "streisand", "foxray", "shadowrocket", "v2box"
    ]

    is_client = any(k in ua for k in client_keywords)
    host = request.host.split(":")[0]

    # Check the real traffic status and validity of the user
    active, reason = is_user_active(user_dict)

    if active:
        all_configs = make_all_vless_configs(user_dict, host)
    else:
        # If the user's quota is used up, a notification config is sent instead of the real configs
        all_configs = make_fake_config(reason, user_dict, host)

    # Template suited for VPN clients (sends the usage-monitoring header and subscription)
    if is_client:
        raw_text = "\n".join(item["config"] for item in all_configs)
        encoded = base64.b64encode(raw_text.encode()).decode()
        
        resp = Response(encoded, mimetype="text/plain")
        resp.headers["Subscription-Userinfo"] = get_subscription_header(user_dict)
        return resp

    # Browser view template (subscription website)
    created_dt = datetime.fromisoformat(user_dict["created_at"])
    elapsed_days = (datetime.now() - created_dt).days
    days_left = max(0, user_dict["expire_days"] - elapsed_days)

    used_gb = round(user_dict["used_bytes"] / (1024 ** 3), 2)
    remaining_gb = max(0.0, round(user_dict["quota_gb"] - used_gb, 2))

    percent = (
        used_gb / user_dict["quota_gb"] * 100
        if user_dict["quota_gb"] > 0 else 0
    )

    raw_text = "\n".join(item["config"] for item in all_configs)
    encoded_sub = base64.b64encode(raw_text.encode()).decode()

    resp = Response(render_template(
        "subscription.html",
        user_name=user_dict["name"],
        used_gb=used_gb,
        quota_gb=user_dict["quota_gb"],
        remaining_gb=remaining_gb,
        days_left=days_left,
        percent=round(percent, 1),
        configs=all_configs,
        sub_raw=encoded_sub,
        sub_url=request.url
    ))
    resp.headers["Subscription-Userinfo"] = get_subscription_header(user_dict)
    return resp

# ===========================================================================
# Telegram bot: channel gate, credits, referrals and key generation
# ===========================================================================

BTN_GET_KEY = "🔑 Get Key"
BTN_MY_KEYS = "📂 My Keys"
BTN_CREDITS = "💰 My Credits"
BTN_INVITE = "🎁 Invite & Earn"

BTN_A_STATUS = "📊 Panel Status"
BTN_A_USERS = "👥 User List"
BTN_A_BOTUSERS = "🤖 Bot Users"
BTN_A_FIND = "🔍 Find User"
BTN_A_REVOKE = "🔒 Revoke Key"
BTN_A_BROADCAST = "📢 Broadcast"
BTN_A_ALLCREDIT = "💰 All Credits"
BTN_A_SERVER = "🖥 Server Control"
BTN_A_SETTINGS = "⚙️ Settings"
BTN_A_HEALTH = "🩺 Health & RAM"
BTN_A_CHANNEL = "📡 Check Channel Members"
BTN_CANCEL = "❌ Cancel"

ADMIN_BUTTONS = {
    BTN_A_STATUS, BTN_A_USERS, BTN_A_BOTUSERS, BTN_A_FIND,
    BTN_A_REVOKE, BTN_A_BROADCAST, BTN_A_ALLCREDIT, BTN_A_SERVER, BTN_A_SETTINGS,
    BTN_A_HEALTH, BTN_A_CHANNEL,
}

# Pending admin input (e.g. waiting for a chat id or a broadcast message)
ADMIN_STATE = {}
BOT_USERS_PER_PAGE = 8

_XRAY_RESTART_LOCK = threading.Lock()
_XRAY_RESTART_TIMER = None


def esc(value):
    return html.escape(str(value), quote=False)


def db_query(sql, params=(), one=False):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def db_exec(sql, params=()):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(sql, params)
        conn.commit()
        return c.rowcount
    finally:
        conn.close()


def schedule_xray_restart(delay=3):
    """Restart Xray once after a short delay, so many key changes share one restart."""
    global _XRAY_RESTART_TIMER

    def _run():
        global _XRAY_RESTART_TIMER
        with _XRAY_RESTART_LOCK:
            _XRAY_RESTART_TIMER = None
            try:
                restart_xray()
            except Exception as e:
                print("[Telegram Bot] Xray restart error:", e)

    with _XRAY_RESTART_LOCK:
        if _XRAY_RESTART_TIMER is None:
            timer = threading.Timer(delay, _run)
            timer.daemon = True
            _XRAY_RESTART_TIMER = timer
            timer.start()


def get_public_base_url():
    url = get_setting("public_base_url", "").strip() or os.environ.get("PUBLIC_URL", "").strip()
    if not url:
        # Railway exposes the public domain automatically
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway_domain:
            url = f"https://{railway_domain}"
    return url.rstrip("/")


def get_bot_user(chat_id):
    return db_query("SELECT * FROM bot_users WHERE chat_id=?", (str(chat_id),), one=True)


def register_bot_user(tg_user, referrer=""):
    """Create the bot user on first contact. Returns True if the user is new."""
    chat_id = str(tg_user.id)
    username = tg_user.username or ""
    first_name = tg_user.first_name or ""

    if get_bot_user(chat_id):
        db_exec(
            "UPDATE bot_users SET username=?, first_name=? WHERE chat_id=?",
            (username, first_name, chat_id)
        )
        return False

    ref = ""
    if referrer and referrer != chat_id and get_bot_user(referrer):
        ref = referrer

    db_exec(
        """
        INSERT OR IGNORE INTO bot_users
        (chat_id, username, first_name, credits, referred_by, created_at)
        VALUES (?, ?, ?, 0, ?, ?)
        """,
        (chat_id, username, first_name, ref, datetime.now().isoformat())
    )
    return True


def create_panel_user(name, quota_gb, days, owner_chat_id=""):
    user_uuid = str(uuid.uuid4())
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO users
            (name, uuid, quota_gb, expire_days, created_at, owner_chat_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, user_uuid, quota_gb, days, datetime.now().isoformat(), str(owner_chat_id))
        )
        conn.commit()
        return c.lastrowid, user_uuid
    finally:
        conn.close()


def channel_status(bot, user_id):
    """True = joined, False = definitely not joined, None = could not check (API error)."""
    try:
        member = bot.get_chat_member(TG_CHANNEL, user_id)
        status = getattr(member, "status", "")
        if status in ("member", "administrator", "creator"):
            return True
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as e:
        print("[Telegram Bot] Channel check failed (is the bot an admin of the channel?):", e)
        return None


def is_channel_member(bot, user_id):
    return channel_status(bot, user_id) is True


def lock_owner_keys(chat_id):
    """Switch off every key of a user who is not in the channel. Returns how many keys changed."""
    chat_id = str(chat_id)
    db_exec("UPDATE bot_users SET in_channel=0, channel_checked_at=? WHERE chat_id=?",
            (datetime.now().isoformat(), chat_id))
    n = db_exec("UPDATE users SET channel_locked=1 WHERE owner_chat_id=? AND channel_locked=0", (chat_id,))
    if n:
        schedule_xray_restart()
    return n


def unlock_owner_keys(chat_id):
    """Switch the keys back on once the user is in the channel again. Returns how many keys changed."""
    chat_id = str(chat_id)
    db_exec("UPDATE bot_users SET in_channel=1, channel_checked_at=? WHERE chat_id=?",
            (datetime.now().isoformat(), chat_id))
    n = db_exec("UPDATE users SET channel_locked=0 WHERE owner_chat_id=? AND channel_locked=1", (chat_id,))
    if n:
        schedule_xray_restart()
    return n


def apply_channel_result(bot, chat_id, status, notify=True):
    """Store the channel result for a user and lock/unlock their keys. Notifies on every change."""
    if status is None:
        return
    if status:
        n = unlock_owner_keys(chat_id)
        if n and notify:
            safe_send(bot, chat_id,
                      f"✅ <b>Welcome back!</b> Thanks for rejoining the channel.\n"
                      f"🔓 Your <b>{n}</b> key(s) are active again.")
    else:
        n = lock_owner_keys(chat_id)
        if n and notify:
            safe_send(bot, chat_id,
                      f"⚠️ <b>You left our channel.</b>\n"
                      f"🔒 Your <b>{n}</b> key(s) were switched off automatically.\n\n"
                      f"Join again to turn them back on:\n{esc(TG_CHANNEL_URL)}",
                      reply_markup=join_markup())


def channel_enforcer_worker():
    """Every few minutes: owners who left the channel lose their keys, owners who came back get them again."""
    time.sleep(30)
    while True:
        try:
            token = get_setting("telegram_token", "").strip()
            admin_id = get_setting("telegram_admin_id", "").strip()
            if telebot and token:
                bot = telebot.TeleBot(token)
                owners = db_query(
                    "SELECT DISTINCT owner_chat_id AS cid FROM users WHERE owner_chat_id IS NOT NULL AND owner_chat_id != ''"
                )
                for o in owners:
                    cid = o["cid"]
                    if cid == admin_id:
                        continue
                    apply_channel_result(bot, cid, channel_status(bot, int(cid)))
                    time.sleep(0.1)
        except Exception as e:
            print("[Telegram Bot] Channel enforcer error:", e)
        time.sleep(CHANNEL_CHECK_INTERVAL)


def safe_send(bot, chat_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        print(f"[Telegram Bot] Could not send message to {chat_id}:", e)
        return None


def grant_join_bonus(bot, chat_id):
    """Give the one-time join bonus (and reward the inviter) after channel membership is verified."""
    chat_id = str(chat_id)
    given = db_exec(
        "UPDATE bot_users SET credits = credits + ?, bonus_given = 1 WHERE chat_id=? AND bonus_given=0",
        (JOIN_BONUS_CREDITS, chat_id)
    )
    if not given:
        return False

    safe_send(
        bot, chat_id,
        f"🎉 <b>Welcome!</b> You received <b>+{JOIN_BONUS_CREDITS} credits</b> for joining our channel."
    )

    row = get_bot_user(chat_id)
    inviter = (row or {}).get("referred_by", "")
    if inviter:
        rewarded = db_exec(
            "UPDATE bot_users SET referral_rewarded = 1 WHERE chat_id=? AND referral_rewarded=0",
            (chat_id,)
        )
        if rewarded:
            db_exec(
                "UPDATE bot_users SET credits = credits + ? WHERE chat_id=?",
                (REFERRAL_BONUS_CREDITS, inviter)
            )
            safe_send(
                bot, inviter,
                f"🎁 Someone joined using your invite link!\n"
                f"You received <b>+{REFERRAL_BONUS_CREDITS} credits</b>."
            )
    return True


# ---------------------------------------------------------------- keyboards

def _kb(text):
    return telebot.types.KeyboardButton(text)


def _ib(text, **kwargs):
    return telebot.types.InlineKeyboardButton(text, **kwargs)


def user_menu_markup():
    m = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    m.add(_kb(BTN_GET_KEY), _kb(BTN_MY_KEYS))
    m.add(_kb(BTN_CREDITS), _kb(BTN_INVITE))
    return m


def admin_menu_markup():
    m = telebot.types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    m.add(_kb(BTN_A_STATUS), _kb(BTN_A_USERS))
    m.add(_kb(BTN_A_BOTUSERS), _kb(BTN_A_FIND))
    m.add(_kb(BTN_A_REVOKE), _kb(BTN_A_BROADCAST))
    m.add(_kb(BTN_A_ALLCREDIT), _kb(BTN_A_SERVER))
    m.add(_kb(BTN_A_HEALTH), _kb(BTN_A_CHANNEL))
    m.add(_kb(BTN_A_SETTINGS))
    return m


def cancel_markup():
    m = telebot.types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    m.add(_kb(BTN_CANCEL))
    return m


def join_markup():
    m = telebot.types.InlineKeyboardMarkup()
    m.row(_ib("📢 Join Channel", url=TG_CHANNEL_URL))
    m.row(_ib("✅ I've Joined", callback_data="chk"))
    return m


# ------------------------------------------------------------ user features

def build_welcome_text(chat_id, row):
    name = row.get("first_name") or (f"@{row['username']}" if row.get("username") else "-")
    return (
        f"▣▬▬ 𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎 ⚡ 𝕋𝕦𝕣𝕓𝕠 𝕋𝕦𝕟𝕟𝕖𝕝 𝕄𝕄 ▬▬▣\n\n"
        f"👤 𝐔𝐬𝐞𝐫 𝐈𝐧𝐟𝐨𝐫𝐦𝐚𝐭𝐢𝐨𝐧\n"
        f"𝐈𝐃: <code>{esc(chat_id)}</code>\n"
        f"𝐍𝐚𝐦𝐞: {esc(name)}\n\n"
        f"💰 𝐂𝐫𝐞𝐝𝐢𝐭: <b>{row.get('credits', 0)}</b>"
    )


def _send_banner_photo(bot, chat_id, caption, markup):
    """Send the welcome banner. Returns True if the photo went out."""
    if not os.path.exists(WELCOME_IMAGE_PATH):
        return False

    st = os.stat(WELCOME_IMAGE_PATH)
    sig = f"{st.st_size}-{int(st.st_mtime)}"
    cached_sig, _, cached_id = get_setting("welcome_photo_id", "").partition("|")
    kwargs = {"parse_mode": "HTML", "reply_markup": markup}
    if caption:
        kwargs["caption"] = caption

    # Re-use the file already uploaded to Telegram (fast); upload again if the image changed
    if cached_id and cached_sig == sig:
        try:
            bot.send_photo(chat_id, cached_id, **kwargs)
            return True
        except Exception as e:
            print("[Telegram Bot] Cached welcome photo failed, re-uploading:", e)

    try:
        with open(WELCOME_IMAGE_PATH, "rb") as f:
            msg = bot.send_photo(chat_id, f, **kwargs)
        try:
            set_setting("welcome_photo_id", f"{sig}|{msg.photo[-1].file_id}")
        except Exception:
            pass
        return True
    except Exception as e:
        print("[Telegram Bot] Could not send welcome photo, falling back to text:", e)
        return False


def send_banner(bot, chat_id, caption, markup=None):
    """Banner picture with the text as its caption (text only if the picture is unavailable)."""
    if len(caption) <= 1024:          # Telegram's caption limit
        if _send_banner_photo(bot, chat_id, caption, markup):
            return
    else:
        if _send_banner_photo(bot, chat_id, "", None):
            safe_send(bot, chat_id, caption, reply_markup=markup)
            return
    safe_send(bot, chat_id, caption, reply_markup=markup)


def send_main_menu(bot, chat_id):
    """Welcome banner + user info + credit balance, with the user menu buttons."""
    row = get_bot_user(chat_id) or {}
    send_banner(bot, chat_id, build_welcome_text(chat_id, row), user_menu_markup())


def gate_user(bot, tg_user, banner=False):
    """True if the user may continue. Otherwise the user is told why.
    banner=True (used by /start) shows the welcome picture with the join message."""
    chat_id = str(tg_user.id)
    row = get_bot_user(chat_id)

    if row and row["banned"]:
        safe_send(bot, chat_id, "🚫 You are blocked from using this bot.")
        return False

    status = channel_status(bot, tg_user.id)
    if status is not None:
        apply_channel_result(bot, chat_id, status, notify=False)

    if status is not True:
        join_text = (
            f"📢 <b>You must join our channel to use this bot.</b>\n\n"
            f"{esc(TG_CHANNEL_URL)}\n\n"
            f"After joining, tap <b>✅ I've Joined</b> to receive "
            f"<b>{JOIN_BONUS_CREDITS} free credits</b>.\n\n"
            f"🔒 Keys of users who are not in the channel are switched off automatically."
        )
        if banner:
            row = get_bot_user(chat_id) or {}
            send_banner(bot, chat_id, build_welcome_text(chat_id, row) + "\n\n" + join_text, join_markup())
        else:
            safe_send(bot, chat_id, join_text, reply_markup=join_markup())
        return False

    grant_join_bonus(bot, chat_id)
    return True


def describe_key_for_user(u):
    u = enrich_user(dict(u))
    active, reason = is_user_active(u)
    if active:
        status = "Active 🟢"
    elif reason == "limit_exceeded":
        status = "Quota exhausted 🔴"
    elif reason == "expired":
        status = "Subscription expired 🔴"
    elif reason == "channel_left":
        status = "Locked - owner left the channel 🔒"
    else:
        status = "Disabled 🔴"
    return u, status


def format_eta(iso_time):
    try:
        delta = datetime.fromisoformat(iso_time) - datetime.now()
    except Exception:
        return "soon"
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "soon"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def refill_note(row):
    """One line telling the user when their free credits arrive (empty if nothing is pending)."""
    if row and row.get("refill_at") and row.get("credits", 0) < KEY_COST_CREDITS:
        return (
            f"⏳ Free <b>+{AUTO_REFILL_CREDITS} credits</b> will be added automatically "
            f"in <b>{format_eta(row['refill_at'])}</b>."
        )
    return ""


def start_refill_timer(chat_id):
    """Credits ran out (below one key's cost): the free refill arrives AUTO_REFILL_DAYS days from now."""
    db_exec(
        "UPDATE bot_users SET refill_at=? WHERE chat_id=? AND credits < ?",
        ((datetime.now() + timedelta(days=AUTO_REFILL_DAYS)).isoformat(), chat_id, KEY_COST_CREDITS)
    )


def process_due_refills(bot_factory=None):
    """Give AUTO_REFILL_CREDITS to users whose wait is over and who still can't afford a key."""
    now = datetime.now().isoformat()
    due = db_query(
        "SELECT chat_id, credits, banned FROM bot_users WHERE refill_at IS NOT NULL AND refill_at != '' AND refill_at <= ?",
        (now,)
    )
    for r in due:
        cid = r["chat_id"]
        if r["credits"] >= KEY_COST_CREDITS:
            # They earned enough credits meanwhile, nothing to refill
            db_exec("UPDATE bot_users SET refill_at='' WHERE chat_id=? AND credits >= ?", (cid, KEY_COST_CREDITS))
            continue
        if r["banned"]:
            continue
        done = db_exec(
            """
            UPDATE bot_users SET credits = credits + ?, refill_at = ''
            WHERE chat_id=? AND refill_at != '' AND refill_at <= ? AND credits < ?
            """,
            (AUTO_REFILL_CREDITS, cid, now, KEY_COST_CREDITS)
        )
        if done and bot_factory:
            try:
                safe_send(
                    bot_factory(), cid,
                    f"🎁 <b>Credits refilled!</b> You received <b>+{AUTO_REFILL_CREDITS} credits</b>.\n"
                    f"Tap <b>{esc(BTN_GET_KEY)}</b> to create a new key."
                )
            except Exception as e:
                print("[Telegram Bot] Refill notice failed:", e)


def credit_refill_worker():
    while True:
        try:
            def make_bot():
                token = get_setting("telegram_token", "").strip()
                if not (telebot and token):
                    raise RuntimeError("bot unavailable")
                return telebot.TeleBot(token)
            process_due_refills(make_bot)
        except Exception as e:
            print("[Telegram Bot] Refill worker error:", e)
        time.sleep(60)


def get_public_host():
    base = get_public_base_url()
    try:
        return urllib.parse.urlparse(base).hostname or ""
    except Exception:
        return ""


def send_key_configs(bot, chat_id, key_row, intro=""):
    """Send the real VLESS config keys of a key (instead of a subscription link)."""
    host = get_public_host()
    if not host:
        safe_send(bot, chat_id, "⚠️ The service is not ready yet. Please contact the admin.")
        return

    u = dict(key_row)
    active, reason = is_user_active(u)
    if not active:
        why = "you left the channel, join again to unlock it" if reason == "channel_left" else esc(reason)
        safe_send(bot, chat_id, f"{intro}\n\n🔴 This key is not active ({why}), so no config is available.".strip())
        return

    blocks = [
        f"<b>{esc(c['tag'])}</b>\n<code>{esc(c['config'])}</code>"
        for c in make_all_vless_configs(u, host)
    ]

    chunks, current = [], intro
    for block in blocks:
        if current and len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + block
    if current:
        chunks.append(current)

    for chunk in chunks:
        safe_send(bot, chat_id, chunk)


def package_markup():
    mk = telebot.types.InlineKeyboardMarkup()
    for pid, p in KEY_PACKAGES.items():
        mk.row(_ib(f"{p['label']} — {p['cost']} credits", callback_data=f"pkg:{pid}"))
    return mk


def handle_get_key(bot, chat_id):
    if not get_public_host():
        safe_send(bot, chat_id, "⚠️ The service is not ready yet. Please contact the admin.")
        print("[Telegram Bot] Key request refused: panel URL is not set (Settings → Set Panel URL).")
        return
    row = get_bot_user(chat_id) or {}
    safe_send(
        bot, chat_id,
        f"🔑 <b>Choose a package</b>\n"
        f"💰 Your credits: <b>{row.get('credits', 0)}</b>\n"
        f"📅 Validity: {KEY_DAYS} days\n\n"
        f"Tap a package to buy it:",
        reply_markup=package_markup()
    )


def buy_package(bot, chat_id, pkg_id):
    pkg = KEY_PACKAGES.get(pkg_id)
    if not pkg:
        safe_send(bot, chat_id, "⚠️ This package no longer exists. Please tap Get Key again.")
        return
    if not get_public_host():
        safe_send(bot, chat_id, "⚠️ The service is not ready yet. Please contact the admin.")
        return

    cost = pkg["cost"]
    paid = db_exec(
        "UPDATE bot_users SET credits = credits - ? WHERE chat_id=? AND credits >= ?",
        (cost, chat_id, cost)
    )
    if not paid:
        row = get_bot_user(chat_id) or {}
        safe_send(
            bot, chat_id,
            f"❌ <b>Not enough credits.</b>\n"
            f"<b>{esc(pkg['label'])}</b> costs <b>{cost}</b> credits, you have <b>{row.get('credits', 0)}</b>.\n\n"
            + (refill_note(row) + "\n\n" if refill_note(row) else "") +
            f"Tap <b>{esc(BTN_INVITE)}</b> to earn more now."
        )
        return

    created = []
    try:
        for size in pkg["sizes"]:
            name = f"tg{chat_id}-{secrets.token_hex(3)}"
            key_id, _ = create_panel_user(name, size, KEY_DAYS, chat_id)
            created.append(db_query("SELECT * FROM users WHERE id=?", (key_id,), one=True))
        schedule_xray_restart()
    except Exception as e:
        db_exec("UPDATE bot_users SET credits = credits + ? WHERE chat_id=?", (cost, chat_id))
        print("[Telegram Bot] Key creation failed, credits refunded:", e)
        safe_send(bot, chat_id, "⚠️ Could not create your key. Your credits were refunded. Please try again.")
        return

    row = get_bot_user(chat_id) or {}
    if row.get("credits", 0) < KEY_COST_CREDITS:
        start_refill_timer(chat_id)
        row = get_bot_user(chat_id) or {}
    refill = refill_note(row)
    head = (
        f"✅ <b>{esc(pkg['label'])}</b> purchased!\n"
        f"📅 Validity: {KEY_DAYS} days\n"
        f"💰 Credits left: {row.get('credits', 0)}\n"
        + (refill + "\n" if refill else "") +
        f"\n👇 Tap a config to copy it, then import it into your VPN app (v2rayNG, Hiddify, …)."
    )
    for i, key_row in enumerate(created):
        intro = head if i == 0 else f"🔑 <b>Key {i + 1}/{len(created)}</b>"
        send_key_configs(bot, chat_id, key_row, intro)


def handle_my_keys(bot, chat_id):
    keys = db_query("SELECT * FROM users WHERE owner_chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,))
    if not keys:
        safe_send(bot, chat_id, f"You have no keys yet. Tap <b>{esc(BTN_GET_KEY)}</b> to create one.")
        return

    msg = "📂 <b>Your keys</b> (latest 10)\n\n"
    mk = telebot.types.InlineKeyboardMarkup()
    for k in keys:
        u, status = describe_key_for_user(k)
        msg += (
            f"🆔 <code>{esc(u['name'])}</code>\n"
            f"⚡ {status} | 📊 {u['used_gb']}/{u['quota_gb']:g} GB | 📅 {u['days_left']} days left\n\n"
        )
        mk.row(_ib(f"📋 Get config: {u['name']}", callback_data=f"cfg:{u['id']}"))
    safe_send(bot, chat_id, msg, reply_markup=mk)


def handle_my_credits(bot, chat_id):
    row = get_bot_user(chat_id) or {}
    credits = row.get("credits", 0)
    safe_send(
        bot, chat_id,
        f"💰 <b>Your credits:</b> {credits}\n"
        f"🔑 Packages: {esc(PACKAGES_SUMMARY)}\n"
        f"📅 Validity: {KEY_DAYS} days"
        + (("\n\n" + refill_note(row)) if refill_note(row) else "")
    )


_BOT_USERNAME_CACHE = {}


def handle_invite(bot, chat_id):
    if "name" not in _BOT_USERNAME_CACHE:
        try:
            _BOT_USERNAME_CACHE["name"] = bot.get_me().username
        except Exception:
            _BOT_USERNAME_CACHE["name"] = ""
    bot_name = _BOT_USERNAME_CACHE["name"]

    refs = db_query(
        "SELECT COUNT(*) AS n FROM bot_users WHERE referred_by=? AND referral_rewarded=1",
        (chat_id,), one=True
    )["n"]

    link = f"https://t.me/{bot_name}?start=ref_{chat_id}" if bot_name else "(unavailable)"
    safe_send(
        bot, chat_id,
        f"🎁 <b>Invite & Earn</b>\n"
        f"Get <b>+{REFERRAL_BONUS_CREDITS} credits</b> for every friend who starts the bot with your "
        f"link and joins our channel.\n\n"
        f"🔗 Your invite link:\n<code>{esc(link)}</code>\n\n"
        f"📢 Our channel: {esc(TG_CHANNEL_URL)}\n\n"
        f"👥 Friends invited so far: <b>{refs}</b>\n\n"
        f"ℹ️ Credits are only counted for people who join through <b>your invite link</b>."
    )


def handle_key_lookup(bot, chat_id, text):
    """Users can look up a key by its name or UUID (their own keys, or keys without an owner)."""
    row = db_query("SELECT * FROM users WHERE name=? OR uuid=?", (text, text), one=True)
    if row and row.get("owner_chat_id") in ("", None, chat_id):
        u, status = describe_key_for_user(row)
        safe_send(
            bot, chat_id,
            f"👤 <b>Subscription details</b>\n"
            f"━━━━━━━━━━━━━\n"
            f"🆔 Username: <code>{esc(u['name'])}</code>\n"
            f"⚡ Account status: {status}\n"
            f"📊 Traffic used: {u['used_gb']} GB\n"
            f"💾 Total quota: {u['quota_gb']:g} GB\n"
            f"📅 Remaining validity: {u['days_left']} days\n"
            f"━━━━━━━━━━━━━"
        )
    else:
        safe_send(
            bot, chat_id,
            "Please use the buttons below.",
            reply_markup=user_menu_markup()
        )


def handle_user_message(bot, message):
    tg_user = message.from_user
    chat_id = str(message.chat.id)
    text = (message.text or "").strip()

    register_bot_user(tg_user)
    if not gate_user(bot, tg_user):
        return

    if text == BTN_GET_KEY:
        handle_get_key(bot, chat_id)
    elif text == BTN_MY_KEYS:
        handle_my_keys(bot, chat_id)
    elif text == BTN_CREDITS:
        handle_my_credits(bot, chat_id)
    elif text == BTN_INVITE:
        handle_invite(bot, chat_id)
    elif text:
        handle_key_lookup(bot, chat_id, text)
    else:
        safe_send(bot, chat_id, "Please use the buttons below.", reply_markup=user_menu_markup())


# ----------------------------------------------------------- server health & channel check

def read_cpu_times():
    with open("/proc/stat", "r") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def get_system_stats():
    stats = {"ram_total": None, "ram_used": None, "ram_pct": None,
             "cpu_pct": None, "load": None, "disk_pct": None}
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        stats.update(ram_total=total, ram_used=used, ram_pct=round(used * 100 / total, 1))
    except Exception:
        pass
    try:
        idle1, total1 = read_cpu_times()
        time.sleep(0.5)
        idle2, total2 = read_cpu_times()
        dt = total2 - total1
        if dt > 0:
            stats["cpu_pct"] = round((1 - (idle2 - idle1) / dt) * 100, 1)
    except Exception:
        pass
    try:
        stats["load"] = os.getloadavg()
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        stats["disk_pct"] = round((total - free) * 100 / total, 1)
    except Exception:
        pass
    return stats


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:
        return False


def render_health():
    s = get_system_stats()
    users = get_all_users()
    active = sum(1 for u in users if is_user_active(u)[0])

    if is_server_stopped():
        xray = "Stopped by admin 🔴"
    elif is_xray_running() and port_open(XRAY_PORT):
        xray = "Running & accepting connections 🟢"
    elif is_xray_running():
        xray = "Process running, port not listening ⚠️"
    else:
        xray = "Not running ⚠️"

    ram = "?" if s["ram_pct"] is None else (
        f"{s['ram_used'] / 1024 ** 3:.2f} / {s['ram_total'] / 1024 ** 3:.2f} GB ({s['ram_pct']}%)")
    cpu = "?" if s["cpu_pct"] is None else f"{s['cpu_pct']}%"
    load = "?" if not s["load"] else " / ".join(f"{x:.2f}" for x in s["load"])
    disk = "?" if s["disk_pct"] is None else f"{s['disk_pct']}%"

    warn = []
    if s["ram_pct"] is not None and s["ram_pct"] >= 85:
        warn.append("RAM high")
    if s["cpu_pct"] is not None and s["cpu_pct"] >= 85:
        warn.append("CPU high")
    if s["disk_pct"] is not None and s["disk_pct"] >= 90:
        warn.append("disk almost full")
    if xray.startswith("Not running") or xray.startswith("Process running"):
        warn.append("Xray not serving")
    verdict = ("⚠️ " + ", ".join(warn)) if warn else "✅ Healthy"

    text = (
        f"🩺 <b>Server health</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚡ Xray: {xray}\n"
        f"🧠 RAM: {ram}\n"
        f"🔥 CPU: {cpu}\n"
        f"📈 Load (1/5/15 min): {load}\n"
        f"💽 Disk: {disk}\n"
        f"🟢 Active keys: {active}/{len(users)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"Status: {verdict}"
    )
    mk = telebot.types.InlineKeyboardMarkup()
    mk.row(_ib("🔃 Refresh", callback_data="hl_refresh"))
    mk.row(_ib("📡 Check all channel members", callback_data="chk_all"))
    return text, mk


def run_channel_check_all(bot, admin_chat):
    """Check every bot user against the channel. Users who left get their keys switched off, rejoiners get them back."""
    admin_id = get_setting("telegram_admin_id", "").strip()
    rows = db_query("SELECT chat_id FROM bot_users WHERE banned=0")
    joined = left = unknown = 0
    safe_send(bot, admin_chat, f"⏳ Checking <b>{len(rows)}</b> users against the channel…")
    for r in rows:
        cid = r["chat_id"]
        if cid == admin_id:
            continue
        status = channel_status(bot, int(cid))
        if status is None:
            unknown += 1
        else:
            if status:
                joined += 1
            else:
                left += 1
            apply_channel_result(bot, cid, status, notify=True)
        time.sleep(0.05)  # stay under Telegram rate limits
    safe_send(
        bot, admin_chat,
        f"📡 <b>Channel check finished</b>\n"
        f"✅ Joined: {joined}\n"
        f"🚪 Not in channel: {left} (their keys are switched off)\n"
        f"❓ Could not check: {unknown}\n\n"
        f"If 'could not check' is high, make sure the bot is an admin of {esc(TG_CHANNEL)}."
    )


# ----------------------------------------------------------- admin features

def resolve_bot_user(query):
    q = (query or "").strip()
    if q.startswith("@"):
        q = q[1:]
    if not q:
        return None
    if q.isdigit():
        return get_bot_user(q)
    return db_query("SELECT * FROM bot_users WHERE lower(username)=lower(?)", (q,), one=True)


def channel_line(bot, row):
    """Live channel check for the profile (falls back to the last stored result)."""
    status = channel_status(bot, int(row["chat_id"])) if bot else None
    if status is not None:
        db_exec("UPDATE bot_users SET in_channel=?, channel_checked_at=? WHERE chat_id=?",
                (1 if status else 0, datetime.now().isoformat(), row["chat_id"]))
    else:
        stored = row.get("in_channel", -1)
        status = None if stored in (None, -1) else bool(stored)

    if status is True:
        text = "Joined ✅"
    elif status is False:
        text = "Not joined ❌"
    else:
        text = "Unknown ⚠️"

    locked = db_query(
        "SELECT COUNT(*) AS n FROM users WHERE owner_chat_id=? AND channel_locked=1",
        (row["chat_id"],), one=True
    )["n"]
    if locked:
        text += f" (🔒 {locked} key(s) locked)"
    return f"📢 Channel: {text}\n"


def render_profile(row, bot=None):
    cid = row["chat_id"]
    refs = db_query(
        "SELECT COUNT(*) AS n FROM bot_users WHERE referred_by=? AND referral_rewarded=1",
        (cid,), one=True
    )["n"]
    keys = db_query("SELECT COUNT(*) AS n FROM users WHERE owner_chat_id=?", (cid,), one=True)["n"]
    banned = bool(row["banned"])
    uname = f"@{row['username']}" if row["username"] else "-"

    text = (
        f"👤 <b>User</b> <code>{esc(cid)}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📛 Name: {esc(row['first_name'] or '-')}\n"
        f"🔗 Username: {esc(uname)}\n"
        f"💰 Credits: {row['credits']}\n"
        f"🔑 Keys created: {keys}\n"
        f"🎁 Successful invites: {refs}\n"
        f"🚦 Bot access: {'Blocked 🚫' if banned else 'Allowed ✅'}\n"
        + channel_line(bot, row)
        + (f"⏳ Auto-refill in: {format_eta(row['refill_at'])}\n" if row.get("refill_at") else "") +
        f"📅 Registered: {esc((row['created_at'] or '')[:10])}"
    )

    mk = telebot.types.InlineKeyboardMarkup()
    if banned:
        mk.row(_ib("✅ Unblock user", callback_data=f"unban:{cid}"))
    else:
        mk.row(_ib("🚫 Block user", callback_data=f"ban:{cid}"))
    mk.row(
        _ib("➕ 5", callback_data=f"cr:{cid}:5"),
        _ib("➕ 10", callback_data=f"cr:{cid}:10"),
        _ib("➖ 5", callback_data=f"cr:{cid}:-5"),
    )
    mk.row(_ib("🔑 View keys", callback_data=f"keys:{cid}"))
    return text, mk


def render_key_card(u):
    u, status = describe_key_for_user(u)
    text = (
        f"🔑 <b>Key</b> <code>{esc(u['name'])}</code>\n"
        f"👤 Owner chat ID: <code>{esc(u.get('owner_chat_id') or '-')}</code>\n"
        f"⚡ Status: {status}\n"
        f"📊 Used: {u['used_gb']}/{u['quota_gb']:g} GB\n"
        f"📅 Days left: {u['days_left']}"
    )
    mk = telebot.types.InlineKeyboardMarkup()
    if u["enabled"]:
        mk.row(_ib("🔒 Revoke key", callback_data=f"kt:{u['id']}"))
    else:
        mk.row(_ib("🔓 Restore key", callback_data=f"kt:{u['id']}"))
    mk.row(_ib("📋 Get config", callback_data=f"acfg:{u['id']}"))
    return text, mk


def send_keys_of_owner(bot, admin_chat, owner_chat_id):
    keys = db_query("SELECT * FROM users WHERE owner_chat_id=? ORDER BY id DESC LIMIT 10", (str(owner_chat_id),))
    if not keys:
        safe_send(bot, admin_chat, "This user has no keys.")
        return
    for k in keys:
        text, mk = render_key_card(k)
        safe_send(bot, admin_chat, text, reply_markup=mk)


def render_bot_users_page(page):
    total = db_query("SELECT COUNT(*) AS n FROM bot_users", one=True)["n"]
    pages = max(1, (total + BOT_USERS_PER_PAGE - 1) // BOT_USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    rows = db_query(
        "SELECT * FROM bot_users ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (BOT_USERS_PER_PAGE, page * BOT_USERS_PER_PAGE)
    )

    if total == 0:
        return "🤖 No bot users yet.", None

    text = (
        f"🤖 <b>Bot users</b>: {total} (page {page + 1}/{pages})\n"
        f"Tap a user to see details and manage them."
    )
    mk = telebot.types.InlineKeyboardMarkup()
    for r in rows:
        flag = "🚫 " if r["banned"] else ""
        label = f"{flag}{r['chat_id']} | {(r['first_name'] or '-')[:12]} | 💰{r['credits']}"
        mk.row(_ib(label, callback_data=f"pf:{r['chat_id']}"))

    nav = []
    if page > 0:
        nav.append(_ib("⬅️ Prev", callback_data=f"bu:{page - 1}"))
    if page < pages - 1:
        nav.append(_ib("Next ➡️", callback_data=f"bu:{page + 1}"))
    if nav:
        mk.row(*nav)
    return text, mk


def render_settings():
    base = get_public_base_url()
    text = (
        f"⚙️ <b>Settings</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🌐 Panel URL: <code>{esc(base) if base else 'not set'}</code>\n"
        f"📢 Channel: {esc(TG_CHANNEL)}\n"
        f"🎁 Join bonus: {JOIN_BONUS_CREDITS} credits\n"
        f"👥 Referral bonus: {REFERRAL_BONUS_CREDITS} credits\n"
        f"🔑 Packages: {esc(PACKAGES_SUMMARY)}\n"
        f"📅 Key validity: {KEY_DAYS} days\n"
        f"📋 Configs per key: {CONFIGS_PER_KEY}\n"
        f"♻️ Auto-refill: +{AUTO_REFILL_CREDITS} credits, {AUTO_REFILL_DAYS} days after a user runs out\n\n"
        f"The panel URL is used to build subscription links, e.g. <code>https://your-domain.com</code>"
    )
    mk = telebot.types.InlineKeyboardMarkup()
    mk.row(_ib("🌐 Set Panel URL", callback_data="set_url"))
    mk.row(_ib("🔑 Change Panel Password", callback_data="set_pw"))
    return text, mk


def render_all_credits():
    n_all = db_query("SELECT COUNT(*) AS n FROM bot_users WHERE banned=0", one=True)["n"]
    total = db_query("SELECT COALESCE(SUM(credits),0) AS s FROM bot_users WHERE banned=0", one=True)["s"]
    text = (
        f"💰 <b>All users credit</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👥 Users (not blocked): {n_all}\n"
        f"💰 Total credits now: {total}\n\n"
        f"Choose a change to apply to <b>every user</b>:"
    )
    mk = telebot.types.InlineKeyboardMarkup()
    mk.row(_ib("➕ 5", callback_data="ac:5"), _ib("➕ 10", callback_data="ac:10"))
    mk.row(_ib("➖ 5", callback_data="ac:-5"), _ib("➖ 10", callback_data="ac:-10"))
    mk.row(_ib("✏️ Custom amount", callback_data="ac_custom"))
    return text, mk


def render_server_panel():
    stopped = is_server_stopped()
    running = is_xray_running()
    if stopped:
        state = "Stopped by admin 🔴"
    elif running:
        state = "Running 🟢"
    else:
        state = "Not running ⚠️ (tap Start to launch it)"
    users = get_all_users()
    active = sum(1 for u in users if is_user_active(u)[0])
    text = (
        f"🖥 <b>Server control</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚡ Xray server: {state}\n"
        f"🟢 Active keys: {active}/{len(users)}\n\n"
        + ("While the server is stopped <b>nobody</b> can connect." if stopped else
           "Stopping the server disconnects <b>all users</b> until you start it again.")
    )
    mk = telebot.types.InlineKeyboardMarkup()
    if stopped:
        mk.row(_ib("▶️ Start server", callback_data="srv_start"))
    else:
        mk.row(_ib("⏹ Stop server", callback_data="srv_stop"))
        mk.row(_ib("🔄 Restart server", callback_data="srv_restart"))
    mk.row(_ib("🔃 Refresh status", callback_data="srv_refresh"))
    return text, mk


def apply_all_credits(bot, admin_chat, delta):
    """Add/remove credits for every user who is not blocked, then tell them."""
    rows = db_query("SELECT chat_id FROM bot_users WHERE banned=0")
    db_exec("UPDATE bot_users SET credits = MAX(0, credits + ?) WHERE banned=0", (delta,))
    # Anyone who dropped below the price of a key waits for the normal auto-refill
    db_exec(
        "UPDATE bot_users SET refill_at=? WHERE banned=0 AND credits < ? AND (refill_at IS NULL OR refill_at='')",
        ((datetime.now() + timedelta(days=AUTO_REFILL_DAYS)).isoformat(), KEY_COST_CREDITS)
    )
    sign = "+" if delta > 0 else ""
    ok = fail = 0
    for r in rows:
        cur = get_bot_user(r["chat_id"]) or {}
        msg = safe_send(
            bot, r["chat_id"],
            f"💰 Your credits were changed by the admin: <b>{sign}{delta}</b>\n"
            f"Balance: <b>{cur.get('credits', 0)}</b>"
        )
        if msg:
            ok += 1
        else:
            fail += 1
        time.sleep(0.06)
    safe_send(
        bot, admin_chat,
        f"✅ <b>Done.</b> All users credit changed by <b>{sign}{delta}</b>.\n"
        f"👥 Users updated: {len(rows)}\n📨 Notified: {ok} | ❌ Not reachable: {fail}",
        reply_markup=admin_menu_markup()
    )


def send_all_credits_confirm(bot, chat_id, delta, show_menu=True):
    n = db_query("SELECT COUNT(*) AS n FROM bot_users WHERE banned=0", one=True)["n"]
    sign = "+" if delta > 0 else ""
    mk = telebot.types.InlineKeyboardMarkup()
    mk.row(
        _ib(f"✅ Apply {sign}{delta} to {n} users", callback_data=f"ac_ok:{delta}"),
        _ib("❌ Cancel", callback_data="ac_no"),
    )
    safe_send(
        bot, chat_id,
        f"⚠️ Change credits by <b>{sign}{delta}</b> for <b>all {n} users</b>? Every user will be notified.",
        reply_markup=mk
    )
    if show_menu:
        back_to_admin_menu(bot, chat_id)


def ask_admin(bot, chat_id, action, prompt):
    ADMIN_STATE[chat_id] = {"action": action}
    safe_send(bot, chat_id, prompt, reply_markup=cancel_markup())


def back_to_admin_menu(bot, chat_id):
    safe_send(bot, chat_id, "Back to the menu 👇", reply_markup=admin_menu_markup())


def run_broadcast(bot, admin_chat, src_chat, src_msg_id):
    rows = db_query("SELECT chat_id FROM bot_users WHERE banned=0")
    ok = fail = 0
    for r in rows:
        try:
            bot.copy_message(r["chat_id"], src_chat, src_msg_id)
            ok += 1
        except Exception:
            fail += 1
        time.sleep(0.06)
    safe_send(
        bot, admin_chat,
        f"📢 <b>Broadcast finished</b>\n✅ Sent: {ok}\n❌ Failed: {fail}",
        reply_markup=admin_menu_markup()
    )


def admin_button_action(bot, chat_id, text):
    if text == BTN_A_STATUS:
        users = get_all_users()
        total = len(users)
        active = sum(1 for u in users if is_user_active(u)[0])
        total_gb = round(sum(u["used_bytes"] for u in users) / (1024 ** 3), 2)
        bot_total = db_query("SELECT COUNT(*) AS n FROM bot_users", one=True)["n"]
        bot_banned = db_query("SELECT COUNT(*) AS n FROM bot_users WHERE banned=1", one=True)["n"]
        bot_keys = db_query("SELECT COUNT(*) AS n FROM users WHERE owner_chat_id != ''", one=True)["n"]
        safe_send(
            bot, chat_id,
            f"🌐 <b>Turbo Tunnel MM Server Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total users: {total}\n"
            f"🟢 Active users: {active}\n"
            f"📊 Total database usage: {total_gb} GB\n"
            f"🖥 Xray server: {'Stopped by admin 🔴' if is_server_stopped() else ('Running 🟢' if is_xray_running() else 'Not running ⚠️')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot users: {bot_total} (blocked: {bot_banned})\n"
            f"🔑 Keys created by the bot: {bot_keys}"
        )

    elif text == BTN_A_USERS:
        users = get_all_users()
        if not users:
            safe_send(bot, chat_id, "No users found.")
            return
        msg = "👥 <b>Panel user list (showing last 15 users):</b>\n\n"
        for u in users[:15]:
            active, _ = is_user_active(u)
            flag = "🟢" if active else "🔴"
            used = round(u["used_bytes"] / (1024 ** 3), 2)
            msg += f"{flag} <code>{esc(u['name'])}</code> | {used}/{u['quota_gb']:g} GB\n"
        if len(users) > 15:
            msg += f"\nand {len(users) - 15} more users..."
        safe_send(bot, chat_id, msg)

    elif text == BTN_A_BOTUSERS:
        body, mk = render_bot_users_page(0)
        safe_send(bot, chat_id, body, reply_markup=mk)

    elif text == BTN_A_FIND:
        ask_admin(bot, chat_id, "find", "🔍 Send the user's <b>Chat ID</b> or <b>@username</b>:")

    elif text == BTN_A_REVOKE:
        ask_admin(
            bot, chat_id, "revoke",
            "🔒 Send the <b>key name</b>, <b>UUID</b>, or the owner's <b>Chat ID / @username</b>:"
        )

    elif text == BTN_A_BROADCAST:
        ask_admin(
            bot, chat_id, "broadcast",
            "📢 Send the message you want to broadcast to all bot users "
            "(text, photo, video, file…):"
        )

    elif text == BTN_A_ALLCREDIT:
        body, mk = render_all_credits()
        safe_send(bot, chat_id, body, reply_markup=mk)

    elif text == BTN_A_SERVER:
        body, mk = render_server_panel()
        safe_send(bot, chat_id, body, reply_markup=mk)

    elif text == BTN_A_HEALTH:
        body, mk = render_health()
        safe_send(bot, chat_id, body, reply_markup=mk)

    elif text == BTN_A_CHANNEL:
        threading.Thread(target=run_channel_check_all, args=(bot, chat_id), daemon=True).start()

    elif text == BTN_A_SETTINGS:
        body, mk = render_settings()
        safe_send(bot, chat_id, body, reply_markup=mk)


def handle_admin_message(bot, message):
    chat_id = str(message.chat.id)
    text = (message.text or "").strip()

    if text == BTN_CANCEL:
        ADMIN_STATE.pop(chat_id, None)
        safe_send(bot, chat_id, "Cancelled.", reply_markup=admin_menu_markup())
        return

    if text in ADMIN_BUTTONS:
        ADMIN_STATE.pop(chat_id, None)
        admin_button_action(bot, chat_id, text)
        return

    state = ADMIN_STATE.get(chat_id)
    if not state:
        safe_send(bot, chat_id, "Please choose one of the buttons below.", reply_markup=admin_menu_markup())
        return

    action = state.get("action")

    if action == "find":
        row = resolve_bot_user(text)
        if not row:
            safe_send(bot, chat_id, "❌ User not found. Send a Chat ID or @username, or tap Cancel.")
            return
        ADMIN_STATE.pop(chat_id, None)
        body, mk = render_profile(row, bot)
        safe_send(bot, chat_id, body, reply_markup=mk)
        back_to_admin_menu(bot, chat_id)

    elif action == "revoke":
        key = db_query("SELECT * FROM users WHERE name=? OR uuid=?", (text, text), one=True)
        if key:
            ADMIN_STATE.pop(chat_id, None)
            body, mk = render_key_card(key)
            safe_send(bot, chat_id, body, reply_markup=mk)
            back_to_admin_menu(bot, chat_id)
            return
        row = resolve_bot_user(text)
        if row:
            ADMIN_STATE.pop(chat_id, None)
            send_keys_of_owner(bot, chat_id, row["chat_id"])
            back_to_admin_menu(bot, chat_id)
            return
        safe_send(bot, chat_id, "❌ Nothing found. Send a key name, UUID, Chat ID or @username, or tap Cancel.")

    elif action == "broadcast":
        count = db_query("SELECT COUNT(*) AS n FROM bot_users WHERE banned=0", one=True)["n"]
        ADMIN_STATE[chat_id] = {"action": "bc_confirm", "msg_id": message.message_id}
        mk = telebot.types.InlineKeyboardMarkup()
        mk.row(
            _ib(f"✅ Send to {count} users", callback_data="bc_ok"),
            _ib("❌ Cancel", callback_data="bc_no"),
        )
        safe_send(bot, chat_id, "👆 This message will be sent to all users. Confirm?", reply_markup=mk)

    elif action == "ac_custom":
        try:
            delta = int(text.replace("+", "").strip())
        except ValueError:
            safe_send(bot, chat_id, "❌ Send a whole number like <code>5</code> or <code>-3</code>, or tap Cancel.")
            return
        if delta == 0 or abs(delta) > 1000:
            safe_send(bot, chat_id, "❌ The number must be between -1000 and 1000 (not 0). Try again or tap Cancel.")
            return
        ADMIN_STATE.pop(chat_id, None)
        send_all_credits_confirm(bot, chat_id, delta)

    elif action == "set_pw":
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        if len(text) < 4:
            safe_send(bot, chat_id, "❌ The password must be at least 4 characters. Try again or tap Cancel.")
            return
        ADMIN_STATE.pop(chat_id, None)
        username, _ = get_admin_credentials()
        save_admin_credentials(username, text)
        safe_send(
            bot, chat_id,
            f"✅ <b>Panel password changed.</b>\n👤 Username: <code>{esc(username)}</code>\n"
            f"All logged-in panel sessions were signed out.",
            reply_markup=admin_menu_markup()
        )

    elif action == "set_url":
        url = text.strip().rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            safe_send(bot, chat_id, "❌ The URL must start with http:// or https://. Try again or tap Cancel.")
            return
        ADMIN_STATE.pop(chat_id, None)
        set_setting("public_base_url", url)
        safe_send(bot, chat_id, f"✅ Panel URL saved: <code>{esc(url)}</code>", reply_markup=admin_menu_markup())

    else:
        ADMIN_STATE.pop(chat_id, None)
        safe_send(bot, chat_id, "Please choose one of the buttons below.", reply_markup=admin_menu_markup())


def handle_admin_callback(bot, call, admin_id):
    data = call.data or ""
    chat_id = str(call.message.chat.id)
    msg_id = call.message.message_id

    def edit(text, mk=None):
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=mk, parse_mode="HTML")
        except Exception as e:
            print("[Telegram Bot] edit failed:", e)

    if data.startswith("bu:"):
        body, mk = render_bot_users_page(int(data.split(":")[1]))
        edit(body, mk)
        bot.answer_callback_query(call.id)

    elif data.startswith("pf:"):
        row = get_bot_user(data.split(":")[1])
        if not row:
            bot.answer_callback_query(call.id, "User not found", show_alert=True)
            return
        body, mk = render_profile(row, bot)
        safe_send(bot, chat_id, body, reply_markup=mk)
        bot.answer_callback_query(call.id)

    elif data.startswith("ban:") or data.startswith("unban:"):
        action, cid = data.split(":", 1)
        if cid == str(admin_id):
            bot.answer_callback_query(call.id, "You cannot block the admin.", show_alert=True)
            return
        db_exec("UPDATE bot_users SET banned=? WHERE chat_id=?", (1 if action == "ban" else 0, cid))
        row = get_bot_user(cid)
        if row:
            body, mk = render_profile(row, bot)
            edit(body, mk)
        if action == "ban":
            safe_send(bot, cid, "🚫 You have been blocked from using this bot.")
        else:
            safe_send(bot, cid, "✅ You can use the bot again. Send any message to continue.")
        bot.answer_callback_query(call.id, "Done")

    elif data.startswith("cr:"):
        _, cid, delta = data.split(":")
        delta = int(delta)
        db_exec("UPDATE bot_users SET credits = MAX(0, credits + ?) WHERE chat_id=?", (delta, cid))
        cur = get_bot_user(cid)
        if cur and cur["credits"] < KEY_COST_CREDITS and not cur.get("refill_at"):
            start_refill_timer(cid)
        row = get_bot_user(cid)
        if row:
            body, mk = render_profile(row, bot)
            edit(body, mk)
            sign = "+" if delta > 0 else ""
            safe_send(bot, cid, f"💰 Your credits were changed by the admin: <b>{sign}{delta}</b>\nBalance: <b>{row['credits']}</b>")
        bot.answer_callback_query(call.id, "Credits updated")

    elif data.startswith("keys:"):
        send_keys_of_owner(bot, chat_id, data.split(":")[1])
        bot.answer_callback_query(call.id)

    elif data.startswith("kt:"):
        key_id = int(data.split(":")[1])
        row = db_query("SELECT * FROM users WHERE id=?", (key_id,), one=True)
        if not row:
            bot.answer_callback_query(call.id, "Key not found", show_alert=True)
            return
        new_val = 0 if row["enabled"] else 1
        db_exec("UPDATE users SET enabled=? WHERE id=?", (new_val, key_id))
        schedule_xray_restart()
        row = db_query("SELECT * FROM users WHERE id=?", (key_id,), one=True)
        body, mk = render_key_card(row)
        edit(body, mk)
        bot.answer_callback_query(call.id, "Key restored" if new_val else "Key revoked")

    elif data.startswith("acfg:"):
        key = db_query("SELECT * FROM users WHERE id=?", (int(data.split(":")[1]),), one=True)
        if not key:
            bot.answer_callback_query(call.id, "Key not found", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        send_key_configs(bot, chat_id, key, f"🔑 <b>Configs for</b> <code>{esc(key['name'])}</code>")

    elif data == "bc_ok":
        state = ADMIN_STATE.pop(chat_id, None)
        if not state or state.get("action") != "bc_confirm":
            bot.answer_callback_query(call.id, "Nothing to send.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Broadcast started")
        edit("📢 Broadcast started…")
        threading.Thread(
            target=run_broadcast,
            args=(bot, chat_id, chat_id, state["msg_id"]),
            daemon=True
        ).start()

    elif data == "bc_no":
        ADMIN_STATE.pop(chat_id, None)
        edit("❌ Broadcast cancelled.")
        bot.answer_callback_query(call.id)
        back_to_admin_menu(bot, chat_id)

    elif data.startswith("ac:"):
        delta = int(data.split(":")[1])
        bot.answer_callback_query(call.id)
        send_all_credits_confirm(bot, chat_id, delta, show_menu=False)

    elif data == "ac_custom":
        bot.answer_callback_query(call.id)
        ask_admin(
            bot, chat_id, "ac_custom",
            "✏️ Send the amount to change for <b>all users</b>, e.g. <code>5</code> to add or <code>-3</code> to remove:"
        )

    elif data.startswith("ac_ok:"):
        delta = int(data.split(":")[1])
        bot.answer_callback_query(call.id, "Applying…")
        edit("⏳ Updating all users…")
        threading.Thread(target=apply_all_credits, args=(bot, chat_id, delta), daemon=True).start()

    elif data == "ac_no":
        bot.answer_callback_query(call.id, "Cancelled")
        edit("❌ Cancelled.")

    elif data == "srv_refresh":
        body, mk = render_server_panel()
        edit(body, mk)
        bot.answer_callback_query(call.id, "Refreshed")

    elif data == "srv_stop":
        mk = telebot.types.InlineKeyboardMarkup()
        mk.row(_ib("⏹ Yes, stop the server", callback_data="srv_stop_ok"), _ib("❌ No", callback_data="srv_refresh"))
        edit("⚠️ <b>Stop the whole server?</b>\nAll users will be disconnected until you start it again.", mk)
        bot.answer_callback_query(call.id)

    elif data == "srv_stop_ok":
        set_setting("server_stopped", "1")
        bot.answer_callback_query(call.id, "Stopping…")
        threading.Thread(target=restart_xray, daemon=True).start()
        time.sleep(1.5)
        body, mk = render_server_panel()
        edit(body, mk)

    elif data == "srv_start":
        set_setting("server_stopped", "0")
        bot.answer_callback_query(call.id, "Starting…")
        threading.Thread(target=restart_xray, daemon=True).start()
        time.sleep(2)
        body, mk = render_server_panel()
        edit(body, mk)

    elif data == "srv_restart":
        set_setting("server_stopped", "0")
        bot.answer_callback_query(call.id, "Restarting…")
        threading.Thread(target=restart_xray, daemon=True).start()
        time.sleep(2)
        body, mk = render_server_panel()
        edit(body, mk)

    elif data == "hl_refresh":
        body, mk = render_health()
        edit(body, mk)
        bot.answer_callback_query(call.id, "Refreshed")

    elif data == "chk_all":
        bot.answer_callback_query(call.id, "Started")
        threading.Thread(target=run_channel_check_all, args=(bot, chat_id), daemon=True).start()

    elif data == "set_pw":
        bot.answer_callback_query(call.id)
        ask_admin(
            bot, chat_id, "set_pw",
            "🔑 Send the <b>new panel password</b> (at least 4 characters).\n"
            "Your message will be deleted right after it is read."
        )

    elif data == "set_url":
        bot.answer_callback_query(call.id)
        ask_admin(
            bot, chat_id, "set_url",
            "🌐 Send the public URL of your panel, e.g. <code>https://your-domain.com</code>"
        )

    else:
        bot.answer_callback_query(call.id)


def handle_user_callback(bot, call):
    tg_user = call.from_user
    chat_id = str(tg_user.id)

    data = call.data or ""

    if data.startswith("pkg:"):
        register_bot_user(tg_user)
        if not gate_user(bot, tg_user):
            bot.answer_callback_query(call.id)
            return
        bot.answer_callback_query(call.id)
        buy_package(bot, chat_id, data.split(":", 1)[1])
        return

    if data.startswith("cfg:"):
        register_bot_user(tg_user)
        if not gate_user(bot, tg_user):
            bot.answer_callback_query(call.id)
            return
        key = db_query(
            "SELECT * FROM users WHERE id=? AND owner_chat_id=?",
            (int(data.split(":")[1]), chat_id), one=True
        )
        if not key:
            bot.answer_callback_query(call.id, "Key not found", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        send_key_configs(bot, chat_id, key, f"🔑 <b>Configs for</b> <code>{esc(key['name'])}</code>")
        return

    if data != "chk":
        bot.answer_callback_query(call.id)
        return

    register_bot_user(tg_user)
    row = get_bot_user(chat_id)
    if row and row["banned"]:
        bot.answer_callback_query(call.id, "You are blocked from using this bot.", show_alert=True)
        return

    status = channel_status(bot, tg_user.id)
    if status is not True:
        if status is False:
            apply_channel_result(bot, chat_id, False, notify=False)
        bot.answer_callback_query(call.id, "❌ You have not joined the channel yet.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "✅ Verified")
    apply_channel_result(bot, chat_id, True)
    grant_join_bonus(bot, chat_id)
    send_main_menu(bot, chat_id)


def handle_start(bot, message, admin_id):
    chat_id = str(message.chat.id)

    if chat_id == admin_id:
        send_banner(
            bot, chat_id,
            "⚡ <b>Hello Admin!</b>\nWelcome to the 🌐 Turbo Tunnel MM management panel.\n"
            "Choose one of the buttons below:",
            admin_menu_markup()
        )
        return

    parts = (message.text or "").split(maxsplit=1)
    referrer = ""
    if len(parts) > 1 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
        referrer = parts[1][4:]

    register_bot_user(message.from_user, referrer)
    if gate_user(bot, message.from_user, banner=True):
        send_main_menu(bot, chat_id)


def register_bot_handlers(bot, admin_id):
    @bot.message_handler(commands=["start"])
    def on_start(message):
        if message.chat.type != "private":
            return
        try:
            handle_start(bot, message, admin_id)
        except Exception as e:
            print("[Telegram Bot] /start error:", e)

    @bot.message_handler(func=lambda m: True, content_types=telebot.util.content_type_media)
    def on_message(message):
        if message.chat.type != "private":
            return
        try:
            if str(message.chat.id) == admin_id:
                handle_admin_message(bot, message)
            else:
                handle_user_message(bot, message)
        except Exception as e:
            print("[Telegram Bot] message error:", e)

    @bot.callback_query_handler(func=lambda c: True)
    def on_callback(call):
        try:
            if str(call.from_user.id) == admin_id and call.data != "chk":
                handle_admin_callback(bot, call, admin_id)
            else:
                handle_user_callback(bot, call)
        except Exception as e:
            print("[Telegram Bot] callback error:", e)
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass


def run_telegram_bot_thread():
    if not telebot:
        print("[Telegram Bot] telebot module not found. Skipping bot launch.")
        return

    while True:
        try:
            tg_token = get_setting("telegram_token", "").strip()
            tg_admin_id = get_setting("telegram_admin_id", "").strip()

            if not tg_token:
                time.sleep(10)
                continue

            bot = telebot.TeleBot(tg_token)
            register_bot_handlers(bot, tg_admin_id)

            print("[Telegram Bot] Bot polling started successfully.")
            bot.infinity_polling(timeout=10, long_polling_timeout=5)

        except Exception as e:
            print("[Telegram Bot] Polling crash/error, restarting in 10s:", e)
            time.sleep(10)


def start_telegram_bot():
    t = threading.Thread(target=run_telegram_bot_thread, daemon=True)
    t.start()
    threading.Thread(target=credit_refill_worker, daemon=True).start()
    threading.Thread(target=channel_enforcer_worker, daemon=True).start()

if __name__ == "__main__":
    init_db()
    restart_xray()
    start_nginx()
    start_stats_collector()
    start_telegram_bot()
    app.run(host="127.0.0.1", port=FLASK_PORT)
