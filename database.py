"""数据库初始化与种子数据"""

import os
import shutil
import glob
import sqlite3
from datetime import datetime, date, timedelta
from decimal import Decimal
from models import (
    db, User, StoreInfo, Employee, ServiceItem, Medicine,
    Customer, PrepaidAccount, SystemConfig, InventoryLedger
)
from sqlalchemy import event, text


def _set_sqlite_pragma(dbapi_conn, connection_record):
    """SQLite 连接级 PRAGMA：WAL、外键、忙等待。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def setup_engine(app):
    """连接层初始化（每次启动均需调用，幂等）：注册 PRAGMA 监听器 + 创建表结构。
    与版本无关，即使版本一致（跳过升级）也必须启用 WAL/外键。
    """
    with app.app_context():
        engine = db.engine
        # 防止重复注册监听器
        if not event.contains(engine, "connect", _set_sqlite_pragma):
            event.listen(engine, "connect", _set_sqlite_pragma)
        db.create_all()


def init_db(app):
    """建表 + 补列（版本升级路径调用）。幂等，不触碰已有数据。"""
    setup_engine(app)
    with app.app_context():
        _migrate_schema(db.engine)


def integrity_check(db_path):
    """数据库完整性校验：返回 (ok, 详情dict)。
    含 PRAGMA integrity_check、外键一致性、关键表存在性。
    """
    detail = {'integrity': 'unknown', 'foreign_key_violations': 0, 'tables': 0}
    try:
        conn = sqlite3.connect(db_path)
        try:
            detail['integrity'] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            try:
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                detail['foreign_key_violations'] = len(fk)
            except Exception:
                detail['foreign_key_violations'] = -1
            detail['tables'] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        detail['integrity'] = f'error: {e}'
    ok = (detail['integrity'] == 'ok') and (detail['foreign_key_violations'] == 0)
    return ok, detail


def validate_data_summary():
    """业务数据核验摘要（升级后确认已有数据未被破坏）。返回各关键表行数。"""
    summary = {}
    for label, model in [('users', User), ('customers', Customer),
                         ('employees', Employee), ('service_items', ServiceItem),
                         ('medicines', Medicine), ('configs', SystemConfig)]:
        try:
            summary[label] = model.query.count()
        except Exception:
            summary[label] = -1
    return summary


def _migrate_schema(engine):
    """自动检测并补充数据库表中的缺失列（解决打包升级后schema不同步问题）"""
    import sqlite3

    conn = sqlite3.connect(engine.url.database)
    added = []

    try:
        for table_name, table in db.metadata.tables.items():
            # 获取数据库中已有的列
            try:
                cursor = conn.execute(f"PRAGMA table_info({table_name})")
                existing_cols = {row[1] for row in cursor.fetchall()}
            except Exception:
                continue  # 表不存在则跳过（由create_all处理）

            # 对比模型定义中的列
            for col in table.columns:
                if col.name not in existing_cols:
                    col_type = _sqlalchemy_type_to_sqlite(col.type)
                    default = _get_default_for_type(col_type)
                    sql = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type} DEFAULT {default}"
                    try:
                        conn.execute(sql)
                        added.append(f'{table_name}.{col.name}')
                    except Exception:
                        pass
        if added:
            conn.commit()
            print(f'[Schema迁移] 已自动补充 {len(added)} 个缺失列: {", ".join(added)}')
    finally:
        conn.close()


def _ensure_board_configs():
    """确保看板相关配置项存在（平滑升级：旧数据库自动补充）"""
    board_defaults = [
        ('board_theme', 'light', '看板默认主题(light/dark)'),
        ('board_view_mode', 'timeline', '看板默认视图(timeline/matrix/list)'),
        ('board_refresh_interval', '60', '看板自动刷新间隔(秒)'),
        ('board_mask_customer_name', '1', '看板客户姓名脱敏(1=开启/0=关闭)'),
    ]
    created = False
    for key, default_val, desc in board_defaults:
        if not SystemConfig.query.filter_by(key=key).first():
            db.session.add(SystemConfig(key=key, value=default_val, description=desc))
            created = True
    if created:
        db.session.commit()
        print('[Schema迁移] 已自动补充看板配置项')


def _sqlalchemy_type_to_sqlite(sa_type):
    """将SQLAlchemy类型转为SQLite类型字符串"""
    type_name = type(sa_type).__name__.upper()
    if 'VARCHAR' in type_name or 'TEXT' in type_name or 'STRING' in type_name:
        length = getattr(sa_type, 'length', 255) or 255
        return f'VARCHAR({length})'
    if 'INTEGER' in type_name or 'SMALLINT' in type_name:
        return 'INTEGER'
    if 'NUMERIC' in type_name or 'DECIMAL' in type_name:
        p = getattr(sa_type, 'precision', 10) or 10
        s = getattr(sa_type, 'scale', 2) or 2
        return f'NUMERIC({p}, {s})'
    if 'FLOAT' in type_name or 'REAL' in type_name:
        return 'REAL'
    if 'BOOLEAN' in type_name:
        return 'BOOLEAN'
    if 'DATE' in type_name or 'TIME' in type_name:
        return 'VARCHAR(30)'
    return 'TEXT'


def _get_default_for_type(col_type):
    """根据列类型返回默认值"""
    if col_type.startswith('VARCHAR') or col_type == 'TEXT':
        return "''"
    if col_type in ('INTEGER', 'REAL', 'BOOLEAN') or col_type.startswith('NUMERIC'):
        return '0'
    return 'NULL'


def _seed_core_if_empty():
    """仅当用户表为空时创建核心账号与门店信息（全新安装场景）。
    已有用户时绝不重复插入，保证已有数据不被初始化。
    """
    if User.query.first() is not None:
        return False

    # 管理员账号
    admin = User(username='admin', display_name='系统管理员', role='管理员')
    admin.set_password('admin123')
    db.session.add(admin)

    # 前台账号
    front = User(username='front', display_name='前台接待', role='前台')
    front.set_password('123456')
    db.session.add(front)

    # 理疗师账号
    thera = User(username='therapist', display_name='张理疗师', role='理疗师')
    thera.set_password('123456')
    db.session.add(thera)

    # 门店信息
    if StoreInfo.query.first() is None:
        store = StoreInfo(name='昭德堂健康管理中心', address='北京市朝阳区健康路88号',
                          phone='010-88886666', license_no='京卫中医字[2024]第0088号')
        db.session.add(store)

    db.session.commit()
    return True


def _ensure_default_configs():
    """幂等补齐系统配置项：仅插入不存在的 key，绝不覆盖已有配置。
    新版本新增的配置项在升级时自动补全。
    """
    defaults = [
        ('site_title', '昭德堂健康管理中心', '系统名称(顶栏展示，可自定义)'),
        ('enable_prepaid', '1', '是否启用预充值功能'),
        ('recharge_gift_rule', '500:50,1000:150,2000:400', '充值赠送规则(充值额:赠送额,多档逗号分隔)'),
        ('recharge_packages', '500,1000,2000,5000', '预设充值套餐金额'),
        ('refund_gift_mode', '1', '退款赠送处理: 1=赠送不可退 2=等比例退'),
        ('cancel_time_limit', '24:00', '消费撤销时限(当天时间前)'),
        ('stock_alert_days', '30', '近效期预警天数'),
        ('auto_backup', '1', '是否自动备份'),
        ('backup_time', '22:00', '自动备份时间'),
        ('time_slots', '08:00-09:00,09:00-10:00,10:00-11:00,11:00-12:00,14:00-15:00,15:00-16:00,16:00-17:00,17:00-18:00,18:00-19:00,19:00-20:00', '预约时段配置(逗号分隔)'),
    ]
    added = 0
    for key, val, desc in defaults:
        if not SystemConfig.query.filter_by(key=key).first():
            db.session.add(SystemConfig(key=key, value=val, description=desc))
            added += 1
    if added:
        db.session.commit()
    return added


def _seed_demo_if_empty():
    """仅当对应业务表为空时插入演示数据（员工/项目/药品/客户）。
    已有数据的表绝不被重新初始化。
    """
    seeded = []
    # 员工（含排班配置）
    if Employee.query.first() is None:
        employees = [
            Employee(name='张师傅', phone='13800001111', role='理疗师', hire_date=date(2023, 3, 1),
                     schedule_config='{"weekdays":[1,2,3,4,5]}'),
            Employee(name='李医师', phone='13800002222', role='理疗师', hire_date=date(2023, 6, 15),
                     schedule_config='{"weekdays":[1,2,3,4,5]}'),
            Employee(name='王技师', phone='13800003333', role='理疗师', hire_date=date(2024, 1, 10),
                     schedule_config='{"weekdays":[1,3,5]}'),
            Employee(name='赵前台', phone='13800004444', role='前台', hire_date=date(2023, 1, 1),
                     schedule_config='{"weekdays":[1,2,3,4,5,6]}'),
        ]
        db.session.add_all(employees)
        seeded.append('员工')

    # 理疗项目
    if ServiceItem.query.first() is None:
        items = [
            ServiceItem(name='全身推拿', category='推拿', price=Decimal('198'), cost_price=Decimal('60'), duration=60, indication='颈肩腰腿痛、肌肉劳损'),
            ServiceItem(name='局部推拿', category='推拿', price=Decimal('128'), cost_price=Decimal('40'), duration=30, indication='局部酸痛、僵硬'),
            ServiceItem(name='艾灸', category='艾灸', price=Decimal('88'), cost_price=Decimal('20'), duration=40, indication='体寒、气血不足、关节冷痛'),
            ServiceItem(name='拔罐', category='拔罐', price=Decimal('68'), cost_price=Decimal('10'), duration=20, indication='湿气重、感冒、肩背酸痛'),
            ServiceItem(name='刮痧', category='刮痧', price=Decimal('78'), cost_price=Decimal('15'), duration=30, indication='中暑、感冒、肌肉酸痛'),
            ServiceItem(name='针灸', category='针灸', price=Decimal('158'), cost_price=Decimal('30'), duration=40, indication='各类疼痛、面瘻、中风后遗症'),
            ServiceItem(name='正骨', category='正骨', price=Decimal('258'), cost_price=Decimal('50'), duration=30, indication='关节错位、脊柱侧弯、骨盆矫正'),
            ServiceItem(name='足疗', category='推拿', price=Decimal('128'), cost_price=Decimal('35'), duration=50, indication='失眠、疲劳、足部不适'),
        ]
        db.session.add_all(items)
        seeded.append('理疗项目')

    # 药品
    if Medicine.query.first() is None:
        meds = [
            Medicine(name='当归', spec='500g/袋', unit='g', retail_price=Decimal('0.15'), cost_price=Decimal('0.08'), alert_threshold=500, category='中药饮片'),
            Medicine(name='黄芪', spec='500g/袋', unit='g', retail_price=Decimal('0.12'), cost_price=Decimal('0.06'), alert_threshold=500, category='中药饮片'),
            Medicine(name='川芎', spec='500g/袋', unit='g', retail_price=Decimal('0.18'), cost_price=Decimal('0.10'), alert_threshold=300, category='中药饮片'),
            Medicine(name='红花', spec='500g/袋', unit='g', retail_price=Decimal('0.25'), cost_price=Decimal('0.12'), alert_threshold=200, category='中药饮片'),
            Medicine(name='艾叶', spec='500g/袋', unit='g', retail_price=Decimal('0.08'), cost_price=Decimal('0.03'), alert_threshold=1000, category='中药饮片'),
            Medicine(name='活血止痛膏', spec='10片/盒', unit='盒', retail_price=Decimal('25'), cost_price=Decimal('12'), alert_threshold=50, category='中成药'),
            Medicine(name='云南白药', spec='4g/瓶', unit='瓶', retail_price=Decimal('18'), cost_price=Decimal('9'), alert_threshold=30, category='中成药'),
            Medicine(name='一次性针灸针', spec='100支/盒', unit='盒', retail_price=Decimal('35'), cost_price=Decimal('18'), alert_threshold=20, category='耗材'),
            Medicine(name='拔罐器', spec='24罐/套', unit='套', retail_price=Decimal('120'), cost_price=Decimal('55'), alert_threshold=5, category='耗材'),
            Medicine(name='艾条', spec='10支/盒', unit='盒', retail_price=Decimal('28'), cost_price=Decimal('12'), alert_threshold=30, category='耗材'),
        ]
        db.session.add_all(meds)
        seeded.append('药品')

    # 示例客户 + 预充值账户
    if Customer.query.first() is None:
        customers = [
            Customer(name='陈大明', gender='男', birthday=date(1975, 5, 20), phone='13900001111',
                     constitution_type='气虚质', member_level='金卡', member_discount=Decimal('0.85'), points=520),
            Customer(name='刘芳', gender='女', birthday=date(1988, 11, 3), phone='13900002222',
                     constitution_type='阴虚质', member_level='银卡', member_discount=Decimal('0.90'), points=180),
            Customer(name='周建国', gender='男', birthday=date(1965, 8, 15), phone='13900003333',
                     constitution_type='阳虚质', member_level='钻石', member_discount=Decimal('0.80'), points=1200),
            Customer(name='吴美丽', gender='女', birthday=date(1992, 3, 28), phone='13900004444',
                     constitution_type='平和质', member_level='普通', member_discount=Decimal('1.00'), points=30),
        ]
        db.session.add_all(customers)
        db.session.flush()
        for c in customers:
            db.session.add(PrepaidAccount(customer_id=c.id, balance=0, total_recharge=0, total_consumed=0))
        seeded.append('客户')

    if seeded:
        db.session.commit()
    return seeded


def backup_db(db_path, backup_dir=None):
    """备份数据库。
    使用 SQLite 在线备份 API（而非 shutil.copy2），以正确处理 WAL 模式：
    WAL 模式下未 checkpoint 的提交存在 -wal 文件中，直接复制主文件会遗漏近期数据。
    """
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(db_path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    dest = os.path.join(backup_dir, f'clinic_backup_{ts}.db')
    # SQLite 原生备份：生成包含 WAL 未 checkpoint 数据的一致性快照
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    # 备份文件时间戳置为当前，避免下方 cleanup 误判超期而删除
    os.utime(dest, None)
    # 清理30天前的备份
    _cleanup_old_backups(backup_dir, days=30)
    return dest


def restore_db(db_path, backup_file):
    """从备份文件恢复数据库。
    恢复前先用 SQLite 备份 API 对当前库做一致性 pre_restore 快照；
    恢复后清理残留的 -wal/-shm，避免旧 WAL 覆盖恢复结果（需重启生效）。
    """
    if not os.path.exists(backup_file):
        raise FileNotFoundError(f'备份文件不存在: {backup_file}')
    # 先备份当前数据库（一致性快照，含 WAL 数据）
    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    current_backup = os.path.join(os.path.dirname(db_path), 'backups', f'pre_restore_{ts}.db')
    os.makedirs(os.path.dirname(current_backup), exist_ok=True)
    if os.path.exists(db_path):
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(current_backup)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.utime(current_backup, None)
    # 恢复：用备份文件覆盖主库
    shutil.copy2(backup_file, db_path)
    # 清理残留 WAL/SHM，确保恢复的库文件为权威数据源
    for suffix in ('-wal', '-shm'):
        stale = db_path + suffix
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
    return current_backup


def _cleanup_old_backups(backup_dir, days=30):
    """清理N天前的备份文件"""
    cutoff = datetime.now() - timedelta(days=days)
    for f in glob.glob(os.path.join(backup_dir, 'clinic_backup_*.db')):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            if mtime < cutoff:
                os.remove(f)
        except Exception:
            pass


def list_backups(backup_dir):
    """列出所有备份文件"""
    if not os.path.exists(backup_dir):
        return []
    files = []
    for f in sorted(glob.glob(os.path.join(backup_dir, '*.db')), reverse=True):
        stat = os.stat(f)
        files.append({
            'name': os.path.basename(f),
            'path': f,
            'size': stat.st_size,
            'time': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        })
    return files
