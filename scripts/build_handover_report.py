"""Build docs/BACKEND_HANDOVER_REPORT.md from docs/_handover_template.md + the generated endpoint table.

    python scripts/export_endpoints.py && python scripts/build_handover_report.py
"""

from pathlib import Path

template = Path("docs/_handover_template.md").read_text(encoding="utf-8")
table = Path("docs/_endpoint_reference.md").read_text(encoding="utf-8")
Path("docs/BACKEND_HANDOVER_REPORT.md").write_text(template.replace("{{ENDPOINTS}}", table), encoding="utf-8", newline="\n")
print("wrote docs/BACKEND_HANDOVER_REPORT.md")
