#!/usr/bin/env python3
"""Test połączenia z GA4 Data API: JEDNO zapytanie runReport, tylko odczyt.

Pobiera sumy sessions, activeUsers i screenPageViews za ostatnie 7 pełnych dni (7daysAgo–yesterday,
w strefie czasowej usługi GA4). Bez wymiarów, więc odpowiedź nie zawiera adresów, źródeł ani danych osobowych.

REPOZYTORIUM PUBLICZNE: logi Actions są jawne, dlatego skrypt NIE wypisuje wartości metryk ani zużycia
limitu — tylko to, czy połączenie działa i czy usługa zwróciła dane. Tokeny i adresy e-mail są ukrywane.
Skrypt jest samodzielny: tylko biblioteka standardowa Pythona, bez innych plików.

Wejście (zmienne środowiska):
  GOOGLE_OAUTH_ACCESS_TOKEN   token z google-github-actions/auth (zakres analytics.readonly); nigdy nie jest wypisywany
  GA4_PROPERTY_ID             numeryczny identyfikator usługi (zmienna środowiska GitHub production-readonly)
  GA4_EXPECTED_PROPERTY_ID    opcjonalnie: oczekiwana wartość — niezgodność przerywa test przed zapytaniem

Kody wyjścia: 0 połączenie działa (także przy braku ruchu), 2 konfiguracja, 3 = 401, 4 = 403, 5 = 404,
6 inny błąd HTTP, 7 błąd sieci, 8 STOP aktywny.
Celowo: brak ponowień (dokładnie jedno zapytanie), brak zapytań do Search Console, tylko biblioteka standardowa.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

API_HOST = "analyticsdata.googleapis.com"
METRICS = ["sessions", "activeUsers", "screenPageViews"]
REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_RE = re.compile(r"(ya29\.[A-Za-z0-9._-]+|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# transport(url, body_bytes, headers) -> (status, body_text)
Transport = Callable[[str, bytes, dict], tuple]


def redact(s: str) -> str:
    return EMAIL_RE.sub("[konto]", TOKEN_RE.sub("[UKRYTO]", s or ""))


def endpoint(property_id: str) -> str:
    return f"https://{API_HOST}/v1beta/properties/{property_id}:runReport"


def request_body() -> dict:
    return {"dateRanges": [{"startDate": "7daysAgo", "endDate": "yesterday"}],
            "metrics": [{"name": m} for m in METRICS]}


def urllib_transport(url: str, data: bytes, headers: dict) -> tuple:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _explicit_dates(tz: str | None, today: date | None = None) -> str:
    d = None
    if tz:
        try:
            from zoneinfo import ZoneInfo
            d = today or datetime.now(ZoneInfo(tz)).date()
        except Exception:  # noqa: BLE001 — nieznana strefa: zostaje opis względny
            d = None
    if not d:
        return "7daysAgo – yesterday (strefa usługi)"
    return f"{(d - timedelta(days=7)).isoformat()} – {(d - timedelta(days=1)).isoformat()} ({tz})"


def _error_info(text: str) -> tuple[str, str, str]:
    try:
        err = json.loads(text).get("error", {})
    except ValueError:
        return "", redact(text[:200]), ""
    reasons = " ".join(d.get("reason", "") for d in err.get("details", []) if isinstance(d, dict))
    return err.get("status", ""), redact(str(err.get("message", ""))[:300]), reasons


HINTS = {
    401: ("Token odrzucony (nieważny, wygasły lub dla innej usługi).",
          "Sprawdź krok uwierzytelnienia: workload_identity_provider i service_account w zmiennych środowiska, "
          "powiązanie roles/iam.workloadIdentityUser na koncie serwisowym, warunek OIDC providera."),
    403: ("Brak uprawnień do usługi GA4 albo wyłączone API.",
          "GA4 → Administracja → Zarządzanie dostępem do usługi: konto serwisowe z rolą „Wyświetlający”. "
          "Jeśli komunikat mówi o wyłączonym API: włącz „Google Analytics Data API” w projekcie Google Cloud."),
    404: ("Usługa GA4 o tym identyfikatorze nie istnieje albo nie jest widoczna.",
          "Sprawdź GA4_PROPERTY_ID: numer z Administracja → Szczegóły usługi (nie identyfikator pomiaru G-…)."),
}


def run(env: dict, transport: Transport = urllib_transport, out=print, today: date | None = None,
        repo_root: Path = REPO_ROOT) -> int:
    if (repo_root / "autopilot" / "STOP").exists():
        out("STOP: plik autopilot/STOP jest aktywny — test nie został wykonany.")
        return 8
    pid = (env.get("GA4_PROPERTY_ID") or "").strip()
    expected = (env.get("GA4_EXPECTED_PROPERTY_ID") or "").strip()
    token = env.get("GOOGLE_OAUTH_ACCESS_TOKEN") or ""
    if not re.fullmatch(r"\d{6,12}", pid):
        out("KONFIGURACJA: GA4_PROPERTY_ID musi być numerem usługi (np. 123456789), nie identyfikatorem pomiaru G-…. "
            "Zapytanie nie zostało wysłane.")
        return 2
    if expected and pid != expected:
        out(f"KONFIGURACJA: GA4_PROPERTY_ID ({pid}) różni się od oczekiwanego ({expected}). Zapytanie nie zostało wysłane.")
        return 2
    if not token:
        out("KONFIGURACJA: brak tokenu z kroku uwierzytelnienia. Zapytanie nie zostało wysłane.")
        return 2
    out(f"GA4 Data API runReport · usługa {pid} · metryki: {', '.join(METRICS)} · jedno zapytanie, tylko odczyt")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "seo-autopilot-ga4-test"}
    try:
        status, text = transport(endpoint(pid), json.dumps(request_body()).encode(), headers)
    except Exception as exc:  # noqa: BLE001
        out(f"BŁĄD SIECI: {type(exc).__name__}: {redact(str(exc))[:200]}")
        return 7
    if status != 200:
        st, msg, reasons = _error_info(text)
        out(f"BŁĄD HTTP {status} {st}: {msg}")
        if status in HINTS:
            what, fix = HINTS[status]
            if status == 403 and ("SERVICE_DISABLED" in reasons or "has not been used" in msg or "disabled" in msg.lower()):
                what, fix = ("Google Analytics Data API jest wyłączone w projekcie.",
                             "Google Cloud → APIs & Services → włącz „Google Analytics Data API” (analyticsdata.googleapis.com).")
            out(f"Co to znaczy: {what}")
            out(f"Co zrobić: {fix}")
            return {401: 3, 403: 4, 404: 5}[status]
        if status == 429:
            out("Co to znaczy: przekroczony limit zapytań GA4. Test nie ponawia zapytania — uruchom go ponownie później.")
        return 6
    try:
        data = json.loads(text)
    except ValueError:
        out("BŁĄD: odpowiedź nie jest poprawnym JSON.")
        return 6
    tz = (data.get("metadata") or {}).get("timeZone")
    out(f"Okres: ostatnie 7 pełnych dni: {_explicit_dates(tz, today)}")
    rows = data.get("rows") or []
    has_data = bool(rows) and any(v.get("value") not in (None, "", "0") for v in rows[0].get("metricValues", []))
    out("POŁĄCZENIE DZIAŁA: GA4 Data API przyjęło zapytanie i zwróciło poprawną odpowiedź.")
    if has_data:
        out(f"Dane w okresie: TAK (usługa zwróciła wartości dla: {', '.join(METRICS)}). "
            "Wartości nie są wypisywane, bo logi tego repozytorium są publiczne.")
    else:
        out("Dane w okresie: BRAK. To nie jest błąd połączenia. Możliwa przyczyna: pusty identyfikator pomiaru "
            "na stronie (SNX_GA4_ID), więc GA4 nie zbiera danych.")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Test połączenia GA4\n\nPołączenie działa. Okres: {_explicit_dates(tz, today)}. "
                    f"Dane w okresie: {'tak' if has_data else 'brak'}. Wartości metryk celowo nie są publikowane.\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
