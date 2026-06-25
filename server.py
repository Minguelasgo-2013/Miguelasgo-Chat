#!/usr/bin/env python3
"""
Miguelasgo Chat - Servidor principal
Chat en tiempo real + Correo interno con cifrado E2E
Python stdlib puro - sin dependencias externas
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

# IMPORTANTE: Usar directorio persistente en Render si existe
RENDER_PERSISTENT = os.environ.get("RENDER_PERSISTENT_DATA", "")
if RENDER_PERSISTENT:
    DATA_DIR = RENDER_PERSISTENT
else:
    DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

USERS_FILE   = os.path.join(DATA_DIR, "users.json")
CHATS_FILE   = os.path.join(DATA_DIR, "chats.json")
MAILS_FILE   = os.path.join(DATA_DIR, "mails.json")
ADMIN_FILE   = os.path.join(DATA_DIR, "admins.json")
SESSION_TTL  = 86400  # 24 horas
ADMIN_USERNAME = "admin"

# ─────────────────────────────────────────────
# RESTO DEL CÓDIGO IGUAL...
# (Mantén todo el resto del código igual)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# ESTADO EN MEMORIA
# ─────────────────────────────────────────────
sessions = {}          # token -> {username, expires}
sse_clients = {}       # username -> [queue, ...]
sse_lock = threading.Lock()

# ─────────────────────────────────────────────
# UTILIDADES DE DATOS
# ─────────────────────────────────────────────
def ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    for f in [USERS_FILE, CHATS_FILE, MAILS_FILE]:
        if not os.path.exists(f):
            with open(f, "w") as fp:
                json.dump([], fp)

def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def new_id():
    return str(uuid.uuid4())

def now():
    return int(time.time() * 1000)

# ─────────────────────────────────────────────
# SESIONES
# ─────────────────────────────────────────────
def create_session(username):
    token = str(uuid.uuid4())
    sessions[token] = {"username": username, "expires": time.time() + SESSION_TTL}
    return token

def get_session(request_handler):
    cookie_header = request_handler.headers.get("Cookie", "")
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

def delete_session(request_handler):
    cookie_header = request_handler.headers.get("Cookie", "")
    cookies = SimpleCookie(cookie_header)
    token = cookies.get("session")
    if token:
        sessions.pop(token.value, None)

# ─────────────────────────────────────────────
# SSE - SERVIDOR DE EVENTOS
# ─────────────────────────────────────────────
def sse_push(to_user, event_type, data):
    """Envía un evento SSE a todos los clientes de un usuario."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        queues = sse_clients.get(to_user, [])
        for q in queues:
            q.append(msg)

# ─────────────────────────────────────────────
# HANDLER HTTP
# ─────────────────────────────────────────────
class ChatHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silenciar logs

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, code, msg):
        self.send_json(code, {"error": msg})

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def get_index_html(self):
        html_path = os.path.join(os.path.dirname(__file__), "index.html")
        with open(html_path, "rb") as f:
            return f.read()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            try:
                body = self.get_index_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error_json(404, "index.html no encontrado")
            return

        user = get_session(self)

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
                "avatar": u.get("avatar", ""),
                "pubkey": u.get("pubkey", ""),
                "created_at": u.get("created_at", 0)
            })
            return

        if path == "/api/users":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            users = load(USERS_FILE)
            result = [{"username": u["username"], "avatar": u.get("avatar",""), "pubkey": u.get("pubkey","")}
                      for u in users if u["username"] != user]
            self.send_json(200, result)
            return

        if path == "/api/chat/history":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            other = qs.get("with", [None])[0]
            if not other:
                self.send_error_json(400, "Falta parámetro 'with'")
                return
            chats = load(CHATS_FILE)
            conv = [m for m in chats if
                    (m["from"] == user and m["to"] == other) or
                    (m["from"] == other and m["to"] == user)]
            # Marcar como leídos los mensajes recibidos
            changed = False
            for m in conv:
                if m["to"] == user and not m.get("read"):
                    m["read"] = True
                    changed = True
            if changed:
                save(CHATS_FILE, chats)
            self.send_json(200, conv)
            return

        if path == "/api/mail/inbox":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            inbox = [m for m in mails if m["to"] == user and not m.get("draft") and not m.get("deleted_by_to")]
            # Marcar como leídos
            changed = False
            for m in inbox:
                if not m.get("read"):
                    m["read"] = True
                    changed = True
            if changed:
                save(MAILS_FILE, mails)
            self.send_json(200, sorted(inbox, key=lambda x: x["timestamp"], reverse=True))
            return

        if path == "/api/mail/sent":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            sent = [m for m in mails if m["from"] == user and not m.get("draft") and not m.get("deleted_by_from")]
            self.send_json(200, sorted(sent, key=lambda x: x["timestamp"], reverse=True))
            return

        if path == "/api/mail/drafts":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mails = load(MAILS_FILE)
            drafts = [m for m in mails if m["from"] == user and m.get("draft")]
            self.send_json(200, sorted(drafts, key=lambda x: x["timestamp"], reverse=True))
            return

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
                # Ping inicial
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    if queue:
                        msg = queue.pop(0)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    else:
                        # Heartbeat cada 20s
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

        self.send_error_json(404, "Ruta no encontrada")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body()

        if path == "/api/register":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            pubkey   = body.get("pubkey") or ""
            avatar   = body.get("avatar") or "👤"

            if not username or not password:
                self.send_error_json(400, "Faltan datos")
                return
            if len(username) < 3 or len(username) > 20:
                self.send_error_json(400, "El nombre debe tener entre 3 y 20 caracteres")
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

        if path == "/api/profile":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            users = load(USERS_FILE)
            u = next((x for x in users if x["username"] == user), None)
            if not u:
                self.send_error_json(404, "Usuario no encontrado")
                return
            if "avatar" in body:
                u["avatar"] = body["avatar"]
            if "pubkey" in body:
                u["pubkey"] = body["pubkey"]
            save(USERS_FILE, users)
            self.send_json(200, {"ok": True})
            return

        if path == "/api/chat/send":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            to      = body.get("to")
            content = body.get("content")  # texto cifrado en base64
            iv      = body.get("iv")       # vector de inicialización en base64

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
                "timestamp": now(),
                "read": False
            }
            chats = load(CHATS_FILE)
            chats.append(msg)
            save(CHATS_FILE, chats)

            # Notificar al receptor por SSE
            sse_push(to, "chat", msg)

            self.send_json(200, {"ok": True, "id": msg["id"]})
            return

        if path == "/api/mail/send" or path == "/api/mail/draft":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            is_draft = path == "/api/mail/draft"
            to         = body.get("to")
            subject    = body.get("subject")   # cifrado
            content    = body.get("content")   # cifrado
            iv_subject = body.get("iv_subject")
            iv_body    = body.get("iv_body")
            draft_id   = body.get("draft_id")  # para actualizar borrador existente

            if not is_draft and (not to or not subject or not content):
                self.send_error_json(400, "Faltan datos")
                return

            mails = load(MAILS_FILE)

            # Si hay draft_id, actualizar en vez de crear
            if draft_id:
                existing = next((m for m in mails if m["id"] == draft_id and m["from"] == user), None)
                if existing:
                    existing["to"]         = to or existing["to"]
                    existing["subject"]    = subject or existing["subject"]
                    existing["content"]    = content or existing["content"]
                    existing["iv_subject"] = iv_subject or existing.get("iv_subject")
                    existing["iv_body"]    = iv_body or existing.get("iv_body")
                    existing["timestamp"]  = now()
                    existing["draft"]      = is_draft
                    save(MAILS_FILE, mails)
                    self.send_json(200, {"ok": True, "id": existing["id"]})
                    return

            mail = {
                "id": new_id(),
                "from": user,
                "to": to or "",
                "subject": subject or "",
                "content": content or "",
                "iv_subject": iv_subject or "",
                "iv_body": iv_body or "",
                "timestamp": now(),
                "read": False,
                "draft": is_draft
            }
            mails.append(mail)
            save(MAILS_FILE, mails)

            if not is_draft and to:
                sse_push(to, "mail", {
                    "id": mail["id"],
                    "from": user,
                    "timestamp": mail["timestamp"]
                })

            self.send_json(200, {"ok": True, "id": mail["id"]})
            return

        if path == "/api/mail/delete":
            if not user:
                self.send_error_json(401, "No autenticado")
                return
            mail_id = body.get("id")
            mails = load(MAILS_FILE)
            mail = next((m for m in mails if m["id"] == mail_id), None)
            if not mail:
                self.send_error_json(404, "Correo no encontrado")
                return
            if mail["from"] == user:
                mail["deleted_by_from"] = True
            if mail["to"] == user:
                mail["deleted_by_to"] = True
            # Si ambos lo borraron, eliminar del todo
            if mail.get("deleted_by_from") and mail.get("deleted_by_to"):
                mails = [m for m in mails if m["id"] != mail_id]
            save(MAILS_FILE, mails)
            self.send_json(200, {"ok": True})
            return

        self.send_error_json(404, "Ruta no encontrada")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        user = get_session(self)
        if not user:
            self.send_error_json(401, "No autenticado")
            return
        # DELETE /api/mail/:id
        if path.startswith("/api/mail/"):
            mail_id = path.split("/")[-1]
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
        self.send_error_json(404, "Ruta no encontrada")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    ensure_data()
    server = HTTPServer((HOST, PORT), ChatHandler)
    print(f"🔒 Miguelasgo Chat corriendo en http://localhost:{PORT}")
    print(f"   Datos en: {DATA_DIR}")
    print("   Ctrl+C para parar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Servidor parado")
