"""Group cdk-nag findings from a captured synth log: rule, appliesTo, construct path.
Usage: python tools/nag_findings.py synth.log"""
import collections
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
HEAD = re.compile(r"^(ERROR|WARNING) ((?:AwsSolutions-[A-Z0-9]+)|CdkNagValidationFailure)(?:\[(.*?)\])?:")
lines = open(sys.argv[1], encoding="utf-8-sig", errors="replace").read().splitlines()
found = []
for i, line in enumerate(lines):
    m = HEAD.match(line.strip())
    if not m:
        continue
    path = next((x.strip() for x in lines[i + 1:i + 4] if x.strip().startswith("Adi-")), "?")
    rule = m.group(2)
    if rule == "CdkNagValidationFailure":
        inner = re.search(r"\[(AwsSolutions-[A-Z0-9]+)\]", line)
        rule = f"ValidationFailure:{inner.group(1) if inner else '?'}"
    found.append((m.group(1), rule, m.group(3) or "", path))

errors = [f for f in found if f[0] == "ERROR"]
print(f"errors={len(errors)} warnings={len(found) - len(errors)}")
by_rule = collections.Counter(f[1] for f in errors)
print("by rule:", dict(by_rule.most_common()))
for sev, rule, applies, path in sorted(set(found), key=lambda f: (f[1], f[3], f[2])):
    print(f"{sev[0]} {rule:22s} {path:90s} {applies}")
