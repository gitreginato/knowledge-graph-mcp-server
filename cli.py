#!/usr/bin/env python3
"""CLI para testar o kg-infra manualmente, sem precisar de MCP client.
Uso: python3 cli.py <tool> [json_args]
Ex: python3 cli.py get_architecture '{}'
    python3 cli.py add_node '{"label":"Customer","name":"Teste"}'
    python3 cli.py search_graph '{"name_pattern":"acme"}'
"""
import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from server import TOOLS

def main():
    if len(sys.argv) < 2:
        print("Uso: cli.py <tool> [json_args]")
        print(f"Tools: {', '.join(TOOLS.keys())}")
        sys.exit(1)
    tool_name = sys.argv[1]
    if tool_name not in TOOLS:
        print(f"Tool invalida: {tool_name}")
        print(f"Validas: {', '.join(TOOLS.keys())}")
        sys.exit(1)
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        result = TOOLS[tool_name]["fn"](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
