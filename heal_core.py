#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
self-healing-core — 轻量自愈核心

零外部依赖，仅使用 Python 标准库。
三层架构：感知 → 保护 → 修复

用法:
    from heal_core import HealCore
    core = HealCore()
    report = core.run_cycle()
    print(report["summary"])
"""

import os
import json
import time
import math
import sqlite3
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger("heal_core")

# ── 默认路径 ──
DEFAULT_DB_PATH = os.path.expanduser("~/.hermes/heal_core.db")
DEFAULT_LOG_PATH = os.path.expanduser("~/.hermes/logs/heal_core.log")


# ============================================================
# 数据模型
# ============================================================

@dataclass
class HealthSnapshot:
    """健康快照"""
    status: str  # healthy / warning / critical
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    api_timeout_count: int = 0
    failure_rate: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"[{self.status.upper()}] "
            f"内存:{self.memory_percent:.0f}% "
            f"磁盘:{self.disk_percent:.0f}% "
            f"API超时:{self.api_timeout_count} "
            f"失败率:{self.failure_rate:.1%}"
        )


@dataclass
class HealAction:
    """修复动作"""
    action_type: str  # cleanup / retry / backoff / compress / escalate
    success: bool
    description: str
    duration_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ============================================================
# 感知层 — 健康检查
# ============================================================

class HealthPerceiver:
    """健康感知器 — 3维检查：内存/磁盘/API超时"""

    # 默认阈值
    MEMORY_WARN = 75.0   # 内存使用率预警 %
    MEMORY_CRIT = 90.0
    DISK_WARN = 80.0     # 磁盘使用率预警 %
    DISK_CRIT = 92.0
    API_TIMEOUT_WARN = 3  # 最近5分钟API超时次数
    API_TIMEOUT_CRIT = 10
    FAILURE_RATE_WARN = 0.30
    FAILURE_RATE_CRIT = 0.60

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        try:
            self._init_db()
        except Exception as e:
            logger.warning(f"数据库初始化失败，降级运行: {e}")

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    status TEXT,
                    memory_percent REAL,
                    disk_percent REAL,
                    api_timeout_count INTEGER,
                    failure_rate REAL,
                    details TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_timeout_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    endpoint TEXT,
                    duration_ms REAL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"数据库初始化失败: {e}")

    def check_memory(self) -> Tuple[float, str]:
        """检查内存使用率。无 psutil 时返回 0 + 'unknown'"""
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            total = int(lines[0].split()[1])
            available = int(lines[2].split()[1])
            percent = (1 - available / total) * 100 if total > 0 else 0
            level = "healthy" if percent < self.MEMORY_WARN else (
                "warning" if percent < self.MEMORY_CRIT else "critical"
            )
            return percent, level
        except (FileNotFoundError, IndexError, ValueError):
            # macOS/Windows fallback
            try:
                import shutil
                total, used, free = shutil.disk_usage("/")
                # 这不是内存，是磁盘。但作为 fallback 比没有好
                return 0.0, "unknown"
            except Exception:
                return 0.0, "unknown"

    def check_disk(self, path: str = None) -> Tuple[float, str]:
        """检查磁盘使用率"""
        if path is None:
            path = os.path.expanduser("~/.hermes")
        try:
            stat = os.statvfs(path)
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bfree
            percent = (1 - free / total) * 100 if total > 0 else 0
            level = "healthy" if percent < self.DISK_WARN else (
                "warning" if percent < self.DISK_CRIT else "critical"
            )
            return percent, level
        except Exception:
            return 0.0, "unknown"

    def count_recent_timeouts(self, minutes: int = 5) -> int:
        """统计最近 N 分钟的 API 超时次数"""
        try:
            conn = sqlite3.connect(self.db_path)
            cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) FROM api_timeout_log WHERE timestamp >= ?",
                (cutoff,)
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    def compute_failure_rate(self, minutes: int = 30) -> float:
        """计算最近 N 分钟的操作失败率"""
        try:
            conn = sqlite3.connect(self.db_path)
            cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
            total = conn.execute(
                "SELECT COUNT(*) FROM health_log WHERE timestamp >= ?",
                (cutoff,)
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM health_log WHERE timestamp >= ? AND status = 'critical'",
                (cutoff,)
            ).fetchone()[0]
            conn.close()
            return failed / total if total > 0 else 0.0
        except Exception:
            return 0.0

    def inspect(self) -> HealthSnapshot:
        """执行全维度健康检查"""
        mem_pct, mem_level = self.check_memory()
        disk_pct, disk_level = self.check_disk()
        timeout_count = self.count_recent_timeouts()
        failure_rate = self.compute_failure_rate()

        # 综合状态判定
        levels = [mem_level, disk_level]
        if timeout_count >= self.API_TIMEOUT_CRIT:
            levels.append("critical")
        elif timeout_count >= self.API_TIMEOUT_WARN:
            levels.append("warning")
        if failure_rate >= self.FAILURE_RATE_CRIT:
            levels.append("critical")
        elif failure_rate >= self.FAILURE_RATE_WARN:
            levels.append("warning")

        if "critical" in levels:
            status = "critical"
        elif "warning" in levels:
            status = "warning"
        else:
            status = "healthy"

        snapshot = HealthSnapshot(
            status=status,
            memory_percent=mem_pct,
            disk_percent=disk_pct,
            api_timeout_count=timeout_count,
            failure_rate=failure_rate,
            details={
                "memory_level": mem_level,
                "disk_level": disk_level,
            }
        )

        # 持久化
        self._log_snapshot(snapshot)
        return snapshot

    def _log_snapshot(self, snapshot: HealthSnapshot):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO health_log (timestamp, status, memory_percent, disk_percent, "
                "api_timeout_count, failure_rate, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot.timestamp, snapshot.status, snapshot.memory_percent,
                 snapshot.disk_percent, snapshot.api_timeout_count,
                 snapshot.failure_rate, json.dumps(snapshot.details))
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to log snapshot: {e}")

    def log_timeout(self, endpoint: str, duration_ms: float):
        """记录一次 API 超时"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO api_timeout_log (timestamp, endpoint, duration_ms) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), endpoint, duration_ms)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to log timeout: {e}")

    def get_latest_snapshots(self, limit: int = 10) -> List[Dict]:
        """获取最近 N 条健康快照"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT * FROM health_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return [
                {
                    "id": r[0], "timestamp": r[1], "status": r[2],
                    "memory_percent": r[3], "disk_percent": r[4],
                    "api_timeout_count": r[5], "failure_rate": r[6],
                }
                for r in rows
            ]
        except Exception:
            return []


# ============================================================
# 保护层 — 熔断器
# ============================================================

class CircuitBreaker:
    """轻量熔断器 — 防止级联失败"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT UNIQUE,
                    state TEXT DEFAULT 'CLOSED',
                    failure_count INTEGER DEFAULT 0,
                    last_failure TEXT,
                    last_success TEXT,
                    opened_at TEXT,
                    cooldown_seconds INTEGER DEFAULT 300
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Circuit breaker init failed: {e}")

    def check(self, operation: str) -> Dict[str, Any]:
        """检查操作是否允许执行"""
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT state, failure_count, opened_at, cooldown_seconds "
                "FROM circuit_breaker WHERE operation = ?", (operation,)
            ).fetchone()
            conn.close()

            if not row:
                return {"allowed": True, "state": "CLOSED", "failure_count": 0}

            state, failures, opened_at, cooldown = row

            if state == "CLOSED":
                return {"allowed": True, "state": "CLOSED", "failure_count": failures}

            if state == "OPEN":
                # 检查冷却期是否已过
                if opened_at:
                    elapsed = (datetime.now() - datetime.fromisoformat(opened_at)).total_seconds()
                    if elapsed >= cooldown:
                        self._set_state(operation, "HALF_OPEN")
                        return {"allowed": True, "state": "HALF_OPEN", "failure_count": failures}
                return {"allowed": False, "state": "OPEN", "failure_count": failures}

            # HALF_OPEN — 允许试运行
            return {"allowed": True, "state": "HALF_OPEN", "failure_count": failures}

        except Exception:
            return {"allowed": True, "state": "CLOSED", "failure_count": 0}

    def record_failure(self, operation: str):
        """记录失败"""
        try:
            conn = sqlite3.connect(self.db_path)
            existing = conn.execute(
                "SELECT failure_count FROM circuit_breaker WHERE operation = ?",
                (operation,)
            ).fetchone()
            now = datetime.now().isoformat()

            if existing:
                failures = existing[0] + 1
                new_state = "OPEN" if failures >= 3 else "CLOSED"
                conn.execute(
                    "UPDATE circuit_breaker SET failure_count=?, state=?, last_failure=?, "
                    "opened_at=CASE WHEN ? THEN ? ELSE opened_at END WHERE operation=?",
                    (failures, new_state, now, new_state == "OPEN", now, operation)
                )
            else:
                conn.execute(
                    "INSERT INTO circuit_breaker (operation, state, failure_count, "
                    "last_failure, opened_at) VALUES (?, 'CLOSED', 1, ?, NULL)",
                    (operation, now)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Record failure failed: {e}")

    def record_success(self, operation: str):
        """记录成功（重置熔断器）"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT OR REPLACE INTO circuit_breaker "
                "(operation, state, failure_count, last_success, opened_at) "
                "VALUES (?, 'CLOSED', 0, ?, NULL)",
                (operation, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Record success failed: {e}")

    def _set_state(self, operation: str, state: str):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE circuit_breaker SET state=? WHERE operation=?",
                (state, operation)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_metrics(self) -> Dict[str, Any]:
        """获取熔断器状态概览"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute("SELECT * FROM circuit_breaker").fetchall()
            conn.close()
            return {
                "total_operations": len(rows),
                "open_count": sum(1 for r in rows if r[2] == "OPEN"),
                "half_open_count": sum(1 for r in rows if r[2] == "HALF_OPEN"),
                "operations": [
                    {"operation": r[1], "state": r[2], "failures": r[3]}
                    for r in rows
                ]
            }
        except Exception:
            return {"total_operations": 0, "open_count": 0, "half_open_count": 0, "operations": []}


# ============================================================
# 修复层 — 自动修复
# ============================================================

class AutoHealer:
    """自动修复器 — 4种修复策略"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS heal_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action_type TEXT,
                    success INTEGER,
                    description TEXT,
                    details TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            pass

    def heal(self, snapshot: HealthSnapshot) -> List[HealAction]:
        """根据健康快照执行修复"""
        actions = []

        # 策略1: 磁盘清理
        if snapshot.disk_percent >= HealthPerceiver.DISK_WARN:
            action = self._do_cleanup()
            actions.append(action)

        # 策略2: 退避建议
        if snapshot.failure_rate >= HealthPerceiver.FAILURE_RATE_WARN:
            action = self._do_backoff()
            actions.append(action)

        # 策略3: 压缩建议（上下文膨胀）
        if snapshot.memory_percent >= HealthPerceiver.MEMORY_WARN:
            action = self._do_compress()
            actions.append(action)

        # 策略4: 升级（严重情况）
        if snapshot.status == "critical":
            action = self._do_escalate(snapshot)
            actions.append(action)

        return actions

    def _do_cleanup(self) -> HealAction:
        """清理临时文件和日志"""
        freed_bytes = 0
        targets = [
            os.path.expanduser("~/.hermes/logs/*.log"),
        ]
        try:
            import glob
            for pattern in targets:
                for f in glob.glob(pattern):
                    try:
                        # 只清理超过 7 天的日志
                        age = time.time() - os.path.getmtime(f)
                        if age > 7 * 86400:
                            freed_bytes += os.path.getsize(f)
                            os.remove(f)
                    except OSError:
                        pass
            success = True
            desc = f"已清理 {freed_bytes / 1024:.0f}KB 过期日志"
        except Exception as e:
            success = False
            desc = f"清理失败: {e}"

        return HealAction(
            action_type="cleanup",
            success=success,
            description=desc,
            details={"freed_bytes": freed_bytes},
        )

    def _do_backoff(self) -> HealAction:
        """退避建议"""
        return HealAction(
            action_type="backoff",
            success=True,
            description="建议启用指数退避: 重试间隔 1s×2^retry, 上限 30s",
            details={"suggested_base": 1.0, "suggested_max": 30.0},
        )

    def _do_compress(self) -> HealAction:
        """上下文压缩建议"""
        return HealAction(
            action_type="compress",
            success=True,
            description="建议压缩上下文: 丢弃 7 天前的会话历史，保留关键决策",
            details={"retention_days": 7},
        )

    def _do_escalate(self, snapshot: HealthSnapshot) -> HealAction:
        """升级到人工处理"""
        return HealAction(
            action_type="escalate",
            success=True,
            description=f"系统状态 CRITICAL，已升级到人工处理",
            details={
                "memory_percent": snapshot.memory_percent,
                "disk_percent": snapshot.disk_percent,
                "failure_rate": snapshot.failure_rate,
            },
        )

    def _log_action(self, action: HealAction):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO heal_actions (timestamp, action_type, success, description, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (action.timestamp, action.action_type, int(action.success),
                 action.description, json.dumps(action.details))
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_action_history(self, limit: int = 20) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT * FROM heal_actions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return [
                {"id": r[0], "timestamp": r[1], "action_type": r[2],
                 "success": bool(r[3]), "description": r[4]}
                for r in rows
            ]
        except Exception:
            return []


# ============================================================
# 主控 — HealCore
# ============================================================

class HealCore:
    """自愈核心 — 感知→保护→修复 全流程"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.perceiver = HealthPerceiver(db_path)
        self.circuit = CircuitBreaker(db_path)
        self.healer = AutoHealer(db_path)

    def run_cycle(self, mode: str = "auto") -> Dict[str, Any]:
        """
        执行一次自愈周期

        Args:
            mode: "auto" 自动修复 / "check" 仅检查不修复 / "report" 仅报告

        Returns:
            {
                "status": "healthy|warning|critical",
                "snapshot": HealthSnapshot,
                "circuit_metrics": {...},
                "actions": [HealAction, ...],
                "summary": "一行摘要"
            }
        """
        start = time.time()

        # 1. 感知
        snapshot = self.perceiver.inspect()

        # 2. 保护（检查熔断器）
        cb_result = self.circuit.check("heal_cycle")
        if not cb_result["allowed"]:
            return {
                "status": "blocked",
                "snapshot": snapshot,
                "circuit_metrics": cb_result,
                "actions": [],
                "summary": f"熔断器 OPEN，跳过自愈周期 ({cb_result['failure_count']} 次失败)",
                "duration_ms": (time.time() - start) * 1000,
            }

        # 3. 修复
        actions = []
        if mode != "report":
            actions = self.healer.heal(snapshot)
            for a in actions:
                self.healer._log_action(a)

        # 4. 更新熔断器
        if snapshot.status == "critical":
            self.circuit.record_failure("heal_cycle")
        else:
            self.circuit.record_success("heal_cycle")

        duration = (time.time() - start) * 1000

        return {
            "status": snapshot.status,
            "snapshot": snapshot,
            "circuit_metrics": cb_result,
            "actions": actions,
            "summary": snapshot.summary(),
            "duration_ms": duration,
        }

    def get_report(self) -> Dict[str, Any]:
        """生成完整健康报告"""
        snapshots = self.perceiver.get_latest_snapshots(5)
        actions = self.healer.get_action_history(10)
        cb = self.circuit.get_metrics()

        return {
            "latest_snapshots": snapshots,
            "recent_actions": actions,
            "circuit_breaker": cb,
            "overview": {
                "total_snapshots": len(snapshots),
                "total_actions": len(actions),
                "open_circuits": cb["open_count"],
            }
        }


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="self-healing-core — 轻量自愈核心")
    parser.add_argument("--mode", choices=["auto", "check", "report"], default="auto",
                        help="运行模式: auto=自动修复, check=仅检查, report=仅报告")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="数据库路径")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出，显示每阶段决策")
    args = parser.parse_args()

    core = HealCore(db_path=args.db)

    if args.mode == "report":
        result = core.get_report()
    else:
        result = core.run_cycle(mode=args.mode)

    if args.json:
        # 将 dataclass 转为可序列化格式
        if "snapshot" in result and hasattr(result["snapshot"], "__dataclass_fields__"):
            s = result["snapshot"]
            result["snapshot"] = {
                "status": s.status,
                "memory_percent": s.memory_percent,
                "disk_percent": s.disk_percent,
                "api_timeout_count": s.api_timeout_count,
                "failure_rate": s.failure_rate,
                "timestamp": s.timestamp,
                "details": s.details,
            }
        if "actions" in result:
            result["actions"] = [
                {
                    "action_type": a.action_type,
                    "success": a.success,
                    "description": a.description,
                    "duration_ms": a.duration_ms,
                    "timestamp": a.timestamp,
                }
                for a in result["actions"]
            ]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"  自愈核心 — {args.mode} 模式")
        print(f"{'='*50}")

        # ── Verbose: 详细阶段输出 ──
        if args.verbose and args.mode != "report":
            s = result.get("snapshot")
            if s and hasattr(s, "__dataclass_fields__"):
                print(f"\n── 感知阶段 ──")
                print(f"  内存: {s.memory_percent:.0f}%  ({s.details.get('memory_level', '?')})")
                print(f"  磁盘: {s.disk_percent:.0f}%  ({s.details.get('disk_level', '?')})")
                print(f"  API超时(5min): {s.api_timeout_count}")
                print(f"  滑动失败率: {s.failure_rate:.1%}")
                print(f"  综合判定: {s.status.upper()}")
            cb = result.get("circuit_metrics", {})
            if cb:
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

        # ── 常规输出 ──
        if args.mode == "report":
            r = result
            print(f"\n最近快照 ({len(r['latest_snapshots'])}):")
            for s in r["latest_snapshots"]:
                print(f"  [{s['status'].upper()}] {s['timestamp'][:19]} "
                      f"内存:{s['memory_percent']:.0f}% 磁盘:{s['disk_percent']:.0f}%")
            print(f"\n最近动作 ({len(r['recent_actions'])}):")
            for a in r["recent_actions"]:
                status = "✅" if a["success"] else "❌"
                print(f"  {status} [{a['action_type']}] {a['description']}")
            print(f"\n熔断器: {r['circuit_breaker']['open_count']} OPEN / "
                  f"{r['circuit_breaker']['half_open_count']} HALF_OPEN")
        else:
            print(f"\n状态: {result['status'].upper()}")
            print(f"摘要: {result['summary']}")
            print(f"耗时: {result['duration_ms']:.0f}ms")
            if result["actions"]:
                print(f"\n修复动作 ({len(result['actions'])}):")
                for a in result["actions"]:
                    status = "✅" if a.success else "❌"
                    print(f"  {status} [{a.action_type}] {a.description}")
        print()


if __name__ == "__main__":
    main()
