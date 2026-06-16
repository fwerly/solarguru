# SolarGuru ☀️

Dashboard pessoal de energia solar (inversor Growatt MIN 6000TL-X2, via API ShinePhone)
com análise didática da conta de luz da Enel RJ.

## Rodar localmente
1. `setup.bat` (uma vez — cria o ambiente e instala dependências)
2. `run.bat` (abre no navegador)

As credenciais ficam no arquivo `.env` (não versionado).

## Publicar no Streamlit Community Cloud

1. Este repositório já está no GitHub.
2. Acesse https://share.streamlit.io → **New app** → **Deploy a public app from GitHub**.
3. Escolha:
   - **Repository:** `fwerly/solarguru`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Clique em **Advanced settings → Secrets** e cole (formato TOML):

   ```toml
   GROWATT_USERNAME = "seu_usuario_shinephone"
   GROWATT_PASSWORD = "sua_senha_growatt"
   ENEL_PDF_PASSWORD = "5_primeiros_digitos_do_cpf"
   # Opcionais (já têm padrão no código; só para sobrescrever):
   # TARIFA_TE_KWH = "0.42941"
   # TARIFA_TUSD_KWH = "0.95395"
   # BANDEIRA_ATUAL = "amarela"
   # CIP_MENSAL = "42.17"
   ```
   (os valores reais você cola só no painel do Streamlit, nunca aqui no repositório)
5. **Deploy**. Em ~2 min o app fica no ar num link `https://....streamlit.app`.

> As senhas vão **só** nos Secrets da plataforma — nunca no código (o `.env` é ignorado pelo git).

## Observação sobre o histórico de faturas
O `bills.json` (faturas já importadas) viaja no repositório, então o histórico aparece no ar.
PDFs novos enviados pela tela funcionam durante a sessão, mas o disco do Streamlit Cloud é
temporário: ao reiniciar o app, novos uploads sem commit se perdem. Para manter, reimporte
o PDF localmente e faça commit do `bills.json`, ou me peça para evoluir para um banco de dados.
