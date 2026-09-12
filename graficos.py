#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Números dos gráficos do painel (faturamento dia a dia, régua mensal, curva ABC,
parados, quanto entrou, fechamentos, contas a pagar) calculados do GestãoClick.

Mora aqui, dentro do webapp, de propósito: assim o MESMO código roda no Mac
(scripts/build_dashboard_data.py) e na NUVEM (app.py). Antes o painel só se
atualizava se o Mac do dono estivesse ligado às 8h30 — no fim de semana, com o
laptop fechado, ele passava o dia inteiro mostrando número velho.
"""
import math
from collections import defaultdict
from datetime import date, timedelta

# ---- modelo financeiro (reconstruído do snapshot e conferido centavo a centavo) ----
R_VAR      = 0.749     # custo variável (mercadoria+DAS+cartão) sobre a venda
FIXOS      = 11333.0   # custos fixos/mês (v2, JÁ com a retirada de R$1.500)
BE_DIA     = 1306
BE_DIA_RET = 1505
BE_MES     = 45152
BE_MES_SR  = 39175
CARTAO_FEE = 0.0068    # ~0,68% líquido do cartão
TROCO      = 200.0     # troco/abertura padrão da gaveta (aprox., p/ o esperado)

PRIM_MES_VIVO = "2026-07"   # daqui pra frente recalcula do CRM; antes, histórico publicado
DINI_VENDAS   = date(2026, 6, 25)
CONTA_GAVETA  = "696747"
FORMA_GAVETA  = "6055919"

YMD = lambda d: d.strftime("%Y-%m-%d")


def num(x, d=0.0):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return d


def _flat(row):
    if isinstance(row, dict) and len(row) == 1:
        return next(iter(row.values()))
    return row


def _classifica_pgto(nome):
    n = (nome or "").upper()
    if "DINHEIRO" in n:
        return "din"
    if "PIX" in n:
        return "pix"
    if "CART" in n:
        return "cartao"
    return "outro"


def _eh_servico(nome):
    n = (nome or "").upper()
    return n.startswith("SAQUE") or n.startswith("TAXA DELIVERY")


def _venda_eh_saque(v):
    return any(_eh_servico((w.get("produto", w) or {}).get("nome_produto"))
               for w in (v.get("produtos") or []))


def _alvo(vel, dias, fardo):
    need = vel * dias
    if need <= 0:
        return {"un": 0, "fardos": None}
    if fardo and fardo > 1:
        fardos = max(1, math.ceil(need / fardo))
        return {"un": fardos * fardo, "fardos": fardos}
    return {"un": max(1, round(need)), "fardos": None}


def baixar(fetch_pages, hoje, log=lambda *a: None):
    """Traz do CRM o que os gráficos precisam. `fetch_pages(path, params)` pagina."""
    dini = YMD(DINI_VENDAS)
    vendas = fetch_pages("/vendas", {"tipo": "vendas_balcao",
                                     "data_inicio": dini, "data_fim": YMD(hoje)})
    # O delivery do Anota AI entra como venda tipo "produto" e ficava fora de TODOS os
    # gráficos — em setembro isso era ~30% do movimento. Venda tipo "produto" que não é
    # do app (saque etc.) continua de fora: é a observação "Anota AI" que separa.
    prod = fetch_pages("/vendas", {"tipo": "produto",
                                   "data_inicio": dini, "data_fim": YMD(hoje)})
    delivery = [v for v in prod if "ANOTA AI" in (v.get("observacoes") or "").upper()]
    vendas += delivery
    produtos = fetch_pages("/produtos", {})
    # SEM período o GestãoClick devolve só o MÊS CORRENTE (122 lançamentos em vez de
    # 675) — o DRE saía com julho e agosto sem custo nenhum, como se fossem lucro puro.
    pagamentos = fetch_pages("/pagamentos", {"data_inicio": "2026-01-01",
                                             "data_fim": f"{hoje.year + 1}-12-31"})
    log(f"  vendas={len(vendas)} (delivery {len(delivery)}) produtos={len(produtos)} "
        f"pagamentos={len(pagamentos)}")
    return vendas, produtos, pagamentos


def construir(vendas, produtos, pagamentos, old, fardo_by_nome=None, hoje=None,
              categoria_fn=None):
    """Monta o dicionário dos gráficos. `old` é o pacote anterior (mantém o histórico
    de meses antes de PRIM_MES_VIVO e os blocos que não recalculamos)."""
    TODAY = hoje or date.today()
    fardo_by_nome = fardo_by_nome or {}
    ativas = [v for v in vendas if "cancel" not in (v.get("nome_situacao") or "").lower()]

    prod_by_id = {str(p.get("id")): {"nome": p.get("nome"),
                                     "estoque": num(p.get("estoque")),
                                     "custo": num(p.get("valor_custo"))}
                  for p in produtos}

    # ---------- agregados por dia / mês ----------
    dia = defaultdict(lambda: {"fat": 0.0, "n": 0, "pix": 0.0, "cartao": 0.0,
                               "din": 0.0, "outro": 0.0})
    mes = defaultdict(lambda: {"fat": 0.0, "n": 0})
    saque_mes = defaultdict(float)
    for v in ativas:
        dt = (v.get("data") or "")[:10]
        if not dt:
            continue
        tot = num(v.get("valor_total"))
        dia[dt]["fat"] += tot
        dia[dt]["n"] += 1
        mes[dt[:7]]["fat"] += tot
        mes[dt[:7]]["n"] += 1
        if _venda_eh_saque(v):
            saque_mes[dt[:7]] += tot
        for wrap in v.get("pagamentos") or []:
            p = wrap.get("pagamento", wrap)
            dia[dt][_classifica_pgto(p.get("nome_forma_pagamento"))] += num(p.get("valor"))

    CURM = YMD(TODAY)[:7]
    dias_mes = sorted(d for d in dia if d[:7] == CURM)
    ult_dia = dias_mes[-1] if dias_mes else YMD(TODAY)
    dias_corridos = int(ult_dia[8:10])
    # dias reais do mês (era 31 fixo — setembro tem 30 e a projeção saía inflada)
    dim = (date(TODAY.year + (TODAY.month == 12), TODAY.month % 12 + 1, 1)
           - timedelta(days=1)).day

    fat_mtd = mes[CURM]["fat"]
    # A média e a projeção olham só os dias JÁ FECHADOS (até ontem). Contar o dia de
    # hoje, que às 8h da manhã tem quase nada, achatava a projeção do mês inteiro.
    ontem = YMD(TODAY - timedelta(days=1))
    dias_fech = [d for d in dias_mes if d <= ontem]
    base_dias = len(dias_fech) or dias_corridos
    fat_fech = sum(dia[d]["fat"] for d in dias_fech) or fat_mtd
    media_dia = fat_fech / base_dias if base_dias else 0.0
    proj = media_dia * dim
    gastos_proj = proj * R_VAR + FIXOS
    resultado_proj = proj - gastos_proj

    # ---------- KPIs ----------
    def entre(a, b):
        return [v for v in ativas if a <= (v.get("data") or "")[:10] <= b]
    d30 = entre(YMD(TODAY - timedelta(days=29)), YMD(TODAY))
    d7 = entre(YMD(TODAY - timedelta(days=6)), YMD(TODAY))
    fat30 = sum(num(v.get("valor_total")) for v in d30)
    fat7 = sum(num(v.get("valor_total")) for v in d7)
    kpis = {"fat30": round(fat30, 2), "fat7": round(fat7, 2),
            "media_dia7": round(fat7 / 7, 2), "be_dia": BE_DIA, "be_dia_retirada": BE_DIA_RET,
            "vendas_dia7": round(len(d7) / 7, 1),
            "ticket": round(fat30 / len(d30), 2) if d30 else 0.0}
    mes_atual = {"mes": CURM, "fat_mtd": round(fat_mtd, 2), "dias": base_dias,
                 "media_dia": round(media_dia, 2), "proj": round(proj, 2),
                 "be_mes": BE_MES, "be_mes_sem_retirada": BE_MES_SR,
                 "resultado_proj": round(resultado_proj, 2),
                 "saque_fat": round(saque_mes[CURM], 2),
                 "pct_saque": round(saque_mes[CURM] / fat_mtd * 100, 1) if fat_mtd else 0.0}

    # ---------- régua mensal ----------
    # Julho estava chumbado aqui e AGOSTO SUMIA. Agora todo mês fechado que veio no
    # download entra sozinho, e o mês atual segue como projeção.
    meses = [m for m in old.get("meses", []) if m["mes"] < PRIM_MES_VIVO]
    for mk in sorted(m for m in mes if PRIM_MES_VIVO <= m < CURM):
        mfat = mes[mk]["fat"]
        mgastos = mfat * R_VAR + FIXOS
        meses.append({"mes": mk, "fat": round(mfat, 2), "proj": None, "fixos": FIXOS,
                      "gastos": round(mgastos, 2), "resultado": round(mfat - mgastos, 2)})
    meses.append({"mes": CURM, "fat": round(fat_mtd, 2), "proj": round(proj, 2),
                  "fixos": FIXOS, "gastos": round(gastos_proj, 2),
                  "resultado": round(resultado_proj, 2)})

    # ---------- dia a dia do mês ----------
    dia_mes = []
    if dias_mes:
        cur = date(int(CURM[:4]), int(CURM[5:7]), 1)
        dN = date(int(ult_dia[:4]), int(ult_dia[5:7]), int(ult_dia[8:10]))
        while cur <= dN:
            k = YMD(cur)
            dia_mes.append({"data": k, "fat": round(dia[k]["fat"], 2)})
            cur += timedelta(days=1)

    # ---------- quanto de fato entrou ----------
    def conta_liq(r):
        return r["pix"] + r["outro"] + r["cartao"] * (1 - CARTAO_FEE)
    ent_dias = []
    for k in [d["data"] for d in dia_mes]:
        r = dia[k]
        contav, din = conta_liq(r), r["din"]
        ent_dias.append({"data": k, "faturado": round(r["fat"], 2), "conta": round(contav, 2),
                         "dinheiro": round(din, 2), "total": round(contav + din, 2),
                         "dif": round(contav + din - r["fat"], 2)})
    entradas = {"dias": ent_dias,
                "tot_fat": round(sum(e["faturado"] for e in ent_dias), 2),
                "tot_conta": round(sum(e["conta"] for e in ent_dias), 2),
                "tot_din": round(sum(e["dinheiro"] for e in ent_dias), 2),
                "tot_entrou": round(sum(e["total"] for e in ent_dias), 2),
                "dif": round(sum(e["dif"] for e in ent_dias), 2)}
    conta = [{"data": k, "pix": round(dia[k]["pix"], 2), "cartao": round(dia[k]["cartao"], 2),
              "entrada_liq": round(conta_liq(dia[k]), 2), "dinheiro": round(dia[k]["din"], 2)}
             for k in sorted(dia)[-20:]]

    # ---------- fechamentos (esperado na gaveta) ----------
    saida_dia = defaultdict(float)
    for p in pagamentos:
        if str(p.get("liquidado")) != "1":
            continue
        dl = (p.get("data_liquidacao") or "")[:10]
        if not dl:
            continue
        if (str(p.get("conta_bancaria_id")) == CONTA_GAVETA
                or str(p.get("forma_pagamento_id")) == FORMA_GAVETA):
            saida_dia[dl] += num(p.get("valor_total")) or num(p.get("valor"))
    fechamentos = []
    for k in sorted(dia)[-20:]:
        din, sai = dia[k]["din"], round(saida_dia.get(k, 0.0), 2)
        fechamentos.append({"data": k, "abertura": TROCO, "dinheiro": round(din, 2),
                            "saidas": sai, "esperado": round(TROCO + din - sai, 2),
                            "contado": None})

    # ---------- curva ABC / parados ----------
    p30 = defaultdict(lambda: {"un": 0.0, "fat": 0.0, "nome": "", "pid": ""})
    p7, p15, sold7 = defaultdict(float), defaultdict(float), set()
    lim7 = YMD(TODAY - timedelta(days=6))
    lim15 = YMD(TODAY - timedelta(days=14))
    lim30 = YMD(TODAY - timedelta(days=29))
    for v in ativas:
        dt = (v.get("data") or "")[:10]
        if dt < lim30:
            continue
        for wrap in v.get("produtos") or []:
            it = wrap.get("produto", wrap)
            pid = str(it.get("produto_id") or "")
            nome = it.get("nome_produto") or (prod_by_id.get(pid, {}) or {}).get("nome") or "?"
            if _eh_servico(nome):
                continue
            q = num(it.get("quantidade"))
            s = p30[pid]
            s["un"] += q
            s["fat"] += q * num(it.get("valor_venda"))
            s["nome"], s["pid"] = nome, pid
            if dt >= lim7:
                p7[pid] += q
                sold7.add(pid)
            if dt >= lim15:
                p15[pid] += q

    ranked = sorted(p30.values(), key=lambda x: -x["fat"])
    fattot = sum(x["fat"] for x in ranked) or 1.0
    sugestoes, acum = [], 0.0
    for x in ranked:
        acum += x["fat"]
        pctac = acum / fattot * 100
        classe = "A" if pctac <= 80 else ("B" if pctac <= 95 else "C")
        fardo = fardo_by_nome.get((x["nome"] or "").upper())
        vel = x["un"] / 30.0
        est = (prod_by_id.get(x["pid"], {}) or {}).get("estoque")
        sugestoes.append({"nome": x["nome"], "vendeu7": round(p7.get(x["pid"], 0)),
                          "vendeu15": round(p15.get(x["pid"], 0)), "vendeu30": round(x["un"]),
                          "estoque": None if est is None else round(est), "fardo": fardo,
                          "vel_dia": round(vel, 1), "c7": _alvo(vel, 7, fardo),
                          "c15": _alvo(vel, 15, fardo), "c30": _alvo(vel, 30, fardo),
                          "fat30": round(x["fat"], 2), "classe": classe,
                          "estoque_neg": (est is not None and est < 0)})
    sugestoes = sugestoes[:90]

    parados = sorted(
        ({"nome": i["nome"], "estoque": round(i["estoque"]),
          "capital": round(i["estoque"] * i["custo"], 2),
          "vendeu30": round(p30.get(pid, {}).get("un", 0))}
         for pid, i in prod_by_id.items()
         if not _eh_servico(i["nome"]) and i["estoque"] > 0 and pid not in sold7),
        key=lambda x: -x["capital"])[:30]

    # ---------- contas a pagar ----------
    a_pagar = sorted(
        ({"venc": (p.get("data_vencimento") or "")[:10],
          "desc": p.get("descricao") or p.get("nome_plano_conta") or "—",
          "valor": round(num(p.get("valor_total")) or num(p.get("valor")), 2),
          "vencida": bool((p.get("data_vencimento") or "")[:10])
                     and (p.get("data_vencimento") or "")[:10] < YMD(TODAY)}
         for p in pagamentos if str(p.get("liquidado")) != "1"),
        key=lambda x: x["venc"] or "9999")

    novo = dict(old)
    novo.update({"gerado_em": YMD(TODAY), "kpis": kpis, "mes_atual": mes_atual,
                 "meses": meses, "dia_mes": dia_mes, "serie_fat": dia_mes,
                 "entradas": entradas, "conta": conta, "fechamentos": fechamentos,
                 "sugestoes": sugestoes, "parados": parados, "a_pagar": a_pagar,
                 "dre": monta_dre(vendas, pagamentos, categoria_fn, TODAY)})
    return novo

# ======================================================================
# DRE MENSAL — "Demonstração de lucros e perdas", igual à planilha do dono
# ----------------------------------------------------------------------
# Regime de COMPETÊNCIA: cada gasto pesa no mês a que pertence, pago ou não. Tentei
# por caixa primeiro e não serve: o dono dá baixa das contas em lote, então a data de
# liquidação jogava o custo de julho e agosto inteiro dentro de setembro.
#
# Decisões que ELE tomou em 12/09/2026 (não mudar sem falar com ele):
#  · custo da mercadoria = a nota de fornecedor daquele mês;
#  · retirada dos sócios ENTRA como custo (o lucro que sobra já é depois de se pagarem);
#  · o quadro começa em julho/2026 — antes disso só mercadoria e contabilidade estavam
#    lançados, e o lucro dos meses anteriores apareceria inflado.
DRE_INICIO = "2026-07"

# Cada linha da planilha e de onde ela vem. A chave da esquerda é a categoria do
# painel (ou o plano de contas do GestãoClick, quando o lançamento não tem categoria).
DRE_DE_PARA = {
    # --- pessoas ---
    "Igor (pró-labore / retirada)":      "Salários e remunerações",
    "Retirada Victor":                   "Salários e remunerações",
    "Retirada do sócio (Victor)":        "Salários e remunerações",
    "Biel (Gabriel)":                    "Salários e remunerações",
    "Vigia":                             "Salários e remunerações",
    "INSS s/ pró-labore":                "Salários e remunerações",
    # --- estrutura ---
    "Aluguel (IPTU)":                    "Aluguel",
    "Energia (CEMIG)":                   "Luz",
    "Água (COPASA)":                     "Água",
    "Internet / telefone":               "Telefone / internet",
    "Telefonia e internet":              "Telefone / internet",
    "Contabilidade (Werdeiros)":         "Contabilidade",
    "Reparo da loja":                    "Manutenção de equipamentos",
    "Limpeza":                           "Material de limpeza",
    # --- operação ---
    "Motoboy / entrega":                 "Motoboy / entregas",
    "PH Motoca":                         "Motoboy / entregas",
    "Sacolas / gelo / copos":            "Sacolas / gelo / copos",
    "Anota AI":                          "Publicidade",
    # --- impostos (linha própria, embaixo do lucro das operações) ---
    "DAS (Simples)":                     "@impostos",
    "Parcelamento Simples (PARCSN)":     "@impostos",
    "Taxas / alvará (PBH)":              "@impostos",
    # --- financeiro ---
    "Despesas bancárias":                "@financeiro",
    # --- mercadoria (vira o CMV, não é custo operacional) ---
    "Compras":                           "@mercadoria",
    # --- sobras conhecidas ---
    "Lanche":                            "Outros",
    "Almoço":                            "Outros",
    "Padaria":                           "Outros",
    "Seguro do carro":                   "Outros",
    "Material de escritório":            "Outros",
    "Supermercado":                      "Outros",
    "Água / luz / internet":             "Outros",
}

# Lançamento que NÃO é despesa: é o dinheiro da gaveta indo pro cofre/conta.
# Se entrar na conta, o mês inteiro vira prejuízo falso.
DRE_IGNORAR = {"Ajuste de caixa", "Saque", "Transferência entre contas"}

# A ordem exata em que as linhas aparecem, como na planilha.
DRE_OPERACIONAIS = [
    "Salários e remunerações", "Perdas de mercadoria", "Aluguel", "Material de limpeza",
    "Luz", "Telefone / internet", "Água", "Gasolina", "Manutenção de equipamentos",
    "Publicidade", "Contabilidade", "Motoboy / entregas", "Sacolas / gelo / copos",
    "Outros",
]
MES_CURTO = ["jan", "fev", "mar", "abr", "mai", "jun",
             "jul", "ago", "set", "out", "nov", "dez"]


def _dre_venda_eh_saque(v):
    """Saque = troca de cartão por dinheiro. Não é venda, não entra no faturamento."""
    return any((((w.get("produto", w) or {}).get("nome_produto")) or "").upper()
               .startswith("SAQUE") for w in (v.get("produtos") or []))


def monta_dre(vendas, pagamentos, categoria_fn=None, hoje=None):
    """Demonstração de lucros e perdas, mês a mês. Devolve também o que não soube
    classificar, pra perguntar ao dono em vez de enfiar em 'Outros' calado."""
    TODAY = hoje or date.today()
    curm = YMD(TODAY)[:7]

    receita = defaultdict(lambda: {"bruto": 0.0, "desconto": 0.0, "devolucao": 0.0,
                                   "cartao": 0.0, "n": 0})
    for v in vendas:
        mk = (v.get("data") or "")[:7]
        if not mk or mk < DRE_INICIO:
            continue
        if _dre_venda_eh_saque(v):
            continue
        r = receita[mk]
        if "cancel" in (v.get("nome_situacao") or "").lower():
            r["devolucao"] += num(v.get("valor_total"))
            continue
        # valor_total = produtos + frete − desconto. O bruto da planilha é antes do
        # desconto, senão a linha "Descontos (redução)" desconta duas vezes.
        r["bruto"] += num(v.get("valor_produtos")) + num(v.get("valor_frete"))
        r["desconto"] += num(v.get("desconto_valor"))
        r["n"] += 1
        for wrap in v.get("pagamentos") or []:
            p = wrap.get("pagamento", wrap)
            if _classifica_pgto(p.get("nome_forma_pagamento")) == "cartao":
                r["cartao"] += num(p.get("valor"))

    # ---- previsão que virou conta de verdade e ninguém apagou ----
    # O dono lança "[PREV] Aluguel" no começo do mês e depois a conta real entra pelo
    # DDA/PIX. Quando a previsão fica aberta do lado da paga, o mês conta DUAS VEZES
    # (R$ 3.781 em set/2026). Se existe uma paga do mesmo valor, a previsão é ela.
    # conta "de verdade" = a que NÃO é previsão, paga ou não: a de agosto da CEMIG
    # ainda estava em aberto do lado da previsão e as duas somavam na linha da Luz.
    pagas = defaultdict(list)
    for p in pagamentos:
        if not (p.get("descricao") or "").startswith("[PREV]"):
            mk = ((p.get("data_competencia") or p.get("data_vencimento") or "")[:7])
            pagas[((categoria_fn(p) if categoria_fn else None) or "", mk)].append(
                num(p.get("valor_total")) or num(p.get("valor")))
    duplicadas = []

    def _prev_ja_paga(p, chave, mk, v):
        if not (p.get("descricao") or "").startswith("[PREV]"):
            return False
        for vp in pagas.get((chave, mk), []):
            if abs(vp - v) <= max(60.0, v * 0.10):
                duplicadas.append({"categoria": chave, "mes": mk, "valor": round(v, 2),
                                   "real": round(vp, 2)})
                return True
        return False

    custo = defaultdict(lambda: defaultdict(float))   # linha -> mês -> valor
    desconhecidos = defaultdict(lambda: {"valor": 0.0, "n": 0, "exemplo": "", "plano": ""})
    for p in pagamentos:
        # Mês do gasto = COMPETÊNCIA (ou vencimento). NÃO dá pra usar a data em que a
        # conta foi baixada: o dono dá baixa em lote, e aí julho e agosto apareciam sem
        # custo nenhum e setembro com tudo. Conta atrasada continua pesando no mês dela.
        mk = ((p.get("data_competencia") or p.get("data_vencimento") or "")[:7])
        if not mk or mk < DRE_INICIO:
            continue
        plano = p.get("nome_plano_conta") or ""
        if plano in DRE_IGNORAR:
            continue
        v = num(p.get("valor_total")) or num(p.get("valor"))
        if v <= 0:
            continue
        chave = (categoria_fn(p) if categoria_fn else None) or plano
        if _prev_ja_paga(p, (categoria_fn(p) if categoria_fn else None) or "", mk, v):
            continue
        linha = DRE_DE_PARA.get(chave)
        if linha is None:
            d = desconhecidos[chave or "(sem plano de contas)"]
            d["valor"] += v
            d["n"] += 1
            d["plano"] = plano
            if not d["exemplo"]:
                d["exemplo"] = (p.get("descricao") or "")[:60]
            linha = "Outros"                           # entra, mas sinalizado
        custo[linha][mk] += v

    # Só até o mês corrente: contas de aluguel já lançadas pra outubro/novembro faziam
    # o quadro mostrar "prejuízo" em mês que ainda nem começou.
    meses = sorted(set(receita) | {m for l in custo.values() for m in l})
    meses = [m for m in meses if DRE_INICIO <= m <= curm]
    if not meses:
        return {}

    def mesdict(fn):
        return {m: round(fn(m), 2) for m in meses}

    vendas_liq = mesdict(lambda m: receita[m]["bruto"] - receita[m]["desconto"]
                         - receita[m]["devolucao"])
    merc = mesdict(lambda m: custo["@mercadoria"].get(m, 0.0))
    bruto = mesdict(lambda m: vendas_liq[m] - merc[m])
    oper = {l: mesdict(lambda m, l=l: custo[l].get(m, 0.0)) for l in DRE_OPERACIONAIS}
    tot_oper = mesdict(lambda m: sum(oper[l][m] for l in DRE_OPERACIONAIS))
    lucro_op = mesdict(lambda m: bruto[m] - tot_oper[m])
    # taxa de cartão não é boleto: sai descontada na hora, então é calculada
    fin = mesdict(lambda m: custo["@financeiro"].get(m, 0.0)
                  + receita[m]["cartao"] * CARTAO_FEE)
    antes_imp = mesdict(lambda m: lucro_op[m] - fin[m])
    imp = mesdict(lambda m: custo["@impostos"].get(m, 0.0))
    liquido = mesdict(lambda m: antes_imp[m] - imp[m])

    def L(label, dados, estilo="normal", dica=""):
        return {"label": label, "mes": dados, "aad": round(sum(dados.values()), 2),
                "estilo": estilo, "dica": dica}

    linhas = [
        L("Vendas", mesdict(lambda m: receita[m]["bruto"]), "normal",
          "balcão + delivery, antes do desconto; saque não entra"),
        L("Devoluções (redução)", mesdict(lambda m: -receita[m]["devolucao"])),
        L("Descontos (redução)", mesdict(lambda m: -receita[m]["desconto"])),
        L("Vendas líquidas", vendas_liq, "subtotal"),
        L("Custo das mercadorias vendidas", merc, "normal",
          "nota de fornecedor lançada no mês"),
        L("Lucro bruto", bruto, "subtotal"),
    ]
    linhas += [L(l, oper[l]) for l in DRE_OPERACIONAIS]
    linhas += [
        L("Total de custos operacionais", tot_oper, "subtotal"),
        L("Lucro das operações", lucro_op, "subtotal"),
        L("Taxas de cartão e banco", fin, "normal",
          f"~{CARTAO_FEE*100:.2f}% do que passou no cartão, mais tarifa do banco"),
        L("Lucro antes do imposto", antes_imp, "subtotal"),
        L("Impostos", imp, "normal", "DAS, parcelamento do Simples e taxas da prefeitura"),
        L("LUCRO LÍQUIDO", liquido, "total"),
    ]

    pend = [{"categoria": k, "valor": round(d["valor"], 2), "n": d["n"],
             "exemplo": d["exemplo"], "plano": d["plano"]}
            for k, d in sorted(desconhecidos.items(), key=lambda x: -x[1]["valor"])]

    # o mês corrente está pela metade: dizer até que dia, pra ninguém comparar um mês
    # de 12 dias com um mês fechado e achar que despencou
    dia_hoje = TODAY.day
    dim = (date(TODAY.year + (TODAY.month == 12), TODAY.month % 12 + 1, 1)
           - timedelta(days=1)).day
    return {"meses": [{"key": m, "label": MES_CURTO[int(m[5:7]) - 1] + "/" + m[2:4],
                       "atual": m == curm,
                       "parcial": (f"{dia_hoje} de {dim} dias" if m == curm else None)}
                      for m in meses],
            "linhas": linhas, "pendentes": pend, "mes_atual": curm,
            "duplicadas": duplicadas,
            "inicio": DRE_INICIO, "gerado_em": YMD(TODAY)}
