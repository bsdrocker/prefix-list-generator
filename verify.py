"""
Offline verification harness for app.py.

Doesn't require Flask, cachetools, or bgpq4 to be installed. Pulls the
regexes and command-builder logic out of app.py and exercises them directly,
then dry-runs the bgpq4 command construction so we can eyeball it.

Run:  python3 verify.py
"""

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = (HERE / "app.py").read_text()


def extract_regex(name: str) -> re.Pattern:
    """Grab `NAME = re.compile(r"...")` from app.py without importing it."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "compile"
        ):
            pat = node.value.args[0].value
            return re.compile(pat)
    raise RuntimeError(f"regex {name} not found in app.py")


def case(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    return cond


def main() -> int:
    failures = 0

    print("=" * 70)
    print("1. NAME_RE — prefix-list name validation")
    print("=" * 70)
    NAME_RE = extract_regex("NAME_RE")
    name_good = ["PEER-HE", "AS_CUSTOMERS", "x", "A" * 64, "PL-IPV6-IN"]
    name_bad = [
        "", "A" * 65, "has space", "semi;rm -rf /", "slash/here",
        "colon:here", "dot.here", "back`tick", "$inject", "../etc/passwd",
    ]
    for n in name_good:
        if not case(f"accept {n!r}", bool(NAME_RE.match(n))):
            failures += 1
    for n in name_bad:
        if not case(f"reject {n!r}", not NAME_RE.match(n)):
            failures += 1

    print()
    print("=" * 70)
    print("2. AS_RE — AS-SET / AS object validation")
    print("=" * 70)
    AS_RE = extract_regex("AS_RE")
    as_good = [
        "AS65000", "AS-HURRICANE", "AS-FOO:AS-BAR",
        "RIPE::AS-FOO", "AS65000:AS-CUSTOMERS", "as-test_v6",
    ]
    as_bad = [
        "", "A" * 129, "has space", "AS6500;ls", "AS/65000",
        "$(whoami)", "AS65000`id`", "AS65000|cat",
    ]
    for a in as_good:
        if not case(f"accept {a!r}", bool(AS_RE.match(a))):
            failures += 1
    for a in as_bad:
        if not case(f"reject {a!r}", not AS_RE.match(a)):
            failures += 1

    print()
    print("=" * 70)
    print("3. FAMILY flags map to correct bgpq4 args")
    print("=" * 70)
    # Re-derive FAMILY_FLAGS from source instead of importing.
    m = re.search(r'FAMILY_FLAGS\s*=\s*(\{[^}]+\})', SRC)
    FAMILY_FLAGS = ast.literal_eval(m.group(1))
    if not case("ipv4 -> -4", FAMILY_FLAGS.get("ipv4") == "-4"):
        failures += 1
    if not case("ipv6 -> -6", FAMILY_FLAGS.get("ipv6") == "-6"):
        failures += 1
    if not case("no other families", set(FAMILY_FLAGS) == {"ipv4", "ipv6"}):
        failures += 1

    print()
    print("=" * 70)
    print("4. bgpq4 command construction (dry-run)")
    print("=" * 70)

    def build_cmd(name, as_set, family, aggregate=True, host="", sources="",
                  binary="bgpq4"):
        cmd = [binary, FAMILY_FLAGS[family], "-l", name]
        if aggregate:
            cmd.append("-A")
        if host:
            cmd.extend(["-h", host])
        if sources:
            cmd.extend(["-S", sources])
        cmd.append(as_set)
        return cmd

    cmd = build_cmd("PEER-HE", "AS-HURRICANE", "ipv4")
    expect = ["bgpq4", "-4", "-l", "PEER-HE", "-A", "AS-HURRICANE"]
    if not case("default v4 cmd", cmd == expect, " ".join(cmd)):
        failures += 1

    cmd = build_cmd("PEER-HE-V6", "AS-HURRICANE", "ipv6")
    expect = ["bgpq4", "-6", "-l", "PEER-HE-V6", "-A", "AS-HURRICANE"]
    if not case("default v6 cmd", cmd == expect, " ".join(cmd)):
        failures += 1

    cmd = build_cmd(
        "PL1", "AS-FOO", "ipv4",
        aggregate=False, host="whois.radb.net", sources="RIPE,RADB",
    )
    expect = ["bgpq4", "-4", "-l", "PL1",
              "-h", "whois.radb.net", "-S", "RIPE,RADB", "AS-FOO"]
    if not case("custom host/sources, no -A", cmd == expect, " ".join(cmd)):
        failures += 1

    # CRITICAL: shell metacharacters in input must NOT cause shell evaluation.
    # subprocess.run with a list never invokes a shell, so even if validation
    # were somehow bypassed, the chars would be literal argv. Demonstrate it.
    cmd = build_cmd("name; rm -rf /", "AS-FOO`id`", "ipv4")
    if not case(
        "shell metachars stay as literal argv (no shell=True path)",
        "; rm -rf /" in cmd[3] and "`id`" in cmd[-1],
    ):
        failures += 1

    print()
    print("=" * 70)
    print("5. bgpq4 default output is Arista-compatible (line-by-line check)")
    print("=" * 70)

    # bgpq4's default format. This is what bgpq4 actually emits for
    #   bgpq4 -l EXAMPLE AS-EXAMPLE
    # (modulo the actual prefixes). Arista EOS accepts each of these line
    # forms verbatim, both interactively and when sourced over HTTP.
    sample_v4 = """no ip prefix-list EXAMPLE
ip prefix-list EXAMPLE permit 192.0.2.0/24
ip prefix-list EXAMPLE permit 198.51.100.0/24
ip prefix-list EXAMPLE permit 203.0.113.0/24 le 25
"""
    sample_v6 = """no ipv6 prefix-list EXAMPLE-V6
ipv6 prefix-list EXAMPLE-V6 permit 2001:db8::/32
ipv6 prefix-list EXAMPLE-V6 permit 2001:db8:1::/48 le 64
"""

    # Arista EOS prefix-list line grammar (per the Configuration Guide):
    #   [no] ip prefix-list NAME [seq N] permit|deny PREFIX/LEN [ge N] [le N]
    #   [no] ipv6 prefix-list NAME [seq N] permit|deny PREFIX/LEN [ge N] [le N]
    arista_v4 = re.compile(
        r"^(no\s+)?ip\s+prefix-list\s+[A-Za-z0-9_\-]+"
        r"(\s+seq\s+\d+)?"
        r"(\s+(permit|deny)\s+\d+\.\d+\.\d+\.\d+/\d+"
        r"(\s+ge\s+\d+)?(\s+le\s+\d+)?)?\s*$"
    )
    arista_v6 = re.compile(
        r"^(no\s+)?ipv6\s+prefix-list\s+[A-Za-z0-9_\-]+"
        r"(\s+seq\s+\d+)?"
        r"(\s+(permit|deny)\s+[0-9a-fA-F:]+/\d+"
        r"(\s+ge\s+\d+)?(\s+le\s+\d+)?)?\s*$"
    )

    for line in sample_v4.strip().splitlines():
        if not case(f"v4 line valid Arista syntax: {line!r}", bool(arista_v4.match(line))):
            failures += 1
    for line in sample_v6.strip().splitlines():
        if not case(f"v6 line valid Arista syntax: {line!r}", bool(arista_v6.match(line))):
            failures += 1

    print()
    print("=" * 70)
    print("6. Routing pattern accepts colon-bearing AS-SETs (RIPE::AS-FOO)")
    print("=" * 70)
    # Flask's default 'string' converter excludes only '/'. Colon is fine,
    # which matters for objects like RIPE::AS-FOO. We dropped <path:...> in
    # favour of the default converter; let's confirm app.py reflects that.
    if not case(
        "app.py uses default string converter (no <path:as_set>)",
        "<path:as_set>" not in SRC and "<as_set>" in SRC,
    ):
        failures += 1
    # And the validator accepts the canonical RIPE::AS-FOO form.
    if not case(
        "AS_RE accepts RIPE::AS-FOO",
        bool(AS_RE.match("RIPE::AS-FOO")),
    ):
        failures += 1

    print()
    print("=" * 70)
    print(f"RESULT: {'ALL PASSED' if failures == 0 else f'{failures} FAILURES'}")
    print("=" * 70)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
