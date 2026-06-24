"""
SUNAI Pro v2 - Flask Backend
New features:
- Chat history saved per user
- Image upload + analysis
- File/PDF upload + analysis
- Auto-login (remember me forever)
- Email pre-filled on login page
"""
import os, json, hashlib, uuid, base64
from flask import Flask, request, jsonify, render_template, session, redirect
from flask_cors import CORS
from datetime import date, timedelta
from functools import wraps
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sunai-secret-2024")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=365)  # stay logged in 1 year
CORS(app, supports_credentials=True)

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

FREE_LIMIT = 10
USERS_FILE  = os.environ.get("USERS_FILE_PATH", "users.json")
HISTORY_DIR = "chat_history"

os.makedirs(HISTORY_DIR, exist_ok=True)

# ── DB helpers ────────────────────────────────────────────────────────────────
def load_users():
    if not os.path.exists(USERS_FILE): return {}
    try:
        with open(USERS_FILE) as f: return json.load(f)
    except: return {}

def save_users(u):
    with open(USERS_FILE, "w") as f: json.dump(u, f, indent=2)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Chat history helpers ──────────────────────────────────────────────────────
def history_path(uid): return os.path.join(HISTORY_DIR, f"{uid}.json")

def load_history(uid):
    p = history_path(uid)
    if not os.path.exists(p): return []
    try:
        with open(p) as f: return json.load(f)
    except: return []

def save_history(uid, history):
    with open(history_path(uid), "w") as f:
        json.dump(history[-100:], f, indent=2)  # keep last 100 messages

# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "login_required"}), 401
        return f(*args, **kwargs)
    return decorated

def get_user():
    return load_users().get(session.get("user_id"))

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", free_limit=FREE_LIMIT)

@app.route("/register", methods=["POST"])
def register():
    data  = request.json or {}
    name  = data.get("name","").strip()
    email = data.get("email","").strip().lower()
    pw    = data.get("password","")
    if not name or not email or not pw:
        return jsonify({"error": "All fields required"}), 400
    users = load_users()
    if any(u["email"] == email for u in users.values()):
        return jsonify({"error": "Email already registered"}), 400
    uid   = str(uuid.uuid4())
    today = str(date.today())
    users[uid] = {"id": uid, "name": name, "email": email,
                  "password": hash_pw(pw), "plan": "free",
                  "joined": today, "usage": {today: 0}}
    save_users(users)
    session.permanent = True
    session["user_id"] = uid
    return jsonify({"success": True, "name": name, "plan": "free", "email": email})

@app.route("/login", methods=["POST"])
def login():
    data  = request.json or {}
    email = data.get("email","").strip().lower()
    pw    = data.get("password","")
    users = load_users()
    user  = next((u for u in users.values()
                  if u["email"] == email and u["password"] == hash_pw(pw)), None)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"success": True, "name": user["name"],
                    "plan": user["plan"], "email": user["email"]})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/me")
@login_required
def me():
    user  = get_user()
    if not user: session.clear(); return jsonify({"error": "not_found"}), 404
    today = str(date.today())
    used  = user.get("usage", {}).get(today, 0)
    remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - used)
    return jsonify({"name": user["name"], "plan": user["plan"],
                    "email": user["email"],
                    "used_today": used, "remaining": remaining})

@app.route("/upgrade", methods=["POST"])
@login_required
def upgrade():
    users = load_users()
    uid   = session["user_id"]
    if uid in users:
        users[uid]["plan"] = "pro"
        save_users(users)
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404

# ── Chat (text) ───────────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    users = load_users()
    uid   = session["user_id"]
    if uid not in users: return jsonify({"error": "not_found"}), 404
    user  = users[uid]
    today = str(date.today())
    user.setdefault("usage", {})
    user["usage"] = {today: user["usage"].get(today, 0)}
    used  = user["usage"][today]
    if user["plan"] == "free" and used >= FREE_LIMIT:
        return jsonify({"error": "limit_reached",
                        "message": f"You've used all {FREE_LIMIT} free queries today. Upgrade to Pro!"}), 429
    messages = (request.json or {}).get("messages", [])
    if not messages: return jsonify({"error": "No message"}), 400
    try:
        resp  = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content":
                "You are SUNAI, a brilliant friendly AI assistant. Help with coding, science, career, math, and any topic. Be clear, concise and helpful."}
            ] + messages, max_tokens=1500)
        reply = resp.choices[0].message.content
        user["usage"][today] = used + 1
        users[uid] = user
        save_users(users)
        # Save to history
        history = load_history(uid)
        history.append({"role": "user",    "content": messages[-1]["content"], "time": str(date.today())})
        history.append({"role": "assistant","content": reply,                   "time": str(date.today())})
        save_history(uid, history)
        remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - user["usage"][today])
        return jsonify({"reply": reply, "remaining": remaining, "plan": user["plan"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Image upload + analysis ───────────────────────────────────────────────────
@app.route("/analyze-image", methods=["POST"])
@login_required
def analyze_image():
    users = load_users()
    uid   = session["user_id"]
    user  = users.get(uid)
    if not user: return jsonify({"error": "not_found"}), 404
    today = str(date.today())
    user.setdefault("usage", {})
    used  = user["usage"].get(today, 0)
    if user["plan"] == "free" and used >= FREE_LIMIT:
        return jsonify({"error": "limit_reached",
                        "message": "Daily limit reached. Upgrade to Pro!"}), 429
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img_file = request.files["image"]
    question = request.form.get("question", "Describe this image in detail.")
    img_data = base64.b64encode(img_file.read()).decode("utf-8")
    mime     = img_file.content_type or "image/jpeg"
    try:
        # Use Groq vision model
        resp = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": [
                {"type": "text",       "text": f"You are SUNAI. {question}"},
                {"type": "image_url",  "image_url": {"url": f"data:{mime};base64,{img_data}"}}
            ]}], max_tokens=1000)
        reply = resp.choices[0].message.content
        user["usage"][today] = used + 1
        users[uid] = user
        save_users(users)
        history = load_history(uid)
        history.append({"role": "user",     "content": f"[Image] {question}", "time": str(date.today())})
        history.append({"role": "assistant", "content": reply,                  "time": str(date.today())})
        save_history(uid, history)
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── File/PDF upload + analysis ────────────────────────────────────────────────
@app.route("/analyze-file", methods=["POST"])
@login_required
def analyze_file():
    users = load_users()
    uid   = session["user_id"]
    user  = users.get(uid)
    if not user: return jsonify({"error": "not_found"}), 404
    today = str(date.today())
    user.setdefault("usage", {})
    used  = user["usage"].get(today, 0)
    if user["plan"] == "free" and used >= FREE_LIMIT:
        return jsonify({"error": "limit_reached",
                        "message": "Daily limit reached. Upgrade to Pro!"}), 429
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file     = request.files["file"]
    question = request.form.get("question", "Summarize this document.")
    fname    = file.filename.lower()
    try:
        if fname.endswith(".pdf"):
            import PyPDF2, io
            reader = PyPDF2.PdfReader(io.BytesIO(file.read()))
            text   = "\n".join(p.extract_text() or "" for p in reader.pages)
        elif fname.endswith((".txt",".py",".js",".html",".css",".csv",".md")):
            text = file.read().decode("utf-8", errors="ignore")
        else:
            return jsonify({"error": "Unsupported file type. Upload PDF, TXT, PY, JS, HTML, CSV or MD"}), 400
        text = text[:8000]  # limit to avoid token overflow
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are SUNAI, a helpful AI assistant."},
                      {"role": "user",   "content": f"File content:\n\n{text}\n\nQuestion: {question}"}],
            max_tokens=1500)
        reply = resp.choices[0].message.content
        user["usage"][today] = used + 1
        users[uid] = user
        save_users(users)
        history = load_history(uid)
        history.append({"role": "user",     "content": f"[File: {file.filename}] {question}", "time": str(date.today())})
        history.append({"role": "assistant", "content": reply,                                  "time": str(date.today())})
        save_history(uid, history)
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Chat history ──────────────────────────────────────────────────────────────
@app.route("/history")
@login_required
def history():
    uid = session["user_id"]
    return jsonify({"history": load_history(uid)})

@app.route("/history/clear", methods=["POST"])
@login_required
def clear_history():
    uid = session["user_id"]
    save_history(uid, [])
    return jsonify({"success": True})

# ── SEO ───────────────────────────────────────────────────────────────────────
@app.route("/sitemap.xml")
def sitemap():
    base = os.environ.get("SITE_URL", "https://sunai-ai-assistant-1.onrender.com")
    xml  = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
</urlset>"""
    return app.response_class(xml, mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    base = os.environ.get("SITE_URL", "https://sunai-ai-assistant-1.onrender.com")
    return app.response_class(
        f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml",
        mimetype="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
