"""昭德堂健康管理中心业务系统 - Flask主应用"""

import os
import sys
import json
import csv
import io
import secrets
import hashlib
import hmac
import threading
import time as _time
import random
import string
import math
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   jsonify, send_file, Response, abort, session)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import (
    db, User, StoreInfo, Employee, ServiceItem, Medicine, PrescriptionTemplate,
    Customer, PrepaidAccount, Appointment, Registration, MedicalRecord,
    ChargeRecord, RechargeRecord, DeductionRecord, PrepaidRefund,
    InventoryLedger, StockFlow, DailyReport, SystemLog, SystemConfig, StockTake,
    InboundOrder, InboundOrderItem,
    AnnualCardTemplate, CustomerAnnualCard, AnnualCardUsage
)
from database import backup_db, restore_db, list_backups
from upgrade import check_and_upgrade, get_recorded_version


# ==================== 路径工具 ====================

def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_bundle_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_dir()
BUNDLE_DIR = get_bundle_dir()
DB_PATH = os.path.join(APP_DIR, 'clinic.db')
BACKUP_DIR = os.path.join(APP_DIR, 'backups')
SECRET_KEY_FILE = os.path.join(APP_DIR, '.secret_key')
APP_VERSION = '1.6.0'


def _load_or_create_secret():
    """加载或生成SECRET_KEY"""
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'r') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    try:
        # 使用文件描述符创建，设置仅所有者可读写(0o600)
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(key)
    except IOError:
        pass
    return key


# ==================== 应用工厂 ====================

def create_app():
    app = Flask(__name__,
                template_folder=os.path.join(BUNDLE_DIR, 'templates'),
                static_folder=os.path.join(BUNDLE_DIR, 'static'))
    app.config['SECRET_KEY'] = _load_or_create_secret()
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['WTF_CSRF_ENABLED'] = True

    # Jinja2 filters
    app.jinja_env.filters['from_json'] = _from_json
    app.jinja_env.filters['parse_json'] = _parse_json
    app.jinja_env.filters['fmt_decimal'] = _fmt_decimal

    # CSRF token生成与验证
    app.jinja_env.globals['csrf_token'] = _generate_csrf_token
    app.jinja_env.globals['timedelta'] = timedelta

    # 全局上下文：看板token（供侧边栏快捷入口使用）与可定制系统名称（顶栏展示）
    @app.context_processor
    def inject_board_token():
        try:
            cfg = SystemConfig.query.filter_by(key='board_token').first()
            site_cfg = SystemConfig.query.filter_by(key='site_title').first()
            return {
                'board_token': cfg.value if cfg and cfg.value else '',
                'site_title': site_cfg.value if site_cfg and site_cfg.value else '昭德堂健康管理中心',
            }
        except Exception:
            return {'board_token': '', 'site_title': '昭德堂健康管理中心'}

    db.init_app(app)
    login_manager.init_app(app)

    # 安全头
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # 看板页面允许被嵌入iframe（投屏场景）
        if request.path and request.path.startswith('/board/'):
            response.headers['X-Frame-Options'] = 'ALLOWALL'
        else:
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return response

    # 全局错误处理（避免重定向循环：未登录时不跳转dashboard）
    def _safe_redirect(msg, category='warning'):
        """安全重定向：已登录→dashboard，未登录→login（避免循环）"""
        flash(msg, category)
        try:
            if current_user.is_authenticated:
                return redirect(url_for('dashboard'))
        except Exception:
            pass
        return redirect(url_for('login'))

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/api/'):
            return jsonify(error='E0001', message='资源不存在'), 404
        return _safe_redirect('页面不存在', 'warning')

    @app.errorhandler(500)
    def internal_error(e):
        if request.path.startswith('/api/'):
            return jsonify(error='E9999', message='系统内部错误'), 500
        # 500错误返回纯HTML，不渲染模板（避免模板引擎/DB访问再次失败形成循环）
        return ('''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>系统错误</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#f8f9fa}.box{text-align:center;padding:40px;background:
white;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}h2{color:#dc3545}
a{color:#0d6efd;text-decoration:none;padding:10px 20px;border:1px solid #0d6efd;
border-radius:4px;display:inline-block;margin-top:15px}a:hover{background:#0d6efd;color:white}</style>
</head><body><div class="box"><h2>系统内部错误</h2><p>服务暂时不可用，请稍后重试</p>
<a href="/login">返回登录页</a></div></body></html>''', 500, {'Content-Type': 'text/html'})

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith('/api/'):
            return jsonify(error='E0003', message='无权限'), 403
        if request.path.startswith('/board/'):
            desc = getattr(e, 'description', '无权限访问')
            return (f'''<!DOCTYPE html><html><head><meta charset="UTF-8"><title>访问被拒</title>
<style>body{{font-family:'Microsoft YaHei',sans-serif;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#1a1d23;color:#e9ecef}}.box{{text-align:center;padding:60px;background:
#2d3239;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.3)}}h2{{color:#ff6b6b;margin-bottom:8px}}
p{{color:#adb5bd;margin:12px 0}}</style></head>
<body><div class="box"><h2>⛔ 访问被拒</h2><p>{desc}</p>
<p style="font-size:0.85rem;color:#6c757d">请通过系统管理获取有效的看板访问链接</p></div></body></html>''',
                    403, {'Content-Type': 'text/html'})
        return _safe_redirect('您没有权限执行此操作', 'danger')

    return app


login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.login_message = '请先登录系统'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ==================== CSRF ====================

def _generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


def _validate_csrf():
    token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not token or token != session.get('_csrf_token'):
        abort(403)


# ==================== Jinja2过滤器 ====================

def _from_json(s):
    if not s:
        return []
    try:
        result = json.loads(s)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_json(s):
    """解析JSON字符串，返回任意类型（dict/list/str等）"""
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


def _fmt_decimal(val):
    try:
        return f"{Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
    except (InvalidOperation, TypeError, ValueError):
        return '0.00'


# ==================== 辅助函数 ====================

def to_decimal(val, default=Decimal('0')):
    try:
        return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return default


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


def _safe_get(model, pk, label='记录'):
    obj = db.session.get(model, pk)
    if obj is None:
        flash(f'{label}不存在或已被删除', 'warning')
        abort(404)
    return obj


def log_action(action, detail=''):
    log = SystemLog(user_id=current_user.id if current_user.is_authenticated else None,
                    action=action, detail=detail)
    db.session.add(log)


def db_commit(action='', detail=''):
    """安全提交事务，失败时回滚"""
    try:
        if action:
            log_action(action, detail)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        # 记录错误到系统日志（便于排查）
        log_action('事务失败', f'{action}: {str(e)[:200]}')
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash(f'操作失败: {str(e)}', 'danger')
        return False


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if current_user.role not in roles and current_user.role != '管理员':
                flash('您没有权限执行此操作', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ==================== 登录防暴力 ====================

_login_attempts = {}


def _check_login_rate(key):
    now = _time.time()
    attempts = _login_attempts.get(key, [])
    # 清理5分钟前的记录
    attempts = [t for t in attempts if now - t < 300]
    if len(attempts) >= 5:
        return False
    _login_attempts[key] = attempts
    return True


def _record_login_attempt(key):
    _login_attempts.setdefault(key, []).append(_time.time())


# 验证码请求限速：同IP 1分钟内最多请求10次
_captcha_hits: dict[str, list] = {}


def _check_captcha_rate(ip: str) -> bool:
    now = _time.time()
    hits = _captcha_hits.get(ip, [])
    hits = [t for t in hits if now - t < 60]
    if len(hits) >= 10:
        return False
    _captcha_hits[ip] = hits
    return True


def _record_captcha_hit(ip: str):
    _captcha_hits.setdefault(ip, []).append(_time.time())


# ==================== 认证 ====================

app = create_app()


@app.route('/captcha')
def captcha():
    """SVG图形验证码"""
    ip = request.remote_addr or 'unknown'
    if not _check_captcha_rate(ip):
        return Response('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">'
                        '<rect width="120" height="40" fill="#f0f0f0"/>'
                        '<text x="60" y="25" text-anchor="middle" font-size="11" '
                        'font-family="Arial" fill="#c00">请求过于频繁</text></svg>',
                        status=429, mimetype='image/svg+xml',
                        headers={'Cache-Control': 'no-store, no-cache'})
    _record_captcha_hit(ip)

    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789'
    code = ''.join(secrets.choice(chars) for _ in range(4))
    session['captcha_code'] = code

    W, H = 120, 40
    # 随机颜色
    def _clr():
        return f'rgb({random.randint(0,150)},{random.randint(0,150)},{random.randint(0,150)})'

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">',
           f'<rect width="{W}" height="{H}" fill="#f0f0f0"/>']
    # 干扰线
    for _ in range(5):
        x1, y1, x2, y2 = random.randint(0, W), random.randint(0, H), random.randint(0, W), random.randint(0, H)
        svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{_clr()}" stroke-width="1"/>')
    # 干扰点
    for _ in range(20):
        cx, cy = random.randint(0, W), random.randint(0, H)
        svg.append(f'<circle cx="{cx}" cy="{cy}" r="1" fill="{_clr()}"/>')
    # 字符
    for i, c in enumerate(code):
        x = 10 + i * 26
        y = random.randint(22, 30)
        angle = random.randint(-25, 25)
        color = _clr()
        size = random.randint(18, 24)
        svg.append(
            f'<text x="{x}" y="{y}" font-size="{size}" font-family="Arial,sans-serif" '
            f'font-weight="bold" fill="{color}" transform="rotate({angle},{x},{y})">{c}</text>'
        )
    svg.append('</svg>')
    return Response('\n'.join(svg), mimetype='image/svg+xml',
                    headers={'Cache-Control': 'no-store, no-cache'})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 登录页CSRF验证：失败时重新渲染而非abort(403)，避免重定向循环
        token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or token != session.get('_csrf_token'):
            flash('页面已过期，请重新登录（如已清除Cookie请直接刷新页面）', 'warning')
            return render_template('login.html')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        captcha_input = request.form.get('captcha', '').strip()
        # 验证码校验（常量时间比较，防时序侧信道攻击）
        expected = session.pop('captcha_code', None)
        if not captcha_input or not expected or \
           not hmac.compare_digest(captcha_input.lower(), expected.lower()):
            flash('验证码错误', 'warning')
            return render_template('login.html')
        if not username or not password:
            flash('请输入用户名和密码', 'warning')
            return render_template('login.html')
        # IP级别限速 + 用户名级别限速（双重防护）
        ip = request.remote_addr or 'unknown'
        if not _check_login_rate(ip) or not _check_login_rate(username):
            flash('登录尝试过于频繁，请5分钟后重试', 'danger')
            return render_template('login.html')
        user = User.query.filter_by(username=username, is_active_user=True).first()
        if user and user.check_password(password):
            login_user(user)
            _login_attempts.pop(username, None)
            _login_attempts.pop(ip, None)
            return redirect(url_for('dashboard'))
        _record_login_attempt(username)
        _record_login_attempt(ip)
        flash('用户名或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ==================== 仪表盘 ====================

@app.route('/')
@login_required
def dashboard():
    today = date.today()
    today_regs = Registration.query.filter(db.func.date(Registration.reg_time) == today).count()
    today_charges = ChargeRecord.query.filter(
        db.func.date(ChargeRecord.created_at) == today,
        ChargeRecord.status == '已支付'
    ).all()
    today_revenue = sum(((c.final_amount or 0) for c in today_charges), Decimal('0'))
    today_appts = Appointment.query.filter_by(appt_date=today).filter(
        Appointment.status.in_(['待确认', '已确认'])
    ).order_by(Appointment.time_slot).all()
    recent_charges = ChargeRecord.query.order_by(ChargeRecord.created_at.desc()).limit(10).all()

    # 今日预约提醒
    appt_count = Appointment.query.filter_by(appt_date=today).filter(
        Appointment.status.in_(['已确认'])
    ).count()

    # 库存预警（优化：合并查询）
    alert_meds = _get_stock_alerts()

    # 今日充值统计
    today_recharges = RechargeRecord.query.filter(
        db.func.date(RechargeRecord.created_at) == today
    ).all()
    today_recharge_total = sum(((r.amount or 0) for r in today_recharges), Decimal('0'))

    return render_template('dashboard.html',
                           today_regs=today_regs, today_revenue=today_revenue,
                           today_appts=today_appts, recent_charges=recent_charges,
                           alert_meds=alert_meds, appt_count=appt_count,
                           today_recharge_total=today_recharge_total)


def _get_stock_alerts():
    """获取库存预警列表（优化查询）"""
    meds = Medicine.query.filter_by(is_active=True).all()
    mids = [m.id for m in meds]
    stock_map = _batch_stock_query(mids)
    alert_meds = []
    for m in meds:
        total = stock_map.get(m.id, 0)
        if total <= (m.alert_threshold or 0):
            alert_meds.append({'name': m.name, 'stock': total, 'threshold': m.alert_threshold})
    return alert_meds


# ==================== 客户管理 ====================

@app.route('/customers')
@login_required
def customers():
    keyword = request.args.get('keyword', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20
    q = Customer.query.filter_by(is_deleted=False)
    if keyword:
        q = q.filter(db.or_(
            Customer.name.contains(keyword),
            Customer.phone.contains(keyword),
            Customer.id_card.contains(keyword)
        ))
    pagination = q.order_by(Customer.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('customers.html', customers=pagination.items,
                           keyword=keyword, pagination=pagination)


@app.route('/customer/add', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def customer_add():
    _validate_csrf()
    name = request.form.get('name', '').strip()
    if not name:
        flash('客户姓名不能为空', 'warning')
        return redirect(url_for('customers'))
    try:
        c = Customer(
            name=name, gender=request.form.get('gender'),
            birthday=_parse_date(request.form.get('birthday')),
            phone=request.form.get('phone'), id_card=request.form.get('id_card'),
            address=request.form.get('address'),
            constitution_type=request.form.get('constitution_type'),
            allergy_history=request.form.get('allergy_history'),
            medical_history=request.form.get('medical_history'),
            referrer=request.form.get('referrer'),
            member_level=request.form.get('member_level', '普通'),
            member_discount=to_decimal(request.form.get('member_discount', '1.00'))
        )
        db.session.add(c)
        db.session.flush()
        acct = PrepaidAccount(customer_id=c.id, balance=0, total_recharge=0, total_consumed=0)
        db.session.add(acct)
        db_commit('新建客户', f'客户: {c.name}')
        flash('客户建档成功', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'建档失败: {str(e)}', 'danger')
    return redirect(url_for('customers'))


@app.route('/customer/<int:cid>')
@login_required
def customer_detail(cid):
    c = _safe_get(Customer, cid, '客户')
    acct = PrepaidAccount.query.filter_by(customer_id=cid).first()
    regs = Registration.query.filter_by(customer_id=cid).order_by(Registration.reg_time.desc()).limit(20).all()
    recharges = RechargeRecord.query.filter_by(customer_id=cid).order_by(RechargeRecord.created_at.desc()).limit(20).all()
    charges = ChargeRecord.query.filter_by(customer_id=cid).order_by(ChargeRecord.created_at.desc()).limit(20).all()
    medicals = MedicalRecord.query.join(Registration).filter(Registration.customer_id == cid).order_by(
        MedicalRecord.created_at.desc()).limit(10).all()
    # 年卡信息
    annual_cards = CustomerAnnualCard.query.filter_by(customer_id=cid).order_by(
        CustomerAnnualCard.created_at.desc()).all()
    for ac in annual_cards:
        _check_card_expired(ac)
    return render_template('customer_detail.html', customer=c, account=acct,
                           registrations=regs, recharges=recharges, charges=charges,
                           medicals=medicals, annual_cards=annual_cards)


@app.route('/customer/<int:cid>/edit', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def customer_edit(cid):
    _validate_csrf()
    c = _safe_get(Customer, cid, '客户')
    try:
        for field in ['name', 'gender', 'phone', 'id_card', 'address', 'constitution_type',
                      'allergy_history', 'medical_history', 'referrer', 'member_level']:
            val = request.form.get(field)
            if val is not None:
                setattr(c, field, val)
        c.birthday = _parse_date(request.form.get('birthday'))
        c.member_discount = to_decimal(request.form.get('member_discount', '1.00'))
        db_commit('编辑客户', f'客户: {c.name}')
        flash('客户信息已更新', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'更新失败: {str(e)}', 'danger')
    return redirect(url_for('customer_detail', cid=cid))


@app.route('/customer/<int:cid>/delete', methods=['POST'])
@login_required
@role_required('管理员')
def customer_delete(cid):
    _validate_csrf()
    c = _safe_get(Customer, cid, '客户')
    c.is_deleted = True
    db_commit('删除客户', f'客户: {c.name}')
    flash(f'客户 {c.name} 已删除（软删除）', 'warning')
    return redirect(url_for('customers'))


@app.route('/customer/<int:cid>/freeze', methods=['POST'])
@login_required
@role_required('管理员')
def customer_freeze(cid):
    _validate_csrf()
    c = _safe_get(Customer, cid, '客户')
    c.is_frozen = not c.is_frozen
    status = '冻结' if c.is_frozen else '解冻'
    db_commit(f'{status}客户', f'客户: {c.name}')
    flash(f'客户 {c.name} 已{status}', 'info')
    return redirect(url_for('customers'))


# ==================== 预约管理 ====================

def _get_time_slots():
    """从系统配置获取预约时段列表"""
    cfg = SystemConfig.query.filter_by(key='time_slots').first()
    if cfg and cfg.value:
        return [s.strip() for s in cfg.value.split(',') if s.strip()]
    return ['08:00-09:00', '09:00-10:00', '10:00-11:00', '11:00-12:00',
            '14:00-15:00', '15:00-16:00', '16:00-17:00', '17:00-18:00']


@app.route('/appointments')
@login_required
def appointments():
    view_date = _parse_date(request.args.get('date')) or date.today()
    end_date = _parse_date(request.args.get('end_date')) or view_date
    appts = Appointment.query.filter(
        Appointment.appt_date >= view_date,
        Appointment.appt_date <= end_date
    ).order_by(Appointment.appt_date, Appointment.time_slot).all()
    employees = Employee.query.filter_by(is_active=True, role='理疗师').all()
    items = ServiceItem.query.filter_by(is_active=True).all()
    custs = Customer.query.filter_by(is_deleted=False).order_by(Customer.name).all()
    time_slots = _get_time_slots()

    # 按日期+时段分组，用于卡片展示
    grouped = {}
    for a in appts:
        key = (a.appt_date.isoformat(), a.time_slot or '未分配')
        grouped.setdefault(key, []).append(a)

    return render_template('appointments.html', appointments=appts, view_date=view_date,
                           end_date=end_date, employees=employees, items=items,
                           customers=custs, time_slots=time_slots, grouped=grouped)


@app.route('/appointment/add', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def appointment_add():
    _validate_csrf()
    try:
        cust_id = int(request.form['customer_id'])
        emp_id = int(request.form['employee_id']) if request.form.get('employee_id') else None
        appt_date = _parse_date(request.form['appt_date'])
        time_slot = request.form.get('time_slot')
        item_id = int(request.form['service_item_id']) if request.form.get('service_item_id') else None

        if not appt_date:
            flash('请选择有效的预约日期', 'warning')
            return redirect(url_for('appointments'))

        # 冻结客户检查
        cust = db.session.get(Customer, cust_id)
        if not cust:
            flash('客户不存在', 'warning')
            return redirect(url_for('appointments'))
        if cust.is_frozen:
            flash(f'客户 {cust.name} 已被冻结，无法预约', 'danger')
            return redirect(url_for('appointments'))

        # 预约冲突检查
        if emp_id and time_slot:
            conflict = Appointment.query.filter_by(
                employee_id=emp_id, appt_date=appt_date, time_slot=time_slot
            ).filter(Appointment.status.in_(['待确认', '已确认'])).first()
            if conflict:
                flash(f'该理疗师在 {time_slot} 已有预约，存在冲突', 'danger')
                return redirect(url_for('appointments', date=appt_date))

        a = Appointment(
            customer_id=cust_id, employee_id=emp_id,
            appt_date=appt_date, time_slot=time_slot,
            service_item_id=item_id,
            source=request.form.get('source', '到店'),
            status='待确认',
            remark=request.form.get('remark')
        )
        db.session.add(a)
        db_commit('新建预约', f'客户ID={a.customer_id}, 日期={a.appt_date}')
        flash('预约登记成功', 'success')
        return redirect(url_for('appointments', date=a.appt_date))
    except (ValueError, KeyError) as e:
        flash(f'预约信息不完整: {str(e)}', 'warning')
        return redirect(url_for('appointments'))


@app.route('/appointment/<int:aid>/confirm', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def appointment_confirm(aid):
    _validate_csrf()
    a = _safe_get(Appointment, aid, '预约')
    if a.status == '待确认':
        a.status = '已确认'
        # 取消扣费配置
        a.cancel_fee_enabled = request.form.get('cancel_fee_enabled') == '1'
        a.cancel_fee_ratio = to_decimal(request.form.get('cancel_fee_ratio', '0'))
        db_commit('确认预约', f'预约ID={aid}, 扣费={a.cancel_fee_enabled}({a.cancel_fee_ratio}%)')
        flash('预约已确认', 'success')
    return redirect(url_for('appointments', date=a.appt_date))


@app.route('/appointment/<int:aid>/checkin', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def appointment_checkin(aid):
    _validate_csrf()
    a = _safe_get(Appointment, aid, '预约')
    if a.status == '已签到':
        flash('该预约已签到，请勿重复操作', 'warning')
        return redirect(url_for('appointments', date=a.appt_date))
    a.status = '已签到'
    reg = Registration(customer_id=a.customer_id, employee_id=a.employee_id,
                       visit_type='复诊', status='待诊', appointment_id=a.id)
    db.session.add(reg)
    db_commit('预约签到', f'预约ID={aid}')
    flash('签到成功，已生成挂号记录', 'success')
    return redirect(url_for('appointments', date=a.appt_date))


@app.route('/appointment/<int:aid>/visit', methods=['POST'])
@login_required
@role_required('管理员', '前台', '理疗师')
def appointment_visit(aid):
    """预约直接就诊：创建挂号→跳转病历填写"""
    _validate_csrf()
    a = _safe_get(Appointment, aid, '预约')
    if a.status in ['已签到', '已完成', '已取消']:
        flash(f'预约状态为 {a.status}，无法就诊', 'warning')
        return redirect(url_for('appointments', date=a.appt_date))
    # 更新预约状态
    a.status = '就诊中'
    # 创建挂号记录
    reg = Registration(customer_id=a.customer_id, employee_id=a.employee_id,
                       visit_type='复诊', status='接诊中', appointment_id=a.id)
    db.session.add(reg)
    db.session.flush()  # 获取reg.id
    db_commit('预约就诊', f'预约ID={aid}, 挂号ID={reg.id}')
    flash(f'已为客户创建就诊记录，请填写病历', 'success')
    return redirect(url_for('medical_record', rid=reg.id))


@app.route('/appointment/<int:aid>/cancel', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def appointment_cancel(aid):
    _validate_csrf()
    a = _safe_get(Appointment, aid, '预约')
    old_status = a.status
    a.status = '已取消'

    # 已确认的预约取消时，按扣费规则生成收费单
    charge_amount = Decimal('0')
    if old_status == '已确认' and a.cancel_fee_enabled and a.cancel_fee_ratio > 0:
        if a.service_item:
            base_price = a.service_item.price or Decimal('0')
            charge_amount = base_price * a.cancel_fee_ratio / Decimal('100')
            charge_amount = charge_amount.quantize(Decimal('0.01'))

    if charge_amount > 0:
        items = [{'name': f'{a.service_item.name} (取消扣费{int(a.cancel_fee_ratio)}%)',
                  'qty': 1, 'price': float(charge_amount), 'subtotal': float(charge_amount)}]
        charge = ChargeRecord(
            customer_id=a.customer_id, items_json=json.dumps(items, ensure_ascii=False),
            total_amount=charge_amount, discount_amount=0, final_amount=charge_amount,
            status='待支付', operator_id=current_user.id
        )
        db.session.add(charge)
        db_commit('取消预约(扣费)', f'预约ID={aid}, 扣费金额=¥{charge_amount}')
        flash(f'预约已取消，已生成收费单 ¥{charge_amount}', 'warning')
    else:
        db_commit('取消预约', f'预约ID={aid}')
        flash('预约已取消', 'warning')
    return redirect(url_for('appointments', date=a.appt_date))


# ==================== 挂号与接诊 ====================

@app.route('/registrations')
@login_required
def registrations():
    today = date.today()
    regs = Registration.query.filter(db.func.date(Registration.reg_time) == today).order_by(
        Registration.reg_time.desc()).all()
    employees = Employee.query.filter_by(is_active=True, role='理疗师').all()
    custs = Customer.query.filter_by(is_deleted=False).order_by(Customer.name).all()
    return render_template('registrations.html', registrations=regs, employees=employees, customers=custs)


@app.route('/registration/add', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def registration_add():
    _validate_csrf()
    try:
        cust_id = int(request.form['customer_id'])
        emp_id = int(request.form['employee_id']) if request.form.get('employee_id') else None
        # 冻结客户检查
        cust = db.session.get(Customer, cust_id)
        if cust and cust.is_frozen:
            flash(f'客户 {cust.name} 已被冻结，无法挂号', 'danger')
            return redirect(url_for('registrations'))
        reg = Registration(
            customer_id=cust_id, employee_id=emp_id,
            visit_type=request.form.get('visit_type', '初诊'),
            status='待诊'
        )
        db.session.add(reg)
        db_commit('现场挂号', f'客户ID={reg.customer_id}')
        flash('挂号成功', 'success')
    except (ValueError, KeyError) as e:
        flash(f'挂号信息不完整: {str(e)}', 'warning')
    return redirect(url_for('registrations'))


@app.route('/registration/<int:rid>/medical', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '理疗师')
def medical_record(rid):
    reg = _safe_get(Registration, rid, '挂号记录')
    if request.method == 'POST':
        _validate_csrf()
        try:
            mr = MedicalRecord.query.filter_by(reg_id=rid).first()
            if not mr:
                mr = MedicalRecord(reg_id=rid)
                db.session.add(mr)
            mr.chief_complaint = request.form.get('chief_complaint')
            mr.present_illness = request.form.get('present_illness')
            mr.past_history = request.form.get('past_history')
            mr.four_exam = request.form.get('four_exam')
            mr.syndrome = request.form.get('syndrome')
            mr.treatment_plan = request.form.get('treatment_plan')
            items_data = request.form.get('items_json', '[]')
            mr.prescription_items_json = items_data
            meds_data = request.form.get('meds_json', '[]')
            mr.prescription_meds_json = meds_data
            reg.status = '接诊中'
            db_commit('病历记录', f'挂号ID={rid}')
            flash('病历保存成功', 'success')
            return redirect(url_for('registrations'))
        except Exception as e:
            db.session.rollback()
            flash(f'病历保存失败: {str(e)}', 'danger')

    mr = MedicalRecord.query.filter_by(reg_id=rid).first()
    items = ServiceItem.query.filter_by(is_active=True).all()
    meds = Medicine.query.filter_by(is_active=True).all()
    templates = PrescriptionTemplate.query.all()
    return render_template('medical.html', registration=reg, record=mr,
                           items=items, medicines=meds, templates=templates)


@app.route('/registration/<int:rid>/charge', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '前台')
def charge_register(rid):
    reg = _safe_get(Registration, rid, '挂号记录')
    if request.method == 'POST':
        _validate_csrf()
        try:
            items_json = request.form.get('items_json', '[]')
            items = json.loads(items_json)

            # 服务端重算金额（防止前端篡改）
            total = Decimal('0')
            for it in items:
                qty = int(it.get('qty', 1))
                price = to_decimal(it.get('price', 0))
                it['subtotal'] = float(price * qty)
                total += price * qty

            discount = to_decimal(request.form.get('discount_amount', '0'))

            # --- 年卡抵扣处理 ---
            annual_card_id = request.form.get('annual_card_id', type=int)
            card_deduct_amount = Decimal('0')
            card_deduct_items = []  # [{item_id, item_name, price}]
            if annual_card_id:
                card = CustomerAnnualCard.query.get(annual_card_id)
                if card and card.customer_id == reg.customer_id and card.status == '正常':
                    _check_card_expired(card)
                if card and card.status == '正常':
                    remaining = json.loads(card.remaining_times_json or '{}')
                    for it in items:
                        sid = str(it.get('service_item_id', ''))
                        if sid in remaining and remaining[sid] > 0:
                            times_to_use = min(int(it.get('qty', 1)), remaining[sid])
                            if times_to_use > 0:
                                price_per = to_decimal(it.get('price', 0))
                                deduct_val = price_per * times_to_use
                                card_deduct_amount += deduct_val
                                card_deduct_items.append({
                                    'item_id': int(sid),
                                    'item_name': it.get('name', ''),
                                    'price': float(price_per),
                                    'times': times_to_use
                                })

            # 最终金额 = 总计 - 折扣 - 年卡抵扣
            final = total - discount - card_deduct_amount
            if final < 0:
                final = Decimal('0')

            payments_json = request.form.get('payments_json', '[]')
            payments = json.loads(payments_json)

            # 如果有年卡抵扣，加入支付明细
            if card_deduct_amount > 0:
                payments.append({
                    'method': '年卡抵扣',
                    'amount': float(card_deduct_amount),
                    'card_id': annual_card_id
                })

            # 校验支付金额（含年卡抵扣）
            pay_total = sum(to_decimal(p.get('amount', 0)) for p in payments)
            if pay_total < final - Decimal('0.01'):
                flash(f'支付金额不足，应付{final}元，已付{pay_total}元', 'danger')
                return redirect(url_for('charge_register', rid=rid))

            charge = ChargeRecord(
                customer_id=reg.customer_id, reg_id=rid,
                items_json=json.dumps(items, ensure_ascii=False),
                total_amount=total,
                discount_amount=discount + card_deduct_amount,
                final_amount=final,
                payments_json=json.dumps(payments, ensure_ascii=False),
                status='已支付',
                operator_id=current_user.id
            )
            db.session.add(charge)
            db.session.flush()

            # 处理年卡核销
            if card_deduct_amount > 0 and annual_card_id:
                card = CustomerAnnualCard.query.get(annual_card_id)
                remaining = json.loads(card.remaining_times_json or '{}')
                for ci in card_deduct_items:
                    sid = str(ci['item_id'])
                    times_used = ci['times']
                    remaining[sid] = remaining.get(sid, 0) - times_used
                    if remaining[sid] < 0:
                        remaining[sid] = 0
                    usage = AnnualCardUsage(
                        card_id=card.id,
                        customer_id=reg.customer_id,
                        service_item_id=ci['item_id'],
                        charge_id=charge.id,
                        used_times=times_used,
                        remaining_after=remaining[sid],
                        operator_id=current_user.id
                    )
                    db.session.add(usage)
                card.remaining_times_json = json.dumps(remaining)

            # 处理预充值扣费（含余额校验）
            for p in payments:
                if p.get('method') == '预充值' and p.get('amount', 0) > 0:
                    acct = PrepaidAccount.query.filter_by(customer_id=reg.customer_id).first()
                    if not acct:
                        flash('预充值账户不存在', 'danger')
                        db.session.rollback()
                        return redirect(url_for('charge_register', rid=rid))
                    deduct_amt = to_decimal(p['amount'])
                    if deduct_amt > acct.balance:
                        flash(f'预充值余额不足，当前余额{acct.balance}元，需扣{deduct_amt}元', 'danger')
                        db.session.rollback()
                        return redirect(url_for('charge_register', rid=rid))
                    # 乐观锁扣费
                    old_version = acct.version
                    rows_updated = PrepaidAccount.query.filter_by(
                        customer_id=reg.customer_id, version=old_version
                    ).update({
                        'balance': acct.balance - deduct_amt,
                        'total_consumed': acct.total_consumed + deduct_amt,
                        'version': old_version + 1
                    })
                    if rows_updated == 0:
                        flash('预充值账户已被并发操作，请重试', 'danger')
                        db.session.rollback()
                        return redirect(url_for('charge_register', rid=rid))
                    db.session.expire(acct)
                    acct = PrepaidAccount.query.filter_by(customer_id=reg.customer_id).first()
                    ded = DeductionRecord(
                        customer_id=reg.customer_id, charge_id=charge.id,
                        amount=deduct_amt, deduction_type='金额',
                        balance_after=acct.balance
                    )
                    db.session.add(ded)

            # 更新挂号状态为已收费（等待签字确认后变为已完成）
            reg.status = '已收费'

            # 更新关联预约状态
            if reg.appointment_id:
                appt = db.session.get(Appointment, reg.appointment_id)
                if appt and appt.status in ('已签到', '已确认'):
                    appt.status = '已完成'

            card_msg = f', 年卡抵扣¥{card_deduct_amount}' if card_deduct_amount > 0 else ''
            db_commit('收费', f'挂号ID={rid}, 金额={final}{card_msg}')
            flash(f'收费成功{card_msg}，请引导客户签字确认', 'success')
            return redirect(url_for('registrations'))
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            db.session.rollback()
            flash(f'收费处理失败: {str(e)}', 'danger')
            return redirect(url_for('charge_register', rid=rid))

    mr = MedicalRecord.query.filter_by(reg_id=rid).first()
    acct = PrepaidAccount.query.filter_by(customer_id=reg.customer_id).first()
    # 查询客户有效年卡
    active_cards = CustomerAnnualCard.query.filter_by(
        customer_id=reg.customer_id, status='正常'
    ).all()
    # 过滤掉已过期的
    valid_cards = []
    for c in active_cards:
        if not _check_card_expired(c):
            valid_cards.append(c)
    return render_template('charge.html', registration=reg, medical=mr,
                           account=acct, annual_cards=valid_cards)


# ==================== 打印功能 ====================

@app.route('/print/prescription/<int:rid>')
@login_required
def print_prescription(rid):
    """打印处方单（调用本机默认打印机）"""
    reg = _safe_get(Registration, rid, '挂号记录')
    mr = MedicalRecord.query.filter_by(reg_id=rid).first()
    if not mr:
        flash('尚未记录病历，无法打印处方', 'warning')
        return redirect(url_for('medical_record', rid=rid))
    store = StoreInfo.query.first()
    return render_template('print_prescription.html',
                           registration=reg, record=mr, store=store)


@app.route('/print/receipt/<int:rid>')
@login_required
def print_receipt(rid):
    """打印收费小票（调用本机默认打印机）"""
    reg = _safe_get(Registration, rid, '挂号记录')
    charge = ChargeRecord.query.filter_by(reg_id=rid).first()
    if not charge:
        flash('尚未收费，无法打印小票', 'warning')
        return redirect(url_for('charge_register', rid=rid))
    store = StoreInfo.query.first()
    return render_template('print_receipt.html',
                           registration=reg, charge=charge, store=store)


# ==================== 执行完成确认（签字+评价） ====================

@app.route('/registration/<int:rid>/confirm', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '前台', '理疗师')
def confirm_treatment(rid):
    """治疗完成确认：签字 + 服务评价"""
    reg = _safe_get(Registration, rid, '挂号记录')
    if reg.status not in ('已收费', '接诊中'):
        flash(f'当前状态({reg.status})不可执行确认操作', 'warning')
        return redirect(url_for('registrations'))

    if request.method == 'POST':
        _validate_csrf()
        try:
            # 接收签字图片（Base64）
            sig_data = request.form.get('signature_data', '')
            if sig_data:
                reg.signature_image = sig_data
            # 接收图片上传（纸质签名扫描）
            sig_file = request.files.get('signature_file')
            if sig_file and sig_file.filename:
                import base64
                img_data = sig_file.read()
                # 限制2MB
                if len(img_data) > 2 * 1024 * 1024:
                    flash('签字图片不能超过2MB', 'warning')
                    return redirect(url_for('confirm_treatment', rid=rid))
                reg.signature_image = 'data:image/png;base64,' + base64.b64encode(img_data).decode()
            # 满意度评价
            rating = request.form.get('satisfaction_rating', type=int)
            comment = request.form.get('satisfaction_comment', '').strip()
            reg.satisfaction_rating = rating
            reg.satisfaction_comment = comment
            reg.confirmed_at = datetime.now()
            reg.confirmed_by = current_user.id
            reg.status = '已完成'

            # 更新关联预约状态
            if reg.appointment_id:
                appt = db.session.get(Appointment, reg.appointment_id)
                if appt and appt.status not in ('已完成', '已取消'):
                    appt.status = '已完成'

            db_commit('治疗确认', f'挂号ID={rid}, 评分={rating}')
            flash('治疗确认完成，感谢客户反馈', 'success')
            return redirect(url_for('registrations'))
        except Exception as e:
            db.session.rollback()
            flash(f'确认失败: {str(e)}', 'danger')

    charge = ChargeRecord.query.filter_by(reg_id=rid).first()
    mr = MedicalRecord.query.filter_by(reg_id=rid).first()
    return render_template('confirm_treatment.html',
                           registration=reg, charge=charge, record=mr)


# ==================== 收费退款 ====================

@app.route('/charge/<int:charge_id>/refund', methods=['POST'])
@login_required
@role_required('管理员')
def charge_refund(charge_id):
    _validate_csrf()
    charge = _safe_get(ChargeRecord, charge_id, '收费记录')
    if charge.status != '已支付':
        flash('仅已支付的记录可退款', 'warning')
        return redirect(url_for('charges'))

    reason = request.form.get('reason', '')
    refund_amt = to_decimal(request.form.get('refund_amount', str(charge.final_amount)))
    if refund_amt <= 0 or refund_amt > charge.final_amount:
        flash('退款金额无效', 'danger')
        return redirect(url_for('charges'))

    try:
        charge.status = '已退款' if refund_amt >= charge.final_amount else '部分退款'
        charge.refund_amount = refund_amt
        charge.refund_reason = reason

        # 退还预充值扣费
        deductions = DeductionRecord.query.filter_by(charge_id=charge_id).all()
        for ded in deductions:
            acct = PrepaidAccount.query.filter_by(customer_id=charge.customer_id).first()
            if acct:
                acct.balance += ded.amount
                acct.total_consumed -= ded.amount
                acct.version += 1

        db_commit('收费退款', f'收费ID={charge_id}, 金额={refund_amt}')
        flash(f'退款成功，退款{refund_amt}元', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'退款失败: {str(e)}', 'danger')
    return redirect(url_for('charges'))


# ==================== 收费记录 ====================

@app.route('/charges')
@login_required
def charges():
    start = _parse_date(request.args.get('start')) or (date.today() - timedelta(days=7))
    end = _parse_date(request.args.get('end')) or date.today()
    page = request.args.get('page', 1, type=int)
    recs = ChargeRecord.query.filter(
        db.func.date(ChargeRecord.created_at) >= start,
        db.func.date(ChargeRecord.created_at) <= end
    ).order_by(ChargeRecord.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template('charges.html', records=recs.items, start=start, end=end, pagination=recs)


# ==================== 预充值管理 ====================

@app.route('/prepaid')
@login_required
def prepaid():
    keyword = request.args.get('keyword', '').strip()
    q = db.session.query(Customer, PrepaidAccount).join(
        PrepaidAccount, Customer.id == PrepaidAccount.customer_id)
    if keyword:
        q = q.filter(db.or_(Customer.name.contains(keyword), Customer.phone.contains(keyword)))
    rows = q.all()
    packages = SystemConfig.query.filter_by(key='recharge_packages').first()
    gift_rule = SystemConfig.query.filter_by(key='recharge_gift_rule').first()
    try:
        pkg_list = [int(x.strip()) for x in (packages.value or '500,1000,2000').split(',') if x.strip()]
    except (ValueError, AttributeError):
        pkg_list = [500, 1000, 2000]
    gift_map = {}
    if gift_rule and gift_rule.value:
        for pair in gift_rule.value.split(','):
            parts = pair.strip().split(':')
            if len(parts) == 2:
                try:
                    gift_map[int(parts[0].strip())] = int(parts[1].strip())
                except ValueError:
                    continue
    return render_template('prepaid.html', rows=rows, keyword=keyword,
                           packages=pkg_list, gift_map=gift_map)


@app.route('/prepaid/recharge', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def prepaid_recharge():
    _validate_csrf()
    try:
        cid = int(request.form['customer_id'])
        amount = to_decimal(request.form['amount'])
        if amount <= 0:
            flash('充值金额必须大于0', 'warning')
            return redirect(url_for('prepaid'))
        gift = to_decimal(request.form.get('gift_amount', '0'))
        method = request.form.get('payment_method', '现金')
        pkg_name = request.form.get('package_name', '')

        acct = PrepaidAccount.query.filter_by(customer_id=cid).first()
        if not acct:
            flash('客户预充值账户不存在', 'danger')
            return redirect(url_for('prepaid'))

        acct.balance += amount + gift
        acct.total_recharge += amount
        acct.total_gift += gift

        rec = RechargeRecord(customer_id=cid, amount=amount, gift_amount=gift,
                             payment_method=method, package_name=pkg_name,
                             operator_id=current_user.id)
        db.session.add(rec)
        db_commit('充值', f'客户ID={cid}, 金额={amount}, 赠送={gift}')
        flash(f'充值成功！充值{amount}元，赠送{gift}元，当前余额{acct.balance}元', 'success')
    except (ValueError, KeyError) as e:
        flash(f'充值失败: {str(e)}', 'danger')
    return redirect(url_for('prepaid'))


@app.route('/prepaid/refund', methods=['POST'])
@login_required
@role_required('管理员')
def prepaid_refund():
    _validate_csrf()
    try:
        cid = int(request.form['customer_id'])
        amount = to_decimal(request.form['amount'])
        reason = request.form.get('reason', '')
        acct = PrepaidAccount.query.filter_by(customer_id=cid).first()
        if not acct:
            flash('预充值账户不存在', 'danger')
            return redirect(url_for('prepaid'))
        if amount <= 0:
            flash('退款金额必须大于0', 'warning')
            return redirect(url_for('prepaid'))
        if amount > acct.balance:
            flash('退款金额不能超过账户余额', 'danger')
            return redirect(url_for('prepaid'))
        acct.balance -= amount
        ref = PrepaidRefund(customer_id=cid, amount=amount, reason=reason,
                            approver_id=current_user.id, operator_id=current_user.id)
        db.session.add(ref)
        db_commit('充值退款', f'客户ID={cid}, 金额={amount}')
        flash(f'退款成功！退款{amount}元，剩余余额{acct.balance}元', 'success')
    except (ValueError, KeyError) as e:
        flash(f'退款失败: {str(e)}', 'danger')
    return redirect(url_for('prepaid'))


# ==================== 库存管理 ====================

def _batch_stock_query(medicine_ids):
    """批量查询多个药品的库存总量，返回 {medicine_id: total_qty}"""
    if not medicine_ids:
        return {}
    rows = db.session.query(
        InventoryLedger.medicine_id,
        db.func.coalesce(db.func.sum(InventoryLedger.current_qty), 0)
    ).filter(InventoryLedger.medicine_id.in_(medicine_ids)
    ).group_by(InventoryLedger.medicine_id).all()
    return {mid: qty for mid, qty in rows}


# ==================== 年卡管理 ====================

def _check_card_expired(card):
    """检查年卡是否过期，过期则自动更新状态"""
    if card.status == '正常' and card.expire_date and card.expire_date < date.today():
        card.status = '已过期'
        db.session.commit()
        return True
    return False


@app.route('/annual_cards')
@login_required
def annual_cards():
    """年卡列表（所有客户的年卡）"""
    keyword = request.args.get('keyword', '').strip()
    status_filter = request.args.get('status', '')
    query = CustomerAnnualCard.query.join(Customer).join(AnnualCardTemplate)
    if keyword:
        query = query.filter(
            db.or_(Customer.name.contains(keyword), Customer.phone.contains(keyword))
        )
    if status_filter:
        query = query.filter(CustomerAnnualCard.status == status_filter)
    # 自动检查过期
    for card in query.all():
        _check_card_expired(card)
    cards = CustomerAnnualCard.query.join(Customer).join(AnnualCardTemplate)
    if keyword:
        cards = cards.filter(
            db.or_(Customer.name.contains(keyword), Customer.phone.contains(keyword))
        )
    if status_filter:
        cards = cards.filter(CustomerAnnualCard.status == status_filter)
    cards = cards.order_by(CustomerAnnualCard.created_at.desc()).all()
    templates = AnnualCardTemplate.query.filter_by(is_active=True).all()
    customers = Customer.query.filter_by(is_deleted=False, is_frozen=False).order_by(Customer.name).all()
    return render_template('annual_cards.html', cards=cards, templates=templates,
                           customers=customers, keyword=keyword, status_filter=status_filter)


@app.route('/annual_card/purchase', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def annual_card_purchase():
    """为客户开通年卡"""
    _validate_csrf()
    customer_id = request.form.get('customer_id', type=int)
    template_id = request.form.get('template_id', type=int)
    purchase_price = to_decimal(request.form.get('purchase_price', '0'))
    remark = request.form.get('remark', '')

    customer = _safe_get(Customer, customer_id, '客户')
    template = _safe_get(AnnualCardTemplate, template_id, '年卡模板')

    # 计算到期日期
    valid_days = template.valid_days or 365
    expire = date.today() + timedelta(days=valid_days)

    # 初始化剩余次数
    services = json.loads(template.services_json or '[]')
    remaining = {}
    for svc in services:
        remaining[str(svc['service_item_id'])] = svc.get('total_times', 0)

    card = CustomerAnnualCard(
        customer_id=customer_id, template_id=template_id,
        purchase_date=date.today(), expire_date=expire,
        purchase_price=purchase_price or template.price,
        remaining_times_json=json.dumps(remaining),
        remark=remark, operator_id=current_user.id
    )
    db.session.add(card)
    db_commit('开通年卡', f'客户={customer.name}, 模板={template.name}, 价格=¥{card.purchase_price}')
    flash(f'已为客户 {customer.name} 开通年卡「{template.name}」，有效期至 {expire}', 'success')
    return redirect(url_for('annual_cards'))


@app.route('/annual_card/<int:card_id>/freeze', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def annual_card_freeze(card_id):
    """冻结/解冻年卡"""
    _validate_csrf()
    card = _safe_get(CustomerAnnualCard, card_id, '年卡')
    if card.status == '正常':
        card.status = '已冻结'
        db_commit('冻结年卡', f'年卡ID={card_id}')
        flash('年卡已冻结', 'warning')
    elif card.status == '已冻结':
        card.status = '正常'
        db_commit('解冻年卡', f'年卡ID={card_id}')
        flash('年卡已解冻', 'success')
    else:
        flash(f'状态为 {card.status}，无法操作', 'danger')
    return redirect(url_for('annual_cards'))


@app.route('/annual_card/<int:card_id>/renew', methods=['POST'])
@login_required
@role_required('管理员', '前台')
def annual_card_renew(card_id):
    """续费年卡：延长有效期并重置次数"""
    _validate_csrf()
    card = _safe_get(CustomerAnnualCard, card_id, '年卡')
    template = card.template

    # 延长有效期
    base_date = max(card.expire_date, date.today())
    card.expire_date = base_date + timedelta(days=template.valid_days or 365)

    # 重置剩余次数
    services = json.loads(template.services_json or '[]')
    remaining = {}
    for svc in services:
        remaining[str(svc['service_item_id'])] = svc.get('total_times', 0)
    card.remaining_times_json = json.dumps(remaining)

    # 续费价格
    renew_price = to_decimal(request.form.get('renew_price', str(template.price)))
    card.purchase_price = renew_price

    if card.status == '已过期':
        card.status = '正常'

    db_commit('续费年卡', f'年卡ID={card_id}, 新到期日={card.expire_date}, 价格=¥{renew_price}')
    flash(f'年卡已续费，新到期日期: {card.expire_date}', 'success')
    return redirect(url_for('annual_cards'))


@app.route('/annual_card/<int:card_id>/usage')
@login_required
def annual_card_usage(card_id):
    """查看年卡使用记录"""
    card = _safe_get(CustomerAnnualCard, card_id, '年卡')
    usages = AnnualCardUsage.query.filter_by(card_id=card_id).order_by(
        AnnualCardUsage.created_at.desc()).all()
    return render_template('annual_card_usage.html', card=card, usages=usages)


# ---- 年卡模板管理（设置页） ----

@app.route('/settings/annual_card_templates')
@login_required
@role_required('管理员')
def settings_annual_card_templates():
    """年卡模板管理"""
    templates = AnnualCardTemplate.query.order_by(AnnualCardTemplate.created_at.desc()).all()
    items = ServiceItem.query.filter_by(is_active=True, is_deleted=False).all()
    return render_template('settings_annual_cards.html', templates=templates, items=items)


@app.route('/settings/annual_card_template/add', methods=['POST'])
@login_required
@role_required('管理员')
def settings_annual_card_template_add():
    """新增年卡模板"""
    _validate_csrf()
    name = request.form.get('name', '').strip()
    price = to_decimal(request.form.get('price', '0'))
    valid_days = request.form.get('valid_days', '365', type=int)
    extra_discount = to_decimal(request.form.get('extra_discount', '1.00'))
    description = request.form.get('description', '')

    # 收集包含的项目和次数
    item_ids = request.form.getlist('item_id[]', type=int)
    item_times = request.form.getlist('item_times[]', type=int)
    services = []
    for i, iid in enumerate(item_ids):
        if iid:
            si = ServiceItem.query.get(iid)
            if si:
                services.append({
                    'service_item_id': iid,
                    'name': si.name,
                    'total_times': item_times[i] if i < len(item_times) else 0
                })

    tpl = AnnualCardTemplate(
        name=name, price=price, valid_days=valid_days,
        extra_discount=extra_discount, description=description,
        services_json=json.dumps(services, ensure_ascii=False)
    )
    db.session.add(tpl)
    db_commit('新增年卡模板', f'模板名={name}')
    flash(f'年卡模板「{name}」已创建', 'success')
    return redirect(url_for('settings_annual_card_templates'))


@app.route('/settings/annual_card_template/<int:tid>/edit', methods=['POST'])
@login_required
@role_required('管理员')
def settings_annual_card_template_edit(tid):
    """编辑年卡模板"""
    _validate_csrf()
    tpl = _safe_get(AnnualCardTemplate, tid, '年卡模板')
    tpl.name = request.form.get('name', '').strip()
    tpl.price = to_decimal(request.form.get('price', '0'))
    tpl.valid_days = request.form.get('valid_days', '365', type=int)
    tpl.extra_discount = to_decimal(request.form.get('extra_discount', '1.00'))
    tpl.description = request.form.get('description', '')
    tpl.is_active = request.form.get('is_active') == '1'

    item_ids = request.form.getlist('item_id[]', type=int)
    item_times = request.form.getlist('item_times[]', type=int)
    services = []
    for i, iid in enumerate(item_ids):
        if iid:
            si = ServiceItem.query.get(iid)
            if si:
                services.append({
                    'service_item_id': iid,
                    'name': si.name,
                    'total_times': item_times[i] if i < len(item_times) else 0
                })
    tpl.services_json = json.dumps(services, ensure_ascii=False)
    db_commit('编辑年卡模板', f'模板ID={tid}')
    flash(f'年卡模板「{tpl.name}」已更新', 'success')
    return redirect(url_for('settings_annual_card_templates'))


@app.route('/settings/annual_card_template/<int:tid>/delete', methods=['POST'])
@login_required
@role_required('管理员')
def settings_annual_card_template_delete(tid):
    """删除年卡模板（软删除）"""
    _validate_csrf()
    tpl = _safe_get(AnnualCardTemplate, tid, '年卡模板')
    # 检查是否有客户在使用
    active_count = CustomerAnnualCard.query.filter_by(
        template_id=tid).filter(CustomerAnnualCard.status.in_(['正常', '已冻结'])).count()
    if active_count > 0:
        flash(f'有 {active_count} 位客户正在使用此模板的年卡，无法删除。请先停用模板。', 'danger')
        return redirect(url_for('settings_annual_card_templates'))
    tpl.is_active = False
    db_commit('停用年卡模板', f'模板ID={tid}')
    flash('模板已停用', 'warning')
    return redirect(url_for('settings_annual_card_templates'))


# ==================== 库存管理 ====================

@app.route('/inventory')
@login_required
def inventory():
    meds = Medicine.query.filter_by(is_active=True).order_by(Medicine.name).all()
    mids = [m.id for m in meds]
    stock_map = _batch_stock_query(mids)
    result = [{'medicine': m, 'stock': stock_map.get(m.id, 0)} for m in meds]
    return render_template('inventory.html', inventory=result)


@app.route('/inventory/inbound', methods=['POST'])
@login_required
@role_required('管理员', '库管')
def inventory_inbound():
    _validate_csrf()
    try:
        mid = int(request.form['medicine_id'])
        qty = int(request.form['quantity'])
        if qty <= 0:
            flash('入库数量必须大于0', 'warning')
            return redirect(url_for('inventory'))
        batch = request.form.get('batch_no', '')
        prod_date = _parse_date(request.form.get('production_date'))
        exp_date = _parse_date(request.form.get('expiry_date'))
        price = to_decimal(request.form.get('unit_price', '0'))
        supplier = request.form.get('supplier', '')

        ledger = InventoryLedger(
            medicine_id=mid, batch_no=batch, production_date=prod_date,
            expiry_date=exp_date, in_qty=qty, current_qty=qty,
            unit_price=price, supplier=supplier
        )
        db.session.add(ledger)
        db.session.flush()
        flow = StockFlow(medicine_id=mid, ledger_id=ledger.id, flow_type='入库',
                         quantity=qty, unit_price=price, operator_id=current_user.id)
        db.session.add(flow)
        db_commit('入库', f'药品ID={mid}, 数量={qty}')
        flash('入库成功', 'success')
    except (ValueError, KeyError) as e:
        flash(f'入库信息不完整: {str(e)}', 'warning')
    return redirect(url_for('inventory'))


# ==================== 入库单管理 ====================

@app.route('/inventory/inbound_orders')
@login_required
def inbound_orders():
    """入库单列表"""
    orders = InboundOrder.query.order_by(InboundOrder.created_at.desc()).limit(100).all()
    return render_template('inbound_orders.html', orders=orders)


@app.route('/inventory/batch_inbound', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '库管')
def batch_inbound():
    """批量入库"""
    if request.method == 'POST':
        _validate_csrf()
        try:
            items_json = request.form.get('items_json', '[]')
            items = json.loads(items_json)
            supplier = request.form.get('supplier', '')
            remark = request.form.get('remark', '')

            if not items:
                flash('入库清单不能为空', 'warning')
                return redirect(url_for('batch_inbound'))

            # 生成入库单号
            order_no = f'RK{datetime.now().strftime("%Y%m%d%H%M%S")}'
            total = Decimal('0')

            order = InboundOrder(
                order_no=order_no, supplier=supplier, remark=remark,
                status='已入库', operator_id=current_user.id,
                confirmed_at=datetime.now()
            )
            db.session.add(order)
            db.session.flush()

            for it in items:
                mid = int(it['medicine_id'])
                qty = int(it['quantity'])
                if qty <= 0:
                    continue
                price = to_decimal(it.get('unit_price', 0))
                batch = it.get('batch_no', '')
                prod_date = _parse_date(it.get('production_date'))
                exp_date = _parse_date(it.get('expiry_date'))

                # 创建入库单明细
                oi = InboundOrderItem(
                    order_id=order.id, medicine_id=mid, quantity=qty,
                    unit_price=price, batch_no=batch,
                    production_date=prod_date, expiry_date=exp_date
                )
                db.session.add(oi)

                # 创建库存台账
                ledger = InventoryLedger(
                    medicine_id=mid, batch_no=batch, production_date=prod_date,
                    expiry_date=exp_date, in_qty=qty, current_qty=qty,
                    unit_price=price, supplier=supplier
                )
                db.session.add(ledger)
                db.session.flush()

                # 创建入库流水
                flow = StockFlow(
                    medicine_id=mid, ledger_id=ledger.id, flow_type='入库',
                    quantity=qty, unit_price=price, ref_no=order_no,
                    operator_id=current_user.id
                )
                db.session.add(flow)
                total += price * qty

                # 更新药品采购成本
                med = db.session.get(Medicine, mid)
                if med and price > 0:
                    med.cost_price = price

            order.total_amount = total
            db_commit('批量入库', f'入库单={order_no}, {len(items)}项, 金额={total}')
            flash(f'批量入库成功！入库单号: {order_no}，共{len(items)}项，总金额: {total}元', 'success')
            return redirect(url_for('inbound_orders'))
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            db.session.rollback()
            flash(f'入库失败: {str(e)}', 'danger')

    meds = Medicine.query.filter_by(is_active=True).order_by(Medicine.name).all()
    mids = [m.id for m in meds]
    stock_map = _batch_stock_query(mids)
    return render_template('batch_inbound.html', medicines=meds, stock_map=stock_map)


@app.route('/inbound_order/<int:oid>/edit', methods=['POST'])
@login_required
@role_required('管理员', '库管')
def inbound_order_edit(oid):
    """编辑入库单（仅待确认状态可编辑）"""
    _validate_csrf()
    order = _safe_get(InboundOrder, oid, '入库单')
    if order.status != '待确认':
        flash('仅待确认状态的入库单可编辑', 'warning')
        return redirect(url_for('inbound_orders'))
    try:
        order.supplier = request.form.get('supplier', order.supplier)
        order.remark = request.form.get('remark', order.remark)
        db_commit('编辑入库单', f'单号={order.order_no}')
        flash('入库单已更新', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'编辑失败: {str(e)}', 'danger')
    return redirect(url_for('inbound_orders'))


@app.route('/inbound_order/<int:oid>/cancel', methods=['POST'])
@login_required
@role_required('管理员', '库管')
def inbound_order_cancel(oid):
    """取消入库单"""
    _validate_csrf()
    order = _safe_get(InboundOrder, oid, '入库单')
    if order.status == '已入库':
        flash('已入库的单据不可取消', 'warning')
        return redirect(url_for('inbound_orders'))
    order.status = '已取消'
    db_commit('取消入库单', f'单号={order.order_no}')
    flash('入库单已取消', 'info')
    return redirect(url_for('inbound_orders'))


@app.route('/inventory/outbound', methods=['POST'])
@login_required
@role_required('管理员', '库管')
def inventory_outbound():
    _validate_csrf()
    try:
        mid = int(request.form['medicine_id'])
        qty = int(request.form['quantity'])
        if qty <= 0:
            flash('出库数量必须大于0', 'warning')
            return redirect(url_for('inventory'))
        lid = int(request.form['ledger_id']) if request.form.get('ledger_id') else None
        remark = request.form.get('remark', '')

        if lid:
            ledger = _safe_get(InventoryLedger, lid, '库存台账')
            if ledger.current_qty < qty:
                flash('库存不足', 'danger')
                return redirect(url_for('inventory'))
            ledger.out_qty += qty
            ledger.current_qty -= qty
        else:
            # 无指定批次时，按先进先出自动扣减
            remaining = qty
            ledgers = InventoryLedger.query.filter_by(
                medicine_id=mid).filter(InventoryLedger.current_qty > 0
            ).order_by(InventoryLedger.created_at.asc()).all()
            for lg in ledgers:
                if remaining <= 0:
                    break
                deduct = min(lg.current_qty, remaining)
                lg.out_qty += deduct
                lg.current_qty -= deduct
                remaining -= deduct
            if remaining > 0:
                flash('库存不足，无法完成出库', 'danger')
                db.session.rollback()
                return redirect(url_for('inventory'))

        flow = StockFlow(medicine_id=mid, ledger_id=lid, flow_type='出库',
                         quantity=qty, operator_id=current_user.id, remark=remark)
        db.session.add(flow)
        db_commit('出库', f'药品ID={mid}, 数量={qty}')
        flash('出库成功', 'success')
    except (ValueError, KeyError) as e:
        flash(f'出库信息不完整: {str(e)}', 'warning')
    return redirect(url_for('inventory'))


@app.route('/inventory/flows')
@login_required
def inventory_flows():
    mid = request.args.get('medicine_id', type=int)
    q = StockFlow.query
    if mid:
        q = q.filter_by(medicine_id=mid)
    flows = q.order_by(StockFlow.created_at.desc()).limit(200).all()
    meds = Medicine.query.filter_by(is_active=True).all()
    return render_template('inventory_flows.html', flows=flows, medicines=meds, selected_mid=mid)


@app.route('/inventory/alerts')
@login_required
def inventory_alerts():
    alerts = _get_inventory_alerts_detail()
    return render_template('inventory_alerts.html', alerts=alerts)


def _get_inventory_alerts_detail():
    meds = Medicine.query.filter_by(is_active=True).all()
    mids = [m.id for m in meds]
    stock_map = _batch_stock_query(mids)
    stock_alert_days = 30
    cfg = SystemConfig.query.filter_by(key='stock_alert_days').first()
    if cfg and cfg.value:
        try:
            stock_alert_days = int(cfg.value)
        except ValueError:
            pass
    alerts = []
    for m in meds:
        total = stock_map.get(m.id, 0)
        if total <= (m.alert_threshold or 0):
            alerts.append({'type': '低库存', 'medicine': m, 'stock': total})
        ledgers = InventoryLedger.query.filter_by(medicine_id=m.id).filter(
            InventoryLedger.current_qty > 0).all()
        for lg in ledgers:
            if lg.expiry_date and lg.expiry_date <= (date.today() + timedelta(days=stock_alert_days)):
                alerts.append({'type': '近效期', 'medicine': m, 'stock': lg.current_qty,
                               'expiry': lg.expiry_date, 'batch': lg.batch_no})
    return alerts


# ==================== 库存盘点 ====================

@app.route('/inventory/stocktake', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '库管')
def inventory_stocktake():
    if request.method == 'POST':
        _validate_csrf()
        try:
            items_data = json.loads(request.form.get('items_json', '[]'))
            for item in items_data:
                mid = int(item['medicine_id'])
                actual = int(item['actual_qty'])
                book = db.session.query(
                    db.func.coalesce(db.func.sum(InventoryLedger.current_qty), 0)
                ).filter_by(medicine_id=mid).scalar() or 0
                diff = actual - book
                st = StockTake(
                    medicine_id=mid, book_qty=book, actual_qty=actual,
                    diff_qty=diff, operator_id=current_user.id
                )
                db.session.add(st)
            db_commit('库存盘点', f'盘点{len(items_data)}项')
            flash('盘点数据已提交，请管理员审核', 'success')
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            flash(f'盘点数据格式错误: {str(e)}', 'danger')
        return redirect(url_for('inventory_stocktake'))

    meds = Medicine.query.filter_by(is_active=True).order_by(Medicine.name).all()
    mids = [m.id for m in meds]
    stock_map = _batch_stock_query(mids)
    result = [{'medicine': m, 'stock': stock_map.get(m.id, 0)} for m in meds]
    # 待审核盘点
    pending = StockTake.query.filter_by(status='待审核').order_by(StockTake.created_at.desc()).all()
    return render_template('inventory_stocktake.html', inventory=result, pending_takes=pending)


@app.route('/inventory/stocktake/<int:st_id>/approve', methods=['POST'])
@login_required
@role_required('管理员')
def stocktake_approve(st_id):
    _validate_csrf()
    st = _safe_get(StockTake, st_id, '盘点记录')
    action = request.form.get('action', 'approve')
    if action == 'approve':
        # 修正库存
        if st.diff_qty != 0:
            ledgers = InventoryLedger.query.filter_by(
                medicine_id=st.medicine_id).filter(InventoryLedger.current_qty > 0
            ).order_by(InventoryLedger.created_at.asc()).all()
            remaining = abs(st.diff_qty)
            for lg in ledgers:
                if remaining <= 0:
                    break
                if st.diff_qty < 0:  # 盘亏：扣减
                    deduct = min(lg.current_qty, remaining)
                    lg.current_qty -= deduct
                    remaining -= deduct
                elif st.diff_qty > 0:  # 盘盈：增加到最新批次
                    pass
            if st.diff_qty > 0:
                # 盘盈：创建新台账
                new_ledger = InventoryLedger(
                    medicine_id=st.medicine_id, in_qty=st.diff_qty,
                    current_qty=st.diff_qty, batch_no=f'盘盈-{st.id}'
                )
                db.session.add(new_ledger)
        st.status = '已审核'
        st.approver_id = current_user.id
        db_commit('盘点审核通过', f'盘点ID={st_id}')
        flash('盘点已审核通过，库存已修正', 'success')
    elif action == 'reject':
        st.status = '已驳回'
        st.approver_id = current_user.id
        db_commit('盘点驳回', f'盘点ID={st_id}')
        flash('盘点已驳回', 'warning')
    return redirect(url_for('inventory_stocktake'))


# ==================== 基础信息 ====================

@app.route('/settings/employees', methods=['GET', 'POST'])
@login_required
@role_required('管理员')
def settings_employees():
    if request.method == 'POST':
        _validate_csrf()
        try:
            action = request.form.get('action')
            if action == 'add':
                name = request.form.get('name', '').strip()
                if not name:
                    flash('员工姓名不能为空', 'warning')
                else:
                    e = Employee(name=name, phone=request.form.get('phone'),
                                 role=request.form.get('role', '理疗师'),
                                 hire_date=_parse_date(request.form.get('hire_date')))
                    db.session.add(e)
                    db_commit('员工管理', f'新增={name}')
                    flash('员工添加成功', 'success')
            elif action == 'toggle':
                e = _safe_get(Employee, int(request.form['id']), '员工')
                e.is_active = not e.is_active
                db_commit('员工管理', f'切换状态={e.name}')
                flash('操作成功', 'success')
            elif action == 'edit':
                e = _safe_get(Employee, int(request.form['id']), '员工')
                e.name = request.form.get('name', e.name).strip()
                e.phone = request.form.get('phone', e.phone)
                e.role = request.form.get('role', e.role)
                e.hire_date = _parse_date(request.form.get('hire_date')) or e.hire_date
                sched = request.form.get('schedule_config')
                if sched:
                    e.schedule_config = sched
                db_commit('员工管理', f'编辑={e.name}')
                flash('员工信息已更新', 'success')
            elif action == 'delete':
                e = _safe_get(Employee, int(request.form['id']), '员工')
                # 检查是否有关联预约
                if e.appointments.count() > 0:
                    flash(f'员工 {e.name} 有预约记录，无法删除，请使用停用', 'warning')
                else:
                    db.session.delete(e)
                    db_commit('员工管理', f'删除={e.name}')
                    flash(f'员工 {e.name} 已删除', 'success')
        except (ValueError, KeyError) as e:
            flash(f'操作失败: {str(e)}', 'danger')
        return redirect(url_for('settings_employees'))
    emps = Employee.query.order_by(Employee.created_at.desc()).all()
    return render_template('settings_employees.html', employees=emps)


@app.route('/settings/items', methods=['GET', 'POST'])
@login_required
@role_required('管理员')
def settings_items():
    if request.method == 'POST':
        _validate_csrf()
        try:
            action = request.form.get('action')
            if action == 'add':
                name = request.form.get('name', '').strip()
                if not name:
                    flash('项目名称不能为空', 'warning')
                else:
                    si = ServiceItem(name=name, category=request.form.get('category'),
                                     price=to_decimal(request.form.get('price', '0')),
                                     cost_price=to_decimal(request.form.get('cost_price', '0')),
                                     duration=int(request.form.get('duration', 0)) if request.form.get('duration') else None,
                                     indication=request.form.get('indication'))
                    db.session.add(si)
                    db_commit('项目管理', f'新增={name}')
                    flash('项目添加成功', 'success')
            elif action == 'toggle':
                si = _safe_get(ServiceItem, int(request.form['id']), '项目')
                si.is_active = not si.is_active
                db_commit('项目管理', f'切换状态={si.name}')
                flash('操作成功', 'success')
            elif action == 'edit':
                si = _safe_get(ServiceItem, int(request.form['id']), '项目')
                si.name = request.form.get('name', si.name).strip()
                si.category = request.form.get('category', si.category)
                si.price = to_decimal(request.form.get('price', str(si.price)))
                si.cost_price = to_decimal(request.form.get('cost_price', str(si.cost_price or 0)))
                si.duration = int(request.form.get('duration', 0)) if request.form.get('duration') else si.duration
                si.indication = request.form.get('indication', si.indication)
                db_commit('项目管理', f'编辑={si.name}')
                flash('项目信息已更新', 'success')
            elif action == 'delete':
                si = _safe_get(ServiceItem, int(request.form['id']), '项目')
                si.is_deleted = True
                si.is_active = False
                db_commit('项目管理', f'删除={si.name}')
                flash(f'项目 {si.name} 已删除', 'success')
        except (ValueError, KeyError) as e:
            flash(f'操作失败: {str(e)}', 'danger')
        return redirect(url_for('settings_items'))
    items = ServiceItem.query.filter_by(is_deleted=False).order_by(ServiceItem.category, ServiceItem.name).all()
    return render_template('settings_items.html', items=items)


@app.route('/settings/medicines', methods=['GET', 'POST'])
@login_required
@role_required('管理员', '库管')
def settings_medicines():
    if request.method == 'POST':
        _validate_csrf()
        try:
            action = request.form.get('action')
            if action == 'add':
                name = request.form.get('name', '').strip()
                if not name:
                    flash('药品名称不能为空', 'warning')
                else:
                    m = Medicine(name=name, spec=request.form.get('spec'),
                                 unit=request.form.get('unit'),
                                 retail_price=to_decimal(request.form.get('price', '0')),
                                 cost_price=to_decimal(request.form.get('cost_price', '0')),
                                 alert_threshold=int(request.form.get('threshold', 10)) if request.form.get('threshold') else 10,
                                 category=request.form.get('category'))
                    db.session.add(m)
                    db_commit('药品管理', f'新增={name}')
                    flash('药品添加成功', 'success')
            elif action == 'toggle':
                m = _safe_get(Medicine, int(request.form['id']), '药品')
                m.is_active = not m.is_active
                db_commit('药品管理', f'切换状态={m.name}')
                flash('操作成功', 'success')
            elif action == 'edit':
                m = _safe_get(Medicine, int(request.form['id']), '药品')
                m.name = request.form.get('name', m.name).strip()
                m.category = request.form.get('category', m.category)
                m.spec = request.form.get('spec', m.spec)
                m.unit = request.form.get('unit', m.unit)
                m.retail_price = to_decimal(request.form.get('price', str(m.retail_price)))
                m.cost_price = to_decimal(request.form.get('cost_price', str(m.cost_price or 0)))
                m.alert_threshold = int(request.form.get('threshold', m.alert_threshold)) if request.form.get('threshold') else m.alert_threshold
                db_commit('药品管理', f'编辑={m.name}')
                flash('药品信息已更新', 'success')
            elif action == 'delete':
                m = _safe_get(Medicine, int(request.form['id']), '药品')
                # 检查是否有库存
                total_stock = db.session.query(db.func.coalesce(db.func.sum(InventoryLedger.current_qty), 0)).filter_by(medicine_id=m.id).scalar()
                if total_stock and total_stock > 0:
                    flash(f'药品 {m.name} 有库存({total_stock})，无法删除，请使用停用', 'warning')
                else:
                    m.is_deleted = True
                    m.is_active = False
                    db_commit('药品管理', f'删除={m.name}')
                    flash(f'药品 {m.name} 已删除', 'success')
        except (ValueError, KeyError) as e:
            flash(f'操作失败: {str(e)}', 'danger')
        return redirect(url_for('settings_medicines'))
    meds = Medicine.query.filter_by(is_deleted=False).order_by(Medicine.category, Medicine.name).all()
    return render_template('settings_medicines.html', medicines=meds)


# ==================== 报表 ====================

@app.route('/reports')
@login_required
def reports():
    rtype = request.args.get('type', 'daily')
    start = _parse_date(request.args.get('start')) or (date.today() - timedelta(days=30))
    end = _parse_date(request.args.get('end')) or date.today()

    charges = ChargeRecord.query.filter(
        db.func.date(ChargeRecord.created_at) >= start,
        db.func.date(ChargeRecord.created_at) <= end,
        ChargeRecord.status == '已支付'
    ).all()

    recharges = RechargeRecord.query.filter(
        db.func.date(RechargeRecord.created_at) >= start,
        db.func.date(RechargeRecord.created_at) <= end
    ).all()

    total_revenue = sum(((c.final_amount or 0) for c in charges), Decimal('0'))
    total_recharge = sum(((r.amount or 0) for r in recharges), Decimal('0'))
    total_gift = sum(((r.gift_amount or 0) for r in recharges), Decimal('0'))

    # 成本核算 & 项目统计
    total_cost = Decimal('0')
    item_stats = {}
    # 预加载成本价格映射
    si_cost_map = {si.name: (si.cost_price or 0) for si in ServiceItem.query.all()}
    med_cost_map = {m.name: (m.cost_price or 0) for m in Medicine.query.all()}
    for c in charges:
        try:
            items = json.loads(c.items_json or '[]')
            for it in items:
                name = it.get('name', '未知')
                qty = it.get('qty', 1)
                revenue = to_decimal(it.get('subtotal', 0))
                # 查找成本价：先理疗项目，再药品
                unit_cost = si_cost_map.get(name, med_cost_map.get(name, Decimal('0')))
                cost = unit_cost * qty
                total_cost += cost
                if name not in item_stats:
                    item_stats[name] = {'count': 0, 'revenue': Decimal('0'), 'cost': Decimal('0')}
                item_stats[name]['count'] += qty
                item_stats[name]['revenue'] += revenue
                item_stats[name]['cost'] += cost
        except (json.JSONDecodeError, TypeError):
            pass

    total_profit = total_revenue - total_cost
    profit_rate = (total_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')

    # 员工业绩
    emp_stats = {}
    for c in charges:
        if c.operator:
            name = c.operator.display_name
            if name not in emp_stats:
                emp_stats[name] = {'count': 0, 'revenue': Decimal('0')}
            emp_stats[name]['count'] += 1
            emp_stats[name]['revenue'] += (c.final_amount or 0)

    # 客户消费排行
    cust_stats = {}
    for c in charges:
        cname = c.customer.name if c.customer else '未知'
        if cname not in cust_stats:
            cust_stats[cname] = {'count': 0, 'revenue': Decimal('0')}
        cust_stats[cname]['count'] += 1
        cust_stats[cname]['revenue'] += (c.final_amount or 0)
    cust_rank = sorted(cust_stats.items(), key=lambda x: x[1]['revenue'], reverse=True)[:20]

    return render_template('reports.html', rtype=rtype, start=start, end=end,
                           total_revenue=total_revenue, total_recharge=total_recharge,
                           total_gift=total_gift, charge_count=len(charges),
                           total_cost=total_cost, total_profit=total_profit,
                           profit_rate=profit_rate,
                           item_stats=item_stats, emp_stats=emp_stats,
                           cust_rank=cust_rank, charges=charges)


@app.route('/reports/export')
@login_required
def reports_export():
    start = _parse_date(request.args.get('start')) or (date.today() - timedelta(days=30))
    end = _parse_date(request.args.get('end')) or date.today()
    charges = ChargeRecord.query.filter(
        db.func.date(ChargeRecord.created_at) >= start,
        db.func.date(ChargeRecord.created_at) <= end,
        ChargeRecord.status == '已支付'
    ).all()

    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    writer = csv.writer(output)
    writer.writerow(['收费单号', '客户姓名', '总金额', '折扣', '实付金额', '退款金额', '状态', '操作员', '时间'])
    for c in charges:
        writer.writerow([
            c.id,
            c.customer.name if c.customer else '',
            c.total_amount, c.discount_amount,
            c.final_amount, c.refund_amount or 0,
            c.status,
            c.operator.display_name if c.operator else '',
            c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else ''
        ])

    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment;filename=charges_{start}_{end}.csv'})


# ==================== 经营日报生成 ====================

@app.route('/reports/daily/generate', methods=['POST'])
@login_required
@role_required('管理员')
def generate_daily_report():
    _validate_csrf()
    report_date = _parse_date(request.form.get('date')) or date.today()
    try:
        charges = ChargeRecord.query.filter(
            db.func.date(ChargeRecord.created_at) == report_date,
            ChargeRecord.status == '已支付'
        ).all()
        recharges = RechargeRecord.query.filter(
            db.func.date(RechargeRecord.created_at) == report_date
        ).all()
        regs = Registration.query.filter(
            db.func.date(Registration.reg_time) == report_date
        ).count()

        revenue = sum(((c.final_amount or 0) for c in charges), Decimal('0'))
        recharge_income = sum(((r.amount or 0) for r in recharges), Decimal('0'))
        recharge_consumed = Decimal('0')
        deductions = DeductionRecord.query.filter(
            db.func.date(DeductionRecord.created_at) == report_date
        ).all()
        recharge_consumed = sum(((d.amount or 0) for d in deductions), Decimal('0'))

        # 项目统计
        item_stats = {}
        for c in charges:
            try:
                items = json.loads(c.items_json or '[]')
                for it in items:
                    name = it.get('name', '未知')
                    item_stats[name] = item_stats.get(name, 0) + it.get('qty', 1)
            except (json.JSONDecodeError, TypeError):
                pass

        report = DailyReport.query.filter_by(report_date=report_date).first()
        if not report:
            report = DailyReport(report_date=report_date)
            db.session.add(report)
        report.revenue = revenue
        report.customer_count = regs
        report.recharge_income = recharge_income
        report.recharge_consumed = recharge_consumed
        report.item_stats_json = json.dumps(item_stats, ensure_ascii=False)
        db_commit('生成日报', f'日期={report_date}')
        flash(f'{report_date} 经营日报已生成', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'日报生成失败: {str(e)}', 'danger')
    return redirect(url_for('reports'))


# ==================== 排班看板（独立页面） ====================

def _verify_board_token():
    """验证券牌请求的token是否有效，返回True/False"""
    token = request.args.get('token', '')
    if not token:
        return False
    cfg = SystemConfig.query.filter_by(key='board_token').first()
    return cfg and cfg.value and hmac.compare_digest(cfg.value, token)


def _mask_name(name):
    """客户姓名脱敏：仅显示姓+**"""
    if not name:
        return '未知'
    return name[0] + '**'


@app.route('/board/schedule')
def board_schedule():
    """排班看板独立页面（免登录，Token鉴权，全屏模式）"""
    if not _verify_board_token():
        abort(403, description='无效的访问令牌，请通过系统管理获取看板链接')
    store = StoreInfo.query.first()
    # 获取看板配置
    theme_cfg = SystemConfig.query.filter_by(key='board_theme').first()
    view_cfg = SystemConfig.query.filter_by(key='board_view_mode').first()
    refresh_cfg = SystemConfig.query.filter_by(key='board_refresh_interval').first()
    mask_cfg = SystemConfig.query.filter_by(key='board_mask_customer_name').first()
    return render_template('board_schedule.html',
                           store=store,
                           token=request.args.get('token', ''),
                           theme=theme_cfg.value if theme_cfg else 'light',
                           view_mode=view_cfg.value if view_cfg else 'timeline',
                           refresh_interval=int(refresh_cfg.value) if refresh_cfg and refresh_cfg.value else 60,
                           mask_name=(mask_cfg.value if mask_cfg else '1') == '1',
                           version=APP_VERSION)


@app.route('/api/board/schedule')
def api_board_schedule():
    """排班看板数据API（Token鉴权，JSON返回）"""
    if not _verify_board_token():
        return jsonify({'error': '无效的访问令牌'}), 403
    target_date = request.args.get('date')
    try:
        target = datetime.strptime(target_date, '%Y-%m-%d').date() if target_date else date.today()
    except ValueError:
        target = date.today()

    appts = Appointment.query.filter_by(appt_date=target).order_by(
        Appointment.time_slot, Appointment.employee_id).all()

    employees = Employee.query.filter(Employee.is_active == True).all()
    emp_map = {e.id: e for e in employees}

    # 脱敏设置
    mask_cfg = SystemConfig.query.filter_by(key='board_mask_customer_name').first()
    do_mask = (mask_cfg.value if mask_cfg else '1') == '1'

    data = []
    for a in appts:
        emp = emp_map.get(a.employee_id)
        cust = Customer.query.get(a.customer_id) if a.customer_id else None
        # 解析时段 "09:00-10:00"
        start_time = ''
        end_time = ''
        if a.time_slot and '-' in a.time_slot:
            parts = a.time_slot.split('-')
            start_time = parts[0].strip()
            end_time = parts[1].strip()
        # 理疗项目名称
        svc_name = ''
        if a.service_item:
            svc_name = a.service_item.name
        data.append({
            'id': a.id,
            'start_time': start_time,
            'end_time': end_time,
            'employee_name': emp.name if emp else '未指定',
            'customer_name': _mask_name(cust.name) if (cust and do_mask) else (cust.name if cust else '未知'),
            'service_item': svc_name,
            'status': a.status or '待确认',
            'customer_phone': '',  # 看板不暴露电话
            'note': a.remark or ''
        })

    # 本周统计
    week_start = target - timedelta(days=target.weekday())
    week_end = week_start + timedelta(days=6)
    week_count = Appointment.query.filter(
        Appointment.appt_date >= week_start,
        Appointment.appt_date <= week_end
    ).count()
    today_count = len(appts)
    done_today = sum(1 for a in appts if a.status in ('已完成', '已取消'))
    pending_today = today_count - done_today

    return jsonify({
        'date': target.isoformat(),
        'weekday': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][target.weekday()],
        'appointments': data,
        'stats': {
            'today_total': today_count,
            'today_pending': pending_today,
            'today_done': done_today,
            'week_total': week_count,
            'employees_count': len(employees)
        }
    })


@app.route('/system/board_token', methods=['POST'])
@login_required
@role_required('管理员')
def system_board_token():
    """生成/刷新看板访问令牌"""
    _validate_csrf()
    action = request.form.get('action', 'generate')
    try:
        cfg = SystemConfig.query.filter_by(key='board_token').first()
        if action == 'generate':
            new_token = secrets.token_urlsafe(24)
            if not cfg:
                cfg = SystemConfig(key='board_token', value=new_token,
                                   description='排班看板访问令牌')
                db.session.add(cfg)
            else:
                cfg.value = new_token
            db_commit('生成看板令牌')
            flash(f'看板令牌已生成: {new_token}', 'success')
        elif action == 'revoke':
            if cfg:
                cfg.value = ''
                db_commit('撤销看板令牌')
                flash('看板令牌已撤销', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'操作失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


# ==================== 系统管理 ====================

@app.route('/system')
@login_required
@role_required('管理员')
def system():
    store = StoreInfo.query.first()
    configs = SystemConfig.query.all()
    users = User.query.all()
    logs = SystemLog.query.order_by(SystemLog.created_at.desc()).limit(50).all()
    backups = list_backups(BACKUP_DIR)
    return render_template('system.html', store=store, configs=configs, users=users,
                           logs=logs, backups=backups, version=APP_VERSION,
                           db_version=get_recorded_version(DB_PATH),
                           upgrade_report=UPGRADE_REPORT)


@app.route('/system/store', methods=['POST'])
@login_required
@role_required('管理员')
def system_store_update():
    _validate_csrf()
    try:
        store = StoreInfo.query.first()
        if not store:
            store = StoreInfo()
            db.session.add(store)
        store.name = request.form.get('name')
        store.address = request.form.get('address')
        store.phone = request.form.get('phone')
        store.license_no = request.form.get('license_no')
        db_commit('更新门店信息')
        flash('门店信息已更新', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'更新失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


@app.route('/system/user/add', methods=['POST'])
@login_required
@role_required('管理员')
def system_user_add():
    _validate_csrf()
    try:
        username = request.form.get('username', '').strip()
        if not username:
            flash('用户名不能为空', 'warning')
            return redirect(url_for('system'))
        existing = User.query.filter_by(username=username).first()
        if existing:
            flash('用户名已存在', 'warning')
            return redirect(url_for('system'))
        u = User(username=username, display_name=request.form.get('display_name', username),
                 role=request.form.get('role', '前台'))
        u.set_password(request.form.get('password', '123456'))
        db.session.add(u)
        db_commit('新建用户', f'用户={u.username}')
        flash('用户创建成功', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'创建失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


@app.route('/system/user/<int:uid>/toggle', methods=['POST'])
@login_required
@role_required('管理员')
def system_user_toggle(uid):
    _validate_csrf()
    u = _safe_get(User, uid, '用户')
    if u.username != 'admin':
        u.is_active_user = not u.is_active_user
        db_commit('用户管理', f'{"启用" if u.is_active_user else "禁用"} {u.display_name}')
        flash(f'用户 {u.display_name} 已{"启用" if u.is_active_user else "禁用"}', 'info')
    return redirect(url_for('system'))


@app.route('/system/user/<int:uid>/password', methods=['POST'])
@login_required
def change_password(uid):
    _validate_csrf()
    if current_user.id != uid and current_user.role != '管理员':
        flash('无权修改他人密码', 'danger')
        return redirect(url_for('system'))
    u = _safe_get(User, uid, '用户')
    old_pwd = request.form.get('old_password', '')
    new_pwd = request.form.get('new_password', '')
    confirm_pwd = request.form.get('confirm_password', '')
    if current_user.role != '管理员' and not u.check_password(old_pwd):
        flash('原密码错误', 'danger')
        return redirect(url_for('system'))
    if len(new_pwd) < 6:
        flash('新密码长度不能少于6位', 'warning')
        return redirect(url_for('system'))
    if new_pwd != confirm_pwd:
        flash('两次输入的密码不一致', 'warning')
        return redirect(url_for('system'))
    u.set_password(new_pwd)
    db_commit('修改密码', f'用户={u.username}')
    flash('密码修改成功', 'success')
    return redirect(url_for('system'))


@app.route('/system/config', methods=['POST'])
@login_required
@role_required('管理员')
def system_config_update():
    _validate_csrf()
    try:
        for key, value in request.form.items():
            if key.startswith('cfg_'):
                config_key = key[4:]
                cfg = SystemConfig.query.filter_by(key=config_key).first()
                if cfg:
                    cfg.value = value
        db_commit('更新系统配置')
        flash('系统配置已更新', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'配置更新失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


@app.route('/system/backup', methods=['POST'])
@login_required
@role_required('管理员')
def system_backup():
    _validate_csrf()
    try:
        dest = backup_db(DB_PATH)
        log_action('数据备份', f'备份路径={dest}')
        db.session.commit()
        flash(f'备份成功！文件: {dest}', 'success')
    except Exception as e:
        flash(f'备份失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


@app.route('/system/restore', methods=['POST'])
@login_required
@role_required('管理员')
def system_restore():
    _validate_csrf()
    backup_name = request.form.get('backup_file', '')
    if not backup_name:
        flash('请选择备份文件', 'warning')
        return redirect(url_for('system'))
    # 路径穿越防护：确保备份文件名合法
    if '..' in backup_name or '/' in backup_name or '\\' in backup_name:
        flash('非法的备份文件名', 'danger')
        return redirect(url_for('system'))
    backup_file = os.path.join(BACKUP_DIR, os.path.basename(backup_name))
    if not os.path.exists(backup_file):
        flash('备份文件不存在', 'danger')
        return redirect(url_for('system'))
    try:
        pre_backup = restore_db(DB_PATH, backup_file)
        flash(f'数据恢复成功！恢复前已自动备份到: {pre_backup}。请重启系统以生效。', 'success')
    except Exception as e:
        flash(f'数据恢复失败: {str(e)}', 'danger')
    return redirect(url_for('system'))


@app.route('/system/logs')
@login_required
@role_required('管理员')
def system_logs():
    page = request.args.get('page', 1, type=int)
    logs = SystemLog.query.order_by(SystemLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('system_logs.html', logs=logs.items, pagination=logs)


# ==================== 自动备份调度 ====================

def _auto_backup_scheduler():
    """后台线程：定时自动备份（所有DB操作必须在app_context内）"""
    while True:
        _time.sleep(60)
        try:
            with app.app_context():
                cfg = SystemConfig.query.filter_by(key='auto_backup').first()
                time_cfg = SystemConfig.query.filter_by(key='backup_time').first()
                if cfg and cfg.value == '1' and time_cfg:
                    target_time = time_cfg.value or '22:00'
                    now = datetime.now()
                    current_hm = now.strftime('%H:%M')
                    if current_hm == target_time:
                        today_backup = os.path.join(BACKUP_DIR, f'clinic_backup_{now.strftime("%Y%m%d")}_auto.db')
                        if not os.path.exists(today_backup):
                            import shutil
                            os.makedirs(BACKUP_DIR, exist_ok=True)
                            shutil.copy2(DB_PATH, today_backup)
                            # 重置时间戳，避免保留源文件旧mtime
                            os.utime(today_backup, None)
                # 显式移除当前线程的scoped session，防止泄漏
                db.session.remove()
        except Exception:
            pass


# ==================== 启动 ====================

# 版本升级检查：数据库版本与程序版本一致时直接跳过（不做任何初始化/迁移）；
# 新装或版本升级时自动执行：备份 -> 完整性预检 -> 补表/补列/补配置 -> 复检 -> 记录版本。
# 已有业务数据绝不会被重新初始化。
UPGRADE_REPORT = check_and_upgrade(app, APP_VERSION, DB_PATH)

# 启动自动备份线程
_backup_thread = threading.Thread(target=_auto_backup_scheduler, daemon=True)
_backup_thread.start()


if __name__ == '__main__':
    import webbrowser
    port = 5678
    print(f'昭德堂健康管理中心业务系统 v{APP_VERSION}')
    print(f'数据库: {DB_PATH}')
    print(f'访问地址: http://127.0.0.1:{port}')
    threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    app.run(host='127.0.0.1', port=port, debug=False)
