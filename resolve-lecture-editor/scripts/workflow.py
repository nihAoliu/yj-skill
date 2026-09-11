#!/usr/bin/env python3
"""Local-only frame math, evidenced quote checks, approvals and atomic job journal.

No Resolve connection, network, media generation or credential handling.
"""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from uuid import uuid4


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def filehash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(dt.tzinfo is not None, "timestamp must include timezone")
    return dt


def fps(value):
    s = str(value).replace(" DF", "").strip()
    f = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
         "59.94": Fraction(60000, 1001)}.get(s)
    f = f if f is not None else Fraction(s)
    require(f > 0, "fps must be positive")
    return f


def nearest(value):
    """Nearest frame, ties away from zero; independent of Python banker's rounding."""
    x = Fraction(value)
    return (2 * x.numerator + x.denominator) // (2 * x.denominator) if x >= 0 else -nearest(-x)


def tc_frames(tc, rate, drop=False):
    """Timecode label -> frame count, no timeline/source-origin assumption."""
    f = fps(rate)
    match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2})[:;](\d{2})", tc)
    require(match is not None, "invalid timecode")
    h, m, s, n = map(int, match.groups())
    nominal = nearest(f)
    require(m < 60 and s < 60 and n < nominal, "timecode component out of range")
    require(";" not in tc or drop, "semicolon requires explicit drop=True")
    total = ((h * 60 + m) * 60 + s) * nominal + n
    if drop:
        require(f in (Fraction(30000, 1001), Fraction(60000, 1001)), "unsupported drop-frame rate")
        d = 2 if nominal == 30 else 4
        require(not (m % 10 and s == 0 and n < d), "nonexistent drop-frame timecode")
        minutes = h * 60 + m
        total -= d * (minutes - minutes // 10)
    return total


def frame_window(in_ms, out_ms, rate, origin_frame=0):
    require(out_ms > in_ms >= 0, "invalid millisecond interval")
    a = int(origin_frame) + nearest(Fraction(str(in_ms)) * fps(rate) / 1000)
    b = int(origin_frame) + nearest(Fraction(str(out_ms)) * fps(rate) / 1000)
    require(b > a, "interval rounds to zero frames")
    return {"in_frame": a, "out_frame": b, "duration_frames": b - a,
            "marker_frame": a - int(origin_frame)}


def validate_plan(plan):
    f = fps(plan["fps"])
    start, end = plan["start_frame"], plan["end_frame_exclusive"]
    require(type(start) is int and type(end) is int and end > start, "invalid timeline bounds")
    ids, lanes = set(), {}
    for item in plan["items"]:
        key = item["id"]
        require(key not in ids, "duplicate item id: " + key)
        ids.add(key)
        a, b = item["in_frame"], item["out_frame"]
        require(type(a) is int and type(b) is int and start <= a < b <= end, "invalid interval: " + key)
        require(item["kind"] in ("broll", "title"), "unknown item kind")
        lane = item.get("lane", item["kind"])
        lanes.setdefault(lane, []).append((a, b, key))
    for lane, intervals in lanes.items():
        intervals.sort()
        for left, right in zip(intervals, intervals[1:]):
            require(left[1] <= right[0], "overlap on " + lane + ": " + left[2] + "/" + right[2])
    return {"items": len(ids), "fps": str(f), "plan_hash": digest(plan)}


def generation_hash(doc):
    require(doc.get("shots"), "no generation shots")
    ids, inputs = set(), []
    for s in doc["shots"]:
        key = s["id"]
        require(key not in ids, "duplicate generation id")
        ids.add(key)
        require(all(s.get(k) == "approved" for k in
                    ("plan_status", "start_status", "end_status")), "unapproved shot: " + key)
        for k in ("start_prompt", "end_prompt", "motion_prompt", "sfx_prompt", "style_hash"):
            require(isinstance(s.get(k), str) and s[k].strip(), "missing " + k)
        require(s.get("audio_strategy") == "native_sfx", "native_sfx required")
        require(s.get("generation_parameters"), "missing generation_parameters")
        inputs.append({k: s[k] for k in ("id", "start_prompt", "end_prompt", "motion_prompt",
                                         "sfx_prompt", "style_hash", "audio_strategy", "generation_parameters")})
        inputs[-1].update(start_hash=filehash(s["start_path"]), end_hash=filehash(s["end_path"]))
    return digest(sorted(inputs, key=lambda x: x["id"]))


def money(value):
    require(isinstance(value, str), "money and quantities must be decimal strings")
    d = Decimal(value)
    require(d.is_finite() and d >= 0, "negative or non-finite money/quantity")
    return d


def validate_quote(q, gen):
    require(q.get("provider") and q.get("quote_id"), "provider and quote_id required")
    require(q["generation_hash"] == generation_hash(gen), "generation inputs changed")
    checked = timestamp(q["checked_at"])
    until = timestamp(q["valid_until"])
    current = datetime.now(timezone.utc)
    require(checked <= current < until, "quote stale or future dated")
    require(q.get("validity_basis"), "explain quote validity")
    require(q.get("official_source_verified") is True, "official source must be checked by agent")
    require(q.get("pricing_complete") is True, "unresolved pricing fields")
    require(re.fullmatch(r"[A-Z]{3}", q["currency"]) is not None, "one ISO currency per quote")
    evidence = q["evidence"]
    require(evidence, "official evidence required")
    for e in evidence.values():
        require(e["url"].startswith("https://") and e.get("basis"), "missing official pricing URL/basis")
        require(timestamp(e["checked_at"]) <= current, "future evidence")
    ids, total, pilot, fixed = set(), Decimal(0), Decimal(0), True
    for job in q["jobs"]:
        key = job["shot_id"]
        require(key not in ids, "duplicate quote shot")
        ids.add(key)
        require(job.get("model") and job.get("request_parameters"), "exact model/parameters required")
        require(job.get("rounding_rule") and job.get("billing_scope"), "rounding/add-on scope required")
        require(job.get("first_frame_field") and job.get("last_frame_field") and
                job["first_frame_field"] != job["last_frame_field"], "distinct frame inputs required")
        require(job.get("native_sfx_evidence") in evidence, "native SFX capability evidence required")
        require(money(job["requested_seconds"]) > 0, "requested seconds must be positive")
        require(job.get("lines"), "price lines required")
        subtotal = Decimal(0)
        for line in job["lines"]:
            require(line.get("evidence_id") in evidence and line.get("unit"), "line lacks evidence/unit")
            require(line.get("quantity_basis"), "quantity derivation required")
            require(line["certainty"] in ("fixed", "estimated", "metered"), "invalid price certainty")
            fixed = fixed and line["certainty"] == "fixed"
            subtotal += money(line["quantity"]) * money(line["unit_price"])
        require(subtotal == money(job["subtotal"]), "incorrect subtotal: " + key)
        total += subtotal
        if key == q["pilot_id"]:
            pilot = subtotal
    require(ids == {s["id"] for s in gen["shots"]}, "quote job set differs from approved generation")
    require(q["pilot_id"] in ids, "pilot missing")
    require(total == money(q["total"]), "incorrect total")
    return {"quote_id": q["quote_id"], "currency": q["currency"], "total": str(total),
            "pilot": str(pilot), "remaining": str(total - pilot), "jobs": len(ids),
            "amount_type": "fixed" if fixed else "estimate", "quote_hash": digest(q)}


def approved(run):
    q, g, a = read(run / "quote.json"), read(run / "generation.json"), read(run / "approval.json")
    validate_quote(q, g)
    require(a["quote_hash"] == digest(q) and a.get("confirmation"), "quote approval changed/missing")
    return q


def db(run):
    c = sqlite3.connect(run / "jobs.sqlite", timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS jobs (quote_id TEXT, shot_id TEXT, state TEXT, "
              "job_id TEXT, detail TEXT, updated_at TEXT, PRIMARY KEY(quote_id,shot_id))")
    return c


def claim(run, shot):
    q = approved(run)
    require(shot in {j["shot_id"] for j in q["jobs"]}, "shot not in approved quote")
    if shot != q["pilot_id"]:
        p = read(run / "pilot_approval.json")
        require(p["quote_hash"] == digest(q) and p["mode"] == "all_remaining" and
                p["file_hash"] == filehash(p["path"]), "pilot approval invalid")
    with db(run) as c:
        try:
            c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (q["quote_id"], shot,
                      "submitting", None, "intent recorded before POST", now()))
        except sqlite3.IntegrityError as e:
            raise ValueError("already claimed; reconcile original task, never re-POST") from e
    return {"shot_id": shot, "state": "submitting"}


TRANSITIONS = {
    "submitting": {"submitted", "unknown_submission", "failed"},
    "unknown_submission": {"submitted", "failed"},
    "submitted": {"completed", "failed"},
    "completed": {"downloaded", "download_failed"},
    "download_failed": {"downloaded", "download_failed"},
    "downloaded": {"ready", "conform_failed"},
    "conform_failed": {"ready", "conform_failed"},
    "ready": set(), "failed": set(),
}


def update_job(run, quote_id, shot, state, job_id, detail):
    # Recovery/polling of known jobs must work even after quote expiry.
    with db(run) as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT state,job_id FROM jobs WHERE quote_id=? AND shot_id=?",
                        (quote_id, shot)).fetchone()
        require(old is not None, "unclaimed task")
        require(state in TRANSITIONS[old[0]], "invalid state transition")
        require(not old[1] or not job_id or old[1] == job_id, "cannot change paid job identity")
        identifier = old[1] or job_id
        require(state in ("unknown_submission", "failed") or identifier, "job_id required")
        require(detail.strip(), "record response/error/output evidence")
        c.execute("UPDATE jobs SET state=?,job_id=?,detail=?,updated_at=? WHERE quote_id=? AND shot_id=?",
                  (state, identifier, detail, now(), quote_id, shot))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest="command", required=True)
    for name in ("init", "fingerprint", "quote", "approve", "approve-pilot", "claim", "job", "status"):
        s = sp.add_parser(name)
        s.add_argument("run_dir", type=Path)
        if name in ("approve", "approve-pilot"):
            s.add_argument("--confirmation", required=True)
        if name == "approve-pilot":
            s.add_argument("--path", required=True)
        if name in ("claim", "job"):
            s.add_argument("--shot", required=True)
        if name == "job":
            s.add_argument("--quote-id", required=True)
            s.add_argument("--state", choices=TRANSITIONS, required=True)
            s.add_argument("--job-id")
            s.add_argument("--detail", required=True)
    s = sp.add_parser("plan")
    s.add_argument("path", type=Path)
    args = p.parse_args()
    if args.command == "plan":
        result = validate_plan(read(args.path))
    else:
        run = args.run_dir.resolve()
        if args.command == "init":
            run.mkdir(parents=True, exist_ok=True)
            require(not (run / "state.json").exists(), "run already initialized; resume instead")
            save(run / "state.json", {"schema_version": 1, "run_id": uuid4().hex,
                "phase": "analysis", "edit_mode": "B", "auto_export": False,
                "style_id": "vox-paper-v1", "submission_mode": "all_remaining",
                "original_timeline": None, "edit_timeline": None, "package_timeline": None})
            save(run / "style.json", {"id": "vox-paper-v1", "version": 1,
                "palette": ["cream", "charcoal", "deep red", "mustard"],
                "material": "hand-cut paper, halftone, newsprint, tape, separate rigid pieces",
                "baked_text": False, "caption_safe_area": "derive from target timeline",
                "motion": "one coherent transformation; preserve flat collage"})
            db(run).close()
            result = {"created": str(run)}
        elif args.command == "fingerprint":
            result = {"generation_hash": generation_hash(read(run / "generation.json"))}
        elif args.command in ("quote", "approve"):
            q, g = read(run / "quote.json"), read(run / "generation.json")
            result = validate_quote(q, g)
            if args.command == "approve":
                require(args.confirmation.strip(), "explicit user confirmation required")
                save(run / "approval.json", {"quote_hash": digest(q), "confirmed_at": now(),
                                             "confirmation": args.confirmation})
        elif args.command == "approve-pilot":
            q = approved(run)
            with db(run) as c:
                row = c.execute("SELECT state,job_id FROM jobs WHERE quote_id=? AND shot_id=?",
                                (q["quote_id"], q["pilot_id"])).fetchone()
            require(row and row[0] == "ready" and row[1], "pilot must be ready with job ID")
            require(args.confirmation.strip(), "explicit sample approval required")
            result = {"quote_hash": digest(q), "shot_id": q["pilot_id"], "job_id": row[1],
                      "path": str(Path(args.path).resolve()), "file_hash": filehash(args.path),
                      "mode": "all_remaining", "confirmation": args.confirmation, "confirmed_at": now()}
            save(run / "pilot_approval.json", result)
        elif args.command == "claim":
            result = claim(run, args.shot)
        elif args.command == "job":
            update_job(run, args.quote_id, args.shot, args.state, args.job_id, args.detail)
            result = {"shot_id": args.shot, "state": args.state}
        else:
            with db(run) as c:
                c.row_factory = sqlite3.Row
                result = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY quote_id,shot_id")]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, InvalidOperation, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
