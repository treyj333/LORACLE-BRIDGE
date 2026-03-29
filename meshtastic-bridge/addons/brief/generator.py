"""SITREP generator — turns aggregated mesh traffic into structured situation reports."""

import logging
import time
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# SITREP generation system prompt
SITREP_SYSTEM_PROMPT = """You are LORACLE BRIEF, a situation report generator for a LoRa mesh radio network.
Generate a concise SITREP (situation report) from the provided mesh traffic data.

FORMAT (keep it SHORT — this goes over LoRa radio):
SITREP — [time period]
1. SITUATION: [overall mesh status in 1-2 sentences]
2. KEY ACTIVITY: [notable events, queries, or alerts — bullet list]
3. NODE STATUS: [who checked in, who's active]
4. ASSESSMENT: [any patterns, concerns, or recommendations in 1 sentence]

RULES:
- Be CONCISE — every word counts on LoRa
- Focus on ACTIONABLE information
- Flag any anomalies (unusual activity, long silence from active nodes)
- Use military time format (e.g., 1430)
- If no significant activity, say so briefly"""


def generate_sitrep(
    ollama_client,
    events: List[Dict],
    summary: Dict,
    period_start: float,
    period_end: float,
) -> str:
    """Generate a SITREP from aggregated traffic data.

    Args:
        ollama_client: OllamaClient instance for LLM generation.
        events: List of traffic event dicts.
        summary: Aggregated stats from aggregator.get_event_summary().
        period_start: Start of the reporting period (timestamp).
        period_end: End of the reporting period (timestamp).

    Returns:
        SITREP text string.
    """
    # Build event digest for the LLM
    event_lines = []
    for e in events[-50:]:  # Cap at 50 most recent events
        ts = time.strftime("%H%M", time.localtime(e["timestamp"]))
        event_lines.append(f"[{ts}] {e['node_id']} ({e['event_type']}): {e['summary']}")

    if not event_lines:
        return _template_sitrep(summary, period_start, period_end)

    event_digest = "\n".join(event_lines)
    start_str = time.strftime("%H%M", time.localtime(period_start))
    end_str = time.strftime("%H%M", time.localtime(period_end))

    prompt = (
        f"Generate a SITREP for the period {start_str}-{end_str}.\n\n"
        f"MESH TRAFFIC DATA:\n"
        f"Total events: {summary['total_events']}\n"
        f"Unique nodes: {summary['unique_nodes']}\n"
        f"Events by type: {summary['by_type']}\n\n"
        f"EVENT LOG:\n{event_digest}"
    )

    try:
        response = ollama_client.chat(
            "brief_generator",
            prompt,
            system_prompt_override=SITREP_SYSTEM_PROMPT,
        )
        ollama_client.clear_history("brief_generator")
        return response
    except Exception as e:
        logger.warning(f"LLM SITREP generation failed: {e}, using template fallback")
        return _template_sitrep(summary, period_start, period_end)


def _template_sitrep(summary: Dict, period_start: float, period_end: float) -> str:
    """Fallback template-based SITREP when LLM is unavailable."""
    start_str = time.strftime("%H%M", time.localtime(period_start))
    end_str = time.strftime("%H%M", time.localtime(period_end))
    date_str = time.strftime("%d %b %Y", time.localtime(period_end))

    by_type = summary.get("by_type", {})
    type_str = ", ".join(f"{k}: {v}" for k, v in by_type.items()) or "none"

    return (
        f"SITREP — {start_str}-{end_str} {date_str}\n"
        f"1. SITUATION: Mesh network active. "
        f"{summary.get('unique_nodes', 0)} node(s) observed, "
        f"{summary.get('total_events', 0)} event(s) recorded.\n"
        f"2. KEY ACTIVITY: {type_str}\n"
        f"3. NODE STATUS: {summary.get('unique_nodes', 0)} active node(s)\n"
        f"4. ASSESSMENT: Normal operations. [Auto-generated — LLM unavailable]"
    )


def make_sitrep_id() -> str:
    """Generate a unique SITREP identifier."""
    return f"sitrep-{uuid.uuid4().hex[:8]}"
