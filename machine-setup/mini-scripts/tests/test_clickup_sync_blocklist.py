# tests/test_clickup_sync_blocklist.py
import importlib.util
import json
import os
from pathlib import Path
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = REPO_ROOT / "clickup_sync.py"


def load_sync_with_blocklist(blocklist_path: str):
    """Load a fresh clickup_sync module instance against a chosen blocklist."""
    saved = os.environ.get("IGNITE_BLOCKLIST_JSON")
    os.environ["IGNITE_BLOCKLIST_JSON"] = blocklist_path
    try:
        spec = importlib.util.spec_from_file_location(
            f"clickup_sync_test_{os.path.basename(blocklist_path).replace('.', '_')}",
            str(SYNC_PATH),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is None:
            os.environ.pop("IGNITE_BLOCKLIST_JSON", None)
        else:
            os.environ["IGNITE_BLOCKLIST_JSON"] = saved


def test_default_blocklist_falls_back_when_file_missing():
    mod = load_sync_with_blocklist("/tmp/does-not-exist-ignite-blocklist.json")
    assert mod.clickup_project_blocklist() == {"oeconnection", "partstech"}
    assert mod.publish_domain_blocklist() == {"tofinoelopement"}


def test_project_blocklist_matches_folder_and_list_names():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({
            "clickup_project_blocklist": {"alpha": "Folder policy", "beta": "List policy"},
            "publish_domain_blocklist": {"gamma": "Publish policy"},
        }, fh)
        path = fh.name
    try:
        mod = load_sync_with_blocklist(path)
        blocked, reason = mod.is_clickup_project_blocked({
            "folder": {"name": "Alpha workspace"},
            "list": {"name": "Other"},
        })
        assert blocked and reason == "folder-name:'Alpha workspace'"

        blocked, reason = mod.is_clickup_project_blocked({
            "folder": {"name": "Other"},
            "list": {"name": "beta backlog"},
        })
        assert blocked and reason == "list-name:'beta backlog'"
    finally:
        os.unlink(path)


def test_publish_domain_blocklist_matches_host_and_url():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({
            "clickup_project_blocklist": {"alpha": "Folder policy"},
            "publish_domain_blocklist": {"tofinoelopement": "Publish policy"},
        }, fh)
        path = fh.name
    try:
        mod = load_sync_with_blocklist(path)
        blocked, reason = mod.is_publish_domain_blocked("https://tofinoelopement.ca/path")
        assert blocked and reason == "domain:'tofinoelopement.ca'"
        blocked, reason = mod.is_publish_domain_blocked("tofinoelopement.ca")
        assert blocked and reason == "domain:'tofinoelopement.ca'"
    finally:
        os.unlink(path)
