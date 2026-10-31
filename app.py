"""RecapStudio (working name) - original-design movie recap website.
Functions: register/login, credits, projects, upload or link a video, AI/local recap
pipeline (transcribe, recap script, translate, voiceover, subtitles, 9:16 render),
title/description/hashtags, downloads, admin settings + credit management.
"""
import functools, json, os, sqlite3, threading, time
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data"); os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "app.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
SIGNUP_BONUS = 100
COST_PER_MIN = 10

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d: d.close()

def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE,
      password TEXT, credits INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, created_at REAL);
    CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT,
      source_type TEXT, source_value TEXT, voice TEXT, target_lang TEXT, max_minutes INTEGER,
      status TEXT DEFAULT 'queued', step TEXT DEFAULT '', error TEXT, workdir TEXT,
      meta TEXT, cost INTEGER DEFAULT 0, created_at REAL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    c.commit(); c.close()

def get_settings():
    rows = db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

def current_user():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

def login_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not current_user(): return redirect(url_for("login"))
        return f(*a, **k)
    return w

def admin_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        u = current_user()
        if not u or not u["is_admin"]: abort(403)
        return f(*a, **k)
    return w

@app.route("/")
def index():
    return render_template("index.html", user=current_user())

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email, pw = request.form["name"].strip(), request.form["email"].strip().lower(), request.form["password"]
        if len(pw) < 6: flash("Password must be at least 6 characters."); return render_template("register.html")
        n = db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        try:
            cur = db().execute("INSERT INTO users(name,email,password,credits,is_admin,created_at) VALUES(?,?,?,?,?,?)",
                               (name, email, generate_password_hash(pw), SIGNUP_BONUS, 1 if n == 0 else 0, time.time()))
            db().commit()
        except sqlite3.IntegrityError:
            flash("That email is already registered."); return render_template("register.html")
        session["uid"] = cur.lastrowid
        flash(f"Welcome! You received {SIGNUP_BONUS} free credits.")
        return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),)).fetchone()
        if u and check_password_hash(u["password"], request.form["password"]):
            session["uid"] = u["id"]; return redirect(url_for("dashboard"))
        flash("Wrong email or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    projects = db().execute("SELECT * FROM projects WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    return render_template("dashboard.html", user=u, projects=projects)

@app.route("/projects/new", methods=["GET", "POST"])
@login_required
def new_project():
    u = current_user()
    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Untitled recap"
        voice = request.form.get("voice", "en-male"); lang = request.form.get("target_lang", "en")
        try: max_minutes = max(1, min(30, int(request.form.get("max_minutes", 10))))
        except ValueError: max_minutes = 10
        cost = max_minutes * COST_PER_MIN
        if u["credits"] < cost:
            flash(f"Not enough credits. This job needs {cost}, you have {u['credits']}.")
            return redirect(url_for("new_project"))
        link = request.form.get("video_url", "").strip()
        file = request.files.get("video_file")
        pid = db().execute("INSERT INTO projects(user_id,title,source_type,source_value,voice,target_lang,max_minutes,status,cost,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (u["id"], title, "url" if link else "file", link, voice, lang, max_minutes, "queued", cost, time.time())).lastrowid
        wd = os.path.join(DATA, f"project_{pid}"); os.makedirs(wd, exist_ok=True)
        if link:
            src_value = link
        elif file and file.filename:
            fname = secure_filename(file.filename) or "upload.mp4"
            path = os.path.join(wd, fname); file.save(path); src_value = path
        else:
            db().execute("DELETE FROM projects WHERE id=?", (pid,)); db().commit()
            flash("Please paste a video link or upload a video file."); return redirect(url_for("new_project"))
        db().execute("UPDATE projects SET source_value=?, workdir=? WHERE id=?", (src_value, wd, pid))
        db().execute("UPDATE users SET credits=credits-? WHERE id=?", (cost, u["id"])); db().commit()
        threading.Thread(target=run_job, args=(pid,), daemon=True).start()
        flash("Recap job started. It can take a few minutes - refresh the project page to watch progress.")
        return redirect(url_for("project_detail", pid=pid))
    return render_template("new_project.html", user=u, cost_per_min=COST_PER_MIN)

def run_job(pid):
    import sqlite3 as s3
    c = s3.connect(DB); c.row_factory = s3.Row
    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    settings = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings").fetchall()}
    def step(msg):
        c.execute("UPDATE projects SET status='processing', step=? WHERE id=?", (msg, pid)); c.commit()
    try:
        step("Starting...")
        result = pipeline.run_pipeline(dict(p), settings, step)
        c.execute("UPDATE projects SET status='done', step='Finished', meta=? WHERE id=?",
                  (json.dumps(result["meta"], ensure_ascii=False), pid))
    except Exception as e:
        c.execute("UPDATE projects SET status='failed', error=? WHERE id=?", (str(e)[-800:], pid))
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (p["cost"], p["user_id"]))  # refund on failure
    c.commit(); c.close()

@app.route("/projects/<int:pid>")
@login_required
def project_detail(pid):
    u = current_user()
    p = db().execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not p: abort(404)
    files = {}
    if p["workdir"] and os.path.isdir(p["workdir"]):
        for name, label in [("recap.mp4", "Recap video (9:16, subtitled)"), ("narration.mp3", "Voiceover audio"),
                            ("script.txt", "Recap script"), ("transcript.txt", "Original transcript"),
                            ("transcript.srt", "Transcript subtitles"), ("subtitles.srt", "Narration subtitles")]:
            if os.path.exists(os.path.join(p["workdir"], name)): files[name] = label
    meta = json.loads(p["meta"]) if p["meta"] else None
    return render_template("project.html", user=u, p=p, files=files, meta=meta)

@app.route("/projects/<int:pid>/files/<name>")
@login_required
def project_file(pid, name):
    u = current_user()
    p = db().execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, u["id"])).fetchone()
    if not p or name not in {"recap.mp4", "narration.mp3", "script.txt", "transcript.txt", "transcript.srt", "subtitles.srt"}:
        abort(404)
    return send_from_directory(p["workdir"], name, as_attachment=True)

@app.route("/admin")
@admin_required
def admin():
    users = db().execute("SELECT * FROM users ORDER BY id").fetchall()
    projects = db().execute("SELECT * FROM projects ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("admin.html", user=current_user(), users=users, projects=projects, settings=get_settings())

@app.route("/admin/credits", methods=["POST"])
@admin_required
def admin_credits():
    db().execute("UPDATE users SET credits=credits+? WHERE id=?",
                 (int(request.form["amount"]), int(request.form["user_id"]))); db().commit()
    flash("Credits updated."); return redirect(url_for("admin"))

@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    for k in ["whisper_model", "ai_api_base", "ai_api_key", "ai_model"]:
        db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (k, request.form.get(k, "").strip()))
    db().commit(); flash("Settings saved."); return redirect(url_for("admin"))

init()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5100)))
