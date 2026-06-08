# Spendee → Google Sheets automatizace

Samostatný Python projekt pro automatické načítání aktuálních zůstatků z více Spendee peněženek a zápis do Google Sheets. Projekt je oddělený od webu `hejaboys.cz`, nepoužívá PythonAnywhere backend ani webové UI.

## Co script dělá

1. Načte konfiguraci z environment variables / GitHub Secrets.
2. Získá Spendee access token:
   - preferovaně přes refresh token flow,
   - případně fallbackem přes `SPENDEE_TOKEN`.
3. Zavolá Spendee endpoint:

   ```text
   GET https://api.spendee.com/v1.4/wallet-get-all
   ```

4. Načte všechny vrácené peněženky a podle `SPENDEE_WALLET_MAPPINGS` najde více konkrétních wallet ID.
5. Pro každou namapovanou peněženku vytáhne `balance` a `currency`.
6. Zapíše / přepíše záznam v Google Sheets v listu `Zaznamy`.

Úspěšný běh vypíše pouze:

```text
Updated Google Sheet successfully.
```

Script neloguje tokeny, refresh tokeny, service account JSON, wallet detaily ani osobní údaje.

## Cílový list `Zaznamy`

Script pracuje s listem `Zaznamy`. Pokud list neexistuje, vytvoří ho. Pokud je první řádek prázdný, doplní hlavičku:

| Sloupec | Název | Význam |
| --- | --- | --- |
| A | `Datum` | UTC timestamp poslední aktualizace. |
| B | `ID` | Interní ID z mapování. Podle tohoto sloupce se hledá existující řádek. |
| C | `Hodnota_CZK` | Aktuální `balance` ze Spendee. |
| D | `Zdroj` | Vždy `Spendee API`. |
| E | `Poznamka` | Lidsky čitelný název účtu z mapování. |

Důležité: script se orientuje primárně podle interního ID ve sloupci B. Sloupec E je jen poznámka pro člověka.

Při každém spuštění:

- pokud řádek se stejným interním ID ve sloupci B už existuje, přepíše se celý řádek `A:E` aktuálními daty,
- pokud interní ID neexistuje, přidá se nový řádek na konec listu.

`Hodnota_CZK` zatím nepřepočítává měny. Script zapisuje hodnotu `balance`, kterou vrátí Spendee pro danou peněženku. Pokud některá peněženka není v CZK, je potřeba později doplnit konverzní logiku nebo zajistit CZK hodnotu ve Spendee.

## Požadované secrets / environment variables

### Spendee

| Proměnná | Povinná | Popis |
| --- | --- | --- |
| `SPENDEE_TOKEN` | povinná jen bez refresh flow | Fallback access token bez prefixu `Bearer`. |
| `SPENDEE_TOKEN_URL` | povinná pro refresh flow | Token endpoint ze Spendee webu pro `grant_type=refresh_token`. |
| `SPENDEE_REFRESH_TOKEN` | povinná pro refresh flow | Refresh token ze Spendee auth response. |
| `SPENDEE_CLIENT_ID` | volitelná | Client ID, pokud ho token endpoint vyžaduje. |
| `SPENDEE_CLIENT_SECRET` | volitelná | Client secret, pokud ho token endpoint vyžaduje. |
| `SPENDEE_TOKEN_AUTH_MODE` | volitelná | Výchozí hodnota je `body`. Při hodnotě `basic` se `client_id` a `client_secret` posílají přes HTTP Basic auth. |
| `SPENDEE_DEVICE_UUID` | ano | Device UUID posílané v povinné Spendee hlavičce `device-uuid`. |
| `SPENDEE_WALLET_MAPPINGS` | doporučeno | JSON mapování více Spendee wallet ID na interní ID a poznámku. |
| `SPENDEE_WALLET_ID` | fallback | Zpětná kompatibilita pro jednu peněženku, když není nastavené `SPENDEE_WALLET_MAPPINGS`. |
| `SPENDEE_INTERNAL_ID` | volitelné pro fallback | Interní ID pro fallback jednu peněženku. Pokud chybí, použije se `SPENDEE_WALLET_ID`. |
| `SPENDEE_WALLET_NOTE` | volitelné pro fallback | Poznámka pro fallback jednu peněženku. |

### Google Sheets

| Proměnná | Povinná | Popis |
| --- | --- | --- |
| `GOOGLE_SHEET_ID` | ano | ID cílového Google Sheetu z URL dokumentu. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ano | Service account JSON jako raw JSON, base64 encoded JSON nebo lokální cesta k JSON souboru. |

### Volitelná konfigurace

| Proměnná | Výchozí hodnota | Popis |
| --- | --- | --- |
| `GOOGLE_SHEET_RECORDS_SHEET_NAME` | `Zaznamy` | Název listu, kam se zapisují záznamy. |

## Mapování více Spendee peněženek

Doporučené nastavení je jeden GitHub Secret `SPENDEE_WALLET_MAPPINGS` s JSONem. Hodnoty níže jsou pouze bezpečný příklad, nepoužívejte reálná ID v README ani v commitu.

Doporučený formát jako objekt:

```json
{
  "spendee-wallet-id-kb": {
    "internal_id": "KB_OSOBNI",
    "note": "KB osobní"
  },
  "spendee-wallet-id-kb-sporici": {
    "internal_id": "KB_SPORICI",
    "note": "KB spořící"
  },
  "spendee-wallet-id-partners-osobni": {
    "internal_id": "PARTNERS_OSOBNI",
    "note": "Partners osobní"
  },
  "spendee-wallet-id-partners-spolecny": {
    "internal_id": "PARTNERS_SPOLECNY",
    "note": "Partners společný"
  },
  "spendee-wallet-id-revolut": {
    "internal_id": "REVOLUT",
    "note": "Revolut"
  },
  "spendee-wallet-id-airbank": {
    "internal_id": "AIRBANK",
    "note": "Air Bank"
  }
}
```

Podporovaný je i formát jako pole:

```json
[
  {
    "spendee_wallet_id": "spendee-wallet-id-kb",
    "internal_id": "KB_OSOBNI",
    "note": "KB osobní"
  },
  {
    "spendee_wallet_id": "spendee-wallet-id-revolut",
    "internal_id": "REVOLUT",
    "note": "Revolut"
  }
]
```

Script hledá Spendee wallet ID v odpovědi přes možné klíče `id`, `wallet_id` a `uuid`. Interní ID se zapisuje do sloupce B a používá se pro upsert existujícího řádku.


## Jak získat Spendee wallet ID

Spendee wallet ID získáte jednorázově přes endpoint `wallet-get-all`. **Lepší varianta je použít refresh token flow** a z něj si lokálně vyžádat krátkodobý `ACCESS_TOKEN`; ručně zkopírovaný Bearer token z prohlížeče používejte jen jednorázově pro kontrolu / debug.

1. Pokud máte refresh token flow, nastavte lokálně existující hodnoty:

   ```bash
   export SPENDEE_TOKEN_URL="https://..."
   export SPENDEE_REFRESH_TOKEN="..."
   export SPENDEE_DEVICE_UUID="..."
   ```

2. Z refresh tokenu si vyžádejte krátkodobý access token:

   ```bash
   ACCESS_TOKEN=$(curl -sS -X POST "$SPENDEE_TOKEN_URL" \
     -H "accept: application/json, text/plain, */*" \
     -H "device-uuid: $SPENDEE_DEVICE_UUID" \
     -H "origin: https://app.spendee.com" \
     -H "referer: https://app.spendee.com/" \
     -H "spendee-platform: web" \
     -H "spendee-version: master" \
     --data-urlencode "grant_type=refresh_token" \
     --data-urlencode "refresh_token=$SPENDEE_REFRESH_TOKEN" \
     | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
   ```

   Pokud token endpoint vyžaduje `client_id` / `client_secret`, přidejte je do form body stejně jako ve workflow/scriptu.

3. Vypište ID a názvy peněženek. Tohle je nejrychlejší varianta s `jq`:

   ```bash
   curl -sS 'https://api.spendee.com/v1.4/wallet-get-all' \
     -H "Authorization: Bearer $ACCESS_TOKEN" \
     -H "device-uuid: $SPENDEE_DEVICE_UUID" \
     -H "accept: application/json, text/plain, */*" \
     -H "origin: https://app.spendee.com" \
     -H "referer: https://app.spendee.com/" \
     -H "spendee-platform: web" \
     -H "spendee-version: master" \
     | jq '.result[] | {id, name, balance, currency, status}'
   ```

   Pokud už máte jednorázový token z prohlížeče v proměnné `TOKEN`, můžete pro tento diagnostický krok použít `-H "Authorization: Bearer $TOKEN"`. Do GitHub Secrets pro automatizaci ho ale ukládat nemusíte, pokud funguje refresh token flow.

4. Hodnotu `id` ze Spendee použijte jako klíč v `SPENDEE_WALLET_MAPPINGS`. Například pokud Spendee vypíše Air Bank s ID `123456789` a v dashboardu je interní ID `air_bank`, secret bude obsahovat:

   ```json
   {
     "123456789": {
       "internal_id": "air_bank",
       "note": "Air Bank"
     }
   }
   ```

Pro přidání další banky stačí postup zopakovat, najít její `id` ve výpisu a přidat další položku do `SPENDEE_WALLET_MAPPINGS`. Reálná ID je praktičtější držet v GitHub Secretu; do README patří hlavně postup a případně neprodukční příklady.

### Když GitHub Action hlásí, že `result` není pole

Pokud lokální `curl` s tokenem z prohlížeče funguje, ale GitHub Action vrací chybu typu `Spendee wallet response does not contain a wallet array`, není to problém mapování peněženek. Znamená to, že Spendee API vrátilo chybovou odpověď, typicky `status='ERROR'` a `result=null`, takže script žádný seznam peněženek nedostal.

Script v takové situaci vypíše jen bezpečné shrnutí odpovědi bez tokenů a bez detailů peněženek. Pokud je `error` objekt, nově se vypíšou i jeho bezpečné položky jako `error.code`, `error.message`, `error.status` nebo `error.service`, aby bylo jasnější, proč Spendee request odmítlo.

Co zkontrolovat:

1. `SPENDEE_DEVICE_UUID` v GitHub Secrets musí být stejný jako v lokálním funkčním `curl` příkazu.
2. `SPENDEE_TOKEN_URL` musí být skutečný token endpoint, který vrací `access_token` použitelný pro `https://api.spendee.com/v1.4/wallet-get-all`.
3. Pokud token endpoint potřebuje klientské údaje, doplňte `SPENDEE_CLIENT_ID`, `SPENDEE_CLIENT_SECRET` a případně `SPENDEE_TOKEN_AUTH_MODE`.
4. Pro rychlé ověření vložte do GitHub Secret `SPENDEE_TOKEN` stejný krátkodobý token, se kterým vám fungoval lokální `curl` — bez prefixu `Bearer`. Script ho zkusí jako fallback, pokud refresh token flow vrátí token, se kterým nejde načíst wallet list.
5. Pokud fallback `SPENDEE_TOKEN` projde, problém je potvrzeně v refresh flow / token endpointu, ne v Google Sheets ani v `SPENDEE_WALLET_MAPPINGS`.

## Spendee autentizace

Preferovaná varianta je **refresh token flow**. Nastavte alespoň:

- `SPENDEE_TOKEN_URL`
- `SPENDEE_REFRESH_TOKEN`
- `SPENDEE_DEVICE_UUID`

Pokud token endpoint vyžaduje klientské údaje, nastavte také `SPENDEE_CLIENT_ID` a `SPENDEE_CLIENT_SECRET`.

Výchozí `SPENDEE_TOKEN_AUTH_MODE=body` posílá `client_id` a `client_secret` do form body. Při `SPENDEE_TOKEN_AUTH_MODE=basic` se použije HTTP Basic auth.

`SPENDEE_TOKEN` je fallback pro situaci, kdy refresh token flow není nastavené, a také dočasná záchrana, když refresh token vrátí access token, se kterým Spendee nevrátí seznam peněženek. Hodnota musí být samotný access token bez prefixu `Bearer`.

## Google service account JSON

Rychle: najdete / vytvoříte ho v **Google Cloud Console → IAM & Admin → Service Accounts → vybraný service account → Keys → Add key → Create new key → JSON**. Stáhne se `.json` soubor; celý jeho obsah vložte do GitHub Secret `GOOGLE_SERVICE_ACCOUNT_JSON`.

Vypadá zhruba takto — hodnoty níže jsou jen ukázka:

```json
{
  "type": "service_account",
  "project_id": "my-project",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "nazev-service-accountu@my-project.iam.gserviceaccount.com",
  "client_id": "..."
}
```

1. V Google Cloud Console vytvořte nebo vyberte projekt.
2. Povolte **Google Sheets API**.
3. Vytvořte **Service account**.
4. Vytvořte pro service account JSON key.
5. Obsah JSON klíče uložte do GitHub Secret `GOOGLE_SERVICE_ACCOUNT_JSON` jednou z těchto forem:
   - raw JSON text,
   - base64 encoded JSON,
   - pro lokální běh můžete použít cestu k lokálnímu JSON souboru.

Service account JSON nikdy necommitujte do repozitáře a nevypisujte ho do logů.

## Nasdílení Google Sheetu service accountu

Cílový Google Sheet musí být nasdílený service accountu jako editor:

1. Otevřete JSON klíč service accountu a najděte hodnotu `client_email`.
2. Otevřete cílový Google Sheet.
3. Klikněte na **Share / Sdílet**.
4. Přidejte `client_email` jako uživatele s oprávněním **Editor**.

Bez tohoto sdílení bude Google Sheets API vracet chybu oprávnění.

## Lokální spuštění

Vytvořte virtuální prostředí a nainstalujte závislosti:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Nastavte proměnné prostředí, například:

```bash
export SPENDEE_TOKEN_URL="https://..."
export SPENDEE_REFRESH_TOKEN="..."
export SPENDEE_DEVICE_UUID="..."
export SPENDEE_WALLET_MAPPINGS='{"spendee-wallet-id-kb":{"internal_id":"KB_OSOBNI","note":"KB osobní"}}'
export GOOGLE_SHEET_ID="..."
export GOOGLE_SERVICE_ACCOUNT_JSON="/bezpecna/lokalni/cesta/service-account.json"
```

Spusťte script:

```bash
python scripts/update_spendee_google_sheets.py
```

## Spuštění přes GitHub Actions

Workflow je v `.github/workflows/update-spendee-google-sheets.yml`.

Spouští se:

- ručně přes `workflow_dispatch`,
- automaticky každý den v `06:00 UTC` přes cron `0 6 * * *`.

V GitHub repozitáři nastavte secrets v **Settings → Secrets and variables → Actions**. Poté spusťte workflow ručně nebo počkejte na plánovaný běh.

## Poznámka k lokálnímu `.xlsx`

V pracovním stromu repozitáře jsem při úpravě nenašel žádný `.xlsx` / `.xls` / `.xlsm` soubor. Script proto cílí na Google Sheet a list `Zaznamy` podle popisu výše. Pokud má existující Excel dashboard obsahovat další konkrétní interní ID nebo jiné listy/sloupce, je potřeba dodat buď daný soubor do repozitáře, nebo vypsat přesná interní ID a očekávané řádky.

## Bezpečnostní upozornění

- Nikdy necommitujte Spendee tokeny, refresh tokeny, client secret ani Google service account JSON.
- Neposílejte service account JSON do kódu ani do README příkladů s reálnými hodnotami.
- Nelogujte secrets, tokeny, wallet detaily, osobní údaje ani service account JSON.
- Při chybách script vypisuje jen obecné technické informace, ne citlivé hodnoty.
