"""The automatic part of the screen audit, from <shots>/report.json: what each check found, screen by screen (a draft for the report;
the judgement of what is a duplicate or out of place is a person's).

    python tests/e2e/summarize.py [shots]       # prints Markdown"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(shots: Path) -> None:
    report = json.loads((shots / "report.json").read_text(encoding="utf-8"))
    rows = report["screens"]
    print("screens: %d, %ds, outside calls: %d" % (len(rows), report["seconds"], len(report["outside_calls"])))
    for call in Counter(c[0] for c in report["outside_calls"]).most_common():
        print("  outside:", call)
    print("\n## HTTP >= 400 (page itself)")
    for r in rows:
        if not isinstance(r.get("status"), int) or r["status"] >= 400:
            print("- %s %s %s → %s (%s)" % (r["role"], r["vp"], r["url"], r.get("status"), r.get("shot", "")))
    print("\n## sub-requests >= 400")
    sub = defaultdict(set)
    for r in rows:
        for b in r.get("bad_requests", []):
            sub[b].add("%s %s %s" % (r["role"], r["vp"], r["key"]))
    for b, where in sorted(sub.items()):
        print("- %s ← %s" % (b, "; ".join(sorted(where)[:4])))
    print("\n## console errors")
    con = defaultdict(set)
    for r in rows:
        for e in r.get("console", []):
            con[e[:200]].add("%s %s %s" % (r["role"], r["vp"], r["key"]))
    for e, where in sorted(con.items()):
        print("- %s ← %s (%d)" % (e, "; ".join(sorted(where)[:3]), len(where)))
    print("\n## landed elsewhere (redirect)")
    for r in rows:
        if r.get("landed") and r["landed"].split("#")[0] != r["url"] and not r.get("modal"):
            print("- %s %s %s → %s" % (r["role"], r["vp"], r["url"], r["landed"]))
    print("\n## horizontal overflow at 375")
    for r in rows:
        if r["vp"] == "375" and r.get("scrollW", 0) > 376:
            print("- %s %s (scrollWidth %s) %s: %s" % (r["role"], r["key"], r["scrollW"], r["shot"],
                                                      "; ".join("%s [%s]" % (o["el"], o["text"][:30]) for o in r.get("overflow", [])[:3])))
    print("\n## clipped text")
    for r in rows:
        if r.get("clipped"):
            print("- %s %s %s: %s" % (r["role"], r["vp"], r["key"], "; ".join("%s [%s]" % (o["el"], o["text"][:30]) for o in r["clipped"][:3])))
    print("\n## overlapping controls")
    for r in rows:
        if r.get("overlap"):
            print("- %s %s %s: %s" % (r["role"], r["vp"], r["key"], "; ".join("[%s] × [%s] (%s × %s)" % (o["ta"], o["tb"], o["a"], o["b"]) for o in r["overlap"][:3])))
    print("\n## old palette / fonts under body.ui-new")
    old = defaultdict(set)
    for r in rows:
        if "ui-new" not in (r.get("bodyClass") or ""):
            print("- NO ui-new: %s %s %s (%s)" % (r["role"], r["vp"], r["key"], r.get("bodyClass")))
        for k, v in (r.get("oldColors") or {}).items():
            old["colour " + k + " — " + v["el"]].add("%s %s" % (r["role"], r["key"]))
        for k, v in (r.get("oldFonts") or {}).items():
            old["font " + k + " — " + v["el"]].add("%s %s" % (r["role"], r["key"]))
    for k, where in sorted(old.items(), key=lambda kv: -len(kv[1])):
        print("- %s ← %d screens, e.g. %s" % (k, len(where), "; ".join(sorted(where)[:3])))
    print("\n## font sizes off the scale")
    sizes = Counter()
    for r in rows:
        for k, v in (r.get("offScale") or {}).items():
            sizes[(k, v["el"])] += 1
    for (k, el), n in sizes.most_common(25):
        print("- %s %s (%d screens)" % (k, el, n))
    print("\n## inline colour styles")
    inl = Counter()
    for r in rows:
        for i in r.get("inlineStyles", []):
            inl[(i["el"], i["style"])] += 1
    for (el, st), n in inl.most_common(20):
        print("- %s style=%s (%d)" % (el, st, n))
    print("\n## repeated text on one screen")
    rep = defaultdict(set)
    for r in rows:
        for t in r.get("repeated", []):
            rep[t].add("%s %s %s" % (r["role"], r["vp"], r["key"]))
    for t, where in sorted(rep.items(), key=lambda kv: -len(kv[1])):
        print("- «%s» ← %d, e.g. %s" % (t[:100], len(where), "; ".join(sorted(where)[:3])))
    print("\n## modal errors")
    for r in rows:
        if r.get("modal_error"):
            print("- %s %s %s: %s" % (r["role"], r["vp"], r["key"], r["modal_error"][:120]))
    print("\n## broken links")
    for b in report["broken_links"]:
        print("- %s %s → %s (seen on %s)" % (b["role"], b["link"], b["status"], b["seen_on"]))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "shots")
