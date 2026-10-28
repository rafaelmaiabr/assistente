"""Choose which public routes the assistant may read (GET, no credentials). See docs/TEAM-CONNECTOR-HTTP.md.

    python scripts/http_routes.py list
    python scripts/http_routes.py enable registro-dominio
    python scripts/http_routes.py disable registro-dominio
    python scripts/http_routes.py add meu-cep "CEP dos Correios" "https://api.exemplo.com/cep/{valor}" cep
    python scripts/http_routes.py remove meu-cep

Only the operator changes this file; the chat and the model cannot. A route is an https address with one {valor} of a known
kind (dominio, cnpj, cep, numero). Nothing is ever sent except the value the person asked about. Restart the app after a change.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import DATA_ROOT  # noqa: E402
from team.connectors.http_routes import KINDS, RouteRejected, RouteStore  # noqa: E402

FILE = "http-routes.json"


def main(argv=None, root=DATA_ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("enable", "disable", "remove"):
        commands.add_parser(name).add_argument("route")
    add = commands.add_parser("add")
    add.add_argument("route")
    add.add_argument("label")
    add.add_argument("template", help="https address with exactly one {valor}")
    add.add_argument("kind", choices=sorted(KINDS))
    args = parser.parse_args(argv)
    store = RouteStore(Path(root) / FILE)
    try:
        if args.command == "list":
            for route, on in store.routes():
                print(f"{'ON ' if on else 'off'}  {route.name:<22} {route.kind:<8} {route.template}  ({route.label}{'' if not route.builtin else ', revisada'})")
        elif args.command in ("enable", "disable"):
            store.enable(args.route, args.command == "enable")
            print(("Ligada: " if args.command == "enable" else "Desligada: ") + args.route)
        elif args.command == "remove":
            store.remove(args.route)
            print("Removida: " + args.route)
        else:
            route = store.add(args.route, args.label, args.template, args.kind)
            print(f"Cadastrada (desligada): {route.name}. Use 'enable {route.name}' para ligar.")
    except RouteRejected as error:
        print(f"Nao foi possivel: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
