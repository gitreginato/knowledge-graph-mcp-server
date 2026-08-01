#!/usr/bin/env python3
"""Exporta o grafo para graph.json (compativel com Obsidian, D3.js, vis.js).
Uso: python3 export.py [output.json]
"""
import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from server import tool_export_json

def main():
    output = sys.argv[1] if len(sys.argv) > 1 else "graph.json"
    data = tool_export_json({})
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Exportado: {output}")
    print(f"  nos: {len(data['nodes'])}")
    print(f"  arestas: {len(data['edges'])}")

if __name__ == "__main__":
    main()
