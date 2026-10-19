#!/usr/bin/env python3
"""
Radar eBay de cartas Pokémon PSA abaixo do mercado.

O que ele faz a cada execução:
  1. Busca na API oficial do eBay (Browse API) os anúncios mais recentes de
     cartas Pokémon graduadas PSA, com compra imediata ("Buy It Now").
  2. Fica só com PSA 7 a 10, ignora lotes, outras graduadoras e cartas suspeitas.
  3. Exige pelo menos um sinal de potencial de valorização
     (Pokémon icônico, vintage, arte especial, japonesa, promo...).
  4. Calcula o preço de mercado da MESMA carta na MESMA nota:
       - pelo PriceCharting (vendas concluídas), se você tiver o token; ou
       - pela mediana dos outros anúncios ativos da mesma carta/nota no eBay.
  5. Se o anúncio estiver X% (padrão 30%) abaixo do mercado, manda alerta
     no Telegram e/ou e-mail. Opcionalmente o Claude escreve uma análise curta.

Nunca compra, nunca dá lance. Só avisa.

Uso:
  python radar.py            # roda uma varredura
  python radar.py --loop     # fica rodando a cada INTERVALO_MINUTOS
  python radar.py --teste    # roda com anúncios fictícios, sem chamar o eBay
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import smtplib
import statistics
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Instale as dependências: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Configuração (via .env ou variáveis de ambiente)
# ---------------------------------------------------------------------------

def env(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, padrao).strip()


EBAY_CLIENT_ID = env("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = env("EBAY_CLIENT_SECRET")
EBAY_MARKETPLACE = env("EBAY_MARKETPLACE", "EBAY_US")

DESCONTO_MINIMO = float(env("DESCONTO_MINIMO", "30"))        # em %
VALOR_MERCADO_MINIMO = float(env("VALOR_MERCADO_MINIMO", "60"))  # em USD
NOTA_MINIMA = int(env("NOTA_MINIMA", "7"))
NOTA_MAXIMA = int(env("NOTA_MAXIMA", "10"))
MIN_COMPARAVEIS = int(env("MIN_COMPARAVEIS", "4"))
# Máximo de buscas de comparáveis por rodada; o que sobrar fica para a próxima.
MAX_BUSCAS_COMPARAVEIS = int(env("MAX_BUSCAS_COMPARAVEIS", "25"))
INTERVALO_MINUTOS = int(env("INTERVALO_MINUTOS", "15"))

TAXA_EBAY = float(env("TAXA_EBAY", "13")) / 100              # taxa de venda
FRETE_REVENDA_USD = float(env("FRETE_REVENDA_USD", "10"))
FRETE_INTERNACIONAL_USD = float(env("FRETE_INTERNACIONAL_USD", "30"))
IMPOSTO_IMPORTACAO = float(env("IMPOSTO_IMPORTACAO", "60")) / 100

PRICECHARTING_TOKEN = env("PRICECHARTING_TOKEN")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
EMAIL_TO = env("EMAIL_TO")

# App no celular (Lovable + Supabase): o radar grava cada carta na tabela "alertas".
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = env("SUPABASE_SERVICE_KEY")  # chave "service_role": secreta, só no .env
LINK_APP = env("LINK_APP")  # endereço do app no Lovable, vira botão no Telegram

ARQUIVO_VISTOS = Path(env("ARQUIVO_VISTOS", str(Path(__file__).with_name("vistos.json"))))
# Histórico dos alertas; a página com fotos é salva ao lado, em alertas.html.
ARQUIVO_ALERTAS = Path(env("ARQUIVO_ALERTAS", str(Path(__file__).with_name("alertas.json"))))
# Abre a página no navegador quando aparece alerta novo (nunca no GitHub Actions).
ABRIR_PAGINA = env("ABRIR_PAGINA", "sim").lower() not in ("nao", "não", "0", "false")

# Buscas que rodam a cada varredura. Edite à vontade.
BUSCAS = [
    "pokemon PSA 10",
    "pokemon PSA 9",
    "pokemon PSA 8",
    "pokemon PSA 7",
    "pokemon PSA charizard",
    "pokemon PSA japanese",
    "pokemon PSA vintage 1st edition",
    "pokemon PSA special illustration rare",
    "pokemon PSA umbreon",
    "pokemon PSA pikachu",
]

CATEGORIA_CARTAS = "183454"  # eBay: CCG Individual Cards

# ---------------------------------------------------------------------------
# Regras de filtragem e sinais de valorização
# ---------------------------------------------------------------------------

ICONICOS = [
    "charizard", "pikachu", "umbreon", "espeon", "eevee", "vaporeon", "jolteon",
    "flareon", "leafeon", "glaceon", "sylveon", "mew", "mewtwo", "lugia",
    "rayquaza", "gengar", "gyarados", "blastoise", "venusaur", "dragonite",
    "giratina", "ho-oh", "snorlax", "lucario", "greninja", "tyranitar",
]

# Pokémon menos icônicos, usados só para identificar a carta nos comparáveis.
OUTROS_POKEMON = [
    "garchomp", "gardevoir", "alakazam", "arcanine", "ninetales", "machamp",
    "zapdos", "articuno", "moltres", "raichu", "celebi", "jirachi", "darkrai",
    "palkia", "dialga", "arceus", "zekrom", "reshiram", "kyogre", "groudon",
    "latias", "latios", "suicune", "entei", "raikou", "scizor", "metagross",
    "salamence", "typhlosion", "feraligatr", "meganium", "zacian", "zamazenta",
    "miraidon", "koraidon", "iono", "lillie", "marnie", "cynthia", "erika",
    "misty", "pokemon center", "magikarp", "psyduck", "bulbasaur", "squirtle",
    "charmander", "jigglypuff", "clefairy", "chansey", "lapras", "ditto",
]

VINTAGE = [
    "base set", "shadowless", "1st edition", "1st ed", "jungle", "fossil",
    "team rocket", "gym heroes", "gym challenge", "neo genesis", "neo discovery",
    "neo revelation", "neo destiny", "legendary collection", "expedition",
    "aquapolis", "skyridge", "e-card", "wotc", "1999", "2000", "2001", "2002", "2003",
]

ARTE_ESPECIAL = [
    "special illustration", " sir ", "illustration rare", " ir ", "alt art",
    "alternate art", "gold star", "goldstar", "shining", "crystal", "secret rare",
    "full art", "rainbow", "gold ", "trainer gallery", "hyper rare", "art rare",
    " ar ", " sar ", " chr ", " csr ", "master ball",
]

LIMITADA = ["promo", "trophy", "prerelease", "staff", "championship", "worlds",
            "illustrator", "pokemon center", "black star"]

JAPONES = ["japanese", "japan", " jp ", "jpn"]

EXCLUIR = [
    "lot", "bundle", "collection of", "mystery", "repack", "proxy", "custom",
    "reprint", "orica", "fan art", "replica", "digital", "code card", "sealed",
    "booster", "pack", "box", "etb", "cgc", "bgs", "beckett", "tag ", "sgc",
    "ace grading", "psa/dna", "autograph", "auto ", "signed", "empty slab",
    "slab only", "case only", "you pick", "choose",
]

RE_NOTA = re.compile(r"\bPSA\s*(10|[1-9](?:\.5)?)\b", re.IGNORECASE)
RE_NUMERO = re.compile(r"(?<![\d$.])(\d{1,3})\s*/\s*(\d{1,3})(?!\d)|#\s*(\d{1,3})\b|\b(SM|SWSH|SVP|XY|BW|DP|HGSS)\s?-?(\d{1,3})\b",
                       re.IGNORECASE)


@dataclass
class Anuncio:
    item_id: str
    titulo: str
    preco: float
    frete: float
    moeda: str
    url: str
    pais_vendedor: str = ""
    vendedor: str = ""
    feedback_pct: float | None = None
    feedback_score: int | None = None
    imagem: str = ""
    # calculados
    nota: int | None = None
    chave_carta: str = ""
    pokemon: str = ""
    sinais: list[str] = field(default_factory=list)
    mercado: float | None = None
    fonte_mercado: str = ""
    n_comparaveis: int = 0
    desconto_pct: float = 0.0
    lucro_revenda_eua: float = 0.0
    custo_brasil_usd: float = 0.0
    alertas: list[str] = field(default_factory=list)
    analise_claude: str = ""

    @property
    def custo_total(self) -> float:
        return self.preco + self.frete


# ---------------------------------------------------------------------------
# Leitura e classificação do título
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    return " " + re.sub(r"\s+", " ", texto.lower().replace("-", " ")) + " "


def extrair_nota(titulo: str) -> int | None:
    notas = RE_NOTA.findall(titulo)
    if len(set(notas)) != 1:  # nenhuma nota, ou mais de uma (provável lote)
        return None
    nota = notas[0]
    if "." in nota:  # PSA não dá meia nota; título estranho
        return None
    return int(nota)


def extrair_numero(titulo: str) -> str:
    m = RE_NUMERO.search(titulo)
    if not m:
        return ""
    if m.group(1):
        return f"{int(m.group(1))}/{int(m.group(2))}"
    if m.group(3):
        return f"#{int(m.group(3))}"
    return f"{m.group(4).upper()}{int(m.group(5))}"


def extrair_pokemon(titulo_norm: str) -> str:
    for nome in ICONICOS + OUTROS_POKEMON:
        # "mew" não pode casar com "mewtwo"
        if re.search(rf"(?<![a-z]){re.escape(nome)}(?![a-z])", titulo_norm):
            return nome
    return ""


def deve_excluir(titulo_norm: str) -> str:
    for termo in EXCLUIR:
        padrao = rf"(?<![a-z]){re.escape(termo.strip())}(?![a-z])"
        if re.search(padrao, titulo_norm):
            return termo.strip()
    return ""


def sinais_valorizacao(titulo_norm: str, pokemon: str) -> list[str]:
    sinais = []
    if pokemon in ICONICOS:
        sinais.append(f"Pokémon icônico ({pokemon.title()})")

    def tem(lista):
        return [t.strip() for t in lista if t in titulo_norm]

    if achados := tem(VINTAGE):
        sinais.append(f"vintage ({achados[0]})")
    if achados := tem(ARTE_ESPECIAL):
        sinais.append(f"arte especial ({achados[0].upper() if len(achados[0]) <= 4 else achados[0]})")
    if achados := tem(LIMITADA):
        sinais.append(f"edição limitada/promo ({achados[0]})")
    if tem(JAPONES):
        sinais.append("carta japonesa")
    return sinais


def chave_de(titulo: str) -> tuple[str, int | None, str]:
    """Identifica a carta pelo título. Devolve (chave, nota, motivo_descarte)."""
    t = normalizar(titulo)
    if (termo := deve_excluir(t)):
        return "", None, f"excluído ({termo})"
    nota = extrair_nota(titulo)
    if nota is None:
        return "", None, "sem nota PSA clara"
    pokemon = extrair_pokemon(t)
    numero = extrair_numero(titulo)
    if not pokemon or not numero:
        return "", nota, "não deu para identificar a carta (nome + número)"
    idioma = "jp" if any(j in t for j in JAPONES) else "en"
    edicao = "1st" if ("1st edition" in t or "1st ed" in t) else ""
    return f"{pokemon}|{numero}|{idioma}|{edicao}|psa{nota}", nota, ""


def classificar(a: Anuncio) -> str | None:
    """Preenche nota, chave e sinais. Devolve motivo de descarte ou None."""
    chave, a.nota, motivo = chave_de(a.titulo)
    if a.nota is not None and not (NOTA_MINIMA <= a.nota <= NOTA_MAXIMA):
        return f"nota PSA {a.nota} fora da faixa"
    if motivo:
        return motivo
    a.chave_carta = chave
    a.pokemon = chave.split("|")[0]
    a.sinais = sinais_valorizacao(normalizar(a.titulo), a.pokemon)
    if not a.sinais:
        return "sem sinal de valorização"
    return None


# ---------------------------------------------------------------------------
# eBay Browse API
# ---------------------------------------------------------------------------

class LimiteEbay(Exception):
    """O eBay recusou a chamada por excesso de uso (HTTP 429)."""


class Ebay:
    TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, client_id: str, client_secret: str, marketplace: str):
        if not client_id or not client_secret:
            sys.exit("Faltam EBAY_CLIENT_ID e EBAY_CLIENT_SECRET no .env (veja o README).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self._token = ""
        self._expira = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expira - 60:
            return self._token
        cred = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = requests.post(
            self.TOKEN_URL,
            headers={"Authorization": f"Basic {cred}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30,
        )
        r.raise_for_status()
        dados = r.json()
        self._token = dados["access_token"]
        self._expira = time.time() + int(dados.get("expires_in", 7200))
        return self._token

    def buscar(self, consulta: str, limite: int = 200, recentes: bool = True,
               preco_min: float | None = None) -> list[Anuncio]:
        filtros = ["buyingOptions:{FIXED_PRICE}", "priceCurrency:USD"]
        if preco_min:
            filtros.append(f"price:[{preco_min:.0f}..]")
        params = {
            "q": consulta,
            "category_ids": CATEGORIA_CARTAS,
            "filter": ",".join(filtros),
            "limit": str(min(limite, 200)),
        }
        if recentes:
            params["sort"] = "newlyListed"
        r = requests.get(
            self.SEARCH_URL,
            headers={"Authorization": f"Bearer {self.token()}",
                     "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
            params=params,
            timeout=30,
        )
        if r.status_code == 429:
            raise LimiteEbay()
        r.raise_for_status()
        return [self._converter(i) for i in r.json().get("itemSummaries", []) if i.get("price")]

    @staticmethod
    def _converter(i: dict) -> Anuncio:
        frete = 0.0
        for op in i.get("shippingOptions", []) or []:
            custo = op.get("shippingCost", {}).get("value")
            if custo is not None:
                frete = float(custo)
                break
        vendedor = i.get("seller", {}) or {}
        pct = vendedor.get("feedbackPercentage")
        return Anuncio(
            item_id=i["itemId"],
            titulo=i.get("title", ""),
            preco=float(i["price"]["value"]),
            frete=frete,
            moeda=i["price"].get("currency", "USD"),
            url=i.get("itemWebUrl", ""),
            pais_vendedor=(i.get("itemLocation") or {}).get("country", ""),
            vendedor=vendedor.get("username", ""),
            feedback_pct=float(pct) if pct else None,
            feedback_score=vendedor.get("feedbackScore"),
            imagem=(i.get("image") or {}).get("imageUrl", ""),
        )


# ---------------------------------------------------------------------------
# Preço de mercado
# ---------------------------------------------------------------------------

# Campos do PriceCharting para cartas graduadas (valores em centavos).
CAMPO_PRICECHARTING = {
    7: "cib-price",          # Grade 7
    8: "new-price",          # Grade 8
    9: "graded-price",       # Grade 9
    10: "manual-only-price", # PSA 10
}


def mercado_pricecharting(a: Anuncio) -> tuple[float | None, str]:
    if not PRICECHARTING_TOKEN:
        return None, ""
    pokemon, numero, idioma, edicao, _ = a.chave_carta.split("|")
    q = f"pokemon {pokemon} {numero.replace('#', '')}"
    if idioma == "jp":
        q += " japanese"
    if edicao:
        q += " 1st edition"
    try:
        r = requests.get("https://www.pricecharting.com/api/product",
                         params={"t": PRICECHARTING_TOKEN, "q": q}, timeout=30)
        r.raise_for_status()
        dados = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  PriceCharting falhou ({e}); usando comparáveis do eBay.")
        return None, ""
    valor = dados.get(CAMPO_PRICECHARTING.get(a.nota, ""))
    if not valor:
        return None, ""
    nome = dados.get("product-name", q)
    return valor / 100, f"PriceCharting – {nome} (vendas concluídas)"


def mercado_comparaveis(a: Anuncio, ebay: Ebay, cache: dict) -> tuple[float | None, int, str]:
    pokemon, numero, idioma, edicao, _ = a.chave_carta.split("|")
    if a.chave_carta not in cache:
        consulta = f"{pokemon} {numero.replace('#', '')} PSA {a.nota}"
        if idioma == "jp":
            consulta += " japanese"
        if edicao:
            consulta += " 1st edition"
        resultados = ebay.buscar(consulta, limite=100, recentes=False)
        cache[a.chave_carta] = {
            c.item_id: c.preco + c.frete
            for c in resultados
            if chave_de(c.titulo)[0] == a.chave_carta
        }
    precos = {k: v for k, v in cache[a.chave_carta].items() if k != a.item_id}
    if len(precos) < MIN_COMPARAVEIS:
        return None, len(precos), ""
    return statistics.median(precos.values()), len(precos), "mediana dos anúncios ativos no eBay"


# ---------------------------------------------------------------------------
# Avaliação da oportunidade
# ---------------------------------------------------------------------------

def avaliar(a: Anuncio) -> None:
    a.desconto_pct = (1 - a.custo_total / a.mercado) * 100
    a.lucro_revenda_eua = a.mercado * (1 - TAXA_EBAY) - FRETE_REVENDA_USD - a.custo_total
    frete_br = FRETE_INTERNACIONAL_USD if a.pais_vendedor != "BR" else 0
    imposto = IMPOSTO_IMPORTACAO if a.pais_vendedor != "BR" else 0
    a.custo_brasil_usd = (a.custo_total + frete_br) * (1 + imposto)

    if a.feedback_score is not None and a.feedback_score < 50:
        a.alertas.append(f"vendedor com pouco histórico ({a.feedback_score} avaliações)")
    if a.feedback_pct is not None and a.feedback_pct < 98:
        a.alertas.append(f"feedback do vendedor {a.feedback_pct:.1f}%")
    if a.desconto_pct >= 60:
        a.alertas.append("desconto muito alto: confira se não é golpe ou slab falso")
    if a.fonte_mercado.startswith("mediana") and a.n_comparaveis < 6:
        a.alertas.append(f"poucos comparáveis ({a.n_comparaveis}) para o preço de mercado")
    a.alertas.append("confira o número do certificado em psacard.com/cert antes de comprar")


def veredito(a: Anuncio) -> str:
    graves = [x for x in a.alertas if "golpe" in x or "pouco histórico" in x or "feedback" in x]
    if a.desconto_pct >= 60 and graves:
        return "Provavelmente armadilha"
    if graves or a.lucro_revenda_eua <= 0:
        return "Olhar com cautela"
    return "Vale olhar"


# ---------------------------------------------------------------------------
# Parecer: explica os sinais e diz se vale comprar (regras, sem custo)
# ---------------------------------------------------------------------------

DESTAQUE_POKEMON = {
    "charizard": "Charizard é a carta mais disputada da franquia: tem procura constante e é a primeira que os colecionadores buscam.",
    "pikachu": "Pikachu é o mascote da marca: atrai colecionador e público geral, o que mantém a procura alta.",
    "umbreon": "Umbreon é um dos favoritos da comunidade; suas versões especiais estão entre as mais valorizadas das coleções recentes.",
    "lugia": "Lugia é um lendário muito querido da 2ª geração, com procura forte em versões antigas e de arte especial.",
    "rayquaza": "Rayquaza é um lendário muito procurado; suas versões de arte especial costumam liderar as coleções em que aparecem.",
    "mew": "Mew tem forte apelo nostálgico e versões promocionais muito disputadas.",
    "mewtwo": "Mewtwo é um ícone da 1ª geração, com procura constante entre colecionadores antigos.",
}


def explicar_sinais(a: Anuncio) -> list[str]:
    """Transforma os sinais curtos em frases que explicam o porquê."""
    t = normalizar(a.titulo)
    frases = []
    if a.pokemon in ICONICOS:
        frases.append(DESTAQUE_POKEMON.get(
            a.pokemon,
            f"{a.pokemon.title()} está entre os Pokémon mais colecionados: graduadas dele costumam vender rápido e segurar bem o preço."))
    if any(x in t for x in ("1st edition", "1st ed")):
        frases.append("1ª edição: é a primeira tiragem, a versão mais valorizada pelos colecionadores dessa carta.")
    elif "shadowless" in t:
        frases.append("Shadowless: tiragem inicial do Base Set, bem mais rara que a versão comum.")
    elif any(x in t for x in VINTAGE):
        frases.append("Carta da era clássica (1999–2003): não volta a ser impressa, e cópias bem conservadas ficam mais raras com o tempo.")
    arte = [x.strip() for x in ARTE_ESPECIAL if x in t]
    if arte:
        frases.append("Arte especial (" + arte[0].upper() + "): versões de ilustração especial são as mais procuradas de cada coleção e tendem a liderar a valorização.")
    lim = [x for x in LIMITADA if x in t]
    if lim:
        frases.append(f"Distribuição limitada ({lim[0]}): saiu em pouca quantidade, o que reduz a oferta no futuro.")
    if any(x in t for x in JAPONES):
        frases.append("Carta japonesa: acabamento muito elogiado e mercado crescente entre colecionadores fora do Japão.")
    if a.nota == 10:
        frases.append("PSA 10 é a nota máxima: tem o maior prêmio de preço e é a mais fácil de revender.")
    elif a.nota == 9:
        frases.append("PSA 9 é uma nota alta com boa liquidez, bem mais barata que a 10.")
    elif a.nota in (7, 8):
        frases.append(f"PSA {a.nota}: entrada mais barata na carta; valoriza mais devagar que as notas 9 e 10.")
    return frases or list(a.sinais)


def parecer(a: Anuncio) -> tuple[str, list[str]]:
    """Decisão (Comprar / Talvez / Não comprar) e os motivos, em linguagem simples."""
    v = veredito(a)
    graves = [x for x in a.alertas if "golpe" in x or "pouco histórico" in x or "feedback" in x]
    pct_lucro = a.lucro_revenda_eua / a.custo_total * 100 if a.custo_total else 0
    base_ok = a.fonte_mercado.startswith("PriceCharting") or a.n_comparaveis >= 6
    fortes = len(a.sinais) >= 2 or a.nota in (9, 10)

    a_favor, contra = [], []
    base = (f"{a.n_comparaveis} anúncios iguais" if a.n_comparaveis else a.fonte_mercado)
    a_favor.append(f"Está {a.desconto_pct:.0f}% abaixo do mercado (base: {base}).")
    if a.lucro_revenda_eua > 0:
        a_favor.append(f"Lucro estimado revendendo no eBay: {usd(a.lucro_revenda_eua)} ({pct_lucro:.0f}% sobre o que você paga), já descontando a taxa do eBay e o frete.")
    else:
        contra.append("Depois da taxa do eBay e do frete, a revenda não dá lucro.")
    if fortes:
        a_favor.append("Tem bons sinais de valorização (veja abaixo).")
    if not base_ok:
        contra.append("Poucos anúncios iguais para comparar: o preço de mercado pode estar impreciso.")
    contra += [x[0].upper() + x[1:] + "." for x in graves]
    if a.pais_vendedor != "BR" and a.mercado and a.custo_brasil_usd >= a.mercado:
        contra.append("Trazendo ao Brasil, o custo passa do valor de mercado: compensa mais revender lá fora.")

    if v == "Provavelmente armadilha" or a.lucro_revenda_eua <= 0 or (graves and a.desconto_pct >= 50):
        decisao = "Não comprar"
    elif v == "Vale olhar" and base_ok and fortes and pct_lucro >= 15 and a.lucro_revenda_eua >= 40:
        decisao = "Comprar"
    else:
        decisao = "Talvez"
    return decisao, a_favor + contra


def texto_parecer(a: Anuncio) -> str:
    decisao, motivos = parecer(a)
    linhas = [f"Parecer: {decisao.upper()}"] + [f"• {m}" for m in motivos]
    if decisao != "Não comprar":
        linhas.append("• Antes de comprar: confira o certificado em psacard.com/cert e se as fotos mostram a carta e o slab de verdade.")
    if a.analise_claude:
        linhas += ["", a.analise_claude]
    return "\n".join(linhas)


def analise_claude(a: Anuncio) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    prompt = (
        "Você ajuda um revendedor brasileiro de cartas Pokémon graduadas. Em no máximo 4 linhas, "
        "em português, diga se este anúncio do eBay parece uma boa compra para revenda, o que conferir "
        "nas fotos/slab e o principal risco. Seja direto.\n\n"
        f"Título: {a.titulo}\nPreço+frete: US$ {a.custo_total:.2f}\nMercado estimado: US$ {a.mercado:.2f} "
        f"({a.fonte_mercado})\nDesconto: {a.desconto_pct:.0f}%\nSinais de valorização: {', '.join(a.sinais)}\n"
        f"Vendedor: ({a.feedback_score} avaliações, {a.feedback_pct}% positivo), país {a.pais_vendedor}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:  # noqa: BLE001
        print(f"  Análise do Claude falhou: {e}")
        return ""


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

ICONE_VEREDITO = {"Vale olhar": "🟢", "Olhar com cautela": "🟡", "Provavelmente armadilha": "🔴"}
COR_VEREDITO = {"Vale olhar": "#1a7f37", "Olhar com cautela": "#9a6700", "Provavelmente armadilha": "#cf222e"}


def usd(valor: float) -> str:
    return f"US$ {valor:,.0f}".replace(",", ".")


def foto_grande(url: str) -> str:
    """O eBay devolve miniaturas (s-l225); pede uma versão maior da mesma foto."""
    return re.sub(r"/s-l\d+\.", "/s-l1000.", url) if url else ""


def link_seguro(url: str) -> str:
    return url if url.startswith("https://") else ""


def resumo_alerta(a: Anuncio) -> dict:
    """Tudo que um alerta mostra, já em linguagem simples. Usado no Telegram, na página e no e-mail."""
    v = veredito(a)
    brasil = ""
    if a.pais_vendedor != "BR":
        brasil = f"{usd(a.custo_brasil_usd)} (com frete internacional e {IMPOSTO_IMPORTACAO*100:.0f}% de imposto)"
        if a.custo_brasil_usd >= a.mercado:
            brasil += " — não compensa trazer para revender no Brasil"
    return {
        "titulo": a.titulo,
        "nota": a.nota,
        "veredito": v,
        "icone": ICONE_VEREDITO[v],
        "cor": COR_VEREDITO[v],
        "desconto": f"{a.desconto_pct:.0f}%",
        "preco": usd(a.custo_total),
        "frete": usd(a.frete),
        "mercado": usd(a.mercado),
        "fonte_mercado": a.fonte_mercado + (f" ({a.n_comparaveis} anúncios)" if a.n_comparaveis else ""),
        "lucro": usd(a.lucro_revenda_eua),
        "brasil": brasil,
        "porque": ", ".join(a.sinais),
        "vendedor": f"{a.feedback_score} avaliações · {a.feedback_pct}% positivas · {a.pais_vendedor}",
        "atencao": a.alertas,
        "analise": a.analise_claude,
        "foto": link_seguro(foto_grande(a.imagem)),
        "link": link_seguro(a.url),
        "quando": time.strftime("%d/%m/%Y %H:%M"),
    }


def formatar(a: Anuncio) -> str:
    """Versão em texto simples (janela do terminal)."""
    r = resumo_alerta(a)
    linhas = [
        f"{r['icone']} {r['veredito'].upper()} — {r['desconto']} abaixo do mercado",
        f"🃏 {r['titulo']}",
        f"💰 Preço: {r['preco']} (já com frete)",
        f"📈 Vale no mercado: {r['mercado']}",
        f"💵 Lucro se revender no eBay: {r['lucro']}",
    ]
    if r["brasil"]:
        linhas.append(f"🇧🇷 Custo trazendo ao Brasil: {r['brasil']}")
    linhas.append(f"⭐ Por que pode valorizar: {r['porque']}")
    linhas.append(f"🧭 Parecer: {parecer(a)[0].upper()}")
    for x in r["atencao"]:
        linhas.append(f"⚠️ {x}")
    if r["analise"]:
        linhas.append(f"🤖 {r['analise']}")
    linhas.append(f"🔗 {r['link']}")
    return "\n".join(linhas)


def legenda_telegram(a: Anuncio) -> str:
    """Legenda curta da foto no Telegram (limite de 1024 caracteres)."""
    r = resumo_alerta(a)
    e = html.escape
    linhas = [
        f"{r['icone']} <b>{e(r['veredito'].upper())} — {r['desconto']} abaixo do mercado</b>",
        "",
        f"🃏 {e(r['titulo'])}",
        "",
        f"💰 Preço: <b>{r['preco']}</b> (com frete)",
        f"📈 Mercado: {r['mercado']}",
        f"💵 Lucro revendendo: <b>{r['lucro']}</b>",
    ]
    if r["brasil"]:
        linhas.append(f"🇧🇷 Trazendo ao Brasil: {e(r['brasil'])}")
    linhas.append(f"⭐ {e(r['porque'])}")
    decisao, motivos = parecer(a)
    linhas.append(f"🧭 <b>Parecer: {e(decisao.upper())}</b>")
    graves = [x for x in r["atencao"] if "psacard" not in x]
    if graves:
        linhas.append("⚠️ " + e("; ".join(graves)))
    if r["analise"]:
        linhas.append(f"🤖 {e(r['analise'])}")
    texto = "\n".join(linhas)
    return texto if len(texto) <= 1000 else texto[:997] + "..."


def telegram(metodo: str, dados: dict) -> bool:
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{metodo}", json=dados, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"  Telegram falhou: {e}")
        return False
    if not r.ok:
        print(f"  Telegram falhou: {r.status_code} {r.text[:200]}")
    return r.ok


def enviar_telegram(texto: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    return telegram("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": texto[:4000], "parse_mode": "HTML"})


def enviar_telegram_alerta(a: Anuncio) -> bool:
    """Foto da carta + resumo + botão que abre o anúncio no eBay."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    r = resumo_alerta(a)
    botao = {"inline_keyboard": [[{"text": "🛒 Ver anúncio no eBay", "url": r["link"]}]]} if r["link"] else None
    legenda = legenda_telegram(a)
    if r["foto"]:
        dados = {"chat_id": TELEGRAM_CHAT_ID, "photo": r["foto"], "caption": legenda, "parse_mode": "HTML"}
        if botao:
            dados["reply_markup"] = botao
        if telegram("sendPhoto", dados):
            return True
    # sem foto (ou o Telegram não conseguiu baixar): manda só o texto com o botão
    dados = {"chat_id": TELEGRAM_CHAT_ID, "text": legenda, "parse_mode": "HTML"}
    if botao:
        dados["reply_markup"] = botao
    return telegram("sendMessage", dados)


# --- Página com fotos (abre no navegador) e e-mail ---------------------------

def cartao_html(r: dict) -> str:
    e = html.escape
    foto = (f'<img src="{e(r["foto"])}" alt="Foto do anúncio" loading="lazy">' if r["foto"]
            else '<div class="sem-foto">sem foto</div>')
    atencao = "".join(f"<li>{e(x)}</li>" for x in r["atencao"])
    linha_brasil = f'<tr><td>🇧🇷 Trazendo ao Brasil</td><td>{e(r["brasil"])}</td></tr>' if r["brasil"] else ""
    analise = f'<p class="analise">🤖 {e(r["analise"])}</p>' if r["analise"] else ""
    botao = (f'<a class="botao" href="{e(r["link"])}" target="_blank" rel="noopener">🛒 Ver anúncio no eBay</a>'
             if r["link"] else "")
    return f"""
<article class="cartao">
  <a class="foto" href="{e(r["link"]) or '#'}" target="_blank" rel="noopener">{foto}</a>
  <div class="info">
    <div class="selo" style="background:{r["cor"]}">{r["icone"]} {e(r["veredito"])} · {r["desconto"]} abaixo do mercado</div>
    <h2>{e(r["titulo"])}</h2>
    <table>
      <tr><td>💰 Preço (com frete)</td><td><b>{r["preco"]}</b></td></tr>
      <tr><td>📈 Vale no mercado</td><td>{r["mercado"]} <small>{e(r["fonte_mercado"])}</small></td></tr>
      <tr><td>💵 Lucro revendendo no eBay</td><td><b>{r["lucro"]}</b> <small>já tirando taxa e frete</small></td></tr>
      {linha_brasil}
      <tr><td>⭐ Por que pode valorizar</td><td>{e(r["porque"])}</td></tr>
      <tr><td>👤 Vendedor</td><td>{e(r["vendedor"])}</td></tr>
    </table>
    <ul class="atencao">{atencao}</ul>
    {analise}
    {botao}
    <p class="quando">Encontrado em {e(r["quando"])}</p>
  </div>
</article>"""


ESTILO_PAGINA = """
:root { --fundo:#f4f5f7; --cartao:#fff; --texto:#1f2328; --suave:#656d76; --linha:#e5e7eb; --botao:#0064d2; }
@media (prefers-color-scheme: dark) {
  :root { --fundo:#0d1117; --cartao:#161b22; --texto:#e6edf3; --suave:#9198a1; --linha:#30363d; --botao:#3b8eea; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--fundo); color:var(--texto);
       font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { padding:24px 16px 8px; max-width:960px; margin:auto; }
header h1 { margin:0 0 4px; font-size:24px; }
header p { margin:0; color:var(--suave); }
main { max-width:960px; margin:auto; padding:8px 16px 40px; display:grid; gap:16px; }
.cartao { display:grid; grid-template-columns:260px 1fr; gap:20px; background:var(--cartao);
          border:1px solid var(--linha); border-radius:14px; padding:16px; }
.foto img, .sem-foto { width:100%; aspect-ratio:3/4; object-fit:contain; border-radius:10px; background:var(--fundo); display:block; }
.sem-foto { display:flex; align-items:center; justify-content:center; color:var(--suave); }
.selo { display:inline-block; color:#fff; font-weight:600; padding:4px 12px; border-radius:999px; font-size:14px; }
h2 { font-size:18px; margin:10px 0; }
table { border-collapse:collapse; width:100%; font-size:15px; }
td { padding:6px 0; border-bottom:1px solid var(--linha); vertical-align:top; }
td:first-child { color:var(--suave); width:46%; padding-right:12px; }
small { color:var(--suave); }
.atencao { margin:12px 0; padding-left:20px; color:var(--suave); font-size:14px; }
.analise { background:var(--fundo); padding:10px 12px; border-radius:8px; font-size:14px; }
.botao { display:inline-block; background:var(--botao); color:#fff; text-decoration:none; font-weight:600;
         padding:12px 20px; border-radius:10px; margin-top:4px; }
.quando { color:var(--suave); font-size:13px; margin:12px 0 0; }
.vazio { text-align:center; color:var(--suave); padding:40px; }
@media (max-width:640px) { .cartao { grid-template-columns:1fr; } .foto img, .sem-foto { max-height:360px; } }
"""


def pagina_html(resumos: list[dict], titulo: str = "Radar eBay — cartas PSA abaixo do mercado") -> str:
    e = html.escape
    cartoes = "".join(cartao_html(r) for r in resumos) or '<p class="vazio">Nenhum alerta ainda.</p>'
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radar eBay</title><style>{ESTILO_PAGINA}</style></head>
<body><header><h1>🃏 {e(titulo)}</h1>
<p>{len(resumos)} alerta(s), o mais novo primeiro. Clique na foto ou no botão para abrir o anúncio.
Sempre confira o certificado em psacard.com/cert antes de comprar.</p></header>
<main>{cartoes}</main></body></html>"""


def salvar_pagina(novos: list[Anuncio]) -> Path:
    """Guarda os alertas em alertas.json e refaz alertas.html (histórico, mais novo primeiro)."""
    try:
        historico = json.loads(ARQUIVO_ALERTAS.read_text())
    except Exception:  # noqa: BLE001
        historico = []
    historico = [resumo_alerta(a) for a in novos] + historico
    historico = historico[:200]
    ARQUIVO_ALERTAS.write_text(json.dumps(historico, ensure_ascii=False))
    pagina = ARQUIVO_ALERTAS.with_suffix(".html")
    pagina.write_text(pagina_html(historico), encoding="utf-8")
    return pagina


def abrir_no_navegador(pagina: Path) -> None:
    if ABRIR_PAGINA and not os.environ.get("GITHUB_ACTIONS"):
        webbrowser.open(pagina.resolve().as_uri())


def enviar_email(assunto: str, texto: str, html_corpo: str = "") -> bool:
    if not (SMTP_USER and SMTP_PASSWORD and EMAIL_TO):
        return False
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    if html_corpo:
        msg.attach(MIMEText(html_corpo, "html", "utf-8"))
    msg["Subject"] = assunto
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  E-mail falhou: {e}")
        return False


def linha_app(a: Anuncio) -> dict:
    """Uma linha da tabela "alertas" do app (mesmos nomes de coluna do prompt-lovable.md)."""
    return {
        "item_id": a.item_id,
        "titulo": a.titulo,
        "nota": a.nota,
        "veredito": veredito(a),
        "desconto_pct": round(a.desconto_pct, 1),
        "preco_usd": round(a.custo_total, 2),
        "frete_usd": round(a.frete, 2),
        "mercado_usd": round(a.mercado, 2),
        "fonte_mercado": a.fonte_mercado,
        "n_comparaveis": a.n_comparaveis,
        "lucro_usd": round(a.lucro_revenda_eua, 2),
        "custo_brasil_usd": round(a.custo_brasil_usd, 2) if a.pais_vendedor != "BR" else None,
        "sinais": explicar_sinais(a),
        "avisos": a.alertas,
        "vendedor": None,  # não guardamos o nome de usuário (isenção do eBay)
        "vendedor_avaliacoes": a.feedback_score,
        "vendedor_positivas_pct": a.feedback_pct,
        "pais_vendedor": a.pais_vendedor,
        "foto_url": link_seguro(foto_grande(a.imagem)) or None,
        "link_ebay": link_seguro(a.url) or None,
        "analise": texto_parecer(a),
    }


def enviar_app(oportunidades: list[Anuncio]) -> bool:
    """Grava as cartas no banco do app; anúncio repetido é ignorado."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return False
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/alertas",
            params={"on_conflict": "item_id"},
            headers={"apikey": SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=[linha_app(a) for a in oportunidades],
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  App (Supabase) falhou: {e}")
        return False
    if not r.ok:
        print(f"  App (Supabase) falhou: {r.status_code} {r.text[:200]}")
    return r.ok


def notificar(oportunidades: list[Anuncio]) -> None:
    if not oportunidades:
        return
    ordem = {"Vale olhar": 0, "Olhar com cautela": 1, "Provavelmente armadilha": 2}
    oportunidades.sort(key=lambda x: (ordem[veredito(x)], -x.lucro_revenda_eua))
    melhor = max(oportunidades, key=lambda x: x.desconto_pct)
    resumo = (f"{len(oportunidades)} carta(s) PSA abaixo do mercado no eBay, "
              f"a melhor com {melhor.desconto_pct:.0f}% de desconto")
    corpo = "\n\n".join(formatar(a) for a in oportunidades)

    pagina = salvar_pagina(oportunidades)
    enviado = enviar_app(oportunidades)
    if TELEGRAM_BOT_TOKEN:
        aviso = f"🔔 <b>{html.escape(resumo)}</b>"
        if link_seguro(LINK_APP):
            aviso += f'\n📱 <a href="{html.escape(LINK_APP)}">Abrir no app Radar PSA</a>'
        enviado |= enviar_telegram(aviso)
        for a in oportunidades:
            enviado |= enviar_telegram_alerta(a)
    enviado |= enviar_email(f"Radar eBay: {resumo}", f"{resumo}\n\n{corpo}",
                            pagina_html([resumo_alerta(a) for a in oportunidades], resumo))
    print(f"\n🔔 {resumo}\n\n{corpo}")
    print(f"\n📄 Página com fotos: {pagina}")
    if not enviado:
        print("(App, Telegram e e-mail ainda não configurados; abrindo a página no navegador.)")
    abrir_no_navegador(pagina)


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def carregar_vistos() -> dict:
    try:
        return json.loads(ARQUIVO_VISTOS.read_text())
    except Exception:  # noqa: BLE001
        return {}


def salvar_vistos(vistos: dict) -> None:
    limite = time.time() - 30 * 86400  # esquece anúncios com mais de 30 dias
    vistos = {k: v for k, v in vistos.items() if v > limite}
    ARQUIVO_VISTOS.write_text(json.dumps(vistos))


def varredura(ebay: Ebay, fonte_anuncios=None) -> list[Anuncio]:
    vistos = carregar_vistos()
    cache_comp: dict = {}
    novos: dict[str, Anuncio] = {}

    buscas = fonte_anuncios() if fonte_anuncios else None
    if buscas is None:
        for consulta in BUSCAS:
            try:
                resultados = ebay.buscar(consulta, preco_min=VALOR_MERCADO_MINIMO * 0.3)
            except LimiteEbay:
                print("  eBay: limite de chamadas atingido; o resto fica para a próxima rodada.")
                break
            for a in resultados:
                if a.item_id not in vistos:
                    novos[a.item_id] = a
    else:
        novos = {a.item_id: a for a in buscas if a.item_id not in vistos}

    print(f"{time.strftime('%H:%M')} – {len(novos)} anúncios novos para analisar")
    oportunidades = []
    descartes: dict[str, int] = {}
    adiados = 0          # sem preço de mercado nesta rodada; NÃO entram em vistos
    sem_cota = False     # eBay recusou ou o teto de buscas por rodada foi atingido
    for a in novos.values():
        motivo = classificar(a)
        if motivo:
            vistos[a.item_id] = time.time()
            descartes[motivo.split(" (")[0]] = descartes.get(motivo.split(" (")[0], 0) + 1
            continue
        mercado, fonte = mercado_pricecharting(a)
        if mercado is None:
            if a.chave_carta not in cache_comp and (sem_cota or len(cache_comp) >= MAX_BUSCAS_COMPARAVEIS):
                sem_cota = True
                adiados += 1
                continue
            try:
                mercado, a.n_comparaveis, fonte = mercado_comparaveis(a, ebay, cache_comp)
            except LimiteEbay:
                sem_cota = True
                adiados += 1
                continue
        vistos[a.item_id] = time.time()
        if mercado is None:
            descartes["sem preço de mercado confiável"] = descartes.get("sem preço de mercado confiável", 0) + 1
            continue
        a.mercado, a.fonte_mercado = mercado, fonte
        if mercado < VALOR_MERCADO_MINIMO:
            descartes["mercado abaixo do mínimo"] = descartes.get("mercado abaixo do mínimo", 0) + 1
            continue
        if a.custo_total > mercado * (1 - DESCONTO_MINIMO / 100):
            descartes["desconto insuficiente"] = descartes.get("desconto insuficiente", 0) + 1
            continue
        avaliar(a)
        a.analise_claude = analise_claude(a)
        oportunidades.append(a)

    salvar_vistos(vistos)
    if descartes:
        print("  descartados: " + ", ".join(f"{k}: {v}" for k, v in sorted(descartes.items())))
    if adiados:
        print(f"  {adiados} anúncio(s) adiados para a próxima rodada (limite de buscas no eBay).")
    if oportunidades:
        notificar(oportunidades)
    else:
        print(f"  Nenhuma carta PSA {NOTA_MINIMA}–{NOTA_MAXIMA} {DESCONTO_MINIMO:.0f}%+ abaixo do mercado.")
    return oportunidades


def main() -> None:
    p = argparse.ArgumentParser(description="Radar eBay de cartas Pokémon PSA abaixo do mercado")
    p.add_argument("--loop", action="store_true", help="roda continuamente a cada INTERVALO_MINUTOS")
    p.add_argument("--teste", action="store_true", help="usa anúncios fictícios, sem chamar o eBay")
    args = p.parse_args()

    if args.teste:
        from teste_simulado import rodar_teste
        rodar_teste()
        return

    ebay = Ebay(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_MARKETPLACE)
    if not args.loop:
        varredura(ebay)
        return
    while True:
        try:
            varredura(ebay)
        except Exception as e:  # noqa: BLE001
            print(f"Erro na varredura: {e}")
        time.sleep(INTERVALO_MINUTOS * 60)


if __name__ == "__main__":
    main()
