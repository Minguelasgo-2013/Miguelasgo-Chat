#!/usr/bin/env python3
"""
Miguelasgo Chat - Servidor principal
CORREGIDO para Render.com
"""

import json
import os
import uuid
import hashlib
import time
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from http.cookies import SimpleCookie

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

print(f"📁 Directorio de datos: {DATA_DIR}")
print(f"📁 Directorio base: {BASE_DIR}")

USERS_FILE   = os.path.join(DATA_DIR, "users.json")
CHATS_FILE   = os.path.join(DATA_DIR, "chats.json")
MAILS_FILE   = os.path.join(DATA_DIR, "mails.json")
ADMIN_FILE   = os.path.join(DATA_DIR, "admins.json")
SESSION_TTL  = 86400

# ─────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────
def ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    for f in [USERS_FILE, CHATS_FILE, MAILS_FILE, ADMIN_FILE]:
        if not os.path.exists(f):
            with open(f, "w") as fp:
                json.dump([], fp)
    admins = load(ADMIN_FILE)
    if not any(a.get("username") == "admin" for a in admins):
        admins.append({
            "username": "admin",
            "password_hash": hashlib.sha256("admin123".encode()).hexdigest(),
            "created_at": now()
        })
        save(ADMIN_FILE, admins)

def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"❌ Error guardando {path}: {e}")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def new_id():
    return str(uuid.uuid4())

def now():
    return int(time.time() * 1000)

# ─────────────────────────────────────────────
# SESIONES
# ─────────────────────────────────────────────
sessions = {}
sse_clients = {}
sse_lock = threading.Lock()

def create_session(username):
    token = str(uuid.uuid4())
    sessions[token] = {"username": username, "expires": time.time() + SESSION_TTL}
    return token

def get_session(handler):
    cookie_header = handler.headers.get("Cookie", "")
    cookies = SimpleCookie(cookie_header)
    token = cookies.get("session")
    if not token:
        return None
    token_val = token.value
    sess = sessions.get(token_val)
    if not sess or sess["expires"] < time.time():
        sessions.pop(token_val, None)
        return None
    return sess["username"]

def delete_session(handler):
    cookie_header = handler.headers.get("Cookie", "")
    cookies = SimpleCookie(cookie_header)
    token = cookies.get("session")
    if token:
        sessions.pop(token.value, None)

def sse_push(to_user, event_type, data):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        queues = sse_clients.get(to_user, [])
        for q in queues:
            q.append(msg)

# ─────────────────────────────────────────────
# HANDLER
# ─────────────────────────────────────────────
class ChatHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, code, msg):
        self.send_json(code, {"error": msg})

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def get_index_html(self):
        html_path = os.path.join(BASE_DIR, "index.html")
        try:
            with open(html_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b'<h1>index.html no encontrado</h1>'

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        print(f"📥 GET: {path}")  # Log para depuración

        # Servir index.html
        if path == "/" or path == "/index.html":
            body = self.get_index_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        user = get_session(self)

        # ─── API: /api/me ───
        if path == "/api/me":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            users = load(USERS_FILE)
            u = next((x for x in users if x["username"] == user), None)
            if not u:
                self.send_error_json(404, "Usuario no encontrado")
                return
            self.send_json(200, {
                "username": u["username"],
                "avatar": u.get("avatar", "👤"),
                "pubkey": u.get("pubkey", ""),
                "created_at": u.get("created_at", 0)
            })
            return

        # ─── API: /api/users ───
        if path == "/api/users":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            users = load(USERS_FILE)
            result = [{"username": u["username"], "avatar": u.get("avatar","👤"), "pubkey": u.get("pubkey","")}
                      for u in users if u["username"] != user]
            self.send_json(200, result)
            return

        # ─── API: /api/chat/history ───
        if path == "/api/chat/history":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            other = qs.get("with", [None])[0]
            if not other:
                self.send_error_json(400, "Falta 'with'")
                return
            chats = load(CHATS_FILE)
            conv = [m for m in chats if
                    (m["from"] == user and m["to"] == other) or
                    (m["from"] == other and m["to"] == user)]
            self.send_json(200, conv)
            return

        # ─── API: /api/mail/inbox ───
        if path == "/api/mail/inbox":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            inbox = [m for m in mails if m["to"] == user and not m.get("draft") and not m.get("deleted_by_to")]
            self.send_json(200, sorted(inbox, key=lambda x: x["timestamp"], reverse=True))
            return

        # ─── API: /api/mail/sent ───
        if path == "/api/mail/sent":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            sent = [m for m in mails if m["from"] == user and not m.get("draft") and not m.get("deleted_by_from")]
            self.send_json(200, sorted(sent, key=lambda x: x["timestamp"], reverse=True))
            return

        # ─── API: /api/mail/drafts ───
        if path == "/api/mail/drafts":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            drafts = [m for m in mails if m["from"] == user and m.get("draft")]
            self.send_json(200, sorted(drafts, key=lambda x: x["timestamp"], reverse=True))
            return

        # ─── API: /api/unread ───
        if path == "/api/unread":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            chats = load(CHATS_FILE)
            mails = load(MAILS_FILE)
            unread_chat = sum(1 for m in chats if m["to"] == user and not m.get("read"))
            unread_mail = sum(1 for m in mails if m["to"] == user and not m.get("read") and not m.get("draft"))
            self.send_json(200, {"chat": unread_chat, "mail": unread_mail})
            return

        # ─── API: /api/stream (SSE) ───
        if path == "/api/stream":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            queue = []
            with sse_lock:
                if user not in sse_clients:
                    sse_clients[user] = []
                sse_clients[user].append(queue)

            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    if queue:
                        msg = queue.pop(0)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    else:
                        try:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        except Exception:
                            break
                        time.sleep(1)
            except Exception:
                pass
            finally:
                with sse_lock:
                    if user in sse_clients and queue in sse_clients[user]:
                        sse_clients[user].remove(queue)
                        if not sse_clients[user]:
                            del sse_clients[user]
            return

        # Si no coincide con ninguna ruta
        self.send_error_json(404, f"Ruta no encontrada: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body()

        print(f"📤 POST: {path}")  # Log para depuración

        # ─── /api/register ───
        if path == "/api/register":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            pubkey = body.get("pubkey") or ""
            avatar = body.get("avatar") or "👤"

            if not username or not password:
                self.send_error_json(400, "Faltan datos")
                return
            if len(username) < 3 or len(username) > 20:
                self.send_error_json(400, "El nombre debe tener entre 3 y 20 caracteres")
                return
            if not re.match(r'^[a-zA-Z0-9_]+$', username):
                self.send_error_json(400, "Solo letras, números y _")
                return

            users = load(USERS_FILE)
            if any(u["username"].lower() == username.lower() for u in users):
                self.send_error_json(409, "Usuario ya existe")
                return

            users.append({
                "id": new_id(),
                "username": username,
                "password_hash": hash_password(password),
                "pubkey": pubkey,
                "avatar": avatar,
                "created_at": now()
            })
            save(USERS_FILE, users)

            token = create_session(username)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"session={token}; HttpOnly; Path=/; Max-Age={SESSION_TTL}")
            body_resp = json.dumps({"ok": True, "username": username}).encode()
            self.send_header("Content-Length", len(body_resp))
            self.end_headers()
            self.wfile.write(body_resp)
            return

        # ─── /api/login ───
        if path == "/api/login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            users = load(USERS_FILE)
            u = next((x for x in users if x["username"].lower() == username.lower()), None)
            if not u or u["password_hash"] != hash_password(password):
                self.send_error_json(401, "Usuario o contraseña incorrectos")
                return

            token = create_session(u["username"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"session={token}; HttpOnly; Path=/; Max-Age={SESSION_TTL}")
            body_resp = json.dumps({"ok": True, "username": u["username"]}).encode()
            self.send_header("Content-Length", len(body_resp))
            self.end_headers()
            self.wfile.write(body_resp)
            return

        # ─── /api/logout ───
        if path == "/api/logout":
            delete_session(self)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=; HttpOnly; Path=/; Max-Age=0")
            body_resp = b'{"ok":true}'
            self.send_header("Content-Length", len(body_resp))
            self.end_headers()
            self.wfile.write(body_resp)
            return

        user = get_session(self)
        if not user:
            self.send_error_json(401, "No autenticado")
            return

        # ─── /api/chat/send ───
        if path == "/api/chat/send":
            to = body.get("to")
            content = body.get("content")
            iv = body.get("iv")
            encKey = body.get("encKey")

            if not to or not content or not iv:
                self.send_error_json(400, "Faltan datos")
                return

            users = load(USERS_FILE)
            if not any(u["username"] == to for u in users):
                self.send_error_json(404, "Destinatario no encontrado")
                return

            msg = {
                "id": new_id(),
                "from": user,
                "to": to,
                "content": content,
                "iv": iv,
                "encKey": encKey or iv,
                "timestamp": now(),
                "read": False
            }
            chats = load(CHATS_FILE)
            chats.append(msg)
            save(CHATS_FILE, chats)

            sse_push(to, "chat", msg)
            self.send_json(200, {"ok": True, "id": msg["id"]})
            return

        # ─── /api/mail/send ───
        if path == "/api/mail/send":
            to = body.get("to")
            subject = body.get("subject")
            content = body.get("content")
            iv_subject = body.get("iv_subject")
            iv_body = body.get("iv_body")
            draft_id = body.get("draft_id")

            if not to or not subject or not content:
                self.send_error_json(400, "Faltan datos")
                return

            mails = load(MAILS_FILE)

            if draft_id:
                existing = next((m for m in mails if m["id"] == draft_id and m["from"] == user), None)
                if existing:
                    existing["to"] = to
                    existing["subject"] = subject
                    existing["content"] = content
                    existing["iv_subject"] = iv_subject
                    existing["iv_body"] = iv_body
                    existing["timestamp"] = now()
                    existing["draft"] = False
                    save(MAILS_FILE, mails)
                    self.send_json(200, {"ok": True, "id": existing["id"]})
                    return

            mail = {
                "id": new_id(),
                "from": user,
                "to": to,
                "subject": subject,
                "content": content,
                "iv_subject": iv_subject or "",
                "iv_body": iv_body or "",
                "timestamp": now(),
                "read": False,
                "draft": False
            }
            mails.append(mail)
            save(MAILS_FILE, mails)

            sse_push(to, "mail", {"id": mail["id"], "from": user, "timestamp": mail["timestamp"]})
            self.send_json(200, {"ok": True, "id": mail["id"]})
            return

        # ─── /api/mail/draft ───
        if path == "/api/mail/draft":
            to = body.get("to") or ""
            subject = body.get("subject") or ""
            content = body.get("content") or ""
            iv_subject = body.get("iv_subject") or ""
            iv_body = body.get("iv_body") or ""
            draft_id = body.get("draft_id")

            mails = load(MAILS_FILE)

            if draft_id:
                existing = next((m for m in mails if m["id"] == draft_id and m["from"] == user), None)
                if existing:
                    existing["to"] = to or existing["to"]
                    existing["subject"] = subject or existing["subject"]
                    existing["content"] = content or existing["content"]
                    existing["iv_subject"] = iv_subject or existing.get("iv_subject")
                    existing["iv_body"] = iv_body or existing.get("iv_body")
                    existing["timestamp"] = now()
                    existing["draft"] = True
                    save(MAILS_FILE, mails)
                    self.send_json(200, {"ok": True, "id": existing["id"]})
                    return

            mail = {
                "id": new_id(),
                "from": user,
                "to": to,
                "subject": subject,
                "content": content,
                "iv_subject": iv_subject,
                "iv_body": iv_body,
                "timestamp": now(),
                "read": False,
                "draft": True
            }
            mails.append(mail)
            save(MAILS_FILE, mails)
            self.send_json(200, {"ok": True, "id": mail["id"]})
            return

        # ─── /api/mail/delete ───
        if path == "/api/mail/delete":
            mail_id = body.get("id")
            mails = load(MAILS_FILE)
            mail = next((m for m in mails if m["id"] == mail_id), None)
            if not mail:
                self.send_error_json(404, "No encontrado")
                return
            if mail["from"] == user:
                mail["deleted_by_from"] = True
            if mail["to"] == user:
                mail["deleted_by_to"] = True
            if mail.get("deleted_by_from") and mail.get("deleted_by_to"):
                mails = [m for m in mails if m["id"] != mail_id]
            save(MAILS_FILE, mails)
            self.send_json(200, {"ok": True})
            return

        self.send_error_json(404, f"Ruta no encontrada: {path}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    ensure_data()
    server = HTTPServer((HOST, PORT), ChatHandler)
    print(f"🔒 Miguelasgo Chat en http://0.0.0.0:{PORT}")
    print(f"📁 Datos: {DATA_DIR}")
    print(f"👑 Admin: admin / admin123")
    print("   Ctrl+C para parar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Servidor parado")
