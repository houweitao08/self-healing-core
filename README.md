# self-healing-core

Lightweight, zero-dependency self-healing skill for Hermes Agent.

Three-layer architecture: **Perceive → Protect → Repair**.

- **HealthPerceiver**: monitors memory, disk, API timeouts, failure rate
- **CircuitBreaker**: prevents cascading failures (3 strikes → OPEN → cooldown)
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

# Cron: every 30 minutes
# */30 * * * * python3 ~/.hermes/skills/self-healing-core/scripts/heal_core.py --mode auto
```

## Test

```bash
python3 tests/test_self_healing_core.py
# 41 tests, all passing
```

## Python API

```python
from heal_core import HealCore
core = HealCore()
result = core.run_cycle(mode="check")
print(result["summary"])
```

## Requirements

- Python 3.8+
- Zero external dependencies
- Linux (memory check via /proc/meminfo; macOS/Windows fallback)
