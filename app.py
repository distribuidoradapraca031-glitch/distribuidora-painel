#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App web da Distribuidora da Praça — painel ao vivo + gravação direta no GestãoClick,
protegido por senha. (Fase 1: login + leitura ao vivo.)"""
import os, functools, time, re, calendar, datetime, threading
from collections import defaultdict
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, abort
import gclient as gcapi

# O servidor da nuvem roda em UTC: depois das 21h daqui já era "amanhã" pra ele, e o
# fechamento da noite caía no dia seguinte. Tudo no painel usa a hora de Minas.
os.environ.setdefault("TZ", "America/Sao_Paulo")
try:
    time.tzset()
except AttributeError:      # Windows não tem tzset
    pass

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
                   and not _categoria_conta(p)
                   and not _eh_interno(p.get("descricao"))]
        itens = [{
            "id": p.get("id"),
            "desc": _nota_conta(p) or p.get("nome_plano_conta") or "—",
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

@app.route("/api/ultima-atualizacao")
@login_required
def api_ultima_atualizacao():
    """Quando as contas a pagar foram atualizadas pela última vez.

    Serve pro dono saber a partir de qual data mandar o próximo lote (guias da
    contabilidade, DDA, boleto). Sai do próprio GestãoClick (campo cadastrado_em),
    então não tem o que desencontrar. Fechamento de caixa/sangria ficam de fora —
    entram todo dia sozinhos e não são "conta que o dono mandou".
    """
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        def _quando(p):
            return (p.get("cadastrado_em") or "")[:16]
        def _vale(p):
            d = (p.get("descricao") or "").upper()
            plano = (p.get("nome_plano_conta") or "").upper()
            if "FECHAMENTO DE CAIXA" in d or "SANGRIA" in d or "AJUSTE DE CAIXA" in plano:
                return False
            return bool(_quando(p))
        validos = [p for p in pgs if _vale(p)]
        def _ult(sub):
            if not sub:
                return {"quando": "", "n": 0}
            q = max(_quando(p) for p in sub)
            return {"quando": q, "n": sum(1 for p in sub if _quando(p)[:10] == q[:10])}
        contas = [p for p in validos if _categoria_conta(p)]           # guias/fixos
        notas = [p for p in validos if not _categoria_conta(p)]        # nota de compra/DDA
        return {"geral": _ult(validos), "contas": _ult(contas), "notas": _ult(notas),
                "hoje": _hoje()}
    return jsonify(cached("ultima_atualizacao", 120, build))

# ---- mapeamentos: potes (conta+forma) e categorias de gasto ----
RESERVA_CONTA = os.environ.get("RESERVA_CONTA_ID", "696747")  # trocar p/ conta RESERVA quando existir
GAVETA_CONTA = "696747"      # conta CAIXA = a gaveta da loja (é ela que o fechamento confere)
RESERVA_CONTA_ID = "681760"  # onde fica a reserva/banco — FORA da gaveta
RP_FORMA_ID = "6776761"      # forma "Recurso próprio Victor" no GestãoClick
# "Dinheiro guardado (reserva/sobra)": forma amarrada à CONTA BANCÁRIA. Antes a reserva e a
# sobra saíam como "Dinheiro à Vista", que no cadastro do GestãoClick aponta pro CAIXA — o
# painel ficava certo (a conta do lançamento era a bancária), mas o GC mostrava o valor
# abatendo do caixa da loja. Com forma própria, os dois ficam iguais.
GUARDADO_FORMA_ID = "6780142"
POTES = {
    "Caixa":    {"conta": GAVETA_CONTA,     "forma": "6055919"},  # gaveta + Dinheiro
    "Dinheiro": {"conta": RESERVA_CONTA_ID, "forma": GUARDADO_FORMA_ID},  # RESERVA (fora da gaveta)
    "Sobra":    {"conta": RESERVA_CONTA_ID, "forma": GUARDADO_FORMA_ID},  # SOBRA DE CAIXA (fora da gaveta)
    "PIX":      {"conta": RESERVA_CONTA_ID, "forma": "6055931"},  # conta bancária + PIX
    # dinheiro do bolso do Victor: paga a conta (some do "a pagar") sem tirar nada da loja
    # — nem gaveta, nem reserva, nem sobra. Vai pro quadro "Recurso próprio" pela marca [RP].
    "Victor":   {"conta": RESERVA_CONTA_ID, "forma": RP_FORMA_ID},
}

def _saiu_da_gaveta(p):
    """Esse pagamento tirou dinheiro da GAVETA do dia?

    Só conta o que saiu EM DINHEIRO. Pagamento em PIX/cartão sai do banco, não da
    gaveta — e isso importa porque o GestãoClick carimba conta "CAIXA" em todo
    pagamento de compra, mesmo quando foi PIX (era o que derrubava o esperado do
    fechamento pra negativo). Dinheiro da RESERVA também não está na gaveta.
    """
    if str(p.get("forma_pagamento_id")) != "6055919":     # não é dinheiro
        return False
    # só a conta CAIXA é a gaveta. Qualquer outra (banco, reserva, recurso próprio) está
    # fora do fechamento — antes isso era testado só contra a conta da reserva, e uma
    # conta nova entraria na gaveta por engano.
    return str(p.get("conta_bancaria_id") or "") == GAVETA_CONTA
CATS = {
    "Lanche": "33015662", "Almoço": "33015662", "Padaria": "33015662",
    "Papelaria": "33015658", "Combustível": "33015633", "Motoboy / entrega": "33015664",
    "Limpeza / higiene": "33015655", "Descartáveis (copo/saco)": "33015669",
    "Manutenção / conserto": "33015656", "Água / luz / internet": "33015649",
    # reparo da loja (freezer, porta, estrutura) — o dono lança tudo isso numa linha só
    "Reparo da loja": "33015656",
    "Retirada do sócio (Victor)": "33015638",
    # cada pessoa tem UMA categoria só (o dono paga a mesma pessoa com nomes diferentes)
    "Igor (pró-labore / retirada)": "33015660",
    "Biel (Gabriel)": "33015660",
    "PH Motoca": "33015664",
    "Seguro do carro": "33015633",
    "Sacolas / gelo / copos": "33015662",
    "Outros": "33015669",
    # nomes antigos (lançamentos já feitos) — continuam valendo se aparecerem
    "Retirada do sócio (Igor)": "33015660", "Pró-labore Igor": "33015660",
    "Pagamento Biel": "33015660", "Funcionário Gabriel (FDS)": "33015660",
    "Pagamento PH Motoca": "33015664", "PH Motoca / motoboy": "33015664",
}
# categorias que dividem o mesmo plano (Victor/Igor) — separadas pela descrição no resumo
SPLIT_LABELS = ["Retirada do sócio (Victor)", "Igor (pró-labore / retirada)"]
BOLETO_FORMA_ID = "6687681"  # forma "Boleto" (em aberto) no GestãoClick

# ---- RECURSO PRÓPRIO (dinheiro do Victor) ----
# pagamento que o dono fez com o próprio dinheiro. Fica SÓ no painel, separado:
# guardado como lançamento ABERTO (liquidado=0) com a tag [RP] — assim NÃO mexe na
# gaveta, no banco, no fechamento nem no "Total gasto no mês" da loja. Some de todos
# os blocos normais (é filtrado) e aparece só no quadro "Recurso próprio".
RP_TAG = "[RP]"

# ---- DINHEIRO RESERVA (stash das sobras de caixa) ----
# saldo = depósitos [RES+] − saídas [RES-]. Registros liquidado=0 (não mexem em conta
# nem no fechamento); só o painel lê. Pagar com fonte "Dinheiro" (reserva) já sai FORA
# da gaveta (conta banco) e cria um [RES-] pra descontar do saldo.
RES_DEP_TAG = "[RES+]"
RES_OUT_TAG = "[RES-]"

# ---- SOBRA DE CAIXA (o outro bolo de dinheiro, separado da reserva) ----
# O dono guarda DOIS montes de dinheiro fora da gaveta: a RESERVA (cofre, notas altas,
# quitação de dívida) e a SOBRA DE CAIXA, que ele usa no dia a dia pra pagar conta
# menor. Mesma mecânica da reserva: saldo = [SOB+] − [SOB-], registros liquidado=0 que
# só o painel lê. Pagar com a fonte "Sobra" sai FORA da gaveta (não mexe no fechamento).
SOB_DEP_TAG = "[SOB+]"
SOB_OUT_TAG = "[SOB-]"

def _sem_tag(desc, tag):
    d = desc or ""
    if d.startswith(tag):
        d = d[len(tag):].strip().lstrip("—-").strip()
    return d

def _rp_nota(desc):
    return _sem_tag(desc, RP_TAG)

def _marca_rp(desc, forma):
    """Conta paga com o dinheiro do Victor: a marca [RP] é o que joga o lançamento no
    quadro 'Recurso próprio' e tira ele do dinheiro da loja."""
    d = (desc or "Conta").strip()
    if forma != "Victor" or d.startswith(RP_TAG):
        return d
    return f"{RP_TAG} {d}"

def _eh_interno(desc):
    """Registro de controle do painel (recurso próprio / reserva) — some dos blocos normais."""
    d = (desc or "")
    return (d.startswith(RP_TAG) or d.startswith(RES_DEP_TAG) or d.startswith(RES_OUT_TAG)
            or d.startswith(SOB_DEP_TAG) or d.startswith(SOB_OUT_TAG))

def _sangria_caixa(valor, motivo, data):
    """Tira da GAVETA o dinheiro que o dono guardou no cofre/sobra no mesmo dia.

    Sem isso o fechamento continuava esperando esse dinheiro na gaveta e dava falta:
    o depósito da reserva é só um registro do painel (liquidado=0) e não mexe em conta.
    Descrição começa com SANGRIA — é assim que o resumo do mês sabe que não é gasto.
    """
    gcapi.post("/pagamentos", {
        "descricao": f"SANGRIA {motivo}"[:180], "valor": f"{_num(valor):.2f}",
        "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
        "liquidado": "1", "plano_contas_id": SAQUE_SANGRIA_PLANO,
        "conta_bancaria_id": GAVETA_CONTA, "forma_pagamento_id": "6055919"})

def _saque_banco(valor, motivo, data):
    """Dinheiro que o dono SACOU do banco pra guardar (cofre/sobra): sai da conta
    bancária e vira dinheiro em espécie. Não passa pela gaveta, então não mexe no
    fechamento; e começa com SANGRIA pra não contar como gasto do mês."""
    gcapi.post("/pagamentos", {
        "descricao": f"SANGRIA {motivo}"[:180], "valor": f"{_num(valor):.2f}",
        "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
        "liquidado": "1", "plano_contas_id": SAQUE_SANGRIA_PLANO,
        "conta_bancaria_id": RESERVA_CONTA_ID, "forma_pagamento_id": GUARDADO_FORMA_ID})

def _origem_dinheiro(body):
    """De onde veio o dinheiro que o dono está guardando: 'caixa' (gaveta), 'banco'
    (saque) ou 'nenhum' (já estava fora). do_caixa é o formato antigo da tela."""
    o = (body.get("origem") or "").strip().lower()
    if o in ("caixa", "banco", "nenhum"):
        return o
    return "caixa" if body.get("do_caixa") else "nenhum"

def _reserva_saldo():
    """Quanto tem hoje no Dinheiro Reserva = [RES+] guardado − [RES-] gasto."""
    dep = out = 0.0
    for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
        d = (p.get("descricao") or "")
        v = _num(p.get("valor_total")) or _num(p.get("valor"))
        if d.startswith(RES_DEP_TAG):
            dep += v
        elif d.startswith(RES_OUT_TAG):
            out += v
    return round(dep - out, 2)

def _reserva_apaga_saida(codigo):
    """Tira o [RES-] de uma compra que foi apagada (senão a reserva fica devendo à toa)."""
    if not codigo:
        return 0.0
    total = 0.0
    for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
        d = (p.get("descricao") or "")
        if d.startswith(RES_OUT_TAG) and str(codigo) in d:
            try:
                gcapi.delete(f"/pagamentos/{p.get('id')}")
                total += _num(p.get("valor_total")) or _num(p.get("valor"))
            except Exception:
                pass
    if total:
        _invalida("reserva")
    return round(total, 2)

def _reserva_saida(valor, desc, data):
    """Desconta da reserva: grava um [RES-] (só ledger do painel) quando um pagamento
    foi feito com a fonte Dinheiro reserva."""
    try:
        gcapi.post("/pagamentos", {
            "descricao": f"{RES_OUT_TAG} {_sem_cat(desc)}"[:180], "valor": f"{_num(valor):.2f}",
            "data_vencimento": data, "data_competencia": data, "liquidado": "0",
            "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
        _invalida("reserva")
    except Exception:
        pass

def _sobra_saldo():
    """Quanto tem hoje na Sobra de Caixa = [SOB+] guardado − [SOB-] gasto."""
    dep = out = 0.0
    for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
        d = (p.get("descricao") or "")
        v = _num(p.get("valor_total")) or _num(p.get("valor"))
        if d.startswith(SOB_DEP_TAG):
            dep += v
        elif d.startswith(SOB_OUT_TAG):
            out += v
    return round(dep - out, 2)

def _sobra_saida(valor, desc, data):
    """Desconta da sobra de caixa: grava um [SOB-] (só ledger do painel)."""
    try:
        gcapi.post("/pagamentos", {
            "descricao": f"{SOB_OUT_TAG} {_sem_cat(desc)}"[:180], "valor": f"{_num(valor):.2f}",
            "data_vencimento": data, "data_competencia": data, "liquidado": "0",
            "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
        _invalida("sobra")
    except Exception:
        pass

def _sobra_apaga_saida(codigo):
    """Tira o [SOB-] de uma compra apagada (senão a sobra fica devendo à toa)."""
    if not codigo:
        return 0.0
    total = 0.0
    for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
        d = (p.get("descricao") or "")
        if d.startswith(SOB_OUT_TAG) and str(codigo) in d:
            try:
                gcapi.delete(f"/pagamentos/{p.get('id')}")
                total += _num(p.get("valor_total")) or _num(p.get("valor"))
            except Exception:
                pass
    if total:
        _invalida("sobra")
    return round(total, 2)

# ---- PREVISÕES (topo do contas a pagar): lista fixa de categorias recorrentes ----
# cada item que o dono adiciona vira um pagamento em aberto com descrição
# "[PREV] <categoria> — <obs>". Começam zeradas; somam conforme o dono insere.
# ---- CATEGORIA ESCOLHIDA (manda mais que o texto digitado) ----
# O dono escolhe a categoria no formulário e depois escreve uma observação livre.
# Antes eu classificava lendo a observação, e "Almoço Igor e Biel" virava pró-labore do
# Igor. Ele mandou parar: vale SEMPRE a categoria que ele escolheu. Então carimbo
# "[cat:<categoria>]" no começo da descrição e leio isso primeiro; a observação fica só
# como texto, e o painel tira a etiqueta antes de mostrar.
CAT_TAG_RE = re.compile(r"^\[cat:([^\]]+)\]\s*")

def _cat_marcada(desc):
    m = CAT_TAG_RE.match(desc or "")
    return m.group(1).strip() if m else None

def _sem_cat(desc):
    return CAT_TAG_RE.sub("", desc or "").strip()

# categoria do formulário -> linha do resumo "gastos do mês"
CAT_RESUMO = {
    "Lanche": "Lanches", "Almoço": "Lanches", "Padaria": "Lanches",
    "Sacolas / gelo / copos": "Sacolas / gelo / copos",
    "Motoboy / entrega": "PH Motoca", "PH Motoca": "PH Motoca",
    "Igor (pró-labore / retirada)": "Igor (pró-labore / retirada)",
    "Pró-labore Igor": "Igor (pró-labore / retirada)",
    "Retirada do sócio (Igor)": "Igor (pró-labore / retirada)",
    "Biel (Gabriel)": "Biel (Gabriel)", "Pagamento Biel": "Biel (Gabriel)",
    "Funcionário Gabriel (FDS)": "Biel (Gabriel)",
    "Retirada do sócio (Victor)": "Retirada Victor",
    "Outros": "Gastos adicionais",
}

PREV_TAG = "[PREV]"
CATEGORIAS_PREV = [
    ("Aluguel (IPTU)", "33015630"),
    ("Energia (CEMIG)", "33015649"),
    ("Água (COPASA)", "33015649"),
    ("Internet / telefone", "33015663"),
    ("Contabilidade (Werdeiros)", "33015635"),
    ("Igor (pró-labore / retirada)", "33015660"),
    ("Retirada Victor", "33015638"),
    ("Motoboy / entrega", "33015664"),
    ("Anota AI", "33015654"),
    ("DAS (Simples)", "35981822"),
    ("Parcelamento Simples (PARCSN)", "35981822"),
    ("Taxas / alvará (PBH)", "33015650"),
    ("INSS s/ pró-labore", "33015646"),
    ("Vigia", "33015661"),
    ("Seguro do carro", "33015633"),
    ("Sacolas / gelo / copos", "33015662"),
    ("Biel (Gabriel)", "33015660"),
    ("PH Motoca", "33015664"),
    ("Reparo da loja", "33015656"),
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
    ("PRO-LABORE", "Igor (pró-labore / retirada)"),
    ("PRÓ-LABORE", "Igor (pró-labore / retirada)"),
    ("RETIRADA", "Retirada Victor"),
    ("PH MOTOCA", "PH Motoca"),
    ("BIEL", "Biel (Gabriel)"), ("GABRIEL", "Biel (Gabriel)"),
    ("MOTOBOY", "Motoboy / entrega"),
    ("ANOTA", "Anota AI"),
    ("PARCSN", "Parcelamento Simples (PARCSN)"),
    ("PARCELAMENTO SIMPLES", "Parcelamento Simples (PARCSN)"),
    ("DRAM", "Taxas / alvará (PBH)"), ("FISCALIZ", "Taxas / alvará (PBH)"),
    ("ALVARA", "Taxas / alvará (PBH)"), ("ALVARÁ", "Taxas / alvará (PBH)"),
    ("SIMPLES", "DAS (Simples)"), ("DAS ", "DAS (Simples)"),
    ("VIGIA", "Vigia"),
    ("SEGURO", "Seguro do carro"),
    ("SACOLAS", "Sacolas / gelo / copos"), ("GELO", "Sacolas / gelo / copos"),
]

def _categoria_conta(p):
    """Categoria (das 17) de uma conta a pagar, ou None se não for previsão/recorrente."""
    desc = p.get("descricao") or ""
    marcada = _cat_marcada(desc)   # categoria escolhida no formulário manda
    if marcada:
        return None if marcada == "Outros" else marcada
    c = _prev_categoria(desc)      # itens [PREV] que o dono adiciona
    if c:
        return c
    du = desc.upper()
    # uma pessoa = uma categoria (nomes antigos caem na mesma linha); INSS antes do Igor
    if "IGOR" in du and "INSS" not in du:
        return "Igor (pró-labore / retirada)"
    if "BIEL" in du or "GABRIEL" in du:
        return "Biel (Gabriel)"
    for kw, label in PREV_KEYWORDS:  # provisões antigas e recorrentes
        if kw in du:
            return label
    return None

def _nota_conta(p):
    """Texto limpo pra mostrar de uma conta (tira as tags [PREV]/[provisao])."""
    desc = (p.get("descricao") or "")
    if desc.startswith(PREV_TAG):
        return _prev_nota(desc)
    return re.sub(r"\[provisao\]|\[PREV\]", "", _sem_cat(desc), flags=re.I).strip()

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
    # qualquer mexida em conta a pagar muda a "última atualização" do cabeçalho
    if any(k in ("pagar", "previsoes") for k in keys):
        _cache.pop("ultima_atualizacao", None)

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
    # o dono às vezes paga UMA conta com DUAS formas (ex.: aluguel de 1.035 = 1.000 no PIX
    # + 35 da gaveta). Nesse caso a conta original fica com a 1ª parte e as outras viram
    # lançamentos irmãos, pra cada pote ser descontado do jeito certo.
    partes = [x for x in (body.get("partes") or []) if _num(x.get("valor")) > 0]
    if partes:
        if len(partes) > 3:
            return jsonify({"ok": False, "erro": "no máximo 3 formas"}), 400
        forma = partes[0].get("forma") or "Caixa"
        valor_real = _num(partes[0].get("valor"))
    pot = POTES.get(forma, POTES["Caixa"])
    # preserva os campos da conta e só marca como paga
    cur = gcapi.get(f"/pagamentos/{pid}")
    p = cur.get("data") or cur
    if isinstance(p, list):
        p = p[0] if p else {}
    p = p.get("Pagamento", p) if isinstance(p, dict) else {}
    payload = {
        "descricao": _marca_rp(p.get("descricao") or "Conta", forma),
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
        if forma == "Dinheiro":
            _reserva_saida(payload["valor"], payload["descricao"] or "Conta", _hoje())
        elif forma == "Sobra":
            _sobra_saida(payload["valor"], payload["descricao"] or "Conta", _hoje())
        # demais formas da mesma conta: um lançamento por parte, já liquidado
        for parte in partes[1:]:
            f2 = parte.get("forma") or "Caixa"
            v2 = round(_num(parte.get("valor")), 2)
            pot2 = POTES.get(f2, POTES["Caixa"])
            desc2 = _marca_rp(f"{_rp_nota(payload['descricao'])} ({f2})", f2)
            gcapi.post("/pagamentos", {
                "descricao": desc2, "valor": f"{v2:.2f}",
                "plano_contas_id": payload["plano_contas_id"],
                "data_vencimento": payload["data_vencimento"],
                "data_competencia": payload["data_competencia"],
                "fornecedor_id": payload["fornecedor_id"],
                "liquidado": "1", "data_liquidacao": _hoje(),
                "conta_bancaria_id": pot2["conta"], "forma_pagamento_id": pot2["forma"]})
            if f2 == "Dinheiro":
                _reserva_saida(v2, desc2, _hoje())
            elif f2 == "Sobra":
                _sobra_saida(v2, desc2, _hoje())
        _invalida("pagar", "resumo", "previsoes", "gastos_mes", "reserva", "sobra", "mapa")
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
    desc = _marca_rp(f"[cat:{cat}] " + ((body.get("descricao") or "").strip() or cat), forma)
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
        if forma == "Dinheiro":
            _reserva_saida(valor, desc, data)
        elif forma == "Sobra":
            _sobra_saida(valor, desc, data)
        _invalida("pagar", "resumo", "reserva", "sobra", "gastos_mes", "mapa")
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
    # abrir o painel serve de gatilho: se já passou o intervalo, busca pedido novo em
    # segundo plano (a resposta não espera). Cobre o tempo em que o serviço estava dormindo.
    threading.Thread(target=_sync_anota, daemon=True).start()

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

def _produto_bruto(pid):
    """Cadastro do produto direto do GC (sem cache)."""
    p = gcapi.get(f"/produtos/{pid}").get("data")
    if isinstance(p, list):
        p = p[0] if p else {}
    return p.get("Produto", p) if isinstance(p, dict) else {}

def _fardo_cadastrado(prod):
    """Quantas unidades vêm no fardo, do jeito que o dono confirmou.

    O GestãoClick tem a "unidade de compra" dele, mas a API não lê nem escreve esse
    campo — então guardo o número na DESCRIÇÃO do produto como [fardo=N]. É esse
    valor que aparece pro dono na tela de compra, e é ele que manda no estoque.
    """
    m = re.search(r"\[fardo=(\d+(?:[.,]\d+)?)\]", prod.get("descricao") or "", re.I)
    return _num(m.group(1)) if m else None

def _grava_fardo(prod, n):
    """Guarda [fardo=N] na descrição do produto, preservando o texto que já existir."""
    desc = re.sub(r"\s*\[fardo=[^\]]*\]", "", prod.get("descricao") or "", flags=re.I).strip()
    novo = (desc + f" [fardo={int(n)}]").strip()
    gcapi.put(f"/produtos/{prod.get('id')}", {
        "nome": prod.get("nome"), "codigo_interno": prod.get("codigo_interno"),
        "descricao": novo})

def _fatores_das_compras():
    """Fator de fardo que o GestãoClick usa em cada produto.

    O cadastro do produto não expõe a "unidade de compra" pela API, MAS a linha da
    compra devolve `quantidade_saida` = quantas unidades entram por embalagem. Então
    aprendo o fator olhando as compras já lançadas (a mais recente vale).
    """
    def build():
        fat = {}
        for c in sorted(gcapi.get_all("/compras"), key=lambda x: (x.get("data_emissao") or "")):
            for w in (c.get("produtos") or []):
                p = w.get("produto", w)
                qs = _num(p.get("quantidade_saida"))
                if qs > 0:
                    fat[str(p.get("produto_id"))] = qs
        return fat
    return cached("fatores_compra", 3600, build)

@app.route("/api/compras-painel")
@login_required
def api_compras_painel():
    """Histórico das últimas compras lançadas (pro dono conferir e corrigir)."""
    def build():
        cs = sorted(gcapi.get_all("/compras"),
                    key=lambda c: ((c.get("data_emissao") or ""), str(c.get("id"))), reverse=True)
        itens = []
        for c in cs[:15]:
            prods = []
            for w in (c.get("produtos") or []):
                p = w.get("produto", w)
                q = _num(p.get("quantidade"))
                fator = _num(p.get("quantidade_saida")) or 1
                prods.append({"produto_id": str(p.get("produto_id")), "nome": p.get("nome_produto"),
                              "qtd": q, "fardo": fator, "unidades": round(q * fator, 2),
                              "valor": _num(p.get("valor_total")),
                              "unid": p.get("unidade") or "", "painel": "painel" in (p.get("detalhes") or "")})
            pags = [{"forma": (w.get("pagamento", w)).get("nome_forma_pagamento") or "—",
                     "valor": _num((w.get("pagamento", w)).get("valor"))}
                    for w in (c.get("pagamentos") or [])]
            itens.append({"id": str(c.get("id")), "codigo": c.get("codigo"),
                          "data": (c.get("data_emissao") or "")[:10],
                          "fornecedor": c.get("nome_fornecedor") or "—",
                          "valor": _num(c.get("valor_total")), "situacao": c.get("nome_situacao"),
                          "nota": c.get("numero_nfe") or "", "produtos": prods, "pagamentos": pags,
                          "do_painel": any(p["painel"] for p in prods)})
        return {"itens": itens, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("compras_painel", 120, build))

@app.route("/api/compra-excluir", methods=["POST"])
@login_required
def api_compra_excluir():
    """Apaga uma compra lançada errado e devolve o estoque ao que era.

    Compra confirmada não pode ser excluída direto: reabre ('Em aberto', que reverte
    o estoque) e só então apaga. Depois confiro o estoque item a item, porque a
    reversão do GC usa a conversão dele — que pode não ser a que eu usei ao lançar.
    """
    body = request.get_json(force=True, silent=True) or {}
    cid = str(body.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "erro": "sem id da compra"}), 400
    try:
        c = gcapi.get(f"/compras/{cid}").get("data")
        c = c[0] if isinstance(c, list) else c
        c = c.get("Compra", c)
        linhas, produtos_put = [], []
        for w in (c.get("produtos") or []):
            p = w.get("produto", w)
            pid = str(p.get("produto_id"))
            q = _num(p.get("quantidade"))
            prod = _produto_bruto(pid)
            fardo = _fardo_cadastrado(prod) or _num(p.get("quantidade_saida")) or 1
            linhas.append({"pid": pid, "nome": prod.get("nome"), "antes": _num(prod.get("estoque")),
                           "units": q * fardo})
            produtos_put.append({"produto": {"produto_id": pid, "quantidade": q,
                                             "valor_custo": _num(p.get("valor_custo")),
                                             "valor_total": _num(p.get("valor_total"))}})
        gcapi.put(f"/compras/{cid}", {"fornecedor_id": c.get("fornecedor_id"),
                                      "situacao_id": "1979925", "produtos": produtos_put})
        time.sleep(0.4)
        gcapi.delete(f"/compras/{cid}")
        ajustes = []
        for l in linhas:
            prod = _produto_bruto(l["pid"])
            alvo = round(l["antes"] - l["units"], 2)
            if abs(_num(prod.get("estoque")) - alvo) > 0.01:
                gcapi.put(f"/produtos/{l['pid']}", {
                    "nome": prod.get("nome"), "codigo_interno": prod.get("codigo_interno"),
                    "estoque": str(alvo)})
                ajustes.append(l["nome"])
        devolvido = _reserva_apaga_saida(c.get("codigo"))   # devolve o que saiu da reserva
        devolvido += _sobra_apaga_saida(c.get("codigo"))     # e o que saiu da sobra de caixa
        _invalida("resumo", "pagar", "catalogo", "reserva", "sobra", "abc", "compras_painel", "fatores_compra", "mapa")
        return jsonify({"ok": True, "codigo": c.get("codigo"), "ajustados": ajustes,
                        "itens": len(linhas), "reserva_devolvida": devolvido})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/fardo", methods=["POST"])
@login_required
def api_fardo():
    """Salva quantas unidades vêm no fardo de um produto (o dono corrige na tela)."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("produto_id") or "").strip()
    n = _num(body.get("fardo"))
    if not pid or n <= 0:
        return jsonify({"ok": False, "erro": "produto e quantidade do fardo são obrigatórios"}), 400
    try:
        p = _produto_bruto(pid)
        _grava_fardo(p, n)
        _invalida("catalogo")
        return jsonify({"ok": True, "nome": p.get("nome"), "fardo": int(n)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

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
    produtos, total, plano = [], 0.0, []
    for it in itens:
        pid = str(it.get("produto_id") or "").strip()
        qtd = _num(it.get("quantidade"))          # quantos FARDOS/caixas
        mult = _num(it.get("mult")) or 1          # unidades dentro de cada fardo
        valor = _num(it.get("valor"))             # valor total pago nesse item
        if not pid or qtd <= 0 or valor <= 0:
            return jsonify({"ok": False, "erro": "item incompleto"}), 400
        units = qtd * mult                        # unidades que TÊM que entrar no estoque
        total += valor
        # o GC multiplica a quantidade pela conversão do cadastro dele (que a API não
        # lê nem escreve), então mando em FARDOS e confiro o estoque depois.
        produtos.append({"produto": {
            "produto_id": pid, "quantidade": qtd,
            "valor_custo": round(valor / qtd, 4), "valor_total": round(valor, 2),
            "detalhes": "compra sem nota (painel)",
        }})
        plano.append({"pid": pid, "qtd": qtd, "mult": mult, "units": units, "valor": valor,
                      "antes": _num(_produto_bruto(pid).get("estoque"))})
    # pagamento pode vir dividido: [{"forma": "PIX", "valor": 300}, {"forma": "Caixa", ...}]
    # (o dono às vezes paga parte no PIX e parte em dinheiro). Sem a lista, cai no
    # comportamento antigo: uma forma só, com o total.
    partes = [p for p in (body.get("pagamentos") or []) if _num(p.get("valor")) > 0]
    if not partes:
        partes = [{"forma": forma, "valor": total}]
    # não deixa a reserva ficar negativa (o dono pediu): se o que ele quer tirar
    # passa do que tem guardado, avisa antes de gravar qualquer coisa
    quer_reserva = round(sum(_num(p.get("valor")) for p in partes
                             if (p.get("forma") or "Caixa") == "Dinheiro"), 2)
    if quer_reserva > 0 and not body.get("forcar_reserva"):
        saldo = _reserva_saldo()
        if quer_reserva > saldo + 0.01:
            return jsonify({"ok": False, "reserva_insuficiente": True, "saldo": saldo,
                            "pedido": quer_reserva,
                            "erro": f"a reserva tem R$ {saldo:.2f} e você quer tirar "
                                    f"R$ {quer_reserva:.2f} dela. Ajuste o valor ou troque "
                                    f"a forma (Caixa/PIX)."}), 400
    # mesma proteção pra sobra de caixa
    quer_sobra = round(sum(_num(p.get("valor")) for p in partes
                           if (p.get("forma") or "Caixa") == "Sobra"), 2)
    if quer_sobra > 0 and not body.get("forcar_reserva"):
        saldo_s = _sobra_saldo()
        if quer_sobra > saldo_s + 0.01:
            return jsonify({"ok": False, "reserva_insuficiente": True, "saldo": saldo_s,
                            "pedido": quer_sobra,
                            "erro": f"a sobra de caixa tem R$ {saldo_s:.2f} e você quer tirar "
                                    f"R$ {quer_sobra:.2f} dela. Ajuste o valor ou troque "
                                    f"a forma (Caixa/PIX)."}), 400
    if len(partes) > 4:
        return jsonify({"ok": False, "erro": "no máximo 4 formas de pagamento"}), 400
    soma = round(sum(_num(p.get("valor")) for p in partes), 2)
    if abs(soma - round(total, 2)) > 0.02:
        return jsonify({"ok": False, "erro": f"as formas somam R$ {soma:.2f} e a compra é "
                                             f"R$ {total:.2f} — ajuste os valores"}), 400
    pagamentos = []
    for p in partes:
        f = p.get("forma") or "Caixa"
        v = round(_num(p.get("valor")), 2)
        if f == "Boleto":     # a prazo: vira conta a pagar, não sai do caixa agora
            pagamentos.append({"pagamento": {
                "data_vencimento": (p.get("vencimento") or data)[:10], "valor": v,
                "forma_pagamento_id": BOLETO_FORMA_ID, "plano_contas_id": "33015669",
                "liquidado": "0"}})
        else:
            pot = POTES.get(f, POTES["Caixa"])
            pagamentos.append({"pagamento": {
                "data_vencimento": data, "valor": v,
                "forma_pagamento_id": pot["forma"], "plano_contas_id": "33015669",
                "conta_bancaria_id": pot["conta"], "liquidado": "1", "data_liquidacao": data}})
    condicao = "a_prazo" if all(p.get("forma") == "Boleto" for p in partes) else "a_vista"
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
        # ---- confere o que ENTROU de verdade ----
        # o GC aplica a conversão de compra dele (que a API não enxerga). Então leio o
        # estoque depois: se não entrou o que devia, acerto na hora e gravo o custo
        # por UNIDADE. Assim não tem mais "lancei 10 e entraram 120".
        # o MESMO produto pode aparecer em mais de uma linha da compra (dois fardos com
        # preço diferente, por exemplo). Conferindo linha a linha, a 2ª linha lê o
        # estoque que a 1ª acabou de gravar, acha que "entrou demais" e sobrescreve —
        # sobra só a última linha. Foi isso que comeu 48 un de JACK POWER na compra
        # 810812 (14/08/2026). Então junto por produto e confiro UMA vez, com a soma.
        agreg = {}
        for it in plano:
            a = agreg.get(it["pid"])
            if a is None:
                agreg[it["pid"]] = dict(it)
            else:
                a["units"] += it["units"]
                a["valor"] += it["valor"]
        conf = []
        for it in agreg.values():
            prod = _produto_bruto(it["pid"])
            depois = _num(prod.get("estoque"))
            entrou = round(depois - it["antes"], 2)
            corrigido = False
            if abs(entrou - it["units"]) > 0.01:
                gcapi.put(f"/produtos/{it['pid']}", {
                    "nome": prod.get("nome"), "codigo_interno": prod.get("codigo_interno"),
                    "estoque": str(round(it["antes"] + it["units"], 2))})
                corrigido = True
            # custo sempre por unidade (o GC copia o valor da linha, que é do fardo)
            gcapi.put(f"/produtos/{it['pid']}", {
                "nome": prod.get("nome"), "codigo_interno": prod.get("codigo_interno"),
                "valor_custo": f"{it['valor'] / it['units']:.4f}"})
            if it["mult"] > 1 and _fardo_cadastrado(prod) != it["mult"]:
                _grava_fardo(prod, it["mult"])       # aprende o fardo que o dono usou
            conf.append({"nome": prod.get("nome"), "unidades": it["units"],
                         "entrou_sozinho": entrou, "corrigido": corrigido,
                         "custo_unit": round(it["valor"] / it["units"], 4)})
        # ---- acerta a conta/forma de cada parcela ----
        # o GC cria os pagamentos da compra sempre na conta CAIXA e, com mais de uma
        # forma, repete a primeira em todas. Então reescrevo cada parcela com o pote
        # certo (o PUT exige o body COMPLETO: mandar só um campo zera o valor).
        try:
            codigo = str(d.get("codigo") or "")
            pgs = [x for x in gcapi.get_all("/pagamentos", {"data_inicio": data, "data_fim": data})
                   if (x.get("descricao") or "") == f"Compra de nº {codigo}"]
            usados = set()
            for parte in partes:
                f = parte.get("forma") or "Caixa"
                v = round(_num(parte.get("valor")), 2)
                alvo = next((x for x in pgs if str(x.get("id")) not in usados
                             and abs(_num(x.get("valor_total")) - v) < 0.01), None)
                if not alvo:
                    continue
                usados.add(str(alvo.get("id")))
                pot = POTES.get(f)
                corpo = {"descricao": _marca_rp(alvo.get("descricao"), f), "valor": f"{v:.2f}",
                         "data_vencimento": (parte.get("vencimento") or data)[:10],
                         "data_competencia": data, "plano_contas_id": "33015669"}
                if f == "Boleto":
                    corpo.update({"liquidado": "0", "forma_pagamento_id": BOLETO_FORMA_ID})
                else:
                    corpo.update({"liquidado": "1", "data_liquidacao": data,
                                  "conta_bancaria_id": pot["conta"],
                                  "forma_pagamento_id": pot["forma"]})
                gcapi.put(f"/pagamentos/{alvo.get('id')}", corpo)
        except Exception:
            pass   # se falhar, a compra continua válida; só o rótulo do pote fica genérico
        # a reserva desconta só a parte paga com "Dinheiro" (pode ser parcial agora)
        em_reserva = round(sum(_num(p.get("valor")) for p in partes
                               if (p.get("forma") or "Caixa") == "Dinheiro"), 2)
        if em_reserva > 0:
            _reserva_saida(em_reserva, f"Compra {d.get('codigo') or ''}".strip(), data)
        em_sobra = round(sum(_num(p.get("valor")) for p in partes
                             if (p.get("forma") or "Caixa") == "Sobra"), 2)
        if em_sobra > 0:
            _sobra_saida(em_sobra, f"Compra {d.get('codigo') or ''}".strip(), data)
        _invalida("resumo", "pagar", "catalogo", "reserva", "sobra", "abc", "mapa")
        return jsonify({"ok": True, "id": d.get("id"), "codigo": d.get("codigo"),
                        "total": round(total, 2), "itens": len(produtos),
                        "conferencia": conf})
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
    # fardo: primeiro o que o dono confirmou ([fardo=N] na descrição); se não tiver,
    # o fator que o próprio GC usou na última compra desse produto
    fardo = _fardo_cadastrado(p)
    origem = "confirmado" if fardo else None
    if not fardo:
        fardo = _fatores_das_compras().get(pid)
        origem = "compra anterior" if fardo else None
    return jsonify({"ok": True, "nome": p.get("nome"),
                    "estoque": _num(p.get("estoque")), "custo": _num(p.get("valor_custo")),
                    "fardo": fardo, "fardo_origem": origem})

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
        _invalida("resumo", "catalogo", "abc")
        return jsonify({"ok": True, "antes": antes, "depois": contagem,
                        "dif": contagem - antes, "nome": cur.get("nome")})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

AJUSTE_CAIXA_PLANO = "33015682"  # plano "Ajuste de caixa" no GestãoClick

def _abertura_caixa(data, tolerante=True):
    """Valor da 'Abertura de caixa' (troco) lançada no GestãoClick nesse dia, ou None
    se o dono ainda não abriu o caixa.

    `tolerante=False` faz a falha de leitura estourar em vez de virar None: na hora de
    GRAVAR um fechamento, None cai no chute de R$ 200 e grava a quebra errada — foi o
    que aconteceu em 30/08/2026 (a consulta falhou, o troco virou 200 no lugar de 250 e
    a sobra saiu R$ 50 maior). Pra só mostrar na tela, tolerante=True está de bom tamanho.
    """
    try:
        recs = gcapi.get_all("/recebimentos", {"data_inicio": data, "data_fim": data})
    except Exception:
        if tolerante:
            return None
        raise
    for r in recs:
        if "ABERTURA DE CAIXA" in (r.get("descricao") or "").upper():
            return round(_num(r.get("valor_total")) or _num(r.get("valor")), 2)
    return None

def _fech_calc(data):
    """Dinheiro que ENTROU (vendas em dinheiro, balcão + delivery) e SAÍDAS em
    dinheiro do caixa no dia. O delivery em dinheiro entra na MESMA gaveta (o
    entregador traz), então conta pro fechamento."""
    din = 0.0
    for v in gcapi.get_all("/vendas", {"tipo": "vendas_balcao", "data_inicio": data, "data_fim": data}):
        for w in v.get("pagamentos") or []:
            p = w.get("pagamento", w)
            if "DINHEIRO" in (p.get("nome_forma_pagamento") or "").upper():
                din += _num(p.get("valor"))
    for v in gcapi.get_all("/vendas", {"tipo": "produto", "data_inicio": data, "data_fim": data}):
        if "ANOTA AI" not in (v.get("observacoes") or "").upper():
            continue  # só delivery (não saque, que já é sangria)
        for w in v.get("pagamentos") or []:
            p = w.get("pagamento", w)
            if "DINHEIRO" in (p.get("nome_forma_pagamento") or "").upper():
                din += _num(p.get("valor"))
    din += _entradas_gaveta(data)   # troca de PIX / pagamento de conta (fiado)
    saidas = 0.0
    # janela larga em vez de "todos os pagamentos": o filtro da API é pelo VENCIMENTO e o
    # que importa aqui é a data de liquidação (conta antiga paga hoje tem que entrar),
    # mas puxar a base inteira é pesado e já derrubou a consulta com erro 500 do GC.
    _d = datetime.date.fromisoformat(data)
    _jan = {"data_inicio": (_d - datetime.timedelta(days=120)).isoformat(),
            "data_fim": (_d + datetime.timedelta(days=60)).isoformat()}
    for p in gcapi.get_all("/pagamentos", _jan):
        if str(p.get("liquidado")) != "1":
            continue
        if (p.get("data_liquidacao") or "")[:10] != data:
            continue
        if str(p.get("plano_contas_id")) == AJUSTE_CAIXA_PLANO:
            continue  # o próprio ajuste de fechamento NÃO é saída da gaveta (evita loop/erro)
        if _saiu_da_gaveta(p):
            saidas += _num(p.get("valor_total")) or _num(p.get("valor"))
    return round(din, 2), round(saidas, 2)

@app.route("/api/fechamento-hoje")
@login_required
def api_fechamento_hoje():
    """Quanto DEVERIA ter na gaveta no DIA escolhido (?data=AAAA-MM-DD, default hoje)
    = abertura + dinheiro que entrou − saídas em dinheiro, tudo daquele dia."""
    data = (request.args.get("data") or _hoje())[:10]
    def build():
        ab = _abertura_caixa(data)                  # abertura real lançada no GC no dia
        troco = ab if ab is not None else (_num(request.args.get("troco")) or 200.0)
        din, saidas = _fech_calc(data)              # vendas em dinheiro + saídas DO DIA
        return {"data": data, "troco": round(troco, 2), "abertura_gc": ab is not None,
                "dinheiro": din, "saidas": saidas,
                "esperado": round(troco + din - saidas, 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("fech_" + data, 45, build))

@app.route("/api/abc")
@login_required
def api_abc():
    """Curva ABC + sugestão de compra + parados, tudo AO VIVO.

    Antes isso vinha do snapshot dos gráficos (gerado 8:30 no Mac e publicado no
    deploy), então acerto de estoque feito durante o dia não aparecia. Agora sai
    direto do GestãoClick: venda dos últimos 30 dias + estoque do momento.
    """
    def build():
        hoje = datetime.date.today()
        ini30 = (hoje - datetime.timedelta(days=29)).isoformat()
        d7 = (hoje - datetime.timedelta(days=6)).isoformat()
        un30, fat30, un7 = defaultdict(float), defaultdict(float), defaultdict(float)
        for v in gcapi.get_all("/vendas", {"tipo": "vendas_balcao",
                                           "data_inicio": ini30, "data_fim": hoje.isoformat()}):
            d = (v.get("data") or "")[:10]
            for w in (v.get("produtos") or []):
                p = w.get("produto", w)
                pid = str(p.get("produto_id") or "")
                q = _num(p.get("quantidade"))
                un30[pid] += q
                fat30[pid] += _num(p.get("valor_total"))
                if d >= d7:
                    un7[pid] += q
        prods = {str(p.get("id")): p for p in gcapi.get_all("/produtos")
                 if str(p.get("ativo")) == "1"}
        total_fat = sum(fat30.values()) or 1.0
        ordem = sorted(fat30.items(), key=lambda kv: -kv[1])
        acum, classe = 0.0, {}
        for pid, f in ordem:
            acum += f
            classe[pid] = "A" if acum <= 0.8 * total_fat else ("B" if acum <= 0.95 * total_fat else "C")
        itens = []
        for pid, f in ordem:
            p = prods.get(pid)
            if not p:
                continue
            vel = un30[pid] / 30.0
            est = _num(p.get("estoque"))
            def alvo(dias):
                falta = vel * dias - est
                return {"un": int(falta + 0.999)} if falta > 0 else {"un": 0}
            itens.append({"pid": pid, "nome": p.get("nome"), "classe": classe.get(pid, "C"),
                          "vel_dia": round(vel, 2), "fat30": round(f, 2),
                          "vendeu30": round(un30[pid], 1), "vendeu7": round(un7[pid], 1),
                          "estoque": est, "custo": _num(p.get("valor_custo")),
                          "c7": alvo(7), "c15": alvo(15), "c30": alvo(30)})
        # parados: tem estoque parado e não vendeu nada na semana
        parados = [{"nome": p.get("nome"), "estoque": _num(p.get("estoque")),
                    "capital": round(_num(p.get("estoque")) * _num(p.get("valor_custo")), 2),
                    "vendeu30": round(un30.get(pid, 0), 1)}
                   for pid, p in prods.items()
                   if _num(p.get("estoque")) > 0 and un7.get(pid, 0) == 0]
        parados.sort(key=lambda x: -x["capital"])
        resumo = {"A": sum(1 for c in classe.values() if c == "A"),
                  "B": sum(1 for c in classe.values() if c == "B"),
                  "C": sum(1 for c in classe.values() if c == "C")}
        return {"sugestoes": itens[:150], "parados": parados[:30], "abc_resumo": resumo,
                "negativos": sum(1 for p in prods.values() if _num(p.get("estoque")) < 0),
                "valor_estoque": round(sum(_num(p.get("estoque")) * _num(p.get("valor_custo"))
                                           for p in prods.values()), 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("abc", 900, build))  # pesado (30 dias de venda): cache 15 min

@app.route("/api/fechamentos")
@login_required
def api_fechamentos():
    """Últimos N dias de fechamento, AO VIVO (a tabela antiga vinha do snapshot dos
    gráficos, que é gerado 8:30 e nunca trazia o contado — por isso um fechamento
    feito à noite aparecia como 'sem contagem' no dia seguinte).

    Regras (as mesmas do cálculo do dia): saída em dinheiro NÃO inclui o plano
    'Ajuste de caixa' — lá dentro estão o 'Fechamento de caixa' do PDV (que é a
    retirada da gaveta DEPOIS da contagem) e o próprio ajuste da quebra. Somar
    isso derrubava o esperado pra negativo.
    """
    dias = max(1, min(31, int(request.args.get("dias") or 12)))
    def build():
        hoje = datetime.date.today()
        ini = (hoje - datetime.timedelta(days=dias - 1)).isoformat()
        fim = hoje.isoformat()
        din = defaultdict(float)
        for tipo in ("vendas_balcao", "produto"):
            for v in gcapi.get_all("/vendas", {"tipo": tipo, "data_inicio": ini, "data_fim": fim}):
                if tipo == "produto" and "ANOTA AI" not in (v.get("observacoes") or "").upper():
                    continue  # em 'produto' só o delivery conta (o resto é saque/serviço)
                d = (v.get("data") or v.get("data_venda") or "")[:10]
                for w in (v.get("pagamentos") or []):
                    p = w.get("pagamento", w)
                    if "DINHEIRO" in (p.get("nome_forma_pagamento") or "").upper():
                        din[d] += _num(p.get("valor"))
        saidas = defaultdict(float)
        for p in gcapi.get_all("/pagamentos", {"data_inicio": ini, "data_fim": fim}):
            if str(p.get("liquidado")) != "1":
                continue
            d = (p.get("data_liquidacao") or "")[:10]
            if not d or str(p.get("plano_contas_id")) == AJUSTE_CAIXA_PLANO:
                continue
            if _saiu_da_gaveta(p):
                saidas[d] += _num(p.get("valor_total")) or _num(p.get("valor"))
        aberturas, contados, moedas = {}, {}, {}
        for r in (gcapi.get_all("/recebimentos", {"data_inicio": ini, "data_fim": fim})
                  + gcapi.get_all("/pagamentos", {"data_inicio": ini, "data_fim": fim})):
            desc = (r.get("descricao") or "")
            d = (r.get("data_liquidacao") or r.get("data_vencimento") or "")[:10]
            if "ABERTURA DE CAIXA" in desc.upper():
                aberturas[d] = _num(r.get("valor_total")) or _num(r.get("valor"))
            if _eh_entrada_gaveta(r):      # troca de PIX / pagamento de conta entram na gaveta
                din[(r.get("data_liquidacao") or "")[:10]] += (_num(r.get("valor_total"))
                                                               or _num(r.get("valor")))
            m = re.search(r"FECHAMENTO (\d{4}-\d{2}-\d{2}).*?contado R\$ ?([\d.,]+)", desc, re.I)
            if m:
                contados[m.group(1)] = _num(m.group(2))
                mm = re.search(r"moedas R\$ ?([\d.,]+)", desc, re.I)
                if mm:
                    moedas[m.group(1)] = _num(mm.group(1))
        itens = []
        for i in range(dias):
            d = (hoje - datetime.timedelta(days=dias - 1 - i)).isoformat()
            ab = aberturas.get(d, 200.0)
            esp = round(ab + din.get(d, 0) - saidas.get(d, 0), 2)
            c = contados.get(d)
            itens.append({"data": d, "abertura": round(ab, 2), "abertura_real": d in aberturas,
                          "dinheiro": round(din.get(d, 0), 2),
                          "saidas": round(saidas.get(d, 0), 2), "esperado": esp,
                          "contado": c, "quebra": None if c is None else round(c - esp, 2),
                          "moedas": moedas.get(d)})
        ult_moedas = next((x["moedas"] for x in reversed(itens) if x.get("moedas") is not None), None)
        return {"itens": itens, "ultimas_moedas": ult_moedas,
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached(f"fechamentos_{dias}", 120, build))

@app.route("/api/fechamento", methods=["POST"])
@login_required
def api_fechamento():
    """Fecha o caixa: compara o contado com o esperado e lança o Ajuste de caixa no
    sistema (faltou = saída; sobrou = entrada) pra o caixa BATER com a gaveta."""
    body = request.get_json(force=True, silent=True) or {}
    data = (body.get("data") or _hoje())[:10]
    if data > _hoje():
        return jsonify({"ok": False, "erro": f"a data {data[8:10]}/{data[5:7]} ainda não "
                        "aconteceu — não dá pra fechar um dia no futuro. Confira o campo Dia."}), 400
    try:
        ab = _abertura_caixa(data, tolerante=False)  # usa a abertura real do GC
    except Exception:
        return jsonify({"ok": False, "erro": "não consegui ler a abertura do caixa desse dia "
                        "no sistema agora. Tente de novo em alguns segundos — assim eu não "
                        "fecho o dia com o troco errado."}), 502
    troco = ab if ab is not None else (_num(body.get("troco")) or 200.0)
    contado = _num(body.get("contado"))
    if body.get("contado") in (None, ""):
        return jsonify({"ok": False, "erro": "conte a gaveta primeiro"}), 400
    din, saidas = _fech_calc(data)
    # Dia SEM abertura e SEM nenhum movimento = quase sempre o dono errou o campo Dia. Se
    # deixar passar, o esperado cai no chute de R$ 200 e a quebra sai gigante e falsa —
    # foi o que criou uma "sobra" de R$ 319 em 20/08/2026 (fechamento feito na noite do 19).
    if ab is None and din == 0 and saidas == 0 and not body.get("forcar_dia_vazio"):
        return jsonify({"ok": False, "dia_vazio": True,
                        "erro": f"o dia {data[8:10]}/{data[5:7]} não tem abertura de caixa nem "
                                "nenhuma venda em dinheiro no sistema. Confira se a data está "
                                "certa antes de fechar."}), 400
    esperado = round(troco + din - saidas, 2)
    quebra = round(contado - esperado, 2)
    ajuste_id = None
    try:
        # o total de moedas fica gravado na descrição: é ele que permite ao dono
        # contar moeda só na segunda e repetir o valor nos outros dias
        moedas = _num(body.get("moedas"))
        extra = f" · moedas R$ {moedas:.2f}" if moedas > 0 else ""
        if body.get("moedas_repetidas"):
            extra += " (moedas da última contagem)"
        resultado = (f"{'sobra' if quebra > 0 else 'falta'} R$ {abs(quebra):.2f}"
                     if abs(quebra) >= 0.01 else "sem quebra")
        desc = (f"FECHAMENTO {data} — contado R$ {contado:.2f} · esperado R$ {esperado:.2f} · "
                f"{resultado}{extra}")
        mov = {"descricao": desc, "valor": f"{abs(quebra):.2f}",
               "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
               "liquidado": "1", "plano_contas_id": AJUSTE_CAIXA_PLANO,
               "conta_bancaria_id": "696747", "forma_pagamento_id": "6055919"}
        # sobra = entra dinheiro (recebimento) ; falta = sai dinheiro (pagamento). Caixa que
        # bate certinho não tem ajuste a fazer, mas o registro entra do mesmo jeito com valor
        # R$ 0,00 (não mexe em saldo nenhum) — senão o dia continuava aparecendo no painel
        # como "sem contagem" mesmo depois de conferido.
        r = gcapi.post("/pagamentos" if quebra <= -0.01 else "/recebimentos", mov)
        ajuste_id = (r.get("data") or {}).get("id") if isinstance(r.get("data"), dict) else None
        _invalida("resumo", "hoje", "fech_hoje", "fechamentos_12", "fechamentos_7", "fechamentos_31")
        return jsonify({"ok": True, "esperado": esperado, "contado": round(contado, 2),
                        "quebra": quebra, "dinheiro": din, "saidas": saidas,
                        "ajuste_id": ajuste_id})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

# nomes fixos que o dono quer ver SEMPRE no topo (mesmo zerados), na ordem
RESUMO_FIXOS = ["PH Motoca", "Biel (Gabriel)", "Retirada Victor",
                "Igor (pró-labore / retirada)", "Sacolas / gelo / copos",
                "Lanches", "Gastos adicionais"]

def _eh_mercadoria(desc):
    """Pagamento de mercadoria = nota/compra (GC nomeia 'Compra de nº ...') ou boleto
    do DDA do fornecedor lançado na mão, que não tem nota vinculada mas é mercadoria
    do mesmo jeito — senão o pagamento dele cai em 'gastos adicionais' no mês."""
    d = (desc or "").upper()
    return d.startswith("COMPRA DE") or "BOLETO DDA" in d

def _cat_resumo(p):
    """Bucket do resumo do topo. Separa Victor/Igor e reconhece os nomes do dono;
    o que não casar cai em 'Gastos adicionais' (nunca no plano 'Compras' bugado)."""
    desc = (p.get("descricao") or "")
    marcada = _cat_marcada(desc)
    if marcada:                       # o dono escolheu no formulário: vale essa, ponto
        return CAT_RESUMO.get(marcada, marcada)
    du = desc.upper()
    # (só pros lançamentos ANTIGOS, sem etiqueta) COMIDA VEM PRIMEIRO, antes do nome das pessoas: "Almoço Igor e Biel" é o almoço
    # que o dono pagou PRA eles, não pró-labore do Igor. Antes caía em "Igor (pró-labore)"
    # e sumia dos Lanches. Aceita as digitadas rápido também ("almço", "almoco").
    COMIDA = ("LANCH", "ALMO", "ALMÇ", "ALMOC", "PADARIA", "PÃO", "PAO",
              "MARMITA", "JANTA", "REFEIÇ", "REFEIC")
    if any(k in du for k in COMIDA):
        return "Lanches"
    if "IGOR" in du and "INSS" not in du:      # pró-labore e retirada do Igor = mesma linha
        return "Igor (pró-labore / retirada)"
    if "RETIRADA" in du:
        return "Retirada Victor"
    if "BIEL" in du or "GABRIEL" in du:        # Biel = Gabriel = mesma pessoa
        return "Biel (Gabriel)"
    if "MOTOCA" in du or "MOTOBOY" in du:
        return "PH Motoca"
    c = _categoria_conta(p)   # aluguel, energia, contab, DAS, seguro... viram linha própria
    if c and c not in ("Retirada Victor", "PH Motoca", "Biel (Gabriel)",
                       "Igor (pró-labore / retirada)", "Motoboy / entrega"):
        return c
    return "Gastos adicionais"

@app.route("/api/gastos-mes")
@login_required
def api_gastos_mes():
    """Resumo do topo do Financeiro: o que JÁ SAIU no mês, ITEMIZADO pelos nomes do
    dono (sempre listados, começam zerados e sobem conforme ele lança) + mercadoria
    (compras) + TOTAL GERAL. Fora: ajuste de caixa (quebra) e sangria de saque."""
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        mes = _hoje()[:7]
        buckets = {k: 0.0 for k in RESUMO_FIXOS}   # nomes fixos sempre aparecem
        extras, compras = {}, 0.0
        for p in pgs:
            if str(p.get("liquidado")) != "1":
                continue  # só o que já saiu da conta (pago)
            if (p.get("data_liquidacao") or "")[:7] != mes:
                continue  # pago dentro deste mês
            desc = (p.get("descricao") or "").strip()
            if desc.upper().startswith("SANGRIA"):
                continue  # saque e dinheiro guardado: troca de lugar, não é gasto
            if (p.get("nome_plano_conta") or "") == "Ajuste de caixa":
                continue  # acerto de gaveta (quebra) não é gasto
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            if _eh_mercadoria(desc):
                compras += val  # mercadoria: soma só no total geral
                continue
            cat = _cat_resumo(p)
            if cat in buckets:
                buckets[cat] += val
            else:
                extras[cat] = extras.get(cat, 0.0) + val
        itens = [{"cat": k, "total": round(buckets[k], 2), "fixo": True} for k in RESUMO_FIXOS]
        itens += sorted([{"cat": k, "total": round(v, 2), "fixo": False}
                         for k, v in extras.items()], key=lambda x: -x["total"])
        total = round(sum(buckets.values()) + sum(extras.values()), 2)
        return {"itens": itens, "total": total,
                "compras": round(compras, 2),
                "total_geral": round(total + compras, 2), "mes": mes,
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

# ---- ENTRADAS AVULSAS NA GAVETA (troca de PIX e pagamento de conta) ----
# Dinheiro que entra no caixa SEM ser venda nova do PDV:
#  · TROCA PIX — o contrário do saque: o cliente entrega dinheiro e o dono manda PIX pra ele
#  · PAGAMENTO DE CONTA — cliente do fiado vem pagar. A venda JÁ foi lançada no sistema, então
#    lançar de novo dobraria o faturamento: aqui entra só o dinheiro na gaveta.
# São recebimentos com a marca na descrição — o fechamento soma eles junto com as vendas.
# (não dá pra somar recebimento em dinheiro em geral: cada venda do balcão já gera um,
# e o fechamento contaria a mesma venda duas vezes.)
ENTRADA_TAGS = ("PAGAMENTO DE CONTA", "TROCA PIX")
PLANO_AJUSTE_C = "33015692"       # "Ajuste de caixa" (crédito) — troca de PIX não é receita
PLANO_VENDA_BALCAO = "33015685"   # o fiado já foi venda; o dinheiro entra como venda no balcão

def _eh_entrada_gaveta(r):
    """Recebimento que é entrada avulsa de dinheiro na gaveta (troca de PIX / fiado)."""
    if str(r.get("liquidado")) != "1":
        return False
    if str(r.get("forma_pagamento_id")) != "6055919":
        return False
    if str(r.get("conta_bancaria_id") or "") != GAVETA_CONTA:
        return False
    d = (r.get("descricao") or "").upper()
    return any(d.startswith(t) for t in ENTRADA_TAGS)

def _entradas_gaveta(data):
    """Total das entradas avulsas em dinheiro do dia (fora as vendas)."""
    try:
        recs = gcapi.get_all("/recebimentos", {"data_inicio": data, "data_fim": data})
    except Exception:
        return 0.0
    return round(sum((_num(r.get("valor_total")) or _num(r.get("valor")))
                     for r in recs if _eh_entrada_gaveta(r)
                     and (r.get("data_liquidacao") or "")[:10] == data), 2)

@app.route("/api/troca-pix", methods=["POST"])
@login_required
def api_troca_pix():
    """O contrário do saque: o cliente entrega dinheiro e a loja manda PIX pra ele.
    Entra dinheiro na gaveta e sai PIX da conta."""
    body = request.get_json(force=True, silent=True) or {}
    recebido = round(_num(body.get("recebido")), 2)          # dinheiro que veio do cliente
    enviado = round(_num(body.get("enviado")) or recebido, 2)  # PIX mandado pra ele
    data = (body.get("data") or _hoje())[:10]
    if recebido <= 0 or enviado <= 0:
        return jsonify({"ok": False, "erro": "informe quanto recebeu em dinheiro"}), 400
    if enviado > recebido + 0.01:
        return jsonify({"ok": False, "erro": f"você mandaria R$ {enviado:.2f} no PIX tendo "
                        f"recebido R$ {recebido:.2f} em dinheiro — confira os valores"}), 400
    ganho = round(recebido - enviado, 2)
    try:
        gcapi.post("/recebimentos", {
            "descricao": f"TROCA PIX R$ {enviado:.2f} — recebi R$ {recebido:.2f} em dinheiro"
                         + (f" · lucro R$ {ganho:.2f}" if ganho > 0 else ""),
            "valor": f"{recebido:.2f}", "data_vencimento": data, "data_competencia": data,
            "data_liquidacao": data, "liquidado": "1", "plano_contas_id": PLANO_AJUSTE_C,
            "conta_bancaria_id": GAVETA_CONTA, "forma_pagamento_id": "6055919"})
        gcapi.post("/pagamentos", {
            "descricao": f"TROCA PIX R$ {enviado:.2f} — PIX enviado ao cliente",
            "valor": f"{enviado:.2f}", "data_vencimento": data, "data_competencia": data,
            "data_liquidacao": data, "liquidado": "1", "plano_contas_id": AJUSTE_CAIXA_PLANO,
            "conta_bancaria_id": RESERVA_CONTA_ID, "forma_pagamento_id": "6055931"})
        _invalida("resumo", "hoje", "fech_hoje", "fechamentos_7", "fechamentos_12",
                  "fechamentos_31", "saque_resumo")
        return jsonify({"ok": True, "recebido": recebido, "enviado": enviado, "ganho": ganho})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/pagamento-conta", methods=["POST"])
@login_required
def api_pagamento_conta():
    """Cliente do fiado pagando a conta: entra dinheiro na gaveta SEM lançar venda de novo
    (a venda já está no sistema — lançar outra dobraria o faturamento)."""
    body = request.get_json(force=True, silent=True) or {}
    valor = round(_num(body.get("valor")), 2)
    cliente = (body.get("cliente") or "").strip()
    forma = (body.get("forma") or "Caixa")
    data = (body.get("data") or _hoje())[:10]
    if valor <= 0:
        return jsonify({"ok": False, "erro": "informe o valor recebido"}), 400
    pot = POTES.get(forma, POTES["Caixa"])
    desc = "PAGAMENTO DE CONTA" + (f" — {cliente}" if cliente else "") + " (abatimento de fiado)"
    try:
        r = gcapi.post("/recebimentos", {
            "descricao": desc[:180], "valor": f"{valor:.2f}",
            "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
            "liquidado": "1", "plano_contas_id": PLANO_VENDA_BALCAO,
            "conta_bancaria_id": pot["conta"], "forma_pagamento_id": pot["forma"]})
        _invalida("resumo", "hoje", "fech_hoje", "fechamentos_7", "fechamentos_12", "fechamentos_31")
        d = r.get("data") or {}
        return jsonify({"ok": True, "valor": valor, "cliente": cliente, "forma": forma,
                        "id": d.get("id") if isinstance(d, dict) else None})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/entradas-hoje")
@login_required
def api_entradas_hoje():
    """Entradas avulsas (troca de PIX / pagamento de conta) do dia escolhido."""
    data = (request.args.get("data") or _hoje())[:10]
    def build():
        itens = []
        for r in gcapi.get_all("/recebimentos", {"data_inicio": data, "data_fim": data}):
            if not _eh_entrada_gaveta(r) or (r.get("data_liquidacao") or "")[:10] != data:
                continue
            itens.append({"id": r.get("id"), "desc": r.get("descricao"),
                          "valor": _num(r.get("valor_total")) or _num(r.get("valor"))})
        return {"data": data, "itens": itens, "total": round(sum(i["valor"] for i in itens), 2),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("entradas_" + data, 45, build))

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
        _invalida("resumo", "hoje", "pagar", "saque_resumo", "mapa")
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
            if _eh_interno(p.get("descricao")):
                continue  # recurso próprio / reserva não são conta a pagar da loja
            cat = _categoria_conta(p)
            if not cat:
                continue
            # entra TUDO que está em aberto, inclusive vencimento longe (o dono quer
            # enxergar tudo que já sabe que deve); o front separa por etiqueta.
            # "estimativa" = provisão velha que EU criei — fica visível mas NÃO soma
            # no total (o total é só o que o dono confirmou).
            venc = (p.get("data_vencimento") or "")[:10]
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            abertos[cat].append({"id": p.get("id"), "nota": _nota_conta(p),
                                 "venc": venc, "valor": val,
                                 "atrasado": bool(venc and venc < hoje),
                                 "futuro": bool(venc and venc > fim_mes),
                                 "estimativa": "[PROVISAO]" in (p.get("descricao") or "").upper()})
        categorias = []
        for lbl, _ in CATEGORIAS_PREV:
            its = sorted(abertos[lbl], key=lambda x: x["venc"] or "9999")
            reais = [i for i in its if not i["estimativa"]]
            categorias.append({"cat": lbl, "n": len(reais),
                               "aberto": round(sum(i["valor"] for i in reais), 2),
                               "atrasado": any(i["atrasado"] for i in reais),
                               "futuro": round(sum(i["valor"] for i in reais if i["futuro"]), 2),
                               "estimativa": round(sum(i["valor"] for i in its if i["estimativa"]), 2),
                               "n_estimativa": sum(1 for i in its if i["estimativa"]),
                               "itens": its})
        todos = [i for c in categorias for i in c["itens"] if not i["estimativa"]]
        atrasado = round(sum(i["valor"] for i in todos if i["atrasado"]), 2)
        futuro = round(sum(i["valor"] for i in todos if i["futuro"]), 2)
        # "deste mês" = o que ainda vence até o fim do mês (fora o atrasado)
        do_mes = round(sum(i["valor"] for i in todos
                           if not i["atrasado"] and not i["futuro"]), 2)
        return {"categorias": categorias,
                "total_aberto": round(sum(c["aberto"] for c in categorias), 2),
                "total_atrasado": atrasado,
                "total_mes": do_mes,
                "total_futuro": futuro,
                "total_estimativa": round(sum(c["estimativa"] for c in categorias), 2),
                "mes": hoje[:7], "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("previsoes", 60, build))

# ---- MAPA MENSAL (aba Contas) ----
# Uma tabela só com TUDO que falta pagar: cada categoria numa linha, os meses até
# dezembro nas colunas. A mercadoria (nota de compra / boleto do DDA), que antes
# ficava num quadro separado, virou a categoria "Mercadoria (notas)" aqui dentro.
MERCADORIA_CAT = "Mercadoria (notas)"
MES_CURTO = ["jan", "fev", "mar", "abr", "mai", "jun", "jul",
             "ago", "set", "out", "nov", "dez"]

@app.route("/api/mapa")
@login_required
def api_mapa():
    """Mapa do que está em aberto: linhas = categorias (+ mercadoria), colunas =
    atrasado + cada mês até dezembro. Cada linha traz seus itens pra abrir e pagar."""
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        hoje = _hoje()
        ano, m0 = int(hoje[:4]), int(hoje[5:7])
        meses = [f"{ano:04d}-{m:02d}" for m in range(m0, 13)]
        cats = [lbl for lbl, _ in CATEGORIAS_PREV] + [MERCADORIA_CAT]
        def nova():
            return {"atrasado": 0.0, "depois": 0.0, "n": 0,
                    "mes": {m: 0.0 for m in meses}, "itens": []}
        linhas = {c: nova() for c in cats}
        for p in pgs:
            if str(p.get("liquidado")) == "1":
                continue                              # pago já saiu do mapa
            desc = p.get("descricao") or ""
            if _eh_interno(desc):
                continue                              # recurso próprio / reserva
            if "[PROVISAO]" in desc.upper():
                continue                              # estimativa velha minha, não é conta lançada
            cat = _categoria_conta(p) or MERCADORIA_CAT
            if cat not in linhas:
                linhas[cat] = nova()
                cats.append(cat)
            venc = (p.get("data_vencimento") or "")[:10]
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            atrasado = bool(venc and venc < hoje)
            if atrasado:
                col = "atrasado"
            elif not venc or venc[:7] in meses:
                col = venc[:7] if venc else meses[0]  # sem data cai no mês corrente
            else:
                col = "depois"                        # vence depois de dezembro
            L = linhas[cat]
            L["n"] += 1
            if col in ("atrasado", "depois"):
                L[col] += val
            else:
                L["mes"][col] += val
            L["itens"].append({"id": p.get("id"),
                               "nota": _nota_conta(p) or p.get("nome_plano_conta") or "—",
                               "fornecedor": p.get("nome_fornecedor") or "",
                               "venc": venc, "valor": val,
                               "atrasado": atrasado, "col": col})
        saida, tot_mes = [], {m: 0.0 for m in meses}
        tot_atr = tot_dep = 0.0
        for c in cats:
            L = linhas[c]
            total = L["atrasado"] + L["depois"] + sum(L["mes"].values())
            tot_atr += L["atrasado"]
            tot_dep += L["depois"]
            for m in meses:
                tot_mes[m] += L["mes"][m]
            L["itens"].sort(key=lambda x: x["venc"] or "9999")
            saida.append({"cat": c, "n": L["n"],
                          "atrasado": round(L["atrasado"], 2),
                          "depois": round(L["depois"], 2),
                          "mes": {m: round(v, 2) for m, v in L["mes"].items()},
                          "total": round(total, 2), "itens": L["itens"]})
        # quem tem conta em aberto sobe (maior primeiro); as categorias zeradas
        # ficam no fim, na ordem de sempre, só pra lembrar que existem.
        ordem = {c: i for i, c in enumerate(cats)}
        saida.sort(key=lambda L: (0, -L["total"]) if L["n"] else (1, ordem[L["cat"]]))
        return {"meses": [{"key": m, "label": MES_CURTO[int(m[5:7]) - 1],
                           "atual": m == hoje[:7]} for m in meses],
                "linhas": saida,
                "cats_form": [lbl for lbl, _ in CATEGORIAS_PREV],
                "tot_atrasado": round(tot_atr, 2),
                "tot_depois": round(tot_dep, 2),
                "tot_mes": {m: round(v, 2) for m, v in tot_mes.items()},
                "tot_mes_atual": round(tot_mes.get(hoje[:7], 0.0), 2),
                "total": round(tot_atr + tot_dep + sum(tot_mes.values()), 2),
                "hoje": hoje, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("mapa", 60, build))

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
        _invalida("previsoes", "pagar", "mapa")
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
        _invalida("pagar", "previsoes", "resumo", "gastos_mes", "recurso_proprio", "reserva", "sobra", "mapa")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/recurso-proprio", methods=["GET", "POST"])
@login_required
def api_recurso_proprio():
    """GET: pagamentos com recurso próprio (dinheiro do Victor) do mês — separados,
    fora do caixa e do total da loja. POST: lança um novo (guarda tag [RP], aberto)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        valor = _num(body.get("valor"))
        data = (body.get("data") or _hoje())[:10]
        nota = (body.get("descricao") or "").strip()
        cat = body.get("categoria") or "Outros"
        if valor <= 0:
            return jsonify({"ok": False, "erro": "valor inválido"}), 400
        plano = CATS.get(cat, CATS["Outros"])
        desc = f"{RP_TAG} " + (nota or cat)
        payload = {"descricao": desc, "valor": f"{valor:.2f}",
                   "data_vencimento": data, "data_competencia": data,
                   "liquidado": "0", "plano_contas_id": plano,
                   "forma_pagamento_id": BOLETO_FORMA_ID}
        try:
            r = gcapi.post("/pagamentos", payload)
            d = r.get("data") or {}
            _invalida("recurso_proprio", "pagar", "previsoes", "mapa")
            return jsonify({"ok": True, "id": d.get("id") if isinstance(d, dict) else None})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502

    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        mes = _hoje()[:7]
        itens = []
        for p in pgs:
            desc = (p.get("descricao") or "")
            if not desc.startswith(RP_TAG):
                continue
            comp = (p.get("data_competencia") or p.get("data_vencimento") or "")[:10]
            if comp[:7] != mes:
                continue
            itens.append({"id": p.get("id"), "desc": _rp_nota(desc) or "—", "data": comp,
                          "valor": _num(p.get("valor_total")) or _num(p.get("valor"))})
        itens.sort(key=lambda x: x["data"] or "9999")
        return {"itens": itens, "total": round(sum(i["valor"] for i in itens), 2),
                "n": len(itens), "mes": mes, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("recurso_proprio", 60, build))

@app.route("/api/reserva", methods=["GET", "POST"])
@login_required
def api_reserva():
    """GET: saldo do Dinheiro Reserva (sobras de caixa) = depósitos − saídas + movimentos.
    POST: guarda um valor na reserva (depósito [RES+]). Tudo só no painel (liquidado=0)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        valor = _num(body.get("valor"))
        data = (body.get("data") or _hoje())[:10]
        nota = (body.get("descricao") or "").strip()
        if valor <= 0:
            return jsonify({"ok": False, "erro": "valor inválido"}), 400
        desc = f"{RES_DEP_TAG} guardei" + (f" — {nota}" if nota else "")
        origem = _origem_dinheiro(body)
        rot = f"RESERVA — guardei no cofre{(' (' + nota + ')') if nota else ''}"
        try:
            gcapi.post("/pagamentos", {"descricao": desc, "valor": f"{valor:.2f}",
                "data_vencimento": data, "data_competencia": data, "liquidado": "0",
                "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
            if origem == "caixa":     # saiu da gaveta: sangria pra o fechamento bater
                _sangria_caixa(valor, rot, data)
                _invalida("hoje", "fech_hoje", "fech_" + data, "fechamentos_7",
                          "fechamentos_12", "fechamentos_31", "gastos_mes")
            elif origem == "banco":   # saque no banco: sai da conta, vira dinheiro guardado
                _saque_banco(valor, f"BANCO — saquei pra {rot.split('—')[0].strip().lower()}"
                                    f"{(' (' + nota + ')') if nota else ''}", data)
                _invalida("gastos_mes")
            _invalida("reserva", "pagar", "previsoes", "mapa")
            return jsonify({"ok": True, "origem": origem, "sangria": origem == "caixa"})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502

    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        dep = out = 0.0
        movs = []
        for p in pgs:
            desc = (p.get("descricao") or "")
            v = _num(p.get("valor_total")) or _num(p.get("valor"))
            data = (p.get("data_competencia") or p.get("data_vencimento") or "")[:10]
            if desc.startswith(RES_DEP_TAG):
                dep += v
                movs.append({"id": p.get("id"), "data": data, "tipo": "guardei",
                             "desc": _sem_tag(desc, RES_DEP_TAG) or "guardei", "valor": v})
            elif desc.startswith(RES_OUT_TAG):
                out += v
                movs.append({"id": p.get("id"), "data": data, "tipo": "gastei",
                             "desc": _sem_tag(desc, RES_OUT_TAG) or "gasto", "valor": -v})
        movs.sort(key=lambda x: x["data"] or "", reverse=True)
        return {"saldo": round(dep - out, 2), "guardado": round(dep, 2),
                "gasto": round(out, 2), "movimentos": movs[:40],
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("reserva", 45, build))

@app.route("/api/sobra", methods=["GET", "POST"])
@login_required
def api_sobra():
    """GET: saldo da Sobra de Caixa (o outro monte de dinheiro, fora da reserva).
    POST: guarda um valor na sobra ([SOB+]). Tudo só no painel (liquidado=0)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        data = (body.get("data") or _hoje())[:10]
        nota = (body.get("descricao") or "").strip()
        # "ajustar_para": o dono contou o dinheiro e o saldo tem que FICAR nesse valor.
        # Gravo só a diferença (pra mais ou pra menos) e o histórico continua inteiro.
        if body.get("ajustar_para") is not None:
            alvo = _num(body.get("ajustar_para"))
            if alvo < 0:
                return jsonify({"ok": False, "erro": "valor inválido"}), 400
            atual = _sobra_saldo()
            delta = round(alvo - atual, 2)
            if abs(delta) < 0.01:
                return jsonify({"ok": True, "saldo": atual, "delta": 0.0})
            tag = SOB_DEP_TAG if delta > 0 else SOB_OUT_TAG
            rot = "contagem" + (f" — {nota}" if nota else "")
            try:
                gcapi.post("/pagamentos", {"descricao": f"{tag} {rot}"[:180],
                    "valor": f"{abs(delta):.2f}", "data_vencimento": data,
                    "data_competencia": data, "liquidado": "0",
                    "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
                _invalida("sobra", "pagar", "previsoes", "mapa")
                return jsonify({"ok": True, "saldo": alvo, "delta": delta})
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)[:200]}), 502
        valor = _num(body.get("valor"))
        if valor <= 0:
            return jsonify({"ok": False, "erro": "valor inválido"}), 400
        desc = f"{SOB_DEP_TAG} guardei" + (f" — {nota}" if nota else "")
        origem = _origem_dinheiro(body)
        rot = f"SOBRA — guardei fora da gaveta{(' (' + nota + ')') if nota else ''}"
        try:
            gcapi.post("/pagamentos", {"descricao": desc, "valor": f"{valor:.2f}",
                "data_vencimento": data, "data_competencia": data, "liquidado": "0",
                "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
            if origem == "caixa":     # saiu da gaveta hoje: sangria pra o fechamento bater
                _sangria_caixa(valor, rot, data)
                _invalida("hoje", "fech_hoje", "fech_" + data, "fechamentos_7",
                          "fechamentos_12", "fechamentos_31", "gastos_mes")
            elif origem == "banco":   # saque no banco: sai da conta e vira dinheiro guardado
                _saque_banco(valor, f"BANCO — saquei pra sobra de caixa"
                                    f"{(' (' + nota + ')') if nota else ''}", data)
                _invalida("gastos_mes")
            _invalida("sobra", "pagar", "previsoes", "mapa")
            return jsonify({"ok": True, "origem": origem, "sangria": origem == "caixa"})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502

    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
        dep = out = 0.0
        movs = []
        for p in pgs:
            desc = (p.get("descricao") or "")
            v = _num(p.get("valor_total")) or _num(p.get("valor"))
            data = (p.get("data_competencia") or p.get("data_vencimento") or "")[:10]
            if desc.startswith(SOB_DEP_TAG):
                dep += v
                movs.append({"id": p.get("id"), "data": data, "tipo": "guardei",
                             "desc": _sem_tag(desc, SOB_DEP_TAG) or "guardei", "valor": v})
            elif desc.startswith(SOB_OUT_TAG):
                out += v
                movs.append({"id": p.get("id"), "data": data, "tipo": "gastei",
                             "desc": _sem_tag(desc, SOB_OUT_TAG) or "gasto", "valor": -v})
        movs.sort(key=lambda x: x["data"] or "", reverse=True)
        return {"saldo": round(dep - out, 2), "guardado": round(dep, 2),
                "gasto": round(out, 2), "movimentos": movs[:40],
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("sobra", 45, build))


# ---- ROBÔ DO DELIVERY (Anota AI) ----
# Antes isso rodava só no MacBook do dono (launchd de 30 em 30 min): quando o Mac dormia,
# o robô parava e o pedido sumia da lista do Anota antes de virar venda. Agora roda aqui
# dentro: de 10 em 10 min e também toda vez que o painel é aberto (se já passou o
# intervalo). O dedupe é pelo próprio GestãoClick, então rodar nos dois lugares ao mesmo
# tempo não duplica pedido.
try:
    import anota
except Exception:                      # sem credencial do Anota o painel continua de pé
    anota = None

SYNC_INTERVALO = 600                   # 10 min
_sync = {"ultimo": None, "em": 0.0, "rodando": False}
_sync_lock = threading.Lock()


def _sync_anota(forcar=False):
    """Puxa os pedidos novos. Respeita o intervalo (o painel chama a cada abertura)."""
    if anota is None:
        return {"ok": False, "novos": [], "pendentes": [], "erros":
                [{"ref": "-", "erro": "robô do delivery não carregou (falta credencial)"}]}
    with _sync_lock:
        if _sync["rodando"]:
            return _sync["ultimo"] or {"ok": True, "novos": [], "rodando": True}
        if not forcar and _sync["em"] and time.time() - _sync["em"] < SYNC_INTERVALO:
            return _sync["ultimo"]
        _sync["rodando"] = True
    try:
        r = anota.rodar(post=True)
    except Exception as e:
        r = {"ok": False, "quando": time.strftime("%d/%m/%Y %H:%M"), "novos": [],
             "pendentes": [], "erros": [{"ref": "-", "erro": str(e)[:200]}]}
    finally:
        _sync["rodando"] = False
    _sync["ultimo"], _sync["em"] = r, time.time()
    if r.get("novos"):                 # venda nova muda faturamento, caixa e delivery
        _invalida("hoje_deliv", "delivery", "hoje", "resumo", "fech_hoje",
                  "fechamentos_7", "fechamentos_12", "fechamentos_31")
    return r


def _sync_loop():
    time.sleep(20)                     # deixa o serviço subir antes da primeira busca
    while True:
        try:
            _sync_anota()
        except Exception:
            pass
        time.sleep(SYNC_INTERVALO)


@app.route("/api/sync-anota", methods=["POST"])
@login_required
def api_sync_anota():
    """Botão 'puxar pedidos agora' do painel."""
    return jsonify(_sync_anota(forcar=True) or {"ok": True, "novos": []})


@app.route("/api/sync-status")
@login_required
def api_sync_status():
    u = _sync["ultimo"] or {}
    return jsonify({"quando": u.get("quando"), "novos": len(u.get("novos") or []),
                    "ultimos": (u.get("novos") or [])[-5:],
                    "pendentes": u.get("pendentes") or [], "erros": u.get("erros") or [],
                    "na_lista": u.get("na_lista"), "rodando": _sync["rodando"],
                    "faz_min": round((time.time() - _sync["em"]) / 60) if _sync["em"] else None})


if anota is not None and os.environ.get("SYNC_ANOTA", "1") == "1":
    threading.Thread(target=_sync_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
