from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from functools import wraps
from datetime import datetime, timedelta
import jwt
import re
import random
import string
import os
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'clanboost-pro-super-secret-key-2025'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_EXPIRATION_HOURS'] = 24

# ==================== REAL GUILD GLORY AUTOMATION ====================
GUILD_INFO_API = 'https://danger-guild-management-web.vercel.app/guild'
GUILD_JOIN_API = 'https://danger-guild-management-web.vercel.app/join'
BASE_DIR = Path(__file__).resolve().parent
UDP_SOURCE_DIR = BASE_DIR / 'udp'
RUNTIME_DIR = BASE_DIR / 'runtime'  # retained for compatibility; launch no longer copies into it
RUN_PROCESSES = {}
RUN_LOCK = threading.Lock()
REGION_MAP = {'in': 'IND', 'ind': 'IND', 'bd': 'BD', 'br': 'BR', 'me': 'ME', 'indo': 'ID', 'id': 'ID', 'ghrab': 'ME'}

def normalize_region(value):
    return REGION_MAP.get(str(value or '').strip().lower(), str(value or 'IND').strip().upper())

def fetch_guild_info(guild_id, region):
    # Guild info is best-effort. A temporary upstream account/API error must
    # not prevent a paid launch from reaching the account/join stage.
    try:
        r = requests.get(
            GUILD_INFO_API,
            params={'guild_id': str(guild_id), 'region': normalize_region(region)},
            timeout=20
        )
        r.raise_for_status()
        data = r.json()
        if data.get('status') != 'error' and data.get('success') is not False:
            return data
        raise RuntimeError(data.get('message') or data.get('error') or 'Guild info API failed')
    except Exception as exc:
        return {
            'status': 'partial',
            'GuildId': int(guild_id) if str(guild_id).isdigit() else str(guild_id),
            'GuildRegion': normalize_region(region),
            'GuildName': f'Guild {guild_id}',
            'GuildLevel': 0,
            'CurrentMembers': 0,
            'MaxMembers': 0,
            'GuildLeader': {},
            '_info_warning': f'Guild details unavailable: {exc}'
        }

def copy_udp_runtime(run_id):
    import shutil
    runtime = RUNTIME_DIR / str(run_id)
    if runtime.exists(): shutil.rmtree(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    shutil.copytree(UDP_SOURCE_DIR, runtime)
    return runtime

def write_runtime_json(path, name, data):
    tmp = path / (name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path / name)

def run_glory_job(run_id, group_id, guild_id, region, guild_data):
    # Single fixed UDP workspace:
    # Guild Info -> gen.py in udp/ (4 threads) -> join -> udp/app.py -> Glory for 8h.
    with app.app_context():
        run = db.session.get(AutomationRun, run_id)
        group = db.session.get(Group, group_id)
        if not run or not group:
            return
        try:
            run.stage = 'guild'
            run.message = 'Guild information loaded. Preparing 4-account generation...'
            db.session.commit()
            time.sleep(1.0)

            # Do NOT create/copy a per-run runtime for the bot files.
            # The requested UDP folder itself is the working directory.
            udp_dir = UDP_SOURCE_DIR
            gen_script = udp_dir / 'gen.py'
            launcher = udp_dir / 'launch_runner.py'
            bot_app = udp_dir / 'app.py'
            if not gen_script.exists():
                raise RuntimeError(f'gen.py not found in udp folder: {gen_script}')
            if not launcher.exists():
                raise RuntimeError(f'launch_runner.py not found in udp folder: {launcher}')
            if not bot_app.exists():
                raise RuntimeError(f'app.py not found in udp folder: {bot_app}')

            # One shared udp/accounts.json is intentional for this flow.
            accounts_file = udp_dir / 'accounts.json'

            run.stage = 'generating'
            run.message = 'Generating 4 accounts with gen.py (4 threads)...'
            db.session.commit()

            # Run the supplied launcher from the SAME udp directory.
            gen = subprocess.run(
                [sys.executable, '-u', 'launch_runner.py',
                 '--total', '4', '--threads', '4', '--fresh'],
                cwd=str(udp_dir),
                capture_output=True,
                text=True,
                timeout=1800,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'}
            )
            if gen.returncode != 0:
                detail = (gen.stderr or gen.stdout or 'Account generation failed').strip()
                raise RuntimeError('Account generation failed: ' + detail[-1800:])

            if not accounts_file.exists():
                raise RuntimeError('gen.py finished but udp/accounts.json was not created')

            try:
                accounts = json.loads(accounts_file.read_text(encoding='utf-8'))
            except Exception as exc:
                raise RuntimeError(f'Invalid udp/accounts.json: {exc}')

            accounts = [
                a for a in accounts
                if isinstance(a, dict) and a.get('uid') and a.get('password')
            ][:4]
            if len(accounts) != 4:
                raise RuntimeError(f'Expected 4 generated accounts, got {len(accounts)}')

            run.stage = 'joining'
            run.message = 'Sending 4 join requests...'
            db.session.commit()

            results = []
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def join_one(account):
                uid = str(account['uid'])
                try:
                    resp = requests.get(
                        GUILD_JOIN_API,
                        params={
                            'guild_id': str(guild_id),
                            'uid': uid,
                            'password': account['password']
                        },
                        timeout=20
                    )
                    try:
                        payload = resp.json()
                    except Exception:
                        payload = {'raw': resp.text}
                    ok = payload.get('success') is True
                    return {
                        'uid': uid,
                        'success': ok,
                        'status': 'Join Success' if ok else 'Join Failed',
                        'message': payload.get('message', ''),
                        'http_status': resp.status_code
                    }
                except Exception as exc:
                    return {
                        'uid': uid,
                        'success': False,
                        'status': 'Join Failed',
                        'message': str(exc)
                    }

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(join_one, a) for a in accounts]
                for future in as_completed(futures):
                    results.append(future.result())

            results.sort(key=lambda x: str(x.get('uid', '')))
            run.join_results = json.dumps(results, ensure_ascii=False)
            run.join_success = sum(1 for x in results if x['success'])
            run.join_failed = len(results) - run.join_success
            db.session.commit()

            # Give the UDP app the guild information without copying its files.
            write_runtime_json(udp_dir, 'guild_config.json', {
                'guild_id': str(guild_id),
                'region': normalize_region(region),
                'guild_name': guild_data.get('GuildName', ''),
                'guild_level': guild_data.get('GuildLevel', 0),
                'current_members': guild_data.get('CurrentMembers', 0),
                'max_members': guild_data.get('MaxMembers', 0),
                'leader_name': (guild_data.get('GuildLeader') or {}).get('Name', '')
            })

            run.stage = 'starting'
            run.message = 'Starting UDP app.py...'
            db.session.commit()

            # Start the supplied UDP app.py from the SAME udp folder.
            port = 9000 + (int(group_id) % 900)
            env = os.environ.copy()
            env['UDP_WEB_PORT'] = str(port)

            proc = subprocess.Popen(
                [sys.executable, '-u', 'app.py'],
                cwd=str(udp_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT
            )
            with RUN_LOCK:
                RUN_PROCESSES[group_id] = {
                    'process': proc,
                    'port': port,
                    'runtime': str(udp_dir)
                }

            # Wait for the UDP app, then start Glory workers.
            ready = False
            for _ in range(30):
                try:
                    requests.get(f'http://127.0.0.1:{port}/api/automation', timeout=2)
                    ready = True
                    break
                except Exception:
                    time.sleep(1)

            if not ready:
                raise RuntimeError('UDP app.py did not start')

            try:
                start_resp = requests.post(
                    f'http://127.0.0.1:{port}/api/glory/start',
                    timeout=15
                )
                if start_resp.status_code >= 400:
                    raise RuntimeError(
                        f'Glory start failed: HTTP {start_resp.status_code} '
                        f'{start_resp.text[:500]}'
                    )
            except requests.RequestException as exc:
                raise RuntimeError(f'Glory start request failed: {exc}')

            run.pid = proc.pid
            run.port = port
            run.stage = 'running'
            run.status = 'running'
            run.message = 'Glory started'
            run.started_at = datetime.utcnow()
            run.ends_at = datetime.utcnow() + timedelta(hours=8)
            group.status = 'running'
            group.bot_count = 4
            db.session.commit()

            deadline = time.time() + 8 * 3600
            while time.time() < deadline and proc.poll() is None:
                try:
                    info = requests.get(
                        f'http://127.0.0.1:{port}/api/automation',
                        timeout=5
                    ).json()
                    live = info.get('guild_live') or {}
                    if live.get('glory') is not None:
                        group.glory_earned = int(live.get('glory') or 0)
                    if live.get('total_members') is not None:
                        group.members = (
                            f"{live.get('total_members')}/"
                            f"{guild_data.get('MaxMembers', live.get('total_members'))}"
                        )
                    db.session.commit()
                except Exception:
                    pass
                time.sleep(15)

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()

            run.status = 'completed'
            run.stage = 'completed'
            run.message = '8 hour run completed'
            group.status = 'stopped'
            db.session.commit()

        except Exception as exc:
            run.status = 'failed'
            run.stage = 'error'
            run.message = str(exc)
            group.status = 'stopped'
            db.session.commit()
        finally:
            with RUN_LOCK:
                RUN_PROCESSES.pop(group_id, None)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# ============================================
# Database Models
# ============================================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    role = db.Column(db.String(20), default='user')
    max_groups = db.Column(db.Integer, default=13)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    
    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'basic_credits': self.basic_credits,
            'premium_credits': self.premium_credits,
            'role': self.role,
            'max_groups': self.max_groups
        }

class PendingOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    clan_id = db.Column(db.String(50), nullable=False)
    region = db.Column(db.String(20), nullable=False)
    region_tier = db.Column(db.String(20), default='basic')
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('pending_orders', lazy=True))
    
    def to_dict(self):
        return {
            'id': self.id,
            'clan_id': self.clan_id,
            'region': self.region,
            'region_tier': self.region_tier,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'username': self.user.username if self.user else 'Unknown'
        }

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    clan_id = db.Column(db.String(50), nullable=False)
    region = db.Column(db.String(20), nullable=False)
    region_tier = db.Column(db.String(20), default='basic')
    status = db.Column(db.String(20), default='running')
    glory_earned = db.Column(db.Integer, default=0)
    guild_name = db.Column(db.String(100), default='')
    leader_name = db.Column(db.String(100), default='')
    bot_count = db.Column(db.Integer, default=6)
    members = db.Column(db.String(20), default='0/20')
    level = db.Column(db.Integer, default=1)
    target_glory = db.Column(db.Integer, default=100000)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Auto glory settings per group
    auto_glory_enabled = db.Column(db.Boolean, default=False)
    auto_glory_interval = db.Column(db.Integer, default=5)
    auto_glory_amount = db.Column(db.Integer, default=100)
    auto_glory_last_run = db.Column(db.DateTime, nullable=True)
    
    user = db.relationship('User', backref=db.backref('groups', lazy=True))
    
    def to_dict(self):
        return {
            'id': self.id,
            'clan_id': self.clan_id,
            'region': self.region,
            'region_tier': self.region_tier,
            'status': self.status,
            'glory_earned': self.glory_earned,
            'guild_name': self.guild_name,
            'leader_name': self.leader_name,
            'bot_count': self.bot_count,
            'members': self.members,
            'level': self.level,
            'target_glory': self.target_glory,
            'auto_glory_enabled': self.auto_glory_enabled,
            'auto_glory_interval': self.auto_glory_interval,
            'auto_glory_amount': self.auto_glory_amount,
            'started_at': self.started_at.isoformat() if self.started_at else None
        }

class AutomationRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    status = db.Column(db.String(20), default='starting')
    stage = db.Column(db.String(30), default='starting')
    message = db.Column(db.String(500), default='')
    join_results = db.Column(db.Text, default='[]')
    join_success = db.Column(db.Integer, default=0)
    join_failed = db.Column(db.Integer, default=0)
    runtime_path = db.Column(db.String(500), default='')
    pid = db.Column(db.Integer, nullable=True)
    port = db.Column(db.Integer, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    def to_dict(self):
        try: results = json.loads(self.join_results or '[]')
        except Exception: results = []
        return {'id': self.id, 'group_id': self.group_id, 'status': self.status, 'stage': self.stage, 'message': self.message,
                'join_results': results, 'join_success': self.join_success, 'join_failed': self.join_failed,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'ends_at': self.ends_at.isoformat() if self.ends_at else None}

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    order_id = db.Column(db.String(100))
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('transactions', lazy=True))
    
    def to_dict(self):
        return {
            'id': self.id,
            'amount': self.amount,
            'basic_credits': self.basic_credits,
            'premium_credits': self.premium_credits,
            'order_id': self.order_id,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    basic_credits = db.Column(db.Integer, default=0)
    premium_credits = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='active')
    used_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    creator = db.relationship('User', foreign_keys=[created_by])
    redeemer = db.relationship('User', foreign_keys=[used_by])
    
    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'basic_credits': self.basic_credits,
            'premium_credits': self.premium_credits,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    clan_id = db.Column(db.String(50))
    details = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('activities', lazy=True))
    
    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'clan_id': self.clan_id,
            'details': self.details,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None
        }

# ============================================
# Background Thread for Auto Glory (Per Group)
# ============================================

def auto_glory_worker():
    """Background thread to add glory automatically per group settings"""
    import threading
    import time
    while True:
        try:
            with app.app_context():
                groups = Group.query.filter_by(status='running', auto_glory_enabled=True).all()
                now = datetime.utcnow()
                
                for group in groups:
                    if group.auto_glory_last_run:
                        next_run = group.auto_glory_last_run + timedelta(minutes=group.auto_glory_interval)
                        if now < next_run:
                            continue
                    
                    # Real Glory is read from the running UDP worker; no fake increments.
                    group.auto_glory_last_run = now
                
                db.session.commit()
            time.sleep(30)
        except Exception as e:
            print(f"Auto glory error: {e}")
            time.sleep(60)

# Start background thread
import threading
thread = threading.Thread(target=auto_glory_worker, daemon=True)
thread.start()

# ============================================
# JWT Helper Functions
# ============================================

def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(hours=app.config['JWT_EXPIRATION_HOURS']),
        'iat': datetime.utcnow()
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def verify_token(token):
    try:
        payload = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload['user_id']
    except:
        return None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('auth_token')
        if not token:
            resp = redirect(url_for('login_page'))
            return resp
        user_id = verify_token(token)
        if not user_id:
            resp = redirect(url_for('login_page'))
            resp.set_cookie('auth_token', '', expires=0, path='/')
            return resp
        user = db.session.get(User, user_id)
        if not user:
            resp = redirect(url_for('login_page'))
            resp.set_cookie('auth_token', '', expires=0, path='/')
            return resp
        return f(user, *args, **kwargs)
    return decorated_function

def api_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('auth_token')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        user_id = verify_token(token)
        if not user_id:
            return jsonify({'error': 'Session expired'}), 401
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 401
        return f(user, *args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('auth_token')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        user_id = verify_token(token)
        if not user_id:
            return jsonify({'error': 'Session expired'}), 401
        user = db.session.get(User, user_id)
        if not user or user.role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(user, *args, **kwargs)
    return decorated_function

def generate_coupon_code():
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choices(characters, k=16))

# ============================================
# Page Routes
# ============================================

@app.route('/')
def index_page():
    token = request.cookies.get('auth_token')
    if token:
        user_id = verify_token(token)
        if user_id:
            user = db.session.get(User, user_id)
            if user:
                if user.role == 'admin':
                    return redirect(url_for('admin_page'))
                return redirect(url_for('home_page'))
    return render_template('index.html')

@app.route('/login')
def login_page():
    token = request.cookies.get('auth_token')
    if token:
        user_id = verify_token(token)
        if user_id:
            user = db.session.get(User, user_id)
            if user:
                if user.role == 'admin':
                    return redirect(url_for('admin_page'))
                return redirect(url_for('home_page'))
        else:
            resp = redirect(url_for('login_page'))
            resp.set_cookie('auth_token', '', expires=0, path='/')
            return resp
    return render_template('login.html')

@app.route('/home')
@login_required
def home_page(user):
    if user.role == 'admin':
        return redirect(url_for('admin_page'))
    return render_template('home.html')

@app.route('/admin')
def admin_page():
    token = request.cookies.get('auth_token')
    if not token:
        return redirect(url_for('login_page'))
    user_id = verify_token(token)
    if not user_id:
        resp = redirect(url_for('login_page'))
        resp.set_cookie('auth_token', '', expires=0, path='/')
        return resp
    user = db.session.get(User, user_id)
    if not user or user.role != 'admin':
        return redirect(url_for('home_page'))
    return render_template('admin.html')

# ============================================
# Auth API Routes
# ============================================

@app.route('/api/auth/me', methods=['GET'])
@api_login_required
def api_get_me(user):
    return jsonify(user.to_dict()), 200

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    
    if not username or not email or not password:
        return jsonify({'error': 'All fields required'}), 400
    
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    if not re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', email):
        return jsonify({'error': 'Invalid email'}), 400
    
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 400
    
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username taken'}), 400
    
    user = User(username=username, email=email)
    user.set_password(password)
    user.basic_credits = 0
    user.premium_credits = 0
    
    db.session.add(user)
    db.session.commit()
    
    log = ActivityLog(user_id=user.id, action='register', details='User registered')
    db.session.add(log)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Registration successful'}), 200

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    token = generate_token(user.id)
    
    log = ActivityLog(user_id=user.id, action='login', details='User logged in')
    db.session.add(log)
    db.session.commit()
    
    response = jsonify({'success': True, 'user': user.to_dict()})
    response.set_cookie('auth_token', token, httponly=True, samesite='Lax', max_age=86400)
    return response, 200

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    response = jsonify({'success': True})
    response.set_cookie('auth_token', '', expires=0, path='/')
    return response, 200

@app.route('/api/auth/change-password', methods=['POST'])
@api_login_required
def api_change_password(user):
    data = request.get_json()
    current = data.get('current_password')
    new_pw = data.get('new_password')
    
    if not current or not new_pw:
        return jsonify({'error': 'All fields required'}), 400
    
    if len(new_pw) < 6:
        return jsonify({'error': 'Password must be 6+ characters'}), 400
    
    if not user.check_password(current):
        return jsonify({'error': 'Current password incorrect'}), 401
    
    user.set_password(new_pw)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Password changed'}), 200

# ============================================
# Admin API Routes
# ============================================

@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_get_stats(admin):
    total_users = User.query.count()
    active_groups = Group.query.filter_by(status='running').count()
    total_glory = db.session.query(db.func.sum(Group.glory_earned)).scalar() or 0
    pending_tx = Transaction.query.filter_by(status='pending').count()
    pending_orders = PendingOrder.query.filter_by(status='pending').count()
    
    return jsonify({
        'total_users': total_users,
        'active_groups': active_groups,
        'total_glory': total_glory,
        'pending_transactions': pending_tx,
        'pending_orders': pending_orders
    }), 200

@app.route('/api/admin/active-groups', methods=['GET'])
@admin_required
def admin_get_active_groups(admin):
    groups = Group.query.filter_by(status='running').order_by(Group.id.desc()).all()
    result = []
    for group in groups:
        user = db.session.get(User, group.user_id)
        result.append({
            'id': group.id,
            'guild_name': group.guild_name,
            'clan_id': group.clan_id,
            'region': group.region,
            'region_tier': group.region_tier,
            'glory_earned': group.glory_earned,
            'target_glory': group.target_glory,
            'level': group.level,
            'auto_glory_enabled': group.auto_glory_enabled,
            'auto_glory_interval': group.auto_glory_interval,
            'auto_glory_amount': group.auto_glory_amount,
            'username': user.username if user else 'Unknown',
            'email': user.email if user else 'Unknown',
            'started_at': group.started_at.isoformat() if group.started_at else None
        })
    return jsonify(result), 200

@app.route('/api/admin/update-group-glory', methods=['POST'])
@admin_required
def admin_update_group_glory(admin):
    data = request.get_json()
    group_id = data.get('group_id')
    new_glory = data.get('new_glory', 0)
    
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    old_glory = group.glory_earned
    group.glory_earned = new_glory
    db.session.commit()
    
    log = ActivityLog(
        user_id=group.user_id,
        action='glory_updated',
        clan_id=group.clan_id,
        details=f'Glory updated from {old_glory} to {new_glory} by admin'
    )
    db.session.add(log)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Glory updated successfully'}), 200

@app.route('/api/admin/update-group-auto-glory', methods=['POST'])
@admin_required
def admin_update_group_auto_glory(admin):
    data = request.get_json()
    group_id = data.get('group_id')
    enabled = data.get('enabled', False)
    interval = data.get('interval', 5)
    amount = data.get('amount', 100)
    
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    group.auto_glory_enabled = enabled
    group.auto_glory_interval = interval
    group.auto_glory_amount = amount
    if enabled:
        group.auto_glory_last_run = datetime.utcnow()
    
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Auto glory settings updated'}), 200

@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_get_users(admin):
    users = User.query.order_by(User.id.desc()).all()
    result = []
    for user in users:
        group_count = Group.query.filter_by(user_id=user.id).count()
        result.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'basic_credits': user.basic_credits,
            'premium_credits': user.premium_credits,
            'role': user.role,
            'group_count': group_count,
            'created_at': user.created_at.isoformat() if user.created_at else None
        })
    return jsonify(result), 200

@app.route('/api/admin/groups', methods=['GET'])
@admin_required
def admin_get_groups(admin):
    groups = Group.query.order_by(Group.id.desc()).all()
    result = []
    for group in groups:
        user = db.session.get(User, group.user_id)
        result.append({
            'id': group.id,
            'clan_id': group.clan_id,
            'region': group.region,
            'region_tier': group.region_tier,
            'status': group.status,
            'glory_earned': group.glory_earned,
            'guild_name': group.guild_name,
            'leader_name': group.leader_name,
            'bot_count': group.bot_count,
            'members': group.members,
            'level': group.level,
            'target_glory': group.target_glory,
            'username': user.username if user else 'Unknown',
            'started_at': group.started_at.isoformat() if group.started_at else None
        })
    return jsonify(result), 200

@app.route('/api/admin/transactions', methods=['GET'])
@admin_required
def admin_get_transactions(admin):
    txs = Transaction.query.order_by(Transaction.id.desc()).all()
    result = []
    for tx in txs:
        user = db.session.get(User, tx.user_id)
        result.append({
            'id': tx.id,
            'user_id': tx.user_id,
            'username': user.username if user else 'Unknown',
            'amount': tx.amount,
            'basic_credits': tx.basic_credits,
            'premium_credits': tx.premium_credits,
            'order_id': tx.order_id,
            'status': tx.status,
            'created_at': tx.created_at.isoformat() if tx.created_at else None
        })
    return jsonify(result), 200

@app.route('/api/admin/transaction/approve', methods=['POST'])
@admin_required
def admin_approve_transaction(admin):
    data = request.get_json()
    tx_id = data.get('transaction_id')
    
    tx = db.session.get(Transaction, tx_id)
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404
    
    if tx.status != 'pending':
        return jsonify({'error': 'Transaction already processed'}), 400
    
    user = db.session.get(User, tx.user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    user.basic_credits += tx.basic_credits
    user.premium_credits += tx.premium_credits
    tx.status = 'completed'
    
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Transaction approved'}), 200

@app.route('/api/admin/transaction/reject', methods=['POST'])
@admin_required
def admin_reject_transaction(admin):
    data = request.get_json()
    tx_id = data.get('transaction_id')
    
    tx = db.session.get(Transaction, tx_id)
    if not tx:
        return jsonify({'error': 'Transaction not found'}), 404
    
    if tx.status != 'pending':
        return jsonify({'error': 'Transaction already processed'}), 400
    
    tx.status = 'failed'
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Transaction rejected'}), 200

@app.route('/api/admin/update-credits', methods=['POST'])
@admin_required
def admin_update_credits(admin):
    data = request.get_json()
    user_id = data.get('user_id')
    basic_credits = data.get('basic_credits', 0)
    premium_credits = data.get('premium_credits', 0)
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    user.basic_credits = basic_credits
    user.premium_credits = premium_credits
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Credits updated'}), 200

@app.route('/api/admin/create-coupon', methods=['POST'])
@admin_required
def admin_create_coupon(admin):
    data = request.get_json()
    basic = data.get('basic_credits', 0)
    premium = data.get('premium_credits', 0)
    
    if basic == 0 and premium == 0:
        return jsonify({'error': 'Select at least 1 credit'}), 400
    
    code = generate_coupon_code()
    while Coupon.query.filter_by(code=code).first():
        code = generate_coupon_code()
    
    coupon = Coupon(
        code=code,
        created_by=admin.id,
        basic_credits=basic,
        premium_credits=premium,
        status='active'
    )
    db.session.add(coupon)
    db.session.commit()
    
    return jsonify({'success': True, 'coupon': coupon.to_dict()}), 200

@app.route('/api/admin/pending-orders', methods=['GET'])
@admin_required
def admin_get_pending_orders(admin):
    orders = PendingOrder.query.filter_by(status='pending').order_by(PendingOrder.id.desc()).all()
    result = []
    for order in orders:
        user = db.session.get(User, order.user_id)
        result.append({
            'id': order.id,
            'clan_id': order.clan_id,
            'region': order.region,
            'region_tier': order.region_tier,
            'created_at': order.created_at.isoformat() if order.created_at else None,
            'username': user.username if user else 'Unknown',
            'email': user.email if user else 'Unknown'
        })
    return jsonify(result), 200

@app.route('/api/admin/approve-order', methods=['POST'])
@admin_required
def admin_approve_order(admin):
    data = request.get_json()
    order_id = data.get('order_id')
    guild_name = data.get('guild_name', '')
    leader_name = data.get('leader_name', '')
    bot_count = data.get('bot_count', 6)
    members = data.get('members', '0/20')
    glory = data.get('glory', 0)
    level = data.get('level', 1)
    target_glory = data.get('target_glory', 100000)
    
    order = db.session.get(PendingOrder, order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    
    if order.status != 'pending':
        return jsonify({'error': 'Order already processed'}), 400
    
    group = Group(
        user_id=order.user_id,
        clan_id=order.clan_id,
        region=order.region,
        region_tier=order.region_tier,
        status='running',
        glory_earned=glory,
        guild_name=guild_name,
        leader_name=leader_name,
        bot_count=bot_count,
        members=members,
        level=level,
        target_glory=target_glory,
        started_at=datetime.utcnow()
    )
    db.session.add(group)
    
    order.status = 'approved'
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Order approved! Group created.'}), 200

@app.route('/api/admin/reject-order', methods=['POST'])
@admin_required
def admin_reject_order(admin):
    data = request.get_json()
    order_id = data.get('order_id')
    
    order = db.session.get(PendingOrder, order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    
    if order.status != 'pending':
        return jsonify({'error': 'Order already processed'}), 400
    
    user = db.session.get(User, order.user_id)
    if user:
        if order.region_tier == 'premium':
            user.premium_credits += 1
        else:
            user.basic_credits += 1
    
    order.status = 'rejected'
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Order rejected. Credits refunded.'}), 200

# ============================================
# User API Routes
# ============================================

@app.route('/api/client/pending-orders', methods=['GET'])
@api_login_required
def api_get_pending_orders(user):
    orders = PendingOrder.query.filter_by(user_id=user.id, status='pending').order_by(PendingOrder.id.desc()).all()
    return jsonify([o.to_dict() for o in orders]), 200

@app.route('/api/client/launch', methods=['POST'])
@api_login_required
def api_launch_group(user):
    data = request.get_json() or {}
    region_raw = data.get('region'); clan_id = str(data.get('clan_id') or '').strip()
    if not region_raw or not clan_id: return jsonify({'error': 'Region and Guild ID required'}), 400
    region = normalize_region(region_raw); tier = 'premium' if region in {'BR', 'ME'} else 'basic'
    if tier == 'premium':
        if user.premium_credits < 1: return jsonify({'error': 'Need 1 premium credit'}), 400
        user.premium_credits -= 1
    else:
        if user.basic_credits < 1: return jsonify({'error': 'Need 1 basic credit'}), 400
    try: guild_data = fetch_guild_info(clan_id, region)
    except Exception as exc:
        db.session.rollback(); return jsonify({'error': f'Guild info failed: {exc}'}), 502
    group = Group(user_id=user.id, clan_id=clan_id, region=region_raw, region_tier=tier, status='starting', glory_earned=0,
                  guild_name=guild_data.get('GuildName',''), leader_name=(guild_data.get('GuildLeader') or {}).get('Name',''), bot_count=4,
                  members=f"{guild_data.get('CurrentMembers',0)}/{guild_data.get('MaxMembers',0)}", level=int(guild_data.get('GuildLevel') or 1), target_glory=100000)
    db.session.add(group); db.session.flush()
    run = AutomationRun(group_id=group.id, status='starting', stage='guild', message='Guild loaded')
    db.session.add(run)
    db.session.add(ActivityLog(user_id=user.id, action='launch', clan_id=clan_id, details=f'Launch started for {guild_data.get("GuildName", clan_id)}'))
    db.session.commit()
    threading.Thread(target=run_glory_job, args=(run.id, group.id, clan_id, region, guild_data), daemon=True).start()
    return jsonify({'success': True, 'message': 'Launch started', 'group': group.to_dict(), 'run': run.to_dict(), 'guild': guild_data}), 200

@app.route('/api/client/launch-status/<int:group_id>', methods=['GET'])
@api_login_required
def api_launch_status(user, group_id):
    group = Group.query.filter_by(id=group_id, user_id=user.id).first()
    if not group: return jsonify({'error': 'Group not found'}), 404
    run = AutomationRun.query.filter_by(group_id=group.id).order_by(AutomationRun.id.desc()).first()
    if not run: return jsonify({'error': 'Launch status not found'}), 404
    return jsonify({'group': group.to_dict(), 'run': run.to_dict()}), 200

@app.route('/api/client/groups', methods=['GET'])
@api_login_required
def api_get_groups(user):
    groups = Group.query.filter_by(user_id=user.id).order_by(Group.id.desc()).all()
    return jsonify([g.to_dict() for g in groups]), 200

@app.route('/api/client/group-action', methods=['POST'])
@api_login_required
def api_group_action(user):
    data = request.get_json()
    group_id = data.get('group_id')
    action = data.get('action')
    
    group = Group.query.filter_by(id=group_id, user_id=user.id).first()
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    if action == 'stop':
        group.status = 'stopped'
        db.session.commit()
        return jsonify({'success': True, 'message': 'Bot stopped'}), 200
    
    elif action == 'start':
        group.status = 'running'
        db.session.commit()
        return jsonify({'success': True, 'message': 'Bot started'}), 200
    
    elif action == 'restart':
        group.status = 'running'
        group.glory_earned = 0
        group.started_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'message': 'Bot restarted'}), 200
    
    elif action == 'delete':
        db.session.delete(group)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Bot deleted'}), 200
    
    elif action == 'refund':
        if group.region_tier == 'premium':
            user.premium_credits += 1
        else:
            user.basic_credits += 1
        db.session.delete(group)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Refund processed'}), 200
    
    return jsonify({'error': 'Invalid action'}), 400

@app.route('/api/client/glory-progression', methods=['GET'])
@api_login_required
def api_glory_progression(user):
    group_id = request.args.get('group_id')
    group = Group.query.filter_by(id=group_id, user_id=user.id).first()
    if not group:
        return jsonify({'error': 'Group not found'}), 404
    
    snapshots = []
    total_glory = group.glory_earned or 0
    
    for i in range(5):
        snapshots.append({
            'captured_at': (datetime.utcnow() - timedelta(minutes=i*15)).isoformat(),
            'total_glory': max(0, total_glory - random.randint(0, 1000)),
            'change': random.randint(100, 500)
        })
    
    return jsonify({
        'snapshots': snapshots[::-1],
        'final_glory': {'total_glory': total_glory},
        'glory_farmed': total_glory
    }), 200

@app.route('/api/client/transactions', methods=['GET'])
@api_login_required
def api_get_transactions(user):
    txs = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id.desc()).all()
    return jsonify([t.to_dict() for t in txs]), 200

@app.route('/api/client/buy-credits', methods=['POST'])
@api_login_required
def api_buy_credits(user):
    data = request.get_json()
    basic = data.get('basic_credits', 0)
    premium = data.get('premium_credits', 0)
    order_id = data.get('order_id')
    
    if basic == 0 and premium == 0:
        return jsonify({'error': 'Select at least 1 credit'}), 400
    
    if not order_id:
        return jsonify({'error': 'Order ID required'}), 400
    
    amount = (basic * 1.7) + (premium * 20)
    
    tx = Transaction(
        user_id=user.id,
        amount=amount,
        basic_credits=basic,
        premium_credits=premium,
        order_id=order_id,
        status='pending'
    )
    db.session.add(tx)
    db.session.commit()
    
    return jsonify({'success': True, 'transaction_id': tx.id, 'verified': False}), 200

@app.route('/api/client/coupons/create', methods=['POST'])
@api_login_required
def api_create_coupon(user):
    data = request.get_json()
    basic = data.get('basic_credits', 0)
    premium = data.get('premium_credits', 0)
    
    if basic == 0 and premium == 0:
        return jsonify({'error': 'Select credits'}), 400
    
    if basic > user.basic_credits:
        return jsonify({'error': f'Need {basic} basic credits'}), 400
    if premium > user.premium_credits:
        return jsonify({'error': f'Need {premium} premium credits'}), 400
    
    user.basic_credits -= basic
    user.premium_credits -= premium
    
    code = generate_coupon_code()
    while Coupon.query.filter_by(code=code).first():
        code = generate_coupon_code()
    
    coupon = Coupon(
        code=code,
        created_by=user.id,
        basic_credits=basic,
        premium_credits=premium,
        status='active'
    )
    db.session.add(coupon)
    db.session.commit()
    
    return jsonify({'success': True, 'coupon': coupon.to_dict()}), 200

@app.route('/api/client/coupons/redeem', methods=['POST'])
@api_login_required
def api_redeem_coupon(user):
    data = request.get_json()
    code = data.get('code', '').upper().strip()
    
    coupon = Coupon.query.filter_by(code=code, status='active').first()
    if not coupon:
        return jsonify({'error': 'Invalid code'}), 400
    
    user.basic_credits += coupon.basic_credits
    user.premium_credits += coupon.premium_credits
    
    coupon.status = 'used'
    coupon.used_by = user.id
    coupon.used_at = datetime.utcnow()
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Coupon redeemed',
        'basic_credits': coupon.basic_credits,
        'premium_credits': coupon.premium_credits
    }), 200

@app.route('/api/client/coupons/my', methods=['GET'])
@api_login_required
def api_my_coupons(user):
    coupons = Coupon.query.filter_by(created_by=user.id).order_by(Coupon.id.desc()).all()
    return jsonify([c.to_dict() for c in coupons]), 200

# ============================================
# Public API
# ============================================

@app.route('/api/public/regions', methods=['GET'])
def api_get_regions():
    regions = [
        {'id': 'bd', 'region_name': 'Bangladesh', 'tier': 'basic', 'price': 1.7, 'enabled': True},
        {'id': 'in', 'region_name': 'India', 'tier': 'basic', 'price': 1.7, 'enabled': True},
        {'id': 'indo', 'region_name': 'Indonesia', 'tier': 'basic', 'price': 1.7, 'enabled': True},
        {'id': 'br', 'region_name': 'Brazil', 'tier': 'premium', 'price': 20, 'enabled': True},
        {'id': 'me', 'region_name': 'Middle East', 'tier': 'premium', 'price': 20, 'enabled': True},
        {'id': 'ghrab', 'region_name': 'MENA', 'tier': 'premium', 'price': 20, 'enabled': True}
    ]
    return jsonify({'regions': regions}), 200

# ============================================
# Create Admin User if not exists
# ============================================

def create_admin_user():
    admin_email = 'admin@clanboost.com'
    admin_username = 'JUBAYER_ADMIN'
    admin_password = 'Ajayyy!'
    
    admin = User.query.filter_by(email=admin_email).first()
    if not admin:
        admin = User(
            username=admin_username,
            email=admin_email,
            role='admin',
            basic_credits=9999,
            premium_credits=9999,
            max_groups=999
        )
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print(f"✅ Admin user created! Email: {admin_email}, Password: {admin_password}")
    else:
        admin.role = 'admin'
        admin.set_password(admin_password)
        db.session.commit()
        print(f"✅ Admin password synced. Email: {admin_email}, Password: {admin_password}")

# ============================================
# Create Tables and Run
# ============================================

with app.app_context():
    db.create_all()
    create_admin_user()
    print("✅ Database created successfully!")

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)