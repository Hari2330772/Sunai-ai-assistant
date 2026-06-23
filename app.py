"""
SUNAI Pro - Flask Backend with Groq API
Run: python app.py
"""
import os, json, hashlib, uuid
from flask import Flask, request, jsonify, render_template, session, redirect
from flask_cors import CORS
from datetime import datetime, date
from functools import wraps
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sunai-secret-2024")
CORS(app)

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

FREE_LIMIT = 10
USERS_FILE = "users.json"

def load_users():
    if not os.path.exists(USERS_FILE): return {}
    with open(USERS_FILE) as f: return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w") as f: json.dump(users, f, indent=2)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "login_required"}), 401
        return f(*args, **kwargs)
    return decorated

def get_user():
    users = load_users()
    return users.get(session.get("user_id"))

@app.route("/")
def index():
    user = get_user()
    return render_template("index.html", user=user, free_limit=FREE_LIMIT)

@app.route("/register", methods=["POST"])
def register():
    data = request.json
    name  = data.get("name","").strip()
    email = data.get("email","").strip().lower()
    pw    = data.get("password","")
    if not name or not email or not pw:
        return jsonify({"error": "All fields required"}), 400
    users = load_users()
    if any(u["email"] == email for u in users.values()):
        return jsonify({"error": "Email already registered"}), 400
    uid = str(uuid.uuid4())
    users[uid] = {
        "id": uid, "name": name, "email": email,
        "password": hash_pw(pw), "plan": "free",
        "joined": str(date.today()),
        "usage": {str(date.today()): 0}
    }
    save_users(users)
    session["user_id"] = uid
    return jsonify({"success": True, "name": name, "plan": "free"})

@app.route("/login", methods=["POST"])
def login():
    data  = request.json
    email = data.get("email","").strip().lower()
    pw    = data.get("password","")
    users = load_users()
    user  = next((u for u in users.values()
                  if u["email"] == email and u["password"] == hash_pw(pw)), None)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    session["user_id"] = user["id"]
    return jsonify({"success": True, "name": user["name"], "plan": user["plan"]})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/me")
@login_required
def me():
    user  = get_user()
    today = str(date.today())
    used  = user.get("usage", {}).get(today, 0)
    remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - used)
    return jsonify({
        "name": user["name"], "plan": user["plan"],
        "used_today": used, "remaining": remaining
    })

@app.route("/upgrade", methods=["POST"])
@login_required
def upgrade():
    users = load_users()
    uid = session["user_id"]
    users[uid]["plan"] = "pro"
    save_users(users)
    return jsonify({"success": True, "message": "Upgraded to Pro!"})

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    users = load_users()
    uid   = session["user_id"]
    user  = users[uid]
    today = str(date.today())

    if "usage" not in user:
        user["usage"] = {}

    used = user["usage"].get(today, 0)

    if user["plan"] == "free" and used >= FREE_LIMIT:
        return jsonify({
            "error": "limit_reached",
            "message": f"You've used all {FREE_LIMIT} free queries today. Upgrade to Pro!"
        }), 429

    messages = request.json.get("messages", [])
    if not messages:
        return jsonify({"error": "No message provided"}), 400

    try:
        response = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[{
                "role": "system",
                "content": "You are SUNAI, a brilliant and friendly AI assistant. Help users with coding, science, career, math, and any topic. Be clear, concise and helpful."
            }] + messages,
            max_tokens=1000
        )
        reply = response.choices[0].message.content

        user["usage"][today] = used + 1
        users[uid] = user
        save_users(users)

        remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - user["usage"][today])

        return jsonify({
            "reply": reply,
            "remaining": remaining,
            "plan": user["plan"]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/sitemap.xml")
def sitemap():
    base = os.environ.get("SITE_URL", "https://sunai-ai-assistant-1.onrender.com")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
</urlset>"""
    return app.response_class(xml, mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    base = os.environ.get("SITE_URL", "https://sunai-ai-assistant-1.onrender.com")
    return app.response_class(
        f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml",
        mimetype="text/plain"
    )

if __name__ == "__main__":
    app.run(debug=True, port=5000)