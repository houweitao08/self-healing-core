# 故障场景演示：内存泄漏 → API 超时 → 全链路自愈

> 一个虚构但真实的故障场景，演示 self-healing-core 三层架构如何协同工作。
> 所有 L1 代码片段均可直接运行（依赖 `heal_core.py`）。

---

## 场景设定

你运行了一个 Hermes Agent 实例，部署了一个需要频繁调用外部 API 的自动化工作流。一切正常运行了 3 天。

**第 4 天，问题开始。**

---

## 时间线

```
T+0min    内存正常 (45%)     → L1 检查通过
T+30min   内存 72%           → L1 预警，压缩建议
T+60min   内存 88%           → L1 预警，压缩建议（但没用——内存还在涨）
T+90min   内存 94%           → L1 CRITICAL，升级人工
T+95min   首次 API 超时       → L1 记录超时
T+100min  3 次 API 超时      → L1 熔断器 OPEN，阻断所有 API 调用
T+120min  L2 介入诊断         → 发现"内存泄漏"模式
T+180min  L3 学习             → 调整阈值，创建关联规则
```

---

## 第 1 阶段：L1 感知（HealthPerceiver）

L1 每 30 分钟跑一次健康检查。以下是实际代码运行的效果：

```bash
# 模拟第 1 次检查：正常
python3 heal_core.py --mode check --verbose
```

输出：

```
── 感知阶段 ──
  内存: 45%  (healthy)
  磁盘: 23%  (healthy)
  API超时(5min): 0
  滑动失败率: 0.0%
  综合判定: HEALTHY

── 保护阶段 ──
  熔断器: CLOSED
  允许执行: ✅
  历史失败: 0

── 修复阶段 ──
  无需修复
```

一切正常。但内存泄漏已经开始了——只是还没超过阈值。

---

## 第 2 阶段：L1 预警（阈值触发）

30 分钟后，内存升到 72%。L1 的 `HealthPerceiver` 触发 `MEMORY_WARN`（默认 75%）：

```python
# heal_core.py 中的阈值逻辑
MEMORY_WARN = 75.0   # 预警线
MEMORY_CRIT = 90.0   # 危险线
```

此时状态还是 `healthy`（72 < 75），但再过 30 分钟：

```bash
# 模拟第 3 次检查：内存 88%
python3 heal_core.py --mode auto --verbose
```

输出：

```
── 感知阶段 ──
  内存: 88%  (warning)
  磁盘: 25%  (healthy)
  API超时(5min): 0
  滑动失败率: 33.3%
  综合判定: WARNING

── 保护阶段 ──
  熔断器: CLOSED
  允许执行: ✅
  历史失败: 0

── 修复阶段 (1 个动作) ──
  ✅ [compress] 建议压缩上下文: 丢弃 7 天前的会话历史，保留关键决策
```

L1 执行了 `compress` 策略——建议压缩上下文。但问题是：**内存泄漏不是上下文膨胀导致的**，压缩没用。

---

## 第 3 阶段：L1 升级（熔断器触发）

又过了 30 分钟，内存 94%，API 开始超时：

```bash
# 模拟第 4 次检查：CRITICAL
python3 heal_core.py --mode auto --verbose
```

输出：

```
── 感知阶段 ──
  内存: 94%  (critical)
  磁盘: 26%  (healthy)
  API超时(5min): 3
  滑动失败率: 50.0%
  综合判定: CRITICAL

── 保护阶段 ──
  熔断器: CLOSED
  允许执行: ✅
  历史失败: 0

── 修复阶段 (2 个动作) ──
  ✅ [compress] 建议压缩上下文: 丢弃 7 天前的会话历史，保留关键决策
  ✅ [escalate] 系统状态 CRITICAL，已升级到人工处理
```

L1 升级到人工。同时，熔断器开始记录失败：

```python
# heal_core.py 中的熔断逻辑
if snapshot.status == "critical":
    self.circuit.record_failure("heal_cycle")  # 第 1 次失败
```

第 5 次检查——再次 CRITICAL：

```python
self.circuit.record_failure("heal_cycle")  # 第 2 次失败
```

第 6 次检查——再次 CRITICAL：

```python
self.circuit.record_failure("heal_cycle")  # 第 3 次失败 → 熔断器 OPEN
```

此时熔断器状态：

```
── 保护阶段 ──
  熔断器: OPEN
  允许执行: ❌
  历史失败: 3
```

**L1 的修复策略全部失效。** 这不是 L1 的错——L1 设计上就是"规则驱动的快速反应"，它无法理解"为什么内存会持续上涨"。

---

## 第 4 阶段：L2 介入（智能诊断）

L2（smart-healing-system）被 L1 的持续失败触发。它分析 L1 的 SQLite 历史记录：

```sql
-- 从 heal_core.db 读取 L1 的历史快照
SELECT timestamp, status, memory_percent, api_timeout_count, failure_rate
FROM health_log
ORDER BY id DESC LIMIT 10;
```

结果：

| 时间 | 状态 | 内存 | API超时 | 失败率 |
|------|------|------|---------|--------|
| T+90min | critical | 94% | 3 | 50% |
| T+60min | warning | 88% | 0 | 33% |
| T+30min | healthy | 72% | 0 | 0% |
| T+0min | healthy | 45% | 0 | 0% |

L2 的诊断引擎发现三个关键模式：

1. **单调递增**：内存 45% → 72% → 88% → 94%，从未回落
2. **超时滞后**：API 超时在内存超过 88% 后才出现（不是原因，是结果）
3. **修复无效**：L1 的 `compress` 策略执行了 3 次，内存从未下降

**L2 诊断结论：这不是上下文膨胀，是内存泄漏。**

L2 的决策树：

```
内存持续上涨 + 修复无效 + 超时滞后
    ↓
判断：内存泄漏（Memory Leak）
    ↓
策略：不是清理日志，不是压缩上下文
    ↓
行动：重启目标服务 + 设置内存上限
```

L2 执行 `restart_service` 策略——重启了有内存泄漏的工作流进程。内存回落到 45%。

---

## 第 5 阶段：L3 学习（进化引擎）

L3（evolution-engine）分析整个事件的完整记录：

```python
# L3 的学习逻辑（概念代码）
class LeakDetector:
    """从历史修复中学习内存泄漏模式"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def analyze_pattern(self) -> dict:
        """分析 L1 快照，识别内存泄漏特征"""
        snapshots = self._get_snapshots(limit=20)
        if len(snapshots) < 4:
            return {"pattern": "insufficient_data"}

        # 特征 1：内存单调递增
        mem_values = [s["memory_percent"] for s in snapshots]
        is_monotonic = all(
            mem_values[i] >= mem_values[i+1]
            for i in range(len(mem_values)-1)
        )

        # 特征 2：修复无效（compress 执行后内存未降）
        compress_actions = self._get_compress_actions(limit=5)
        compress_ineffective = all(
            a["memory_after"] >= a["memory_before"]
            for a in compress_actions
        )

        # 特征 3：API 超时滞后于内存上涨
        timeout_first = self._first_timeout_time()
        mem_cross_warn = self._first_mem_warn_time()
        timeout_lags = timeout_first > mem_cross_warn if timeout_first and mem_cross_warn else False

        return {
            "is_leak": is_monotonic and compress_ineffective,
            "timeout_lags_memory": timeout_lags,
            "confidence": sum([is_monotonic, compress_ineffective, timeout_lags]) / 3,
        }
```

L3 学到三个东西，并写入进化记录：

### 学到 1：关联规则

```python
# L3 创建的新规则
NEW_RULE = {
    "trigger": "memory > 85% AND compress_attempts >= 2 AND memory_never_dropped",
    "action": "skip_compress → escalate_to_L2_leak_diagnosis",
    "source": "scenario_memory_leak_001",
    "confidence": 0.85,
}
```

下次遇到类似模式，L1 不会继续执行无效的 `compress`，而是直接跳转到 L2 泄漏诊断。

### 学到 2：阈值调整建议

```python
# L3 建议调整 L1 的检查频率
THRESHOLD_ADJUSTMENT = {
    "check_interval": "30min → 15min",  # 内存 > 75% 时加速检查
    "reason": "memory_leak_detected",
    "effectiveness": "faster_detection_by_15min",
}
```

### 学到 3：新检测指标

```python
# L3 建议 L1 增加的新指标
NEW_METRIC = {
    "name": "memory_delta_30min",
    "description": "每 30 分钟内存变化量",
    "threshold_warn": "+10%",
    "threshold_crit": "+20%",
    "purpose": "early_leak_detection",
}
```

---

## 第 6 阶段：闭环验证

3 天后，同样的工作流再次出现内存泄漏。

这次，L1 在 T+30min 检测到 `memory_delta_30min = +27%`（超过 +20% 危险线），**不等内存到 75%**，直接标记为 `leak_suspected`，跳过 `compress`，触发 L2 诊断。

L2 在 T+35min 确认泄漏，执行重启。**相比第一次，检测时间提前了 55 分钟。**

```
第一次：T+90min 才发现 CRITICAL
第二次：T+35min 就诊断并修复
节省：55 分钟
```

---

## 架构价值总结

```
                   第一次（无学习）              第二次（有学习）
                   
L1 感知            T+30min 预警                T+30min 发现 delta=+27%
L1 修复            compress × 3（无效）         跳过 compress，直接标记泄漏
L2 诊断            T+120min 介入                T+35min 介入
L2 修复            重启服务                     重启服务
L3 学习            创建关联规则                  规则已存在，无需学习
                   
总耗时             120 分钟                     35 分钟
修复质量           临时（下次还会发生）            永久（规则已固化）
```

---

## 自己动手跑一遍

这个场景不需要实际的内存泄漏。你可以用 L1 的 API 模拟整个过程：

```python
# scenario_simulator.py — 模拟内存泄漏场景
from heal_core import HealCore
from datetime import datetime, timedelta

core = HealCore()

# 模拟 6 次健康检查，每次内存上涨
memory_timeline = [45, 72, 88, 94, 96, 97]
api_timeouts = [0, 0, 0, 3, 5, 8]

for i, (mem, to) in enumerate(zip(memory_timeline, api_timeouts)):
    # 注入模拟数据到 SQLite
    core.perceiver._log_snapshot(HealthSnapshot(
        status="critical" if mem >= 90 else ("warning" if mem >= 75 else "healthy"),
        memory_percent=mem,
        disk_percent=25,
        api_timeout_count=to,
        failure_rate=to / 10,
        timestamp=(datetime.now() - timedelta(minutes=(5-i)*30)).isoformat(),
        details={"memory_level": "critical" if mem >= 90 else "warning" if mem >= 75 else "healthy"},
    ))

# 查看完整报告
report = core.get_report()
for s in report["latest_snapshots"]:
    print(f"  [{s['status'].upper()}] 内存:{s['memory_percent']:.0f}% "
          f"超时:{s['api_timeout_count']} 时间:{s['timestamp'][:19]}")
```

运行：

```bash
python3 scenario_simulator.py
```

输出：

```
  [CRITICAL] 内存:97% 超时:8 时间:2026-06-11T12:00:00
  [CRITICAL] 内存:96% 超时:5 时间:2026-06-11T11:30:00
  [CRITICAL] 内存:94% 超时:3 时间:2026-06-11T11:00:00
  [WARNING] 内存:88% 超时:0 时间:2026-06-11T10:30:00
  [HEALTHY] 内存:72% 超时:0 时间:2026-06-11T10:00:00
  [HEALTHY] 内存:45% 超时:0 时间:2026-06-11T09:30:00
```

这就是 L1 看到的数据——L2 和 L3 在此基础上做诊断和学习。

---

## 这个场景暴露了什么

1. **L1 不是万能的**——规则驱动意味着它无法理解"为什么"。它知道内存高了，但不知道为什么高。
2. **L2 的价值在于模式识别**——不是看单点数据，而是看趋势和关联。
3. **L3 的价值在于闭环**——一次教训变成永久规则，下次更快。
4. **三层不是替代关系，是接力关系**——L1 快但不深，L2 深但不快，L3 学但不实时。

---

## 下一步

- 在 GitHub 仓库的 Issues 中提交你遇到的"难以用固定规则修复的故障案例"
- 这些案例是驱动 L2 诊断规则和 L3 进化算法的最佳养料
- 欢迎 PR：添加新的故障场景文档到 `docs/` 目录
