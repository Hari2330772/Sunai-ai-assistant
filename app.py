"""
SUNAI v5.0 — Complete Production Backend
Features:
  v4.0: Streaming responses, markdown, syntax highlighting, stop generation
  v4.1: Google Sign-In, Forgot Password, Email verification
  v4.2: Image generation (Gemini Imagen), Voice chat (Groq Whisper), AI Memory
  v5.0: Admin Dashboard, Analytics, Team Workspaces, Custom Domain, Production optimization
"""
import os
import json
import uuid
import hmac
import hashlib
import time
import base64
import secrets
import magic
import bcrypt
import razorpay
from flask import (Flask, request, jsonify, render_template,
                   session, redirect, abort, Response, stream_with_context)
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from datetime import date, timedelta, datetime
from functools import wraps
from dotenv import load_dotenv
from supabase import create_client, Client
from groq import Groq
import google.generativeai as genai
import smtplib
import re
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()
BRIGHTDATA_API_KEY=os.getenv("BRIGHTDATA_API_KEY")

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development",
    SESSION_COOKIE_HTTPONLY=True,
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,  # 20 MB
)
CORS(app, supports_credentials=True,
     origins=os.environ.get("ALLOWED_ORIGINS", "").split(","))

# ── Rate limiter (Redis in prod, memory in dev) ────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "memory://")
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=REDIS_URL,
)

# ── External clients ──────────────────────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
vision_model   = genai.GenerativeModel("gemini-1.5-flash")
imagen_model   = genai.GenerativeModel("gemini-2.5-flash-preview-image-generation")
rzp_client = razorpay.Client(
    auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"])
)

FREE_LIMIT          = 10
HISTORY_LIMIT       = 100
FREE_HISTORY_TTL    = 30
MEMORY_LIMIT        = 20          # max stored memories per user
ADMIN_EMAILS        = set(os.environ.get("ADMIN_EMAILS", "").split(","))


# ══════════════════════════════════════════════════════════════════════════════
# WEB SEARCH — Serper (primary) + DuckDuckGo (free fallback, no key needed)
# ══════════════════════════════════════════════════════════════════════════════
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# Queries that signal the user needs live / current information
_LIVE_PATTERNS = re.compile(
    r"\b(today|tonight|yesterday|this week|this month|this year|right now|"
    r"current(ly)?|latest|recent(ly)?|new(est)?|just (announced|released|happened)|"
    r"live|breaking|update[sd]?|price|stock|weather|score|result|match|"
    r"news|trending|2024|2025|2026|who is (the )?(current|new)|"
    r"what (is|are) (the )?(current|latest)|how much (is|does)|"
    r"when (is|was|did)|where is|is .* (open|closed|available))\b",
    re.IGNORECASE,
)

def needs_web_search(query: str) -> bool:
    """Return True when the query likely needs live web results."""
    return bool(_LIVE_PATTERNS.search(query))


# ── Provider 1: Serper.dev  (Google Search, 2 500 free queries/month) ─────────
def _search_serper(query: str, num: int = 5) -> list[dict]:
    """
    https://serper.dev  → sign up → API Key (free tier: 2 500/month)
    Set SERPER_API_KEY in .env to enable.
    """
    if not SERPER_API_KEY:
        return []
    try:
        payload = json.dumps({"q": query, "num": num}).encode()
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=payload,
            headers={"X-API-KEY": SERPER_API_KEY,
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        return [
            {"title":   item.get("title", ""),
             "snippet": item.get("snippet", ""),
             "link":    item.get("link", "")}
            for item in data.get("organic", [])[:num]
        ]
    except Exception as e:
        app.logger.warning("Serper search error: %s", e)
        return []


# ── Provider 2: DuckDuckGo  (completely free, no API key) ─────────────────────
def _search_ddg(query: str, num: int = 5) -> list[dict]:
    """
    Uses DuckDuckGo's unofficial instant-answer + HTML endpoints.
    No API key required. Rate-limited by DDG (~1 req/sec is safe).

    Strategy:
      1. Try the DuckDuckGo Instant Answer JSON API (fast, structured).
      2. If that returns nothing useful, scrape the lite HTML endpoint.
    """
    results: list[dict] = []

    # ── Step 1: Instant Answer API ───────────────────────────────────────────
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "SUNAI/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())

        # Abstract result (Wikipedia-style answer)
        if data.get("AbstractText") and data.get("AbstractURL"):
            results.append({
                "title":   data.get("Heading", query),
                "snippet": data["AbstractText"][:300],
                "link":    data["AbstractURL"],
            })

        # Related topics
        for topic in data.get("RelatedTopics", []):
            if len(results) >= num:
                break
            # Topics can be nested groups
            if "Topics" in topic:
                for sub in topic["Topics"]:
                    if len(results) >= num:
                        break
                    text = sub.get("Text", "")
                    url_ = sub.get("FirstURL", "")
                    if text and url_:
                        results.append({"title": text[:80], "snippet": text[:250], "link": url_})
            else:
                text = topic.get("Text", "")
                url_ = topic.get("FirstURL", "")
                if text and url_:
                    results.append({"title": text[:80], "snippet": text[:250], "link": url_})
    except Exception as e:
        app.logger.warning("DDG instant API error: %s", e)

    # ── Step 2: Lite HTML scraper fallback ────────────────────────────────────
    if len(results) < 2:
        try:
            encoded = urllib.parse.quote_plus(query)
            url  = f"https://lite.duckduckgo.com/lite/?q={encoded}"
            req  = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; SUNAI/5.0)",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Parse result snippets — DDG lite uses <a class="result-link"> and
            # nearby <td> cells for snippet text.
            link_pattern   = re.compile(
                r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)<', re.S)
            snippet_pattern = re.compile(
                r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>', re.S)

            links   = link_pattern.findall(html)
            snippets = [re.sub(r'<[^>]+>', '', s).strip()
                        for s in snippet_pattern.findall(html)]

            for i, (href, title) in enumerate(links[:num]):
                snippet = snippets[i] if i < len(snippets) else ""
                # DDG lite uses redirect URLs — extract the real URL
                real_url = href
                uddg_match = re.search(r'uddg=([^&]+)', href)
                if uddg_match:
                    try:
                        real_url = urllib.parse.unquote(uddg_match.group(1))
                    except Exception:
                        pass
                results.append({
                    "title":   title.strip()[:120],
                    "snippet": snippet[:300],
                    "link":    real_url,
                })
                if len(results) >= num:
                    break
        except Exception as e:
            app.logger.warning("DDG HTML scrape error: %s", e)

    return results[:num]


# ── Unified search dispatcher ──────────────────────────────────────────────────
def web_search(query: str, num: int = 5) -> list[dict]:
    """
    Try Serper first (if key is set). Fall back to DuckDuckGo automatically.
    Always returns a list of {title, snippet, link} dicts (may be empty).
    """
    if SERPER_API_KEY:
        results = _search_serper(query, num)
        if results:
            return results
        app.logger.info("Serper returned empty — falling back to DuckDuckGo")

    return _search_ddg(query, num)


# ── Search context formatter ───────────────────────────────────────────────────
def format_search_context(results: list[dict]) -> str:
    """Turn search results into a compact context block injected into the prompt."""
    if not results:
        return ""
    lines = [
        "--- WEB SEARCH RESULTS ---",
        f"Current date: {datetime.utcnow().strftime('%B %d, %Y')} UTC",
        "",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    {r['snippet']}")
        lines.append(f"    Source: {r['link']}")
        lines.append("")
    lines += [
        "--- END OF SEARCH RESULTS ---",
        "Use the above results to answer accurately. "
        "Cite sources using [1], [2] etc. when you use them.",
    ]
    return "\n".join(lines)


# ── Manual search endpoint (frontend can call directly) ───────────────────────
@app.route("/search", methods=["POST"])
@limiter.limit("30/minute")
def search_endpoint():
    """
    Expose web search to the frontend for standalone search queries.
    POST { "query": "...", "num": 5 }
    Returns { "results": [...], "provider": "serper"|"duckduckgo" }
    """
    d     = request.get_json(silent=True) or {}
    query = d.get("query", "").strip()[:300]
    num   = min(int(d.get("num", 5)), 10)
    if not query:
        return jsonify({"error": "Query required"}), 400
    results  = web_search(query, num)
    provider = "serper" if SERPER_API_KEY and results else "duckduckgo"
    return jsonify({"results": results, "provider": provider, "query": query})

# ── Password helpers ──────────────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())

# ── Email helper ──────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, html_body: str):
    try:
        smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")
        from_name = os.environ.get("FROM_NAME", "SUNAI")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{from_name} <{smtp_user}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to, msg.as_string())
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")

# ── Supabase helpers ──────────────────────────────────────────────────────────
def get_user_by_id(uid):
    try:
        res = supabase.table("users").select("*").eq("id", uid).maybe_single().execute()
        return getattr(res, "data", None)
    except Exception as e:
        app.logger.error(f"get_user_by_id: {e}")
        return None

def get_user_by_email(email):
    try:
        res = supabase.table("users").select("*").eq("email", email).maybe_single().execute()
        return getattr(res, "data", None)
    except Exception as e:
        app.logger.error(f"get_user_by_email: {e}")
        return None

def save_user(user: dict):
    supabase.table("users").upsert(user).execute()

def get_today_usage(uid: str) -> int:
    today = str(date.today())
    try:
        res = (supabase.table("usage_counts").select("count")
               .eq("user_id", uid).eq("day", today).maybe_single().execute())
        if not res or not getattr(res, "data", None):
            return 0
        return res.data.get("count", 0)
    except:
        return 0

def increment_usage(uid: str) -> int:
    res = supabase.rpc("increment_daily_usage", {
        "p_user_id": uid,
        "p_day": str(date.today()),
        "p_limit": FREE_LIMIT,
    }).execute()
    return res.data

def get_history(uid: str) -> list:
    res = (supabase.table("chat_history").select("role,content,created_at")
           .eq("user_id", uid).order("id", desc=False).limit(HISTORY_LIMIT).execute())
    return res.data or []

def add_history(uid: str, role: str, content: str):
    supabase.table("chat_history").insert({
        "user_id": uid, "role": role, "content": content,
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

def clear_history_db(uid: str):
    supabase.table("chat_history").delete().eq("user_id", uid).execute()

def prune_old_history(uid: str, plan: str):
    if plan != "free":
        return
    cutoff = (datetime.utcnow() - timedelta(days=FREE_HISTORY_TTL)).isoformat()
    supabase.table("chat_history").delete().eq("user_id", uid).lt("created_at", cutoff).execute()

def set_session_token(uid: str, token: str):
    supabase.table("users").update({"session_token": token}).eq("id", uid).execute()

def verify_session_token(uid: str, token: str) -> bool:
    res = (supabase.table("users").select("session_token")
           .eq("id", uid).maybe_single().execute())
    return res.data and res.data.get("session_token") == token

# ── AI Memory helpers ─────────────────────────────────────────────────────────
def get_memories(uid: str) -> list:
    try:
        res = (supabase.table("memories").select("memory")
               .eq("user_id", uid).order("created_at", desc=True)
               .limit(MEMORY_LIMIT).execute())
        return [r["memory"] for r in (res.data or [])]
    except:
        return []

def save_memory(uid: str, memory: str):
    try:
        supabase.table("memories").insert({
            "user_id": uid, "memory": memory,
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
        # Keep only latest MEMORY_LIMIT memories
        res = (supabase.table("memories").select("id")
               .eq("user_id", uid).order("created_at", desc=False).execute())
        all_ids = [r["id"] for r in (res.data or [])]
        if len(all_ids) > MEMORY_LIMIT:
            to_delete = all_ids[:len(all_ids) - MEMORY_LIMIT]
            supabase.table("memories").delete().in_("id", to_delete).execute()
    except Exception as e:
        app.logger.error(f"save_memory: {e}")

def extract_and_save_memories(uid: str, conversation: list):
    """Use Groq to extract key facts from the conversation and store them."""
    try:
        extract_prompt = (
            "From the following conversation, extract 1-3 short factual memories about the USER "
            "(preferences, name, projects, goals, background). Each memory on its own line. "
            "If nothing notable, reply with just: NONE\n\n"
        )
        convo_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:300]}" for m in conversation[-6:]
        )
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": extract_prompt + convo_text}],
            max_tokens=200,
        )
        text = resp.choices[0].message.content.strip()
        if text.upper() != "NONE":
            for line in text.split("\n"):
                line = line.strip("- •·").strip()
                if len(line) > 10:
                    save_memory(uid, line)
    except Exception as e:
        app.logger.error(f"Memory extraction failed: {e}")

# ── Analytics helper ──────────────────────────────────────────────────────────
def log_event(event_type: str, uid: str = None, meta: dict = None):
    try:
        supabase.table("events").insert({
            "event_type": event_type,
            "user_id": uid,
            "meta": json.dumps(meta or {}),
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
    except:
        pass

# ── Auth decorator ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid   = session.get("user_id")
        token = session.get("session_token")
        if not uid or not token:
            return jsonify({"error": "login_required"}), 401
        if not verify_session_token(uid, token):
            session.clear()
            return jsonify({"error": "session_expired"}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "login_required"}), 401
        user = get_user_by_id(uid)
        if not user or user.get("role") != "admin":
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

def get_current_user() -> dict | None:
    return get_user_by_id(session.get("user_id", ""))

# ── File validation ────────────────────────────────────────────────────────────
ALLOWED_MIME = {
    "application/pdf", "text/plain", "text/x-python", "application/javascript",
    "text/html", "text/css", "text/csv", "text/markdown", "application/octet-stream",
}
ALLOWED_EXT = {".pdf", ".txt", ".py", ".js", ".html", ".css", ".csv", ".md"}

def validate_file(fs) -> tuple[bool, str]:
    ext = os.path.splitext(fs.filename.lower())[1]
    if ext not in ALLOWED_EXT:
        return False, f"Extension {ext} not allowed."
    header = fs.read(2048); fs.seek(0)
    mime = magic.from_buffer(header, mime=True)
    if mime not in ALLOWED_MIME:
        return False, f"File type {mime} not allowed."
    return True, ""

def _check_quota(user: dict):
    if user["plan"] == "pro":
        return True, None
    new_count = increment_usage(user["id"])
    if new_count == -1:
        return False, (jsonify({
            "error": "limit_reached",
            "message": f"You've used all {FREE_LIMIT} free queries today. Upgrade to Pro!",
        }), 429)
    return True, None

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", free_limit=FREE_LIMIT,
                           razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""))

# ── Register ──────────────────────────────────────────────────────────────────
@app.route("/register", methods=["POST"])
@limiter.limit("5/minute; 20/hour")
def register():
    data  = request.get_json(silent=True) or {}
    name  = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    pw    = data.get("password", "")

    if not name or not email or not pw:
        return jsonify({"error": "All fields required"}), 400
    if len(pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if get_user_by_email(email):
        time.sleep(0.3)
        return jsonify({"error": "Email already registered"}), 400

    uid = str(uuid.uuid4())
    session_token = str(uuid.uuid4())
    verification_token = secrets.token_urlsafe(32)
    role = "admin" if email in ADMIN_EMAILS else "user"

    user = {
        "id": uid, "name": name, "email": email,
        "password": hash_pw(pw), "plan": "free",
        "joined": str(date.today()), "session_token": session_token,
        "email_verified": False, "verification_token": verification_token,
        "role": role,
    }
    save_user(user)

    # Send verification email
    site_url = os.environ.get("SITE_URL", "https://sunai.onrender.com")
    verify_link = f"{site_url}/verify-email?token={verification_token}&uid={uid}"
    send_email(email, "Verify your SUNAI account", f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">
      <h2 style="color:#FF6B00">Welcome to SUNAI, {name}!</h2>
      <p>Click the button below to verify your email address.</p>
      <a href="{verify_link}" style="display:inline-block;background:#FF6B00;color:white;
         padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:600;margin:20px 0">
        Verify Email
      </a>
      <p style="color:#888;font-size:12px">This link expires in 24 hours.</p>
    </div>""")

    session.permanent = True
    session["user_id"] = uid
    session["session_token"] = session_token
    log_event("register", uid)
    return jsonify({"success": True, "name": name, "plan": "free", "email_verified": False})

# ── Email verification ─────────────────────────────────────────────────────────
@app.route("/verify-email")
def verify_email():
    token = request.args.get("token", "")
    uid   = request.args.get("uid", "")
    user  = get_user_by_id(uid)
    if not user or user.get("verification_token") != token:
        return "<h3 style='font-family:sans-serif;color:red'>Invalid or expired verification link.</h3>", 400
    supabase.table("users").update({
        "email_verified": True, "verification_token": None
    }).eq("id", uid).execute()
    return redirect("/?verified=1")

# ── Resend verification ────────────────────────────────────────────────────────
@app.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    user = get_current_user()
    if not user or user.get("email_verified"):
        return jsonify({"error": "Already verified"}), 400
    token = secrets.token_urlsafe(32)
    supabase.table("users").update({"verification_token": token}).eq("id", user["id"]).execute()
    site_url = os.environ.get("SITE_URL", "https://sunai.onrender.com")
    verify_link = f"{site_url}/verify-email?token={token}&uid={user['id']}"
    send_email(user["email"], "Verify your SUNAI account", f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">
      <h2 style="color:#FF6B00">Verify your SUNAI email</h2>
      <a href="{verify_link}" style="display:inline-block;background:#FF6B00;color:white;
         padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:600;margin:20px 0">
        Verify Email
      </a>
    </div>""")
    return jsonify({"success": True})

# ── Login ──────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["POST"])
@limiter.limit("10/minute; 30/hour")
def login():
    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    pw    = data.get("password", "")
    user  = get_user_by_email(email)
    dummy = "$2b$12$invalidhashfortimingpurposesonly000000000000000000000000"
    stored = user["password"] if user else dummy
    pw_ok = check_pw(pw, stored)
    if not user or not pw_ok:
        time.sleep(0.3)
        return jsonify({"error": "Invalid email or password"}), 401
    session_token = str(uuid.uuid4())
    set_session_token(user["id"], session_token)
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_token"] = session_token
    log_event("login", user["id"])
    return jsonify({
        "success": True, "name": user["name"], "plan": user["plan"],
        "email_verified": user.get("email_verified", True),
    })

# ── Google OAuth ───────────────────────────────────────────────────────────────
@app.route("/auth/google")
def google_auth():
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [os.environ.get("SITE_URL", "") + "/auth/google/callback"],
            }
        },
        scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile"],
    )
    flow.redirect_uri = os.environ.get("SITE_URL", "") + "/auth/google/callback"
    auth_url, state = flow.authorization_url(access_type="offline", prompt="select_account")
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/auth/google/callback")
def google_callback():
    try:
        from google_auth_oauthlib.flow import Flow
        import google.auth.transport.requests
        import requests as pyrequests
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [os.environ.get("SITE_URL", "") + "/auth/google/callback"],
                }
            },
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"],
            state=session.get("oauth_state"),
        )
        flow.redirect_uri = os.environ.get("SITE_URL", "") + "/auth/google/callback"
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        resp = pyrequests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"}
        )
        info = resp.json()
        email = info.get("email", "").lower()
        name  = info.get("name", email.split("@")[0])

        user = get_user_by_email(email)
        if not user:
            uid = str(uuid.uuid4())
            role = "admin" if email in ADMIN_EMAILS else "user"
            user = {
                "id": uid, "name": name, "email": email,
                "password": hash_pw(secrets.token_hex(16)),
                "plan": "free", "joined": str(date.today()),
                "email_verified": True, "role": role,
            }
            save_user(user)
            log_event("register_google", uid)

        session_token = str(uuid.uuid4())
        set_session_token(user["id"], session_token)
        session.permanent = True
        session["user_id"] = user["id"]
        session["session_token"] = session_token
        log_event("login_google", user["id"])
        return redirect("/")
    except Exception as e:
        app.logger.exception("Google OAuth callback failed")
        return redirect("/?error=oauth_failed")

# ── Forgot password ────────────────────────────────────────────────────────────
@app.route("/forgot-password", methods=["POST"])
@limiter.limit("3/minute; 10/hour")
def forgot_password():
    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    user  = get_user_by_email(email)
    # Always return success to prevent enumeration
    if user:
        token   = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        supabase.table("users").update({
            "reset_token": token, "reset_expires": expires
        }).eq("id", user["id"]).execute()
        site_url = os.environ.get("SITE_URL", "https://sunai.onrender.com")
        reset_link = f"{site_url}/reset-password?token={token}&uid={user['id']}"
        send_email(email, "Reset your SUNAI password", f"""
        <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">
          <h2 style="color:#FF6B00">Reset your password</h2>
          <p>Click the button below to reset your SUNAI password. This link expires in 1 hour.</p>
          <a href="{reset_link}" style="display:inline-block;background:#FF6B00;color:white;
             padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:600;margin:20px 0">
            Reset Password
          </a>
        </div>""")
    return jsonify({"success": True, "message": "If that email exists, a reset link was sent."})

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if request.method == "GET":
        token = request.args.get("token", "")
        uid   = request.args.get("uid", "")
        return render_template("index.html", free_limit=FREE_LIMIT,
                               razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
                               reset_token=token, reset_uid=uid)
    data  = request.get_json(silent=True) or {}
    token = data.get("token", "")
    uid   = data.get("uid", "")
    pw    = data.get("password", "")
    if len(pw) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    user = get_user_by_id(uid)
    if not user or user.get("reset_token") != token:
        return jsonify({"error": "Invalid or expired reset link"}), 400
    try:
        expires = datetime.fromisoformat(user.get("reset_expires", "2000-01-01"))
        if datetime.utcnow() > expires:
            return jsonify({"error": "Reset link has expired"}), 400
    except:
        return jsonify({"error": "Invalid reset link"}), 400
    supabase.table("users").update({
        "password": hash_pw(pw), "reset_token": None, "reset_expires": None
    }).eq("id", uid).execute()
    return jsonify({"success": True})

# ── Logout ─────────────────────────────────────────────────────────────────────
@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        set_session_token(uid, "")
    session.clear()
    return redirect("/")

# ── Me ─────────────────────────────────────────────────────────────────────────
@app.route("/me")
@login_required
def me():
    user = get_current_user()
    if not user:
        session.clear()
        return jsonify({"error": "not_found"}), 404
    used      = get_today_usage(user["id"])
    remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - used)
    prune_old_history(user["id"], user["plan"])
    return jsonify({
        "name": user["name"], "plan": user["plan"],
        "email": user.get("email", ""),
        "email_verified": user.get("email_verified", True),
        "role": user.get("role", "user"),
        "used_today": used, "remaining": remaining,
    })

# ── Razorpay ───────────────────────────────────────────────────────────────────
@app.route("/create-order", methods=["POST"])
@login_required
@limiter.limit("5/minute")
def create_order():
    try:
        order = rzp_client.order.create({
            "amount": 19900, "currency": "INR", "payment_capture": 1,
        })
        return jsonify({"order_id": order["id"], "amount": order["amount"]})
    except Exception:
        app.logger.exception("Razorpay order creation failed")
        return jsonify({"error": "Could not initiate payment."}), 500

@app.route("/verify-payment", methods=["POST"])
@login_required
@limiter.limit("5/minute")
def verify_payment():
    data       = request.get_json(silent=True) or {}
    order_id   = data.get("razorpay_order_id", "")
    payment_id = data.get("razorpay_payment_id", "")
    signature  = data.get("razorpay_signature", "")
    if not all([order_id, payment_id, signature]):
        return jsonify({"error": "Missing payment fields"}), 400
    key_secret = os.environ["RAZORPAY_KEY_SECRET"].encode()
    payload    = f"{order_id}|{payment_id}".encode()
    expected   = hmac.new(key_secret, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Payment verification failed"}), 400
    uid = session["user_id"]
    supabase.table("users").update({"plan": "pro"}).eq("id", uid).execute()
    log_event("upgrade_pro", uid, {"payment_id": payment_id})
    return jsonify({"success": True, "message": "Upgraded to Pro!"})

# ── Streaming Chat ─────────────────────────────────────────────────────────────
@app.route("/chat/stream", methods=["POST"])
@login_required
@limiter.limit("60/minute")
def chat_stream():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    allowed, err = _check_quota(user)
    if not allowed:
        return err

    messages = (request.get_json(silent=True) or {}).get("messages", [])
    if not messages:
        return jsonify({"error": "No message provided"}), 400

    clean_messages = [
        {"role": m["role"] if m.get("role") in ("user", "assistant") else "user",
         "content": str(m.get("content", ""))[:8000]}
        for m in messages[-40:]
    ]

    # Inject AI memories into system prompt
    memories = get_memories(user["id"])
    mem_block = ""
    if memories:
        mem_block = "\n\nKnown facts about this user:\n" + "\n".join(f"- {m}" for m in memories)

    # Web search: check last user message for live-info signals
    last_user_msg = next((m["content"] for m in reversed(clean_messages) if m["role"] == "user"), "")
    search_results = web_search(last_user_msg) if needs_web_search(last_user_msg) else []
    search_context = format_search_context(search_results)

    system_prompt = (
        "You are SUNAI, a brilliant friendly AI assistant. "
        "Help with coding, science, career, math, and any topic. Be clear, concise and helpful. "
        "Your training has a knowledge cutoff, so when WEB SEARCH RESULTS are provided below, "
        "always prioritise them for current facts, prices, news, or recent events. "
        "Cite sources using [1], [2] etc. when you use search results."
        + (("\n\n" + search_context) if search_context else "")
        + mem_block
    )

    # Tell the frontend if we ran a search (so it can show a small indicator)
    did_search = bool(search_results)
    search_provider = "serper" if (SERPER_API_KEY and did_search) else "duckduckgo"

    def generate():
        if did_search:
            yield f"data: {json.dumps({'searching': True, 'provider': search_provider, 'sources': [r['link'] for r in search_results]})}\n\n"

        full_reply = []
        try:
            stream = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + clean_messages,
                max_tokens=2048,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_reply.append(delta)
                    yield f"data: {json.dumps({'token': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        # Save to DB after stream completes
        final_reply = "".join(full_reply)
        uid = user["id"]
        if clean_messages:
            add_history(uid, "user", clean_messages[-1]["content"])
        add_history(uid, "assistant", final_reply)

        # Background memory extraction (every 4th message)
        try:
            count = len(clean_messages)
            if count % 4 == 0:
                extract_and_save_memories(uid, clean_messages + [{"role": "assistant", "content": final_reply}])
        except:
            pass

        used = get_today_usage(uid)
        remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - used)
        yield f"data: {json.dumps({'done': True, 'remaining': remaining, 'plan': user['plan']})}\n\n"

    return Response(stream_with_context(generate()),
                    content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

# ── Non-streaming chat (fallback) ──────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
@login_required
@limiter.limit("60/minute")
def chat():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    allowed, err = _check_quota(user)
    if not allowed:
        return err
    messages = (request.get_json(silent=True) or {}).get("messages", [])
    if not messages:
        return jsonify({"error": "No message provided"}), 400
    clean_messages = [
        {"role": m["role"] if m.get("role") in ("user", "assistant") else "user",
         "content": str(m.get("content", ""))[:8000]}
        for m in messages[-40:]
    ]
    memories  = get_memories(user["id"])
    mem_block = ("\n\nKnown facts about this user:\n" + "\n".join(f"- {m}" for m in memories)) if memories else ""
    last_user_msg = next((m["content"] for m in reversed(clean_messages) if m["role"] == "user"), "")
    search_results = web_search(last_user_msg) if needs_web_search(last_user_msg) else []
    search_context = format_search_context(search_results)
    system_prompt = (
        "You are SUNAI, a brilliant friendly AI assistant. Help with coding, science, career, math, and any topic. Be clear, concise and helpful. "
        "When WEB SEARCH RESULTS are provided, prioritise them for current facts. Cite as [1],[2] etc."
        + (("\n\n" + search_context) if search_context else "")
        + mem_block
    )
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + clean_messages,
            max_tokens=2048,
        )
        reply = resp.choices[0].message.content
        uid = user["id"]
        add_history(uid, "user", clean_messages[-1]["content"])
        add_history(uid, "assistant", reply)
        used = get_today_usage(uid)
        remaining = 999 if user["plan"] == "pro" else max(0, FREE_LIMIT - used)
        search_provider = "serper" if (SERPER_API_KEY and search_results) else "duckduckgo"
        return jsonify({"reply": reply, "remaining": remaining, "plan": user["plan"],
                        "searched": bool(search_results), "provider": search_provider})
    except Exception:
        app.logger.exception("Chat failed")
        return jsonify({"error": "Processing failed."}), 500

# ── Image analysis ──────────────────────────────────────────────────────────────
@app.route("/analyze-image", methods=["POST"])
@login_required
@limiter.limit("30/minute")
def analyze_image():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    allowed, err = _check_quota(user)
    if not allowed:
        return err
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img_file = request.files["image"]
    header = img_file.read(2048); img_file.seek(0)
    if not magic.from_buffer(header, mime=True).startswith("image/"):
        return jsonify({"error": "Not a valid image"}), 400
    question = request.form.get("question", "Describe this image in detail.")[:1000]
    try:
        import PIL.Image, io
        img  = PIL.Image.open(io.BytesIO(img_file.read()))
        resp = vision_model.generate_content([f"You are SUNAI. {question}", img])
        reply = resp.text
        add_history(user["id"], "user", f"[Image] {question}")
        add_history(user["id"], "assistant", reply)
        return jsonify({"reply": reply})
    except Exception:
        app.logger.exception("Image analysis failed")
        return jsonify({"error": "Image processing failed."}), 500

# ── Image generation ────────────────────────────────────────────────────────────
@app.route("/generate-image", methods=["POST"])
@login_required
@limiter.limit("10/minute")
def generate_image():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    allowed, err = _check_quota(user)
    if not allowed:
        return err
    data   = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()[:500]
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400
    try:
        response = imagen_model.generate_content(
            contents=prompt,
            
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data:
                img_b64 = base64.b64encode(part.inline_data.data).decode()
                mime    = part.inline_data.mime_type
                add_history(user["id"], "user", f"[Image Gen] {prompt}")
                add_history(user["id"], "assistant", f"[Generated image for: {prompt}]")
                log_event("image_gen", user["id"], {"prompt": prompt[:100]})
                return jsonify({"image": f"data:{mime};base64,{img_b64}", "prompt": prompt})
        return jsonify({"error": "No image generated"}), 500
    except Exception:
        app.logger.exception("Image generation failed")
        return jsonify({"error": "Image generation failed."}), 500

# ── Voice transcription (Whisper) ───────────────────────────────────────────────
@app.route("/transcribe", methods=["POST"])
@login_required
@limiter.limit("20/minute")
def transcribe():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    if "audio" not in request.files:
        return jsonify({"error": "No audio uploaded"}), 400
    audio = request.files["audio"]
    if audio.content_length and audio.content_length > 25 * 1024 * 1024:
        return jsonify({"error": "Audio too large (max 25MB)"}), 400
    try:
        import io
        audio_bytes = audio.read()
        transcription = groq_client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=(audio.filename or "audio.webm", io.BytesIO(audio_bytes)),
            response_format="text",
        )
        return jsonify({"text": transcription})
    except Exception:
        app.logger.exception("Transcription failed")
        return jsonify({"error": "Transcription failed."}), 500

# ── File analysis ────────────────────────────────────────────────────────────────
@app.route("/analyze-file", methods=["POST"])
@login_required
@limiter.limit("20/minute")
def analyze_file():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not_found"}), 404
    allowed, err = _check_quota(user)
    if not allowed:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    ok, reason = validate_file(file)
    if not ok:
        return jsonify({"error": reason}), 400
    question = request.form.get("question", "Summarize this document.")[:1000]
    try:
        if file.filename.lower().endswith(".pdf"):
            import PyPDF2, io
            reader = PyPDF2.PdfReader(io.BytesIO(file.read()))
            text   = "\n".join(p.extract_text() or "" for p in reader.pages)
        else:
            text = file.read().decode("utf-8", errors="ignore")
        text = text[:8000]
        resp = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "You are SUNAI, a helpful AI assistant."},
                {"role": "user",   "content": f"File:\n\n{text}\n\nQuestion: {question}"},
            ],
            max_tokens=2048,
        )
        reply = resp.choices[0].message.content
        add_history(user["id"], "user", f"[File: {file.filename}] {question}")
        add_history(user["id"], "assistant", reply)
        return jsonify({"reply": reply})
    except Exception:
        app.logger.exception("File analysis failed")
        return jsonify({"error": "File processing failed."}), 500

# ── History ─────────────────────────────────────────────────────────────────────
@app.route("/history")
@login_required
def history():
    rows = get_history(session["user_id"])
    return jsonify({"history": [
        {"role": r["role"], "content": r["content"], "time": r.get("created_at", "")}
        for r in rows
    ]})

@app.route("/history/clear", methods=["POST"])
@login_required
@limiter.limit("10/minute")
def clear_history():
    clear_history_db(session["user_id"])
    return jsonify({"success": True})

# ── Memories ────────────────────────────────────────────────────────────────────
@app.route("/memories")
@login_required
def list_memories():
    return jsonify({"memories": get_memories(session["user_id"])})

@app.route("/memories/clear", methods=["POST"])
@login_required
def clear_memories():
    try:
        supabase.table("memories").delete().eq("user_id", session["user_id"]).execute()
        return jsonify({"success": True})
    except:
        return jsonify({"error": "Failed to clear memories"}), 500

# ── Workspaces ───────────────────────────────────────────────────────────────────
@app.route("/workspaces", methods=["GET"])
@login_required
def list_workspaces():
    uid = session["user_id"]
    try:
        res = (supabase.table("workspace_members").select("workspace_id, role, workspaces(id, name, created_at)")
               .eq("user_id", uid).execute())
        workspaces = []
        for r in (res.data or []):
            ws = r.get("workspaces") or {}
            workspaces.append({
                "id": ws.get("id"), "name": ws.get("name"),
                "role": r.get("role"), "created_at": ws.get("created_at"),
            })
        return jsonify({"workspaces": workspaces})
    except Exception as e:
        app.logger.error(f"list_workspaces: {e}")
        return jsonify({"workspaces": []})

@app.route("/workspaces", methods=["POST"])
@login_required
def create_workspace():
    user = get_current_user()
    if not user or user.get("plan") != "pro":
        return jsonify({"error": "Pro plan required for team workspaces"}), 403
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()[:80]
    if not name:
        return jsonify({"error": "Workspace name required"}), 400
    wid = str(uuid.uuid4())
    supabase.table("workspaces").insert({
        "id": wid, "name": name, "owner_id": user["id"],
        "created_at": datetime.utcnow().isoformat(),
    }).execute()
    supabase.table("workspace_members").insert({
        "workspace_id": wid, "user_id": user["id"], "role": "owner",
    }).execute()
    log_event("workspace_create", user["id"], {"workspace_id": wid})
    return jsonify({"success": True, "workspace_id": wid})

@app.route("/workspaces/<wid>/invite", methods=["POST"])
@login_required
def invite_workspace(wid):
    uid  = session["user_id"]
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    # Verify caller is owner
    res = (supabase.table("workspace_members").select("role")
           .eq("workspace_id", wid).eq("user_id", uid).maybe_single().execute())
    if not res.data or res.data.get("role") != "owner":
        return jsonify({"error": "Only workspace owner can invite"}), 403
    invitee = get_user_by_email(email)
    if not invitee:
        return jsonify({"error": "No SUNAI account found for that email"}), 404
    # Check not already a member
    existing = (supabase.table("workspace_members").select("id")
                .eq("workspace_id", wid).eq("user_id", invitee["id"]).maybe_single().execute())
    if existing.data:
        return jsonify({"error": "User is already a member"}), 400
    supabase.table("workspace_members").insert({
        "workspace_id": wid, "user_id": invitee["id"], "role": "member",
    }).execute()
    return jsonify({"success": True})

# ── Admin Dashboard ────────────────────────────────────────────────────────────
@app.route("/admin/stats")
@admin_required
def admin_stats():
    try:
        total_users = supabase.table("users").select("id", count="exact").execute()
        pro_users   = supabase.table("users").select("id", count="exact").eq("plan", "pro").execute()
        today       = str(date.today())
        dau         = (supabase.table("usage_counts").select("user_id", count="exact")
                       .eq("day", today).execute())
        total_chats = supabase.table("chat_history").select("id", count="exact").execute()
        recent_events = (supabase.table("events").select("event_type, created_at")
                         .order("created_at", desc=True).limit(50).execute())
        # Revenue estimate: pro users × ₹199
        revenue = (getattr(pro_users, "count", 0) or 0) * 199

        # Usage over last 7 days
        week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        week_usage = (supabase.table("usage_counts").select("day, count")
                      .gte("day", week_ago).execute())

        return jsonify({
            "total_users":   getattr(total_users, "count", 0) or 0,
            "pro_users":     getattr(pro_users,   "count", 0) or 0,
            "free_users":    (getattr(total_users, "count", 0) or 0) - (getattr(pro_users, "count", 0) or 0),
            "dau":           getattr(dau, "count", 0) or 0,
            "total_chats":   getattr(total_chats, "count", 0) or 0,
            "estimated_revenue_inr": revenue,
            "recent_events": recent_events.data or [],
            "week_usage":    week_usage.data or [],
        })
    except Exception as e:
        app.logger.exception("admin_stats failed")
        return jsonify({"error": str(e)}), 500

@app.route("/admin/users")
@admin_required
def admin_users():
    try:
        res = (supabase.table("users")
               .select("id, name, email, plan, joined, email_verified, role")
               .order("joined", desc=True).limit(200).execute())
        return jsonify({"users": res.data or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/users/<uid>/plan", methods=["POST"])
@admin_required
def admin_set_plan(uid):
    data = request.get_json(silent=True) or {}
    plan = data.get("plan", "free")
    if plan not in ("free", "pro"):
        return jsonify({"error": "Invalid plan"}), 400
    supabase.table("users").update({"plan": plan}).eq("id", uid).execute()
    log_event("admin_plan_change", session["user_id"], {"target_uid": uid, "plan": plan})
    return jsonify({"success": True})

# ── SEO ────────────────────────────────────────────────────────────────────────
@app.route("/sitemap.xml")
def sitemap():
    base = os.environ.get("SITE_URL", "https://sunai.onrender.com")
    xml  = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
</urlset>"""
    return app.response_class(xml, mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    base = os.environ.get("SITE_URL", "https://sunai.onrender.com")
    return app.response_class(
        f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml", mimetype="text/plain"
    )

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "5.0"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=os.environ.get("FLASK_ENV") == "development",
            host="0.0.0.0", port=port)
