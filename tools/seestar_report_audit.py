r"""Local, read-only structural audit of DB-02 hardware validation reports.

    python tools\seestar_report_audit.py report1.json report2.json ... > audit_summary.txt

What it does: opens each report for reading only, checks its structure and the
presence of the evidence the validator is supposed to record, and scans for
sanitization violations. What it never does: connect to anything, run any
RPC, modify or delete an input, or print an address, serial, fingerprint,
secret or raw telemetry value. Output is limited to step statuses, fixed
vocabulary values, counts and pass/fail flags, and is ASCII only.

This tool checks reports. It is not hardware evidence and never declares a
stage accepted.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys

EXPECTED_STEPS = [
    "credential configuration present",
    "discovery returns one runtime Device Reference",
    "connection established with identity verified",
    "evidence refresh reaches ready",
    "capability report is fresh and read-only",
    "telemetry sample carries host observation time",
    "preview availability is evidence only",
    "mount and view state unchanged across the session",
    "reconnect uses a new connection identity",
]
ALLOWED_TOP_KEYS = {"all_passed", "steps"}
ALLOWED_STEP_KEYS = {"step", "ok", "outcome", "error_category", "model", "firmware", "device_fingerprint",
                     "state", "supported", "states", "items", "availability"}
EXPECTED_CAPABILITIES = {"identity.read", "state.read", "telemetry.read", "preview.availability.read"}
STATE_VOCAB = {"known", "stale", "unknown", "unavailable"}
AVAILABILITY_VOCAB = {"available", "unavailable", "unknown"}
NETWORK_CATEGORIES = {"connect_failed", "connect_timeout", "read_timeout", "send_timeout", "connection_lost", "connection_closed"}
AUTH_CATEGORIES = {"auth_rejected", "key_unusable", "crypto_library_missing", "key_not_configured"}
OTHER_CATEGORIES = {"rpc_error", "protocol_error", "identity_unavailable", "malformed_frame", "malformed_state", "frame_too_large",
                    "too_many_events", "no_discovery_source", "udp_discovery_disabled", "not_connected", "unknown_device",
                    "identity_mismatch"}
MODEL_FORMAT = re.compile(r"^[A-Za-z][A-Za-z0-9 ._-]{0,39}$")
FIRMWARE_FORMAT = re.compile(r"^\d{1,3}(\.\d{1,3}){0,2}$")
SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{4}$")
LEAK_PATTERNS = {
    "ipv4_address": r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
    "ipv6_like": r"\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){3,}\b",
    "hex_run_8plus": r"\b[0-9a-fA-F]{8,}\b",
    "base64_like_24plus": r"[A-Za-z0-9+/]{24,}={0,2}",
    "key_or_pem_marker": r"(?i)\.pem\b|BEGIN [A-Z ]+KEY|PRIVATE KEY",
    "filesystem_path": r"(?i)[A-Za-z]:\\\\|\\\\\\\\|/home/|/users/|/etc/",
    "wifi_or_credential_term": r"(?i)passwd|password|ssid|gateway|netmask|key_mgmt|cpuid|location_lon_lat|secret|token",
    "serial_term": r"(?i)\bsn\b|serial",
}


def _label(value, fmt):
    """Print a value only if it has the exact expected shape and matches no leak pattern."""
    if not isinstance(value, str) or not fmt.match(value):
        return "<unexpected-format>"
    if any(re.search(rx, value) for rx in LEAK_PATTERNS.values()):
        return "<unexpected-format>"
    return value


def _category_class(category):
    if category in NETWORK_CATEGORIES:
        return "network"
    if category in AUTH_CATEGORIES:
        return "auth"
    if category in OTHER_CATEGORIES:
        return "other"
    return "unrecognized" if category else "none"


def audit_one(path):
    """Return a sanitized description of one report. Never raises on malformed content."""
    result = {"name": os.path.basename(path), "violations": [], "missing": [], "kind": "unreadable",
              "steps_ok": 0, "steps_total": 0, "leaks": {}, "facts": {}, "fingerprint_token": None}
    try:
        stat = os.stat(path)
        result["mtime"] = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "rb") as handle:  # read-only
            raw_bytes = handle.read()
    except OSError:
        result["violations"].append("file_not_readable")
        return result
    result["sha"] = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw = raw_bytes.decode("utf-8-sig")
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        result["violations"].append("not_valid_utf8_json")
        return result
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        result["violations"].append("missing_steps_list")
        return result

    result["leaks"] = {name: len(re.findall(rx, raw)) for name, rx in LEAK_PATTERNS.items()}
    for name, count in result["leaks"].items():
        if count:
            result["violations"].append(f"sanitization:{name}:{count}")

    extra_top = sorted(k for k in data if k not in ALLOWED_TOP_KEYS)
    if extra_top:
        result["violations"].append("unexpected_top_level_keys:" + ",".join(k if SAFE_KEY.match(k) else "<redacted>" for k in extra_top))
    steps = [s for s in data["steps"] if isinstance(s, dict)]
    result["steps_total"] = len(steps)
    result["steps_ok"] = sum(1 for s in steps if s.get("ok") is True)
    names = [s.get("step") for s in steps]
    for s in steps:
        bad = sorted(k for k in s if k not in ALLOWED_STEP_KEYS)
        if bad:
            result["violations"].append("unexpected_step_keys:" + ",".join(k if SAFE_KEY.match(k) else "<redacted>" for k in bad))
        for key, value in s.items():
            if isinstance(value, str) and len(value) > 80 and key != "step":
                result["violations"].append("overlong_string_value")

    facts = result["facts"]
    if names == EXPECTED_STEPS:
        result["kind"] = "full_pass" if result["steps_ok"] == len(EXPECTED_STEPS) and data.get("all_passed") is True else "full_with_failures"
    elif names == EXPECTED_STEPS[:len(names)] and names and steps[-1].get("ok") is False:
        result["kind"] = "early_stop"
        facts["stopped_at"] = names[-1]
    else:
        result["kind"] = "unknown_shape"
        result["violations"].append("step_sequence_not_recognized")

    def by(i):
        return steps[i] if i < len(steps) else {}

    discovery = by(1)
    if "outcome" in discovery:
        facts["discovery_outcome"] = discovery["outcome"] if discovery["outcome"] in {
            "devices_found", "empty_valid", "empty_required_device_missing", "nonfatal_failure", "fatal_failure"} else "<unrecognized>"
    if "error_category" in discovery:
        facts["discovery_error_category"] = discovery["error_category"] if discovery["error_category"] in (
            NETWORK_CATEGORIES | AUTH_CATEGORIES | OTHER_CATEGORIES) else ("none" if discovery["error_category"] is None else "<unrecognized>")
        facts["discovery_error_class"] = _category_class(discovery["error_category"])
    if result["kind"] in ("full_pass", "full_with_failures"):
        for index, field, check in (
            (1, "outcome", lambda v: v == "devices_found"),
            (1, "model", lambda v: _label(v, MODEL_FORMAT) != "<unexpected-format>"),
            (1, "firmware", lambda v: _label(v, FIRMWARE_FORMAT) != "<unexpected-format>"),
            (1, "device_fingerprint", lambda v: isinstance(v, str) and FINGERPRINT.match(v)),
            (2, "state", lambda v: v == "connected"),
            (4, "supported", lambda v: isinstance(v, list) and set(v) == EXPECTED_CAPABILITIES),
            (5, "states", lambda v: isinstance(v, dict) and set(v) <= STATE_VOCAB and all(isinstance(c, int) and c >= 0 for c in v.values())),
            (5, "items", lambda v: isinstance(v, int) and v > 0),
            (6, "availability", lambda v: v in AVAILABILITY_VOCAB),
        ):
            step = by(index)
            if field not in step:
                result["missing"].append(f"step{index + 1}.{field}")
            elif not check(step[field]):
                result["violations"].append(f"step{index + 1}.{field}:unexpected_value")
        states, items = by(5).get("states"), by(5).get("items")
        if isinstance(states, dict) and isinstance(items, int) and sum(v for v in states.values() if isinstance(v, int)) != items:
            result["violations"].append("telemetry_state_counts_do_not_sum_to_items")
        if isinstance(states, dict):
            facts["telemetry_state_counts"] = {k: states[k] for k in sorted(states) if k in STATE_VOCAB and isinstance(states[k], int)}
            facts["telemetry_items"] = items if isinstance(items, int) else None
        facts["model"] = _label(discovery.get("model"), MODEL_FORMAT)
        facts["firmware"] = _label(discovery.get("firmware"), FIRMWARE_FORMAT)
        facts["preview_availability"] = by(6).get("availability") if by(6).get("availability") in AVAILABILITY_VOCAB else "<unrecognized>"
        fp = discovery.get("device_fingerprint")
        if isinstance(fp, str) and FINGERPRINT.match(fp):
            result["fingerprint_token"] = hashlib.sha256(fp.encode()).hexdigest()  # compared across reports, never printed
    return result


def audit(paths):
    reports = [audit_one(p) for p in paths]
    seen = {}
    for index, report in enumerate(reports):
        sha = report.get("sha")
        if sha and sha in seen:
            # Informational only: reports hold no timestamps, so healthy repeat sessions can be byte-identical.
            report["facts"]["identical_content_to_report"] = seen[sha] + 1
        elif sha:
            seen[sha] = index
    return reports


def render(reports):
    lines = ["DB-02 REPORT AUDIT (structure and sanitization only; no addresses, serials, fingerprints or raw telemetry are printed)", ""]
    groups = {}
    for report in reports:
        token = report["fingerprint_token"]
        if token is not None:
            groups.setdefault(token, len(groups))
    for index, report in enumerate(reports, 1):
        lines.append(f"[{index}] {report['name']}  modified={report.get('mtime', 'n/a')}  kind={report['kind']}  steps_ok={report['steps_ok']}/{report['steps_total']}")
        for key, value in sorted(report["facts"].items()):
            lines.append(f"      {key}: {value}")
        if report["fingerprint_token"] is not None:
            lines.append(f"      device_identity_group: {chr(65 + groups[report['fingerprint_token']])}  (same letter = same device identity; value not printed)")
        if report["leaks"]:
            lines.append("      leak_pattern_counts: " + ", ".join(f"{k}={v}" for k, v in report["leaks"].items()))
        lines.append("      missing_evidence: " + (", ".join(report["missing"]) or "none"))
        lines.append("      violations: " + (", ".join(report["violations"]) or "none"))
        lines.append("")
    ordered = sorted(enumerate(reports), key=lambda pair: (pair[1].get("mtime", ""), pair[0]))
    lines.append("TIMELINE by file modification time: " + " -> ".join(f"[{i + 1}]{r['kind']}" for i, r in ordered))
    kinds = [r["kind"] for _i, r in ordered]
    loss = [n for n, r in enumerate(r for _i, r in ordered) if r["kind"] == "early_stop" and r["facts"].get("discovery_error_class") == "network"]
    recovered = bool(loss) and sum(1 for k in kinds[loss[0] + 1:] if k == "full_pass") >= 1
    lines.append(f"LOSS PATTERN: network-category discovery failure present={bool(loss)}; full passing session(s) after it={recovered}")
    lines.append("  Each report is a separate validator process. A later passing report shows recovery after a restart,")
    lines.append("  not automatic reconnect within one session.")
    total_violations = sum(len(r["violations"]) for r in reports)
    total_missing = sum(len(r["missing"]) for r in reports if r["kind"] in ("full_pass", "full_with_failures"))
    lines += ["",
              f"CHECK RESULT: {'STRUCTURE_AND_SANITIZATION_OK' if total_violations == 0 and total_missing == 0 else 'ISSUES_FOUND'} "
              f"(violations={total_violations}, missing_evidence={total_missing})",
              "",
              "NOT PROVEN BY THESE REPORTS (a report cannot show these):",
              "  - that the telescope did not move (user observation only)",
              "  - that no camera mode was started (state comparison inside one session only; no independent confirmation)",
              "  - that read requests are refused without the handshake",
              "  - automatic reconnect within one session",
              "  - when each session ran: reports contain no timestamps, ordering relies on file modification times,",
              "    and byte-identical reports (possible for repeat healthy sessions) cannot be told apart from copies",
              "This tool never declares a stage accepted."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only sanitized audit of DB-02 validation reports.")
    parser.add_argument("reports", nargs="+", help="report files or wildcards")
    args = parser.parse_args(argv)
    paths = []
    for pattern in args.reports:
        matches = sorted(glob.glob(pattern))
        paths.extend(matches if matches else [pattern])
    reports = audit(paths)
    text = render(reports)
    sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")
    bad = any(r["violations"] or (r["missing"] and r["kind"] in ("full_pass", "full_with_failures")) for r in reports)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
