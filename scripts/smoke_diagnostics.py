"""Run the read-only site diagnosis against the REAL network for one domain you own or may test (manual).

    python scripts/smoke_diagnostics.py exemplo.com.br

Only that domain (and its subdomains) is authorized for the run. Nothing is changed anywhere; the registry
lookup uses the public RDAP bootstrap service (rdap.org). The result is printed as JSON.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from team.diagnostics import DiagnosticRefused, Diagnostics, SystemNetwork, normalize_host  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        host = normalize_host(sys.argv[1])
    except DiagnosticRefused as refused:
        print(refused)
        return 2
    diagnostics = Diagnostics(SystemNetwork(), lambda candidate: candidate == host or candidate.endswith("." + host))
    print(json.dumps(diagnostics.diagnose(host), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
