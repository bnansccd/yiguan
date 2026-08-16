"""数据库模型定义 - 昭德堂健康管理中心业务系统"""

from datetime import datetime, date
from decimal import Decimal
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ==================== 系统用户 ====================

class User(UserMixin, db.Model):
    """系统用户"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(50), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='前台')  # 管理员/前台/理疗师/库管
    is_active_user = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


# ==================== 基础信息 ====================

class StoreInfo(db.Model):
    """门店信息"""
    __tablename__ = 'store_info'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    license_no = db.Column(db.String(50))


class Employee(db.Model):
    """员工信息"""
    __tablename__ = 'employees'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), default='理疗师')
    hire_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    schedule_config = db.Column(db.Text)  # JSON: 排班配置 {"weekdays":[1,2,3,4,5]}
    appointments = db.relationship('Appointment', backref='employee', lazy='dynamic')
    registrations = db.relationship('Registration', backref='employee', lazy='dynamic')


class ServiceItem(db.Model):
    """理疗项目"""
    __tablename__ = 'service_items'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50))  # 推拿/艾灸/拔罐/刮痧/针灸/正骨
    price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(10, 2), default=0)  # 成本价
    duration = db.Column(db.Integer)  # 建议时长(分钟)
    indication = db.Column(db.Text)  # 适用症描述
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False)  # 软删除


class Medicine(db.Model):
    """药品/耗材字典"""
    __tablename__ = 'medicines'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    spec = db.Column(db.String(50))  # 规格
    unit = db.Column(db.String(20))  # 单位
    retail_price = db.Column(db.Numeric(10, 2), default=0)
    cost_price = db.Column(db.Numeric(10, 2), default=0)  # 采购成本价
    alert_threshold = db.Column(db.Integer, default=10)
    category = db.Column(db.String(50))  # 中药饮片/中成药/耗材
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False)  # 软删除

    inventory_ledgers = db.relationship('InventoryLedger', backref='medicine', lazy='dynamic')


class PrescriptionTemplate(db.Model):
    """处方模板"""
    __tablename__ = 'prescription_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    indication = db.Column(db.String(200))
    items_json = db.Column(db.Text)  # JSON: [{item_id, qty}]
    medicines_json = db.Column(db.Text)  # JSON: [{medicine_id, dosage, usage}]
    created_at = db.Column(db.DateTime, default=datetime.now)


# ==================== 客户管理 ====================

class Customer(db.Model):
    """客户档案"""
    __tablename__ = 'customers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(10))
    birthday = db.Column(db.Date)
    phone = db.Column(db.String(20))
    id_card = db.Column(db.String(20))
    address = db.Column(db.String(200))
    constitution_type = db.Column(db.String(20))  # 九种体质
    allergy_history = db.Column(db.Text)
    medical_history = db.Column(db.Text)
    referrer = db.Column(db.String(50))
    member_level = db.Column(db.String(20), default='普通')  # 普通/银卡/金卡/钻石
    member_discount = db.Column(db.Numeric(3, 2), default=1.00)
    points = db.Column(db.Integer, default=0)
    is_deleted = db.Column(db.Boolean, default=False)  # 软删除
    is_frozen = db.Column(db.Boolean, default=False)  # 冻结
    created_at = db.Column(db.DateTime, default=datetime.now)

    prepaid_account = db.relationship('PrepaidAccount', backref='customer', uselist=False)
    appointments = db.relationship('Appointment', backref='customer', lazy='dynamic')
    registrations = db.relationship('Registration', backref='customer', lazy='dynamic')
    charges = db.relationship('ChargeRecord', backref='customer', lazy='dynamic')


# ==================== 预约管理 ====================

class Appointment(db.Model):
    """预约记录"""
    __tablename__ = 'appointments'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'))
    appt_date = db.Column(db.Date, nullable=False)
    time_slot = db.Column(db.String(20))  # 如 "09:00-10:00"
    service_item_id = db.Column(db.Integer, db.ForeignKey('service_items.id'))
    source = db.Column(db.String(20), default='到店')  # 到店/电话/线上
    status = db.Column(db.String(20), default='待确认')  # 待确认/已确认/已签到/已取消/已完成/就诊中
    remark = db.Column(db.String(200))
    # 取消扣费配置（确认时设置）
    cancel_fee_enabled = db.Column(db.Boolean, default=False)  # 是否启用取消扣费
    cancel_fee_ratio = db.Column(db.Numeric(5, 2), default=0)  # 扣费比例(0-100%)
    created_at = db.Column(db.DateTime, default=datetime.now)

    service_item = db.relationship('ServiceItem')


# ==================== 挂号与接诊 ====================

class Registration(db.Model):
    """挂号记录"""
    __tablename__ = 'registrations'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'))
    reg_time = db.Column(db.DateTime, default=datetime.now)
    visit_type = db.Column(db.String(10), default='初诊')  # 初诊/复诊
    status = db.Column(db.String(20), default='待诊')  # 待诊/接诊中/已收费/已完成
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointments.id'))
    # 执行完成确认（签字+评价）
    signature_image = db.Column(db.Text)  # Base64编码的签字图片
    satisfaction_rating = db.Column(db.Integer)  # 满意度星级 1-5
    satisfaction_comment = db.Column(db.String(500))  # 评价文字
    confirmed_at = db.Column(db.DateTime)  # 签字确认时间
    confirmed_by = db.Column(db.Integer, db.ForeignKey('users.id'))  # 确认操作人

    appointment = db.relationship('Appointment')
    medical_record = db.relationship('MedicalRecord', backref='registration', uselist=False)
    charge_record = db.relationship('ChargeRecord', backref='registration', uselist=False)
    confirmer = db.relationship('User', foreign_keys=[confirmed_by])


class MedicalRecord(db.Model):
    """诊疗记录(病历)"""
    __tablename__ = 'medical_records'

    id = db.Column(db.Integer, primary_key=True)
    reg_id = db.Column(db.Integer, db.ForeignKey('registrations.id'), nullable=False)
    chief_complaint = db.Column(db.Text)  # 主诉
    present_illness = db.Column(db.Text)  # 现病史
    past_history = db.Column(db.Text)  # 既往史
    four_exam = db.Column(db.Text)  # 四诊信息(望闻问切)
    syndrome = db.Column(db.String(200))  # 辨证分型
    treatment_plan = db.Column(db.Text)  # 理疗方案
    prescription_items_json = db.Column(db.Text)  # JSON: 理疗项目明细
    prescription_meds_json = db.Column(db.Text)  # JSON: 药品明细
    attachments = db.Column(db.Text)  # 附件路径
    created_at = db.Column(db.DateTime, default=datetime.now)


# ==================== 收费管理 ====================

class ChargeRecord(db.Model):
    """收费记录"""
    __tablename__ = 'charge_records'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    reg_id = db.Column(db.Integer, db.ForeignKey('registrations.id'))
    items_json = db.Column(db.Text)  # JSON: [{name, qty, price, subtotal}]
    total_amount = db.Column(db.Numeric(10, 2), default=0)
    discount_amount = db.Column(db.Numeric(10, 2), default=0)
    final_amount = db.Column(db.Numeric(10, 2), default=0)
    payments_json = db.Column(db.Text)  # JSON: [{method, amount}]
    status = db.Column(db.String(20), default='待支付')  # 待支付/已支付/已退款/部分退款
    refund_amount = db.Column(db.Numeric(10, 2), default=0)  # 退款金额
    refund_reason = db.Column(db.String(200))  # 退款原因
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)

    operator = db.relationship('User')
    deductions = db.relationship('DeductionRecord', backref='charge', lazy='dynamic')


# ==================== 预充值管理 ====================

class PrepaidAccount(db.Model):
    """预充值账户"""
    __tablename__ = 'prepaid_accounts'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), unique=True, nullable=False)
    balance = db.Column(db.Numeric(12, 2), default=0)
    total_recharge = db.Column(db.Numeric(12, 2), default=0)
    total_consumed = db.Column(db.Numeric(12, 2), default=0)
    total_gift = db.Column(db.Numeric(12, 2), default=0)
    status = db.Column(db.String(20), default='正常')  # 正常/冻结
    version = db.Column(db.Integer, default=0)  # 乐观锁版本号


class RechargeRecord(db.Model):
    """充值记录"""
    __tablename__ = 'recharge_records'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    gift_amount = db.Column(db.Numeric(10, 2), default=0)
    payment_method = db.Column(db.String(20))  # 现金/微信/支付宝
    package_name = db.Column(db.String(50))
    remark = db.Column(db.String(200))
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship('Customer')
    operator = db.relationship('User')


class DeductionRecord(db.Model):
    """消费扣费记录"""
    __tablename__ = 'deduction_records'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    charge_id = db.Column(db.Integer, db.ForeignKey('charge_records.id'))
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    deduction_type = db.Column(db.String(20), default='金额')  # 金额/次数
    balance_after = db.Column(db.Numeric(12, 2))
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship('Customer')


class PrepaidRefund(db.Model):
    """充值退款记录"""
    __tablename__ = 'prepaid_refunds'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    reason = db.Column(db.String(200))
    approver_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship('Customer')


# ==================== 库存管理 ====================

class InventoryLedger(db.Model):
    """库存台账"""
    __tablename__ = 'inventory_ledgers'

    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicines.id'), nullable=False)
    batch_no = db.Column(db.String(50))
    production_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date)
    in_qty = db.Column(db.Integer, default=0)
    out_qty = db.Column(db.Integer, default=0)
    current_qty = db.Column(db.Integer, default=0)
    unit_price = db.Column(db.Numeric(10, 2), default=0)
    supplier = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.now)

    stock_flows = db.relationship('StockFlow', backref='ledger', lazy='dynamic')


class StockFlow(db.Model):
    """出入库流水"""
    __tablename__ = 'stock_flows'

    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicines.id'), nullable=False)
    ledger_id = db.Column(db.Integer, db.ForeignKey('inventory_ledgers.id'))
    flow_type = db.Column(db.String(10), nullable=False)  # 入库/出库
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), default=0)
    ref_no = db.Column(db.String(50))  # 关联单号
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    remark = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.now)

    medicine = db.relationship('Medicine')
    operator = db.relationship('User')


# ==================== 年卡管理 ====================

class AnnualCardTemplate(db.Model):
    """年卡模板（定义可售年卡类型）"""
    __tablename__ = 'annual_card_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)  # 说明
    price = db.Column(db.Numeric(10, 2), nullable=False)  # 售价
    valid_days = db.Column(db.Integer, default=365)  # 有效天数
    # JSON: [{service_item_id, name, total_times}]
    services_json = db.Column(db.Text)
    extra_discount = db.Column(db.Numeric(3, 2), default=1.00)  # 额外项目折扣率
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CustomerAnnualCard(db.Model):
    """客户年卡（购买记录）"""
    __tablename__ = 'customer_annual_cards'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('annual_card_templates.id'), nullable=False)
    purchase_date = db.Column(db.Date, default=date.today, nullable=False)
    expire_date = db.Column(db.Date, nullable=False)
    purchase_price = db.Column(db.Numeric(10, 2), default=0)
    # JSON: {service_item_id_str: remaining_times}
    remaining_times_json = db.Column(db.Text)
    status = db.Column(db.String(20), default='正常')  # 正常/冻结/过期
    remark = db.Column(db.String(200))
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship('Customer', backref='annual_cards')
    template = db.relationship('AnnualCardTemplate')
    operator = db.relationship('User')
    usages = db.relationship('AnnualCardUsage', backref='card', lazy='dynamic')


class AnnualCardUsage(db.Model):
    """年卡使用/核销记录"""
    __tablename__ = 'annual_card_usages'

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey('customer_annual_cards.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    service_item_id = db.Column(db.Integer, db.ForeignKey('service_items.id'))
    charge_id = db.Column(db.Integer, db.ForeignKey('charge_records.id'))
    used_times = db.Column(db.Integer, default=1)
    remaining_after = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    customer = db.relationship('Customer')
    service_item = db.relationship('ServiceItem')
    operator = db.relationship('User')


# ==================== 经营报表 ====================

class DailyReport(db.Model):
    """经营日报"""
    __tablename__ = 'daily_reports'

    id = db.Column(db.Integer, primary_key=True)
    report_date = db.Column(db.Date, unique=True, nullable=False)
    revenue = db.Column(db.Numeric(12, 2), default=0)
    customer_count = db.Column(db.Integer, default=0)
    recharge_income = db.Column(db.Numeric(12, 2), default=0)
    recharge_consumed = db.Column(db.Numeric(12, 2), default=0)
    refund_amount = db.Column(db.Numeric(12, 2), default=0)
    item_stats_json = db.Column(db.Text)  # JSON: [{item_name, count, revenue}]


# ==================== 系统日志 ====================

class SystemLog(db.Model):
    """系统操作日志"""
    __tablename__ = 'system_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(50), nullable=False)  # 收费/退费/入库/出库/充值/扣费/删除
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User')


# ==================== 系统参数 ====================

class SystemConfig(db.Model):
    """系统参数配置"""
    __tablename__ = 'system_configs'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500))
    description = db.Column(db.String(200))


class StockTake(db.Model):
    """库存盘点记录"""
    __tablename__ = 'stock_takes'

    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicines.id'), nullable=False)
    book_qty = db.Column(db.Integer, default=0)  # 账面数量
    actual_qty = db.Column(db.Integer, default=0)  # 实盘数量
    diff_qty = db.Column(db.Integer, default=0)  # 差异(正=盈，负=亏)
    status = db.Column(db.String(20), default='待审核')  # 待审核/已审核
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    approver_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    remark = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.now)

    medicine = db.relationship('Medicine')
    operator = db.relationship('User', foreign_keys=[operator_id])
    approver = db.relationship('User', foreign_keys=[approver_id])


# ==================== 入库单 ====================

class InboundOrder(db.Model):
    """入库单"""
    __tablename__ = 'inbound_orders'

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(50), unique=True, nullable=False)  # 入库单号
    supplier = db.Column(db.String(100))  # 供应商
    total_amount = db.Column(db.Numeric(12, 2), default=0)  # 采购总金额
    status = db.Column(db.String(20), default='待确认')  # 待确认/已入库/已取消
    remark = db.Column(db.String(200))
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)
    confirmed_at = db.Column(db.DateTime)

    operator = db.relationship('User')
    items = db.relationship('InboundOrderItem', backref='order', lazy='select',
                            cascade='all, delete-orphan')


class InboundOrderItem(db.Model):
    """入库单明细"""
    __tablename__ = 'inbound_order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('inbound_orders.id'), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey('medicines.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    unit_price = db.Column(db.Numeric(10, 2), default=0)  # 采购单价
    batch_no = db.Column(db.String(50))
    production_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date)

    medicine = db.relationship('Medicine')
