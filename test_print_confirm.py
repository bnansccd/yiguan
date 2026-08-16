#!/usr/bin/env python3
"""打印功能+签字确认全流程测试"""
import sys, os, json, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from models import (Registration, MedicalRecord, ChargeRecord, Customer,
                    Employee, ServiceItem, SystemConfig)
from datetime import date, datetime
from decimal import Decimal

def test_print_and_confirm():
    print("=" * 60)
    print("打印功能 + 签字确认 全流程测试")
    print("=" * 60)

    with app.app_context():
        client = app.test_client()

        # === 准备测试数据 ===
        print("\n[准备] 创建测试数据...")
        cust = Customer.query.first()
        emp = Employee.query.filter_by(role='理疗师').first()
        assert cust and emp, "需要至少一个客户和一个理疗师"

        # 创建挂号记录
        reg = Registration(customer_id=cust.id, employee_id=emp.id,
                          visit_type='初诊', status='接诊中')
        db.session.add(reg)
        db.session.flush()
        reg_id = reg.id
        print(f"  ✓ 挂号记录 #{reg_id}")

        # 创建病历
        mr = MedicalRecord(
            reg_id=reg_id,
            chief_complaint='测试主诉：肩颈酸痛',
            syndrome='气滞血瘀',
            treatment_plan='推拿+艾灸',
            prescription_items_json=json.dumps([
                {'id': '1', 'name': '全身推拿', 'qty': 1, 'price': 198, 'subtotal': 198}
            ], ensure_ascii=False),
            prescription_meds_json=json.dumps([
                {'id': '1', 'name': '活血止痛膏', 'qty': 2, 'unit': '盒', 'price': 25, 'subtotal': 50}
            ], ensure_ascii=False)
        )
        db.session.add(mr)
        db.session.flush()
        print(f"  ✓ 病历记录")

        # 创建收费记录
        charge = ChargeRecord(
            customer_id=cust.id, reg_id=reg_id,
            items_json=json.dumps([
                {'name': '全身推拿', 'qty': 1, 'price': 198, 'subtotal': 198},
                {'name': '活血止痛膏', 'qty': 2, 'price': 25, 'subtotal': 50}
            ], ensure_ascii=False),
            total_amount=Decimal('248'),
            discount_amount=Decimal('0'),
            final_amount=Decimal('248'),
            payments_json=json.dumps([{'method': '现金', 'amount': 248}]),
            status='已支付', operator_id=1
        )
        db.session.add(charge)
        reg.status = '已收费'
        db.session.commit()
        print(f"  ✓ 收费记录 ¥248")

        # === Test 1: 处方打印页面 ===
        print("\n[Test 1] 处方打印页面...")
        # 需要先登录（通过session hack）
        with client.session_transaction() as sess:
            sess['_user_id'] = '1'
        resp = client.get(f'/print/prescription/{reg_id}')
        assert resp.status_code == 200, f"预期200，实际{resp.status_code}"
        body = resp.data.decode('utf-8')
        assert '处方单' in body, "缺少处方单标题"
        assert 'print-area' in body, "缺少打印区域"
        assert 'window.print' in body, "缺少自动打印脚本"
        assert '全身推拿' in body, "缺少理疗项目名称"
        print("  ✓ 处方打印页面正常渲染，包含自动打印")

        # === Test 2: 收费小票打印页面 ===
        print("\n[Test 2] 收费小票打印页面...")
        resp = client.get(f'/print/receipt/{reg_id}')
        assert resp.status_code == 200, f"预期200，实际{resp.status_code}"
        body = resp.data.decode('utf-8')
        assert '收费小票' in body, "缺少小票标题"
        assert 'print-area' in body, "缺少打印区域"
        assert 'window.print' in body, "缺少自动打印脚本"
        assert '¥248' in body, "缺少金额"
        assert '现金' in body, "缺少支付方式"
        print("  ✓ 收费小票打印正常，金额¥248")

        # === Test 3: 签字确认页面GET ===
        print("\n[Test 3] 签字确认页面GET...")
        resp = client.get(f'/registration/{reg_id}/confirm')
        assert resp.status_code == 200, f"预期200，实际{resp.status_code}"
        body = resp.data.decode('utf-8')
        assert '治疗完成确认' in body, "缺少页面标题"
        assert 'signatureCanvas' in body, "缺少签字画布"
        assert 'starRating' in body or 'star-rating' in body, "缺少星级评分"
        assert 'satisfaction_rating' in body, "缺少满意度字段"
        print("  ✓ 签字确认页面包含画布+星级评分")

        # === Test 4: 签字确认POST（手写签字） ===
        print("\n[Test 4] 签字确认提交（手写签字Base64）...")
        # 先获取CSRF token
        resp = client.get(f'/registration/{reg_id}/confirm')
        with client.session_transaction() as sess:
            csrf = sess.get('_csrf_token', '')
        # 生成一个模拟签字图片（1x1白色像素PNG）
        fake_sig = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=='
        resp = client.post(f'/registration/{reg_id}/confirm', data={
            '_csrf_token': csrf,
            'signature_data': fake_sig,
            'satisfaction_rating': '5',
            'satisfaction_comment': '服务很好，下次还来',
        }, follow_redirects=True)
        assert resp.status_code == 200, f"提交失败: {resp.status_code}"

        # 验证数据已保存
        db.session.expire_all()
        reg = db.session.get(Registration, reg_id)
        assert reg.status == '已完成', f"状态应为已完成，实际{reg.status}"
        assert reg.signature_image == fake_sig, "签字图片未保存"
        assert reg.satisfaction_rating == 5, f"评分应为5，实际{reg.satisfaction_rating}"
        assert reg.satisfaction_comment == '服务很好，下次还来', "评价未保存"
        assert reg.confirmed_at is not None, "确认时间未记录"
        assert reg.confirmed_by == 1, "确认操作人未记录"
        print(f"  ✓ 签字确认完成: 状态={reg.status}, 评分={reg.satisfaction_rating}")

        # === Test 5: 无病历不能打印处方 ===
        print("\n[Test 5] 无病历时打印处方应拒绝...")
        reg2 = Registration(customer_id=cust.id, employee_id=emp.id,
                           visit_type='复诊', status='待诊')
        db.session.add(reg2)
        db.session.flush()
        resp = client.get(f'/print/prescription/{reg2.id}', follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode('utf-8')
        assert '尚未记录病历' in body or '无法打印' in body or '挂号接诊' in body
        print("  ✓ 无病历打印处方被正确拒绝")

        # === Test 6: 无收费不能打印小票 ===
        print("\n[Test 6] 无收费时打印小票应拒绝...")
        resp = client.get(f'/print/receipt/{reg2.id}', follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode('utf-8')
        assert '尚未收费' in body or '无法打印' in body or '挂号接诊' in body
        print("  ✓ 无收费打印小票被正确拒绝")

        # === Test 7: 已完成状态不能重复确认 ===
        print("\n[Test 7] 已完成状态不能重复确认...")
        resp = client.get(f'/registration/{reg_id}/confirm', follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode('utf-8')
        assert '不可执行' in body or '挂号接诊' in body
        print("  ✓ 已完成状态被正确拒绝重复确认")

        # === Test 8: 验证新列存在 ===
        print("\n[Test 8] 验证Registration模型新字段...")
        reg_check = db.session.get(Registration, reg_id)
        fields = ['signature_image', 'satisfaction_rating', 'satisfaction_comment',
                  'confirmed_at', 'confirmed_by']
        for f in fields:
            assert hasattr(reg_check, f), f"缺少字段: {f}"
            print(f"  ✓ {f} = {getattr(reg_check, f, '-')[:40] if isinstance(getattr(reg_check, f, ''), str) else getattr(reg_check, f, '-')}")

        # === 清理测试数据 ===
        print("\n[清理] 删除测试数据...")
        db.session.delete(charge)
        db.session.delete(mr)
        db.session.delete(reg)
        db.session.delete(reg2)
        db.session.commit()
        print("  ✓ 测试数据已清理")

        print("\n" + "=" * 60)
        print("全部测试通过 ✅")
        print("=" * 60)


if __name__ == '__main__':
    test_print_and_confirm()
