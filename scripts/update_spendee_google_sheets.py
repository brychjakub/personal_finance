#!/usr/bin/env python3
"""Update selected Spendee wallet data in Google Sheets."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests.auth import HTTPBasicAuth

SPENDEE_WALLETS_URL = "https://api.spendee.com/v1.4/wallet-get-all"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_RECORDS_SHEET_NAME = "Zaznamy"
RECORDS_HEADER = ["Datum", "ID", "Hodnota_CZK", "Zdroj", "Poznamka"]
SPENDEE_SOURCE_NAME = "Spendee API"
SAFE_SPENDEE_RESPONSE_KEYS = ("error", "error_description", "message", "code", "status", "service")
SAFE_SPENDEE_NESTED_KEYS = ("error", "error_description", "message", "code", "status", "type", "name", "service")
SAFE_SPENDEE_MAX_VALUE_LENGTH = 180
SAFE_GOOGLE_ERROR_KEYS = ("message", "status", "code")


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


class SpendeeError(Exception):
    """Raised when communication with Spendee fails."""


class GoogleSheetsError(Exception):
    """Raised when communication with Google Sheets fails."""


def log_progress(message: str) -> None:
    """Print a non-sensitive progress marker for GitHub Actions logs."""
    print(f"Progress: {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class WalletMapping:
    spendee_wallet_id: str
    internal_id: str
    note: str


@dataclass(frozen=True)
class SheetRecord:
    date: str
    internal_id: str
    value_czk: Any
    source: str
    note: str


@dataclass(frozen=True)
class Config:
    spendee_token: str | None
    spendee_token_url: str | None
    spendee_refresh_token: str | None
    spendee_client_id: str | None
    spendee_client_secret: str | None
    spendee_token_auth_mode: str
    spendee_device_uuid: str
    wallet_mappings: list[WalletMapping]
    google_sheet_id: str
    google_service_account_json: str
    records_sheet_name: str = DEFAULT_RECORDS_SHEET_NAME


def _get_optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_required_env(name: str) -> str:
    value = _get_optional_env(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _require_mapping_field(value: Any, field_name: str, mapping_index: str) -> str:
    if value is None or str(value).strip() == "":
        raise ConfigError(f"SPENDEE_WALLET_MAPPINGS item {mapping_index} is missing {field_name}.")
    return str(value).strip()


def _wallet_mapping_from_dict(item: dict[str, Any], mapping_index: str) -> WalletMapping:
    spendee_wallet_id = _require_mapping_field(
        item.get("spendee_wallet_id") or item.get("spendee_id") or item.get("wallet_id"),
        "spendee_wallet_id",
        mapping_index,
    )
    internal_id = _require_mapping_field(
        item.get("internal_id") or item.get("id"),
        "internal_id",
        mapping_index,
    )
    note = str(item.get("note") or item.get("name") or internal_id).strip()
    return WalletMapping(
        spendee_wallet_id=spendee_wallet_id,
        internal_id=internal_id,
        note=note or internal_id,
    )


def parse_wallet_mappings(value: str | None) -> list[WalletMapping]:
    """Parse wallet mapping config from JSON in SPENDEE_WALLET_MAPPINGS."""
    if value is None:
        legacy_wallet_id = _get_optional_env("SPENDEE_WALLET_ID")
        if not legacy_wallet_id:
            raise ConfigError("Set SPENDEE_WALLET_MAPPINGS with at least one wallet mapping.")
        internal_id = _get_optional_env("SPENDEE_INTERNAL_ID") or legacy_wallet_id
        note = _get_optional_env("SPENDEE_WALLET_NOTE") or internal_id
        return [
            WalletMapping(
                spendee_wallet_id=legacy_wallet_id,
                internal_id=internal_id,
                note=note,
            )
        ]

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError("SPENDEE_WALLET_MAPPINGS must be valid JSON.") from exc

    mappings: list[WalletMapping] = []
    if isinstance(parsed, list):
        for index, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ConfigError(f"SPENDEE_WALLET_MAPPINGS item #{index} must be an object.")
            mappings.append(_wallet_mapping_from_dict(item, f"#{index}"))
    elif isinstance(parsed, dict):
        for spendee_wallet_id, item in parsed.items():
            if isinstance(item, str):
                internal_id = _require_mapping_field(item, "internal_id", str(spendee_wallet_id))
                mappings.append(
                    WalletMapping(
                        spendee_wallet_id=str(spendee_wallet_id).strip(),
                        internal_id=internal_id,
                        note=internal_id,
                    )
                )
            elif isinstance(item, dict):
                item_with_wallet_id = {"spendee_wallet_id": spendee_wallet_id, **item}
                mappings.append(_wallet_mapping_from_dict(item_with_wallet_id, str(spendee_wallet_id)))
            else:
                raise ConfigError(
                    "SPENDEE_WALLET_MAPPINGS object values must be strings or objects."
                )
    else:
        raise ConfigError("SPENDEE_WALLET_MAPPINGS must be a JSON array or object.")

    if not mappings:
        raise ConfigError("SPENDEE_WALLET_MAPPINGS must contain at least one mapping.")

    internal_ids: set[str] = set()
    spendee_wallet_ids: set[str] = set()
    for mapping in mappings:
        if mapping.internal_id in internal_ids:
            raise ConfigError("SPENDEE_WALLET_MAPPINGS contains duplicate internal_id values.")
        if mapping.spendee_wallet_id in spendee_wallet_ids:
            raise ConfigError("SPENDEE_WALLET_MAPPINGS contains duplicate Spendee wallet IDs.")
        internal_ids.add(mapping.internal_id)
        spendee_wallet_ids.add(mapping.spendee_wallet_id)
    return mappings


def load_config() -> Config:
    """Load runtime configuration from environment variables."""
    token_url = _get_optional_env("SPENDEE_TOKEN_URL")
    refresh_token = _get_optional_env("SPENDEE_REFRESH_TOKEN")
    fallback_token = _get_optional_env("SPENDEE_TOKEN")
    refresh_fields = [token_url, refresh_token]

    if any(refresh_fields) and not all(refresh_fields):
        raise ConfigError(
            "SPENDEE_TOKEN_URL and SPENDEE_REFRESH_TOKEN must both be set for refresh token flow."
        )
    if not all(refresh_fields) and not fallback_token:
        raise ConfigError(
            "Set refresh token flow variables (SPENDEE_TOKEN_URL and SPENDEE_REFRESH_TOKEN) "
            "or fallback SPENDEE_TOKEN."
        )

    auth_mode = (_get_optional_env("SPENDEE_TOKEN_AUTH_MODE") or "body").lower()
    if auth_mode not in {"body", "basic"}:
        raise ConfigError('SPENDEE_TOKEN_AUTH_MODE must be "body" or "basic" when set.')

    return Config(
        spendee_token=fallback_token,
        spendee_token_url=token_url,
        spendee_refresh_token=refresh_token,
        spendee_client_id=_get_optional_env("SPENDEE_CLIENT_ID"),
        spendee_client_secret=_get_optional_env("SPENDEE_CLIENT_SECRET"),
        spendee_token_auth_mode=auth_mode,
        spendee_device_uuid=_get_required_env("SPENDEE_DEVICE_UUID"),
        wallet_mappings=parse_wallet_mappings(_get_optional_env("SPENDEE_WALLET_MAPPINGS")),
        google_sheet_id=_get_required_env("GOOGLE_SHEET_ID"),
        google_service_account_json=_get_required_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        records_sheet_name=_get_optional_env("GOOGLE_SHEET_RECORDS_SHEET_NAME")
        or DEFAULT_RECORDS_SHEET_NAME,
    )


def _spendee_headers(device_uuid: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "device-uuid": device_uuid,
        "origin": "https://app.spendee.com",
        "referer": "https://app.spendee.com/",
        "spendee-platform": "web",
        "spendee-version": "master",
        "user-agent": "Mozilla/5.0",
    }


def _safe_scalar_repr(value: Any) -> str | None:
    if not (isinstance(value, str | int | float | bool) or value is None):
        return None
    rendered = repr(value)
    if len(rendered) > SAFE_SPENDEE_MAX_VALUE_LENGTH:
        rendered = rendered[: SAFE_SPENDEE_MAX_VALUE_LENGTH - 3] + "..."
    return rendered


def _safe_spendee_response_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"top-level type={type(payload).__name__}"

    summary_parts = [f"keys={','.join(sorted(str(key) for key in payload.keys())) or 'none'}"]
    for key in SAFE_SPENDEE_RESPONSE_KEYS:
        value = payload.get(key)
        scalar = _safe_scalar_repr(value)
        if scalar is not None:
            summary_parts.append(f"{key}={scalar}")
        elif isinstance(value, dict):
            nested_keys = ",".join(sorted(str(nested_key) for nested_key in value.keys())) or "none"
            summary_parts.append(f"{key}_keys={nested_keys}")
            for nested_key in SAFE_SPENDEE_NESTED_KEYS:
                nested_scalar = _safe_scalar_repr(value.get(nested_key))
                if nested_scalar is not None:
                    summary_parts.append(f"{key}.{nested_key}={nested_scalar}")

    result = payload.get("result")
    summary_parts.append(f"result_type={type(result).__name__}")
    return "; ".join(summary_parts)


def _wallet_list_from_payload(payload: Any) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        result = payload.get("result")
        candidates.append(result)
        if isinstance(result, dict):
            candidates.extend(
                result.get(key)
                for key in ("wallets", "data", "items", "records")
            )
        candidates.extend(payload.get(key) for key in ("wallets", "data", "items", "records"))

    for candidate in candidates:
        if isinstance(candidate, list):
            if not all(isinstance(item, dict) for item in candidate):
                raise SpendeeError('Spendee wallet list must contain wallet objects only.')
            return candidate

    raise SpendeeError(
        "Spendee wallet response does not contain a wallet array. "
        f"Safe response summary: {_safe_spendee_response_summary(payload)}. "
        "If local curl works with a browser Bearer token, verify that the GitHub Secrets refresh token "
        "flow returns the same kind of access token, or set SPENDEE_TOKEN as a temporary fallback."
    )


def refresh_spendee_access_token(config: Config) -> str:
    """Return an access token using refresh token flow or fallback token."""
    if not config.spendee_token_url or not config.spendee_refresh_token:
        if not config.spendee_token:
            raise ConfigError("SPENDEE_TOKEN is required when refresh token flow is not configured.")
        return config.spendee_token

    data = {
        "grant_type": "refresh_token",
        "refresh_token": config.spendee_refresh_token,
    }
    auth = None
    if config.spendee_token_auth_mode == "basic":
        if config.spendee_client_id and config.spendee_client_secret:
            auth = HTTPBasicAuth(config.spendee_client_id, config.spendee_client_secret)
    else:
        if config.spendee_client_id:
            data["client_id"] = config.spendee_client_id
        if config.spendee_client_secret:
            data["client_secret"] = config.spendee_client_secret

    try:
        response = requests.post(
            config.spendee_token_url,
            data=data,
            headers=_spendee_headers(config.spendee_device_uuid),
            auth=auth,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SpendeeError("Failed to refresh Spendee access token.") from exc

    if not response.ok:
        raise SpendeeError(f"Spendee token refresh failed with HTTP status {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SpendeeError("Spendee token refresh response is not valid JSON.") from exc

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise SpendeeError("Spendee token refresh response does not contain access_token.")
    return access_token.strip()


def fetch_spendee_wallets(access_token: str, device_uuid: str) -> list[dict[str, Any]]:
    """Fetch wallets from Spendee and validate the response shape."""
    headers = _spendee_headers(device_uuid)
    headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(SPENDEE_WALLETS_URL, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise SpendeeError("Failed to fetch Spendee wallets.") from exc

    if not response.ok:
        raise SpendeeError(f"Spendee wallet request failed with HTTP status {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SpendeeError("Spendee wallet response is not valid JSON.") from exc

    return _wallet_list_from_payload(payload)


def find_wallet(wallets: list[dict[str, Any]], wallet_id: str) -> dict[str, Any]:
    """Find a wallet by one of the known wallet identifier fields."""
    id_keys = ("id", "wallet_id", "uuid")
    for wallet in wallets:
        for key in id_keys:
            value = wallet.get(key)
            if value is not None and str(value) == wallet_id:
                return wallet
    raise SpendeeError("Configured Spendee wallet was not found in wallet response.")


def extract_wallet_summary(wallet: dict[str, Any]) -> dict[str, Any]:
    """Extract balance and currency from supported wallet payload variants."""
    balance_value = wallet.get("balance")
    currency = wallet.get("currency")

    if isinstance(balance_value, dict):
        if "amount" in balance_value:
            balance = balance_value["amount"]
        elif "value" in balance_value:
            balance = balance_value["value"]
        else:
            raise SpendeeError("Wallet balance object does not contain amount or value.")
        if currency is None:
            currency = balance_value.get("currency")
    else:
        balance = balance_value

    if balance is None:
        raise SpendeeError("Wallet balance is missing.")
    if currency is None or str(currency).strip() == "":
        raise SpendeeError("Wallet currency is missing.")

    return {"balance": balance, "currency": str(currency).strip()}


def build_sheet_records(
    wallets: list[dict[str, Any]],
    wallet_mappings: list[WalletMapping],
    updated_at: str,
) -> list[SheetRecord]:
    """Build Google Sheets records for all configured wallet mappings."""
    records: list[SheetRecord] = []
    for mapping in wallet_mappings:
        wallet = find_wallet(wallets, mapping.spendee_wallet_id)
        summary = extract_wallet_summary(wallet)
        records.append(
            SheetRecord(
                date=updated_at,
                internal_id=mapping.internal_id,
                value_czk=summary["balance"],
                source=SPENDEE_SOURCE_NAME,
                note=mapping.note,
            )
        )
    return records


def parse_service_account_json(value: str) -> dict[str, Any]:
    """Parse service account JSON from raw JSON, base64 JSON, or a local file path."""
    stripped = value.strip()
    if not stripped:
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is empty.")

    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON raw value is not valid JSON.") from exc

    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
        return json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        pass

    try:
        path = Path(stripped).expanduser()
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ConfigError("Could not read GOOGLE_SERVICE_ACCOUNT_JSON file path.") from exc
            except json.JSONDecodeError as exc:
                raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON file does not contain valid JSON.") from exc
    except OSError as exc:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid raw JSON/base64 JSON and is too long or invalid as a file path."
        ) from exc

    raise ConfigError(
        "GOOGLE_SERVICE_ACCOUNT_JSON must be raw JSON, base64 encoded JSON, or a local JSON file path."
    )


def build_sheets_service(service_account_info: dict[str, Any]):
    """Build an authenticated Google Sheets API service."""
    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=SHEETS_SCOPES,
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except (ValueError, TypeError) as exc:
        raise ConfigError("Invalid Google service account configuration.") from exc
    except Exception as exc:
        raise GoogleSheetsError("Failed to build Google Sheets service.") from exc


def _quote_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _google_http_error_summary(exc: HttpError) -> str:
    """Return a bounded, non-secret summary of a Google API HttpError."""
    summary_parts: list[str] = []
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = getattr(exc, "reason", None)
    if status is not None:
        summary_parts.append(f"HTTP status {status}")
    if reason:
        summary_parts.append(f"reason={_safe_scalar_repr(reason)}")

    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        try:
            content_text = content.decode("utf-8")
        except UnicodeDecodeError:
            content_text = ""
    else:
        content_text = str(content)

    if content_text:
        try:
            payload = json.loads(content_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                for key in SAFE_GOOGLE_ERROR_KEYS:
                    scalar = _safe_scalar_repr(error.get(key))
                    if scalar is not None:
                        summary_parts.append(f"error.{key}={scalar}")
                details = error.get("details")
                if isinstance(details, list):
                    summary_parts.append(f"error.details_count={len(details)}")

    if not summary_parts:
        return "No Google API error details were available."
    return "; ".join(summary_parts)


def ensure_sheet_exists(sheets_service: Any, spreadsheet_id: str, sheet_name: str) -> None:
    """Create a sheet tab when it is missing."""
    try:
        spreadsheet = (
            sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
    except HttpError as exc:
        raise GoogleSheetsError(
            "Failed to read Google spreadsheet metadata while ensuring the sheet tab exists. "
            f"{_google_http_error_summary(exc)}. "
            "Check GOOGLE_SHEET_ID, enable Google Sheets API, and share the target spreadsheet "
            "with the service account client_email as Editor."
        ) from exc

    existing_titles = {
        sheet.get("properties", {}).get("title")
        for sheet in spreadsheet.get("sheets", [])
    }
    if sheet_name in existing_titles:
        return

    try:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
    except HttpError as exc:
        raise GoogleSheetsError(
            f"Failed to create Google Sheets tab {sheet_name!r}. "
            f"{_google_http_error_summary(exc)}. "
            "Check that the service account has Editor access to the spreadsheet."
        ) from exc


def ensure_records_header(sheets_service: Any, spreadsheet_id: str, sheet_name: str) -> None:
    """Write the records header only when the first row is empty."""
    quoted_sheet_name = _quote_sheet_name(sheet_name)
    try:
        response = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{quoted_sheet_name}!A1:E1")
            .execute()
        )
        if response.get("values"):
            return
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quoted_sheet_name}!A1:E1",
            valueInputOption="RAW",
            body={"values": [RECORDS_HEADER]},
        ).execute()
    except HttpError as exc:
        raise GoogleSheetsError(
            "Failed to ensure Google Sheets records header exists. "
            f"{_google_http_error_summary(exc)}."
        ) from exc


def fetch_existing_record_rows(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> tuple[dict[str, int], int]:
    """Return internal ID -> row number mapping and the next append row."""
    quoted_sheet_name = _quote_sheet_name(sheet_name)
    try:
        response = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{quoted_sheet_name}!A:E")
            .execute()
        )
    except HttpError as exc:
        raise GoogleSheetsError(
            "Failed to read existing Google Sheet records. "
            f"{_google_http_error_summary(exc)}."
        ) from exc

    rows = response.get("values", [])
    if not isinstance(rows, list):
        raise GoogleSheetsError("Google Sheets records response has an invalid shape.")

    record_rows: dict[str, int] = {}
    for row_index, row in enumerate(rows[1:], start=2):
        if not isinstance(row, list) or len(row) < 2:
            continue
        internal_id = str(row[1]).strip()
        if internal_id and internal_id not in record_rows:
            record_rows[internal_id] = row_index
    return record_rows, len(rows) + 1


def update_google_sheet(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    records: list[SheetRecord],
) -> None:
    """Upsert wallet records into the records sheet by internal ID in column B."""
    if not records:
        raise GoogleSheetsError("No records to update in Google Sheets.")

    quoted_sheet_name = _quote_sheet_name(sheet_name)
    try:
        ensure_sheet_exists(sheets_service, spreadsheet_id, sheet_name)
        ensure_records_header(sheets_service, spreadsheet_id, sheet_name)
        existing_rows, _next_row = fetch_existing_record_rows(
            sheets_service,
            spreadsheet_id,
            sheet_name,
        )

        append_values: list[list[Any]] = []
        for record in records:
            values = [
                record.date,
                record.internal_id,
                record.value_czk,
                record.source,
                record.note,
            ]
            existing_row = existing_rows.get(record.internal_id)
            if existing_row:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{quoted_sheet_name}!A{existing_row}:E{existing_row}",
                    valueInputOption="RAW",
                    body={"values": [values]},
                ).execute()
            else:
                append_values.append(values)

        if append_values:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{quoted_sheet_name}!A:E",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": append_values},
            ).execute()
    except HttpError as exc:
        raise GoogleSheetsError(
            "Failed to update Google Sheet records. "
            f"{_google_http_error_summary(exc)}."
        ) from exc


def run() -> None:
    log_progress("1/7 loading configuration")
    config = load_config()
    log_progress(f"configuration loaded; wallet mappings configured: {len(config.wallet_mappings)}")

    if config.spendee_token_url and config.spendee_refresh_token:
        log_progress("2/7 refreshing Spendee access token")
    else:
        log_progress("2/7 using SPENDEE_TOKEN fallback as primary token")
    access_token = refresh_spendee_access_token(config)

    try:
        log_progress("3/7 fetching Spendee wallets with refreshed/primary token")
        wallets = fetch_spendee_wallets(access_token, config.spendee_device_uuid)
    except SpendeeError as exc:
        if config.spendee_token and access_token != config.spendee_token:
            log_progress("primary Spendee wallet fetch failed; trying SPENDEE_TOKEN fallback")
            try:
                wallets = fetch_spendee_wallets(config.spendee_token, config.spendee_device_uuid)
            except SpendeeError as fallback_exc:
                raise SpendeeError(
                    "Spendee wallet fetch failed with both refreshed token and SPENDEE_TOKEN fallback. "
                    f"Primary error: {exc}. Fallback error: {fallback_exc}"
                ) from fallback_exc
        else:
            if config.spendee_token:
                log_progress("SPENDEE_TOKEN fallback was configured but is the same token already tried")
            else:
                log_progress("SPENDEE_TOKEN fallback is not configured")
            raise

    log_progress(f"Spendee wallets fetched; wallet objects received: {len(wallets)}")
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    log_progress("4/7 building Google Sheets records from wallet mappings")
    records = build_sheet_records(wallets, config.wallet_mappings, updated_at)
    log_progress(f"records prepared for Google Sheets: {len(records)}")

    log_progress("5/7 parsing Google service account credentials")
    service_account_info = parse_service_account_json(config.google_service_account_json)

    log_progress("6/7 building Google Sheets service")
    sheets_service = build_sheets_service(service_account_info)

    log_progress("7/7 upserting records into Google Sheet")
    update_google_sheet(
        sheets_service,
        config.google_sheet_id,
        config.records_sheet_name,
        records,
    )


def main() -> int:
    try:
        run()
    except (ConfigError, SpendeeError, GoogleSheetsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Updated Google Sheet successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
