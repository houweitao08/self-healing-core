#!/usr/bin/env python3
"""
scenario_simulator.py — 内存泄漏故障场景模拟器

模拟 6 次健康检查，每次内存上涨，演示 L1 感知→预警→熔断的完整过程。

用法:
    python3 scenario_simulator.py            # 模拟并显示报告
    python3 scenario_simulator.py --verbose  # 模拟并运行 L1 auto 检查
    python3 scenario_simulator.py --clean    # 清理模拟数据
"""
import os
import sys
import sqlite3
import argparse
from datetime import datetime, timedelta

# 添加 heal_core.py 路径
sys.path.insert(0, os.path.expanduser("~/.hermes/skills/self-healing-core/scripts"))
from heal_core import HealCore, HealthSnapshot

DB_PATH = os.path.expanduser("~/.hermes/heal_core.db")


def simulate_memory_leak():
    """注入 6 条模拟快照，模拟内存从 45% 涨到 97%"""
    core = HealCore()
    memory_timeline = [45, 72, 88, 94, 96, 97]
    api_timeouts = [0, 0, 0, 3, 5, 8]

    for i, (mem, to) in enumerate(zip(memory_timeline, api_timeouts)):
        status = (
            "critical" if mem >= 90
            else "warning" if mem >= 75
            else "healthy"
        )
        level = (
            "critical" if mem >= 90
            else "warning" if mem >= 75
            else "healthy"
        )
        snapshot = HealthSnapshot(
            status=status,
            memory_percent=mem,
            disk_percent=25,
            api_timeout_count=to,
            failure_rate=to / 10,
            timestamp=(datetime.now() - timedelta(minutes=(5 - i) * 30)).isoformat(),
            details={"memory_level": level, "disk_level": "healthy"},
        )
        core.perceiver._log_snapshot(snapshot)

        # 记录 API 超时
        if to > 0:
            for _ in range(to):
                core.perceiver.log_timeout("external_api", 5000)

    return core


def show_report(core):
    """显示模拟后的完整报告"""
    report = core.get_report()
    print(f"\n{'='*50}")
    print(f"  内存泄漏模拟 — 健康报告")
    print(f"{'='*50}")
    print(f"\n📊 快照历史 ({len(report['latest_snapshots'])} 条):")
    print(f"  {'状态':<10} {'内存':<8} {'超时':<6} {'时间'}")
    print(f"  {'-'*40}")
    for s in report["latest_snapshots"]:
        icon = {"critical": "🔴", "warning": "🟡", "healthy": "🟢"}.get(s["status"], "⚪")
        print(f"  {icon} {s['status']:<8} {s['memory_percent']:>5.0f}%   {s['api_timeout_count']:>3}   {s['timestamp'][:19]}")

    cb = report["circuit_breaker"]
    print(f"\n🔒 熔断器:")
    print(f"  总操作: {cb['total_operations']}")
    print(f"  OPEN: {cb['open_count']}  HALF_OPEN: {cb['half_open_count']}")
    for op in cb.get("operations", []):
        icon = {"OPEN": "🔴", "HALF_OPEN": "🟡", "CLOSED": "🟢"}.get(op["state"], "⚪")
        print(f"  {icon} {op['operation']}: {op['state']} ({op['failures']} 次失败)")

    print(f"\n🔧 修复动作 ({len(report['recent_actions'])} 条):")
    for a in report["recent_actions"]:
        icon = "✅" if a["success"] else "❌"
        print(f"  {icon} [{a['action_type']}] {a['description']}")

    print()


def run_verbose_check(core):
    """运行一次 L1 verbose 检查，显示当前状态"""
    print(f"\n{'='*50}")
    print(f"  运行 L1 自愈检查 (verbose)")
    print(f"{'='*50}")
    result = core.run_cycle(mode="check")

    s = result["snapshot"]
    print(f"\n── 感知阶段 ──")
    print(f"  内存: {s.memory_percent:.0f}%  ({s.details.get('memory_level', '?')})")
    print(f"  磁盘: {s.disk_percent:.0f}%  ({s.details.get('disk_level', '?')})")
    print(f"  API超时(5min): {s.api_timeout_count}")
    print(f"  滑动失败率: {s.failure_rate:.1%}")
    print(f"  综合判定: {s.status.upper()}")

    cb = result.get("circuit_metrics", {})
    print(f"\n── 保护阶段 ──")
    print(f"  熔断器: {cb.get('state', '?')}")
    print(f"  允许执行: {'✅' if cb.get('allowed', True) else '❌'}")
    print(f"  历史失败: {cb.get('failure_count', 0)}")

    actions = result.get("actions", [])
    if actions:
        print(f"\n── 修复阶段 ({len(actions)} 个动作) ──")
        for a in actions:
            icon = "✅" if a.success else "❌"
            print(f"  {icon} [{a.action_type}] {a.description}")
    else:
        print(f"\n── 修复阶段 ──")
        print(f"  无需修复")

    print(f"\n── 耗时 ──")
    print(f"  {result.get('duration_ms', 0):.0f}ms")
    print()


def clean_data():
    """清理模拟数据"""
    if not os.path.exists(DB_PATH):
        print("没有数据需要清理。")
        return
    conn = sqlite3.connect(DB_PATH)
    for table in ["health_log", "api_timeout_log", "heal_actions"]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    print(f"✅ 已清理 {DB_PATH} 中的模拟数据。")


def main():
    parser = argparse.ArgumentParser(description="内存泄漏故障场景模拟器")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="模拟后运行 L1 verbose 检查")
    parser.add_argument("--clean", action="store_true",
                        help="清理模拟数据")
    args = parser.parse_args()

    if args.clean:
        clean_data()
        return

    core = simulate_memory_leak()

    if args.verbose:
        run_verbose_check(core)
    else:
        show_report(core)

    print("💡 提示: 用 --verbose 查看 L1 详细输出，用 --clean 清理数据")


if __name__ == "__main__":
    main()
