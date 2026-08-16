#!/usr/bin/env python3
"""
系统综合审查测试
覆盖：1)数据备份与恢复 2)多角色权限矩阵 3)业务流程完整性交叉验证
"""
import sys, os, shutil, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app, db, BACKUP_DIR, DB_PATH
from flask import g
from models import (User, Registration, Customer, MedicalRecord, ChargeRecord,
                    Appointment, Employee, ServiceItem)
from database import backup_db, restore_db, list_backups
from datetime import datetime

PASS, FAIL = 0, 0
def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")

def login_as(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)

def fresh_client(uid=None):
    """创建独立测试客户端。
    Flask-Login 将当前用户缓存在 g._login_user（g 绑定外层 app context），
    同一外层 context 下多个 client 会互相污染，故每次清除缓存。
    """
    g.pop('_login_user', None)
    c = app.test_client()
    if uid is not None:
        with c.session_transaction() as sess:
            sess['_user_id'] = str(uid)
    return c

def get_csrf(client):
    client.get('/system')
    with client.session_transaction() as sess:
        return sess.get('_csrf_token', '')

# 备份原始数据库，测试后还原
ORIG_DB_BAK = DB_PATH + '.audit_original'

def main():
    global PASS, FAIL
    print("=" * 64)
    print("系统综合审查测试 (备份恢复 / 多角色权限 / 业务流程)")
    print("=" * 64)

    # 一致性快照原始库（含 WAL 数据），避免 shutil.copy2 遗漏近期数据
    import sqlite3 as _sql
    _s = _sql.connect(DB_PATH); _d = _sql.connect(ORIG_DB_BAK)
    with _d: _s.backup(_d)
    _d.close(); _s.close()
    print(f"[准备] 原始数据库已一致性快照 -> {ORIG_DB_BAK}")

    with app.app_context():
        client = app.test_client()

        # ============================================================
        print("\n" + "─"*64)
        print("【模块一】数据备份与恢复")
        print("─"*64)

        # T1: 备份函数创建文件
        print("\n[T1] backup_db() 创建备份文件...")
        before = set(glob.glob(os.path.join(BACKUP_DIR, '*.db')))
        dest = backup_db(DB_PATH)
        ok(os.path.exists(dest), f"备份文件已创建: {os.path.basename(dest)}")
        ok(os.path.getsize(dest) > 0, f"备份文件非空 ({os.path.getsize(dest)} bytes)")
        ok(dest not in before, "新增了一个备份文件")
        created_backup = dest

        # T2: list_backups 列出备份
        print("\n[T2] list_backups() 列出备份...")
        lst = list_backups(BACKUP_DIR)
        names = [b['name'] for b in lst]
        ok(len(lst) > 0, f"备份列表非空 ({len(lst)}个)")
        ok(os.path.basename(created_backup) in names, "新备份出现在列表中")
        b0 = lst[0]
        ok(all(k in b0 for k in ('name','size','time')), "备份项含 name/size/time 字段")

        # T3: 备份路由（管理员）
        print("\n[T3] POST /system/backup (管理员)...")
        login_as(client, 1)
        csrf = get_csrf(client)
        n_before = len(glob.glob(os.path.join(BACKUP_DIR, '*.db')))
        resp = client.post('/system/backup', data={'_csrf_token': csrf}, follow_redirects=True)
        n_after = len(glob.glob(os.path.join(BACKUP_DIR, '*.db')))
        ok(resp.status_code == 200, f"请求成功 ({resp.status_code})")
        ok('备份成功' in resp.data.decode('utf-8'), "返回'备份成功'提示")
        ok(n_after > n_before, f"备份文件数量增加 ({n_before}->{n_after})")

        # T4: 系统页面展示备份列表
        print("\n[T4] GET /system 展示备份列表...")
        resp = client.get('/system')
        body = resp.data.decode('utf-8')
        ok(resp.status_code == 200, "系统页面可访问")
        ok('备份文件列表' in body, "包含'备份文件列表'区块")
        ok('立即备份' in body, "包含'立即备份'按钮")
        ok('恢复' in body, "包含'恢复'操作按钮")

        # T5: 备份/恢复功能验证（在临时数据库副本上进行，避免干扰 live engine）
        print("\n[T5] 备份/恢复功能验证 (临时库: 标记->备份->删改->恢复->验证还原)...")
        import sqlite3, tempfile
        tmp_dir = tempfile.mkdtemp(prefix='audit_restore_')
        tmp_db = os.path.join(tmp_dir, 'tmp.db')
        tmp_bk_dir = os.path.join(tmp_dir, 'backups')
        # 将 live 库一致性复制到临时库
        _s = sqlite3.connect(DB_PATH); _d = sqlite3.connect(tmp_db)
        with _d: _s.backup(_d)
        _d.close(); _s.close()
        # 在临时库插入标记
        _c = sqlite3.connect(tmp_db)
        _c.execute("INSERT INTO customers(name,phone) VALUES('审查标记客户_应被还原','13900000099')")
        _c.commit()
        marker_name = _c.execute("SELECT name FROM customers WHERE phone='13900000099'").fetchone()[0]
        _c.close()
        ok(marker_name == '审查标记客户_应被还原', "临时库已插入标记客户")
        # 备份含标记的临时库
        snap_with_marker = backup_db(tmp_db, tmp_bk_dir)
        ok(os.path.exists(snap_with_marker), f"临时库备份已创建: {os.path.basename(snap_with_marker)}")
        # 删除标记（模拟数据变更）
        _c = sqlite3.connect(tmp_db)
        _c.execute("DELETE FROM customers WHERE phone='13900000099'"); _c.commit()
        gone = _c.execute("SELECT COUNT(*) FROM customers WHERE phone='13900000099'").fetchone()[0]
        _c.close()
        ok(gone == 0, "标记客户已删除(模拟变更)")
        # 从含标记的备份恢复
        pre = restore_db(tmp_db, snap_with_marker)
        ok(os.path.exists(pre), f"恢复前自动备份: {os.path.basename(pre)}")
        ok(os.path.basename(pre).startswith('pre_restore_'), "pre_restore 备份命名正确")
        # 验证恢复后临时库已还原标记数据
        _c = sqlite3.connect(tmp_db)
        row2 = _c.execute("SELECT name FROM customers WHERE phone='13900000099'").fetchone()
        _c.close()
        ok(row2 is not None and '审查标记客户' in row2[0], "恢复后临时库已还原标记数据")
        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # snap_with_marker 现指向临时目录(已删)，改用真实库备份供 T6/T7 使用
        snap_with_marker = backup_db(DB_PATH)

        # 注：T6(恢复路由)与T7(路径穿越)为破坏性操作，移至所有非破坏性测试之后执行

        # ============================================================
        print("\n" + "─"*64)
        print("【模块二】多角色权限矩阵")
        print("─"*64)

        # 角色 -> 期望的系统管理访问权限
        # 管理员(1) 可访问；前台(2)、理疗师(3) 不可访问
        # 注：每个角色使用独立 fresh client，避免共享会话污染
        print("\n[T8] 系统管理页面角色访问控制...")
        role_cases = [(1,'管理员',True),(2,'前台',False),(3,'理疗师',False)]
        for uid, rname, allowed in role_cases:
            rc = fresh_client(uid)
            resp = rc.get('/system')
            if allowed:
                ok(resp.status_code == 200 and '系统管理' in resp.data.decode('utf-8'),
                   f"管理员可访问系统管理 ({resp.status_code})")
            else:
                ok(resp.status_code == 302,
                   f"{rname} 被拒绝访问系统管理 (302重定向)")

        print("\n[T9] 备份操作角色访问控制...")
        for uid, rname in [(2,'前台'),(3,'理疗师')]:
            rc = fresh_client(uid)
            resp = rc.post('/system/backup', data={}, follow_redirects=True)
            body = resp.data.decode('utf-8')
            ok('备份成功' not in body, f"{rname} 无法执行备份操作")

        print("\n[T10] 恢复操作角色访问控制...")
        for uid, rname in [(2,'前台'),(3,'理疗师')]:
            rc = fresh_client(uid)
            resp = rc.post('/system/restore',
                           data={'_csrf_token':'x','backup_file':os.path.basename(snap_with_marker)},
                           follow_redirects=True)
            body = resp.data.decode('utf-8')
            ok('数据恢复成功' not in body, f"{rname} 无法执行恢复操作")

        print("\n[T11] 未登录访问受保护页面...")
        c2 = fresh_client(None)
        resp = c2.get('/system')
        ok(resp.status_code == 302 and '/login' in resp.headers.get('Location',''),
           f"未登录访问/system被重定向到登录 ({resp.status_code})")

        print("\n[T12] CSRF防护验证 (管理员缺失token)...")
        # 403错误处理器对非API路径会重定向+flash，故验证“操作被拒绝且未产生备份”
        n_before = len(glob.glob(os.path.join(BACKUP_DIR, 'clinic_backup_*.db')))
        rc = fresh_client(1)
        resp = rc.post('/system/backup', data={})  # 无 csrf
        n_after = len(glob.glob(os.path.join(BACKUP_DIR, 'clinic_backup_*.db')))
        rejected = (resp.status_code == 403) or (resp.status_code == 302)
        ok(rejected and n_after == n_before,
           f"缺失CSRF被拒绝且未产生备份 (status={resp.status_code}, 备份数{n_before}->{n_after})")

        # ============================================================
        print("\n" + "─"*64)
        print("【模块三】业务流程完整性交叉验证")
        print("─"*64)

        print("\n[T13] 状态机流转完整性 (待诊→接诊中→已收费→已完成)...")
        cust = Customer.query.first()
        emp = Employee.query.filter_by(role='理疗师').first()
        if cust and emp:
            reg = Registration(customer_id=cust.id, employee_id=emp.id,
                               visit_type='初诊', status='待诊')
            db.session.add(reg); db.session.flush()
            rid = reg.id
            ok(db.session.get(Registration, rid).status == '待诊', "初始状态=待诊")
            # 接诊
            db.session.get(Registration, rid).status = '接诊中'
            db.session.commit()
            ok(db.session.get(Registration, rid).status == '接诊中', "挂号→接诊中")
            # 收费路由验证：接诊中可直接确认
            login_as(client, 1)
            csrf = get_csrf(client)
            resp = client.get(f'/registration/{rid}/confirm')
            ok(resp.status_code == 200, f"接诊中状态可进入确认页 ({resp.status_code})")
            db.session.delete(db.session.get(Registration, rid)); db.session.commit()
        else:
            ok(False, "缺少客户或理疗师测试数据")

        print("\n[T14] 非法状态拦截 (待诊状态不可签字确认)...")
        if cust and emp:
            reg = Registration(customer_id=cust.id, employee_id=emp.id, status='待诊')
            db.session.add(reg); db.session.flush()
            rid = reg.id
            login_as(client, 1)
            resp = client.get(f'/registration/{rid}/confirm', follow_redirects=True)
            ok('不可执行确认操作' in resp.data.decode('utf-8'), "待诊状态被拦截确认")
            db.session.delete(db.session.get(Registration, rid)); db.session.commit()

        print("\n[T15] 签字确认角色权限 (管理员/前台/理疗师均可)...")
        if cust and emp:
            reg = Registration(customer_id=cust.id, employee_id=emp.id, status='已收费')
            db.session.add(reg); db.session.flush()
            rid = reg.id
            for uid, rname in [(1,'管理员'),(2,'前台'),(3,'理疗师')]:
                login_as(client, uid)
                resp = client.get(f'/registration/{rid}/confirm')
                ok(resp.status_code == 200, f"{rname}可访问确认页 ({resp.status_code})")
            db.session.delete(db.session.get(Registration, rid)); db.session.commit()

        print("\n[T16] 签字确认联动预约状态同步...")
        if cust and emp:
            svc = ServiceItem.query.first()
            appt = Appointment(customer_id=cust.id, appt_date=datetime.now().date(),
                               time_slot='09:00-10:00', status='就诊中')
            if svc: appt.service_item_id = svc.id
            db.session.add(appt); db.session.flush()
            reg = Registration(customer_id=cust.id, employee_id=emp.id,
                               status='已收费', appointment_id=appt.id)
            db.session.add(reg); db.session.flush()
            rid, aid = reg.id, appt.id
            login_as(client, 1)
            csrf = get_csrf(client)
            fake_sig = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=='
            client.post(f'/registration/{rid}/confirm', data={
                '_csrf_token': csrf, 'signature_data': fake_sig,
                'satisfaction_rating':'5','satisfaction_comment':'ok'})
            db.session.expire_all()
            ok(db.session.get(Registration, rid).status == '已完成', "挂号→已完成")
            ok(db.session.get(Appointment, aid).status == '已完成', "关联预约同步→已完成")
            db.session.delete(db.session.get(Registration, rid))
            db.session.delete(db.session.get(Appointment, aid))
            db.session.commit()

        # ============================================================
        print("\n" + "─"*64)
        print("【模块四】破坏性恢复测试 (最后执行)")
        print("─"*64)

        # T6: 恢复路由（管理员）
        print("\n[T6] POST /system/restore (管理员)...")
        login_as(client, 1)
        csrf = get_csrf(client)
        target = os.path.basename(snap_with_marker)
        resp = client.post('/system/restore',
                           data={'_csrf_token': csrf, 'backup_file': target},
                           follow_redirects=True)
        body = resp.data.decode('utf-8')
        ok(resp.status_code == 200, f"请求成功 ({resp.status_code})")
        ok('数据恢复成功' in body, "返回'数据恢复成功'提示")

        # T7: 路径穿越防护
        print("\n[T7] 恢复路径穿越防护...")
        csrf = get_csrf(client)
        resp = client.post('/system/restore',
                           data={'_csrf_token': csrf, 'backup_file': '../../etc/passwd'},
                           follow_redirects=True)
        ok('非法的备份文件名' in resp.data.decode('utf-8'), "拦截 ../ 路径穿越")
        resp = client.post('/system/restore',
                           data={'_csrf_token': csrf, 'backup_file': 'not_exist.db'},
                           follow_redirects=True)
        ok('备份文件不存在' in resp.data.decode('utf-8'), "拦截不存在的备份文件")
        resp = client.post('/system/restore',
                           data={'_csrf_token': csrf, 'backup_file': ''},
                           follow_redirects=True)
        ok('请选择备份文件' in resp.data.decode('utf-8'), "拦截空备份文件名")

        # 释放连接池，便于后续文件级还原
        db.session.remove()
        db.engine.dispose()

    # ============================================================
    # 还原原始数据库
    print("\n" + "─"*64)
    print("[还原] 恢复原始数据库并清理测试备份...")
    shutil.copy2(ORIG_DB_BAK, DB_PATH)
    # 清理残留 WAL/SHM，使一致性快照成为权威数据源
    for sf in ('-wal', '-shm'):
        p = DB_PATH + sf
        if os.path.exists(p):
            try: os.remove(p)
            except OSError: pass
    os.remove(ORIG_DB_BAK)
    # 清理本次测试产生的备份文件
    for f in glob.glob(os.path.join(BACKUP_DIR, 'clinic_backup_*.db')):
        if '20260705_auto' not in f and '20260706_auto' not in f:
            os.remove(f)
    for f in glob.glob(os.path.join(BACKUP_DIR, 'pre_restore_*.db')):
        os.remove(f)
    print("  ✓ 数据库与备份目录已还原")

    print("\n" + "=" * 64)
    print(f"测试结果: 通过 {PASS} / 失败 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
