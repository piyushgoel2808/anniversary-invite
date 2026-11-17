"""Self-contained offline HTML visual calibration report for Phase 1 benchmarking."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from face_indexer.benchmark import BenchmarkSummary


def render_html_report(
    summary: BenchmarkSummary,
    ground_truth_pairs: set[tuple[str, str]] | None = None,
) -> str:
    """Render an offline, zero-CDN HTML calibration report for local inspection."""
    esc = html.escape

    profile_rows: list[str] = []
    for prof in summary.guest_profiles:
        warn_html = (
            "<br>".join(f"<span class='warn'>{esc(w)}</span>" for w in prof.warnings)
            if prof.warnings
            else "<span class='ok'>Clean</span>"
        )
        profile_rows.append(
            f"<tr>"
            f"<td><strong>{esc(prof.display_name)}</strong><br>"
            f"<code>{esc(prof.guest_id)}</code></td>"
            f"<td>{prof.accepted_reference_count}</td>"
            f"<td>{prof.mean_intra_cosine:.3f}</td>"
            f"<td>{esc(', '.join(prof.accepted_files) or 'None')}</td>"
            f"<td>{warn_html}</td>"
            f"</tr>"
        )

    sweep_section = ""
    if summary.threshold_sweep:
        sweep_rows: list[str] = []
        for row in summary.threshold_sweep:
            is_best = (
                summary.recommended_threshold is not None
                and abs(row.threshold - summary.recommended_threshold) < 1e-6
            )
            row_cls = " class='best-row'" if is_best else ""
            sweep_rows.append(
                f"<tr{row_cls}>"
                f"<td>{row.threshold:.3f}{' ★' if is_best else ''}</td>"
                f"<td>{row.true_positives}</td>"
                f"<td>{row.false_positives}</td>"
                f"<td>{row.false_negatives}</td>"
                f"<td>{row.precision * 100:.1f}%</td>"
                f"<td>{row.recall * 100:.1f}%</td>"
                f"<td><strong>{row.f1 * 100:.1f}%</strong></td>"
                f"</tr>"
            )
        sweep_section = f"""
        <section>
          <h2>Threshold Calibration Sweep (Ground Truth)</h2>
          <p class="subtitle">
            Recommended threshold by F1: <strong>{summary.recommended_threshold:.3f}</strong>
            (current run threshold: <strong>{summary.cosine_threshold:.3f}</strong>).
            Matched items become <code>PENDING</code> guest suggestions, not auto-published.
          </p>
          <table>
            <thead>
              <tr>
                <th>Cosine Threshold</th><th>TP</th><th>FP</th><th>FN</th>
                <th>Precision</th><th>Recall</th><th>F1</th>
              </tr>
            </thead>
            <tbody>{"".join(sweep_rows)}</tbody>
          </table>
        </section>
        """

    # Group suggestions by guest for fast visual review
    by_guest: dict[str, list[tuple[str, dict[str, Any], str | None]]] = {
        p.guest_id: [] for p in summary.guest_profiles
    }
    for photo in summary.photos:
        for sug in photo.suggestions:
            gid = str(sug["guest_id"])
            by_guest.setdefault(gid, []).append(
                (photo.filename, sug, photo.suggestion_crops.get(gid))
            )

    guest_sections: list[str] = []
    for prof in summary.guest_profiles:
        items = sorted(
            by_guest.get(prof.guest_id, []),
            key=lambda x: float(x[1]["confidence"]),
            reverse=True,
        )
        cards: list[str] = []
        for filename, sug, crop_url in items:
            badge_html = ""
            if ground_truth_pairs is not None:
                is_tp = (filename, prof.guest_id) in ground_truth_pairs
                badge_html = (
                    "<span class='badge tp'>GT Match</span>"
                    if is_tp
                    else "<span class='badge fp'>False Positive</span>"
                )
            amb_html = (
                "<span class='badge warn-badge'>Ambiguous Face</span>"
                if sug.get("ambiguous_face")
                else ""
            )
            img_tag = (
                f"<img src='{crop_url}' alt='Face crop' class='crop'>"
                if crop_url
                else "<div class='crop placeholder'>No crop</div>"
            )
            cards.append(
                f"<div class='card'>"
                f"{img_tag}"
                f"<div class='meta'>"
                f"<div class='fname'>{esc(filename)}</div>"
                f"<div class='score'>Cosine: <strong>{float(sug['confidence']):.3f}</strong> "
                f"(L2: {float(sug['l2_distance']):.3f})</div>"
                f"<div class='small'>Source: {esc(str(sug['resolution_source']))} · "
                f"Status: <code>{esc(str(sug['status']))}</code></div>"
                f"<div class='badges'>{badge_html}{amb_html}</div>"
                f"</div>"
                f"</div>"
            )

        cards_html = (
            "".join(cards) if cards else "<p class='empty'>No suggestions above threshold.</p>"
        )
        guest_sections.append(
            f"<div class='guest-block'>"
            f"<h3>{esc(prof.display_name)} <span>({len(items)} suggestions)</span></h3>"
            f"<div class='card-grid'>{cards_html}</div>"
            f"</div>"
        )

    photo_rows: list[str] = []
    for photo in summary.photos:
        retry_label = (
            f"Yes ({esc(photo.retry_reason or '')})" if photo.used_original_retry else "No"
        )
        matched_str = ", ".join(
            f"{s['guest_id']} ({float(s['confidence']):.2f})" for s in photo.suggestions
        )
        analyzed_dim = f"{photo.analyzed_width}x{photo.analyzed_height}"
        photo_rows.append(
            f"<tr>"
            f"<td><code>{esc(photo.filename)}</code></td>"
            f"<td>{photo.original_width}x{photo.original_height}</td>"
            f"<td>{esc(photo.resolution_source)} ({analyzed_dim})</td>"
            f"<td>{retry_label}</td>"
            f"<td>{photo.face_count}</td>"
            f"<td>{photo.total_ms:.1f} ms</td>"
            f"<td>{esc(matched_str or '-')}</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Silver Jubilee Face Indexer — Phase 1 Benchmark Report</title>
  <style>
    :root {{
      --ivory: #FAF7F2;
      --blue: #1B3664;
      --silver: #9BA4B5;
      --card: #FFFFFF;
      --border: #E2DFD7;
      --text: #1F2937;
      --muted: #6B7280;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 28px; background: var(--ivory); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 2px solid var(--blue); padding-bottom: 14px; margin-bottom: 24px;
    }}
    h1 {{ margin: 0 0 6px; color: var(--blue); font-family: Georgia, serif; }}
    h2 {{ color: var(--blue); font-family: Georgia, serif; margin-top: 32px; }}
    h3 {{ color: var(--blue); margin: 18px 0 10px; }}
    h3 span {{ font-weight: normal; color: var(--muted); font-size: 0.9rem; }}
    .subtitle {{ color: var(--muted); margin: 4px 0 14px; }}
    .kpi-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px;
    }}
    .kpi {{
      background: var(--card); border: 1px solid var(--border); border-radius: 10px;
      padding: 14px 16px;
    }}
    .kpi .label {{ font-size: 0.78rem; text-transform: uppercase; color: var(--muted); }}
    .kpi .value {{ font-size: 1.5rem; font-weight: 700; color: var(--blue); margin-top: 4px; }}
    table {{
      width: 100%; border-collapse: collapse; background: var(--card);
      border: 1px solid var(--border); font-size: 0.9rem;
    }}
    th, td {{ padding: 9px 11px; border-bottom: 1px solid var(--border); text-align: left; }}
    th {{ background: #F0ECE3; color: var(--blue); font-weight: 600; }}
    .best-row {{ background: #ECFDF5; }}
    .warn {{ color: #B45309; font-size: 0.85rem; }}
    .ok {{ color: #047857; font-size: 0.85rem; font-weight: 600; }}
    .card-grid {{
      display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px;
    }}
    .card {{
      display: flex; gap: 10px; align-items: center; background: var(--card);
      border: 1px solid var(--border); border-radius: 10px; padding: 10px;
    }}
    .crop {{
      width: 68px; height: 68px; object-fit: cover; border-radius: 8px;
      border: 1px solid var(--border); flex-shrink: 0;
    }}
    .crop.placeholder {{
      display: flex; align-items: center; justify-content: center;
      font-size: 0.72rem; color: var(--muted); background: #F3F4F6;
    }}
    .fname {{ font-size: 0.82rem; font-weight: 600; word-break: break-all; }}
    .score {{ font-size: 0.85rem; margin-top: 2px; }}
    .small {{ font-size: 0.75rem; color: var(--muted); margin-top: 2px; }}
    .badges {{ margin-top: 5px; display: flex; gap: 5px; flex-wrap: wrap; }}
    .badge {{
      font-size: 0.7rem; padding: 2px 6px; border-radius: 999px; font-weight: 600;
    }}
    .badge.tp {{ background: #D1FAE5; color: #065F46; }}
    .badge.fp {{ background: #FEE2E2; color: #991B1B; }}
    .badge.warn-badge {{ background: #FEF3C7; color: #92400E; }}
    .empty {{ color: var(--muted); font-style: italic; }}
    code {{ background: #F3F4F6; padding: 1px 5px; border-radius: 4px; font-size: 0.84em; }}
  </style>
</head>
<body>
  <header>
    <h1>Silver Jubilee Face Indexer — Phase 1 Local Benchmark</h1>
    <div class="subtitle">
      Models: <code>{esc(summary.model_version)}</code> ·
      Strategy: <code>{esc(summary.scoring_strategy)}</code> ·
      Cosine Threshold: <strong>{summary.cosine_threshold:.3f}</strong> ·
      100% Local Offline Report
    </div>
  </header>

  <section class="kpi-grid">
    <div class="kpi">
      <div class="label">Album Photos</div>
      <div class="value">{summary.photo_count}</div>
    </div>
    <div class="kpi">
      <div class="label">Guest Profiles</div>
      <div class="value">{summary.guest_count}</div>
    </div>
    <div class="kpi">
      <div class="label">Faces Detected</div>
      <div class="value">{summary.total_faces_detected}</div>
    </div>
    <div class="kpi">
      <div class="label">Pending Suggestions</div>
      <div class="value">{summary.total_suggestions}</div>
    </div>
    <div class="kpi">
      <div class="label">Mean / P95 Latency</div>
      <div class="value">{summary.mean_ms_per_photo:.0f} / {summary.p95_ms_per_photo:.0f} ms</div>
    </div>
    <div class="kpi">
      <div class="label">Throughput</div>
      <div class="value">{summary.throughput_photos_per_sec:.1f} img/s</div>
    </div>
    <div class="kpi">
      <div class="label">Original-Res Retries</div>
      <div class="value">
        {summary.original_retry_count} ({summary.original_retry_rate * 100:.0f}%)
      </div>
    </div>
  </section>

  {sweep_section}

  <section>
    <h2>Guest Reference Profiles</h2>
    <table>
      <thead>
        <tr>
          <th>Guest</th><th>Accepted Refs</th><th>Intra-Ref Cosine</th>
          <th>Accepted Files</th><th>Health / Warnings</th>
        </tr>
      </thead>
      <tbody>{"".join(profile_rows)}</tbody>
    </table>
  </section>

  <section>
    <h2>Suggested Matches by Guest (Visual Review)</h2>
    {"".join(guest_sections)}
  </section>

  <section>
    <h2>Per-Photo Processing Telemetry</h2>
    <table>
      <thead>
        <tr>
          <th>Filename</th><th>Original Size</th><th>Analyzed Pass</th>
          <th>Original Retry?</th><th>Faces</th><th>Time</th><th>Suggestions</th>
        </tr>
      </thead>
      <tbody>{"".join(photo_rows)}</tbody>
    </table>
  </section>
</body>
</html>
"""


def write_html_report(
    summary: BenchmarkSummary,
    output_path: Path,
    ground_truth_pairs: set[tuple[str, str]] | None = None,
) -> Path:
    """Write the offline HTML visual report to ``output_path``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_html_report(summary, ground_truth_pairs=ground_truth_pairs)
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
