import os
import shutil
import subprocess
import sys
import signal
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, flash, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-change-me-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///hostx_bot1.db'
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'hosted_files')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

OWNER_EMAIL = "deepchomal8@gmail.com"
active_processes = {} # Run hone wale bots ko track karne ke liye

# ==========================================
# MODELS
# ==========================================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_owner = db.Column(db.Boolean, default=False)
    trial_expiry = db.Column(db.DateTime, nullable=True)
    file_limit_mb = db.Column(db.Integer, default=50)
    is_suspended = db.Column(db.Boolean, default=False)
    projects = db.relationship('Project', backref='owner', lazy=True, cascade="all, delete-orphan")

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='STOPPED')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class SiteConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_username = db.Column(db.String(100), default='YourTelegramUsername')
    broadcast_message = db.Column(db.String(500), default='')
    site_name      = db.Column(db.String(100), default='HostX')
    site_tagline   = db.Column(db.String(100), default='Bot Hosting')
    login_title    = db.Column(db.String(100), default='Welcome Back')
    login_subtitle = db.Column(db.String(200), default='Sign in to continue')
    button_text    = db.Column(db.String(100), default='DM to Buy Access')
    button_link    = db.Column(db.String(300), default='')

class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    days = db.Column(db.Integer, nullable=False)
    is_used = db.Column(db.Boolean, default=False)

# ==========================================
# SEED + AUTO-MIGRATION
# ==========================================
def ensure_site_config_columns():
    from sqlalchemy import text, inspect
    inspector = inspect(db.engine)
    if 'site_config' not in inspector.get_table_names():
        return
    existing = {c['name'] for c in inspector.get_columns('site_config')}
    new_cols = {
        'site_name':      "VARCHAR(100) DEFAULT 'HostX'",
        'site_tagline':   "VARCHAR(100) DEFAULT 'Bot Hosting'",
        'login_title':    "VARCHAR(100) DEFAULT 'Welcome Back'",
        'login_subtitle': "VARCHAR(200) DEFAULT 'Sign in to continue'",
        'button_text':    "VARCHAR(100) DEFAULT 'DM to Buy Access'",
        'button_link':    "VARCHAR(300) DEFAULT ''",
    }
    with db.engine.connect() as conn:
        for col, ddl in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE site_config ADD COLUMN {col} {ddl}"))
                conn.commit()

with app.app_context():
    db.create_all()
    ensure_site_config_columns()

    old_admin = User.query.filter_by(email="admin@hostx.com").first()
    new_admin = User.query.filter_by(email=OWNER_EMAIL).first()
    if old_admin and not new_admin:
        old_admin.email = OWNER_EMAIL
        old_admin.is_owner = True
        db.session.commit()
    elif not new_admin:
        admin = User(email=OWNER_EMAIL, password_hash=generate_password_hash("admin123"), is_owner=True)
        db.session.add(admin)
        db.session.commit()

    all_owners = User.query.filter_by(is_owner=True).all()
    if len(all_owners) > 1:
        keeper = next((u for u in all_owners if u.email == OWNER_EMAIL), all_owners[0])
        for u in all_owners:
            if u.id != keeper.id: u.is_owner = False
        keeper.is_owner = True
        db.session.commit()

    if not SiteConfig.query.first():
        db.session.add(SiteConfig())
        db.session.commit()

@app.context_processor
def inject_config():
    cfg = SiteConfig.query.first()
    buy_url = cfg.button_link.strip() if cfg.button_link and cfg.button_link.strip() else f"https://t.me/{cfg.telegram_username}"
    return {'site_config': cfg, 'buy_url': buy_url}

# ==========================================
# DECORATORS & HELPERS
# ==========================================
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def owner_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_owner:
            flash('Owner access required!', 'error')
            return redirect('/dashboard')
        return f(*args, **kwargs)
    return wrapper

def active_trial_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if (not current_user.is_owner and current_user.trial_expiry and datetime.utcnow() > current_user.trial_expiry):
            flash('Trial expired. Contact admin to renew.', 'error')
            return redirect('/dashboard')
        return f(*args, **kwargs)
    return wrapper

def log_activity(msg):
    db.session.add(ActivityLog(message=msg))
    db.session.commit()

def dir_size(path):
    if not os.path.exists(path): return 0
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)

# ==========================================
# UI TEMPLATES
# ==========================================
BASE_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>{{ site_config.site_name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; background: #F8FAFC; -webkit-tap-highlight-color: transparent; }
        .glass { background: rgba(255,255,255,0.75); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
        .gradient-purple { background: linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%); }
        .gradient-dark   { background: linear-gradient(135deg, #1E293B 0%, #0F172A 100%); }
        .gradient-red    { background: linear-gradient(135deg, #EF4444 0%, #DC2626 100%); }
        .card-hover { transition: all 0.2s ease; }
        .card-hover:active { transform: scale(0.985); }
        .btn-press:active { transform: scale(0.96); }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        .fade-in { animation: fadeIn 0.3s ease; }
    </style>
</head>
<body class="text-slate-800 pb-24">
    <header class="sticky top-0 z-50 glass border-b border-slate-200/60">
        <div class="flex justify-between items-center px-5 py-3 max-w-md mx-auto">
            <div class="flex items-center gap-2.5">
                <div class="gradient-purple w-9 h-9 rounded-xl flex items-center justify-center text-white text-lg shadow-lg shadow-purple-500/30">⚡</div>
                <div>
                    <p class="text-base font-extrabold tracking-tight leading-none">{{ site_config.site_name }}</p>
                    <p class="text-[10px] text-slate-400 font-bold uppercase tracking-wider">{{ site_config.site_tagline }}</p>
                </div>
            </div>
            <div class="flex items-center gap-2">
                {% if current_user.is_owner %}
                <a href="/owner" class="gradient-red text-white text-[10px] font-bold px-3 py-1.5 rounded-lg shadow-md shadow-red-500/20 btn-press">👑 ADMIN</a>
                {% endif %}
                <a href="/logout" class="w-9 h-9 gradient-purple text-white rounded-full flex items-center justify-center font-extrabold text-sm shadow-md shadow-purple-500/30 btn-press">
                    {{ current_user.email[0]|upper }}
                </a>
            </div>
        </div>
    </header>

    <main class="px-4 pt-4 max-w-md mx-auto">
        {% if session.get('impersonating') %}
        <div class="gradient-dark text-white p-3 rounded-xl mb-4 text-xs font-bold flex items-center justify-between shadow-lg fade-in">
            <span class="flex items-center gap-2">👑 <span class="opacity-90">{{ current_user.email }}</span></span>
            <a href="/return-to-admin" class="bg-white/20 hover:bg-white/30 px-3 py-1 rounded-lg transition">Return</a>
        </div>
        {% endif %}

        {% with msgs = get_flashed_messages(with_categories=true) %}
            {% for cat, msg in msgs %}
            <div class="mb-4 px-4 py-3 rounded-xl text-sm font-semibold flex justify-between items-center shadow-sm fade-in
                {% if cat=='error' %}bg-red-50 text-red-700 border border-red-200
                {% else %}bg-emerald-50 text-emerald-700 border border-emerald-200{% endif %}">
                <span class="flex items-center gap-2">
                    {% if cat=='error' %}⚠️{% else %}✅{% endif %} {{ msg }}
                </span>
                <button onclick="this.parentElement.remove()" class="text-lg font-bold opacity-50 hover:opacity-100">&times;</button>
            </div>
            {% endfor %}
        {% endwith %}
        {{ content|safe }}
    </main>

    <nav class="fixed bottom-0 left-1/2 -translate-x-1/2 w-full max-w-md glass border-t border-slate-200/60 z-50">
        <div class="flex justify-around py-2.5">
            <a href="/dashboard" class="flex flex-col items-center gap-1 px-6 transition {% if request.path == '/dashboard' %}text-[#7C3AED]{% else %}text-slate-400{% endif %}">
                <span class="text-xl">⚡</span>
                <span class="text-[10px] font-bold">Home</span>
            </a>
            {% if current_user.is_owner %}
            <a href="/owner" class="flex flex-col items-center gap-1 px-6 text-red-500">
                <span class="text-xl">👑</span>
                <span class="text-[10px] font-bold">Admin</span>
            </a>
            {% else %}
            <a href="/create-project" class="flex flex-col items-center gap-1 px-6 text-slate-400">
                <span class="text-xl">＋</span>
                <span class="text-[10px] font-bold">New</span>
            </a>
            {% endif %}
        </div>
    </nav>
</body>
</html>
"""

OWNER_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin - {{ site_config.site_name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>body { font-family: 'Inter', sans-serif; background-color: #0F172A; color: #F8FAFC; }</style>
</head>
<body class="min-h-screen pb-12">
    <header class="bg-slate-900 border-b border-slate-800 px-6 py-4 flex justify-between items-center sticky top-0 z-50">
        <div class="flex items-center gap-3">
            <div class="bg-red-500 w-10 h-10 rounded-xl flex items-center justify-center text-xl shadow-lg shadow-red-500/30">👑</div>
            <div>
                <h1 class="text-xl font-extrabold tracking-tight text-white">{{ site_config.site_name }} Admin</h1>
                <p class="text-xs text-slate-400 font-medium">Superuser Control Panel</p>
            </div>
        </div>
        <div class="flex items-center gap-4">
            <a href="/dashboard" class="text-sm font-bold text-slate-300 hover:text-white transition">View User App</a>
            <a href="/logout" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded-lg text-sm font-bold shadow-md transition">Logout</a>
        </div>
    </header>
    <main class="max-w-6xl mx-auto px-4 mt-8">
        {% with msgs = get_flashed_messages(with_categories=true) %}
            {% for cat, msg in msgs %}
            <div class="mb-6 px-4 py-3 rounded-xl text-sm font-medium {% if cat=='error' %}bg-red-900/50 text-red-200 border border-red-800{% else %}bg-emerald-900/50 text-emerald-200 border border-emerald-800{% endif %} flex justify-between items-center">
                {{ msg }}
                <button onclick="this.parentElement.style.display='none'" class="text-lg font-bold">&times;</button>
            </div>
            {% endfor %}
        {% endwith %}
        {{ content|safe }}
    </main>
</body>
</html>
"""

# ==========================================
# AUTH ROUTES
# ==========================================
@app.route('/')
def index():
    return redirect('/dashboard' if current_user.is_authenticated else '/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email'].strip()).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            if user.is_suspended:
                flash('Your account is suspended. Contact Owner.', 'error')
                return redirect('/login')
            login_user(user)
            log_activity(f"{user.email} logged in.")
            return redirect('/owner' if user.is_owner else '/dashboard')
        flash('Invalid Email or Password', 'error')

    login_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ site_config.site_name }} - Login</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
        <style>
            body { font-family: 'Inter', sans-serif; }
            .gradient-bg { background: linear-gradient(135deg, #F8FAFC 0%, #EDE9FE 100%); }
            .gradient-purple { background: linear-gradient(135deg, #7C3AED 0%, #6D28D9 100%); }
            @keyframes float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-6px); } }
            .float { animation: float 3s ease-in-out infinite; }
        </style>
    </head>
    <body class="gradient-bg min-h-screen flex items-center justify-center px-4">
        <div class="w-full max-w-sm">
            <div class="text-center mb-8 float">
                <div class="gradient-purple w-20 h-20 rounded-3xl flex items-center justify-center text-4xl mx-auto shadow-xl shadow-purple-500/40">⚡</div>
            </div>
            <div class="bg-white rounded-3xl shadow-xl shadow-slate-200/50 p-7 border border-slate-100">
                <h1 class="text-2xl font-extrabold text-center text-slate-800 mb-1">{{ site_config.login_title }}</h1>
                <p class="text-center text-sm text-slate-500 mb-7">{{ site_config.login_subtitle }}</p>

                {% with msgs = get_flashed_messages(with_categories=true) %}
                    {% for cat, msg in msgs %}
                    <div class="mb-4 px-3 py-2 rounded-lg text-xs font-semibold {% if cat=='error' %}bg-red-50 text-red-700 border border-red-200{% else %}bg-emerald-50 text-emerald-700 border border-emerald-200{% endif %}">
                        {{ msg }}
                    </div>
                    {% endfor %}
                {% endwith %}

                <form method="POST" class="space-y-3.5">
                    <div>
                        <label class="text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5 block">Email</label>
                        <input name="email" type="email" placeholder="you@gmail.com" required
                            class="w-full px-4 py-3 rounded-xl bg-slate-50 border border-slate-200 focus:ring-2 focus:ring-[#7C3AED] focus:border-transparent outline-none text-sm font-medium transition">
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5 block">Password</label>
                        <input name="password" type="password" placeholder="••••••••" required
                            class="w-full px-4 py-3 rounded-xl bg-slate-50 border border-slate-200 focus:ring-2 focus:ring-[#7C3AED] focus:border-transparent outline-none text-sm font-medium transition">
                    </div>
                    <button type="submit" class="w-full gradient-purple text-white font-bold py-3.5 rounded-xl shadow-lg shadow-purple-500/30 transition active:scale-[0.98] mt-2">
                        Sign In →
                    </button>
                </form>

                <div class="mt-7 pt-6 border-t border-slate-100">
                    <p class="text-center text-xs text-slate-500 mb-3 font-medium">Don't have an account?</p>
                    <a href="{{ buy_url }}" target="_blank" rel="noopener"
                        class="flex items-center justify-center gap-2 w-full bg-[#229ED9] hover:bg-[#1E8BC3] text-white font-bold py-3 rounded-xl transition shadow-lg shadow-blue-500/20 active:scale-[0.98]">
                        <span class="text-lg">✈️</span> {{ site_config.button_text }}
                    </a>
                </div>
            </div>
            <p class="text-center text-[10px] text-slate-400 font-medium mt-6">© 2025 {{ site_config.site_name }} · Premium Bot Hosting</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(login_template)

@app.route('/logout')
@login_required
def logout():
    if session.get('impersonating'):
        admin = User.query.filter_by(is_owner=True).first()
        if admin: login_user(admin)
        session.pop('impersonating', None)
        flash('Returned to admin panel.', 'success')
        return redirect('/owner')
    log_activity(f"{current_user.email} logged out.")
    logout_user()
    return redirect('/login')

# ==========================================
# USER DASHBOARD
# ==========================================
@app.route('/dashboard')
@login_required
def dashboard():
    config = SiteConfig.query.first()
    broadcast_html = ""
    if config.broadcast_message and not current_user.is_owner:
        broadcast_html = f"""
        <div class="gradient-dark text-white p-4 mb-4 rounded-2xl shadow-lg fade-in relative overflow-hidden">
            <div class="absolute -right-6 -top-6 w-24 h-24 bg-white/10 rounded-full"></div>
            <div class="flex items-start gap-3 relative z-10">
                <div class="text-2xl">📢</div>
                <div>
                    <p class="font-extrabold text-xs uppercase tracking-wider text-emerald-300 mb-1">Announcement</p>
                    <p class="text-sm leading-relaxed opacity-95">{config.broadcast_message}</p>
                </div>
            </div>
        </div>
        """

    if not current_user.is_owner and current_user.trial_expiry and datetime.utcnow() > current_user.trial_expiry:
        content = broadcast_html + f"""
        <div class="text-center mt-8 fade-in">
            <div class="bg-white rounded-3xl p-8 shadow-xl border border-red-100">
                <div class="w-20 h-20 gradient-red rounded-3xl flex items-center justify-center text-4xl mx-auto mb-5 shadow-lg shadow-red-500/30">⏰</div>
                <h1 class="text-2xl font-extrabold text-slate-800 mb-2">Trial Expired</h1>
                <p class="text-sm text-slate-500 mb-6 leading-relaxed">Your hosting time is over.<br>Contact admin to renew your access.</p>
                <a href="{buy_url if False else ('https://t.me/' + config.telegram_username if not config.button_link else config.button_link)}" target="_blank"
                    class="inline-flex items-center gap-2 gradient-purple text-white font-bold px-6 py-3 rounded-xl shadow-lg shadow-purple-500/30 active:scale-95 transition">
                    <span>✈️</span> Contact Admin
                </a>
            </div>
        </div>
        """
        return render_template_string(BASE_UI, content=content, current_user=current_user)

    projects = Project.query.filter_by(user_id=current_user.id).all()
    time_left_str = "∞ Unlimited" if not current_user.trial_expiry else f"{(current_user.trial_expiry - datetime.utcnow()).days} days"
    limit_str = "∞ Unlimited" if current_user.is_owner else f"{current_user.file_limit_mb} MB"
    running_count = sum(1 for p in projects if p.status == 'RUNNING')

    project_cards = "".join([f"""
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 mb-3 card-hover fade-in">
            <div class="flex justify-between items-start mb-3">
                <div class="flex items-center gap-3">
                    <div class="w-11 h-11 gradient-purple rounded-xl flex items-center justify-center text-white font-extrabold text-lg shadow-md shadow-purple-500/20">
                        {p.name[0]}
                    </div>
                    <div>
                        <h3 class="font-extrabold text-slate-800 text-base leading-tight">{p.name}</h3>
                        <p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mt-0.5">PROJECT #{p.id}</p>
                    </div>
                </div>
                <span class="text-[10px] font-extrabold px-2.5 py-1 rounded-full
                    {'bg-emerald-50 text-emerald-600 border border-emerald-200' if p.status == 'RUNNING'
                     else 'bg-slate-100 text-slate-500 border border-slate-200'}">
                    ● {p.status}
                </span>
            </div>
            <div class="flex gap-2">
                <a href="/project/{p.id}" class="flex-1 bg-slate-50 hover:bg-slate-100 text-slate-700 font-bold py-2.5 rounded-xl text-xs text-center transition active:scale-[0.98]">Open Project</a>
                <form method="POST" action="/delete-project/{p.id}" onsubmit="return confirm('Delete {p.name}?')">
                    <button type="submit" class="bg-red-50 hover:bg-red-100 text-red-500 w-10 h-10 rounded-xl text-base transition active:scale-95 flex items-center justify-center">🗑️</button>
                </form>
            </div>
        </div>""" for p in projects])

    empty_state = """
    <div class="bg-white rounded-2xl p-10 text-center shadow-sm border border-slate-100 fade-in">
        <div class="text-5xl mb-3 opacity-30">📁</div>
        <p class="font-extrabold text-slate-700 mb-1">No projects yet</p>
        <p class="text-xs text-slate-400 mb-5">Create your first project to get started</p>
        <a href="/create-project" class="inline-flex items-center gap-1.5 gradient-purple text-white font-bold px-5 py-2.5 rounded-xl text-xs shadow-lg shadow-purple-500/30">+ Create Project</a>
    </div>
    """

    content = broadcast_html + f"""
    <div class="grid grid-cols-2 gap-3 mb-5 fade-in">
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100">
            <div class="flex items-center gap-2 mb-2"><div class="w-7 h-7 bg-emerald-50 rounded-lg flex items-center justify-center text-sm">⏳</div><p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Time Left</p></div>
            <p class="text-xl font-extrabold text-slate-800">{time_left_str}</p>
        </div>
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100">
            <div class="flex items-center gap-2 mb-2"><div class="w-7 h-7 bg-purple-50 rounded-lg flex items-center justify-center text-sm">💾</div><p class="text-[10px] font-bold text-slate-400 uppercase tracking-wider">File Limit</p></div>
            <p class="text-xl font-extrabold text-slate-800">{limit_str}</p>
        </div>
    </div>

    <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 mb-5 fade-in">
        <div class="flex items-center gap-2 mb-3">
            <div class="w-8 h-8 bg-amber-50 rounded-lg flex items-center justify-center text-base">🎟️</div>
            <div><p class="text-sm font-extrabold text-slate-800">Redeem Coupon</p><p class="text-[10px] text-slate-400 font-semibold">Add extra days to your trial</p></div>
        </div>
        <form method="POST" action="/redeem-coupon" class="flex gap-2">
            <input name="coupon" placeholder="ENTER CODE" maxlength="20" class="flex-1 px-3 py-2.5 rounded-xl text-sm font-bold bg-slate-50 border border-slate-200 focus:ring-2 focus:ring-[#7C3AED] outline-none uppercase tracking-wider transition">
            <button type="submit" class="gradient-purple text-white font-bold px-5 rounded-xl text-sm shadow-md shadow-purple-500/20 active:scale-95 transition">Apply</button>
        </form>
    </div>

    <div class="flex justify-between items-center mb-3 mt-6 fade-in">
        <div><h2 class="text-lg font-extrabold text-slate-800">My Projects</h2><p class="text-[11px] text-slate-400 font-semibold mt-0.5">{len(projects)} total · {running_count} running</p></div>
        <a href="/create-project" class="gradient-purple text-white font-bold px-4 py-2.5 rounded-xl text-xs shadow-lg shadow-purple-500/30 active:scale-95 transition flex items-center gap-1.5"><span class="text-base leading-none">+</span> New</a>
    </div>
    {project_cards if projects else empty_state}
    """
    return render_template_string(BASE_UI, content=content, current_user=current_user)

@app.route('/redeem-coupon', methods=['POST'])
@login_required
@active_trial_required
def redeem_coupon():
    code = request.form.get('coupon', '').strip().upper()
    coupon = Coupon.query.filter_by(code=code, is_used=False).first()
    if coupon:
        coupon.is_used = True
        if not current_user.trial_expiry or current_user.trial_expiry < datetime.utcnow():
            current_user.trial_expiry = datetime.utcnow() + timedelta(days=coupon.days)
        else:
            current_user.trial_expiry += timedelta(days=coupon.days)
        db.session.commit()
        log_activity(f"{current_user.email} redeemed coupon {code} (+{coupon.days} days).")
        flash(f'Success! {coupon.days} Days added to your account.', 'success')
    else:
        flash('Invalid or already used coupon!', 'error')
    return redirect('/dashboard')

@app.route('/create-project', methods=['GET', 'POST'])
@login_required
@active_trial_required
def create_project():
    if request.method == 'POST':
        name = request.form['name'].strip().upper()
        if name:
            p = Project(name=name, user_id=current_user.id)
            db.session.add(p)
            db.session.commit()
            log_activity(f"{current_user.email} created project {name}.")
            return redirect(f'/project/{p.id}')
        flash('Project name required!', 'error')
    content = """
    <a href='/dashboard' class='text-sm font-bold text-slate-500 mb-4 inline-block'>← Back</a>
    <div class='bg-white p-6 rounded-2xl border border-slate-100 shadow-sm fade-in'>
        <div class='w-12 h-12 gradient-purple rounded-xl flex items-center justify-center text-white text-xl mb-4 shadow-md shadow-purple-500/20'>📁</div>
        <h1 class='text-xl font-extrabold mb-1'>Create Project</h1>
        <p class='text-xs text-slate-400 font-semibold mb-5'>Give your bot a name</p>
        <form method='POST'>
            <input name='name' placeholder='PROJECT NAME' required maxlength='30' class='w-full border border-slate-200 p-3.5 rounded-xl mb-4 uppercase font-bold text-sm focus:ring-2 focus:ring-[#7C3AED] outline-none'>
            <button class='gradient-purple text-white w-full py-3.5 rounded-xl font-bold shadow-lg shadow-purple-500/30 active:scale-[0.98] transition'>Create Project →</button>
        </form>
    </div>
    """
    return render_template_string(BASE_UI, content=content, current_user=current_user)

@app.route('/delete-project/<int:pid>', methods=['POST'])
@login_required
def delete_project(pid):
    p = Project.query.get_or_404(pid)
    if p.user_id == current_user.id or current_user.is_owner:
        # Kill process if running
        process = active_processes.get(p.id)
        if process and process.poll() is None:
            try: os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except: pass
        if p.id in active_processes: del active_processes[p.id]
            
        shutil.rmtree(os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id)), ignore_errors=True)
        db.session.delete(p)
        db.session.commit()
        log_activity(f"Project {p.name} deleted.")
        flash('Project deleted!', 'success')
    return redirect('/dashboard')

@app.route('/project/<int:pid>', methods=['GET', 'POST'])
@login_required
@active_trial_required
def project_detail(pid):
    p = Project.query.get_or_404(pid)
    if p.user_id != current_user.id and not current_user.is_owner:
        flash('Access denied!', 'error')
        return redirect('/dashboard')

    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id))
    os.makedirs(user_dir, exist_ok=True)

    if request.method == 'POST':
        files = request.files.getlist('files')
        limit_bytes = (current_user.file_limit_mb or 50) * 1024 * 1024
        current_size = dir_size(user_dir)
        uploaded_any = False
        
        for f in files:
            if f.filename:
                safe_name = secure_filename(f.filename)
                if not safe_name: 
                    safe_name = f"file_{int(datetime.utcnow().timestamp())}.py"
                
                save_path = os.path.join(user_dir, safe_name)
                f.save(save_path)
                
                fsize = os.path.getsize(save_path)
                if not current_user.is_owner and (current_size + fsize) > limit_bytes:
                    os.remove(save_path) 
                    flash(f'File limit exceeded ({current_user.file_limit_mb} MB). Upload blocked.', 'error')
                    continue
                
                current_size += fsize
                uploaded_any = True
                
        if uploaded_any:
            flash('Files Uploaded successfully!', 'success')
        return redirect(f'/project/{pid}')

    files_list = os.listdir(user_dir)
    files_html = "".join([f'<div class="py-2 px-3 text-xs font-semibold text-slate-700 border-b border-slate-100 flex justify-between"><span>📄 {f}</span></div>' for f in files_list]) or '<p class="text-xs text-slate-400 text-center py-6">No files uploaded yet</p>'
    
    process = active_processes.get(p.id)
    is_running = process is not None and process.poll() is None

    if is_running:
        p.status = 'RUNNING'
    else:
        p.status = 'STOPPED'
        if p.id in active_processes: del active_processes[p.id]
    db.session.commit()

    if is_running:
        host_ui = f'''
        <div class="flex gap-2 mt-3">
            <form method="POST" action="/stop-project/{p.id}" class="flex-1">
                <button class="w-full bg-red-500 text-white font-bold py-3.5 rounded-xl shadow-lg active:scale-[0.98] transition">🛑 Stop Bot</button>
            </form>
            <form method="POST" action="/restart-project/{p.id}" class="flex-1">
                <button class="w-full bg-yellow-500 text-white font-bold py-3.5 rounded-xl shadow-lg active:scale-[0.98] transition">🔄 Restart</button>
            </form>
        </div>
        '''
    else:
        host_ui = f'''
        <form method="POST" action="/host-project/{p.id}">
            <button class="w-full gradient-purple text-white font-bold py-3.5 rounded-xl mt-3 shadow-lg shadow-purple-500/30 active:scale-[0.98] transition">🚀 Start Bot</button>
        </form>
        '''

    used_mb = round(dir_size(user_dir) / (1024 * 1024), 2)
    content = f"""
    <a href='/dashboard' class='text-sm font-bold text-slate-500 mb-4 inline-block'>← Back</a>
    <div class='bg-white p-5 rounded-2xl border border-slate-100 shadow-sm fade-in'>
        <div class='flex items-center gap-3 mb-4'>
            <div class='w-12 h-12 gradient-purple rounded-xl flex items-center justify-center text-white font-extrabold text-lg shadow-md'>{p.name[0]}</div>
            <div class='flex-1'>
                <h1 class='font-extrabold text-lg text-slate-800 leading-tight'>{p.name}</h1>
                <p class='text-[10px] font-bold text-slate-400 uppercase tracking-wider'>PROJECT #{p.id} · {used_mb} MB</p>
            </div>
        </div>

        <div class='bg-slate-50 rounded-xl p-1 max-h-48 overflow-y-auto mb-4'>
            {files_html}
        </div>

        <!-- FIXED FILE UPLOAD BOX -->
        <form method='POST' enctype='multipart/form-data' class='w-full'>
            <label class='block bg-purple-50 border-2 border-dashed border-purple-200 hover:bg-purple-100 rounded-xl p-4 text-center cursor-pointer transition'>
                <p class='text-2xl mb-1'>📤</p>
                <p class='text-xs font-bold text-purple-700'>Select files from phone/PC</p>
                <input type='file' name='files' multiple class='hidden'>
            </label>
            <button type="submit" class="w-full mt-2 bg-blue-500 hover:bg-blue-600 text-white font-bold py-2.5 rounded-xl shadow-md transition active:scale-[0.98]">
                📁 Upload Selected Files
            </button>
        </form>

        {host_ui}
    </div>
    """
    return render_template_string(BASE_UI, content=content, current_user=current_user)

# ==========================================
# ACTUAL BOT EXECUTION ROUTES
# ==========================================
@app.route('/host-project/<int:pid>', methods=['POST'])
@login_required
@active_trial_required
def host_project(pid):
    p = Project.query.get_or_404(pid)
    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id))
    
    entry_file = next((f for f in os.listdir(user_dir) if f in ['main.py', 'bot.py', 'app.py', 'index.js']), None)
            
    if not entry_file:
        flash('Error: Start karne ke liye main.py ya bot.py upload karein!', 'error')
        return redirect(f'/project/{pid}')

    try:
        cmd = [sys.executable, entry_file] if entry_file.endswith('.py') else ['node', entry_file]
        process = subprocess.Popen(cmd, cwd=user_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid)
        active_processes[p.id] = process
        p.status = 'RUNNING'
        db.session.commit()
        log_activity(f"Project {p.name} hosted.")
        flash('Bot Started! 🚀', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    return redirect(f'/project/{pid}')

@app.route('/stop-project/<int:pid>', methods=['POST'])
@login_required
def stop_project(pid):
    p = Project.query.get_or_404(pid)
    process = active_processes.get(p.id)
    if process and process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=5)
        except: pass
    if p.id in active_processes: del active_processes[p.id]
    p.status = 'STOPPED'
    db.session.commit()
    log_activity(f"Project {p.name} stopped.")
    flash('Bot Stopped! 🛑', 'success')
    return redirect(f'/project/{pid}')

@app.route('/restart-project/<int:pid>', methods=['POST'])
@login_required
def restart_project(pid):
    p = Project.query.get_or_404(pid)
    process = active_processes.get(p.id)
    if process and process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=5)
        except: pass
        if p.id in active_processes: del active_processes[p.id]

    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id))
    entry_file = next((f for f in os.listdir(user_dir) if f in ['main.py', 'bot.py', 'app.py', 'index.js']), None)
    
    if entry_file:
        cmd = [sys.executable, entry_file] if entry_file.endswith('.py') else ['node', entry_file]
        try:
            active_processes[p.id] = subprocess.Popen(cmd, cwd=user_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid)
            p.status = 'RUNNING'
            db.session.commit()
            log_activity(f"Project {p.name} restarted.")
            flash('Bot Restarted! 🔄', 'success')
        except Exception as e: flash(f'Restart fail: {str(e)}', 'error')
    else: flash('Error: File nahi mili!', 'error')
    return redirect(f'/project/{pid}')

@app.route('/view/<int:pid>/<path:filename>')
def view_file(pid, filename):
    p = Project.query.get_or_404(pid)
    if p.status != 'RUNNING':
        return "Not Found", 404
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id)), filename)

# ==========================================
# OWNER PANEL
# ==========================================
@app.route('/owner')
@login_required
@owner_required
def owner_dashboard():
    config = SiteConfig.query.first()
    all_users = User.query.filter_by(is_owner=False).all()
    total_projects = Project.query.count()
    disk_mb = round(dir_size(app.config['UPLOAD_FOLDER']) / (1024 * 1024), 2)

    users_html = "".join([f"""
        <div class="bg-slate-800 border border-slate-700 rounded-xl p-4 flex justify-between items-center mb-3">
            <div>
                <p class="font-bold text-white text-lg">{u.email} {'🔴 (Suspended)' if u.is_suspended else ''}</p>
                <div class="flex gap-3 text-xs text-slate-400 mt-1">
                    <span>Limit: {u.file_limit_mb}MB</span>
                    <span>Expires: {u.trial_expiry.strftime('%d %b %Y') if u.trial_expiry else 'Never'}</span>
                </div>
            </div>
            <div class="flex gap-2 flex-wrap justify-end">
                <a href="/owner/impersonate/{u.id}" class="bg-yellow-500/20 text-yellow-400 hover:bg-yellow-500 hover:text-white px-3 py-1.5 rounded-lg text-xs font-bold transition">Login As</a>
                <a href="/owner/suspend/{u.id}" class="bg-orange-500/20 text-orange-400 hover:bg-orange-500 hover:text-white px-3 py-1.5 rounded-lg text-xs font-bold transition">{'Unsuspend' if u.is_suspended else 'Suspend'}</a>
                <a href="/owner/user/{u.id}" class="bg-slate-700 text-white px-3 py-1.5 rounded-lg text-xs font-bold transition">Files</a>
                <a href="/owner/delete/{u.id}" class="bg-red-500/20 text-red-400 hover:bg-red-500 hover:text-white px-3 py-1.5 rounded-lg text-xs font-bold border border-red-500/50 transition" onclick="return confirm('Delete user and all data?')">Delete</a>
            </div>
        </div>""" for u in all_users])

    logs_html = "".join([f"<div class='text-xs text-slate-400 border-b border-slate-700 py-1'>[{l.timestamp.strftime('%H:%M')}] {l.message}</div>" for l in ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(5)])

    content = f"""
    <div class="grid grid-cols-3 gap-4 mb-8">
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 text-center">
            <h3 class="text-slate-400 text-xs font-bold uppercase mb-1">Users</h3>
            <p class="text-2xl font-extrabold text-white">{len(all_users)}</p>
        </div>
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 text-center">
            <h3 class="text-slate-400 text-xs font-bold uppercase mb-1">Projects</h3>
            <p class="text-2xl font-extrabold text-white">{total_projects}</p>
        </div>
        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-4 text-center">
            <h3 class="text-slate-400 text-xs font-bold uppercase mb-1">Disk Used</h3>
            <p class="text-2xl font-extrabold text-emerald-400">{disk_mb} MB</p>
        </div>
    </div>

    <div class="grid md:grid-cols-2 gap-8 mb-8">
        <div class="space-y-4">
            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-5">
                <h2 class="text-sm font-bold text-white mb-3">📢 Broadcast Message</h2>
                <form method="POST" action="/owner/broadcast" class="flex gap-2">
                    <input name="message" value="{config.broadcast_message}" placeholder="Type message for all users..." class="flex-1 p-2 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none">
                    <button class="bg-blue-500 text-white px-3 py-2 rounded-lg text-xs font-bold">Send</button>
                </form>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-5">
                <h2 class="text-sm font-bold text-white mb-4">⚙️ Site Settings</h2>
                <form method="POST" action="/owner/settings" class="space-y-3">
                    <div class="grid grid-cols-2 gap-2">
                        <div>
                            <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Site Name</label>
                            <input name="site_name" value="{config.site_name}" required maxlength="50" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                        </div>
                        <div>
                            <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Tagline</label>
                            <input name="site_tagline" value="{config.site_tagline}" required maxlength="50" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                        </div>
                    </div>
                    <div>
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Login Title</label>
                        <input name="login_title" value="{config.login_title}" required maxlength="80" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Login Subtitle</label>
                        <input name="login_subtitle" value="{config.login_subtitle}" required maxlength="150" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Telegram Username</label>
                        <input name="telegram_username" value="{config.telegram_username}" required maxlength="50" placeholder="without @" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Buy Button Text</label>
                        <input name="button_text" value="{config.button_text}" required maxlength="50" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                    </div>
                    <div>
                        <label class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 block">Buy Button Link</label>
                        <input name="button_link" value="{config.button_link}" maxlength="300" placeholder="https://t.me/username (empty = auto)" class="w-full p-2.5 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none focus:border-blue-500">
                        <p class="text-[9px] text-slate-500 mt-1">Empty chhodo = Telegram username auto use hoga</p>
                    </div>
                    <button type="submit" class="w-full bg-blue-500 hover:bg-blue-600 text-white font-bold py-2.5 rounded-lg text-sm shadow-lg transition">💾 Save All Settings</button>
                </form>
            </div>

            <div class="bg-slate-800 border border-slate-700 rounded-2xl p-5">
                <h2 class="text-sm font-bold text-white mb-3">🎟️ Create Coupon</h2>
                <form method="POST" action="/owner/coupons" class="flex gap-2">
                    <input name="code" placeholder="Code (e.g. VIP7)" required class="w-1/2 p-2 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none uppercase">
                    <input name="days" type="number" placeholder="Days" required class="w-1/4 p-2 bg-slate-900 border border-slate-700 rounded-lg text-sm text-white outline-none">
                    <button class="w-1/4 bg-emerald-600 text-white rounded-lg text-xs font-bold">Create</button>
                </form>
            </div>
        </div>

        <div class="bg-slate-800 border border-slate-700 rounded-2xl p-5 flex flex-col">
            <h2 class="text-sm font-bold text-white mb-3">📝 Recent Activity</h2>
            <div class="flex-1 overflow-y-auto max-h-48">{logs_html if logs_html else '<p class="text-xs text-slate-500">No logs yet.</p>'}</div>
        </div>
    </div>

    <div class="bg-slate-800 border border-slate-700 rounded-2xl p-6 mb-8">
        <h2 class="text-lg font-bold text-white mb-4">➕ Create User</h2>
        <form method="POST" action="/owner/create" class="grid grid-cols-1 md:grid-cols-4 gap-3">
            <input name="email" type="email" placeholder="Email" required class="p-3 bg-slate-900 border border-slate-700 rounded-xl text-white text-sm outline-none">
            <input name="password" type="text" placeholder="Password" required class="p-3 bg-slate-900 border border-slate-700 rounded-xl text-white text-sm outline-none">
            <div class="flex gap-2">
                <input name="days" type="number" placeholder="Days" required class="w-1/2 p-3 bg-slate-900 border border-slate-700 rounded-xl text-white text-sm outline-none">
                <input name="limit" type="number" placeholder="MB" required class="w-1/2 p-3 bg-slate-900 border border-slate-700 rounded-xl text-white text-sm outline-none">
            </div>
            <button type="submit" class="bg-red-500 text-white font-bold rounded-xl py-3 shadow-lg">Create</button>
        </form>
    </div>

    <h2 class="text-xl font-bold text-white mb-4">All Users ({len(all_users)})</h2>
    {users_html}

    <div class="bg-red-900/20 border border-red-500/50 rounded-2xl p-6 mt-12">
        <h2 class="text-xl font-bold text-red-400 mb-2">⚠️ Danger Zone</h2>
        <form method="POST" action="/owner/reset-system" onsubmit="return confirm('WIPE ALL DATA EXCEPT OWNER?');">
            <button type="submit" class="bg-red-500 text-white font-bold py-3 px-6 rounded-xl shadow-lg">Reset Entire System</button>
        </form>
    </div>
    """
    return render_template_string(OWNER_UI, content=content, current_user=current_user)

@app.route('/owner/broadcast', methods=['POST'])
@login_required
@owner_required
def owner_broadcast():
    config = SiteConfig.query.first()
    config.broadcast_message = request.form['message']
    db.session.commit()
    flash('Broadcast updated!', 'success')
    return redirect('/owner')

@app.route('/owner/settings', methods=['POST'])
@login_required
@owner_required
def owner_settings():
    config = SiteConfig.query.first()
    config.telegram_username = request.form.get('telegram_username', '').replace('@', '').strip()
    config.site_name         = request.form.get('site_name', '').strip() or 'HostX'
    config.site_tagline      = request.form.get('site_tagline', '').strip() or 'Bot Hosting'
    config.login_title       = request.form.get('login_title', '').strip() or 'Welcome Back'
    config.login_subtitle    = request.form.get('login_subtitle', '').strip() or 'Sign in to continue'
    config.button_text       = request.form.get('button_text', '').strip() or 'DM to Buy Access'
    config.button_link       = request.form.get('button_link', '').strip()
    db.session.commit()
    log_activity("Admin updated site branding.")
    flash('Site settings updated! ✅', 'success')
    return redirect('/owner')

@app.route('/owner/coupons', methods=['POST'])
@login_required
@owner_required
def owner_coupons():
    from sqlalchemy.exc import IntegrityError
    code = request.form['code'].upper()
    try:
        days = int(request.form['days'])
    except ValueError:
        flash('Days must be a number!', 'error')
        return redirect('/owner')
    try:
        db.session.add(Coupon(code=code, days=days))
        db.session.commit()
        flash('Coupon created!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('Coupon already exists!', 'error')
    return redirect('/owner')

@app.route('/owner/impersonate/<int:uid>')
@login_required
@owner_required
def owner_impersonate(uid):
    user = User.query.get_or_404(uid)
    if user.is_owner:
        flash('Cannot impersonate another owner.', 'error')
        return redirect('/owner')
    if user.is_suspended:
        flash('Cannot impersonate suspended user.', 'error')
        return redirect('/owner')
    session['impersonating'] = True
    login_user(user)
    log_activity(f"Admin impersonated {user.email}.")
    flash(f'Now logged in as {user.email}', 'success')
    return redirect('/dashboard')

@app.route('/return-to-admin')
@login_required
def return_to_admin():
    if session.get('impersonating'):
        admin = User.query.filter_by(is_owner=True).first()
        if admin:
            login_user(admin)
        session.pop('impersonating', None)
        flash('Returned to admin.', 'success')
    return redirect('/owner')

@app.route('/owner/suspend/<int:uid>')
@login_required
@owner_required
def owner_suspend(uid):
    user = User.query.get_or_404(uid)
    if user.is_owner:
        flash('Owner account cannot be suspended.', 'error')
        return redirect('/owner')
    user.is_suspended = not user.is_suspended
    db.session.commit()
    log_activity(f"User {user.email} suspension toggled to {user.is_suspended}.")
    flash(f'User {"suspended" if user.is_suspended else "unsuspended"}!', 'success')
    return redirect('/owner')

@app.route('/owner/create', methods=['POST'])
@login_required
@owner_required
def owner_create():
    try:
        days = int(request.form['days'])
        limit = int(request.form['limit'])
    except ValueError:
        flash('Days and Limit must be numbers!', 'error')
        return redirect('/owner')
    if not User.query.filter_by(email=request.form['email']).first():
        new_user = User(
            email=request.form['email'],
            password_hash=generate_password_hash(request.form['password']),
            trial_expiry=(datetime.utcnow() + timedelta(days=days)) if days > 0 else None,
            file_limit_mb=limit
        )
        db.session.add(new_user)
        db.session.commit()
        log_activity(f"Admin created user {new_user.email}.")
        flash('User created!', 'success')
    else:
        flash('Email exists!', 'error')
    return redirect('/owner')

@app.route('/owner/delete/<int:uid>')
@login_required
@owner_required
def owner_delete(uid):
    user = User.query.get_or_404(uid)
    if user.is_owner:
        flash('Owner account cannot be deleted.', 'error')
        return redirect('/owner')
    shutil.rmtree(os.path.join(app.config['UPLOAD_FOLDER'], str(user.id)), ignore_errors=True)
    db.session.delete(user)
    db.session.commit()
    log_activity(f"Admin deleted user {user.email}.")
    flash('User deleted!', 'success')
    return redirect('/owner')

@app.route('/owner/reset-system', methods=['POST'])
@login_required
@owner_required
def owner_reset_system():
    owner = User.query.filter_by(is_owner=True).first()
    owner_folder = os.path.join(app.config['UPLOAD_FOLDER'], str(owner.id)) if owner else None

    Project.query.delete()
    User.query.filter_by(is_owner=False).delete()
    Coupon.query.delete()
    ActivityLog.query.delete()
    db.session.commit()

    if os.path.exists(app.config['UPLOAD_FOLDER']):
        for name in os.listdir(app.config['UPLOAD_FOLDER']):
            path = os.path.join(app.config['UPLOAD_FOLDER'], name)
            if owner_folder and path == owner_folder:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass

    log_activity("SYSTEM RESET BY ADMIN.")
    flash('System reset! (Owner data preserved)', 'success')
    return redirect('/owner')

@app.route('/owner/user/<int:uid>')
@login_required
@owner_required
def owner_user_projects(uid):
    user = User.query.get_or_404(uid)
    projects = Project.query.filter_by(user_id=uid).all()
    proj_html = "".join([f"<div class='bg-slate-800 p-3 rounded-xl mb-2 flex justify-between'><a href='/owner/project/{p.id}' class='text-white'>{p.name}</a><span class='text-xs'>{p.status}</span></div>" for p in projects])
    content = f"<a href='/owner' class='text-white'>← Back</a><h1 class='text-white text-xl my-4'>{user.email}'s Projects</h1>{proj_html or '<p class=\"text-slate-400\">No projects.</p>'}"
    return render_template_string(OWNER_UI, content=content, current_user=current_user)

@app.route('/owner/project/<int:pid>')
@login_required
@owner_required
def owner_project_files(pid):
    p = Project.query.get_or_404(pid)
    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id))
    files = os.listdir(user_dir) if os.path.exists(user_dir) else []
    files_html = "".join([f"<div class='flex justify-between border-b border-slate-700 py-2 text-white'><span>{f}</span><a href='/owner/download/{p.id}/{f}' class='bg-blue-500 text-xs px-2 py-1 rounded text-white'>Download</a></div>" for f in files]) or '<p class="text-slate-400">No files.</p>'
    content = f"<a href='/owner/user/{p.user_id}' class='text-white'>← Back</a><h1 class='text-white text-xl my-4'>{p.name} Files</h1>{files_html}"
    return render_template_string(OWNER_UI, content=content, current_user=current_user)

@app.route('/owner/download/<int:pid>/<path:filename>')
@login_required
@owner_required
def owner_download_file(pid, filename):
    p = Project.query.get_or_404(pid)
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], str(p.user_id), str(p.id)), filename, as_attachment=True)

# ==========================================
# RUN
# ==========================================
if __name__ == '__main__':
    print("\n🔥 HostX running on http://localhost:5000")
    print(f"👑 Admin: {OWNER_EMAIL} | Pass: admin123\n")
    app.run(host='0.0.0.0', port=5000, debug=True)