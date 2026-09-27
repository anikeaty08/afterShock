# Aftershock — API: PR comment rendering (§3.1)

from __future__ import annotations

from core.ingest import PRFlowReport

_TIER_EMOJI = {"T1": "cited", "T2": "fact", "T3": "neighbour", "T4": "reverse retrieval"}
_VERDICT_EMOJI = {
    "CHANGED": "\U0001f534",  # red circle
    "NOW_ABSTAINS": "⚠️",  # warning
    "NOW_ANSWERS": "\U0001f7e2",  # green circle
    "REWORDED": "⚪",  # white circle (not reported as a row, kept for completeness)
    "UNCHANGED": "⚪",
}


def render_pr_comment(report: PRFlowReport, *, report_url: str | None = None) -> str:
    """Render the PR comment body (§3.1). Excludes UNCHANGED/REWORDED rows
    from the summary table — those aren't reported as changes — but they
    still count in the header totals via ``len(report.entries)``."""
    reportable = [e for e in report.entries if e.verdict not in ("UNCHANGED", "REWORDED")]

    lines: list[str] = []
    lines.append(f"### \U0001f30a Aftershock report — PR #{report.pr} (head {report.head_sha[:7]})")
    lines.append("")
    lines.append(
        f"**{len(report.doc_ids)} docs changed · "
        f"{report.facts_added + report.facts_removed + report.facts_modified} facts changed "
        f"({report.facts_added} added · {report.facts_removed} removed · {report.facts_modified} modified)**"
    )
    lines.append(
        f"**{len(report.entries)} answers re-checked · {report.changed_count} changed · "
        f"{report.now_abstains_count} now unanswerable · {report.now_answers_count} newly answerable**"
    )
    total_ms = report.timings_ms.get("post", sum(report.timings_ms.values()))
    impact_ms = report.timings_ms.get("impact", 0) - report.timings_ms.get("diff", 0)
    lines.append(f"Impact traversal: {max(impact_ms, 0)} ms · Report generated in {total_ms / 1000:.0f}s")
    lines.append("")

    if reportable:
        lines.append("| Question | Verdict | Found via |")
        lines.append("|---|---|---|")
        for e in reportable:
            verdict_label = {
                "CHANGED": f"{_VERDICT_EMOJI['CHANGED']} CHANGED",
                "NOW_ABSTAINS": f"{_VERDICT_EMOJI['NOW_ABSTAINS']} NOW UNANSWERABLE",
                "NOW_ANSWERS": f"{_VERDICT_EMOJI['NOW_ANSWERS']} NEWLY ANSWERABLE",
            }.get(e.verdict, e.verdict)
            via = ", ".join(e.via) if e.via else "—"
            tier_label = _TIER_EMOJI.get(e.tier, e.tier)
            question = e.question.replace("|", "\\|")
            lines.append(f"| {question} | {verdict_label} | **{e.tier}** {tier_label} `{via}` |")
        lines.append("")

    changed_details = [e for e in reportable if e.verdict == "CHANGED"]
    if changed_details:
        lines.append(f"<details><summary>Before / after for {len(changed_details)} changed answers</summary>")
        lines.append("")
        for e in changed_details:
            lines.append(f"**{e.question}**")
            lines.append(f"- Before: {e.old_answer}")
            lines.append(f"- After: {e.new_answer}")
            if e.reason:
                lines.append(f"- Why: {e.reason}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    if report.truncated:
        cap_note = "⚠️ This PR's impacted set exceeded the per-PR cap — some flagged answers were not re-checked."
        lines.append(cap_note)
        lines.append("")

    if report.has_coverage_regression:
        lines.append(
            f"⚠️ Coverage check: **{report.now_abstains_count} question(s) lost all their evidence** "
            "in this PR (check run: warning)"
        )
    else:
        lines.append("✅ Coverage check: no question lost all its evidence in this PR.")

    if report_url:
        lines.append("")
        lines.append(f"[Open the full impact graph →]({report_url})")

    return "\n".join(lines)
