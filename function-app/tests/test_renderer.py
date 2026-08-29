"""Characterization tests for render_and_upload_all orchestration.

timer_transform was timing out (App Insights: 24/24 FunctionTimeoutException) because
render_and_upload_all (a) rebuilt the Jinja2 Environment + reloaded the template on
every one of ~9,300 records and (b) did a sequential get_blob_properties + upload
network round-trip per account. The fix reuses one compiled template and parallelises
the per-account work. These tests pin the upload/skip/error counting + dedup-by-hash
semantics so the refactor stays behaviour-preserving.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from transform import renderer


class _FakeBlobClient:
    def __init__(self, existing_hash, uploaded_sink, blob_name):
        self._existing_hash = existing_hash
        self._sink = uploaded_sink
        self._name = blob_name

    def get_blob_properties(self):
        if self._existing_hash is None:
            raise Exception("BlobNotFound")
        props = type("P", (), {})()
        props.metadata = {"md_hash": self._existing_hash}
        return props

    def upload_blob(self, data, overwrite=True, content_settings=None, metadata=None):
        self._sink.append(self._name)


class _FakeContainer:
    def __init__(self, existing):
        self.existing = existing  # blob_name -> stored md_hash
        self.uploaded = []

    def get_blob_client(self, blob_name):
        return _FakeBlobClient(self.existing.get(blob_name), self.uploaded, blob_name)


def test_counts_uploaded_skipped_errors(monkeypatch):
    # Stub render so the test exercises orchestration, not the Jinja template.
    monkeypatch.setattr(
        renderer, "render_account_md",
        lambda rec, templates_dir=None, template=None: f"md-{rec['account'].get('Account ID', '')}",
    )

    # A2 already stored with a matching hash → must be skipped.
    a2_hash = renderer.compute_hash("md-A2")
    container = _FakeContainer(existing={"account/A2.md": a2_hash})

    records = [
        {"account": {"Account ID": "A1"}},   # new       -> uploaded
        {"account": {"Account ID": "A2"}},   # unchanged -> skipped
        {"account": {}},                      # no id     -> error
    ]

    summary = renderer.render_and_upload_all(records, container)

    assert summary == {"uploaded": 1, "skipped": 1, "errors": 1}
    assert container.uploaded == ["account/A1.md"]
