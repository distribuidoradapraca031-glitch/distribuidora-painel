#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App web da Distribuidora da Praça — painel ao vivo + gravação direta no GestãoClick,
protegido por senha. (Fase 1: login + leitura ao vivo.)"""
import os, functools, time, re, calendar
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, abort
import gclient as gcapi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _carrega_painel_data():
    """JSON dos gráficos (snapshot) que vai embutido no painel. `<` vira \\u003c
    pra não quebrar o <script> onde ele é injetado."""
    try:
        with open(os.path.join(BASE_DIR, "painel_data.json"), encoding="utf-8") as f:
            return f.read().replace("<", "\\u003c")
    except FileNotFoundError:
        return "{}"

PAINEL_DATA = _carrega_painel_data()

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
    with open(os.path.join(BASE_DIR, "templates", "painel.html"), encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("__DATA__", PAINEL_DATA)

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
        # aqui embaixo só mercadoria (nota de compra/boleto DDA). Tudo que é previsão,
        # provisão ou recorrente vai pro bloco de cima, então sai daqui.
        abertos = [p for p in pgs if str(p.get("liquidado")) != "1"
                   and not _categoria_conta(p)]
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
    "Retirada do sócio (Victor)": "33015638", "Retirada do sócio (Igor)": "33015638",
    "Pagamento Biel": "33015664", "Pagamento PH Motoca": "33015664",
    "Outros": "33015669",
}
# categorias que dividem o mesmo plano (Victor/Igor, Biel/PH) — separadas pela descrição no resumo
SPLIT_LABELS = ["Retirada do sócio (Victor)", "Retirada do sócio (Igor)",
                "Pagamento Biel", "Pagamento PH Motoca"]
BOLETO_FORMA_ID = "6687681"  # forma "Boleto" (em aberto) no GestãoClick

# ---- PREVISÕES (topo do contas a pagar): lista fixa de categorias recorrentes ----
# cada item que o dono adiciona vira um pagamento em aberto com descrição
# "[PREV] <categoria> — <obs>". Começam zeradas; somam conforme o dono insere.
PREV_TAG = "[PREV]"
CATEGORIAS_PREV = [
    ("Aluguel (IPTU)", "33015630"),
    ("Energia (CEMIG)", "33015649"),
    ("Água (COPASA)", "33015649"),
    ("Internet / telefone", "33015663"),
    ("Contabilidade (Werdeiros)", "33015635"),
    ("Pró-labore Igor", "33015660"),
    ("Retirada Victor", "33015638"),
    ("Motoboy / entrega", "33015664"),
    ("Funcionário Gabriel (FDS)", "33015660"),
    ("Anota AI", "33015654"),
    ("DAS (Simples)", "33015672"),
    ("INSS s/ pró-labore", "33015646"),
    ("Vigia", "33015661"),
    ("Seguro do carro", "33015633"),
    ("Sacolas / gelo / copos", "33015662"),
    ("Biel", "33015664"),
    ("PH Motoca", "33015664"),
]
_PREV_PLANO = dict(CATEGORIAS_PREV)

def _prev_categoria(desc):
    """Categoria de um item de previsão, ou None se não for [PREV]."""
    d = (desc or "")
    if not d.startswith(PREV_TAG):
        return None
    rest = d[len(PREV_TAG):].strip()
    for label, _ in CATEGORIAS_PREV:
        if rest.startswith(label):
            return label
    return None

def _prev_nota(desc):
    """A observação depois do rótulo da categoria (ou vazio)."""
    d = (desc or "")
    if d.startswith(PREV_TAG):
        d = d[len(PREV_TAG):].strip()
    for label, _ in CATEGORIAS_PREV:
        if d.startswith(label):
            return d[len(label):].strip().lstrip("—-").strip()
    return d

# reconhece a categoria de contas antigas (provisões [provisao] e recorrentes) pela
# descrição — INSS antes de PRO-LABORE, PH MOTOCA antes de MOTOBOY, etc.
PREV_KEYWORDS = [
    ("ALUGUEL", "Aluguel (IPTU)"),
    ("CEMIG", "Energia (CEMIG)"), ("ENERGIA", "Energia (CEMIG)"),
    ("COPASA", "Água (COPASA)"), ("AGUA", "Água (COPASA)"), ("ÁGUA", "Água (COPASA)"),
    ("INTERNET", "Internet / telefone"), ("TELEFON", "Internet / telefone"),
    ("WERDEI", "Contabilidade (Werdeiros)"), ("CONTAB", "Contabilidade (Werdeiros)"),
    ("HONORARIOS CONTAB", "Contabilidade (Werdeiros)"),
    ("INSS", "INSS s/ pró-labore"),
    ("PRO-LABORE", "Pró-labore Igor"), ("PRÓ-LABORE", "Pró-labore Igor"),
    ("RETIRADA", "Retirada Victor"),
    ("PH MOTOCA", "PH Motoca"), ("BIEL", "Biel"),
    ("MOTOBOY", "Motoboy / entrega"),
    ("GABRIEL", "Funcionário Gabriel (FDS)"),
    ("ANOTA", "Anota AI"),
    ("SIMPLES", "DAS (Simples)"), ("DAS ", "DAS (Simples)"),
    ("VIGIA", "Vigia"),
    ("SEGURO", "Seguro do carro"),
    ("SACOLAS", "Sacolas / gelo / copos"), ("GELO", "Sacolas / gelo / copos"),
]

def _categoria_conta(p):
    """Categoria (das 17) de uma conta a pagar, ou None se não for previsão/recorrente."""
    desc = p.get("descricao") or ""
    c = _prev_categoria(desc)      # itens [PREV] que o dono adiciona
    if c:
        return c
    du = desc.upper()
    for kw, label in PREV_KEYWORDS:  # provisões antigas e recorrentes
        if kw in du:
            return label
    return None

def _nota_conta(p):
    """Texto limpo pra mostrar de uma conta (tira as tags [PREV]/[provisao])."""
    desc = (p.get("descricao") or "")
    if desc.startswith(PREV_TAG):
        return _prev_nota(desc)
    return re.sub(r"\[provisao\]|\[PREV\]", "", desc, flags=re.I).strip()

def _ultimo_dia_mes(d):
    """'AAAA-MM-DD' do último dia do mês da data d ('AAAA-MM-...')."""
    y, m = int(d[:4]), int(d[5:7])
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"

# ---- SAQUE (troca cartão -> dinheiro, vira venda + sangria) ----
SAQUE_FEE = {"credito": 0.0314, "debito": 0.0085, "pix": 0.0}   # taxa da maquininha (PIX = sem taxa)
SAQUE_FORMA = {"credito": "6055920", "debito": "6055921", "pix": "6055931"}
SAQUE_CLIENTE = "55346041"          # cliente DELIVERY (consumidor)
SAQUE_SITUACAO = "8468151"          # Concretizada
SAQUE_SANGRIA_PLANO = "33015669"    # provisório (Outros) — dono cria plano "Saque" depois
_saque_ids = {}                     # "SAQUE 50" -> produto_id (cache)

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
    valor_real = _num(body.get("valor"))  # valor de fato pago (pode ter juros / ser variável)
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
        "valor": f"{valor_real:.2f}" if valor_real > 0 else (p.get("valor") or p.get("valor_total") or "0"),
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
        _invalida("pagar", "resumo", "previsoes", "gastos_mes")
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
    plano = CATS.get(cat, CATS["Outros"])
    if forma == "Boleto":
        # não pago: vira conta a pagar (boleto em aberto)
        payload = {
            "descricao": desc,
            "valor": f"{valor:.2f}",
            "data_vencimento": data,
            "data_competencia": data,
            "liquidado": "0",
            "plano_contas_id": plano,
            "forma_pagamento_id": BOLETO_FORMA_ID,
        }
    else:
        pot = POTES.get(forma, POTES["Caixa"])
        payload = {
            "descricao": desc,
            "valor": f"{valor:.2f}",
            "data_vencimento": data,
            "data_competencia": data,
            "data_liquidacao": data,
            "liquidado": "1",
            "plano_contas_id": plano,
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

@app.route("/api/hoje")
@login_required
def api_hoje():
    """Vendas do balcão de hoje, ao vivo, com quebra por forma de pagamento."""
    def build():
        h = _hoje()
        vendas = gcapi.get_all("/vendas", {"tipo": "vendas_balcao", "data_inicio": h, "data_fim": h})
        tot = dinheiro = pix = cartao = outros = 0.0
        for v in vendas:
            tot += _num(v.get("valor_total"))
            for wrap in v.get("pagamentos") or []:
                p = wrap.get("pagamento", wrap)
                val = _num(p.get("valor")); nome = (p.get("nome_forma_pagamento") or "").upper()
                if "DINHEIRO" in nome: dinheiro += val
                elif "PIX" in nome: pix += val
                elif "CART" in nome: cartao += val
                else: outros += val
        return {"n": len(vendas), "total": round(tot, 2), "dinheiro": round(dinheiro, 2),
                "pix": round(pix, 2), "cartao": round(cartao, 2), "outros": round(outros, 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("hoje", 60, build))

@app.route("/api/hoje-delivery")
@login_required
def api_hoje_delivery():
    """Vendas de DELIVERY de hoje (Anota AI), ao vivo. Delivery entra como venda
    tipo 'produto' com cliente DELIVERY e observação 'Anota AI' — separa do saque."""
    def build():
        h = _hoje()
        vendas = gcapi.get_all("/vendas", {"tipo": "produto", "data_inicio": h, "data_fim": h})
        tot = 0.0
        n = 0
        for v in vendas:
            if "ANOTA AI" not in (v.get("observacoes") or "").upper():
                continue  # exclui saque e outras vendas produto
            n += 1
            tot += _num(v.get("valor_total"))
        return {"n": n, "total": round(tot, 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("hoje_deliv", 60, build))

@app.route("/api/delivery")
@login_required
def api_delivery():
    """Delivery do MÊS ATUAL, ao vivo do sistema (vendas do Anota trazidas pelo sync).
    Não congela mais — sempre reflete o mês corrente."""
    def build():
        mes = _hoje()[:7]
        vendas = gcapi.get_all("/vendas", {"tipo": "produto",
                                           "data_inicio": mes + "-01",
                                           "data_fim": _ultimo_dia_mes(_hoje())})
        deliv = [v for v in vendas if "ANOTA AI" in (v.get("observacoes") or "").upper()]
        por_dia, formas = {}, {}
        produtos = frete = 0.0
        for v in deliv:
            d = (v.get("data") or "")[:10]
            tot = _num(v.get("valor_total")); fr = _num(v.get("valor_frete"))
            frete += fr; produtos += (tot - fr)
            pd = por_dia.setdefault(d, {"data": d, "fat": 0.0, "n": 0})
            pd["fat"] += tot; pd["n"] += 1
            for w in v.get("pagamentos") or []:
                p = w.get("pagamento", w)
                nm = p.get("nome_forma_pagamento") or "Outros"
                fo = formas.setdefault(nm, {"forma": nm, "n": 0, "valor": 0.0})
                fo["n"] += 1; fo["valor"] += _num(p.get("valor"))
        n = len(deliv); dias = len(por_dia); fat = round(produtos + frete, 2)
        serie = [{"data": k, "fat": round(por_dia[k]["fat"], 2), "n": por_dia[k]["n"]}
                 for k in sorted(por_dia)]
        pag = sorted(({"forma": f["forma"], "n": f["n"], "valor": round(f["valor"], 2)}
                      for f in formas.values()), key=lambda x: -x["valor"])
        return {"mes": mes, "n": n, "fat": fat, "produtos": round(produtos, 2),
                "frete": round(frete, 2), "ticket": round(fat / n, 2) if n else 0.0,
                "dias": dias, "media_dia": round(fat / dias, 2) if dias else 0.0,
                "ped_dia": round(n / dias, 1) if dias else 0.0,
                "por_dia": serie, "pagamento": pag,
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("delivery", 120, build))

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
    if forma == "Boleto":
        # a prazo: fica em contas a pagar, não sai do caixa agora
        pagamentos = [{"pagamento": {
            "data_vencimento": data, "valor": round(total, 2),
            "forma_pagamento_id": BOLETO_FORMA_ID, "plano_contas_id": "33015669",
            "liquidado": "0",
        }}]
        condicao = "a_prazo"
    else:
        pot = POTES.get(forma, POTES["Caixa"])
        pagamentos = [{"pagamento": {
            "data_vencimento": data, "valor": round(total, 2),
            "forma_pagamento_id": pot["forma"], "plano_contas_id": "33015669",
            "conta_bancaria_id": pot["conta"], "liquidado": "1", "data_liquidacao": data,
        }}]
        condicao = "a_vista"
    payload = {
        "codigo": _proximo_codigo_compra(), "fornecedor_id": forn,
        "data_emissao": data, "situacao_id": "1979927", "condicao_pagamento": condicao,
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

AJUSTE_CAIXA_PLANO = "33015682"  # plano "Ajuste de caixa" no GestãoClick

def _fech_calc(data):
    """Dinheiro que ENTROU (vendas em dinheiro) e SAÍDAS em dinheiro do caixa no dia."""
    vendas = gcapi.get_all("/vendas", {"tipo": "vendas_balcao", "data_inicio": data, "data_fim": data})
    din = 0.0
    for v in vendas:
        for w in v.get("pagamentos") or []:
            p = w.get("pagamento", w)
            if "DINHEIRO" in (p.get("nome_forma_pagamento") or "").upper():
                din += _num(p.get("valor"))
    saidas = 0.0
    for p in gcapi.get_all("/pagamentos"):
        if str(p.get("liquidado")) != "1":
            continue
        if (p.get("data_liquidacao") or "")[:10] != data:
            continue
        if str(p.get("conta_bancaria_id")) == "696747" or str(p.get("forma_pagamento_id")) == "6055919":
            saidas += _num(p.get("valor_total")) or _num(p.get("valor"))
    return round(din, 2), round(saidas, 2)

@app.route("/api/fechamento-hoje")
@login_required
def api_fechamento_hoje():
    """Quanto DEVERIA ter na gaveta hoje = troco + dinheiro que entrou − saídas em dinheiro."""
    def build():
        troco = _num(request.args.get("troco")) or 200.0
        din, saidas = _fech_calc(_hoje())
        return {"troco": round(troco, 2), "dinheiro": din, "saidas": saidas,
                "esperado": round(troco + din - saidas, 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("fech_hoje", 45, build))

@app.route("/api/fechamento", methods=["POST"])
@login_required
def api_fechamento():
    """Fecha o caixa: compara o contado com o esperado e lança o Ajuste de caixa no
    sistema (faltou = saída; sobrou = entrada) pra o caixa BATER com a gaveta."""
    body = request.get_json(force=True, silent=True) or {}
    data = (body.get("data") or _hoje())[:10]
    troco = _num(body.get("troco")) or 200.0
    contado = _num(body.get("contado"))
    if body.get("contado") in (None, ""):
        return jsonify({"ok": False, "erro": "conte a gaveta primeiro"}), 400
    din, saidas = _fech_calc(data)
    esperado = round(troco + din - saidas, 2)
    quebra = round(contado - esperado, 2)
    ajuste_id = None
    try:
        if abs(quebra) >= 0.01:
            desc = (f"FECHAMENTO {data} — contado R$ {contado:.2f} · esperado R$ {esperado:.2f} · "
                    f"{'sobra' if quebra > 0 else 'falta'} R$ {abs(quebra):.2f}")
            mov = {"descricao": desc, "valor": f"{abs(quebra):.2f}",
                   "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
                   "liquidado": "1", "plano_contas_id": AJUSTE_CAIXA_PLANO,
                   "conta_bancaria_id": "696747", "forma_pagamento_id": "6055919"}
            # sobra = entra dinheiro (recebimento) ; falta = sai dinheiro (pagamento)
            r = gcapi.post("/recebimentos" if quebra > 0 else "/pagamentos", mov)
            ajuste_id = (r.get("data") or {}).get("id") if isinstance(r.get("data"), dict) else None
        _invalida("resumo", "hoje", "fech_hoje")
        return jsonify({"ok": True, "esperado": esperado, "contado": round(contado, 2),
                        "quebra": quebra, "dinheiro": din, "saidas": saidas,
                        "ajuste_id": ajuste_id})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/gastos-mes")
@login_required
def api_gastos_mes():
    """Pagos do mês (bloco do meio): só o que JÁ FOI PAGO no mês, por categoria,
    fora mercadoria (compras). Inclui provisões pagas, retirada, lanche, motoca..."""
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        mes = _hoje()[:7]
        cats, total = {}, 0.0
        for p in pgs:
            if str(p.get("liquidado")) != "1":
                continue  # só o que já saiu da conta (pago)
            if (p.get("data_liquidacao") or "")[:7] != mes:
                continue  # pago dentro deste mês
            desc = (p.get("descricao") or "").strip()
            if desc.upper().startswith("SANGRIA SAQUE"):
                continue  # saque não é gasto, é troca de dinheiro
            plano = p.get("nome_plano_conta") or "Outros"
            if plano in ("Compras", "Ajuste de caixa"):
                continue  # mercadoria e acerto de gaveta ficam fora dos gastos
            cat = (_categoria_conta(p)
                   or next((lbl for lbl in SPLIT_LABELS if desc.startswith(lbl)), None)
                   or plano)
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            cats[cat] = cats.get(cat, 0.0) + val
            total += val
        itens = sorted([{"cat": k, "total": round(v, 2)} for k, v in cats.items()],
                       key=lambda x: -x["total"])
        return {"itens": itens, "total": round(total, 2), "mes": mes,
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("gastos_mes", 60, build))

def _saque_pid(valor):
    """Acha o produto SAQUE {valor}; se não existir (valor novo), cria."""
    nome = f"SAQUE {valor}"
    if not _saque_ids:
        for p in gcapi.get_all("/produtos"):
            nm = (p.get("nome") or "").upper()
            if nm.startswith("SAQUE "):
                _saque_ids[nm] = str(p.get("id"))
    pid = _saque_ids.get(nome.upper())
    if pid:
        return pid
    r = gcapi.post("/produtos", {"nome": nome, "movimenta_estoque": "0", "ativo": "1",
                                 "valor_custo": "0.00", "valor_venda": f"{valor*1.2:.2f}"})
    pid = str((r.get("data") or {}).get("id"))
    _saque_ids[nome.upper()] = pid
    return pid

@app.route("/api/saque", methods=["POST"])
@login_required
def api_saque():
    """Saque: cliente troca cartão por dinheiro. Grava a VENDA (cartão, +20%) e a
    SANGRIA (dinheiro que sai do caixa) — assim bate no fechamento."""
    body = request.get_json(force=True, silent=True) or {}
    valor = int(round(_num(body.get("valor"))))
    tipo = (body.get("tipo") or "debito").lower()
    if valor <= 0:
        return jsonify({"ok": False, "erro": "valor inválido"}), 400
    if tipo not in SAQUE_FORMA:
        tipo = "debito"
    total = round(valor * 1.2, 2)                 # cliente paga no cartão (+20%)
    taxa = round(total * SAQUE_FEE[tipo], 2)      # taxa da maquininha
    ganho = round(valor * 0.2 - taxa, 2)          # nosso lucro líquido
    data = _hoje()
    try:
        pid = _saque_pid(valor)
        venda = {
            "tipo": "produto", "cliente_id": SAQUE_CLIENTE, "data": data,
            "situacao_id": SAQUE_SITUACAO, "condicao_pagamento": "a_vista",
            "observacoes": f"Saque R$ {valor} — cliente pagou R$ {total:.2f} no {tipo}",
            "produtos": [{"produto": {"produto_id": pid, "quantidade": 1,
                                      "valor_venda": total, "detalhes": "Saque"}}],
            "pagamentos": [{"pagamento": {"data_vencimento": data, "valor": total,
                                          "forma_pagamento_id": SAQUE_FORMA[tipo],
                                          "observacao": "Saque"}}],
        }
        rv = gcapi.post("/vendas", venda)
        if rv.get("status") != "success":
            return jsonify({"ok": False, "erro": str(rv.get("data") or rv)[:200]}), 502
        vid = (rv.get("data") or {}).get("id")
        sangria = {
            "descricao": f"SANGRIA SAQUE R$ {valor} · lucro R$ {ganho:.2f} — dinheiro entregue ao cliente",
            "valor": f"{valor:.2f}", "data_vencimento": data, "data_competencia": data,
            "data_liquidacao": data, "liquidado": "1", "plano_contas_id": SAQUE_SANGRIA_PLANO,
            "conta_bancaria_id": "696747", "forma_pagamento_id": "6055919",
        }
        gcapi.post("/pagamentos", sangria)
        _invalida("resumo", "hoje", "pagar", "saque_resumo")
        return jsonify({"ok": True, "venda_id": vid, "total": total,
                        "sangria": valor, "taxa": taxa, "ganho": ganho})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/saque-resumo")
@login_required
def api_saque_resumo():
    """Total sacado e lucro do saque acumulados (desde que começou a registrar no
    painel) — lê as sangrias de saque gravadas no sistema."""
    def build():
        pgs = gcapi.get_all("/pagamentos")
        principal = lucro = 0.0
        n = 0
        for p in pgs:
            desc = p.get("descricao") or ""
            if not desc.upper().startswith("SANGRIA SAQUE"):
                continue
            n += 1
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            principal += val
            m = re.search(r"lucro R\$ ?([\d.,]+)", desc)
            lucro += _num(m.group(1)) if m else round(val * 0.19, 2)  # fallback p/ saques antigos
        return {"n": n, "valor": round(principal, 2), "lucro": round(lucro, 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("saque_resumo", 60, build))

@app.route("/api/previsoes")
@login_required
def api_previsoes():
    """Previsões por categoria (topo do contas a pagar): total em aberto de cada
    categoria + itens em aberto + o que já foi pago no mês (pra fechar dia 10)."""
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        hoje = _hoje()
        fim_mes = _ultimo_dia_mes(hoje)
        abertos = {lbl: [] for lbl, _ in CATEGORIAS_PREV}
        for p in pgs:
            if str(p.get("liquidado")) == "1":
                continue  # pago já saiu do topo (vai pros pagos do mês)
            cat = _categoria_conta(p)
            if not cat:
                continue
            venc = (p.get("data_vencimento") or "")[:10]
            if venc and venc > fim_mes:
                continue  # meses futuros não entram (só o do mês + atrasados)
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            abertos[cat].append({"id": p.get("id"), "nota": _nota_conta(p),
                                 "venc": venc, "valor": val,
                                 "atrasado": bool(venc and venc < hoje)})
        categorias = []
        for lbl, _ in CATEGORIAS_PREV:
            its = sorted(abertos[lbl], key=lambda x: x["venc"] or "9999")
            categorias.append({"cat": lbl, "n": len(its),
                               "aberto": round(sum(i["valor"] for i in its), 2),
                               "atrasado": any(i["atrasado"] for i in its),
                               "itens": its})
        atrasado = round(sum(i["valor"] for c in categorias for i in c["itens"]
                             if i["atrasado"]), 2)
        return {"categorias": categorias,
                "total_aberto": round(sum(c["aberto"] for c in categorias), 2),
                "total_atrasado": atrasado,
                "mes": hoje[:7], "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("previsoes", 60, build))

@app.route("/api/previsao", methods=["POST"])
@login_required
def api_previsao():
    """Adiciona uma previsão em aberto numa categoria (vira conta a pagar [PREV])."""
    body = request.get_json(force=True, silent=True) or {}
    cat = (body.get("categoria") or "").strip()
    valor = _num(body.get("valor"))
    venc = (body.get("vencimento") or _hoje())[:10]
    nota = (body.get("descricao") or "").strip()
    plano = _PREV_PLANO.get(cat)
    if not plano:
        return jsonify({"ok": False, "erro": "categoria inválida"}), 400
    if valor <= 0:
        return jsonify({"ok": False, "erro": "valor inválido"}), 400
    desc = f"{PREV_TAG} {cat}" + (f" — {nota}" if nota else "")
    payload = {"descricao": desc, "valor": f"{valor:.2f}",
               "data_vencimento": venc, "data_competencia": venc,
               "liquidado": "0", "plano_contas_id": plano,
               "forma_pagamento_id": BOLETO_FORMA_ID}
    try:
        r = gcapi.post("/pagamentos", payload)
        d = r.get("data") or {}
        _invalida("previsoes", "pagar")
        return jsonify({"ok": True, "id": d.get("id") if isinstance(d, dict) else None})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/excluir-conta", methods=["POST"])
@login_required
def api_excluir_conta():
    """Apaga uma conta a pagar (boleto de golpe ou previsão lançada errada)."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "erro": "sem id"}), 400
    try:
        gcapi.delete(f"/pagamentos/{pid}")
        _invalida("pagar", "previsoes", "resumo", "gastos_mes")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
