#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App web da Distribuidora da Praça — painel ao vivo + gravação direta no GestãoClick,
protegido por senha. (Fase 1: login + leitura ao vivo.)"""
import os, functools, time, re, calendar, datetime, threading
from collections import defaultdict
from flask import Flask, request, session, redirect, url_for, render_template, jsonify, abort
import gg_compat as gcapi     # GestãoGPT desde 30/10/2026 (o que falta migrar ainda passa pro gclient)

# O GestãoClick venceu e saiu de uso. O que gravava nele fica desligado até migrar: baixa de garrafa,
# baixa de tabacaria e varredura de NFC-e (a série é a mesma do GestãoGPT: daria nota duplicada).
# Os gráficos seguem lendo o GC (só leitura, vendas até 29/10) até migrarem.
GC_DESLIGADO = True

# O servidor da nuvem roda em UTC: depois das 21h daqui já era "amanhã" pra ele, e o
# fechamento da noite caía no dia seguinte. Tudo no painel usa a hora de Minas.
os.environ.setdefault("TZ", "America/Sao_Paulo")
try:
    time.tzset()
except AttributeError:      # Windows não tem tzset
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PAINEL_JSON = os.path.join(BASE_DIR, "painel_data.json")

_painel_mtime = [0.0]

def _carrega_painel_data():
    """JSON dos gráficos (snapshot) que vai embutido no painel. `<` vira \\u003c
    pra não quebrar o <script> onde ele é injetado."""
    try:
        _painel_mtime[0] = os.path.getmtime(PAINEL_JSON)
        with open(PAINEL_JSON, encoding="utf-8") as f:
            return f.read().replace("<", "\\u003c")
    except (OSError, FileNotFoundError):
        return "{}"

def _painel_data_atual():
    """Relê o snapshot se o arquivo mudou. PRECISA existir: quem regenera os gráficos é a
    thread de fundo, que vive no processo PRINCIPAL do gunicorn — o processo que atende o
    navegador é outro e ficaria mostrando o número da véspera pra sempre."""
    global PAINEL_DATA
    try:
        if os.path.getmtime(PAINEL_JSON) != _painel_mtime[0]:
            PAINEL_DATA = _carrega_painel_data()
    except OSError:
        pass
    return PAINEL_DATA

PAINEL_DATA = _carrega_painel_data()

# ---- os gráficos se atualizam sozinhos, aqui na nuvem ----
# Antes o painel só recebia número novo se o Mac do dono estivesse ligado às 8h30.
# No fim de semana, com o laptop fechado, ele passava o dia mostrando o dado da véspera
# (foi assim que o domingo 06/09 apareceu com R$ 77,50 em vez de R$ 2.916,69).
# Agora, ao abrir o painel, se o snapshot não é de hoje o próprio servidor regenera
# em segundo plano — o Mac virou reforço, não mais a única fonte.
_graf = {"gerando": False, "quando": None, "erro": None}
_graf_lock = threading.Lock()

def _fetch_pages(path, params):
    """O que os gráficos pedem: GestãoGPT + o histórico do GC congelado lá."""
    if path == "/pagamentos":
        return gcapi.pagamentos_com_hist(params)
    return gcapi.get_all(path, params)

def _monta_catalogo(produtos, old_cat):
    """Lista de produtos e fornecedores das telas de estoque, inventário e compra.

    Isso vinha do `build_catalogo.py` rodando no Mac do dono e era só COPIADO adiante a
    cada regeração aqui na nuvem — quando o Mac saiu das rotinas (12/09/2026) o catálogo
    congelou: produto renomeado aparecia com o nome velho, produto novo não aparecia na
    lista, e o estoque do seletor era o de 12/09. Agora sai do GestãoClick junto com os
    gráficos, todo dia.

    O fardo não vem da API: é o [fardo=N] que o dono confirmou na descrição; se não tiver,
    reaproveito o que o catálogo antigo sabia — por ID primeiro, que sobrevive a renomear.
    """
    fardo_id = {str(p.get("id")): p.get("fardo")
                for p in (old_cat.get("produtos") or []) if p.get("fardo")}
    fardo_nome = {(p.get("nome") or "").upper(): p.get("fardo")
                  for p in (old_cat.get("produtos") or []) if p.get("fardo")}
    prods = []
    for p in produtos:
        if str(p.get("ativo")) not in ("1", "true", "True"):
            continue
        pid, nome = str(p.get("id")), p.get("nome") or ""
        f = _fardo_cadastrado(p) or fardo_id.get(pid) or fardo_nome.get(nome.upper())
        prods.append({"id": pid, "nome": nome,
                      "estoque": round(_num(p.get("estoque")), 0),
                      "custo": round(_num(p.get("valor_custo")), 3),
                      "venda": round(_num(p.get("valor_venda")), 2),
                      "grupo": p.get("nome_grupo") or "",
                      "fardo": int(f) if f else None,
                      "mov": str(p.get("movimenta_estoque")) == "1"})
    prods.sort(key=lambda x: (x["nome"] or "").upper())
    forns = []
    for f in _fetch_pages("/fornecedores", {}):
        if str(f.get("ativo")) not in ("1", "true", "True", ""):
            continue
        forns.append({"id": str(f.get("id")),
                      "nome": f.get("nome") or f.get("razao_social") or f"Fornecedor {f.get('id')}"})
    forns.sort(key=lambda x: (x["nome"] or "").upper())
    if not prods:                      # deu ruim na busca? fica com o que já havia
        return old_cat
    return {"gerado_em": time.strftime("%Y-%m-%d %H:%M"),
            "fornecedores": forns or (old_cat.get("fornecedores") or []),
            "produtos": prods}

def _regera_graficos():
    """Recalcula o pacote dos gráficos do CRM e grava no disco do servidor."""
    global PAINEL_DATA
    try:
        import graficos, json as _json
        try:
            with open(PAINEL_JSON, encoding="utf-8") as f:
                old = _json.load(f)
        except (OSError, ValueError):
            old = {}
        hoje = datetime.date.fromisoformat(_hoje())
        vendas, produtos, pagamentos = graficos.baixar(_fetch_pages, hoje)
        fardo = {(p.get("nome") or "").upper(): int(p["fardo"])
                 for p in (old.get("catalogo") or {}).get("produtos", []) if p.get("fardo")}
        novo = graficos.construir(vendas, produtos, pagamentos, old, fardo, hoje,
                                  categoria_fn=_categoria_dre, interno_fn=_eh_interno,
                                  cats_dre=_categ_dre(),
                                  saidas=gcapi.saidas_de_venda(graficos.YMD(hoje - datetime.timedelta(days=120))))
        novo["catalogo"] = _monta_catalogo(produtos, old.get("catalogo") or {})
        with open(PAINEL_JSON, "w", encoding="utf-8") as f:
            _json.dump(novo, f, ensure_ascii=False)
        PAINEL_DATA = _carrega_painel_data()
        _graf["quando"] = time.strftime("%d/%m/%Y %H:%M")
        _graf["erro"] = None
    except Exception as e:
        _graf["erro"] = str(e)[:200]
    finally:
        _graf["gerando"] = False

# ---- baixa dos maços (cigarro avulso, fumo picado e seda), aqui na nuvem ----
# Mesma história das garrafas: a porção não movimenta estoque, quem sai é o MAÇO —
# 20 porções fecham 1 maço, 33 sedas fecham 1 caixa — e só sai inteiro. Até 15/09/2026
# isso só rodava na mão no Mac (scripts/baixa_tabacaria.py), então os maços iam ficando
# no sistema depois de vendidos. O controle do que já foi baixado mora num produto
# técnico do GestãoGPT, senão o reinício da nuvem faria a baixa em dobro.
_tabaco = {"rodando": False, "quando": None, "erro": None, "ultimo": None}
_tabaco_lock = threading.Lock()

def _roda_baixa_tabacaria():
    global _tabaco
    try:
        import tabacaria
        r = tabacaria.rodar(gcapi, aplicar=True, dias=10)
        _tabaco["ultimo"] = {"vendas": r["vendas_novas"], "porcoes": r["porcoes"],
                             "macos": [{"nome": l["nome"], "baixado": l["baixado"],
                                        "antes": l["antes"], "depois": l["depois"]}
                                       for l in r["linhas"]], "ate": r["ate"]}
        _tabaco["quando"] = time.strftime("%d/%m/%Y %H:%M")
        _tabaco["erro"] = None
        if r["linhas"]:
            _invalida("resumo", "catalogo", "abc", "tabacaria_status")
    except Exception as e:
        _tabaco["erro"] = str(e)[:200]
    finally:
        _tabaco["rodando"] = False

def _baixa_tabacaria_do_dia():
    """Dispara a baixa dos maços em segundo plano, no máximo de hora em hora."""
    agora = time.time()
    with _tabaco_lock:
        if _tabaco["rodando"]:
            return
        if _tabaco.get("_t") and agora - _tabaco["_t"] < 3600:
            return
        _tabaco["_t"] = agora
        _tabaco["rodando"] = True
    threading.Thread(target=_roda_baixa_tabacaria, daemon=True).start()

# ---- notas fiscais do delivery (era a rotina das 9h no Mac) ----
# O robô do delivery já emite a NFC-e na hora que importa o pedido. Quando essa emissão
# falha (API fora do ar, SEFAZ recusando, serviço reiniciando no meio), a venda fica sem
# nota e ninguém percebe. Esta varredura roda uma vez por dia e pega o que sobrou.
_notas = {"rodando": False, "quando": None, "erro": None, "ultimo": None, "dia": None}
_notas_lock = threading.Lock()

def _roda_notas_delivery(dias=7):
    if GC_DESLIGADO:
        return
    global _notas
    try:
        import notas_delivery
        r = notas_delivery.rodar(gcapi, dias=dias)
        _notas["ultimo"] = {"vendas": r["vendas"], "de": r["de"], "ate": r["ate"],
                            "emitidas": r["emitidas"], "esperando": r["esperando"],
                            "erros": r["erros"]}
        _notas["quando"] = r["quando"]
        _notas["erro"] = None
    except Exception as e:
        _notas["erro"] = str(e)[:200]
    finally:
        _notas["rodando"] = False

def _notas_do_dia(forcar=False):
    """Dispara a varredura em segundo plano, uma vez por dia."""
    if GC_DESLIGADO:
        return
    hoje = _hoje()
    with _notas_lock:
        if _notas["rodando"]:
            return
        if not forcar and _notas["dia"] == hoje:
            return
        _notas["dia"] = hoje
        _notas["rodando"] = True
    threading.Thread(target=_roda_notas_delivery, daemon=True).start()

def _graficos_do_dia():
    """Dispara a regeração se o snapshot não for de hoje. Não bloqueia a página:
    quem abriu agora vê o número da véspera e o próximo refresh já vem certo."""
    try:
        import json as _json
        with open(PAINEL_JSON, encoding="utf-8") as f:
            if _json.load(f).get("gerado_em") == _hoje():
                return
    except (OSError, ValueError):
        pass
    with _graf_lock:
        if _graf["gerando"]:
            return
        _graf["gerando"] = True
    threading.Thread(target=_regera_graficos, daemon=True).start()

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
    _graficos_do_dia()      # snapshot velho? o servidor regenera sozinho, em segundo plano
    _baixa_tabacaria_do_dia()  # maços do cigarro avulso/picado/seda: mesma ideia
    _pluggy_do_dia()        # extrato do Itaú e do Nubank pelo Open Finance: concilia sozinho
    with open(os.path.join(BASE_DIR, "templates", "painel.html"), encoding="utf-8") as f:
        tpl = f.read()
    return tpl.replace("__DATA__", _painel_data_atual())

@app.route("/api/drinks-status")
@login_required
def api_drinks_status():
    """Quantos copões, doses e combos já baixam a garrafa sozinhos, pela receita do GestãoGPT."""
    return jsonify(cached("drinks_status", 300, gcapi.receitas_status))

@app.route("/api/tabacaria-status")
@login_required
def api_tabacaria_status():
    """Como está a baixa dos maços: porções paradas, última rodada e o que baixou."""
    def build():
        import tabacaria
        estado, pid = tabacaria.carrega_estado(gcapi)
        return {"ate": estado.get("ate"), "controle_id": pid,
                "saldo": tabacaria.saldo_legivel(gcapi, estado.get("saldo") or {}),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    d = cached("tabacaria_status", 120, build)
    return jsonify(dict(d, rodando=_tabaco["rodando"], erro=_tabaco["erro"],
                        ultima_rodada=_tabaco["quando"], ultimo=_tabaco["ultimo"]))

@app.route("/api/baixa-tabacaria", methods=["POST"])
@login_required
def api_baixa_tabacaria():
    """Roda a baixa dos maços agora (botão do painel)."""
    with _tabaco_lock:
        if _tabaco["rodando"]:
            return jsonify({"ok": True, "ja_rodando": True})
        _tabaco["_t"] = time.time()
        _tabaco["rodando"] = True
    threading.Thread(target=_roda_baixa_tabacaria, daemon=True).start()
    _invalida("tabacaria_status")
    return jsonify({"ok": True, "ja_rodando": False})

@app.route("/api/graficos-status")
@login_required
def api_graficos_status():
    """Diz se os gráficos já são de hoje e se a regeração na nuvem está rodando."""
    try:
        import json as _json
        with open(PAINEL_JSON, encoding="utf-8") as f:
            gerado = _json.load(f).get("gerado_em")
    except (OSError, ValueError):
        gerado = None
    return jsonify({"gerado_em": gerado, "de_hoje": gerado == _hoje(),
                    "gerando": _graf["gerando"], "ultima_regeracao": _graf["quando"],
                    "erro": _graf["erro"], "hoje": _hoje()})

@app.route("/api/regerar-graficos", methods=["POST"])
@login_required
def api_regerar_graficos():
    """Força o recálculo dos gráficos agora (botão Atualizar / pedido meu)."""
    with _graf_lock:
        if _graf["gerando"]:
            return jsonify({"ok": True, "ja_rodando": True})
        _graf["gerando"] = True
    threading.Thread(target=_regera_graficos, daemon=True).start()
    return jsonify({"ok": True, "ja_rodando": False})

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
        # sem data o GestãoClick devolve só o mês corrente — boleto que vence mês que vem
        # (ou atrasado do mês passado) sumia daqui
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
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
            if _eh_interno(p.get("descricao")):     # registro só do painel (saldo, extrato)
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
# O PIX sai (e entra) numa das DUAS contas da loja: Itaú (a principal) ou Nubank. No
# GestãoClick é uma conta bancária só, então o banco vai escrito no lançamento ("PIX Itaú").
# O dono pediu em 20/10/2026: perguntar SEMPRE em qual conta foi, sem deixar marcada.
BANCOS = ["Itaú", "Nubank"]
for _b in BANCOS:
    POTES[f"PIX {_b}"] = POTES["PIX"]

def _banco_da_forma(f):
    f = f or ""
    return f[4:] if f.startswith("PIX ") and f[4:] in BANCOS else None

def _com_banco(texto, f):
    """Escreve no lançamento em qual banco o PIX passou (se ainda não estiver escrito)."""
    b = _banco_da_forma(f)
    if not b or _banco_do_texto(texto) == b:
        return texto
    return f"{texto} (PIX {b})"

def _banco_do_texto(desc):
    u = _sem_acento_up(desc)
    for b in BANCOS:
        bu = _sem_acento_up(b)
        if f"PIX {bu}" in u or f"EXTRATO {bu}" in u:
            return b
    return None

def _sem_acento_up(s):
    import unicodedata
    return unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().upper()

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

# ---- SALDO DOS BANCOS (Itaú e Nubank) ----
# O GestãoClick não sabe quanto tem de verdade em cada banco. O dono informa (ou vem do
# extrato que ele sobe na central) e eu guardo um registro só do painel, igual o da reserva
# (liquidado=0, tag no começo). O valor fica escrito na descrição porque pode ser negativo.
SALDO_TAG = "[SALDO]"
_SALDO_RE = re.compile(r"^\[SALDO\] (\S+) = (-?[\d.]+) \(([^)]*)\)")

# ---- NÃO IDENTIFICADO (gasto do banco que ninguém lançou) ----
# Pedido do dono em 28/10/2026: o extrato concilia SOZINHO. O que saiu do banco e não casa
# com nada (nota lançada, compra sem nota, gasto ou conta) fica guardado aqui até ele dizer
# o que foi, na tela "Não identificado". Mesmo jeito do [SALDO]: registro só do painel,
# valor 0,01 no GC e a linha do banco escrita na descrição. [NIX] = ele disse que não é
# gasto (ou que já estava lançado): fica guardado pra o extrato não trazer de volta.
NI_TAG = "[NI]"
NIX_TAG = "[NIX]"
_NI_RE = re.compile(r"^\[(NIX?)\] (\S+) (\d{4}-\d{2}-\d{2}) (\d+\.\d{2}) #ext:([0-9a-f]{8}) · (.*)$", re.S)
NI_MOTIVO_SEP = " ‖ "

# ---- FREEZER DE GELO CONSIGNADO ----
# O dono paga um valor fixo por mês pelo freezer e esse valor vira CRÉDITO em gelo: cada
# saco que chega é abatido do crédito e entra no estoque. O crédito zera todo mês.
#  · pagamento do mês = conta paga normal com a marca na descrição. Começa com
#    "Compra de": é mercadoria (soma em Compras e entra no DRE como mercadoria).
#  · cada entrega = registro [GELO-] só do painel (liquidado=0), igual ao [RES-]: não é
#    conta a pagar, porque aquele gelo já foi pago pelo crédito.
GELO_CRED_MARCA = "FREEZER CONSIGNADO"
GELO_USO_TAG = "[GELO-]"
GELO_MENSAL = 300.00
GELO_PLANO = "33015669"      # plano "Compras" (mercadoria)
_GELO_SACO = re.compile(r"^GELO\s+\d+\s*KG", re.I)   # gelo em saco: 4, 10, 20 kg
_GELO_USO_RE = re.compile(r"^\[GELO-\] ([\d.]+) x (.+?) · R\$ ([\d.]+) cada · #(\d+)")

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
            or d.startswith(SOB_DEP_TAG) or d.startswith(SOB_OUT_TAG)
            or d.startswith(GELO_USO_TAG) or d.startswith(SALDO_TAG)
            or d.startswith(NI_TAG) or d.startswith(NIX_TAG))

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
    (saque), 'sobra'/'reserva' (veio do outro monte guardado — transferência) ou
    'nenhum' (já estava fora). do_caixa é o formato antigo da tela."""
    o = (body.get("origem") or "").strip().lower()
    if o in ("caixa", "banco", "sobra", "reserva", "nenhum"):
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
# conta paga do bolso do Victor ganha "[RP] " NA FRENTE da etiqueta (ver _marca_rp): sem
# ler essa forma, o almoço pago do bolso caía em "Gastos adicionais" e fora do teto
CAT_TAG_RP_RE = re.compile(r"^(\[RP\]\s*)\[cat:([^\]]+)\]\s*")

def _cat_marcada(desc):
    """Categoria da etiqueta, pela CHAVE fixa: o [cat:nome velho] de antes de o dono
    renomear cai na mesma categoria do nome novo (ver Minhas categorias)."""
    m = CAT_TAG_RE.match(desc or "")
    if m:
        return _cat_chave(m.group(1).strip())
    m = CAT_TAG_RP_RE.match(desc or "")
    return _cat_chave(m.group(2).strip()) if m else None

def _sem_cat(desc):
    return CAT_TAG_RP_RE.sub(r"\1", CAT_TAG_RE.sub("", desc or "")).strip()

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
    """Categoria (pela chave) de uma conta a pagar, ou None se não for previsão/recorrente."""
    c = _categoria_conta_texto(p)
    return _cat_chave(c) if c else c

def _categoria_dre(p):
    """Categoria pro quadro de lucros. Igual à da aba Contas, só que "Outros" escolhido
    pelo dono vai pra linha Outros: lá ele cai junto da mercadoria, e no quadro entrava
    como custo de mercadoria (pelo plano Compras)."""
    return _cat_marcada(p.get("descricao")) or _categoria_conta(p)

def _categoria_conta_texto(p):
    desc = p.get("descricao") or ""
    marcada = _cat_marcada(desc)   # categoria escolhida no formulário manda
    if marcada:
        return None if marcada == "Outros" else marcada
    c = _prev_categoria(desc)      # itens [PREV] que o dono adiciona
    if c:
        return c
    du = desc.upper()
    if GELO_CRED_MARCA in du:
        return None                # crédito do freezer de gelo = mercadoria, não custo fixo
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
    # #ext:xxxxxxxx = marca da linha do extrato do banco (só serve pra não lançar duas vezes)
    return re.sub(r"\[provisao\]|\[PREV\]|\s*#ext:[0-9a-f]{8}", "", _sem_cat(desc), flags=re.I).strip()

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

def _pagamento(pid):
    """Um pagamento do GestãoClick pelo id, com todos os campos (o PUT quer o corpo inteiro)."""
    cur = gcapi.get(f"/pagamentos/{pid}")
    p = cur.get("data") or cur
    if isinstance(p, list):
        p = p[0] if p else {}
    return p.get("Pagamento", p) if isinstance(p, dict) else {}

def _payload_baixa(p, forma, valor_real, dia):
    """Corpo do PUT que marca a conta como paga: mantém os campos dela e troca só o que
    o pagamento muda (pago, dia, conta e forma, e o valor se pagou diferente)."""
    pot = POTES[forma]
    return {
        "descricao": _com_banco(_marca_rp(p.get("descricao") or "Conta", forma), forma)[:180],
        "valor": f"{valor_real:.2f}" if valor_real > 0 else (p.get("valor") or p.get("valor_total") or "0"),
        "plano_contas_id": p.get("plano_contas_id") or "",
        "data_vencimento": (p.get("data_vencimento") or _hoje())[:10],
        "data_competencia": (p.get("data_competencia") or _hoje())[:10],
        "fornecedor_id": p.get("fornecedor_id") or "",
        "liquidado": "1",
        "data_liquidacao": dia,
        "conta_bancaria_id": pot["conta"],
        "forma_pagamento_id": pot["forma"],
    }

@app.route("/api/baixa", methods=["POST"])
@login_required
def api_baixa():
    """Dá baixa (liquida) uma conta a pagar existente."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("id") or "").strip()
    # dia em que saiu o dinheiro: vem do extrato do banco (a conta foi paga lá atrás);
    # pagando pelo mapa é hoje mesmo
    dia = (body.get("data") or _hoje())[:10]
    forma = body.get("forma") or ""
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
    if forma not in POTES or any((x.get("forma") or "") not in POTES for x in partes):
        return jsonify({"ok": False, "erro": "escolha de onde saiu o dinheiro"}), 400
    # preserva os campos da conta e só marca como paga
    p = _pagamento(pid)
    payload = _payload_baixa(p, forma, valor_real, dia)
    try:
        gcapi.put(f"/pagamentos/{pid}", payload)
        if forma == "Dinheiro":
            _reserva_saida(payload["valor"], payload["descricao"] or "Conta", dia)
        elif forma == "Sobra":
            _sobra_saida(payload["valor"], payload["descricao"] or "Conta", dia)
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
                "liquidado": "1", "data_liquidacao": dia,
                "conta_bancaria_id": pot2["conta"], "forma_pagamento_id": pot2["forma"]})
            if f2 == "Dinheiro":
                _reserva_saida(v2, desc2, dia)
            elif f2 == "Sobra":
                _sobra_saida(v2, desc2, dia)
        _invalida("pagar", "resumo", "previsoes", "gastos_mes", "reserva", "sobra", "mapa",
                  "lancamentos_" + _hoje()[:7])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/gasto", methods=["POST"])
@login_required
def api_gasto():
    """Lança um gasto (despesa) já paga, na categoria e no(s) pote(s) certo(s).

    O dono às vezes paga um gasto só com DUAS fontes (ex.: R$ 100 = R$ 60 no PIX +
    R$ 40 da gaveta). Nesse caso vem `partes` e cada pedaço vira um lançamento, pra
    cada pote ser descontado do jeito certo (e o fechamento não errar a gaveta).
    """
    body = request.get_json(force=True, silent=True) or {}
    cat = body.get("categoria") or "Outros"
    cat = _cat_nome(cat)      # a etiqueta leva o nome de hoje (tela velha manda o antigo)
    valor = _num(body.get("valor"))
    forma = body.get("forma") or ""
    data = (body.get("data") or _hoje())[:10]
    nota = (body.get("descricao") or "").strip() or cat
    quem = (body.get("quem") or "").strip()
    if quem and quem.upper() not in nota.upper():
        nota = f"{nota} — pago a {quem}"
    if valor <= 0:
        return jsonify({"ok": False, "erro": "valor inválido"}), 400
    # mercadoria achada no extrato e lançada só pelo valor (sem itens): começa com
    # "Compra de" e vai no plano Compras, sem etiqueta de categoria — é assim que o mês e
    # o quadro de lucros reconhecem mercadoria (ver _eh_mercadoria).
    mercadoria = bool(body.get("mercadoria"))
    plano = CATS["Outros"] if mercadoria else _plano_cat(cat)

    partes = [x for x in (body.get("partes") or []) if _num(x.get("valor")) > 0]
    if len(partes) > 3:
        return jsonify({"ok": False, "erro": "no máximo 3 formas"}), 400
    if partes:
        soma = round(sum(_num(x.get("valor")) for x in partes), 2)
        if abs(soma - round(valor, 2)) > 0.02:
            return jsonify({"ok": False, "erro": f"as formas somam R$ {soma:.2f} e o gasto é "
                                                 f"R$ {valor:.2f} — ajuste os valores"}), 400
        if any((x.get("forma") or "") not in POTES and x.get("forma") != "Boleto" for x in partes):
            return jsonify({"ok": False, "erro": "escolha de onde saiu cada parte"}), 400
    else:
        # o dono pediu: a forma NUNCA vem marcada sozinha. Sem forma, não grava — antes
        # o padrão era "Caixa" e um gasto do PIX tirava dinheiro da gaveta sem ninguém ver.
        if forma not in POTES and forma != "Boleto":
            return jsonify({"ok": False, "erro": "escolha de onde saiu o dinheiro"}), 400
        partes = [{"forma": forma, "valor": valor}]

    # conta que AINDA NÃO FOI PAGA (Boleto) pode repetir (mesmo valor toda semana/mês) ou
    # ser parcelada (o valor dividido). Uma conta a pagar por vencimento, igual o dono
    # lançaria uma por uma. Só quando ele escolhe — nada é pré-carregado sozinho.
    modo = (body.get("modo") or "uma").strip().lower()
    vezes = int(_num(body.get("vezes")) or 1)
    freq = (body.get("freq") or "mensal").strip().lower()
    serie = None
    if modo in ("repete", "parcela") and vezes > 1:
        if len(partes) != 1 or partes[0]["forma"] != "Boleto":
            return jsonify({"ok": False, "erro": "repetir e parcelar é só pra conta que "
                                                 "ainda não foi paga"}), 400
        if vezes > (52 if freq == "semanal" else 24):
            return jsonify({"ok": False, "erro": "vezes demais — no máximo 24 meses ou 52 semanas"}), 400
        serie = _serie_vencimentos(data, vezes, freq if modo == "repete" else "mensal")
        if modo == "parcela":
            base = int(round(valor * 100)) // vezes / 100
            vals = [base] * (vezes - 1) + [round(valor - base * (vezes - 1), 2)]
            rot = [f"parcela {i}/{vezes}" for i in range(1, vezes + 1)]
        else:
            vals = [round(valor, 2)] * vezes
            rot = [f"{i} de {vezes}" for i in range(1, vezes + 1)]
        serie = list(zip(serie, vals, rot))

    def _corpo(f, v, texto, dt=None):
        dt = dt or data
        d = texto if mercadoria else f"[cat:{cat}] {texto}"
        d = _marca_rp(d, f)
        if f == "Boleto":      # não pago: vira conta a pagar (boleto em aberto)
            return {"descricao": d[:180], "valor": f"{v:.2f}", "data_vencimento": dt,
                    "data_competencia": dt, "liquidado": "0", "plano_contas_id": plano,
                    "forma_pagamento_id": BOLETO_FORMA_ID}
        pot = POTES.get(f, POTES["Caixa"])
        return {"descricao": d[:180], "valor": f"{v:.2f}", "data_vencimento": dt,
                "data_competencia": dt, "data_liquidacao": dt, "liquidado": "1",
                "plano_contas_id": plano, "conta_bancaria_id": pot["conta"],
                "forma_pagamento_id": pot["forma"]}

    if serie:
        # uma por uma, SEM repetir em caso de erro (repetir duplica no GestãoClick):
        # se cair no meio, devolvo quais já entraram pro dono não lançar de novo.
        ids, feitas = [], []
        try:
            for dt, v, r_ in serie:
                r = gcapi.post("/pagamentos", _corpo("Boleto", v, f"{nota} ({r_})", dt))
                d = r.get("data") or {}
                ids.append(d.get("id") if isinstance(d, dict) else None)
                feitas.append({"venc": dt, "valor": v})
            _invalida("pagar", "previsoes", "mapa", "ultima_atualizacao", "lancamentos_" + _hoje()[:7])
            return jsonify({"ok": True, "ids": ids, "contas": feitas})
        except Exception as e:
            _invalida("pagar", "previsoes", "mapa", "lancamentos_" + _hoje()[:7])
            return jsonify({"ok": False, "erro": str(e)[:200], "contas": feitas,
                            "parcial": True}), 502

    ids, lancados = [], []
    try:
        for parte in partes:
            f = parte.get("forma") or "Caixa"
            v = round(_num(parte.get("valor")), 2)
            texto = _com_banco(nota, f) if len(partes) == 1 else f"{nota} ({f})"
            r = gcapi.post("/pagamentos", _corpo(f, v, texto))
            d = r.get("data") or {}
            ids.append(d.get("id") if isinstance(d, dict) else None)
            lancados.append({"forma": f, "valor": v})
            if f == "Dinheiro":
                _reserva_saida(v, texto, data)
            elif f == "Sobra":
                _sobra_saida(v, texto, data)
        _invalida("pagar", "resumo", "reserva", "sobra", "gastos_mes", "mapa",
                  "hoje", "fech_hoje", "fech_" + data, "fechamentos_7",
                  "fechamentos_12", "fechamentos_31", "lancamentos_" + _hoje()[:7])
        return jsonify({"ok": True, "id": ids[0] if ids else None, "ids": ids,
                        "partes": lancados})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200], "lancados": lancados}), 502

# ---- CENTRAL DE LANÇAMENTOS (botão "+ Lançar", em todas as abas) ----
# Antes eram 12 formulários espalhados em 4 abas. Em 20/10/2026 o dono comprou a
# planilha "Meu Caixa Organizado" e pediu um lugar só pra lançar tudo, como a central
# dela: Saída · Entrada · Mover dinheiro · Conferir extrato. Os formulários antigos
# saíram das abas. As gravações continuam as mesmas rotas de sempre (gasto, baixa,
# saque, troca, fiado, reserva, sobra) — a central só junta as portas.
CENTRAL_GRUPOS = [
    ("Pessoas", ["PH Motoca", "Biel (Gabriel)", "Retirada do sócio (Victor)",
                 "Igor (pró-labore / retirada)", "Vigia", "INSS s/ pró-labore",
                 "Motoboy / entrega"]),
    ("Contas da loja", ["Aluguel (IPTU)", "Energia (CEMIG)", "Água (COPASA)",
                        "Internet / telefone", "Contabilidade (Werdeiros)", "Anota AI",
                        "Seguro do carro"]),
    ("Impostos e banco", ["DAS (Simples)", "Parcelamento Simples (PARCSN)",
                          "Taxas / alvará (PBH)", "Tarifa do banco"]),
    ("Dia a dia", ["Sacolas / gelo / copos", "Lanche", "Almoço", "Padaria", "Papelaria",
                   "Combustível", "Limpeza / higiene", "Descartáveis (copo/saco)",
                   "Manutenção / conserto", "Reparo da loja", "Outros"]),
]
CATS_EXTRA = {"Tarifa do banco": "33015677"}   # plano "Despesas bancárias"

def _plano_cat_fabrica(cat):
    return CATS.get(cat) or _PREV_PLANO.get(cat) or CATS_EXTRA.get(cat) or CATS["Outros"]

def _plano_cat(cat):
    """Plano de contas do GestãoClick de uma categoria do painel. Antes só valiam as
    categorias do gasto do dia a dia: "Energia (CEMIG)" lançada como gasto ia pro plano
    Compras. O que manda no painel continua sendo a etiqueta [cat:]."""
    it = _cat_item(cat)
    return (it and it["plano"]) or _plano_cat_fabrica(cat)

# ---- MINHAS CATEGORIAS (o dono cria, renomeia e esconde, sem depender de mim) ----
# Fase 2 da central (20/10/2026), na linha do cadastro de plano de contas da planilha
# "Meu Caixa Organizado". As categorias de fábrica são as da central (CENTRAL_GRUPOS). O
# que o dono muda fica no produto técnico inativo "CONTROLE CATEGORIAS" do GestãoClick, só
# a diferença: o disco do servidor some a cada atualização, então não pode ser arquivo.
#  · cada categoria tem uma CHAVE que nunca muda (o nome de fábrica, ou o nome com que
#    nasceu) e o NOME da tela, que ele troca quando quiser. O código continua falando a
#    chave (Igor, Victor e Biel têm regra própria no resumo); a tela mostra o nome;
#  · renomear não reescreve lançamento nenhum: o nome velho vira apelido, e o
#    [cat:nome velho] dos lançamentos antigos cai na categoria com o nome novo;
#  · categoria não se apaga (tem histórico): ela some dos formulários e fica nos quadros.
CATEG_CONTROLE = "CONTROLE CATEGORIAS"
CATEG_GRUPOS = [g for g, _ in CENTRAL_GRUPOS]
# nomes que já foram usados pra mesma coisa (lançamentos antigos e a aba Contas antiga)
CATEG_APELIDOS = {
    "Retirada do sócio (Victor)": ["Retirada Victor"],
    "Igor (pró-labore / retirada)": ["Retirada do sócio (Igor)", "Pró-labore Igor"],
    "Biel (Gabriel)": ["Pagamento Biel", "Funcionário Gabriel (FDS)"],
    "PH Motoca": ["Pagamento PH Motoca", "PH Motoca / motoboy"],
}
LINHA_PROPRIA = "*"     # no quadro de lucros, uma linha só dela, com o nome dela
# linhas do quadro de lucros que não são custo da operação
CATEG_LINHAS_ESPECIAIS = [("@impostos", "Impostos"), ("@financeiro", "Taxas de cartão e banco")]
# plano do GestãoClick de uma categoria NOVA, pela linha que o dono escolheu. O painel
# classifica pela etiqueta [cat:]; o plano só arruma os relatórios do próprio GC.
PLANO_DA_LINHA = {
    "Salários e remunerações": "33015660", "Aluguel": "33015630",
    "Material de limpeza": "33015655", "Luz": "33015649", "Água": "33015649",
    "Telefone / internet": "33015663", "Gasolina": "33015633",
    "Manutenção de equipamentos": "33015656", "Publicidade": "33015657",
    "Sistema (app do delivery)": "33015654", "Contabilidade": "33015635",
    "Motoboy / entregas": "33015664", "Sacolas / gelo / copos": "33015662",
    "Seguro do carro": "33015633", "Lanches": "33015662", "Almoço": "33015662",
    "Padaria": "33015662", "Material de escritório": "33015658",
    "@impostos": "33015650", "@financeiro": "33015677",
}
_CATEG_NOME_OK = re.compile(r"^[0-9A-Za-zÀ-ÿºª ()/.,+'\-]+$")

_categ = {"t": 0.0, "cfg": None, "pid": None, "lista": None, "idx": None, "erro": None}
_categ_lock = threading.Lock()

def _norm_cat(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.upper()).strip()

def _categ_le_gc():
    """(id do produto de controle ou None, o que está guardado nele). Erro sobe: com o
    GestãoClick fora, melhor não gravar nada do que gravar em cima do que ele fez."""
    import json as _json
    pid = _categ["pid"]          # já achado antes: vai direto (a busca por nome pode demorar
    if not pid:                  # a enxergar o produto recém-criado e eu criaria outro)
        for p in gcapi.get_all("/produtos", {"nome": CATEG_CONTROLE}):
            if (p.get("nome") or "").strip().upper() == CATEG_CONTROLE:
                pid = str(p.get("id"))
                break
    if not pid:
        return None, {}
    d = gcapi.get(f"/produtos/{pid}").get("data") or {}
    cfg = _json.loads((d.get("descricao") or "").strip() or "{}")
    return pid, (cfg if isinstance(cfg, dict) else {})

def _categ_cfg(fresh=False):
    """O que o dono mudou nas categorias, com 60 s de cache por processo. Se o GC não
    responder, segue com o último que leu (ou o de fábrica) e tenta de novo depois."""
    agora = time.time()
    if not fresh and _categ["cfg"] is not None and agora - _categ["t"] < 60:
        return _categ["cfg"]
    try:
        pid, cfg = _categ_le_gc()
    except Exception as e:
        with _categ_lock:
            _categ.update(t=agora, erro=str(e)[:200])
            if _categ["cfg"] is None:
                _categ.update(cfg={}, lista=None, idx=None)
        if fresh:
            raise
        return _categ["cfg"]
    with _categ_lock:
        if cfg != _categ["cfg"]:
            _categ.update(lista=None, idx=None)
        _categ.update(t=agora, cfg=cfg, pid=pid, erro=None)
    return cfg

def _categ_monta(cfg):
    """Todas as categorias, na ordem da central: as de fábrica (com o que ele mudou) e,
    no fim de cada grupo, as que ele criou."""
    import graficos
    mud = cfg.get("cats") or {}
    def item(chave, m, grupo, linha, plano, nova):
        g = m.get("g") if m.get("g") in CATEG_GRUPOS else grupo
        l = m.get("l") or linha
        return {"chave": chave, "nome": m.get("n") or chave, "grupo": g, "linha": l,
                "plano": plano or PLANO_DA_LINHA.get(l) or CATS["Outros"],
                "ativa": not m.get("off"), "nova": nova,
                "teto": round(_num(m.get("t")), 2) or None,     # teto do mês, em R$
                "antigos": [a for a in (m.get("a") or []) + CATEG_APELIDOS.get(chave, [])
                            if _norm_cat(a) != _norm_cat(m.get("n") or chave)]}
    fab = []
    for grupo, cats in CENTRAL_GRUPOS:
        for c in cats:
            fab.append(item(c, mud.get(c) or {}, grupo,
                            graficos.DRE_DE_PARA.get(c) or "Outros", _plano_cat_fabrica(c), False))
    novas = [item(c, m, "Dia a dia", LINHA_PROPRIA, None, True)
             for c, m in mud.items() if m.get("novo")]
    return [it for g in CATEG_GRUPOS for it in fab + novas if it["grupo"] == g]

def _categ_lista():
    cfg = _categ_cfg()
    with _categ_lock:
        if _categ["lista"] is None or _categ["idx"] is None:
            lista = _categ_monta(cfg)
            idx = {}
            for it in lista:     # nome de hoje primeiro, depois a chave e os apelidos
                for n in [it["nome"], it["chave"]] + it["antigos"]:
                    idx.setdefault(_norm_cat(n), it)
            _categ.update(lista=lista, idx=idx)
        return _categ["lista"]

def _cat_item(nome):
    """A categoria pelo nome de hoje, pelo nome de fábrica ou por um apelido."""
    if not nome:
        return None
    _categ_lista()
    return (_categ["idx"] or {}).get(_norm_cat(nome))

def _nomes_cat(nome):
    """Todos os nomes (normalizados) que a categoria já teve, ou só o próprio texto."""
    if not nome:
        return set()
    it = _cat_item(nome)
    if not it:
        return {_norm_cat(nome)}
    return {_norm_cat(x) for x in [it["nome"], it["chave"]] + it["antigos"]}

def _cat_chave(nome):
    """Chave fixa da categoria. Nome que não é de categoria nenhuma volta como veio."""
    it = _cat_item(nome)
    return it["chave"] if it else nome

def _cat_nome(nome):
    """Nome de tela: o que o dono deu pra categoria. O resto (Lanches, Mercadoria...) fica."""
    it = _cat_item(nome)
    return it["nome"] if it else nome

def _categ_dre():
    """Pro quadro de lucros: a linha de cada categoria (pela chave e pelos apelidos) e,
    na ordem, as linhas próprias que não existem na planilha."""
    import graficos
    de_para, extras = {}, []
    for it in _categ_lista():
        l = it["linha"]
        if l == LINHA_PROPRIA:
            l = it["nome"]
            if l not in graficos.DRE_OPERACIONAIS and l not in extras:
                extras.append(l)
        for n in [it["chave"]] + it["antigos"]:
            de_para[n] = l
    return {"de_para": de_para, "extras": extras}

def _categ_grava(cfg):
    """Guarda no produto de controle (cria na 1ª vez) e confere lendo de volta."""
    import json as _json
    texto = _json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
    pid = _categ["pid"]
    if not pid:
        r = gcapi.post("/produtos", {"nome": CATEG_CONTROLE, "movimenta_estoque": "0",
                                     "ativo": "0", "valor_custo": "0.00",
                                     "valor_venda": "0.00", "descricao": texto})
        d = r.get("data") or {}
        pid = str(d.get("id") or d.get("dados") or "") or _categ_le_gc()[0]
        if not pid:
            raise RuntimeError("não consegui criar o registro das categorias no sistema")
    else:
        d = gcapi.get(f"/produtos/{pid}").get("data") or {}
        gcapi.put(f"/produtos/{pid}", {"nome": d.get("nome") or CATEG_CONTROLE,
                                       "codigo_interno": d.get("codigo_interno"),
                                       "descricao": texto})
    volta = gcapi.get(f"/produtos/{pid}").get("data") or {}
    try:
        ok = _json.loads(volta.get("descricao") or "{}") == cfg
    except ValueError:
        ok = False
    with _categ_lock:
        _categ.update(t=0.0, cfg=None, lista=None, idx=None, pid=pid)    # relê na próxima
    if not ok:
        raise RuntimeError("o sistema não guardou a mudança inteira, tente de novo")
    return pid

def _categ_linhas_opcoes():
    import graficos
    return ([{"v": LINHA_PROPRIA, "rotulo": "Linha própria, com o nome da categoria"}]
            + [{"v": l, "rotulo": l} for l in graficos.DRE_OPERACIONAIS]
            + [{"v": v, "rotulo": r} for v, r in CATEG_LINHAS_ESPECIAIS])

def _categ_rotulo_linha(it):
    if it["linha"] == LINHA_PROPRIA:
        return f"linha própria ({it['nome']})"
    return dict(CATEG_LINHAS_ESPECIAIS).get(it["linha"], it["linha"])

def _categ_nome_valido(nome, chave_atual=None):
    """Erro (texto pro dono) ou None. O nome não pode repetir o de outra categoria, nem
    um apelido dela, nem um nome que o painel já usa pra outra coisa."""
    import graficos
    if len(nome) < 2 or len(nome) > 40:
        return "o nome precisa ter de 2 a 40 letras"
    if not _CATEG_NOME_OK.match(nome):
        return "use só letras, números, espaço e ( ) / - . , +"
    reservados = {"MERCADORIA", "MERCADORIA (NOTAS)", "DO MEU BOLSO", "SAQUE",
                  "GASTOS ADICIONAIS", "LANCHES", "IMPOSTOS", "TAXAS DE CARTAO E BANCO"}
    reservados |= {_norm_cat(x) for x in ("Vendas no balcão", "Vendas no delivery",
                   "Vendas totais", "Vendas líquidas", "Lucro bruto", "Lucro das operações",
                   "Lucro antes do imposto", "LUCRO LÍQUIDO", "Custo das mercadorias vendidas",
                   "Total de custos operacionais")}
    n = _norm_cat(nome)
    if n in reservados:
        return f'"{nome}" é um nome que o painel já usa pra outra coisa'
    for it in _categ_lista():
        if it["chave"] == chave_atual:
            continue
        if n in {_norm_cat(x) for x in [it["nome"], it["chave"]] + it["antigos"]}:
            return (f'já existe a categoria "{it["nome"]}"' if n == _norm_cat(it["nome"])
                    else f'"{nome}" já foi nome da categoria "{it["nome"]}"')
    return None

@app.route("/api/categorias", methods=["GET", "POST"])
@login_required
def api_categorias():
    """GET: a lista pra tela "Minhas categorias". POST: criar ou editar uma (nome, grupo,
    linha do quadro de lucros, escondida ou não). A mudança é aplicada em cima do que
    está guardado no GC AGORA, nunca em cima do que a tela tinha."""
    if request.method == "GET":
        lista = _categ_lista()
        return jsonify({"itens": [dict(it, linha_rotulo=_categ_rotulo_linha(it),
                                       # nomes que ELE deu antes (os de fábrica não contam)
                                       antes=[x for x in it["antigos"]
                                              if x not in CATEG_APELIDOS.get(it["chave"], [])])
                                  for it in lista],
                        "grupos": CATEG_GRUPOS, "linhas": _categ_linhas_opcoes(),
                        "aviso": _categ["erro"] and "Não consegui ler suas categorias no "
                                 "sistema agora; estou mostrando as últimas que li."})
    body = request.get_json(force=True, silent=True) or {}
    acao = (body.get("acao") or "").strip()
    nome = re.sub(r"\s+", " ", str(body.get("nome") or "")).strip()
    grupo = (body.get("grupo") or "").strip()
    linha = (body.get("linha") or "").strip()
    # teto do mês: em branco ou zero = sem teto; sem o campo (tela antiga) = não mexe
    tem_teto = "teto" in body
    teto = round(_num(body.get("teto")), 2) if tem_teto and str(body.get("teto")).strip() else 0.0
    if teto < 0 or teto > 1000000:
        return jsonify({"ok": False, "erro": "teto inválido"}), 400
    linhas_ok = {o["v"] for o in _categ_linhas_opcoes()}
    try:
        cfg = _categ_cfg(fresh=True)
    except Exception:
        return jsonify({"ok": False, "erro": "o sistema não respondeu agora, nada foi "
                                             "mudado. Tente de novo em alguns segundos"}), 502
    cats = dict(cfg.get("cats") or {})
    if grupo not in CATEG_GRUPOS:
        return jsonify({"ok": False, "erro": "escolha o grupo"}), 400
    if linha not in linhas_ok:
        return jsonify({"ok": False, "erro": "escolha onde ela entra no quadro de lucros"}), 400
    if acao == "criar":
        erro = _categ_nome_valido(nome)
        if erro:
            return jsonify({"ok": False, "erro": erro}), 400
        chave = nome
        cats[chave] = {"novo": 1, "g": grupo, "l": linha}
        if teto:
            cats[chave]["t"] = teto
    elif acao == "editar":
        it = next((x for x in _categ_lista() if x["chave"] == body.get("chave")), None)
        if not it:
            return jsonify({"ok": False, "erro": "não achei essa categoria, recarregue a tela"}), 400
        chave = it["chave"]
        erro = _categ_nome_valido(nome, chave)
        if erro:
            return jsonify({"ok": False, "erro": erro}), 400
        m = dict(cats.get(chave) or {})
        if _norm_cat(nome) != _norm_cat(it["nome"]):          # renomeou
            antigos = [a for a in (m.get("a") or []) if _norm_cat(a) != _norm_cat(nome)]
            if _norm_cat(it["nome"]) != _norm_cat(chave) and it["nome"] not in antigos:
                antigos.append(it["nome"])
            m["a"] = antigos
        m["n"] = nome
        m["g"], m["l"] = grupo, linha
        m["off"] = 0 if body.get("ativa", True) else 1
        if tem_teto:
            if teto:
                m["t"] = teto
            else:
                m.pop("t", None)
        # tira do registro o que voltou a ser igual ao de fábrica (mantém o guardado pequeno)
        if m.get("n") == chave:
            m.pop("n")
        if not m.get("a"):
            m.pop("a", None)
        if not m.get("off"):
            m.pop("off", None)
        if not m.get("novo"):
            fab = next((x for x in _categ_monta({}) if x["chave"] == chave), None)
            if fab:
                if m.get("g") == fab["grupo"]: m.pop("g")
                if m.get("l") == fab["linha"]: m.pop("l")
        cats[chave] = m
        if not cats[chave]:
            cats.pop(chave)
    else:
        return jsonify({"ok": False, "erro": "ação inválida"}), 400
    novo = dict(cfg, v=1, cats=cats)
    try:
        _categ_grava(novo)
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502
    _invalida("mapa", "gastos_mes", "lancamentos_" + _hoje()[:7], "pagar", "previsoes",
              "ultima_atualizacao")
    it = _cat_item(nome)
    return jsonify({"ok": True, "chave": chave, "nome": it["nome"] if it else nome})

# palavra no texto -> categoria SUGERIDA. É só sugestão: o dono confirma, e a categoria
# que ele escolhe é a que vale (ver o [cat:] lá em cima). Da mais longa pra mais curta.
SUGESTAO_PALAVRAS = [
    ("CEMIG", "Energia (CEMIG)"), ("ENERGIA", "Energia (CEMIG)"), ("CONTA DE LUZ", "Energia (CEMIG)"),
    ("COPASA", "Água (COPASA)"), ("CONTA DE AGUA", "Água (COPASA)"),
    ("ALUGUEL", "Aluguel (IPTU)"), ("IMOBILIARIA", "Aluguel (IPTU)"), ("IPTU", "Aluguel (IPTU)"),
    ("WERDEIROS", "Contabilidade (Werdeiros)"), ("CONTABIL", "Contabilidade (Werdeiros)"),
    ("CONTADOR", "Contabilidade (Werdeiros)"),
    ("ANOTA", "Anota AI"),
    ("PARCSN", "Parcelamento Simples (PARCSN)"), ("PARCELAMENTO SIMPLES", "Parcelamento Simples (PARCSN)"),
    ("SIMPLES NACIONAL", "DAS (Simples)"), ("RECEITA FEDERAL", "DAS (Simples)"),
    ("DARF", "DAS (Simples)"), ("DAS ", "DAS (Simples)"),
    ("PREFEITURA", "Taxas / alvará (PBH)"), ("PBH", "Taxas / alvará (PBH)"),
    ("ALVARA", "Taxas / alvará (PBH)"), ("FISCALIZACAO", "Taxas / alvará (PBH)"),
    ("TARIFA", "Tarifa do banco"), ("CESTA DE SERVICOS", "Tarifa do banco"),
    ("PACOTE DE SERVICOS", "Tarifa do banco"), ("TAR ", "Tarifa do banco"),
    ("MANUTENCAO DE CONTA", "Tarifa do banco"), ("IOF", "Tarifa do banco"),
    ("INTERNET", "Internet / telefone"), ("TELECOM", "Internet / telefone"),
    ("VIVO", "Internet / telefone"), ("CLARO", "Internet / telefone"), ("TIM ", "Internet / telefone"),
    ("INSS", "INSS s/ pró-labore"), ("VIGIA", "Vigia"), ("SEGURANCA", "Vigia"),
    ("SEGURO", "Seguro do carro"),
    ("POSTO", "Combustível"), ("COMBUSTIVEL", "Combustível"), ("GASOLINA", "Combustível"),
    ("SHELL", "Combustível"), ("IPIRANGA", "Combustível"), ("PETROBRAS", "Combustível"),
    ("PADARIA", "Padaria"), ("PANIFICADORA", "Padaria"),
    ("LANCHE", "Lanche"), ("ALMOCO", "Almoço"), ("MARMITA", "Almoço"),
    ("PAPELARIA", "Papelaria"),
    ("SACOLA", "Sacolas / gelo / copos"), ("GELO", "Sacolas / gelo / copos"),
    ("COPO", "Descartáveis (copo/saco)"), ("DESCARTAVE", "Descartáveis (copo/saco)"),
    ("LIMPEZA", "Limpeza / higiene"),
    ("CONSERTO", "Manutenção / conserto"), ("MANUTENCAO", "Manutenção / conserto"),
    ("REPARO", "Reparo da loja"),
    ("MOTOCA", "PH Motoca"), ("MOTOBOY", "Motoboy / entrega"),
    ("BIEL", "Biel (Gabriel)"), ("GABRIEL", "Biel (Gabriel)"),
    ("IGOR", "Igor (pró-labore / retirada)"),
    ("LUCCHESI", "Retirada do sócio (Victor)"),
]
SUGESTAO_PALAVRAS.sort(key=lambda x: -len(x[0]))

def _ext_norm(s):
    """Maiúsculo, sem acento e com espaço simples (pra comparar texto de banco)."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.upper()).strip()

def _sugere_por_palavra(texto):
    t = " " + _ext_norm(texto) + " "
    for kw, cat in SUGESTAO_PALAVRAS:
        if kw in t:
            return cat
    return None

def _serie_vencimentos(inicio, vezes, freq):
    """Datas de vencimento a partir da primeira: toda semana, a cada 15 dias ou todo mês
    (no mesmo dia; dia 31 vira o último dia dos meses curtos)."""
    d0 = datetime.date.fromisoformat(inicio[:10])
    out = []
    for i in range(vezes):
        if freq == "semanal":
            d = d0 + datetime.timedelta(days=7 * i)
        elif freq == "quinzenal":
            d = d0 + datetime.timedelta(days=14 * i)
        else:
            m = d0.month - 1 + i
            y, m = d0.year + m // 12, m % 12 + 1
            d = datetime.date(y, m, min(d0.day, calendar.monthrange(y, m)[1]))
        out.append(d.isoformat())
    return out

@app.route("/api/lancar-config")
@login_required
def api_lancar_config():
    """O que a central precisa pra montar os formulários: as categorias por grupo e as
    palavras que sugerem categoria enquanto o dono digita."""
    lista = _categ_lista()        # as categorias do dono (Minhas categorias), sem as escondidas
    grupos = [{"grupo": g, "cats": [it["nome"] for it in lista if it["grupo"] == g and it["ativa"]]}
              for g in CATEG_GRUPOS]
    palavras = []
    for kw, cat in SUGESTAO_PALAVRAS:
        it = _cat_item(cat)
        if it and it["ativa"]:
            palavras.append((kw, it["nome"]))
    return jsonify({"grupos": [x for x in grupos if x["cats"]], "palavras": palavras})

# ---- OUTRA ENTRADA (aporte, empréstimo, rendimento...) ----
# Dinheiro que entra e NÃO é venda: não pode ir pro faturamento. Na gaveta vira uma
# entrada avulsa (o fechamento conta com ela, igual troca de PIX e fiado); no banco, um
# recebimento na conta; na reserva/sobra, um depósito só do painel.
ENTRADA_TIPOS = {"aporte": "Aporte do sócio", "emprestimo": "Empréstimo",
                 "rendimento": "Rendimento do banco", "outra": "Outra entrada"}
PLANO_APLICACOES = "33015690"     # "Aplicações financeiras" (crédito)

@app.route("/api/entrada", methods=["POST"])
@login_required
def api_entrada():
    body = request.get_json(force=True, silent=True) or {}
    tipo = (body.get("tipo") or "outra").strip().lower()
    rot = ENTRADA_TIPOS.get(tipo, ENTRADA_TIPOS["outra"])
    valor = round(_num(body.get("valor")), 2)
    onde = (body.get("onde") or "").strip()
    data = (body.get("data") or _hoje())[:10]
    nota = (body.get("descricao") or "").strip()
    if valor <= 0:
        return jsonify({"ok": False, "erro": "informe o valor que entrou"}), 400
    if onde not in ("Caixa", "PIX", "Dinheiro", "Sobra") and not _banco_da_forma(onde):
        return jsonify({"ok": False, "erro": "escolha onde o dinheiro entrou"}), 400
    texto = rot + (f" — {nota}" if nota else "")
    plano = PLANO_APLICACOES if tipo == "rendimento" else PLANO_AJUSTE_C
    try:
        if onde in ("Caixa", "PIX") or _banco_da_forma(onde):
            pot = POTES[onde]
            r = gcapi.post("/recebimentos", {
                "descricao": _com_banco(f"ENTRADA AVULSA — {texto}", onde)[:180], "valor": f"{valor:.2f}",
                "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
                "liquidado": "1", "plano_contas_id": plano,
                "conta_bancaria_id": pot["conta"], "forma_pagamento_id": pot["forma"]})
        else:
            tag = RES_DEP_TAG if onde == "Dinheiro" else SOB_DEP_TAG
            r = gcapi.post("/pagamentos", {
                "descricao": f"{tag} guardei — {texto}"[:180], "valor": f"{valor:.2f}",
                "data_vencimento": data, "data_competencia": data, "liquidado": "0",
                "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
        d = r.get("data") or {}
        _invalida("resumo", "hoje", "fech_hoje", "fech_" + data, "fechamentos_7",
                  "fechamentos_12", "fechamentos_31", "entradas_" + data, "reserva", "sobra",
                  "pagar", "mapa", "lancamentos_" + _hoje()[:7])
        return jsonify({"ok": True, "valor": valor, "onde": onde, "tipo": rot,
                        "id": d.get("id") if isinstance(d, dict) else None})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

def _saldos_bancos(pgs):
    """Último saldo informado de cada banco: {banco: {saldo, data, origem, quando}}."""
    out = {}
    for p in pgs:
        m = _SALDO_RE.match(p.get("descricao") or "")
        if not m or m.group(1) not in BANCOS:
            continue
        reg = {"saldo": round(float(m.group(2)), 2), "data": (p.get("data_competencia") or "")[:10],
               "origem": m.group(3), "quando": p.get("cadastrado_em") or "", "id": p.get("id")}
        atual = out.get(m.group(1))
        if atual is None or (reg["data"], reg["quando"], str(reg["id"])) > (atual["data"], atual["quando"], str(atual["id"])):
            out[m.group(1)] = reg
    return out

def _ultimo_extrato(pgs):
    """Dia do último extrato de cada banco que foi conferido na central (a rotina)."""
    out = {}
    for p in pgs:
        m = _SALDO_RE.match(p.get("descricao") or "")
        if m and m.group(1) in BANCOS and m.group(3).startswith("extrato"):
            d = (p.get("data_competencia") or "")[:10]
            if d > out.get(m.group(1), ""):
                out[m.group(1)] = d
    return out

def _grava_saldo(banco, v, data, origem):
    """Guarda um saldo do banco (registro só do painel). origem começando com "extrato" conta
    como extrato da semana conferido."""
    r = gcapi.post("/pagamentos", {
        "descricao": f"{SALDO_TAG} {banco} = {v:.2f} ({origem})",
        # o GestãoClick não aceita valor zero em todo lugar; o saldo de verdade é o da descrição
        "valor": f"{max(abs(v), 0.01):.2f}", "data_vencimento": data,
        "data_competencia": data, "liquidado": "0",
        "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
    _invalida("como_esta", "saldos_bancos")
    d = r.get("data") or {}
    return d.get("id") if isinstance(d, dict) else None

@app.route("/api/saldo-banco", methods=["GET", "POST"])
@login_required
def api_saldo_banco():
    """GET: último saldo de cada banco. POST {banco, valor, data, origem}: guarda um saldo
    novo (o que o dono informou, ou o saldo final do extrato que ele subiu)."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        banco = (body.get("banco") or "").strip()
        if banco not in BANCOS:
            return jsonify({"ok": False, "erro": "escolha o banco"}), 400
        if body.get("valor") in (None, ""):
            return jsonify({"ok": False, "erro": "informe o saldo"}), 400
        v = round(_num(body.get("valor")), 2)
        data = (body.get("data") or _hoje())[:10]
        if data > _hoje():
            return jsonify({"ok": False, "erro": "essa data ainda não chegou"}), 400
        origem = "extrato" if (body.get("origem") or "") == "extrato" else "informado"
        try:
            rid = _grava_saldo(banco, v, data, origem)
            return jsonify({"ok": True, "banco": banco, "saldo": v, "data": data, "id": rid})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502
    def build():
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"})
        return {"bancos": _saldos_bancos(pgs), "extrato": _ultimo_extrato(pgs), "hoje": _hoje()}
    return jsonify(cached("saldos_bancos", 60, build))

# ---- INÍCIO (o painel do dia, inspirado no "Meu Caixa Organizado", 20/10/2026) ----
# Entradas × saídas do mês, dia a dia, contra o mesmo período do mês passado.
#  · ENTRADAS = vendas (balcão + delivery), sem saque. Os dias que já passaram vêm da foto
#    diária dos gráficos (buscar 2 meses de venda ao vivo leva ~1 min); o que a foto ainda
#    não tem (hoje, ou dias de quando ela não atualizou) é buscado ao vivo.
#  · SAÍDAS = o que já foi PAGO, pela data do pagamento (despesa + mercadoria), as mesmas
#    regras do "Total gasto no mês": fica de fora sangria, acerto de caixa, troca de PIX,
#    o que saiu do bolso do Victor e os registros só do painel (o gelo entregue já foi
#    pago no crédito de R$ 300 do mês: só o pagamento do freezer conta como saída).
def _vendas_dia_ao_vivo(de, ate):
    out = defaultdict(float)
    for tipo in ("vendas_balcao", "produto"):
        for v in gcapi.get_all("/vendas", {"tipo": tipo, "data_inicio": de, "data_fim": ate}):
            if "cancel" in (v.get("nome_situacao") or "").lower():
                continue
            if tipo == "produto" and "ANOTA AI" not in (v.get("observacoes") or "").upper():
                continue                           # venda "produto" que não é do app (saque etc.)
            if any((((w.get("produto", w) or {}).get("nome_produto")) or "").upper().startswith("SAQUE")
                   for w in (v.get("produtos") or [])):
                continue
            dt = (v.get("data") or "")[:10]
            if dt:
                out[dt] += _num(v.get("valor_total"))
    return out

@app.route("/api/inicio")
@login_required
def api_inicio():
    def build():
        import json as _json
        hoje = _hoje()
        d0 = datetime.date.fromisoformat(hoje)
        mes = hoje[:7]
        ant_ult = d0.replace(day=1) - datetime.timedelta(days=1)
        mes_ant = ant_ult.isoformat()[:7]
        try:
            snap = _json.loads(_painel_data_atual().replace("\\u003c", "<"))
        except Exception:
            snap = {}
        vd = dict(snap.get("vendas_dia") or {})
        foto = (snap.get("gerado_em") or "")[:10]
        if not vd:      # foto antiga, sem o campo novo: usa o dia a dia do mês (inclui saque)
            vd = {x["data"]: x["fat"] for x in (snap.get("dia_mes") or []) if x.get("data")}
        # o que a foto não cobre: do dia da foto até hoje (a foto é da manhã, sem o dia todo)
        ini_vivo = max(foto or hoje, f"{mes_ant}-01")
        if (d0 - datetime.date.fromisoformat(ini_vivo)).days > 10:
            ini_vivo = (d0 - datetime.timedelta(days=10)).isoformat()   # nunca mais que 10 dias ao vivo
        vivo = _vendas_dia_ao_vivo(ini_vivo, hoje)
        for d in list(vd):
            if d >= ini_vivo:
                vd.pop(d)
        vd.update(vivo)
        ant_tem_dias = any(k[:7] == mes_ant for k in vd)

        saidas = defaultdict(float)
        for p in gcapi.pagamentos_com_hist({"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
            if str(p.get("liquidado")) != "1":
                continue
            d = (p.get("descricao") or "").strip()
            du = d.upper()
            if du.startswith(("SANGRIA", "TROCA PIX")) or _eh_interno(d) or "[PROVISAO]" in du:
                continue
            if (p.get("nome_plano_conta") or "") == "Ajuste de caixa":
                continue
            dl = (p.get("data_liquidacao") or "")[:10]
            if dl[:7] in (mes, mes_ant) and dl <= hoje:
                saidas[dl] += _num(p.get("valor_total")) or _num(p.get("valor"))

        def serie(m, ate_dia):
            y, mm = int(m[:4]), int(m[5:7])
            n = min(calendar.monthrange(y, mm)[1], ate_dia)
            return [{"data": f"{m}-{i:02d}", "entradas": round(vd.get(f"{m}-{i:02d}", 0.0), 2),
                     "saidas": round(saidas.get(f"{m}-{i:02d}", 0.0), 2)} for i in range(1, n + 1)]
        cur = serie(mes, d0.day)
        ant_todo = serie(mes_ant, 31)
        ant_mesmo = ant_todo[:d0.day]
        tot = lambda L, k: round(sum(x[k] for x in L), 2)
        resumo = lambda L: {"entradas": tot(L, "entradas"), "saidas": tot(L, "saidas"),
                            "sobrou": round(tot(L, "entradas") - tot(L, "saidas"), 2)}
        return {"hoje": hoje, "mes": mes, "mes_ant": mes_ant, "dias": cur,
                "atual": resumo(cur), "ant_mesmo_periodo": resumo(ant_mesmo) if ant_tem_dias else None,
                "ant_mes_inteiro": resumo(ant_todo) if ant_tem_dias else None,
                "ant_total_vendas": next((x.get("fat") for x in (snap.get("meses") or [])
                                          if x.get("mes") == mes_ant), None),
                "foto": foto, "ao_vivo_desde": ini_vivo,
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("inicio", 120, build))

# ---- COMO ESTÁ A LOJA (topo da aba Loja) ----
# Ideia da planilha (fase 2, 20/10/2026): o dinheiro de hoje somando todos os lugares,
# até quando ele paga as contas já lançadas, o que vence em 30/60/90 dias. Conta só o que
# está lançado (regra do dono): venda que ainda vai entrar não conta no "aguenta até".
@app.route("/api/como-esta")
@login_required
def api_como_esta():
    def build():
        hoje = _hoje()
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"})
        res = sob = 0.0
        for p in pgs:
            d = p.get("descricao") or ""
            v = _num(p.get("valor_total")) or _num(p.get("valor"))
            if d.startswith(RES_DEP_TAG): res += v
            elif d.startswith(RES_OUT_TAG): res -= v
            elif d.startswith(SOB_DEP_TAG): sob += v
            elif d.startswith(SOB_OUT_TAG): sob -= v
        ab, abriu, din, saidas = _fech_dia(hoje)
        moedas, _ = _fundo_moedas(hoje)
        gaveta = round((ab if ab is not None else 200.0) + moedas + din - saidas, 2)
        bancos = _saldos_bancos(pgs)
        lugares = [{"nome": "Gaveta", "valor": gaveta,
                    "obs": "esperado agora" if abriu else "esperado (sem abertura lançada hoje)"},
                   {"nome": "Dinheiro reserva", "valor": round(max(res, 0), 2), "obs": ""},
                   {"nome": "Sobra de caixa", "valor": round(max(sob, 0), 2), "obs": ""}]
        for b in BANCOS:
            x = bancos.get(b)
            lugares.append({"nome": b, "valor": x["saldo"] if x else None,
                            "obs": (f"saldo de {x['data'][8:10]}/{x['data'][5:7]}" if x else "saldo não informado"),
                            "data": x["data"] if x else None})
        total = round(sum(l["valor"] or 0 for l in lugares), 2)
        # contas em aberto (as mesmas do mapa da aba Contas)
        d0 = datetime.date.fromisoformat(hoje)
        contas, abertas = [], []
        for p in pgs:
            if str(p.get("liquidado")) == "1":
                continue
            d = p.get("descricao") or ""
            if _eh_interno(d) or "[PROVISAO]" in d.upper():
                continue
            venc = (p.get("data_vencimento") or hoje)[:10]
            v = round(_num(p.get("valor_total")) or _num(p.get("valor")), 2)
            contas.append((venc, v))
            if p.get("data_vencimento"):
                abertas.append((venc, v, p))
        contas.sort()
        def ate(dias):
            lim = (d0 + datetime.timedelta(days=dias)).isoformat()
            return round(sum(v for venc, v in contas if venc < hoje or venc <= lim), 2)
        atrasado = round(sum(v for venc, v in contas if venc < hoje), 2)
        # até quando o dinheiro de hoje paga as contas, na ordem do vencimento
        acum, cobre_ate, falta_em = 0.0, None, None
        for venc, v in contas:
            acum += v
            if acum > total + 0.01:
                falta_em = max(venc, hoje)
                break
            cobre_ate = max(venc, hoje)
        horizonte = (d0 + datetime.timedelta(days=90)).isoformat()
        # avisos do topo do Início (fase 2): o que vence em até 3 dias, o atrasado e as
        # categorias perto do teto ou acima dele
        lim3 = (d0 + datetime.timedelta(days=3)).isoformat()
        def _aviso(venc, v, p):
            cat = _categoria_conta(p)
            return {"id": p.get("id"), "venc": venc, "valor": v,
                    "cat": _cat_nome(cat) if cat else MERCADORIA_CAT,
                    "nota": _nota_conta(p) or p.get("nome_plano_conta") or "",
                    "fornecedor": p.get("nome_fornecedor") or ""}
        atrasadas = sorted((x for x in abertas if x[0] < hoje), key=lambda x: (x[0], -x[1]))
        vencendo = sorted((x for x in abertas if hoje <= x[0] <= lim3), key=lambda x: (x[0], -x[1]))
        avisos = {"ate": lim3,
                  "vencendo": [_aviso(*x) for x in vencendo],
                  "atrasadas": [_aviso(*x) for x in atrasadas],
                  "total_atrasadas": round(sum(x[1] for x in atrasadas), 2),
                  "tetos": sorted((t for t in _tetos(_gasto_por_cat(pgs, hoje[:7]))
                                   if t.get("nivel") in ("perto", "passou")),
                                  key=lambda t: -t["pct"])}
        # gastos do banco que ainda ninguém disse o que foram (tela Não identificado)
        nis = [x for x in map(_ni_ler, pgs) if x and not x["ignorado"]]
        nao_ident = {"n": len(nis), "total": round(sum(x["valor"] for x in nis), 2)}
        return {"hoje": hoje, "lugares": lugares, "total": total, "avisos": avisos,
                "nao_ident": nao_ident,
                "atrasado": atrasado, "d30": ate(30), "d60": ate(60), "d90": ate(90),
                "falta_em": falta_em, "cobre_ate": cobre_ate,
                "sobra_90": round(total - ate(90), 2),
                "falta_atrasado": round(max(atrasado - total, 0), 2),
                "extrato": _ultimo_extrato(pgs), "horizonte": horizonte,
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("como_esta", 60, build))

# ---- CONFERIR EXTRATO (Itaú e Nubank) ----
# O dono baixa o extrato no app do banco (arquivo OFX ou planilha CSV) e sobe aqui. Eu
# leio as SAÍDAS e separo: o que já está lançado, o que é uma conta aberta no mapa (é só
# dar baixa) e o que falta lançar, com a categoria sugerida. As ENTRADAS do banco são
# venda no PIX/maquininha, que já entram pelas vendas — lançar de novo dobraria o
# faturamento, então só conto. Nada é gravado aqui: cada linha só entra quando o dono
# confirma. O GestãoClick tem UMA conta bancária, então Itaú e Nubank caem nela; o nome
# do banco vai escrito no lançamento.
# Ideia tirada da planilha: sugerir pelo histórico antes da palavra-chave e marcar
# repetido por data + valor. O marcador #ext:xxxxxxxx no lançamento evita lançar a mesma
# linha duas vezes quando o extrato é lido de novo.
BANCOS_EXTRATO = {"itau": "Itaú", "nubank": "Nubank"}
EXT_MARCA_RE = re.compile(r"\s*#ext:([0-9a-f]{8})")
_EXT_PREFIXOS = re.compile(
    r"^(PIX (TRANSF(ERENCIA)?|ENVIADO|ENVIADA|ENV|QRS|QR CODE|QR|PAGTO|PAGAMENTO)|"
    r"TRANSFERENCIA (ENVIADA|ENV)( PELO PIX)?|TRANSF(ERENCIA)?( ENVIADA)?|SISPAG( PIX| TED| DOC)?|"
    r"PAG(TO|AMENTO)?( DE)? (BOLETO|TIT(ULO)?|CONTA|FATURA)( EFETUADO)?|INT PAG TIT( BANCO)?|"
    r"DEB(ITO)? AUT(OMATICO)?|DA|COMPRA (NO )?(DEBITO|CARTAO)|COMPRA|RSHOP|TED|DOC|TEF)\b[\s\-:]*")
_EXT_SOCIETARIO = re.compile(r"\b(LTDA|EIRELI|ME|EPP|S ?A|S/A|SA|CIA|COMERCIO|COM|IND|INDUSTRIA|"
                             r"DISTRIBUIDORA|DISTRIB|DIST|DE|DA|DO|DAS|DOS|E|BEBIDAS)\b")
# fornecedores grandes que nem sempre estão com o nome igual no cadastro
_EXT_MARCAS_MERCADORIA = ("AMBEV", "HEINEKEN", "SOUZA CRUZ", "PHILIP MORRIS", "SPAL",
                          "COCA COLA", "PEPSICO", "BAT BRASIL", "CERVEJARIA")

def _ext_valor(s):
    """'-1.234,56' / '-1234.56' / '1.234,56-' / 'R$ 50,00' → número com sinal."""
    bruto = str(s or "").strip()
    t = re.sub(r"[^\d,.\-]", "", bruto)
    if not re.search(r"\d", t):
        return None
    neg = t.startswith("-") or t.endswith("-") or bruto.startswith("(")
    t = t.strip("-")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v

def _ext_data(s):
    """Data em AAAA-MM-DD a partir de 20261020(…), 20/10/2026, 20/10/26 ou 2026-10-20."""
    t = str(s or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", t)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", t)
        if not m:
            return None
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
    try:
        return datetime.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None

def _ext_ofx(txt):
    """Lançamentos de um OFX (o formato que Itaú e Nubank exportam). Aceita o OFX
    antigo, sem fechar as tags, e o novo, em XML."""
    linhas = []
    blocos = re.split(r"<STMTTRN>", txt, flags=re.I)[1:]
    for b in blocos:
        b = re.split(r"</STMTTRN>|</BANKTRANLIST>", b, flags=re.I)[0]
        def tag(n):
            m = re.search(r"<" + n + r">([^<\r\n]*)", b, flags=re.I)
            return (m.group(1).strip() if m else "").replace("&amp;", "&")
        data, valor = _ext_data(tag("DTPOSTED")), _ext_valor(tag("TRNAMT"))
        if not data or valor is None:
            continue
        memo = " - ".join(x for x in (tag("NAME"), tag("MEMO")) if x) or tag("PAYEE")
        linhas.append({"data": data, "valor": valor, "memo": memo,
                       "fitid": tag("FITID") or tag("CHECKNUM")})
    saldo = None
    m = re.search(r"<LEDGERBAL>(.*?)(</LEDGERBAL>|<AVAILBAL>|</STMTRS>|$)", txt, flags=re.I | re.S)
    if m:
        v = re.search(r"<BALAMT>([^<\r\n]*)", m.group(1), flags=re.I)
        d = re.search(r"<DTASOF>([^<\r\n]*)", m.group(1), flags=re.I)
        if v and _ext_valor(v.group(1)) is not None:
            saldo = {"valor": round(_ext_valor(v.group(1)), 2),
                     "data": _ext_data(d.group(1)) if d else None}
    return linhas, saldo

def _ext_csv(txt):
    """Lançamentos de uma planilha CSV: acha a linha de cabeçalho (data / descrição /
    valor, com os nomes que os bancos usam) e lê dali pra baixo."""
    import csv, io
    primeira = next((l for l in txt.splitlines() if l.strip()), "")
    sep = ";" if primeira.count(";") >= primeira.count(",") else ","
    rows = list(csv.reader(io.StringIO(txt), delimiter=sep))
    NOMES = {"data": ("DATA", "DATE", "DATA LANCAMENTO", "DATA DO LANCAMENTO", "DT"),
             "desc": ("DESCRICAO", "HISTORICO", "LANCAMENTO", "LANCAMENTOS", "TITLE",
                      "DETALHES", "DESCRICAO DO LANCAMENTO", "ESTABELECIMENTO"),
             "valor": ("VALOR", "AMOUNT", "VALOR (R$)", "VALOR R$", "VALOR(R$)")}
    NOMES_SALDO = ("SALDO", "SALDOS", "SALDO (R$)", "SALDOS (R$)", "SALDO R$", "SALDO(R$)")
    col, ini, col_saldo = None, 0, None
    for i, r in enumerate(rows[:15]):
        cab = [_ext_norm(c) for c in r]
        achou = {k: next((j for j, c in enumerate(cab) if c in v), None) for k, v in NOMES.items()}
        if None not in achou.values():
            col, ini = achou, i + 1
            col_saldo = next((j for j, c in enumerate(cab) if c in NOMES_SALDO), None)
            break
    if col is None:                                   # sem cabeçalho: data, descrição, valor
        col = {"data": 0, "desc": 1, "valor": 2}
    linhas, saldo = [], None
    for i, r in enumerate(rows[ini:]):
        if len(r) <= max(col.values()):
            continue
        data, valor = _ext_data(r[col["data"]]), _ext_valor(r[col["valor"]])
        memo = r[col["desc"]].strip()
        if col_saldo is not None and data and col_saldo < len(r) and _ext_valor(r[col_saldo]) is not None:
            if saldo is None or data >= saldo["data"]:
                saldo = {"valor": round(_ext_valor(r[col_saldo]), 2), "data": data}
        if not data or valor is None or "SALDO" in _ext_norm(memo)[:20]:
            continue
        linhas.append({"data": data, "valor": valor, "memo": memo, "fitid": ""})
    return linhas, saldo

def _ext_quem(memo):
    """Quem recebeu, a partir do texto do banco. Nubank escreve "Transferência enviada
    pelo Pix - FULANO - •••.123.456-•• - BANCO"; Itaú escreve "PIX TRANSF FULANO 20/10"."""
    partes = [p.strip() for p in str(memo or "").split(" - ") if p.strip()]
    if len(partes) > 1:
        cab = _ext_norm(partes[0])
        if re.match(r"^(TRANSFERENCIA|PIX|PAGAMENTO|COMPRA|DEBITO|BOLETO|TED|DOC)", cab):
            partes = partes[1:]
        nome = partes[0]
    else:
        nome = partes[0] if partes else ""
    n = _ext_norm(nome)
    for _ in range(2):
        n = _EXT_PREFIXOS.sub("", n).strip()
    n = re.sub(r"\s*\d{2}/\d{2}(/\d{2,4})?\s*$", "", n)            # data no fim (Itaú)
    n = re.sub(r"[*•.\-]*\d{3}[.\-*•]\d{3}[.\-*•]?\d{0,3}[\-*•]*\d{0,2}", "", n)   # CPF mascarado
    n = re.sub(r"\s+", " ", n).strip(" -*•.")
    if not re.search(r"[A-Z]{3}", n):          # sobrou só número ("INT PAG TIT BANCO 341")
        return _ext_norm(memo)[:60]
    return n

def _ext_nucleo(nome):
    """Nome sem LTDA/COMERCIO/DE/DA..., pra comparar fornecedor com o texto do banco."""
    return re.sub(r"\s+", " ", _EXT_SOCIETARIO.sub(" ", _ext_norm(nome))).strip()

def _ext_fornecedores():
    def build():
        return [_ext_nucleo(f.get("nome") or f.get("razao_social") or "")
                for f in gcapi.get_all("/fornecedores")]
    return [n for n in cached("ext_fornecedores", 3600, build) if len(n) >= 5]

def _ext_linhas(banco, txt):
    """Lançamentos do arquivo (OFX ou CSV), cada um com a sua marca e quem recebeu."""
    import hashlib
    linhas, saldo = _ext_ofx(txt) if re.search(r"<STMTTRN>", txt, re.I) else _ext_csv(txt)
    vistos = defaultdict(int)
    for l in linhas:
        # a marca sai do CONTEÚDO da linha (banco, dia, valor e texto), não da posição no
        # arquivo nem do código do banco: o mesmo PIX ganha a mesma marca em qualquer extrato
        # que o dono baixar (mês, semana, o mesmo de novo). Duas linhas iguais no dia: 1ª e 2ª.
        base = f"{banco}|{l['data']}|{l['valor']:.2f}|{_ext_norm(l['memo'])}"
        l["id"] = hashlib.sha1(f"{base}|{vistos[base]}".encode()).hexdigest()[:8]
        vistos[base] += 1
        l["quem"] = _ext_quem(l["memo"])
    return linhas, saldo

# ---- CONCILIAÇÃO AUTOMÁTICA (28/10/2026) ----
# Antes a tela só mostrava e o dono confirmava linha por linha. Agora, ao ler o extrato:
#  · já lançado (pela marca ou por valor + dia) → nada;
#  · casa com UMA conta aberta (mesmo valor, vencimento perto) → baixa sozinha;
#  · PIX pra conta do Victor ou do Igor → retirada lançada sozinha;
#  · dinheiro indo pra outra conta da loja ou pra aplicação → fora (não é gasto);
#  · o resto → Não identificado, até o dono dizer o que foi.
# Tudo que entra leva a marca #ext da linha: ler o mesmo extrato de novo não faz nada duas
# vezes, e o que o extrato fez sozinho dá pra desfazer na mesma tela.
_EXT_DINHEIRO = {"6055919", GUARDADO_FORMA_ID, RP_FORMA_ID}
_EXT_TOL = {"lancado": 3, "aberta": 12, "parecido": 1}
EXT_MAX_ACOES = 25        # gravações por leitura; o resto vai na próxima (a tela chama de novo)
_ext_trava = threading.Lock()
# PIX pra conta do Victor ou do Igor = retirada (o dono mandou em 28/10/2026). Vale o nome
# de QUEM RECEBEU, não o texto todo do banco.
_EXT_RETIRADA = [(re.compile(r"\bIGOR\b"), "Igor (pró-labore / retirada)"),
                 (re.compile(r"\bLUCCHESI\b|^VICTOR\b"), "Retirada do sócio (Victor)")]

def _ni_ler(p):
    """A linha do banco guardada num registro [NI]/[NIX], ou None."""
    m = _NI_RE.match(p.get("descricao") or "")
    if not m:
        return None
    memo, _, motivo = m.group(6).partition(NI_MOTIVO_SEP)
    return {"rid": str(p.get("id")), "ignorado": m.group(1) == "NIX", "banco": m.group(2),
            "data": m.group(3), "valor": float(m.group(4)), "id": m.group(5),
            "memo": memo.strip(), "motivo": motivo.strip(), "quem": _ext_quem(memo),
            "quando": max((p.get("cadastrado_em") or "")[:19], (p.get("modificado_em") or "")[:19])}

def _ni_desc(tag, nb, l, motivo=""):
    cab = f"{tag} {nb} {l['data']} {abs(l['valor']):.2f} #ext:{l['id']} · "
    fim = f"{NI_MOTIVO_SEP}{motivo}" if motivo else ""
    return cab + str(l["memo"])[:max(180 - len(cab) - len(fim), 10)] + fim

def _ni_corpo(desc, data):
    return {"descricao": desc[:180], "valor": "0.01", "data_vencimento": data,
            "data_competencia": data, "liquidado": "0",
            "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID}

def _ni_cria(nb, l, motivo=None):
    """Guarda a linha no Não identificado, ou já como "não é gasto" se vier o motivo (dinheiro
    indo pra outra conta da loja): assim nenhuma leitura depois traz ela de volta, nem o
    arquivo do Itaú, que corta o nome de quem recebeu."""
    tag = NIX_TAG if motivo else NI_TAG
    r = gcapi.post("/pagamentos", _ni_corpo(_ni_desc(tag, nb, l, motivo or ""), l["data"]))
    d = r.get("data") or {}
    return d.get("id") if isinstance(d, dict) else None

def _ext_contexto(pgs, nome_banco, ids_arquivo=None):
    """O que o extrato de um banco pode casar: as marcas já gravadas, o Não identificado
    desse banco e os candidatos por valor (já lançado, conta aberta, pago em dinheiro).
    ids_arquivo = as marcas das linhas do arquivo lido (None = tela do Não identificado)."""
    marcas, nis = {}, []
    cand = {"lancado": [], "aberta": [], "parecido": []}
    for p in pgs:
        d = p.get("descricao") or ""
        ni = _ni_ler(p)
        if ni:
            if ni["banco"] == nome_banco:
                nis.append(ni)
            continue
        if _eh_interno(d) or "[PROVISAO]" in d.upper():
            continue
        m = EXT_MARCA_RE.search(d)
        if m:
            marcas[m.group(1)] = p
        if (p.get("nome_plano_conta") or "") == "Ajuste de caixa":
            continue
        aberto = str(p.get("liquidado")) != "1"
        forma = str(p.get("forma_pagamento_id") or "")
        b_lanc = _banco_do_texto(d)
        if b_lanc and b_lanc != nome_banco:
            continue                    # foi no outro banco
        if aberto:
            grupo = "aberta"
        elif forma not in _EXT_DINHEIRO or d.upper().startswith("SANGRIA BANCO"):
            grupo = "lancado"           # saiu do banco: PIX, boleto, cartão, saque do banco
        elif d.upper().startswith("SANGRIA"):
            continue                    # dinheiro da gaveta indo pro cofre: não passa no banco
        else:
            grupo = "parecido"          # pago em dinheiro com o mesmo valor: pode ser engano
        cand[grupo].append({
            "p": p, "id": str(p.get("id")), "marca": m.group(1) if m else None,
            "data": ((p.get("data_vencimento") if aberto else
                      (p.get("data_liquidacao") or p.get("data_vencimento"))) or "")[:10],
            "valor": round(_num(p.get("valor_total")) or _num(p.get("valor")), 2),
            "merc": _eh_mercadoria(d),
            "quando": max((p.get("cadastrado_em") or "")[:19], (p.get("modificado_em") or "")[:19])})
    return {"marcas": marcas, "nis": nis, "cand": cand, "usados": set(), "nis_usados": set(),
            "arquivo": ids_arquivo}

def _ext_cands(ctx, l, grupo, depois=None):
    """Candidatos do grupo com o mesmo valor da linha, do mais perto pro mais longe.
    depois = só o que foi lançado ou mexido depois desse momento (a linha já estava no Não
    identificado: o que existia antes já tinha sido olhado e não era ela)."""
    v = round(abs(l["valor"]), 2)
    d0 = datetime.date.fromisoformat(l["data"])
    out = []
    for c in ctx["cand"][grupo]:
        if c["id"] in ctx["usados"] or abs(c["valor"] - v) > 0.01 or not c["data"]:
            continue
        # lançamento com a marca de OUTRA linha que está no arquivo é dela. Marca que não está
        # no arquivo pode ser esta mesma linha escrita diferente (OFX num dia, CSV no outro)
        if c["marca"] and c["marca"] != l["id"] and (ctx["arquivo"] is None or c["marca"] in ctx["arquivo"]):
            continue
        if depois is not None and c["quando"] <= depois:
            continue
        try:
            dd = abs((datetime.date.fromisoformat(c["data"]) - d0).days)
        except ValueError:
            continue
        # nota paga à vista entra no dia da nota, e o PIX pro fornecedor às vezes sai dias depois
        tol = 10 if grupo == "lancado" and c["merc"] else _EXT_TOL[grupo]
        if dd <= tol:
            out.append((dd, c["id"], c["p"]))
    out.sort(key=lambda x: (x[0], x[1]))
    return out

def _ext_pega(ctx, cands, unica=False):
    """O candidato mais perto. Conta aberta (unica=True) só quando não tem dúvida: uma só
    com esse valor, ou uma bem mais perto que a outra (uma semana de diferença)."""
    if not cands:
        return None
    if unica and len(cands) > 1 and cands[1][0] - cands[0][0] < 7:
        return None
    ctx["usados"].add(cands[0][1])
    return cands[0][2]

def _ext_fora(memo):
    """Saída que não é gasto: dinheiro indo pra outra conta da loja ou pra aplicação."""
    m = _ext_norm(memo)
    if "DISTRIBUIDORA DA PRACA" in m:
        return "transferência entre as contas da loja"
    if re.search(r"\b(APLICACAO|APLIC|RESGATE)\b", m):
        return "dinheiro indo pra aplicação do banco"
    return None

def _ext_retirada(quem):
    q = _ext_norm(quem)
    return next((cat for rx, cat in _EXT_RETIRADA if rx.search(q)), None)

def _ext_classifica(ctx, l):
    """O que fazer com uma saída do extrato. Ordem: marca gravada → Não identificado de
    antes → já lançado → conta aberta → não é gasto → pago em dinheiro (dúvida) →
    retirada do Victor/Igor → Não identificado."""
    livres = [x for x in ctx["nis"] if x["rid"] not in ctx["nis_usados"]]
    p = ctx["marcas"].get(l["id"])
    if p is not None and str(p.get("id")) not in ctx["usados"]:
        ctx["usados"].add(str(p.get("id")))
        ni = next((x for x in livres if x["id"] == l["id"]), None)
        if ni is not None:              # lançado, mas o registro do Não identificado ficou
            ctx["nis_usados"].add(ni["rid"])
            return {"status": "resolvido", "ni": ni, "p": p}
        return {"status": "lancado", "p": p}
    v = round(abs(l["valor"]), 2)
    # a mesma linha guardada com outra marca (o banco escreveu diferente noutro arquivo)
    ni = (next((x for x in livres if x["id"] == l["id"]), None)
          or next((x for x in livres if x["data"] == l["data"] and abs(x["valor"] - v) < 0.005), None))
    if ni is not None:
        ctx["nis_usados"].add(ni["rid"])
        if ni["ignorado"]:
            return {"status": "ignorado", "ni": ni}
        p = _ext_pega(ctx, _ext_cands(ctx, l, "lancado", ni["quando"]))
        if p is not None:
            return {"status": "resolvido", "ni": ni, "p": p}
        p = _ext_pega(ctx, _ext_cands(ctx, l, "aberta", ni["quando"]), unica=True)
        if p is not None:
            return {"status": "baixa", "ni": ni, "p": p}
        return {"status": "pendente", "ni": ni}
    p = _ext_pega(ctx, _ext_cands(ctx, l, "lancado"))
    if p is not None:
        return {"status": "lancado", "p": p}
    p = _ext_pega(ctx, _ext_cands(ctx, l, "aberta"), unica=True)
    if p is not None:
        return {"status": "baixa", "p": p}
    fora = _ext_fora(l["memo"])
    if fora:
        return {"status": "fora", "motivo": fora}
    if _ext_cands(ctx, l, "parecido"):
        return {"status": "ni"}         # tem um igual pago em dinheiro: o dono decide
    cat = _ext_retirada(l["quem"])
    if cat:
        return {"status": "retirada", "cat": cat}
    return {"status": "ni"}

def _ext_lanca(nb, l, cat=None, mercadoria=False):
    """Gasto já pago no PIX do banco, com a marca da linha (o mesmo corpo do /api/gasto)."""
    pot = POTES[f"PIX {nb}"]
    if mercadoria:      # começa com "Compra de": é assim que o mês reconhece mercadoria
        texto, plano = f"Compra de {l['quem']} — sem nota, extrato {nb}", CATS["Outros"]
    else:
        cat = _cat_nome(cat)
        texto, plano = f"[cat:{cat}] {l['quem']} · extrato {nb}", _plano_cat(cat)
    v, dt = round(abs(l["valor"]), 2), l["data"]
    r = gcapi.post("/pagamentos", {
        "descricao": f"{texto[:165]} #ext:{l['id']}", "valor": f"{v:.2f}",
        "data_vencimento": dt, "data_competencia": dt, "data_liquidacao": dt, "liquidado": "1",
        "plano_contas_id": plano, "conta_bancaria_id": pot["conta"],
        "forma_pagamento_id": pot["forma"]})
    d = r.get("data") or {}
    return d.get("id") if isinstance(d, dict) else None

def _ext_baixa(nb, l, pid, p=None):
    """Baixa na conta aberta que casou com a linha: paga no PIX do banco, no dia e no
    valor do extrato, com a marca da linha na descrição."""
    p = p or _pagamento(pid)
    if str(p.get("liquidado")) == "1":
        raise RuntimeError("essa conta já estava paga")
    payload = _payload_baixa(p, f"PIX {nb}", round(abs(l["valor"]), 2), l["data"])
    payload["descricao"] = f"{EXT_MARCA_RE.sub('', payload['descricao'])[:165]} #ext:{l['id']}"
    gcapi.put(f"/pagamentos/{pid}", payload)

def _ext_resumo_p(p):
    return {"id": p.get("id"), "desc": EXT_MARCA_RE.sub("", _nota_conta(p)) or p.get("nome_plano_conta") or "—",
            "data": ((p.get("data_liquidacao") or p.get("data_vencimento")) or "")[:10],
            "venc": (p.get("data_vencimento") or "")[:10],
            "valor": round(_num(p.get("valor_total")) or _num(p.get("valor")), 2),
            "forma": _rotulo_forma(p), "fornecedor": p.get("nome_fornecedor") or "",
            "cat": "Mercadoria" if _eh_mercadoria(p.get("descricao")) else _cat_nome(_categoria_conta(p) or "")}

def _ext_hist(pgs):
    """O que o dono já lançou pra cada nome: (dia, texto, categoria), do mais novo pro mais velho."""
    hist = []
    for p in pgs:
        if str(p.get("liquidado")) != "1":
            continue
        d = p.get("descricao") or ""
        if _eh_interno(d) or d.upper().startswith("SANGRIA"):
            continue
        texto = _ext_norm(_sem_cat(d) + " " + (p.get("nome_fornecedor") or ""))
        cat = "Mercadoria" if _eh_mercadoria(d) else (_cat_marcada(d) or _categoria_conta(p))
        if cat:
            hist.append((p.get("data_liquidacao") or "", texto, cat))
    hist.sort(reverse=True)
    return hist

def _ext_sugere(quem, memo, hist, forns):
    """(categoria sugerida, por quê, parece mercadoria). Ideia da planilha: o histórico do
    próprio dono antes da palavra-chave."""
    sug = por = None
    qnorm, qn, memo_n = _ext_norm(quem), _ext_nucleo(quem), _ext_norm(memo)
    if len(qnorm) >= 4:
        for _, texto, cat in hist:
            if qnorm in texto:
                sug, por = cat, "historico"
                break
    merc = (sug == "Mercadoria" or any(m in memo_n for m in _EXT_MARCAS_MERCADORIA)
            or (len(qn) >= 5 and any(f in qn or qn in f for f in forns)))
    if sug == "Mercadoria":
        sug = por = None
    if not sug and not merc:
        c = _sugere_por_palavra(memo)
        if c:
            sug, por = c, "palavra"
    if sug:                             # o nome da lista do dono; escondida não é sugerida
        it = _cat_item(sug)
        if it and not it["ativa"]:
            sug = por = None
        elif it:
            sug = it["nome"]
    return sug, por, merc

def _ext_invalida():
    _invalida("pagar", "resumo", "previsoes", "gastos_mes", "mapa", "como_esta", "inicio",
              "hoje", "saldos_bancos", "ultima_atualizacao", "lancamentos_" + _hoje()[:7])

def _ext_concilia(banco, nb, linhas, saldo=None, simular=False):
    """Concilia as linhas de um banco com o sistema e grava o que tem de gravar (simular=True
    só mostra o que faria). Serve pro arquivo do extrato e pro Open Finance. Quem chama
    segura a _ext_trava."""
    saidas = sorted((l for l in linhas if l["valor"] < 0), key=lambda x: (x["data"], x["id"]))
    entradas = [l for l in linhas if l["valor"] > 0]
    datas = [l["data"] for l in linhas]
    out, feitas, erro = [], 0, None
    try:
        ctx = _ext_contexto(gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                                          "data_fim": "2027-12-31"}), nb,
                            {l["id"] for l in linhas})
        for l in saidas:
            r = _ext_classifica(ctx, l)
            st = r["status"]
            item = {"id": l["id"], "data": l["data"], "valor": round(-l["valor"], 2),
                    "memo": l["memo"][:140], "quem": l["quem"][:80], "status": st}
            if r.get("p") is not None:
                item["match"] = _ext_resumo_p(r["p"])
            if r.get("ni"):
                item["rid"], item["motivo"] = r["ni"]["rid"], r["ni"]["motivo"]
            if st == "fora":
                item["motivo"] = r["motivo"]
            if st == "retirada":
                item["cat"] = _cat_nome(r["cat"])
            if st in ("baixa", "retirada", "ni", "resolvido", "fora") and not simular:
                if erro or feitas >= EXT_MAX_ACOES:
                    item["status"] = "fila"
                else:
                    try:
                        if st == "baixa":
                            _ext_baixa(nb, l, r["p"].get("id"))
                            item["pid"] = str(r["p"].get("id"))
                        elif st == "retirada":
                            item["pid"] = _ext_lanca(nb, l, cat=r["cat"])
                        elif st == "ni":
                            item["rid"] = _ni_cria(nb, l)
                        elif st == "fora":
                            item["rid"] = _ni_cria(nb, l, r["motivo"])
                        if r.get("ni"):         # saiu do Não identificado
                            gcapi.delete(f"/pagamentos/{r['ni']['rid']}")
                        feitas += 1
                    except Exception as e:
                        # não repito: a gravação pode ter entrado. A próxima leitura vê o que
                        # ficou no sistema e continua de onde parou, sem fazer nada duas vezes.
                        erro = str(e)[:200]
                        item["status"] = "erro"
            out.append(item)
    finally:
        if feitas or erro:
            _ext_invalida()
    cont = defaultdict(int)
    for x in out:
        cont[x["status"]] += 1
    return {"ok": True, "banco": banco, "nome_banco": nb, "simulado": simular,
            "de": min(datas) if datas else None, "ate": max(datas) if datas else None,
            "saldo": saldo, "saidas": out, "contagem": cont,
            "total_saidas": round(sum(x["valor"] for x in out), 2),
            "continua": bool(cont.get("fila")) and not erro, "erro": erro,
            "entradas": {"n": len(entradas), "total": round(sum(l["valor"] for l in entradas), 2)}}

@app.route("/api/extrato/ler", methods=["POST"])
@login_required
def api_extrato_ler():
    body = request.get_json(force=True, silent=True) or {}
    banco = (body.get("banco") or "").strip().lower()
    txt = body.get("arquivo") or ""
    if banco not in BANCOS_EXTRATO:
        return jsonify({"ok": False, "erro": "escolha o banco do extrato"}), 400
    if len(txt) < 20:
        return jsonify({"ok": False, "erro": "o arquivo veio vazio"}), 400
    if len(txt) > 3_000_000:
        return jsonify({"ok": False, "erro": "arquivo grande demais — baixe um mês por vez"}), 400
    txt = txt.lstrip("\ufeff")
    linhas, saldo_ext = _ext_linhas(banco, txt)
    if not linhas:
        return jsonify({"ok": False, "erro": "não achei lançamento nenhum nesse arquivo. "
                        "No app do banco, exporte o extrato em OFX (ou CSV)."}), 400
    if not _ext_trava.acquire(blocking=False):
        return jsonify({"ok": False, "erro": "já estou conciliando um extrato. Espere terminar "
                        "e leia de novo."}), 409
    try:
        return jsonify(_ext_concilia(banco, BANCOS_EXTRATO[banco], linhas, saldo_ext))
    finally:
        _ext_trava.release()

@app.route("/api/extrato/desfazer", methods=["POST"])
@login_required
def api_extrato_desfazer():
    """Desfaz o que o extrato fez sozinho ("não é essa conta", "não é retirada"): a conta
    volta a ficar aberta, ou o gasto é apagado, e a linha do banco vai pro Não identificado.
    Assim a próxima leitura do extrato não repete o mesmo engano."""
    body = request.get_json(force=True, silent=True) or {}
    tipo = (body.get("tipo") or "").strip()
    pid = str(body.get("pid") or "").strip()
    nb = (body.get("banco") or "").strip()
    x = body.get("linha") or {}
    l = {"id": str(x.get("id") or ""), "data": _ext_data(x.get("data")),
         "valor": round(_num(x.get("valor")), 2), "memo": str(x.get("memo") or "")[:140]}
    if (tipo not in ("baixa", "retirada") or not pid or nb not in BANCOS or not l["data"]
            or not re.fullmatch(r"[0-9a-f]{8}", l["id"]) or l["valor"] <= 0):
        return jsonify({"ok": False, "erro": "não entendi o que desfazer"}), 400
    with _ext_trava:
        try:
            cur = _pagamento(pid)
            d = cur.get("descricao") or ""
            if f"#ext:{l['id']}" not in d:
                return jsonify({"ok": False, "erro": "esse lançamento não foi o extrato que fez"}), 400
            base = {"valor": cur.get("valor") or cur.get("valor_total") or "0",
                    "plano_contas_id": cur.get("plano_contas_id") or "",
                    "data_vencimento": (cur.get("data_vencimento") or _hoje())[:10],
                    "data_competencia": (cur.get("data_competencia") or _hoje())[:10],
                    "fornecedor_id": cur.get("fornecedor_id") or "", "liquidado": "0"}
            if tipo == "baixa":         # a conta volta a ficar aberta, como estava
                limpo = EXT_MARCA_RE.sub("", d).replace(f" (PIX {nb})", "").strip()
                gcapi.put(f"/pagamentos/{pid}", dict(base, descricao=limpo[:180],
                                                     forma_pagamento_id=BOLETO_FORMA_ID))
            else:                       # mesma sequência do excluir: desmarca o pago e apaga
                if str(cur.get("liquidado")) == "1":
                    gcapi.put(f"/pagamentos/{pid}", dict(base, descricao=d))
                gcapi.delete(f"/pagamentos/{pid}")
            rid = _ni_cria(nb, l)
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502
        finally:
            _ext_invalida()
    return jsonify({"ok": True, "rid": rid})

def _ext_contas_perto(ctx, l):
    """Contas abertas com valor parecido (pode ter juros ou desconto), pro dono escolher."""
    v = round(abs(l["valor"]), 2)
    d0 = datetime.date.fromisoformat(l["data"])
    out = []
    for c in ctx["cand"]["aberta"]:
        if c["id"] in ctx["usados"] or c["marca"] or abs(c["valor"] - v) > max(5.0, v * 0.15):
            continue
        try:
            dd = abs((datetime.date.fromisoformat(c["data"]) - d0).days)
        except ValueError:
            dd = 999
        out.append((round(abs(c["valor"] - v), 2), dd, c["id"], c["p"]))
    out.sort(key=lambda x: x[:3])
    return [_ext_resumo_p(x[3]) for x in out[:5]]

@app.route("/api/nao-identificado", methods=["GET", "POST"])
@login_required
def api_nao_identificado():
    """GET: os gastos do banco que ainda não se sabe o que foram, com as pistas (o que o
    dono já lançou pra esse nome, conta aberta parecida, pago em dinheiro no mesmo valor).
    O que foi lançado por outro caminho depois (Gerar compra, gasto, baixa no mapa) sai da
    lista sozinho. POST: o dono diz o que foi."""
    if request.method == "POST":
        return _ni_resolver(request.get_json(force=True, silent=True) or {})
    with _ext_trava:
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"})
        hist, forns = _ext_hist(pgs), _ext_fornecedores()
        itens, ign, limpos = [], [], 0
        for nb in BANCOS:
            ctx = _ext_contexto(pgs, nb)
            for ni in sorted(ctx["nis"], key=lambda x: (x["data"], x["rid"])):
                l = {"id": ni["id"], "data": ni["data"], "valor": -ni["valor"], "memo": ni["memo"]}
                base = {"rid": ni["rid"], "id": ni["id"], "banco": nb, "data": ni["data"],
                        "valor": ni["valor"], "memo": ni["memo"], "quem": ni["quem"]}
                if ni["ignorado"]:
                    ign.append(dict(base, motivo=ni["motivo"]))
                    continue
                p = ctx["marcas"].get(ni["id"])
                if p is None:
                    p = _ext_pega(ctx, _ext_cands(ctx, l, "lancado", ni["quando"]))
                if p is not None:
                    try:
                        gcapi.delete(f"/pagamentos/{ni['rid']}")
                        limpos += 1
                        continue
                    except Exception:
                        pass
                sug, por, merc = _ext_sugere(ni["quem"], ni["memo"], hist, forns)
                par = _ext_cands(ctx, l, "parecido")
                itens.append(dict(base, sugestao=sug, por=por, mercadoria=merc,
                                  parecido=_ext_resumo_p(par[0][2]) if par else None,
                                  contas=_ext_contas_perto(ctx, l)))
        if limpos:
            _ext_invalida()
    itens.sort(key=lambda x: (x["data"], x["rid"]), reverse=True)
    ign.sort(key=lambda x: (x["data"], x["rid"]), reverse=True)
    return jsonify({"ok": True, "itens": itens, "total": round(sum(x["valor"] for x in itens), 2),
                    "ignorados": ign[:40], "limpos": limpos})

def _ni_resolver(body):
    rid = str(body.get("rid") or "").strip()
    acao = (body.get("acao") or "").strip()
    if not rid or acao not in ("categoria", "mercadoria", "conta", "ignorar", "voltar"):
        return jsonify({"ok": False, "erro": "não entendi o que fazer com essa linha"}), 400
    sumiu = jsonify({"ok": False, "erro": "não achei essa linha: ela pode já ter sido resolvida. "
                     "Abra a tela de novo."}), 404
    with _ext_trava:
        try:
            ni = _ni_ler(_pagamento(rid))
        except Exception:
            ni = None
        if ni is None or ni["banco"] not in BANCOS:
            return sumiu
        nb = ni["banco"]
        l = {"id": ni["id"], "data": ni["data"], "valor": -ni["valor"], "memo": ni["memo"],
             "quem": ni["quem"]}
        if acao in ("ignorar", "voltar"):
            if (acao == "ignorar") == ni["ignorado"]:
                return jsonify({"ok": True})
            motivo = str(body.get("motivo") or "não é gasto da loja")[:60] if acao == "ignorar" else ""
            try:
                gcapi.put(f"/pagamentos/{rid}", _ni_corpo(
                    _ni_desc(NIX_TAG if acao == "ignorar" else NI_TAG, nb, l, motivo), l["data"]))
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)[:200]}), 502
            _invalida("como_esta")
            return jsonify({"ok": True})
        if ni["ignorado"]:
            return jsonify({"ok": False, "erro": "essa linha está nas ignoradas: volte ela antes"}), 400
        try:
            if acao == "categoria":
                cat = str(body.get("categoria") or "").strip()[:60]
                if not cat:
                    return jsonify({"ok": False, "erro": "escolha a categoria"}), 400
                pid = _ext_lanca(nb, l, cat=cat)
            elif acao == "mercadoria":
                pid = _ext_lanca(nb, l, mercadoria=True)
            else:
                pid = str(body.get("conta_id") or "").strip()
                conta = _pagamento(pid) if pid else {}
                if not conta or _eh_interno(conta.get("descricao")):
                    return jsonify({"ok": False, "erro": "escolha a conta"}), 400
                if str(conta.get("liquidado")) == "1":
                    return jsonify({"ok": False, "erro": "essa conta já está paga"}), 400
                _ext_baixa(nb, l, pid, conta)
        except Exception as e:
            _ext_invalida()
            return jsonify({"ok": False, "erro": "o sistema não respondeu (" + str(e)[:120] + "). "
                            "Abra o Não identificado de novo antes de repetir: se já entrou, "
                            "a linha some sozinha."}), 502
        try:
            gcapi.delete(f"/pagamentos/{rid}")
        except Exception:
            pass    # a marca já está no lançamento: a linha some na próxima vez que a tela abrir
        _ext_invalida()
    return jsonify({"ok": True, "id": pid})

# ---- OPEN FINANCE (Meu Pluggy, só leitura) ----
# 28/10/2026: o dono conectou as contas da loja (Itaú e Nubank) no Meu Pluggy, que é grátis e
# lê pelo Open Finance o que ele autorizou no app do banco. Só LEITURA: nada aqui move
# dinheiro. O painel lê o banco sozinho (ao abrir, no máximo de 3 em 3 horas, ou no botão da
# tela do extrato) e passa pela MESMA conciliação do arquivo. O Meu Pluggy atualiza os bancos
# 1 vez por dia: o que saiu hoje pode aparecer só amanhã.
# No servidor: PLUGGY_CLIENT_ID, PLUGGY_CLIENT_SECRET (senha: só no Render e no .env, nunca no
# código) e PLUGGY_ITENS = "Itaú=<item id>;Nubank=<item id>".
PLUGGY_API = "https://api.pluggy.ai"
PLUGGY_DIAS = 10                  # cada leitura olha 10 dias pra trás (banco às vezes posta atrasado)
PLUGGY_INTERVALO = 3 * 3600
_BRT = datetime.timezone(datetime.timedelta(hours=-3))     # Brasília, sem horário de verão
_pluggy = {"rodando": False, "ultimo": 0.0, "quando": None, "erro": None, "bancos": {}}
_pluggy_lock = threading.Lock()

def _pluggy_cfg():
    itens = []
    for par in (gcapi._tok("PLUGGY_ITENS") or "").split(";"):
        b, _, i = par.partition("=")
        if b.strip() in BANCOS and i.strip():
            itens.append((b.strip(), i.strip()))
    cid, sec = gcapi._tok("PLUGGY_CLIENT_ID"), gcapi._tok("PLUGGY_CLIENT_SECRET")
    return {"cid": cid, "sec": sec, "itens": itens} if cid and sec and itens else None

def _pluggy_req(metodo, caminho, corpo=None, chave=None):
    import json as _json, urllib.request, urllib.error
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if chave:
        h["X-API-KEY"] = chave
    req = urllib.request.Request(PLUGGY_API + caminho, method=metodo, headers=h,
                                 data=_json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"o Open Finance respondeu {e.code} em {caminho.split('?')[0]}") from None

def _pluggy_data(iso):
    """O Pluggy manda a hora em UTC: PIX de 30/09 às 23h51 chega como 01/10 02h51."""
    s = str(iso or "")
    if len(s) <= 10:
        return s[:10]
    s = re.sub(r"\.\d+", "", s.replace("Z", "+00:00"))
    return datetime.datetime.fromisoformat(s).astimezone(_BRT).date().isoformat()

def _pluggy_transacoes(chave, conta, de, ate):
    import urllib.parse
    base = f"/v2/transactions?accountId={conta}&dateFrom={de}&dateTo={ate}"
    url, out = base, []
    for _ in range(100):
        r = _pluggy_req("GET", url, chave=chave)
        out += r.get("results", [])
        n = r.get("next")
        if not n:
            break
        if n.startswith("http"):
            u = urllib.parse.urlparse(n)
            url = u.path + "?" + u.query
        elif n.startswith("/"):
            url = n
        else:
            url = base + "&cursor=" + urllib.parse.quote(n)
    return out

def _pluggy_le(cfg, dias):
    """Lê a conta de cada banco (corrente/poupança; cartão fica de fora: a fatura sai da conta
    e aparece lá). Devolve as linhas no MESMO formato do arquivo do extrato, o saldo e se o
    banco está em dia com o Open Finance."""
    import hashlib
    chave = _pluggy_req("POST", "/auth", {"clientId": cfg["cid"], "clientSecret": cfg["sec"]})["apiKey"]
    hoje = datetime.date.fromisoformat(_hoje())
    de = (hoje - datetime.timedelta(days=dias)).isoformat()
    ate = (hoje + datetime.timedelta(days=1)).isoformat()
    out = {}
    for nb, item_id in cfg["itens"]:
        item = _pluggy_req("GET", f"/items/{item_id}", chave=chave)
        b = {"status": item.get("status") or "?", "linhas": [], "saldo": None,
             "atualizado": _pluggy_data(item["lastUpdatedAt"]) if item.get("lastUpdatedAt") else None}
        for c in _pluggy_req("GET", f"/accounts?itemId={item_id}", chave=chave).get("results", []):
            if c.get("type") != "BANK":
                continue
            b["saldo"] = round((b["saldo"] or 0) + (c.get("balance") or 0), 2)
            for t in _pluggy_transacoes(chave, c["id"], de, ate):
                if t.get("status") == "PENDING":
                    continue            # ainda pode mudar de valor ou sumir
                v = round(abs(t.get("amount") or 0), 2)
                tipo = t.get("type") or ("CREDIT" if (t.get("amount") or 0) > 0 else "DEBIT")
                pd = t.get("paymentData") or {}
                outro = ((pd.get("receiver") if tipo == "DEBIT" else pd.get("payer")) or {}).get("name") or ""
                memo = re.sub(r"\s+", " ", t.get("description") or "").strip()
                if outro and _ext_norm(outro) not in _ext_norm(memo):
                    memo = f"{memo} · {outro}".strip(" ·")
                b["linhas"].append({
                    "data": _pluggy_data(t.get("date")), "valor": -v if tipo == "DEBIT" else v,
                    "memo": memo, "fitid": t.get("id") or "",
                    # a marca vem do código do lançamento no Open Finance (não muda nunca)
                    "id": hashlib.sha1(f"pluggy|{t.get('id')}".encode()).hexdigest()[:8],
                    # quem recebeu, do jeito que o banco registrou o PIX (é o que acha Victor e Igor)
                    "quem": _ext_norm(outro) if outro else _ext_quem(memo)})
        out[nb] = b
    return out

def _pluggy_saldo(nb, v, data):
    """Guarda o saldo lido do banco, só se mudou (ou é de um dia mais novo que o guardado)."""
    atual = _saldos_bancos(gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                                         "data_fim": "2027-12-31"})).get(nb)
    if atual and (atual["data"] > data or (atual["data"] == data and abs(atual["saldo"] - v) < 0.005)):
        return
    _grava_saldo(nb, v, data, "extrato open finance")

def _pluggy_sincroniza(dias=PLUGGY_DIAS, simular=False, rodadas=8):
    """Lê o banco e concilia. Em segundo plano roda até acabar (rodadas); no botão, uma ou
    duas rodadas e a tela chama de novo."""
    cfg = _pluggy_cfg()
    if not cfg:
        raise RuntimeError("o Open Finance ainda não está ligado")
    lido = _pluggy_le(cfg, dias)
    res = {}
    with _ext_trava:
        for nb, b in lido.items():
            banco = next(k for k, v in BANCOS_EXTRATO.items() if v == nb)
            feitos = {}
            for _ in range(1 if simular else max(rodadas, 1)):
                r = _ext_concilia(banco, nb, b["linhas"], simular=simular)
                feitos.update({x["id"]: x for x in r["saidas"]
                               if not simular and x["status"] in ("baixa", "retirada", "ni", "resolvido", "fora")})
                if not r["continua"]:
                    break
            # o que uma rodada fez a seguinte já vê como "lançado": vale o que foi feito
            r["saidas"] = [x if x["status"] == "erro" else feitos.get(x["id"], x) for x in r["saidas"]]
            cont = defaultdict(int)
            for x in r["saidas"]:
                cont[x["status"]] += 1
            r.update(contagem=cont, status=b["status"], atualizado=b["atualizado"],
                     saldo={"valor": b["saldo"], "data": b["atualizado"] or _hoje()} if b["saldo"] is not None else None)
            if not simular and b["saldo"] is not None and b["status"] == "UPDATED":
                try:
                    _pluggy_saldo(nb, b["saldo"], b["atualizado"] or _hoje())
                except Exception:
                    pass            # o saldo volta na próxima leitura
            res[nb] = r
    return res

def _pluggy_resumo(res):
    return {nb: {"status": r["status"], "atualizado": r["atualizado"], "contagem": dict(r["contagem"]),
                 "erro": r["erro"]} for nb, r in res.items()}

def _pluggy_roda():
    try:
        res = _pluggy_sincroniza()
        _pluggy.update(erro=None, bancos=_pluggy_resumo(res))
    except Exception as e:
        _pluggy.update(erro=str(e)[:200])
        print(f"[open finance] erro: {e}", flush=True)
    finally:
        _pluggy.update(rodando=False, quando=time.strftime("%d/%m %H:%M"))

def _pluggy_do_dia():
    """Ao abrir o painel: lê o banco em segundo plano, no máximo de 3 em 3 horas."""
    if not _pluggy_cfg():
        return
    agora = time.time()
    with _pluggy_lock:
        if _pluggy["rodando"] or agora - _pluggy["ultimo"] < PLUGGY_INTERVALO:
            return
        _pluggy.update(rodando=True, ultimo=agora)
    threading.Thread(target=_pluggy_roda, daemon=True).start()

@app.route("/api/banco/status")
@login_required
def api_banco_status():
    cfg = _pluggy_cfg()
    return jsonify({"configurado": bool(cfg), "bancos": [nb for nb, _ in (cfg or {}).get("itens", [])],
                    "rodando": _pluggy["rodando"], "quando": _pluggy["quando"], "erro": _pluggy["erro"],
                    "resumo": _pluggy["bancos"]})

@app.route("/api/banco/ler", methods=["POST"])
@login_required
def api_banco_ler():
    """Botão "Ler o banco agora". simular=true mostra o que faria, sem gravar nada."""
    body = request.get_json(force=True, silent=True) or {}
    if not _pluggy_cfg():
        return jsonify({"ok": False, "erro": "o Open Finance ainda não está ligado"}), 400
    with _pluggy_lock:
        if _pluggy["rodando"]:
            return jsonify({"ok": False, "erro": "já estou lendo o banco agora. Espere um minuto "
                            "e clique de novo."}), 409
        _pluggy.update(rodando=True, ultimo=time.time())
    simular = bool(body.get("simular"))
    dias = max(1, min(int(_num(body.get("dias")) or PLUGGY_DIAS), 60))
    try:
        res = _pluggy_sincroniza(dias, simular=simular, rodadas=2)
        if not simular:
            _pluggy.update(erro=None, bancos=_pluggy_resumo(res))
        return jsonify({"ok": True, "simulado": simular, "dias": dias, "bancos": res,
                        "continua": any(r["continua"] for r in res.values())})
    except Exception as e:
        _pluggy.update(erro=str(e)[:200])
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502
    finally:
        _pluggy.update(rodando=False, quando=time.strftime("%d/%m %H:%M"))

# ---- MEUS LANÇAMENTOS (tudo que foi lançado no mês, num lugar só) ----
_LANC_SO_PAINEL = (RES_DEP_TAG, RES_OUT_TAG, SOB_DEP_TAG, SOB_OUT_TAG)

@app.route("/api/lancamentos")
@login_required
def api_lancamentos():
    """Lista do mês, pela data em que foi LANÇADO (o que o dono fez no mês): saídas,
    contas a pagar, do bolso, movimentos de reserva/sobra e entradas avulsas. Fica de
    fora o automático: venda (cada venda do balcão gera um recebimento), acerto do
    fechamento e a sangria que acompanha um movimento."""
    mes = (request.args.get("mes") or _hoje()[:7])[:7]
    def build():
        y, m = int(mes[:4]), int(mes[5:7])
        fim = f"{mes}-{calendar.monthrange(y, m)[1]:02d}"
        def _quando(r):
            return (r.get("cadastrado_em") or r.get("data_competencia") or "")[:10]
        itens = []
        for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
            d = p.get("descricao") or ""
            du = d.upper()
            if _quando(p)[:7] != mes:
                continue
            if (p.get("nome_plano_conta") or "") == "Ajuste de caixa" or du.startswith("TROCA PIX"):
                continue
            if du.startswith("SANGRIA") and not du.startswith("SANGRIA SAQUE"):
                continue
            if d.startswith((GELO_USO_TAG, SALDO_TAG, NI_TAG, NIX_TAG)) or "[PROVISAO]" in du:
                continue
            aberto = str(p.get("liquidado")) != "1"
            v = round(_num(p.get("valor_total")) or _num(p.get("valor")), 2)
            base = {"id": p.get("id"), "lancado": _quando(p), "valor": v,
                    "forma": _rotulo_forma(p), "pode_excluir": False}
            if d.startswith(_LANC_SO_PAINEL):
                pos = d.startswith((RES_DEP_TAG, SOB_DEP_TAG))
                onde = "Reserva" if d.startswith((RES_DEP_TAG, RES_OUT_TAG)) else "Sobra de caixa"
                tag = next(t for t in _LANC_SO_PAINEL if d.startswith(t))
                itens.append(dict(base, tipo="Movimento", cat=onde, forma="—",
                                  data=(p.get("data_competencia") or "")[:10],
                                  desc=("entrou · " if pos else "saiu · ") + (_sem_tag(d, tag) or ""),
                                  sinal=1 if pos else -1))
                continue
            if du.startswith("SANGRIA SAQUE"):
                itens.append(dict(base, tipo="Entrada", cat="Saque", sinal=0,
                                  data=(p.get("data_liquidacao") or "")[:10],
                                  desc=d[len("SANGRIA "):][:120]))
                continue
            if d.startswith(RP_TAG):
                itens.append(dict(base, tipo="Saída", cat="Do meu bolso", sinal=-1, pode_excluir=True,
                                  data=(p.get("data_competencia") or "")[:10],
                                  desc=EXT_MARCA_RE.sub("", _rp_nota(_sem_cat(d)))))
                continue
            merc = _eh_mercadoria(d)
            itens.append(dict(base, tipo="A pagar" if aberto else "Saída",
                              cat="Mercadoria" if merc else (
                                  (_categoria_conta(p) or _cat_resumo(p)) if aberto else _cat_resumo(p)),
                              data=(p.get("data_vencimento") if aberto else p.get("data_liquidacao")
                                    or p.get("data_vencimento") or "")[:10],
                              desc=EXT_MARCA_RE.sub("", _nota_conta(p)) or p.get("nome_plano_conta") or "—",
                              sinal=-1,
                              # compra de nota mexe no estoque: apaga pela aba Estoque
                              pode_excluir=not d.upper().startswith("COMPRA DE")))
        for r in gcapi.get_all("/recebimentos", {"data_inicio": f"{mes}-01", "data_fim": fim}):
            d = r.get("descricao") or ""
            if not any(d.upper().startswith(t) for t in ENTRADA_TAGS) or _quando(r)[:7] != mes:
                continue
            itens.append({"id": r.get("id"), "lancado": _quando(r), "tipo": "Entrada",
                          "cat": next(t for t in ENTRADA_TAGS if d.upper().startswith(t)).capitalize(),
                          "data": (r.get("data_liquidacao") or r.get("data_vencimento") or "")[:10],
                          "desc": d[:140], "valor": round(_num(r.get("valor_total")) or _num(r.get("valor")), 2),
                          "forma": _rotulo_forma(r), "sinal": 1, "pode_excluir": False})
        itens.sort(key=lambda x: (x["lancado"], x["data"], str(x["id"])), reverse=True)
        for x in itens:                       # o nome que o dono deu (Minhas categorias)
            x["cat"] = _cat_nome(x["cat"])
        return {"mes": mes, "itens": itens, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("lancamentos_" + mes, 60, build))

@app.route("/api/excluir-lancamento", methods=["POST"])
@login_required
def api_excluir_lancamento():
    """Apaga um gasto/conta da lista "Meus lançamentos". Se ele saiu da reserva ou da
    sobra, apaga junto o desconto que ficou lá — senão o saldo guardado ficava menor à toa."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "erro": "sem id"}), 400
    try:
        cur = gcapi.get(f"/pagamentos/{pid}").get("data") or {}
        if isinstance(cur, list):
            cur = cur[0] if cur else {}
        cur = cur.get("Pagamento", cur) if isinstance(cur, dict) else {}
        d = cur.get("descricao") or ""
        if d.upper().startswith(("COMPRA DE", "SANGRIA")) or d.startswith(_LANC_SO_PAINEL):
            return jsonify({"ok": False, "erro": "esse não se apaga por aqui (compra apaga na "
                            "aba Estoque; movimento de reserva/sobra no quadro dela)"}), 400
        par = None
        if str(cur.get("forma_pagamento_id")) == GUARDADO_FORMA_ID and str(cur.get("liquidado")) == "1":
            alvo_v = round(_num(cur.get("valor_total")) or _num(cur.get("valor")), 2)
            alvo_d = (cur.get("data_liquidacao") or cur.get("data_competencia") or "")[:10]
            alvos = {f"{RES_OUT_TAG} {_sem_cat(d)}"[:180], f"{SOB_OUT_TAG} {_sem_cat(d)}"[:180]}
            achados = [p for p in gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                                                 "data_fim": "2027-12-31"})
                       if (p.get("descricao") or "") in alvos
                       and abs((_num(p.get("valor_total")) or _num(p.get("valor"))) - alvo_v) < 0.01
                       and (p.get("data_competencia") or "")[:10] == alvo_d]
            par = achados[0] if len(achados) == 1 else None
        # mesma sequência da rota de excluir conta: desmarca o pago (corpo inteiro) e apaga
        if str(cur.get("liquidado")) == "1":
            gcapi.put(f"/pagamentos/{pid}", {
                "descricao": d, "valor": cur.get("valor") or cur.get("valor_total") or "0",
                "plano_contas_id": cur.get("plano_contas_id") or "",
                "data_vencimento": (cur.get("data_vencimento") or _hoje())[:10],
                "data_competencia": (cur.get("data_competencia") or _hoje())[:10],
                "fornecedor_id": cur.get("fornecedor_id") or "", "liquidado": "0"})
        gcapi.delete(f"/pagamentos/{pid}")
        junto = None
        if par is not None:
            try:
                gcapi.delete(f"/pagamentos/{par.get('id')}")
                junto = "reserva" if (par.get("descricao") or "").startswith(RES_OUT_TAG) else "sobra"
            except Exception:
                junto = None
        _invalida("pagar", "previsoes", "resumo", "gastos_mes", "recurso_proprio", "reserva",
                  "sobra", "mapa", "hoje", "fech_hoje", "fechamentos_7", "fechamentos_12",
                  "fechamentos_31", "lancamentos_" + (body.get("mes") or _hoje()[:7]))
        return jsonify({"ok": True, "devolvido": junto})
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

@app.route("/api/fornecedor", methods=["POST"])
@login_required
def api_fornecedor():
    """Fornecedor novo, direto da tela de compra."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        d = gcapi.post("/fornecedores", {"nome": body.get("nome")})["data"]
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 400
    _invalida("catalogo", "ext_fornecedores")
    return jsonify({"ok": True, "id": str(d["id"]), "nome": d["nome"]})

@app.route("/api/hoje")
@login_required
def api_hoje():
    """Vendas do balcão de hoje, ao vivo, com quebra por forma de pagamento."""
    def build():
        h = _hoje()
        if h >= FECH_GGPT_DESDE:      # caixa do GestãoGPT; "outros" = pendurado no fiado
            vendas = [p for p in _ggpt_todas(f"/pedido?pdv=sim&situacao=todos&data_de={h}&data_ate={h}")
                      if p.get("EMITIDO") and (p.get("DATA") or "")[:10] == h]
            ids = {p["ID"] for p in vendas}
            f = {"dinheiro": 0.0, "pix": 0.0, "cartao": 0.0}
            for p in _ggpt_todas("/conta-parcela?status=pago"):
                k = p.get("CONTA") or {}
                if k.get("ID_PEDIDO") in ids:
                    nome = _sem_acento_up(p.get("FORMAPAGTO") or "")
                    chave = "dinheiro" if "DINHEIRO" in nome else "pix" if "PIX" in nome else "cartao"
                    f[chave] += _num(p.get("VALOR_PAGO"))
            tot = sum(_num(p.get("TOTAL")) for p in vendas)
            return {"n": len(vendas), "total": round(tot, 2), **{k: round(v, 2) for k, v in f.items()},
                    "outros": round(tot - sum(f.values()), 2), "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
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

def _fardos_confirmados():
    """Mapa produto_id -> [fardo=N] que o dono já confirmou, pra ler em lote.

    Serve pro histórico de compras: quando o GestãoClick tem fator 1 no cadastro (cigarro
    de fumo picado, por exemplo), o estoque só entra certo porque a compra do painel
    corrige depois — e a linha da compra fica com a quantidade em CAIXA. Sem esse mapa a
    tela mostraria "1 un" onde entraram 10 maços.
    """
    def build():
        fard = {}
        for p in gcapi.get_all("/produtos"):
            n = _fardo_cadastrado(p)
            if n:
                fard[str(p.get("id"))] = n
        return fard
    return cached("fardos_confirmados", 3600, build)

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

def _qtd_no_fator_do_gc(units, valor, fator):
    """Quantidade a mandar pro GC pra ele mesmo pôr `units` no estoque.

    O GC multiplica a linha pela "unidade de compra" do cadastro dele, que a API não lê
    nem grava. Em 28/09/2026 eram 59 produtos com esse fator diferente do fardo que o
    dono compra (Corona ×1 comprada em fardo de 24, maço ×10 comprado por maço…), então
    toda compra entrava errada e era corrigida depois ("o sistema tinha colocado X —
    corrigi"), sem nunca aprender. Sabendo o fator dele (vem das compras anteriores),
    mando units ÷ fator e o GC acerta de primeira. Só quando a conta fecha exata em 2
    casas (o GC guarda a quantidade assim) e o custo não sobra centavo contra o total —
    senão devolve None e fica o jeito antigo, com a correção depois.
    """
    if not fator or fator <= 0 or units <= 0:
        return None, None
    q = round(units / fator, 2)
    if q <= 0 or abs(q * fator - units) > 0.0005:
        return None, None
    custo = round(valor / q, 4)
    if abs(q * custo - valor) >= 0.005:
        return None, None
    return q, custo

@app.route("/api/compras-painel")
@login_required
def api_compras_painel():
    """Histórico das últimas compras lançadas (pro dono conferir e corrigir)."""
    def build():
        cs = sorted(gcapi.get_all("/compras"),
                    key=lambda c: ((c.get("data_emissao") or ""), str(c.get("id"))), reverse=True)
        ult = cs[:15]
        # A forma de pagamento que vale é a do lançamento FINANCEIRO ("Compra de nº X"),
        # não a cópia que fica dentro da compra: o GC grava essa cópia na hora do
        # lançamento e não mexe mais nela. Quando o pote de uma parcela é acertado depois
        # (compra dividida, ou forma corrigida — Varejão 10/10/2026, dinheiro → PIX), o
        # dinheiro sai do lugar certo mas a cópia continua dizendo a forma antiga.
        fin = {}
        try:
            datas = [(c.get("data_emissao") or "")[:10] for c in ult if c.get("data_emissao")]
            if datas:   # o filtro é por vencimento: boleto de compra vence semanas depois
                fim = (datetime.date.fromisoformat(max(datas))
                       + datetime.timedelta(days=120)).isoformat()
                for p in gcapi.get_all("/pagamentos", {"data_inicio": min(datas), "data_fim": fim}):
                    m = re.search(r"Compra de nº\s*(\d+)", p.get("descricao") or "")
                    if m:
                        fin.setdefault(m.group(1), []).append(
                            {"forma": p.get("nome_forma_pagamento") or "—",
                             "valor": _num(p.get("valor"))})
        except Exception:
            fin = {}    # sem o financeiro, vale a cópia da compra (melhor que tela vazia)
        itens = []
        for c in ult:
            prods = []
            for w in (c.get("produtos") or []):
                p = w.get("produto", w)
                q = _num(p.get("quantidade"))
                det = p.get("detalhes") or ""
                fator = _num(p.get("quantidade_saida")) or 1
                do_painel = "painel" in det
                # Quanto ENTROU no estoque, que é o que o dono quer ver:
                #  · nota de XML já vai gravada em UNIDADE (maço, garrafa) — não multiplica.
                #    Multiplicar mostrava 10 maços de cigarro como "100 un" (fator 10 = o
                #    pacote do cadastro) e parecia entrada errada.
                #  · compra do painel é digitada em FARDO. O GC multiplica pelo fator DELE
                #    quando tem um; quando o cadastro dele está em 1 (Coyote, Porto Faria),
                #    quem acertou o estoque foi a própria rotina da compra, usando o
                #    [fardo=N] confirmado — então é esse número que vale aqui.
                #  · compra nova marca a conta no `detalhes` ("1 x 10 = 10 un"): quando tem
                #    a marca, ela manda, porque é o que aconteceu de fato naquele dia.
                m = re.search(r"=\s*([\d.,]+)\s*un\b", det, re.I)
                # a marca guarda a conta do jeito do DONO ("2 x 24 = 48 un"); a quantidade
                # da linha pode estar no fator do GC (ver _qtd_no_fator_do_gc)
                m2 = re.search(r"([\d.,]+)\s*x\s*([\d.,]+)\s*=\s*([\d.,]+)\s*un\b", det, re.I)
                if m2:
                    q, emb, un = _num(m2.group(1)), _num(m2.group(2)), _num(m2.group(3))
                elif m:
                    un = _num(m.group(1))
                    emb = round(un / q, 2) if q else 1
                elif do_painel:
                    emb = fator if fator > 1 else (_fardos_confirmados().get(str(p.get("produto_id"))) or 1)
                    un = round(q * emb, 2)
                else:
                    emb, un = 1, q
                prods.append({"produto_id": str(p.get("produto_id")), "nome": p.get("nome_produto"),
                              "qtd": q, "fardo": emb, "unidades": un,
                              "multiplicou": emb > 1,
                              "valor": _num(p.get("valor_total")),
                              "unid": p.get("unidade") or "", "painel": do_painel})
            pags = [{"forma": (w.get("pagamento", w)).get("nome_forma_pagamento") or "—",
                     "valor": _num((w.get("pagamento", w)).get("valor"))}
                    for w in (c.get("pagamentos") or [])]
            reais = fin.get(str(c.get("codigo") or "")) or []
            # só troca se achou TODAS as parcelas (a soma fecha com o total da compra)
            if reais and abs(sum(x["valor"] for x in reais) - _num(c.get("valor_total"))) < 0.05:
                pags = reais
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
            # compra nova diz na marca quantas unidades entraram ("= 48 un") — vale mais
            # que q × fardo, porque q pode estar no fator do GC (_qtd_no_fator_do_gc)
            m = re.search(r"=\s*([\d.,]+)\s*un\b", p.get("detalhes") or "", re.I)
            fardo = _fardo_cadastrado(prod) or _num(p.get("quantidade_saida")) or 1
            linhas.append({"pid": pid, "nome": prod.get("nome"), "antes": _num(prod.get("estoque")),
                           "units": _num(m.group(1)) if m else q * fardo})
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
        _invalida("resumo", "pagar", "catalogo", "reserva", "sobra", "abc", "compras_painel", "fatores_compra", "fardos_confirmados", "mapa")
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
    forma = body.get("forma") or ""
    itens = body.get("itens") or []
    if not forn:
        return jsonify({"ok": False, "erro": "escolha o fornecedor"}), 400
    if not itens:
        return jsonify({"ok": False, "erro": "adicione ao menos um item"}), 400
    produtos, total, plano = [], 0.0, []
    try:
        fatores_gc = _fatores_das_compras()
    except Exception:
        fatores_gc = {}      # sem o fator do GC: manda em fardo e corrige depois, como antes
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
        # a conta fica escrita na linha: o fator do cadastro do GC pode ser outro (ou 1),
        # e sem isso o histórico não sabe que 1 caixa foram 10 maços.
        q_gc, custo_gc = _qtd_no_fator_do_gc(units, valor, fatores_gc.get(pid))
        marca = "compra sem nota (painel)"
        if mult > 1 or q_gc is not None:
            marca += f" · {qtd:g} x {mult:g} = {units:g} un"
        produtos.append({"produto": {
            "produto_id": pid,
            "quantidade": q_gc if q_gc is not None else qtd,
            "valor_custo": custo_gc if q_gc is not None else round(valor / qtd, 4),
            "valor_total": round(valor, 2),
            "detalhes": marca,
        }})
        plano.append({"pid": pid, "qtd": qtd, "mult": mult, "units": units, "valor": valor,
                      "antes": _num(_produto_bruto(pid).get("estoque"))})
    # pagamento pode vir dividido: [{"forma": "PIX", "valor": 300}, {"forma": "Caixa", ...}]
    # (o dono às vezes paga parte no PIX e parte em dinheiro). Sem a lista, cai no
    # comportamento antigo: uma forma só, com o total.
    partes = [p for p in (body.get("pagamentos") or []) if _num(p.get("valor")) > 0]
    if not partes:
        partes = [{"forma": forma, "valor": total}]
    # a forma nunca vem marcada (pedido do dono): sem forma, não grava — antes caía em "Caixa"
    if any((p.get("forma") or "") not in POTES and p.get("forma") != "Boleto" for p in partes):
        return jsonify({"ok": False, "erro": "escolha de onde saiu o dinheiro (e, se for PIX, de qual banco)"}), 400
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
            pg = {"data_vencimento": data, "valor": v,
                  "forma_pagamento_id": pot["forma"], "plano_contas_id": "33015669",
                  "conta_bancaria_id": pot["conta"], "liquidado": "1", "data_liquidacao": data}
            if _banco_da_forma(f):
                pg["observacao"] = f                  # "PIX Itaú" / "PIX Nubank"
            pagamentos.append({"pagamento": pg})
    condicao = "a_prazo" if all(p.get("forma") == "Boleto" for p in partes) else "a_vista"
    payload = {
        "codigo": _proximo_codigo_compra(), "fornecedor_id": forn,
        "data_emissao": data, "situacao_id": "1979927", "condicao_pagamento": condicao,
        "valor_produtos": round(total, 2), "valor_total": round(total, 2),
        "produtos": produtos, "pagamentos": pagamentos,
    }
    bancos_usados = sorted({p.get("forma") for p in partes if _banco_da_forma(p.get("forma"))})
    if bancos_usados:
        payload["observacoes_interna"] = "Pago no " + " e no ".join(bancos_usados)
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
        _invalida("resumo", "pagar", "catalogo", "reserva", "sobra", "abc", "mapa",
                  "compras_painel", "fatores_compra", "fardos_confirmados")
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

def _gelo_produtos():
    """Gelo em saco que vem do freezer (GELO 4 KG, 10 KG, 20 KG...), direto do cadastro."""
    out = []
    for p in gcapi.get_all("/produtos"):
        if str(p.get("ativo")) == "1" and _GELO_SACO.match(p.get("nome") or ""):
            out.append({"id": str(p.get("id")), "nome": p.get("nome"),
                        "custo": round(_num(p.get("valor_custo")), 2),
                        "estoque": _num(p.get("estoque"))})
    out.sort(key=lambda x: _num(re.search(r"\d+", x["nome"]).group()))
    return out

def _gelo_produtos_seguro():
    """A lista de produtos do GestãoClick às vezes cai (20/10/2026 ficou a manhã fora);
    o crédito do gelo não depende dela, então o quadro continua de pé sem a lista."""
    try:
        return _gelo_produtos()
    except Exception:
        return []

@app.route("/api/gelo")
@login_required
def api_gelo():
    """Freezer de gelo no mês: crédito pago, gelo que já chegou (abatido) e o que falta usar."""
    def build():
        hoje = _hoje()
        mes = hoje[:7]
        pagos, itens = [], []
        for p in gcapi.get_all("/pagamentos", {"data_inicio": mes + "-01",
                                               "data_fim": _ultimo_dia_mes(hoje)}):
            desc = p.get("descricao") or ""
            data = (p.get("data_competencia") or p.get("data_vencimento") or "")[:10]
            if data[:7] != mes:
                continue
            val = _num(p.get("valor_total")) or _num(p.get("valor"))
            if desc.startswith(GELO_USO_TAG):
                m = _GELO_USO_RE.match(desc)
                itens.append({"id": p.get("id"), "data": data, "valor": val,
                              "qtd": _num(m.group(1)) if m else 0,
                              "nome": m.group(2) if m else _sem_tag(desc, GELO_USO_TAG),
                              "unit": _num(m.group(3)) if m else 0})
            elif GELO_CRED_MARCA in desc.upper() and str(p.get("liquidado")) == "1":
                pagos.append({"id": p.get("id"), "data": data, "valor": val,
                              "forma": p.get("nome_forma_pagamento") or ""})
        credito = round(sum(x["valor"] for x in pagos), 2)
        usado = round(sum(x["valor"] for x in itens), 2)
        itens.sort(key=lambda x: (x["data"], str(x["id"])), reverse=True)
        return {"mes": mes, "mensal": GELO_MENSAL, "credito": credito, "usado": usado,
                "saldo": round(credito - usado, 2), "pagamentos": pagos, "itens": itens,
                "produtos": _gelo_produtos_seguro(),
                "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("gelo", 60, build))

@app.route("/api/gelo-credito", methods=["POST"])
@login_required
def api_gelo_credito():
    """Lança o pagamento mensal do freezer — é ele que vira o crédito de gelo do mês."""
    body = request.get_json(force=True, silent=True) or {}
    valor = round(_num(body.get("valor")) or GELO_MENSAL, 2)
    forma = body.get("forma") or ""
    data = (body.get("data") or _hoje())[:10]
    if valor <= 0:
        return jsonify({"ok": False, "erro": "valor inválido"}), 400
    if forma not in POTES:
        return jsonify({"ok": False, "erro": "escolha de onde saiu o dinheiro"}), 400
    pot = POTES[forma]
    desc = _com_banco(_marca_rp(f"Compra de gelo — freezer consignado · crédito {data[5:7]}/{data[:4]}", forma), forma)
    try:
        r = gcapi.post("/pagamentos", {
            "descricao": desc, "valor": f"{valor:.2f}", "data_vencimento": data,
            "data_competencia": data, "data_liquidacao": data, "liquidado": "1",
            "plano_contas_id": GELO_PLANO, "conta_bancaria_id": pot["conta"],
            "forma_pagamento_id": pot["forma"]})
        d = r.get("data") or {}
        if forma == "Dinheiro":
            _reserva_saida(valor, desc, data)
        elif forma == "Sobra":
            _sobra_saida(valor, desc, data)
        _invalida("gelo", "pagar", "resumo", "reserva", "sobra", "gastos_mes", "mapa",
                  "hoje", "fech_hoje", "fech_" + data, "fechamentos_7",
                  "fechamentos_12", "fechamentos_31")
        return jsonify({"ok": True, "id": d.get("id") if isinstance(d, dict) else None,
                        "valor": valor})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/gelo-entrada", methods=["POST"])
@login_required
def api_gelo_entrada():
    """Chegou gelo: abate do crédito do freezer e dá entrada no estoque com o custo."""
    body = request.get_json(force=True, silent=True) or {}
    pid = str(body.get("produto_id") or "").strip()
    qtd = _num(body.get("qtd"))
    unit = round(_num(body.get("unit")), 2)
    data = (body.get("data") or _hoje())[:10]
    if not pid or qtd <= 0 or qtd != int(qtd):
        return jsonify({"ok": False, "erro": "escolha o gelo e quantos sacos chegaram"}), 400
    if unit <= 0:
        return jsonify({"ok": False, "erro": "informe o preço do saco"}), 400
    qtd = int(qtd)
    try:
        prod = _produto_bruto(pid)
    except Exception:
        return jsonify({"ok": False, "erro": "o GestãoClick não respondeu agora. NÃO gravei "
                        "nada — espere um pouquinho e clique de novo."}), 502
    nome = prod.get("nome") or ""
    if not _GELO_SACO.match(nome):
        return jsonify({"ok": False, "erro": "esse produto não é gelo do freezer"}), 400
    antes = _num(prod.get("estoque"))
    total = round(qtd * unit, 2)
    try:
        r = gcapi.post("/pagamentos", {
            "descricao": f"{GELO_USO_TAG} {qtd} x {nome} · R$ {unit:.2f} cada · #{pid}"[:180],
            "valor": f"{total:.2f}", "data_vencimento": data, "data_competencia": data,
            "liquidado": "0", "plano_contas_id": CATS["Outros"],
            "forma_pagamento_id": BOLETO_FORMA_ID})
        d = r.get("data") or {}
        lid = d.get("id") if isinstance(d, dict) else None
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502
    try:
        gcapi.put(f"/produtos/{pid}", {
            "nome": nome, "codigo_interno": prod.get("codigo_interno"),
            "estoque": str(round(antes + qtd, 2)), "valor_custo": f"{unit:.4f}"})
    except Exception:
        # abater sem dar entrada no estoque gastaria o crédito à toa: desfaz o abatimento
        try:
            if lid:
                gcapi.delete(f"/pagamentos/{lid}")
        except Exception:
            pass
        _invalida("gelo")
        return jsonify({"ok": False, "erro": "não consegui dar entrada no estoque agora — "
                        "não abati nada do crédito. Tente de novo."}), 502
    _invalida("gelo", "resumo", "catalogo", "abc")
    return jsonify({"ok": True, "id": lid, "nome": nome, "qtd": qtd, "total": total,
                    "antes": antes, "depois": antes + qtd})

@app.route("/api/gelo-excluir", methods=["POST"])
@login_required
def api_gelo_excluir():
    """Apaga uma entrega de gelo lançada errada: tira do estoque e devolve o crédito."""
    body = request.get_json(force=True, silent=True) or {}
    lid = str(body.get("id") or "").strip()
    if not lid:
        return jsonify({"ok": False, "erro": "sem id"}), 400
    try:
        cur = gcapi.get(f"/pagamentos/{lid}").get("data") or {}
        if isinstance(cur, list):
            cur = cur[0] if cur else {}
        cur = cur.get("Pagamento", cur) if isinstance(cur, dict) else {}
        m = _GELO_USO_RE.match(cur.get("descricao") or "")
        if not m:
            return jsonify({"ok": False, "erro": "esse lançamento não é uma entrega de gelo"}), 400
        qtd, pid = _num(m.group(1)), m.group(4)
        prod = _produto_bruto(pid)
        antes = _num(prod.get("estoque"))
        base = {"nome": prod.get("nome"), "codigo_interno": prod.get("codigo_interno")}
        gcapi.put(f"/produtos/{pid}", dict(base, estoque=str(round(antes - qtd, 2))))
        try:
            gcapi.delete(f"/pagamentos/{lid}")
        except Exception:
            gcapi.put(f"/produtos/{pid}", dict(base, estoque=str(antes)))   # volta o estoque
            raise
        _invalida("gelo", "resumo", "catalogo", "abc")
        return jsonify({"ok": True, "nome": prod.get("nome"), "qtd": qtd,
                        "antes": antes, "depois": antes - qtd})
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

MOEDAS_TAG = "FUNDO DE MOEDAS"   # marcador com o valor das moedas que ficam na gaveta

def _fundos_lista():
    """Todas as contagens de moeda já registradas, como [(data, valor)] ordenadas.

    Guardo em /pagamentos (não em /recebimentos): recebimento tem uma linha por venda,
    são milhares, e a busca estourava o limite de páginas sem nunca achar o marcador.
    """
    def build():
        out = []
        try:
            pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                                "data_fim": "2027-12-31"})
        except Exception:
            return out
        for p in pgs:
            desc = (p.get("descricao") or "")
            if not desc.upper().startswith(MOEDAS_TAG):
                continue
            d = (p.get("data_competencia") or p.get("data_vencimento") or "")[:10]
            m = re.search(r"R\$ ?([\d.,]+)", desc)
            if d and m:
                out.append((d, _num(m.group(1))))
        out.sort()
        return out
    return cached("fundos_moedas", 120, build)

def _fundo_moedas(data=None):
    """Quanto tem de MOEDA na gaveta na data pedida (o fundo de troco miúdo).

    O dono conta as moedas uma vez por semana e elas NÃO entram na abertura que ele
    lança no PDV — ficam na gaveta o tempo todo. Então o esperado do fechamento tem
    que somar esse fundo, senão toda contagem fecha com sobra do tamanho das moedas.
    Uso a contagem mais recente ATÉ aquele dia: assim os fechamentos antigos, feitos
    antes de existir o fundo, continuam com o mesmo esperado de quando foram conferidos.
    """
    data = (data or _hoje())[:10]
    valor, quando = 0.0, None
    for d, v in _fundos_lista():
        if d <= data:
            valor, quando = v, d
    return round(valor, 2), quando

@app.route("/api/fundo-moedas", methods=["GET", "POST"])
@login_required
def api_fundo_moedas():
    """GET: quanto tem de moeda na gaveta hoje. POST: o dono contou e atualiza."""
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        valor = round(_num(body.get("valor")), 2)
        data = (body.get("data") or _hoje())[:10]
        if valor < 0:
            return jsonify({"ok": False, "erro": "valor inválido"}), 400
        try:
            gcapi.post("/pagamentos", {
                "descricao": f"{MOEDAS_TAG} R$ {valor:.2f} (contadas em {data[8:10]}/{data[5:7]}/{data[:4]})",
                "valor": "0.00", "data_vencimento": data, "data_competencia": data,
                "data_liquidacao": data, "liquidado": "1",
                "plano_contas_id": AJUSTE_CAIXA_PLANO, "conta_bancaria_id": GAVETA_CONTA,
                "forma_pagamento_id": "6055919"})
            _invalida("fech_hoje", "fech_" + data, "fechamentos_7", "fechamentos_12",
                      "fechamentos_31", "moedas", "fundos_moedas")
            return jsonify({"ok": True, "valor": valor, "desde": data})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 502
    def build():
        v, d = _fundo_moedas()
        return {"valor": v, "desde": d, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
    return jsonify(cached("moedas", 60, build))

def _fech_calc(data):
    """Dinheiro que ENTROU (vendas em dinheiro, balcão + delivery) e SAÍDAS em
    dinheiro do caixa no dia. O delivery em dinheiro entra na MESMA gaveta (o
    entregador traz), então conta pro fechamento."""
    def _cancelada(v):
        # venda cancelada não pôs dinheiro na gaveta. Hoje o GC costuma tirá-la da
        # listagem, mas quando ela vem o esperado do dia sai inflado e o dono leva
        # a culpa por uma "falta" que não existe.
        return "cancel" in (v.get("nome_situacao") or "").lower()

    din = 0.0
    for v in gcapi.get_all("/vendas", {"tipo": "vendas_balcao", "data_inicio": data, "data_fim": data}):
        if _cancelada(v):
            continue
        for w in v.get("pagamentos") or []:
            p = w.get("pagamento", w)
            if "DINHEIRO" in (p.get("nome_forma_pagamento") or "").upper():
                din += _num(p.get("valor"))
    for v in gcapi.get_all("/vendas", {"tipo": "produto", "data_inicio": data, "data_fim": data}):
        if "ANOTA AI" not in (v.get("observacoes") or "").upper():
            continue  # só delivery (não saque, que já é sangria)
        if _cancelada(v):
            continue
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

# ---- fechamento depois da virada pro GestãoGPT (dono, 30/10/2026: "já migramos") ----
# A gaveta é a conta corrente CAIXA PDV/DINHEIRO de lá. Abrir o caixa vira "PDV DIF. ABERTURA"
# e fechar vira "PDV SANGRIA FECHAMENTO", sem hora: a ordem vem do número da conta. Dinheiro
# de fiado recebido lá já entra aqui. Do GestãoClick ainda vêm, só lendo, o delivery em
# dinheiro (o robô ainda lança lá) e o que sai pelo + Lançar, até essas partes migrarem.
FECH_GGPT_DESDE = "2026-10-30"
GGPT_CC_GAVETA = 2
GGPT_CTRL_FECH = 405        # produto técnico inativo: a contagem de cada dia, em JSON (até 3.500 letras)
GGPT_CTRL_FECH_HIST = 420     # idem: os 14 dias antes da virada
FECH_AJUSTE_TAG = "FECHAMENTO "

def _ggpt_gaveta_movs():
    out = []
    for p in _ggpt_todas(f"/conta-parcela?status=pago&id_conta_corrente={GGPT_CC_GAVETA}"):
        dia, k = (p.get("DATA_PAGTO") or "")[:10], p.get("CONTA") or {}
        if dia >= FECH_GGPT_DESDE:
            out.append({"dia": dia, "id": k.get("ID") or 0, "tipo": k.get("TIPO"),
                        "fatura": (k.get("FATURA") or "").upper(),
                        "valor": round(_num(p.get("VALOR_PAGO")), 2)})
    out.sort(key=lambda m: (m["dia"], m["id"]))
    return out

def _ggpt_gaveta_dia(data, movs):
    """Saldo que veio do dia anterior + aberturas, dinheiro e saídas do dia. A sangria que
    FECHOU o caixa (a última coisa do dia) fica de fora: é a retirada depois da contagem."""
    sinal = lambda m: m["valor"] if m["tipo"] == "R" else -m["valor"]
    anterior = sum(sinal(m) for m in movs if m["dia"] < data)
    dia = [m for m in movs if m["dia"] == data]
    marcos = [m for m in dia if "PDV DIF. ABERTURA" in m["fatura"] or "SANGRIA FECHAMENTO" in m["fatura"]]
    final = marcos[-1] if marcos and "SANGRIA FECHAMENTO" in marcos[-1]["fatura"] else None
    # contado diferente do esperado na tela deles vira "PDV DIF. FECHAMENTO": a do último
    # fechamento é depois da contagem, igual à sangria final. A de um turno anterior fica na conta.
    desde = marcos[-2]["id"] if final and len(marcos) > 1 else 0
    abertura, din, saidas = anterior, 0.0, 0.0
    for m in dia:
        if m is final or m["fatura"].startswith(FECH_AJUSTE_TAG):
            continue
        if final and "PDV DIF. FECHAMENTO" in m["fatura"] and m["id"] > desde:
            continue
        if "PDV DIF. ABERTURA" in m["fatura"]:
            abertura += m["valor"]
        elif m["tipo"] == "R":
            din += m["valor"]
        else:
            saidas += m["valor"]
    return {"abertura": round(abertura, 2), "dinheiro": round(din, 2), "saidas": round(saidas, 2),
            "abriu": any("PDV DIF. ABERTURA" in m["fatura"] for m in dia)}

def _ctrl_fech_ler():
    import json as _json
    try:
        return _json.loads(_ggpt_req("GET", f"/produto/{GGPT_CTRL_FECH}").get("INFOADICIONAL") or "{}")
    except ValueError:
        return {}

def _ctrl_fech_grava(reg):
    import json as _json
    reg = dict(sorted(reg.items())[-60:])     # 60 dias cabem no campo
    _ggpt_req("PUT", f"/produto/{GGPT_CTRL_FECH}", {"infoadicional": _json.dumps(reg, separators=(",", ":"))})

def _fech_dia(data):
    """(troco, abriu, dinheiro, saidas) do dia, pelo caixa do GestãoGPT. Tudo que mexe na gaveta
    (venda, delivery, fiado, + Lançar) já cai nessa conta corrente."""
    g = _ggpt_gaveta_dia(data, _ggpt_gaveta_movs())
    return g["abertura"], g["abriu"], g["dinheiro"], g["saidas"]

def _fech_historico():
    """Dias antes da virada: foto tirada do GestãoClick em 30/10 (controle FECH HIST)."""
    import json as _json
    try:
        return _json.loads(_ggpt_req("GET", f"/produto/{GGPT_CTRL_FECH_HIST}").get("INFOADICIONAL") or "{}")
    except (ValueError, RuntimeError):
        return {}

@app.route("/api/fechamento-hoje")
@login_required
def api_fechamento_hoje():
    """Quanto DEVERIA ter na gaveta no DIA escolhido (?data=AAAA-MM-DD, default hoje)
    = abertura + dinheiro que entrou − saídas em dinheiro, tudo daquele dia."""
    data = (request.args.get("data") or _hoje())[:10]
    def build():
        if data >= FECH_GGPT_DESDE:
            troco, abriu, din, saidas = _fech_dia(data)
            moedas, moedas_desde = _fundo_moedas(data)
            return {"data": data, "troco": round(troco, 2), "abertura_gc": True, "abriu": abriu,
                    "dinheiro": din, "saidas": saidas, "moedas": moedas, "moedas_desde": moedas_desde,
                    "esperado": round(troco + moedas + din - saidas, 2),
                    "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
        ab = _abertura_caixa(data)                  # abertura real lançada no GC no dia
        troco = ab if ab is not None else (_num(request.args.get("troco")) or 200.0)
        din, saidas = _fech_calc(data)              # vendas em dinheiro + saídas DO DIA
        moedas, moedas_desde = _fundo_moedas(data)  # troco miúdo que mora na gaveta
        return {"data": data, "troco": round(troco, 2), "abertura_gc": ab is not None,
                "dinheiro": din, "saidas": saidas, "moedas": moedas,
                "moedas_desde": moedas_desde,
                "esperado": round(troco + moedas + din - saidas, 2),
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
        # vendas do caixa novo vêm das saídas de estoque: /vendas de lá não traz os produtos
        for m in gcapi.saidas_de_venda(ini30):
            un30[m["pid"]] += m["qtde"]
            fat30[m["pid"]] += m["valor"]
            if m["dia"] >= d7:
                un7[m["pid"]] += m["qtde"]
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
        movs, ctrl, hist = _ggpt_gaveta_movs(), _ctrl_fech_ler(), _fech_historico()
        itens = []
        for i in range(dias):
            d = (hoje - datetime.timedelta(days=dias - 1 - i)).isoformat()
            if d < FECH_GGPT_DESDE:
                h = hist.get(d)
                if h:
                    c = h[4]
                    itens.append({"data": d, "abertura": h[0], "abertura_real": True, "fundo_moedas": h[1],
                                  "dinheiro": h[2], "saidas": h[3], "esperado": round(h[0] + h[1] + h[2] - h[3], 2),
                                  "contado": c, "quebra": None if c is None else round(c - (h[0] + h[1] + h[2] - h[3]), 2),
                                  "moedas": None})
                continue
            g = _ggpt_gaveta_dia(d, movs)
            fundo, _fd = _fundo_moedas(d)
            esp = round(g["abertura"] + fundo + g["dinheiro"] - g["saidas"], 2)
            c = ctrl[d][0] if d in ctrl else None
            itens.append({"data": d, "abertura": g["abertura"], "abertura_real": g["abriu"], "fundo_moedas": fundo,
                          "dinheiro": g["dinheiro"], "saidas": g["saidas"], "esperado": esp, "contado": c,
                          "quebra": None if c is None else round(c - esp, 2),
                          "moedas": (ctrl[d][2] or None) if d in ctrl else None})
        ult_moedas = next((x["moedas"] for x in reversed(itens) if x.get("moedas") is not None), None)
        return {"itens": itens, "ultimas_moedas": ult_moedas, "gerado_em": time.strftime("%d/%m/%Y %H:%M")}
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
    if data < FECH_GGPT_DESDE:
        return jsonify({"ok": False, "erro": "dias antes de 30/10 ficaram no GestãoClick, que saiu de uso."}), 400
    return _fechamento_ggpt(data, body)
    try:
        ab = _abertura_caixa(data, tolerante=False)  # usa a abertura real do GC
    except Exception:
        return jsonify({"ok": False, "erro": "não consegui ler a abertura do caixa desse dia "
                        "no sistema agora. Tente de novo em alguns segundos — assim eu não "
                        "fecho o dia com o troco errado."}), 502
    try:
        din, saidas = _fech_calc(data)               # vendas e saídas do dia
    except Exception:
        return jsonify({"ok": False, "erro": "o GestãoClick não respondeu agora (ele cai por "
                        "alguns segundos de vez em quando). NÃO gravei nada — espere um "
                        "pouquinho e clique de novo."}), 502
    troco = ab if ab is not None else (_num(body.get("troco")) or 200.0)
    contado = _num(body.get("contado"))
    if body.get("contado") in (None, "") or contado <= 0:
        # contado 0 passava direto e gravava uma "falta" do valor inteiro do dia
        return jsonify({"ok": False, "erro": "conte a gaveta primeiro — o valor contado não "
                        "pode ficar em branco nem zerado."}), 400
    # Dia SEM abertura e SEM nenhum movimento = quase sempre o dono errou o campo Dia. Se
    # deixar passar, o esperado cai no chute de R$ 200 e a quebra sai gigante e falsa —
    # foi o que criou uma "sobra" de R$ 319 em 20/08/2026 (fechamento feito na noite do 19).
    if ab is None and din == 0 and saidas == 0 and not body.get("forcar_dia_vazio"):
        return jsonify({"ok": False, "dia_vazio": True,
                        "erro": f"o dia {data[8:10]}/{data[5:7]} não tem abertura de caixa nem "
                                "nenhuma venda em dinheiro no sistema. Confira se a data está "
                                "certa antes de fechar."}), 400
    fundo, _ = _fundo_moedas(data)          # as moedas moram na gaveta e não entram na abertura
    esperado = round(troco + fundo + din - saidas, 2)
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

def _fechamento_ggpt(data, body):
    """Fecha o dia no GestãoGPT: a contagem vai pro controle e a quebra vira um lançamento na
    gaveta (de lá não aceita valor zero: caixa que bate não lança nada). Não grava no GC."""
    contado = _num(body.get("contado"))
    if body.get("contado") in (None, "") or contado <= 0:
        return jsonify({"ok": False, "erro": "conte a gaveta primeiro — o valor contado não "
                        "pode ficar em branco nem zerado."}), 400
    try:
        ctrl = _ctrl_fech_ler()
        if data in ctrl:
            return jsonify({"ok": False, "erro": f"o dia {data[8:10]}/{data[5:7]} já foi fechado com "
                            f"R$ {ctrl[data][0]:.2f} contados. Não gravei de novo, pra não lançar a "
                            "quebra duas vezes."}), 400
        troco, abriu, din, saidas = _fech_dia(data)
        fundo, _ = _fundo_moedas(data)
    except Exception:
        return jsonify({"ok": False, "erro": "o sistema não respondeu agora. NÃO gravei nada — "
                        "espere um pouquinho e clique de novo."}), 502
    if not abriu and din == 0 and saidas == 0 and not body.get("forcar_dia_vazio"):
        return jsonify({"ok": False, "dia_vazio": True,
                        "erro": f"o dia {data[8:10]}/{data[5:7]} não tem abertura de caixa nem "
                                "nenhuma venda em dinheiro. Confira se a data está certa antes de fechar."}), 400
    esperado = round(troco + fundo + din - saidas, 2)
    quebra = round(contado - esperado, 2)
    try:
        if abs(quebra) >= 0.01:
            r = _ggpt_req("POST", "/conta", {
                "tipo": "receber" if quebra > 0 else "pagar", "origem": "OUT", "data_lanc": data,
                "fatura": f"{FECH_AJUSTE_TAG}{data} {'SOBRA' if quebra > 0 else 'FALTA'} contado {contado:.2f} esperado {esperado:.2f}",
                "parcelas": [{"data_venc": data, "valor": abs(quebra), "forma_pagto": "DINHEIRO",
                              "id_conta_corrente": GGPT_CC_GAVETA}]})
            _ggpt_req("POST", f"/conta/{r['ID']}/baixar", {"data_pagto": data, "id_conta_corrente": GGPT_CC_GAVETA})
        ctrl[data] = [round(contado, 2), esperado, round(_num(body.get("moedas")), 2)]
        _ctrl_fech_grava(ctrl)
    except Exception as e:
        return jsonify({"ok": False, "erro": "não terminei de gravar: " + str(e)[:150] +
                        ". Me avise antes de fechar de novo."}), 502
    _invalida("resumo", "fech_" + data, "fechamentos_12", "fechamentos_7", "fechamentos_31")
    return jsonify({"ok": True, "esperado": esperado, "contado": round(contado, 2), "quebra": quebra,
                    "dinheiro": din, "saidas": saidas, "ajuste_id": None})

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
    if c and c not in ("Retirada Victor", "Retirada do sócio (Victor)", "PH Motoca",
                       "Biel (Gabriel)", "Igor (pró-labore / retirada)", "Motoboy / entrega"):
        return c
    return "Gastos adicionais"

# de onde saiu o dinheiro, com os nomes que o dono usa no formulário
_FORMA_ROTULO = {"6055919": "Caixa (gaveta)", "6780142": "Dinheiro guardado",
                 "6055931": "PIX", "6776761": "Recurso próprio", "6687681": "Boleto"}

def _rotulo_forma(p):
    r = (_FORMA_ROTULO.get(str(p.get("forma_pagamento_id") or ""))
         or p.get("nome_forma_pagamento") or "—")
    if r == "PIX":
        b = _banco_do_texto(p.get("descricao"))
        if b:
            r = f"PIX {b}"
    return r

def _no_gasto_do_mes(p, mes):
    """O valor do lançamento, se ele entra no "Total gasto no mês" de `mes`; senão None.
    A mesma régua vale pro teto de cada categoria (os dois têm que bater)."""
    if str(p.get("liquidado")) != "1":
        return None       # só o que já saiu da conta (pago)
    if (p.get("data_liquidacao") or "")[:7] != mes:
        return None       # pago dentro deste mês
    desc = (p.get("descricao") or "").strip()
    if desc.upper().startswith("SANGRIA"):
        return None       # saque e dinheiro guardado: troca de lugar, não é gasto
    if (p.get("nome_plano_conta") or "") == "Ajuste de caixa":
        return None       # acerto de gaveta (quebra) não é gasto
    return _num(p.get("valor_total")) or _num(p.get("valor"))

def _gasto_por_cat(pgs, mes):
    """Quanto já saiu no mês em cada categoria (pela chave), fora a mercadoria."""
    out = defaultdict(float)
    for p in pgs:
        v = _no_gasto_do_mes(p, mes)
        if v is None or _eh_mercadoria(p.get("descricao")):
            continue
        ch = _cat_marcada(p.get("descricao")) or _categoria_conta(p)
        if ch:
            out[ch] += v
    return out

def _mes_anterior(mes):
    y, m = int(mes[:4]), int(mes[5:7])
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"

# ---- TETO DE GASTO (fase 2, 20/10/2026) ----
# O dono põe um teto por mês em cada categoria (Minhas categorias). O painel mostra quanto
# já foi: perto = 80% do teto, passou = acima. Só avisa, nunca trava o lançamento.
TETO_PERTO = 80

def _tetos(por_cat, por_cat_ant=None):
    """Cada categoria com o que já saiu no mês e, se tiver teto, o nível: ok/perto/passou."""
    out = []
    for it in _categ_lista():
        g = round(por_cat.get(it["chave"], 0.0), 2)
        d = {"chave": it["chave"], "cat": it["nome"], "teto": it["teto"], "gasto": g,
             "ativa": it["ativa"]}
        if por_cat_ant is not None:
            d["gasto_ant"] = round(por_cat_ant.get(it["chave"], 0.0), 2)
        if it["teto"]:
            d["pct"] = round(g / it["teto"] * 100, 1)
            d["nivel"] = ("passou" if g > it["teto"] + 0.004
                          else "perto" if d["pct"] >= TETO_PERTO else "ok")
        out.append(d)
    return out

def _gastos_mes_build():
    """O que JÁ SAIU no mês, ITEMIZADO pelos nomes do dono (os fixos sempre listados,
    começam zerados e sobem conforme ele lança) + mercadoria (compras) + TOTAL GERAL.
    Fora: ajuste de caixa (quebra) e sangria. Cada linha leva o teto das categorias
    que caem nela."""
    pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                        "data_fim": "2027-12-31"})
    mes = _hoje()[:7]
    buckets = {k: 0.0 for k in RESUMO_FIXOS}   # nomes fixos sempre aparecem
    extras, compras = {}, 0.0
    lanc = {}       # cada linha guarda os lançamentos do mês (histórico com a setinha)
    for p in pgs:
        val = _no_gasto_do_mes(p, mes)
        if val is None:
            continue
        desc = (p.get("descricao") or "").strip()
        if _eh_mercadoria(desc):
            compras += val  # mercadoria: soma só no total geral
            continue
        cat = _cat_resumo(p)
        if cat in buckets:
            buckets[cat] += val
        else:
            extras[cat] = extras.get(cat, 0.0) + val
        nota = _nota_conta(p)
        # a descrição que é só o nome da linha (o de hoje ou um antigo) não é nota. Numa
        # linha que junta várias (Lanches = lanche + almoço + padaria) o nome da própria
        # categoria fica, com o nome de hoje: é ele que diz o que foi.
        propria = _cat_marcada(desc)
        if _norm_cat(nota) in _nomes_cat(cat):
            nota = ""
        elif propria and _norm_cat(nota) in _nomes_cat(propria):
            nota = _cat_nome(propria)
        lanc.setdefault(cat, []).append({
            "id": p.get("id"), "data": (p.get("data_liquidacao") or "")[:10],
            "valor": round(val, 2), "forma": _rotulo_forma(p), "nota": nota})
    for L in lanc.values():
        L.sort(key=lambda x: (x["data"], str(x["id"])), reverse=True)   # mais recente primeiro
    # teto: a linha mostra o das categorias que caem nela (Lanches = lanche, almoço e padaria)
    tetos = _tetos(_gasto_por_cat(pgs, mes), _gasto_por_cat(pgs, _mes_anterior(mes)))
    da_linha = defaultdict(list)
    for t in tetos:
        if t["teto"]:
            da_linha[CAT_RESUMO.get(t["chave"], t["chave"])].append(t)
    for k in da_linha:            # categoria com teto aparece mesmo zerada: ele está de olho
        if k not in buckets and k not in extras:
            extras[k] = 0.0
    def _escondida(k):        # linha fixa de categoria que o dono escondeu, zerada, sai
        it = _cat_item(k)
        return bool(it and not it["ativa"] and not buckets[k] and not da_linha.get(k))
    # "cat" = o nome que o dono deu pra categoria em Minhas categorias
    itens = [{"cat": _cat_nome(k), "total": round(buckets[k], 2), "fixo": True,
              "lancamentos": lanc.get(k, []), "tetos": da_linha.get(k, [])}
             for k in RESUMO_FIXOS if not _escondida(k)]
    itens += sorted([{"cat": _cat_nome(k), "total": round(v, 2), "fixo": False,
                      "lancamentos": lanc.get(k, []), "tetos": da_linha.get(k, [])}
                     for k, v in extras.items()], key=lambda x: -x["total"])
    total = round(sum(buckets.values()) + sum(extras.values()), 2)
    return {"itens": itens, "total": total,
            "compras": round(compras, 2),
            "total_geral": round(total + compras, 2), "mes": mes,
            "mes_ant": _mes_anterior(mes), "tetos": tetos,
            "gerado_em": time.strftime("%d/%m/%Y %H:%M")}

@app.route("/api/gastos-mes")
@login_required
def api_gastos_mes():
    """Resumo "Total gasto no mês" (aba Contas)."""
    return jsonify(cached("gastos_mes", 60, _gastos_mes_build))

@app.route("/api/tetos")
@login_required
def api_tetos():
    """Quanto já saiu no mês em cada categoria e o teto dela: a Saída mostra ao escolher
    a categoria e Minhas categorias mostra pra ajudar a decidir o teto."""
    d = cached("gastos_mes", 60, _gastos_mes_build)
    return jsonify({"mes": d["mes"], "mes_ant": d.get("mes_ant"), "itens": d.get("tetos") or []})

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
ENTRADA_TAGS = ("PAGAMENTO DE CONTA", "TROCA PIX", "REFORÇO DE CAIXA", "ENTRADA AVULSA")
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
    banco = (body.get("banco") or "").strip()
    if banco and banco not in BANCOS:
        return jsonify({"ok": False, "erro": "banco inválido"}), 400
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
            "descricao": f"TROCA PIX R$ {enviado:.2f} — PIX enviado ao cliente" + (f" (PIX {banco})" if banco else ""),
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
    forma = (body.get("forma") or "")
    data = (body.get("data") or _hoje())[:10]
    if valor <= 0:
        return jsonify({"ok": False, "erro": "informe o valor recebido"}), 400
    if forma not in ("Caixa", "PIX") and not _banco_da_forma(forma):
        return jsonify({"ok": False, "erro": "escolha onde recebeu (gaveta ou PIX de qual banco)"}), 400
    try:
        rid = _lanca_pagamento_conta(valor, cliente, forma, data)
        return jsonify({"ok": True, "valor": valor, "cliente": cliente, "forma": forma, "id": rid})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

def _lanca_pagamento_conta(valor, cliente, forma, data):
    """Grava no GestãoClick o dinheiro do fiado que entrou (gaveta ou PIX), sem venda nova."""
    pot = POTES.get(forma, POTES["Caixa"])
    desc = _com_banco("PAGAMENTO DE CONTA" + (f" — {cliente}" if cliente else "") + " (abatimento de fiado)", forma)
    r = gcapi.post("/recebimentos", {
        "descricao": desc[:180], "valor": f"{valor:.2f}",
        "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
        "liquidado": "1", "plano_contas_id": PLANO_VENDA_BALCAO,
        "conta_bancaria_id": pot["conta"], "forma_pagamento_id": pot["forma"]})
    _invalida("resumo", "hoje", "fech_hoje", "fechamentos_7", "fechamentos_12", "fechamentos_31")
    d = r.get("data") or {}
    return d.get("id") if isinstance(d, dict) else None

# ---- FIADO (no sistema novo, GestãoGPT) ----
# 29/10/2026: o fiado saiu do caderno e foi pro GestãoGPT, o sistema em teste pra substituir o
# GestãoClick. Cada cliente do fiado é um cliente de lá e o que ele deve é conta a receber em
# aberto no nome dele (o saldo do caderno entrou como "SALDO ANTERIOR FIADO").
# O dono pediu: pagamento e fiado novo ele lança no GestãoGPT; o painel só ACOMPANHA (lê).
# Victor (LUCCHESI) e o sócio Igor ficam à parte: é consumo dos sócios, não dinheiro de cliente
# pra receber, e cada um tem a sua linha (o dono pediu pra NÃO somar os dois).
# No servidor: GESTAOGPT_TOKEN (só no Render e no .env, nunca no código).
GGPT_API = "https://api.gestaogpt.com.br/v1"
GGPT_SOCIOS = {"LUCCHESI": "Victor", "IGOR": "Igor"}
GGPT_CONSUMIDOR = 1                 # CONSUMIDOR BALCÃO: venda sem cliente, não é fiado

def _ggpt_req(metodo, caminho, corpo=None):
    import json as _json, urllib.request, urllib.error
    tok = gcapi._tok("GESTAOGPT_TOKEN")
    if not tok:
        raise RuntimeError("a chave do sistema novo não está configurada no servidor")
    # User-Agent próprio: o Cloudflare deles barra o do Python (erro 1010). Sem corpo, vai sem
    # Content-Type (DELETE com Content-Type volta 400).
    h = {"Authorization": "Bearer " + tok, "Accept": "application/json",
         "User-Agent": "DistribuidoraPraca031/1.0"}
    if corpo is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(GGPT_API + caminho, method=metodo, headers=h,
                                 data=_json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = _json.load(e).get("message")
        except Exception:
            msg = None
        raise RuntimeError(msg or f"o sistema novo respondeu {e.code} em {caminho.split('?')[0]}") from None

def _ggpt_todas(caminho):
    out, page = [], 1
    sep = "&" if "?" in caminho else "?"
    while True:
        j = _ggpt_req("GET", f"{caminho}{sep}count=100&page={page}")
        out += j.get("items") or []
        if not j.get("items") or len(out) >= (j.get("total") or 0):
            return out
        page += 1

def _fiado_dados():
    """Quem deve quanto e desde quando; atrasado = passou do vencimento que está no GestãoGPT.
    Cliente de teste (nome TESTE…) fica fora da tela."""
    clientes = [c for c in _ggpt_todas("/cliente")
                if c["ID"] != GGPT_CONSUMIDOR and not (c.get("FANTASIA") or "").upper().startswith("TESTE")]
    atrasadas = {k["ID"] for k in _ggpt_todas("/conta?tipo=receber&status=atrasado")}
    por = {}
    for k in _ggpt_todas("/conta?tipo=receber&status=aberto"):
        if (k.get("ORIGEM") or "").strip() != "CLI":
            continue
        d = por.setdefault(k.get("ID_EMPRESA"), {"aberto": 0.0, "desde": "", "atrasado": False})
        d["aberto"] += _num(k.get("VALOR_EM_ABERTO"))
        dl = (k.get("DATA_LANC") or "")[:10]
        d["desde"] = min(d["desde"] or dl, dl)
        d["atrasado"] = d["atrasado"] or k["ID"] in atrasadas
    lista, socios = [], []
    for c in clientes:
        nome = (c.get("FANTASIA") or c.get("RAZAOSOCIAL") or "").strip()
        d = por.get(c["ID"]) or {}
        item = {"id": c["ID"], "nome": nome, "aberto": round(d.get("aberto", 0.0), 2),
                "desde": d.get("desde") or None, "atrasado": bool(d.get("atrasado"))}
        socio = GGPT_SOCIOS.get(nome.upper())
        if socio:
            item["socio"] = socio
            socios.append(item)
        else:
            lista.append(item)
    lista.sort(key=lambda x: (-x["aberto"], x["nome"]))
    ordem = list(GGPT_SOCIOS.values())
    socios.sort(key=lambda x: ordem.index(x["socio"]))
    return {"clientes": lista, "socios": socios,
            "total": round(sum(x["aberto"] for x in lista), 2),
            "devendo": sum(1 for x in lista if x["aberto"] > 0.004)}

@app.route("/api/fiado")
@login_required
def api_fiado():
    try:
        return jsonify({"ok": True, **cached("fiado", 60, _fiado_dados)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

_ggpt_ped = {}      # venda do caixa já lida {id: (quando, pedido)}: venda emitida não muda

def _ggpt_pedido(pid):
    hit = _ggpt_ped.get(pid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    p = _ggpt_req("GET", f"/pedido/{pid}")
    _ggpt_ped[pid] = (time.time(), p)
    return p

@app.route("/api/fiado/cliente/<int:cid>")
@login_required
def api_fiado_compras(cid):
    """A setinha do cliente: o que está em aberto no nome dele, compra por compra, com os
    produtos. Pagou no GestãoGPT, a compra some daqui (só entra conta em aberto)."""
    try:
        out = []
        for k in _ggpt_todas(f"/conta?tipo=receber&status=aberto&id_cliente={cid}"):
            if (k.get("ORIGEM") or "").strip() != "CLI":
                continue
            c = {"data": (k.get("DATA_LANC") or "")[:10], "hora": "", "tipo": "outro", "itens": [],
                 "texto": (k.get("FATURA") or "").strip(), "total": round(_num(k.get("VALOR")), 2),
                 "deve": round(_num(k.get("VALOR_EM_ABERTO")), 2)}
            if k.get("ID_PEDIDO"):
                p = _ggpt_pedido(k["ID_PEDIDO"])
                d = p.get("DATA") or ""      # vem com "Z" mas já é hora de Brasília: não converter
                c.update(tipo="venda", numero=p.get("NUMERO"), data=d[:10] or c["data"], hora=d[11:16],
                         total=round(_num(p.get("TOTAL")) or c["total"], 2),   # parte pode ter sido paga na hora
                         itens=[{"nome": (i.get("DESCRICAO") or "").strip(), "qtde": _num(i.get("QTDE")),
                                 "valor": round(_num(i.get("VALORTOTAL")), 2)}
                                for i in p.get("ITENS") or [] if (i.get("STATUS") or "") != "CANCELADO"])
            elif c["texto"].upper().startswith("SALDO ANTERIOR"):
                c["tipo"] = "caderno"
            out.append(c)
        out.sort(key=lambda x: (x["data"], x["hora"]))
        return jsonify({"ok": True, "compras": out, "deve": round(sum(x["deve"] for x in out), 2)})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:200]}), 502

@app.route("/api/reforco-caixa", methods=["POST"])
@login_required
def api_reforco_caixa():
    """Tira dinheiro do guardado (reserva ou sobra de caixa) e põe na GAVETA.

    É o contrário da sangria: acontece quando sobra um troco quebrado da compra
    (R$ 6, R$ 7) e esse dinheiro volta pro caixa. Entra como entrada avulsa —
    o fechamento do dia passa a contar com ele.
    """
    body = request.get_json(force=True, silent=True) or {}
    valor = round(_num(body.get("valor")), 2)
    fonte = (body.get("fonte") or "reserva").strip().lower()
    data = (body.get("data") or _hoje())[:10]
    nota = (body.get("descricao") or "").strip()
    if valor <= 0:
        return jsonify({"ok": False, "erro": "informe o valor que saiu do guardado"}), 400
    if fonte not in ("reserva", "sobra"):
        fonte = "reserva"
    saldo = _reserva_saldo() if fonte == "reserva" else _sobra_saldo()
    rotulo = "reserva" if fonte == "reserva" else "sobra de caixa"
    if valor > saldo + 0.01 and not body.get("forcar"):
        return jsonify({"ok": False, "saldo_insuficiente": True, "saldo": saldo,
                        "erro": f"a {rotulo} tem R$ {saldo:.2f} e você quer tirar "
                                f"R$ {valor:.2f} — confira o valor ou o saldo guardado."}), 400
    desc = f"REFORÇO DE CAIXA — veio da {rotulo}" + (f" ({nota})" if nota else "")
    try:
        gcapi.post("/recebimentos", {
            "descricao": desc[:180], "valor": f"{valor:.2f}",
            "data_vencimento": data, "data_competencia": data, "data_liquidacao": data,
            "liquidado": "1", "plano_contas_id": PLANO_AJUSTE_C,
            "conta_bancaria_id": GAVETA_CONTA, "forma_pagamento_id": "6055919"})
        if fonte == "reserva":
            _reserva_saida(valor, f"foi pro caixa{(' — ' + nota) if nota else ''}", data)
        else:
            _sobra_saida(valor, f"foi pro caixa{(' — ' + nota) if nota else ''}", data)
        _invalida("reserva", "sobra", "resumo", "hoje", "fech_hoje", "fech_" + data,
                  "fechamentos_7", "fechamentos_12", "fechamentos_31", "entradas_" + data)
        return jsonify({"ok": True, "valor": valor, "fonte": fonte,
                        "saldo": round(saldo - valor, 2)})
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
    banco = (body.get("banco") or "").strip()
    if tipo == "pix" and banco and banco not in BANCOS:
        return jsonify({"ok": False, "erro": "banco inválido"}), 400
    total = round(valor * 1.2, 2)                 # cliente paga no cartão (+20%)
    taxa = round(total * SAQUE_FEE[tipo], 2)      # taxa da maquininha
    ganho = round(valor * 0.2 - taxa, 2)          # nosso lucro líquido
    data = _hoje()
    try:
        # não é venda: entra o que o cliente passou (banco) e sai o dinheiro da gaveta
        r = gcapi.post("/recebimentos", {
            "descricao": f"SAQUE R$ {valor} — cliente pagou R$ {total:.2f} no {tipo}"
                         + (f" (PIX {banco})" if tipo == "pix" and banco else ""),
            "valor": f"{total:.2f}", "data_vencimento": data, "data_competencia": data,
            "data_liquidacao": data, "liquidado": "1", "plano_contas_id": PLANO_AJUSTE_C,
            "conta_bancaria_id": RESERVA_CONTA_ID, "forma_pagamento_id": SAQUE_FORMA[tipo]})
        vid = (r.get("data") or {}).get("id")
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
        # "desde a abertura": sem data o GestãoClick só devolve o mês corrente
        pgs = gcapi.get_all("/pagamentos", {"data_inicio": "2026-01-01",
                                            "data_fim": "2027-12-31"})
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
            abertos.setdefault(cat, []).append({"id": p.get("id"), "nota": _nota_conta(p),
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
        # pela chave fixa: a categoria renomeada continua sendo a mesma linha
        cats = list(dict.fromkeys(_cat_chave(lbl) for lbl, _ in CATEGORIAS_PREV)) + [MERCADORIA_CAT]
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
            it = _cat_item(c) if c != MERCADORIA_CAT else None
            if it and not it["ativa"] and not L["n"]:
                continue                              # escondida e sem conta: não precisa lembrar
            total = L["atrasado"] + L["depois"] + sum(L["mes"].values())
            tot_atr += L["atrasado"]
            tot_dep += L["depois"]
            for m in meses:
                tot_mes[m] += L["mes"][m]
            L["itens"].sort(key=lambda x: x["venc"] or "9999")
            saida.append({"cat": c, "nome": it["nome"] if it else c, "n": L["n"],
                          "atrasado": round(L["atrasado"], 2),
                          "depois": round(L["depois"], 2),
                          "mes": {m: round(v, 2) for m, v in L["mes"].items()},
                          "total": round(total, 2), "itens": L["itens"]})
        # quem tem conta em aberto sobe (maior primeiro); as categorias zeradas
        # ficam no fim, na ordem de sempre, só pra lembrar que existem.
        ordem = {c: i for i, c in enumerate(cats)}
        saida.sort(key=lambda L: (0, -L["total"]) if L["n"] else (1, ordem[L["cat"]]))
        for L in saida:                               # a tela mostra o nome que o dono deu
            L["cat"] = L.pop("nome")
        return {"meses": [{"key": m, "label": MES_CURTO[int(m[5:7]) - 1],
                           "atual": m == hoje[:7]} for m in meses],
                "linhas": saida,
                "cats_form": [_cat_nome(lbl) for lbl, _ in CATEGORIAS_PREV],
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
        # O GC recusa apagar lançamento liquidado ("Este registro não pode ser excluído
        # pois já está confirmado!") — era o caso de TODO gasto lançado aqui, que nasce
        # pago. O botão Excluir só funcionava em conta em aberto. Desmarca o pago
        # primeiro (PUT exige o corpo inteiro, não só o campo) e aí apaga.
        cur = gcapi.get(f"/pagamentos/{pid}").get("data") or {}
        if isinstance(cur, list):
            cur = cur[0] if cur else {}
        cur = cur.get("Pagamento", cur) if isinstance(cur, dict) else {}
        if str(cur.get("liquidado")) == "1":
            gcapi.put(f"/pagamentos/{pid}", {
                "descricao": cur.get("descricao") or "",
                "valor": cur.get("valor") or cur.get("valor_total") or "0",
                "plano_contas_id": cur.get("plano_contas_id") or "",
                "data_vencimento": (cur.get("data_vencimento") or _hoje())[:10],
                "data_competencia": (cur.get("data_competencia") or _hoje())[:10],
                "fornecedor_id": cur.get("fornecedor_id") or "",
                "liquidado": "0",
            })
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
        data = (body.get("data") or _hoje())[:10]
        nota = (body.get("descricao") or "").strip()
        # "ajustar_para": o dono contou o cofre e o saldo tem que FICAR nesse valor.
        # Grava só a diferença (pra mais ou pra menos) e o histórico continua inteiro.
        # Acerto de contagem NÃO mexe na gaveta: o dinheiro não saiu do caixa hoje.
        if body.get("ajustar_para") is not None:
            alvo = _num(body.get("ajustar_para"))
            if alvo < 0:
                return jsonify({"ok": False, "erro": "valor inválido"}), 400
            atual = _reserva_saldo()
            delta = round(alvo - atual, 2)
            if abs(delta) < 0.01:
                return jsonify({"ok": True, "saldo": atual, "delta": 0.0})
            tag = RES_DEP_TAG if delta > 0 else RES_OUT_TAG
            rot = "contagem" + (f" — {nota}" if nota else "")
            try:
                gcapi.post("/pagamentos", {"descricao": f"{tag} {rot}"[:180],
                    "valor": f"{abs(delta):.2f}", "data_vencimento": data,
                    "data_competencia": data, "liquidado": "0",
                    "plano_contas_id": CATS["Outros"], "forma_pagamento_id": BOLETO_FORMA_ID})
                _invalida("reserva", "pagar", "previsoes", "mapa")
                return jsonify({"ok": True, "saldo": alvo, "delta": delta})
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)[:200]}), 502
        valor = _num(body.get("valor"))
        if valor <= 0:
            return jsonify({"ok": False, "erro": "valor inválido"}), 400
        desc = f"{RES_DEP_TAG} guardei" + (f" — {nota}" if nota else "")
        origem = _origem_dinheiro(body)
        if origem == "reserva":        # veio dela mesma: não é transferência
            origem = "nenhum"
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
            elif origem == "sobra":   # veio do outro monte: só troca de bolso, desconta lá
                _sobra_saida(valor, f"passei pra reserva{(' (' + nota + ')') if nota else ''}",
                             data)
            _invalida("reserva", "pagar", "previsoes", "mapa")
            return jsonify({"ok": True, "origem": origem, "sangria": origem == "caixa",
                            "saldo_origem": _sobra_saldo() if origem == "sobra" else None})
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
        if origem == "sobra":          # veio dela mesma: não é transferência
            origem = "nenhum"
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
            elif origem == "reserva": # veio do cofre: só troca de bolso, desconta lá
                _reserva_saida(valor, f"passei pra sobra de caixa"
                                      f"{(' (' + nota + ')') if nota else ''}", data)
            _invalida("sobra", "pagar", "previsoes", "mapa")
            return jsonify({"ok": True, "origem": origem, "sangria": origem == "caixa",
                            "saldo_origem": _reserva_saldo() if origem == "reserva" else None})
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
    """Motor de fundo do painel. Puxa os pedidos a cada 10 min e, de quebra, toca as
    tarefas do dia — gráficos, baixa das garrafas e varredura das NFC-e do delivery.
    Antes elas só aconteciam quando alguém ABRIA o painel; aqui o servidor faz sozinho.
    Tudo é idempotente: repetir não duplica nada."""
    print(f"[motor] ligado no processo {os.getpid()}, primeira volta em 20s", flush=True)
    time.sleep(20)                     # deixa o serviço subir antes da primeira busca
    volta = 0
    while True:
        volta += 1
        for tarefa in (_sync_anota, _graficos_do_dia, _notas_do_dia):
            try:
                tarefa()
            except Exception as e:     # uma tarefa quebrada não pode parar as outras
                print(f"[motor] {tarefa.__name__} falhou: {str(e)[:200]}", flush=True)
        print(f"[motor] volta {volta} ok no processo {os.getpid()} — dorme {SYNC_INTERVALO}s", flush=True)
        time.sleep(SYNC_INTERVALO)


@app.route("/api/sync-anota", methods=["POST"])
@login_required
def api_sync_anota():
    """Botão 'puxar pedidos agora' do painel."""
    return jsonify(_sync_anota(forcar=True) or {"ok": True, "novos": []})


@app.route("/api/notas-delivery", methods=["POST"])
@login_required
def api_notas_delivery():
    """Botão 'conferir as notas agora' do painel."""
    _notas_do_dia(forcar=True)
    return jsonify({"ok": True, "rodando": True})


@app.route("/api/notas-status")
@login_required
def api_notas_status():
    """O "quando" vem do GestãoClick, não da memória: o gunicorn troca de processo e o
    painel passava a mostrar "ainda não rodou" mesmo com a varredura em dia."""
    u = _notas["ultimo"] or {}
    quando = _notas["quando"]
    if not quando:
        try:
            est = cached("nfce_estado", 120,
                         lambda: __import__("notas_delivery").carrega_estado(gcapi))
            e = est.get("quando") or ""
            if e:
                quando = f"{e[8:10]}/{e[5:7]}/{e[:4]} {e[11:16]}"
            u = est.get("resumo") or {}
        except Exception:
            pass
    return jsonify({"quando": quando, "rodando": _notas["rodando"],
                    "erro": _notas["erro"], "vendas": u.get("vendas"),
                    "emitidas": u.get("emitidas") or [],
                    "esperando": u.get("esperando") or [],
                    "erros": u.get("erros") or []})


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
else:
    print(f"[motor] NÃO ligou — robô do delivery carregou: {anota is not None}, "
          f"SYNC_ANOTA={os.environ.get('SYNC_ANOTA', '1')}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
