"""
Jinja2 renderer — renders account record to markdown and uploads to silver-md container.

Output blob path: account/{Account ID}.md
Idempotency: skips upload if md_hash is unchanged (compares against existing blob metadata).
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from azure.storage.blob import BlobClient, ContentSettings

from transform.aggregator import _strip_html

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
SILVER_PREFIX = "account"


def _build_env(templates_dir: Path | None = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir or TEMPLATES_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["strip_html"] = _strip_html
    return env


def render_account_md(record: dict, templates_dir: Path | None = None, template=None) -> str:
    """Render one account record to a markdown string.

    Pass a prebuilt `template` (jinja2.Template) to skip per-call Environment
    construction + template loading — the hot path when rendering thousands of
    accounts in one run.
    """
    tmpl = template or _build_env(templates_dir).get_template("account.md.j2")
    body = tmpl.render(
        account=record["account"],
        contacts=record["contacts"],
        opportunities=record["opportunities"],
        activities=record["activities"],
        notes=record.get("notes", []),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return body


def compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _upload_md_if_changed(
    silver_container_client,
    blob_name: str,
    content: str,
    metadata: dict | None = None,
) -> bool:
    """Upload markdown to silver-md, skipping if md_hash is unchanged.

    Returns True if uploaded (new or changed), False if skipped.
    """
    md_hash = compute_hash(content)
    blob_client: BlobClient = silver_container_client.get_blob_client(blob_name)
    try:
        props = blob_client.get_blob_properties()
        if props.metadata.get("md_hash", "") == md_hash:
            logger.debug("Skipping %s — hash unchanged", blob_name)
            return False
    except Exception:
        pass  # Blob doesn't exist yet — upload unconditionally

    blob_client.upload_blob(
        content.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
        metadata={"md_hash": md_hash, **(metadata or {})},
    )
    logger.info("Uploaded %s (hash=%s)", blob_name, md_hash)
    return True


def upload_if_changed(
    silver_container_client,
    account_id: str,
    content: str,
) -> bool:
    """Upload account markdown to account/{Account ID}.md (hash-skip)."""
    return _upload_md_if_changed(
        silver_container_client,
        f"{SILVER_PREFIX}/{account_id}.md",
        content,
        {"account_id": account_id},
    )


def render_and_upload_all(
    records: list[dict],
    silver_container_client,
    templates_dir: Path | None = None,
    max_workers: int = 16,
) -> dict:
    """Render and upload all records. Returns summary counts.

    Compiles the Jinja2 template once and fans the per-account render+upload out
    across a thread pool — the work is I/O-bound (one get_blob_properties + maybe
    one upload per account), so threads collapse ~9,300 sequential round-trips that
    were overrunning the 10-min Consumption-plan timeout (FunctionTimeoutException).
    """
    template = _build_env(templates_dir).get_template("account.md.j2")

    def _process(rec: dict) -> str:
        account_id = rec["account"].get("Account ID", "")
        if not account_id:
            return "errors"
        try:
            md = render_account_md(rec, template=template)
            changed = upload_if_changed(silver_container_client, account_id, md)
            return "uploaded" if changed else "skipped"
        except Exception as exc:
            logger.exception("Error rendering account %s: %s", account_id, exc)
            return "errors"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(_process, records))

    return {
        "uploaded": outcomes.count("uploaded"),
        "skipped":  outcomes.count("skipped"),
        "errors":   outcomes.count("errors"),
    }


def render_lens_md(lens_record: dict, template) -> str:
    """Render one lens record. The record's fields are passed straight to the
    template (e.g. solution/deals, salesperson/activities, industry/accounts)."""
    return template.render(
        generated_at=datetime.now(timezone.utc).isoformat(),
        **{k: v for k, v in lens_record.items() if k != "key"},
    )


def render_and_upload_lens(
    lens_records: list[dict],
    silver_container_client,
    template_name: str,
    prefix: str,
    templates_dir: Path | None = None,
    max_workers: int = 16,
) -> dict:
    """Render + upload a cross-cutting lens to ``{prefix}/{key}.md``.

    Mirrors render_and_upload_all (compile-once + threaded I/O). Each record must
    carry a ``key`` (slug → blob name); remaining fields feed the template.
    """
    template = _build_env(templates_dir).get_template(template_name)

    def _process(rec: dict) -> str:
        key = rec.get("key", "")
        if not key:
            return "errors"
        try:
            md = render_lens_md(rec, template)
            # NB: blob metadata values must be ASCII — the key (a slug that may
            # contain Thai/JP, e.g. industry "…/製造業") lives in the blob name,
            # so we only stamp the ASCII lens type here.
            changed = _upload_md_if_changed(
                silver_container_client, f"{prefix}/{key}.md", md,
                {"lens": prefix},
            )
            return "uploaded" if changed else "skipped"
        except Exception as exc:
            logger.exception("Error rendering %s/%s: %s", prefix, key, exc)
            return "errors"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(_process, lens_records))

    return {
        "uploaded": outcomes.count("uploaded"),
        "skipped":  outcomes.count("skipped"),
        "errors":   outcomes.count("errors"),
    }
