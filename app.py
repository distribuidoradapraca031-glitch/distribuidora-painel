#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App web da Distribuidora da Praça — painel ao vivo + gravação direta no GestãoClick,
protegido por senha. (Fase 1: login + leitura ao vivo.)"""
import os, functools, time
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, abort
import gclient as gcapi

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or gcapi._tok("FLASK_SECRET_KEY") or "troca-esta-chave"
APP_PASSWORD = os.environ.get("APP_PASSWORD") or gcapi._tok("APP_PASSWORD") or ""
app.permanent_session_lifetime = 60 * 60 * 12  # 12h

# cache simples em memória p/ não bater na API a cada request
_cache = {}
def cached(key, ttl, fn):
    now = time.time()
    # ?fresh=1 força buscar de novo no CRM (ignora o cache)
    if request.args.get("fresh"):
        val = fn(); _cache[key] = (now, val); return val
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val

def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **k):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return wrap

@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        if APP_PASSWORD and request.form.get("senha") == APP_PASSWORD:
            session.permanent = True
            session["auth"] = True
            nxt = request.args.get("next") or url_for("home")
            return redirect(nxt)
        err = "Senha incorreta."
        time.sleep(1.0)  # atrasa tentativa por força bruta
    return render_template("login.html", err=err)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    return render_template("painel.html")

def _num(x):
    try: return float(str(x).replace(",", "."))
    except (TypeError, ValueError): return 0.0

@app.route("/api/resumo")
@login_required
def api_resumo():
    """Números ao vivo pra provar a leitura direta do sistema."""
    def build():
        prods = gcapi.get_all("/produtos")
        ativos = [p for p in prods if str(p.get("ativo")) == "1"]
        val_estoque = sum(_num(p.get("estoque")) * _num(p.get("valor_custo")) for p in ativos)
        negativos = sum(1 for p in ativos if _num(p.get("estoque")) < 0)
        pagar = gcapi.get_all("/pagamentos", {"situacao": "0"}) if False else []
        return {
            "produtos_ativos": len(ativos),
            "valor_estoque": round(val_estoque, 2),
            "itens_negativos": negativos,
            "gerado_em": time.strftime("%d/%m/%Y %H:%M"),
        }
    data = cached("resumo", 60, build)
    return jsonify(data)

@app.route("/api/pagar")
@login_required
def api_pagar():
    """Contas a pagar em aberto (liquidado != 1), ao vivo."""
    def build():
        pgs = gcapi.get_all("/pagamentos")
        abertos = [p for p in pgs if str(p.get("liquidado")) != "1"]
        itens = [{
            "id": p.get("id"),
            "desc": p.get("descricao") or p.get("nome_plano_conta") or "—",
            "fornecedor": p.get("nome_fornecedor") or "",
            "venc": (p.get("data_vencimento") or "")[:10],
            "valor": _num(p.get("valor_total")) or _num(p.get("valor")),
        } for p in abertos]
        itens.sort(key=lambda x: x["venc"] or "9999")
        return {
            "itens": itens,
            "total": round(sum(i["valor"] for i in itens), 2),
            "n": len(itens),
            "gerado_em": time.strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(cached("pagar", 60, build))

# ---- mapeamentos: potes (conta+forma) e categorias de gasto ----
RESERVA_CONTA = os.environ.get("RESERVA_CONTA_ID", "696747")  # trocar p/ conta RESERVA quando existir
POTES = {
    "Caixa":    {"conta": "696747", "forma": "6055919"},        # gaveta + Dinheiro
    "Dinheiro": {"conta": RESERVA_CONTA, "forma": "6055919"},   # reserva (notas altas) + Dinheiro
    "PIX":      {"conta": "681760", "forma": "6055931"},        # conta bancária + PIX
}
CATS = {
    "Lanche": "33015662", "Almoço": "33015662", "Padaria": "33015662",
    "Papelaria": "33015658", "Combustível": "33015633", "Motoboy / entrega": "33015664",
    "Limpeza / higiene": "33015655", "Descartáveis (copo/saco)": "33015669",
    "Manutenção / conserto": "33015656", "Água / luz / internet": "33015649",
    "Retirada do sócio": "33015638", "Outros": "33015669",
}

def _hoje():
    return time.strftime("%Y-%m-%d")

def _invalida(*keys):
    for k in keys:
        _cache.pop(k, None)

@app.route("/api/baixa", methods=["POST"])
@login_required
def api_baixa():
    """Dá baixa (liquida) uma conta a pagar existente."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("id") or "").strip()
    forma = body.get("forma") or "Caixa"
    if not pid:
        return jsonify({"ok": False, "erro": "sem id"}), 400
    pot = POTES.get(forma, POTES["Caixa"])
    # preserva os campos da conta e só marca como paga
    cur = gcapi.get(f"/pagamentos/{pid}")
    p = cur.get("data") or cur
    if isinstance(p, list):
        p = p[0] if p else {}
    p = p.get("Pagamento", p) if isinstance(p, dict) else {}
    payload = {
        "descricao": p.get("descricao") or "Conta",
        "valor": p.get("valor") or p.get("valor_total") or "0",
        "plano_contas_id": p.get("plano_contas_id") or "",
        "data_vencimento": (p.get("data_vencimento") or _hoje())[:10],
        "data_competencia": (p.get("data_competencia") or _hoje())[:10],
        "fornecedor_id": p.get("fornecedor_id") or "",
        "liquidado": "1",
        "data_liquidacao": _hoje(),
        "conta_bancaria_id": pot["conta"],
        "forma_pagamento_id": pot["forma"],
    }
    try:
        gcapi.put(f"/pagamentos/{pid}", payload)
        _invalida("pagar", "resumo")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/gasto", methods=["POST"])
@login_required
def api_gasto():
    """Lança um gasto (despesa) já paga, na categoria e pote certos."""
    body = request.get_json(force=True, silent=True) or {}
    cat = body.get("categoria") or "Outros"
    valor = _num(body.get("valor"))
    forma = body.get("forma") or "Caixa"
    data = (body.get("data") or _hoje())[:10]
    desc = (body.get("descricao") or "").strip() or cat
    if valor <= 0:
        return jsonify({"ok": False, "erro": "valor inválido"}), 400
    pot = POTES.get(forma, POTES["Caixa"])
    payload = {
        "descricao": desc,
        "valor": f"{valor:.2f}",
        "data_vencimento": data,
        "data_competencia": data,
        "data_liquidacao": data,
        "liquidado": "1",
        "plano_contas_id": CATS.get(cat, CATS["Outros"]),
        "conta_bancaria_id": pot["conta"],
        "forma_pagamento_id": pot["forma"],
    }
    try:
        r = gcapi.post("/pagamentos", payload)
        d = r.get("data") or {}
        _invalida("pagar", "resumo")
        return jsonify({"ok": True, "id": d.get("id") if isinstance(d, dict) else None})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/catalogo")
@login_required
def api_catalogo():
    def build():
        prods = gcapi.get_all("/produtos")
        produtos = [{
            "id": str(p.get("id")), "nome": p.get("nome"),
            "estoque": _num(p.get("estoque")), "custo": _num(p.get("valor_custo")),
        } for p in prods if str(p.get("ativo")) == "1"]
        produtos.sort(key=lambda x: (x["nome"] or "").upper())
        forns = gcapi.get_all("/fornecedores")
        fornecedores = [{"id": str(f.get("id")), "nome": f.get("nome") or f.get("razao_social")}
                        for f in forns if str(f.get("ativo")) in ("1", "true", "")]
        fornecedores.sort(key=lambda x: (x["nome"] or "").upper())
        return {"produtos": produtos, "fornecedores": fornecedores}
    return jsonify(cached("catalogo", 120, build))

def _proximo_codigo_compra():
    """Código na faixa manual (< 1.000.000), separada dos automáticos (~13 mi)."""
    try:
        comps = gcapi.get_all("/compras")
        manual = [int(str(c.get("codigo"))) for c in comps
                  if str(c.get("codigo")).isdigit() and int(str(c.get("codigo"))) < 1000000]
        return str((max(manual) + 1) if manual else 810807)
    except Exception:
        return str(810807)

@app.route("/api/compra", methods=["POST"])
@login_required
def api_compra():
    """Registra uma compra sem nota: entra estoque + atualiza custo + paga do pote."""
    body = request.get_json(force=True, silent=True) or {}
    forn = str(body.get("fornecedor_id") or "").strip()
    data = (body.get("data") or _hoje())[:10]
    forma = body.get("forma") or "Caixa"
    itens = body.get("itens") or []
    if not forn:
        return jsonify({"ok": False, "erro": "escolha o fornecedor"}), 400
    if not itens:
        return jsonify({"ok": False, "erro": "adicione ao menos um item"}), 400
    pot = POTES.get(forma, POTES["Caixa"])
    produtos, total = [], 0.0
    for it in itens:
        pid = str(it.get("produto_id") or "").strip()
        qtd = _num(it.get("quantidade"))
        mult = _num(it.get("mult")) or 1
        valor = _num(it.get("valor"))
        if not pid or qtd <= 0 or valor <= 0:
            return jsonify({"ok": False, "erro": "item incompleto"}), 400
        units = qtd * mult
        custo_unit = valor / units if units else 0
        total += valor
        produtos.append({"produto": {
            "produto_id": pid, "quantidade": units,
            "valor_custo": round(custo_unit, 4), "valor_total": round(valor, 2),
            "detalhes": "compra sem nota (painel)",
        }})
    pagamentos = [{"pagamento": {
        "data_vencimento": data, "valor": round(total, 2),
        "forma_pagamento_id": pot["forma"], "plano_contas_id": "33015669",
        "conta_bancaria_id": pot["conta"], "liquidado": "1", "data_liquidacao": data,
    }}]
    payload = {
        "codigo": _proximo_codigo_compra(), "fornecedor_id": forn,
        "data_emissao": data, "situacao_id": "1979927", "condicao_pagamento": "a_vista",
        "valor_produtos": round(total, 2), "valor_total": round(total, 2),
        "produtos": produtos, "pagamentos": pagamentos,
    }
    try:
        r = gcapi.post("/compras", payload)
        if r.get("status") != "success":
            return jsonify({"ok": False, "erro": str(r.get("data") or r)[:200]}), 502
        d = r.get("data") or {}
        _invalida("resumo", "pagar", "catalogo")
        return jsonify({"ok": True, "id": d.get("id"), "codigo": d.get("codigo"),
                        "total": round(total, 2), "itens": len(produtos)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/produto")
@login_required
def api_produto():
    """Estoque/custo atuais de um produto (ao vivo, sem cache) — pra tela de inventário."""
    pid = str(request.args.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False}), 400
    p = gcapi.get(f"/produtos/{pid}").get("data")
    if isinstance(p, list):
        p = p[0] if p else {}
    p = p.get("Produto", p) if isinstance(p, dict) else {}
    return jsonify({"ok": True, "nome": p.get("nome"),
                    "estoque": _num(p.get("estoque")), "custo": _num(p.get("valor_custo"))})

@app.route("/api/inventario", methods=["POST"])
@login_required
def api_inventario():
    """Acerta o estoque de um produto pra o valor contado (inventário)."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("produto_id") or "").strip()
    if not pid or body.get("contagem") in (None, ""):
        return jsonify({"ok": False, "erro": "produto e contagem são obrigatórios"}), 400
    contagem = int(round(_num(body.get("contagem"))))
    cur = gcapi.get(f"/produtos/{pid}").get("data")
    if isinstance(cur, list):
        cur = cur[0] if cur else {}
    cur = cur.get("Produto", cur) if isinstance(cur, dict) else {}
    antes = _num(cur.get("estoque"))
    payload = {  # PUT parcial: mexe só no estoque, preserva custo/preço/fiscal
        "nome": cur.get("nome"),
        "codigo_interno": cur.get("codigo_interno"),
        "estoque": str(contagem),
    }
    try:
        gcapi.put(f"/produtos/{pid}", payload)
        _invalida("resumo", "catalogo")
        return jsonify({"ok": True, "antes": antes, "depois": contagem,
                        "dif": contagem - antes, "nome": cur.get("nome")})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
