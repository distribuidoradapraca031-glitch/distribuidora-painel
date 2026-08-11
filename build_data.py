#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monta webapp/painel_data.json (dashboard_data + notas_relatorio + catalogo)
para o app web servir o painel completo com gráficos. Roda LOCAL, no Mac.
Uso: python3 build_data.py  (depois: git add painel_data.json && git push -> Render)"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.abspath(os.path.join(HERE, ".."))
D = os.path.join(DIST, "dashboard")
NR = os.path.join(DIST, "data", "notas_relatorio.json")

data = json.load(open(os.path.join(D, "dashboard_data.json")))
try:
    data["notasrel"] = json.load(open(NR))
except FileNotFoundError:
    data["notasrel"] = {"notas": [], "custos": [], "sugestoes": [], "alertas": [],
                        "status": "Sem relatório ainda.", "ultima_rodada": "", "gerado_em": ""}
try:
    data["catalogo"] = json.load(open(os.path.join(D, "catalogo.json")))
except FileNotFoundError:
    data["catalogo"] = {"gerado_em": "", "fornecedores": [], "produtos": []}

out = os.path.join(HERE, "painel_data.json")
json.dump(data, open(out, "w"), ensure_ascii=False)
print("painel_data.json gerado:", out)
print("  gráficos até:", data.get("gerado_em"), "| catálogo:", data.get("catalogo", {}).get("gerado_em"),
      "| produtos:", len(data.get("catalogo", {}).get("produtos", [])))
