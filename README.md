# Radar eBay — cartas Pokémon PSA abaixo do mercado

Vigia o eBay a cada 15 minutos e avisa no Telegram ou por e-mail quando aparece uma carta Pokémon **graduada PSA 7 a 10**, com **potencial de valorização**, anunciada **30% ou mais abaixo do preço de mercado**.

Ele nunca compra nem dá lance. Só avisa.

## Como ele decide

1. Busca os anúncios mais novos de compra imediata pela **API oficial do eBay**.
2. Descarta lotes, cartas sem graduação, outras graduadoras (CGC, BGS, TAG), notas abaixo de 7 e anúncios suspeitos (proxy, reprint, custom).
3. Exige pelo menos um **sinal de valorização**:
   - Pokémon icônico (Charizard, Pikachu, Umbreon, Mew, Lugia...);
   - vintage de 1999 a 2003;
   - arte especial (SIR, Alt Art, Gold Star...);
   - promo ou edição limitada;
   - carta japonesa.
4. Calcula o **preço de mercado** da mesma carta na mesma nota:
   - pelo PriceCharting (vendas concluídas), se você tiver o token;
   - ou pela mediana de pelo menos 4 outros anúncios ativos da mesma carta e nota no eBay.
5. Se passar no desconto mínimo, manda o alerta com:
   - desconto e lucro estimado (já com taxa do eBay e frete);
   - custo de trazer para o Brasil (frete e 60% de imposto);
   - riscos do vendedor e do anúncio;
   - link e veredito: **Vale olhar**, **Olhar com cautela** ou **Provavelmente armadilha**.

Os limites ficam no `.env`: desconto mínimo, notas, valor mínimo e custos. As buscas e as listas de Pokémon icônicos ficam no topo do `radar.py`.

## Passo a passo

### 1. Chaves do eBay (grátis)
1. Crie uma conta em <https://developer.ebay.com> e entre.
2. Vá em **Application Keys** e crie um keyset de **Production**.
3. Se o eBay pedir, configure a opção de **notificações de exclusão de conta** (Marketplace Account Deletion). Para quem só lê anúncios, dá para marcar que o app não guarda dados de usuários (opção de isenção).
4. Copie o **App ID (Client ID)** e o **Cert ID (Client Secret)**.

### 2. Bot do Telegram (recomendado)
1. No Telegram, fale com **@BotFather**, mande `/newbot` e copie o token.
2. Mande qualquer mensagem para o seu bot.
3. Abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` no navegador e copie o número em `"chat":{"id": ...}`.

### 3. Configurar e testar no seu computador
```bash
cd radar-ebay-pokemon
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # preencha as chaves
python radar.py --teste     # teste com anúncios fictícios (não usa o eBay)
python radar.py             # uma varredura real
python radar.py --loop      # roda direto a cada 15 min (computador ligado)
```

### 4. Rodar sozinho na nuvem, sem deixar o computador ligado (GitHub Actions, grátis)
1. Crie um repositório **privado** no GitHub e suba esta pasta.
2. No repositório, vá em **Settings > Secrets and variables > Actions** e crie os secrets:
   - `EBAY_CLIENT_ID` e `EBAY_CLIENT_SECRET`;
   - `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`;
   - opcionais: `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, `PRICECHARTING_TOKEN`, `ANTHROPIC_API_KEY`.
3. Na aba **Actions**, ative os workflows e clique em **Run workflow** para testar. Depois ele roda sozinho a cada 15 minutos.

## Limitações honestas
- **Preço de mercado sem PriceCharting** usa a mediana de anúncios ativos, ou seja, preço pedido e não preço vendido. Isso pode inflar o "mercado". Por isso ele exige vários comparáveis e avisa quando há poucos. Para mais precisão, assine o PriceCharting e coloque o token.
- A identificação da carta é feita pelo título: nome do Pokémon e número (ex.: 215/203). Títulos sem número são ignorados, para evitar comparar cartas diferentes.
- "Potencial de valorização" é uma estimativa por regras, não uma garantia.
- Sempre confira o certificado em <https://www.psacard.com/cert> e as fotos do slab antes de comprar.
