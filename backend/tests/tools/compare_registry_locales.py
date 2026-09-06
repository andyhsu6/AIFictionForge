"""todo15 acceptance script: ERROR_REGISTRY <-> errors.json (zh/en) full comparison.

Checks:
  1. every registry code (minus email.*) has a zh AND en errors.json key
  2. every flattened locale key maps back to a registry code OR a documented
     frontend-only key
  3. zh value == registry default detail (byte-exact) for registry-derived keys
  4. common 422 pydantic-type keys exist (validation.int_parsing/string_type/missing)
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/tests/tools/x.py -> repo root
sys.path.insert(0, str(REPO_ROOT / "backend"))
from app.core.errors import ERROR_REGISTRY  # noqa: E402

LOCALES = {
    "zh": REPO_ROOT / "frontend/src/locales/zh/errors.json",
    "en": REPO_ROOT / "frontend/src/locales/en/errors.json",
}

# documented frontend-only keys (not registry codes; UI labels / fallbacks /
# handler-generated pydantic types). task.* UI labels are the camelCase ones.
FRONTEND_ONLY = {
    "http_error", "requestFailed", "unknown",
    "network.error",
    "http.badRequest", "http.errorWithStatus", "http.forbidden",
    "http.notFound", "http.serviceUnavailable", "http.unauthorized",
    "validation.failed", "validation.int_parsing", "validation.string_type",
    "validation.missing",
    "security.url_invalid",  # frontend-defined generic for unregistered security codes
    # _other sibling of the registry code validation.characters_selected_min_one
    # (the suffix is baked into the CODE name; the sibling exists only to keep
    # the frontend plural-group ratchet complete — never resolved with count)
    "validation.characters_selected_min_other",
} | {
    f"task.{k}" for k in (
        "batchListFailed", "cancelBatchFailed", "cancelFailed", "clearFailed",
        "createChapterFailed", "createFailed", "deleteFailed", "listFailed",
        "queryStatusFailed",
    )
}

EMAIL_PREFIX = "email."


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, prefix=f"{key}."))
        else:
            out[key] = v
    return out


def main():
    report = []
    locales = {lang: flatten(json.load(open(path))) for lang, path in LOCALES.items()}

    registry_codes = set(ERROR_REGISTRY)
    frontend_codes = {c for c in registry_codes if not c.startswith(EMAIL_PREFIX)}

    # ---- check 1: registry code -> zh & en key ----
    missing = sorted(c for c in frontend_codes if c not in locales["zh"] or c not in locales["en"])
    report.append(f"[1] registry codes (minus email.*): {len(frontend_codes)} (total registry incl. email.*: {len(registry_codes)})")
    report.append(f"[1] registry codes without zh+en locale key: {len(missing)}")
    for c in missing:
        report.append(f"    MISSING {c}")

    # ---- check 2: locale key -> registry code or documented frontend-only ----
    zh_keys = set(locales["zh"])
    en_keys = set(locales["en"])
    allowed = frontend_codes | FRONTEND_ONLY
    orphans = {
        "zh": sorted(k for k in zh_keys - allowed),
        "en": sorted(k for k in en_keys - allowed),
    }
    report.append(f"[2] locale keys zh={len(zh_keys)} en={len(en_keys)}")
    report.append(f"[2] documented frontend-only keys: {len(FRONTEND_ONLY)}")
    report.append(f"[2] zh orphans (not registry / not documented): {len(orphans['zh'])}")
    for k in orphans["zh"]:
        report.append(f"    ORPHAN zh {k}")
    report.append(f"[2] en orphans (not registry / not documented): {len(orphans['en'])}")
    for k in orphans["en"]:
        report.append(f"    ORPHAN en {k}")

    # ---- check 3: zh byte-exact vs registry default detail ----
    # dynamic_detail is a registry MARKER: its detail is intentionally empty
    # (dynamic sites always pass explicit detail; the display text is the
    # frontend's generic locale entry), so it is exempt from the byte-exact rule.
    BYTE_EXACT_EXEMPT = {"dynamic_detail"}
    mismatches = []
    for code in sorted(frontend_codes):
        if code not in locales["zh"] or code in BYTE_EXACT_EXEMPT:
            continue
        want = ERROR_REGISTRY[code][0]
        got = locales["zh"][code]
        if want != got:
            mismatches.append((code, want, got))
    report.append(f"[3] zh != registry default detail: {len(mismatches)}")
    for code, want, got in mismatches:
        report.append(f"    MISMATCH {code}\n      registry: {want!r}\n      zh:       {got!r}")

    # ---- check 3b: en keys present for all registry codes (parity zh<->en) ----
    zh_minus_en = sorted(zh_keys - en_keys)
    en_minus_zh = sorted(en_keys - zh_keys)
    report.append(f"[3b] zh keys missing in en: {len(zh_minus_en)} {zh_minus_en}")
    report.append(f"[3b] en keys missing in zh: {len(en_minus_zh)} {en_minus_zh}")

    # ---- check 4: common 422 pydantic-type keys ----
    need = ["validation.int_parsing", "validation.string_type", "validation.missing"]
    gaps = [k for k in need if k not in locales["zh"] or k not in locales["en"]]
    report.append(f"[4] common 422 pydantic-type keys missing: {len(gaps)} {gaps}")

    # ---- unused {{param}} in templates whose site never passes it: N/A here ----
    # (site-level params audited manually; see evidence file task-15)

    ok = not missing and not any(orphans.values()) and not mismatches and not zh_minus_en and not en_minus_zh and not gaps
    report.append(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return "\n".join(report)


if __name__ == "__main__":
    print(main())
