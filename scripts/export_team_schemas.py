"""Regenerate team/schemas/*.schema.json and team/examples/*.json from the executable specs."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from team import export_json_schema, kinds, validate_bundle  # noqa: E402
from team.samples import bundle  # noqa: E402


def render(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def outputs() -> dict:
    files = {f"schemas/{kind}.schema.json": render(export_json_schema(kind)) for kind in kinds()}
    files["examples/team-bundle.json"] = render(validate_bundle(bundle()))
    return files


if __name__ == "__main__":
    generated = outputs()
    for name, text in generated.items():
        (ROOT / "team" / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"{len(generated)} arquivos em team/")
