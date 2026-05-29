"""Small Prometheus-compatible in-memory metrics registry."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    """Collect counters and timing summaries in process memory."""

    def __init__(self) -> None:
        """Create an empty metrics registry."""
        self._counters: dict[str, int] = defaultdict(int)
        self._timings: dict[str, list[int]] = defaultdict(list)
        self._lock = Lock()

    def increment(self, name: str, **labels: str) -> None:
        """Increment one labeled counter."""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += 1

    def observe_ms(self, name: str, value: int, **labels: str) -> None:
        """Record one duration in milliseconds."""
        key = self._key(name, labels)
        with self._lock:
            self._timings[key].append(value)

    def render_prometheus(self) -> str:
        """Render metrics in a Prometheus-compatible text format."""
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self._counters.items()):
                lines.append(f"{key} {value}")

            for key, values in sorted(self._timings.items()):
                count = len(values)
                total = sum(values)
                average = total / count if count else 0
                lines.append(f"{self._metric_with_suffix(key, '_count')} {count}")
                lines.append(f"{self._metric_with_suffix(key, '_sum_ms')} {total}")
                lines.append(f"{self._metric_with_suffix(key, '_avg_ms')} {average:.2f}")

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Clear all metrics for tests."""
        with self._lock:
            self._counters.clear()
            self._timings.clear()

    def _key(self, name: str, labels: dict[str, str]) -> str:
        """Build a Prometheus-style labeled metric key."""
        if not labels:
            return name

        label_text = ",".join(
            f'{label}="{value}"'
            for label, value in sorted(labels.items())
        )
        return f"{name}{{{label_text}}}"

    def _metric_with_suffix(self, key: str, suffix: str) -> str:
        """Append a metric suffix before labels so Prometheus text stays valid."""
        if "{" not in key:
            return f"{key}{suffix}"

        name, labels = key.split("{", maxsplit=1)
        return f"{name}{suffix}{{{labels}"
