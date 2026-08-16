"""数据库版本升级与完整性校验编排模块

设计目标：
1. 数据库内置 schema_version 版本表，记录当前已应用的版本。
2. 每次启动调用 check_and_upgrade()：
   - 记录版本 == 程序版本  -> 直接跳过，不做任何初始化/迁移（快速路径）。
   - 记录版本 != 程序版本  -> 视为"新装"或"版本升级"，执行：
     a) 先备份当前库（已有数据时）
     b) 升级前完整性预检
     c) 建表 / 补列 / 补配置 / 补缺失内容（全部幂等，绝不覆盖已有数据）
     d) 升级后完整性复检 + 数据行数核验
     e) 写入新版本号
3. 核心安全原则：已有业务数据绝不被重新初始化或覆盖；仅补全缺失的表/列/配置/内容。
"""

import os
import sqlite3
from datetime import datetime

from database import (
    setup_engine, init_db, backup_db, integrity_check, validate_data_summary,
    _seed_core_if_empty, _ensure_default_configs, _ensure_board_configs,
    _seed_demo_if_empty,
)
from models import db

_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT NOT NULL,
    upgraded_at TEXT
)
"""


def _ensure_version_table(db_path):
    """确保版本表存在（用原生 sqlite3，早于 ORM 就绪）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_VERSION_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def get_recorded_version(db_path):
    """读取数据库记录的版本；无记录返回 None（全新库）。"""
    _ensure_version_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_recorded_version(db_path, version):
    """写入/更新版本记录。"""
    _ensure_version_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO schema_version (id, version, upgraded_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, upgraded_at=excluded.upgraded_at",
            (version, now))
        conn.commit()
    finally:
        conn.close()


def _version_tuple(v):
    """将版本字符串转为可比较的元组，非数字段按 0 处理。"""
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except Exception:
        return (0,)


def _db_has_business_data(db_path):
    """判断库中是否已有业务数据（用于区分真空库与无版本记录的遗留库）。
    用原生 sqlite3 检查 users 表是否存在且有记录。
    """
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if not row:
                return False
            cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return cnt > 0
        finally:
            conn.close()
    except Exception:
        return False


def check_and_upgrade(app, target_version, db_path):
    """启动时的版本检查与升级入口。

    返回 dict 报告，便于日志/诊断页面展示。
    """
    report = {
        'target_version': target_version,
        'recorded_version': None,
        'action': 'skipped',
        'backed_up': None,
        'pre_integrity': None,
        'post_integrity': None,
        'data_summary': None,
        'notes': [],
    }

    recorded = get_recorded_version(db_path)
    report['recorded_version'] = recorded

    # 无论版本是否一致，均需初始化连接层（WAL/外键 PRAGMA + 建表），幂等安全。
    setup_engine(app)

    # 版本一致：快速路径，不做任何迁移/播种
    if recorded == target_version:
        print(f'[升级检查] 数据库版本 v{recorded} 与程序版本一致，跳过升级。')
        report['action'] = 'skipped'
        return report

    is_fresh = (recorded is None) and (not _db_has_business_data(db_path))
    has_data = _db_has_business_data(db_path)
    label = '全新安装' if is_fresh else ('遗留库首次纳管' if recorded is None else '版本升级')
    print(f'[升级检查] 检测到{label}：'
          f'{"无记录" if recorded is None else recorded} -> v{target_version}')
    report['action'] = ('fresh_install' if is_fresh
                        else ('legacy_adopt' if recorded is None else 'upgraded'))

    # a) 已有数据时先备份（安全保障，含遗留库）
    if has_data and not is_fresh:
        try:
            pre_bak = backup_db(db_path)
            report['backed_up'] = os.path.basename(pre_bak)
            print(f'[升级检查] 升级前备份: {os.path.basename(pre_bak)}')
        except Exception as e:
            report['notes'].append(f'升级前备份失败: {e}')
            print(f'[升级检查] 警告：升级前备份失败 {e}')

    # b) 升级前完整性预检（仅对已有库）
    if has_data:
        ok, detail = integrity_check(db_path)
        report['pre_integrity'] = detail
        if not ok:
            report['notes'].append(f'升级前完整性异常: {detail}')
            print(f'[升级检查] 警告：升级前完整性检查未通过 {detail}')

    # c) 建表 + 补列（init_db 内含 create_all 与 _migrate_schema）
    init_db(app)

    # 幂等补全核心账号 / 配置项 / 看板配置 / 演示数据（均不覆盖已有数据）
    with app.app_context():
        core_seeded = _seed_core_if_empty()
        cfg_added = _ensure_default_configs()
        _ensure_board_configs()
        demo_seeded = _seed_demo_if_empty()
        db.session.commit()

        if core_seeded:
            report['notes'].append('已创建核心账号与门店信息（全新安装）')
        if cfg_added:
            report['notes'].append(f'已补全 {cfg_added} 个系统配置项')
        if demo_seeded:
            report['notes'].append('已补全演示数据: ' + '、'.join(demo_seeded))

    # d) 升级后完整性复检 + 数据行数核验
    ok, detail = integrity_check(db_path)
    report['post_integrity'] = detail
    with app.app_context():
        report['data_summary'] = validate_data_summary()
    if not ok:
        report['notes'].append(f'升级后完整性异常: {detail}')
        print(f'[升级检查] 错误：升级后完整性检查未通过 {detail}')

    # e) 写入新版本号
    set_recorded_version(db_path, target_version)
    print(f'[升级检查] 完成，数据库版本已更新为 v{target_version}')
    if report['data_summary']:
        print(f'[升级检查] 数据核验: {report["data_summary"]}')
    return report


if __name__ == '__main__':
    # 独立运行：python upgrade.py
    # 导入 app 会在模块加载时自动执行 check_and_upgrade（升级检查+验证），
    # 此处打印完整报告供人工确认。不会启动 Web 服务。
    import app as _app_module
    rep = getattr(_app_module, 'UPGRADE_REPORT', None)
    print('\n========== 升级/验证报告 ==========')
    if rep:
        print(f'目标版本   : v{rep["target_version"]}')
        print(f'原记录版本 : {rep["recorded_version"] or "无（全新安装）"}')
        print(f'执行动作   : {rep["action"]}')
        if rep['backed_up']:
            print(f'升级前备份 : {rep["backed_up"]}')
        if rep['pre_integrity']:
            print(f'升级前完整性: {rep["pre_integrity"]}')
        if rep['post_integrity']:
            print(f'升级后完整性: {rep["post_integrity"]}')
        if rep['data_summary']:
            print(f'数据行数   : {rep["data_summary"]}')
        for n in rep['notes']:
            print(f'备注       : {n}')
    else:
        print('未获取到升级报告。')
    print('=====================================')
