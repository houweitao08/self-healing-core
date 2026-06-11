# self-healing-core

Lightweight, zero-dependency self-healing skill for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Three-layer architecture: **Perceive → Protect → Repair**.

- **HealthPerceiver**: monitors memory, disk, API timeouts, failure rate
- **CircuitBreaker**: prevents cascading failures (3 strikes → OPEN → cooldown) — **state persisted to SQLite**, survives restarts
- **AutoHealer**: 4 repair strategies (cleanup, backoff, compress, escalate)

Zero external dependencies. Pure Python stdlib. One file. ~500 lines.

## Quick Start

```bash
# Install
mkdir -p ~/.hermes/skills/self-healing-core/scripts/
cp heal_core.py ~/.hermes/skills/self-healing-core/scripts/

# Run a health check
python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode check

# Run auto-heal cycle
python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode auto

# Verbose mode — see each stage's detailed decisions
python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode check --verbose
```

### Example: verbose output

```
── 感知阶段 ──
  内存: 17%  (healthy)
  磁盘: 1%  (healthy)
  API超时(5min): 0
  滑动失败率: 0.0%
  综合判定: HEALTHY

── 保护阶段 ──
  熔断器: CLOSED
  允许执行: ✅
  历史失败: 0

── 修复阶段 ──
  无需修复

── 耗时 ──
  14ms
```

### Cron — keep your agent healthy 24/7

Add to your crontab (`crontab -e`):

```cron
# Every 30 minutes: auto-heal cycle
*/30 * * * * python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode auto >> ~/.hermes/logs/heal_core.log 2>&1

# Every 6 hours: generate a report
0 */6 * * * python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode report --json >> ~/.hermes/logs/heal_core_report.log 2>&1
```

Or use Hermes's built-in cron:

```bash
hermes cron create "30m" \
  --prompt "Run self-healing check: python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode auto" \
  --deliver "home"
```

## Python API

```python
from heal_core import HealCore

core = HealCore()

# Health check (no repair)
result = core.run_cycle(mode="check")
print(result["summary"])
# → [HEALTHY] 内存:17% 磁盘:1% API超时:0 失败率:0.0%

# Auto-heal cycle
result = core.run_cycle(mode="auto")
# → {"status": "healthy", "actions": [...], "duration_ms": 14}

# Full report
report = core.get_report()
# → {"latest_snapshots": [...], "recent_actions": [...], "circuit_breaker": {...}}
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  HealCore                        │
│                                                   │
│  ┌──────────────┐   ┌──────────────┐   ┌────────┐ │
│  │  Health       │   │  Circuit     │   │  Auto  │ │
│  │  Perceiver   │ → │  Breaker     │ → │ Healer │ │
│  │  (感知层)     │   │  (保护层)     │   │ (修复层)│ │
│  └──────────────┘   └──────────────┘   └────────┘ │
│        │                   │               │       │
│        ▼                   ▼               ▼       │
│  4维健康检查         级联熔断          4种修复策略   │
│  · 内存使用率        · 3次失败OPEN    · 磁盘清理    │
│  · 磁盘使用率        · 5分钟冷却      · 退避建议    │
│  · API超时计数       · 半开试运行     · 压缩建议    │
│  · 滑动失败率        · SQLite持久化   · 升级人工    │
└─────────────────────────────────────────────────┘
```

## CircuitBreaker — state persistence

The CircuitBreaker stores its state in **SQLite** (`~/.hermes/heal_core.db`), not in memory. This means:

- **Process restarts are safe** — failure counts survive Gateway restarts
- **No state loss** — if your Hermes Agent crashes and recovers, the circuit breaker remembers which operations were OPEN
- **Each operation is tracked independently** — `heal_cycle`, `api_call`, `tool_execute` each have their own failure counter

State machine: `CLOSED` → (3 failures) → `OPEN` → (5 min cooldown) → `HALF_OPEN` → (1 success) → `CLOSED`

## Cross-platform notes

| Platform | Memory check | Disk check | Status |
|----------|-------------|------------|--------|
| **Linux** | `/proc/meminfo` | `os.statvfs` | ✅ Full support |
| **macOS** | Returns `0.0, "unknown"` | `os.statvfs` | ⚠️ Memory skipped |
| **Windows** | Returns `0.0, "unknown"` | `os.statvfs` | ⚠️ Memory skipped |
| **WSL** | `/proc/meminfo` | `os.statvfs` | ✅ Full support |

On macOS and Windows, memory monitoring is skipped gracefully — all other features (disk check, circuit breaker, auto-heal) work normally. The health status never reports `critical` based on memory alone on these platforms.

## Test

```bash
python3 tests/test_self_healing_core.py
# 41 tests, all passing
```

## Requirements

- Python 3.8+
- Zero external dependencies
- Linux / WSL (full support), macOS / Windows (memory check skipped)

## Data storage

All data is stored in `~/.hermes/heal_core.db` (SQLite):

| Table | Purpose |
|-------|---------|
| `health_log` | Health check history |
| `api_timeout_log` | API timeout records |
| `circuit_breaker` | Circuit breaker state (persistent) |
| `heal_actions` | Repair action history |

## CLI reference

```
usage: heal_core.py [-h] [--mode {auto,check,report}] [--db DB] [--json] [--verbose]

optional arguments:
  --mode {auto,check,report}
                        auto=auto-repair, check=check only, report=full report
  --db DB               Database path (default: ~/.hermes/heal_core.db)
  --json                JSON format output
  --verbose, -v         Detailed per-stage output
```
