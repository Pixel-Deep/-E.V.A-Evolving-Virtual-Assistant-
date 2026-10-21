"""Review-first patch proposal and hash-guarded application."""
from __future__ import annotations
import difflib, hashlib, json, os, tempfile
from pathlib import Path

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def propose_text_patch(path: str | Path, new_text: str) -> dict:
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_file(): raise ValueError("Patch target must be a file.")
    original = target.read_text(encoding="utf-8-sig")
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{target.name}", tofile=f"b/{target.name}"))
    return {"target": str(target), "original_sha256": _digest(target.read_bytes()), "proposed_text": new_text,
            "diff": diff, "changed": original != new_text, "status": "review_required"}

def save_patch_proposal(proposal: dict, path: str | Path) -> Path:
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    return out

def apply_patch_proposal(proposal_path: str | Path, *, approved: bool = False) -> dict:
    if not approved: raise PermissionError("Patch not applied. Explicit approval is required.")
    proposal_file = Path(proposal_path).expanduser().resolve(strict=True)
    proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
    if proposal.get("status") != "review_required" or not proposal.get("changed"):
        raise ValueError("Proposal is invalid or contains no changes.")
    target = Path(proposal["target"]).resolve(strict=True)
    if not target.is_file(): raise ValueError("Patch target must remain a file.")
    current = target.read_bytes()
    if _digest(current) != proposal.get("original_sha256"):
        raise RuntimeError("Patch rejected: target changed since proposal; generate a fresh diff.")
    new_text = proposal.get("proposed_text")
    if not isinstance(new_text, str): raise ValueError("Proposal has no replacement text.")
    backup = target.with_name(target.name + ".eva-backup")
    if backup.exists(): raise FileExistsError(f"Backup already exists: {backup}; move it safely before retrying.")
    backup.write_bytes(current)
    fd, temp_name = tempfile.mkstemp(prefix=".eva-patch-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream: stream.write(new_text)
        os.replace(temp_name, target)
    except Exception:
        try: os.unlink(temp_name)
        except OSError: pass
        raise
    proposal["status"] = "applied"
    proposal["backup"] = str(backup)
    proposal_file.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "applied", "target": str(target), "backup": str(backup), "diff": proposal.get("diff", "")}
