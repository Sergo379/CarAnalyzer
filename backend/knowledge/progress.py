"""Derive real elapsed times from the existing persisted build journal."""

from datetime import UTC, datetime


def runtime_timing(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    now = datetime.now(UTC)
    endpoint = now
    if result.get("finished_at"):
        endpoint = datetime.fromisoformat(str(result["finished_at"]))

    def elapsed(key: str) -> float:
        try:
            start = datetime.fromisoformat(str(result.get(key) or ""))
            return round(max(0.0, (endpoint - start).total_seconds()), 1)
        except (ValueError, TypeError):
            return 0.0

    result.update(
        server_time=now.isoformat(),
        build_elapsed_seconds=elapsed("started_at"),
        elapsed_seconds=elapsed("phase_started_at"),
        provider_elapsed_seconds=elapsed("provider_attempt_started_at"),
        provider_total_elapsed_seconds=elapsed("provider_started_at"),
    )
    if result.get("phase") == "provider_retry":
        result["retry_in_seconds"] = round(
            max(0.0, float(result.get("retry_in_seconds") or 0) - elapsed("retry_started_at")), 1
        )
    return result
