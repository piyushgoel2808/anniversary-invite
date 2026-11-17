"""Command-line interface for the Silver Jubilee Face Indexer."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from face_indexer.benchmark import (
    build_profiles_from_directory,
    export_benchmark_artifacts,
    load_ground_truth_csv,
    run_benchmark,
)
from face_indexer.imaging import load_image_bgr
from face_indexer.indexer import (
    CALIBRATED_COSINE_THRESHOLD,
    index_album_folder,
    register_references_root,
    run_matching,
)
from face_indexer.matcher import (
    DEFAULT_COSINE_THRESHOLD,
    ScoringStrategy,
    match_faces_to_guests,
)
from face_indexer.models import (
    DEFAULT_MODELS_DIR,
    MODEL_MANIFEST,
    ModelIntegrityError,
    download_all_models,
    verify_all_models,
)
from face_indexer.recognizer import EngineConfig, FaceRecognitionEngine
from face_indexer.report import write_html_report
from face_indexer.storage import SQLiteFaceStore, load_or_create_encryption_key
from face_indexer.sync import (
    DEFAULT_APPS_SCRIPT_URL,
    AppsScriptBackend,
    GoogleWorkspaceBackend,
    run_sync,
)

DEFAULT_DB_PATH = Path("data/indexer.db")
DEFAULT_KEY_PATH = Path("data/.embedding_key")
DEFAULT_CREDENTIALS_PATH = Path("data/auth/credentials.json")
DEFAULT_TOKEN_PATH = Path("data/auth/token.json")


def _open_store(args: argparse.Namespace) -> SQLiteFaceStore:
    db_path = Path(args.db)
    key: bytes | None = None
    if not getattr(args, "no_encryption", False):
        key_path = Path(getattr(args, "key_file", DEFAULT_KEY_PATH))
        key = load_or_create_encryption_key(key_path)
    return SQLiteFaceStore(db_path, encryption_key=key)


def _cmd_download_models(args: argparse.Namespace) -> int:
    models_dir = Path(args.models_dir)
    print(f"Downloading pinned YuNet + SFace ONNX models into {models_dir} ...")
    paths = download_all_models(models_dir, force=bool(args.force))
    for key, path in paths.items():
        spec = MODEL_MANIFEST[key]
        print(
            f"  [OK] {spec.version} ({spec.license_spdx}) -> {path} "
            f"[sha256:{spec.expected_sha256[:16]}...]"
        )
    return 0


def _cmd_verify_models(args: argparse.Namespace) -> int:
    models_dir = Path(args.models_dir)
    try:
        paths = verify_all_models(models_dir)
    except ModelIntegrityError as err:
        print(f"[FAILED] {err}", file=sys.stderr)
        return 1

    for key, path in paths.items():
        spec = MODEL_MANIFEST[key]
        print(
            f"  [VERIFIED] {spec.version} ({spec.license_spdx}) "
            f"size={spec.expected_size_bytes} sha256={spec.expected_sha256} -> {path}"
        )
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    models_dir = Path(args.models_dir)
    references_dir = Path(args.references)
    album_dir = Path(args.album)
    output_dir = Path(args.output_dir)
    gt_path = Path(args.ground_truth) if args.ground_truth else None

    config = EngineConfig(
        score_threshold=float(args.det_threshold),
        medium_max_edge=int(args.medium_max_edge),
        enable_original_retry=not bool(args.no_original_retry),
    )
    engine = FaceRecognitionEngine(models_dir=models_dir, config=config)

    strategy = cast(ScoringStrategy, args.strategy)
    summary = run_benchmark(
        references_dir=references_dir,
        album_dir=album_dir,
        engine=engine,
        cosine_threshold=float(args.threshold),
        strategy=strategy,
        ground_truth_csv=gt_path,
        include_crops=not bool(args.no_crops),
    )

    json_path, csv_path = export_benchmark_artifacts(summary, output_dir)
    gt_pairs = load_ground_truth_csv(gt_path) if gt_path else None
    html_path = write_html_report(summary, output_dir / "report.html", ground_truth_pairs=gt_pairs)

    print("\n=== Silver Jubilee Face Indexer — Phase 1 Benchmark Summary ===")
    print(f"Models:            {summary.model_version}")
    print(f"Guest profiles:    {summary.guest_count}")
    print(f"Album photos:      {summary.photo_count}")
    print(f"Faces detected:    {summary.total_faces_detected}")
    print(f"Pending matches:   {summary.total_suggestions} (threshold={summary.cosine_threshold})")
    print(
        f"Latency (ms):      mean={summary.mean_ms_per_photo:.1f}, "
        f"p50={summary.p50_ms_per_photo:.1f}, p95={summary.p95_ms_per_photo:.1f} "
        f"({summary.throughput_photos_per_sec:.2f} photos/sec)"
    )
    print(
        f"Original retries:  {summary.original_retry_count}/{summary.photo_count} "
        f"({summary.original_retry_rate * 100:.1f}%)"
    )
    if summary.ground_truth_metrics is not None:
        m = summary.ground_truth_metrics
        print(
            f"Ground truth:      TP={m.true_positives} FP={m.false_positives} "
            f"FN={m.false_negatives} | P={m.precision:.3f} R={m.recall:.3f} F1={m.f1:.3f}"
        )
        print(f"Recommended thr:   {summary.recommended_threshold}")
    print(f"Artifacts written: {json_path}, {csv_path}, {html_path}")
    return 0


def _cmd_check_photo(args: argparse.Namespace) -> int:
    engine = FaceRecognitionEngine(
        models_dir=Path(args.models_dir),
        config=EngineConfig(enable_original_retry=not bool(args.no_original_retry)),
    )
    profiles, _ = build_profiles_from_directory(Path(args.references), engine)
    photo_path = Path(args.photo)
    bgr = load_image_bgr(photo_path)
    analysis = engine.analyze_image(bgr)
    suggestions = match_faces_to_guests(
        photo_id=photo_path.name,
        faces=analysis.faces,
        profiles=profiles,
        cosine_threshold=float(args.threshold),
        strategy=cast(ScoringStrategy, args.strategy),
    )
    payload = {
        "photo": photo_path.name,
        "original_size": list(analysis.original_size),
        "analyzed_size": list(analysis.analyzed_size),
        "resolution_source": analysis.resolution_source,
        "used_original_retry": analysis.used_original_retry,
        "retry_reason": analysis.retry_reason,
        "faces_detected": len(analysis.faces),
        "total_ms": round(analysis.total_ms, 2),
        "suggestions": [
            {
                "guest_id": s.guest_id,
                "status": s.status,
                "confidence": round(s.confidence, 4),
                "l2_distance": round(s.l2_distance, 4),
                "ambiguous_face": s.ambiguous_face,
            }
            for s in suggestions
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_index_album(args: argparse.Namespace) -> int:
    store = _open_store(args)
    engine = FaceRecognitionEngine(
        models_dir=Path(args.models_dir),
        config=EngineConfig(enable_original_retry=not bool(args.no_original_retry)),
    )
    summary = index_album_folder(Path(args.album), store, engine)
    print("\n=== Incremental Album Index Summary ===")
    print(f"Database:       {args.db}")
    print(f"Photos scanned: {summary.scanned}")
    print(f"Cached skipped: {summary.skipped_cached}")
    print(f"Newly indexed:  {summary.newly_indexed}")
    print(f"Faces detected: {summary.faces_detected}")
    print(f"Elapsed:        {summary.elapsed_ms:.1f} ms")
    if summary.errors:
        print(f"Errors ({len(summary.errors)}):", file=sys.stderr)
        for err in summary.errors:
            print(f"  - {err}", file=sys.stderr)
    return 0


def _cmd_register_guests(args: argparse.Namespace) -> int:
    store = _open_store(args)
    engine = FaceRecognitionEngine(models_dir=Path(args.models_dir))
    summaries = register_references_root(Path(args.references), store, engine)
    print("\n=== Guest Reference Registration Summary ===")
    for s in summaries:
        print(
            f"  {s.guest_id} ({s.display_name}): scanned={s.scanned} "
            f"cached={s.skipped_cached} accepted={s.accepted} rejected={s.rejected}"
        )
        for reason in s.reasons:
            print(f"    * {reason}")
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    store = _open_store(args)
    summary = run_matching(
        store,
        threshold=float(args.threshold),
        strategy=cast(ScoringStrategy, args.strategy),
    )
    print("\n=== Local SQLite Match Summary ===")
    print(f"Photos checked:  {summary.photos_checked}")
    print(f"Active guests:   {summary.guests_checked}")
    print(f"Threshold:       {summary.threshold}")
    print(f"New suggestions: {summary.new_suggestions}")
    print(f"Total PENDING:   {summary.total_pending}")
    print(f"Elapsed:         {summary.elapsed_ms:.2f} ms")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = _open_store(args)
    stats = store.get_stats()
    stats["last_sync_at"] = store.get_sync_state("last_sync_at")
    stats["db_path"] = str(args.db)
    print(json.dumps(stats, indent=2))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    store = _open_store(args)
    engine = FaceRecognitionEngine(
        models_dir=Path(args.models_dir),
        config=EngineConfig(enable_original_retry=not bool(args.no_original_retry)),
    )

    if args.spreadsheet_id:
        google_backend = GoogleWorkspaceBackend(
            spreadsheet_id=str(args.spreadsheet_id),
            credentials_path=Path(args.credentials),
            token_path=Path(args.token),
            references_folder_id=args.references_folder_id,
        )
        report = run_sync(
            google_backend,
            store,
            engine,
            threshold=float(args.threshold),
            strategy=cast(ScoringStrategy, args.strategy),
            delete_refs_after_processing=bool(args.delete_refs),
        )
    else:
        admin_user = args.admin_user or os.environ.get("FACE_INDEXER_ADMIN_USER") or ""
        if not admin_user:
            admin_user = input("Admin username: ").strip()
        admin_pass = os.environ.get("FACE_INDEXER_ADMIN_PASSWORD") or ""
        if not admin_pass:
            admin_pass = getpass.getpass("Admin password: ")

        with AppsScriptBackend(
            apps_script_url=str(args.apps_script_url),
            admin_username=admin_user,
            admin_password=admin_pass,
            photo_variant=str(args.variant),
        ) as apps_backend:
            report = run_sync(
                apps_backend,
                store,
                engine,
                threshold=float(args.threshold),
                strategy=cast(ScoringStrategy, args.strategy),
                delete_refs_after_processing=bool(args.delete_refs),
            )

    print("\n=== Silver Jubilee Face Indexer — Sync Summary ===")
    print(f"Reviews pulled:       {report.reviews_pulled}")
    print(f"Profiles processed:   {report.profiles_processed}")
    print(f"Profiles revoked:     {report.profiles_revoked}")
    print(f"Photos skipped:       {report.photos_skipped_cached}")
    print(f"Photos newly indexed: {report.photos_newly_indexed}")
    print(f"Photos failed:        {report.photos_failed}")
    print(f"New suggestions:      {report.new_suggestions_created}")
    print(f"Suggestions pushed:   {report.suggestions_pushed}")
    print(f"Elapsed:              {report.elapsed_ms:.1f} ms")
    if report.errors:
        print(f"Errors ({len(report.errors)}):", file=sys.stderr)
        for err in report.errors:
            print(f"  - {err}", file=sys.stderr)
    return 0


def _add_db_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Local SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    subparser.add_argument(
        "--key-file",
        default=str(DEFAULT_KEY_PATH),
        help=f"Local Fernet encryption key file (default: {DEFAULT_KEY_PATH})",
    )
    subparser.add_argument(
        "--no-encryption",
        action="store_true",
        help="Disable embedding blob encryption (dev/testing only)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the root CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="face-indexer",
        description="Local privacy-first Mac face recognition & sync engine (YuNet + SFace).",
    )
    parser.add_argument(
        "--models-dir",
        default=str(DEFAULT_MODELS_DIR),
        help="Directory for pinned ONNX model weights (default: face-indexer/models).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download-models", help="Download and SHA-256 verify pinned ONNX models.")
    dl.add_argument(
        "--force", action="store_true", help="Re-download even if verified copy exists."
    )
    dl.set_defaults(func=_cmd_download_models)

    vf = sub.add_parser("verify-models", help="Verify SHA-256 checksums of local ONNX models.")
    vf.set_defaults(func=_cmd_verify_models)

    bm = sub.add_parser("benchmark", help="Run local recognition benchmark on a photo folder.")
    bm.add_argument("--references", required=True, help="Directory of references/<guest_id>/*")
    bm.add_argument("--album", required=True, help="Directory of album photographs")
    bm.add_argument("--ground-truth", default=None, help="Optional ground_truth.csv")
    bm.add_argument("--output-dir", default="reports/latest", help="Output directory for report")
    bm.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_COSINE_THRESHOLD,
        help="Cosine similarity suggestion threshold (default: 0.363)",
    )
    bm.add_argument(
        "--strategy",
        choices=("max_ref", "centroid", "hybrid"),
        default="max_ref",
        help="Multi-reference scoring strategy (default: max_ref)",
    )
    bm.add_argument("--det-threshold", type=float, default=0.65, help="YuNet score threshold")
    bm.add_argument("--medium-max-edge", type=int, default=1400, help="Medium pass max edge px")
    bm.add_argument("--no-original-retry", action="store_true", help="Disable original-res retry")
    bm.add_argument("--no-crops", action="store_true", help="Skip base64 face crops in HTML report")
    bm.set_defaults(func=_cmd_benchmark)

    cp = sub.add_parser("check-photo", help="Inspect detections and suggestions for one photo.")
    cp.add_argument("--references", required=True, help="Directory of references/<guest_id>/*")
    cp.add_argument("--photo", required=True, help="Path to a single photograph")
    cp.add_argument("--threshold", type=float, default=CALIBRATED_COSINE_THRESHOLD)
    cp.add_argument("--strategy", choices=("max_ref", "centroid", "hybrid"), default="max_ref")
    cp.add_argument("--no-original-retry", action="store_true")
    cp.set_defaults(func=_cmd_check_photo)

    idx = sub.add_parser("index-album", help="Incrementally index local album photos into SQLite.")
    idx.add_argument("--album", required=True, help="Directory of album photographs")
    idx.add_argument("--no-original-retry", action="store_true")
    _add_db_args(idx)
    idx.set_defaults(func=_cmd_index_album)

    reg = sub.add_parser(
        "register-guests", help="Incrementally register guest references into SQLite."
    )
    reg.add_argument("--references", required=True, help="Directory of references/<guest_id>/*")
    _add_db_args(reg)
    reg.set_defaults(func=_cmd_register_guests)

    mt = sub.add_parser("match", help="Match cached face embeddings against active guest profiles.")
    mt.add_argument(
        "--threshold",
        type=float,
        default=CALIBRATED_COSINE_THRESHOLD,
        help=f"Cosine similarity threshold (default: {CALIBRATED_COSINE_THRESHOLD})",
    )
    mt.add_argument("--strategy", choices=("max_ref", "centroid", "hybrid"), default="max_ref")
    _add_db_args(mt)
    mt.set_defaults(func=_cmd_match)

    st = sub.add_parser("status", help="Show local SQLite indexer statistics.")
    _add_db_args(st)
    st.set_defaults(func=_cmd_status)

    sy = sub.add_parser(
        "sync",
        help="Run incremental face sync (default: Apps Script Web App — zero Google Cloud needed).",
    )
    sy.add_argument(
        "--apps-script-url",
        default=DEFAULT_APPS_SCRIPT_URL,
        help="Apps Script Web App /exec URL (default: Silver Jubilee live deployment)",
    )
    sy.add_argument(
        "--admin-user",
        default=None,
        help="Gallery Admin username (or set FACE_INDEXER_ADMIN_USER; prompts if omitted)",
    )
    sy.add_argument(
        "--variant",
        choices=("original", "medium"),
        default="original",
        help="Photo variant downloaded from Apps Script for face detection (default: original)",
    )
    sy.add_argument(
        "--spreadsheet-id",
        default=None,
        help="Optional: use direct Google Cloud OAuth mode instead of Apps Script",
    )
    sy.add_argument(
        "--credentials",
        default=str(DEFAULT_CREDENTIALS_PATH),
        help="Google OAuth JSON (only for --spreadsheet-id mode)",
    )
    sy.add_argument(
        "--token",
        default=str(DEFAULT_TOKEN_PATH),
        help=f"Cached OAuth token (only for --spreadsheet-id mode; default: {DEFAULT_TOKEN_PATH})",
    )
    sy.add_argument(
        "--references-folder-id",
        default=None,
        help="Optional Drive folder ID for FACE_REFERENCES (only for --spreadsheet-id mode)",
    )
    sy.add_argument(
        "--threshold",
        type=float,
        default=CALIBRATED_COSINE_THRESHOLD,
        help=f"Cosine similarity threshold (default: {CALIBRATED_COSINE_THRESHOLD})",
    )
    sy.add_argument("--strategy", choices=("max_ref", "centroid", "hybrid"), default="max_ref")
    sy.add_argument(
        "--delete-refs",
        action="store_true",
        help="Trash raw guest reference photos in Drive after extracting local embeddings",
    )
    sy.add_argument("--no-original-retry", action="store_true")
    _add_db_args(sy)
    sy.set_defaults(func=_cmd_sync)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func = args.func
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
