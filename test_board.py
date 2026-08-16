#!/usr/bin/env python3
"""排班看板功能测试脚本"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from models import SystemConfig, Appointment, Employee, Customer, StoreInfo
from datetime import date, datetime

def test_board():
    print("=" * 60)
    print("排班看板功能测试")
    print("=" * 60)

    with app.app_context():
        # === Test 1: 检查看板配置项是否自动创建 ===
        print("\n[Test 1] 检查看板配置项自动创建...")
        board_configs = ['board_theme', 'board_view_mode', 'board_refresh_interval', 'board_mask_customer_name']
        for key in board_configs:
            cfg = SystemConfig.query.filter_by(key=key).first()
            assert cfg is not None, f"配置项 {key} 未创建"
            print(f"  ✓ {key} = {cfg.value} ({cfg.description})")

        # === Test 2: 无token访问看板页面（应返回403） ===
        print("\n[Test 2] 无token访问看板（应403）...")
        client = app.test_client()
        resp = client.get('/board/schedule')
        assert resp.status_code == 403, f"预期403，实际{resp.status_code}"
        print(f"  ✓ 无token返回403")

        # === Test 3: 错误token访问（应返回403） ===
        print("\n[Test 3] 错误token访问（应403）...")
        resp = client.get('/board/schedule?token=invalid_token_123')
        assert resp.status_code == 403, f"预期403，实际{resp.status_code}"
        print(f"  ✓ 错误token返回403")

        # === Test 4: 生成有效token ===
        print("\n[Test 4] 生成有效看板令牌...")
        import secrets
        test_token = secrets.token_urlsafe(24)
        cfg = SystemConfig.query.filter_by(key='board_token').first()
        if not cfg:
            cfg = SystemConfig(key='board_token', value=test_token, description='排班看板访问令牌')
            db.session.add(cfg)
        else:
            cfg.value = test_token
        db.session.commit()
        print(f"  ✓ 令牌已生成: {test_token[:16]}...")

        # === Test 5: 有效token访问看板页面 ===
        print("\n[Test 5] 有效token访问看板页面...")
        resp = client.get(f'/board/schedule?token={test_token}')
        assert resp.status_code == 200, f"预期200，实际{resp.status_code}"
        body = resp.data.decode('utf-8')
        assert '排班看板' in body, "页面缺少'排班看板'标题"
        assert 'timeline-container' in body or 'boardContent' in body, "页面缺少看板容器"
        print(f"  ✓ 有效token返回200，页面包含看板内容")

        # === Test 6: API无token访问（应403） ===
        print("\n[Test 6] API无token访问（应403）...")
        resp = client.get('/api/board/schedule')
        assert resp.status_code == 403, f"预期403，实际{resp.status_code}"
        print(f"  ✓ API无token返回403")

        # === Test 7: API有效token访问 ===
        print("\n[Test 7] API有效token访问...")
        resp = client.get(f'/api/board/schedule?token={test_token}')
        assert resp.status_code == 200, f"预期200，实际{resp.status_code}"
        data = resp.get_json()
        assert 'date' in data, "API缺少date字段"
        assert 'weekday' in data, "API缺少weekday字段"
        assert 'appointments' in data, "API缺少appointments字段"
        assert 'stats' in data, "API缺少stats字段"
        stats = data['stats']
        assert 'today_total' in stats, "stats缺少today_total"
        assert 'today_pending' in stats, "stats缺少today_pending"
        assert 'today_done' in stats, "stats缺少today_done"
        assert 'week_total' in stats, "stats缺少week_total"
        print(f"  ✓ API返回JSON: date={data['date']}, weekday={data['weekday']}")
        print(f"    今日预约: {stats['today_total']}, 待服务: {stats['today_pending']}, 已完成: {stats['today_done']}")
        print(f"    本周合计: {stats['week_total']}, 在职员工: {stats['employees_count']}")

        # === Test 8: API指定日期查询 ===
        print("\n[Test 8] API指定日期查询...")
        resp = client.get(f'/api/board/schedule?token={test_token}&date=2026-07-28')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['date'] == '2026-07-28', f"日期不匹配: {data['date']}"
        print(f"  ✓ 指定日期查询: {data['date']} ({data['weekday']})")

        # === Test 9: 数据脱敏验证 ===
        print("\n[Test 9] 数据脱敏验证...")
        mask_cfg = SystemConfig.query.filter_by(key='board_mask_customer_name').first()
        assert mask_cfg is not None
        print(f"  ✓ 脱敏配置: {mask_cfg.value} ({'开启' if mask_cfg.value == '1' else '关闭'})")

        # 创建测试预约数据来验证脱敏
        employees = Employee.query.filter(Employee.is_active == True).all()
        customers = Customer.query.limit(2).all()
        if employees and customers:
            test_appt = Appointment(
                customer_id=customers[0].id,
                employee_id=employees[0].id,
                appt_date=date.today(),
                time_slot='10:00-11:00',
                status='待确认',
                remark='看板测试'
            )
            db.session.add(test_appt)
            db.session.commit()
            print(f"  ✓ 创建测试预约: {customers[0].name} -> {employees[0].name}")

            # 查询API验证脱敏
            resp = client.get(f'/api/board/schedule?token={test_token}')
            data = resp.get_json()
            for appt in data['appointments']:
                if appt.get('note') == '看板测试':
                    masked_name = appt['customer_name']
                    real_name = customers[0].name
                    assert masked_name != real_name, f"脱敏失败: {masked_name} == {real_name}"
                    assert masked_name[0] == real_name[0], f"脱敏姓不匹配: {masked_name[0]} != {real_name[0]}"
                    assert '**' in masked_name, f"脱敏格式错误: {masked_name}"
                    print(f"  ✓ 姓名脱敏: {real_name} -> {masked_name}")

                    # 验证不暴露电话
                    assert appt['customer_phone'] == '', f"电话不应暴露: {appt['customer_phone']}"
                    print(f"  ✓ 电话未暴露")
                    break

            # 清理测试数据
            db.session.delete(test_appt)
            db.session.commit()
            print(f"  ✓ 测试数据已清理")

        # === Test 10: X-Frame-Options验证 ===
        print("\n[Test 10] 安全头验证...")
        resp = client.get(f'/board/schedule?token={test_token}')
        xfo = resp.headers.get('X-Frame-Options', '')
        print(f"  看板页 X-Frame-Options: {xfo}")
        assert xfo == 'ALLOWALL', f"看板页应允许嵌入iframe，实际: {xfo}"
        print(f"  ✓ 看板页允许iframe嵌入")

        resp = client.get('/login')
        xfo = resp.headers.get('X-Frame-Options', '')
        assert xfo == 'SAMEORIGIN', f"普通页面应SAMEORIGIN，实际: {xfo}"
        print(f"  ✓ 普通页面 X-Frame-Options: SAMEORIGIN")

        # === Test 11: 三种视图渲染检查 ===
        print("\n[Test 11] 看板模板结构验证...")
        resp = client.get(f'/board/schedule?token={test_token}')
        body = resp.data.decode('utf-8')
        checks = [
            ('board-header', '页头'),
            ('board-toolbar', '工具栏'),
            ('boardContent', '内容区'),
            ('btn-timeline', '时间线按钮'),
            ('btn-matrix', '矩阵按钮'),
            ('btn-list', '列表按钮'),
            ('themeBtn', '主题切换'),
            ('refreshProgress', '刷新进度条'),
            ('statTotal', '统计-总数'),
            ('statPending', '统计-待服务'),
            ('statDone', '统计-已完成'),
            ('statWeek', '统计-本周'),
            ('toggleFullscreen', '全屏按钮'),
            ('clock', '时钟'),
        ]
        for elem_id, desc in checks:
            assert elem_id in body, f"缺少元素: {desc}({elem_id})"
            print(f"  ✓ {desc}")

        print("\n" + "=" * 60)
        print("全部测试通过 ✅")
        print("=" * 60)


if __name__ == '__main__':
    test_board()
