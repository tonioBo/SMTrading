#!/usr/bin/env bash
# «SMTrading TV»: build, sign and install the Tizen app on the Samsung TV.
#
#   scripts/tv-app.sh check               what is there and what is missing (touches nothing)
#   scripts/tv-app.sh build               the web bundle + config.xml + icon in tv-app/build/pkg
#   scripts/tv-app.sh package             build, then the signed .wgt (tv-app/build/SMTradingTV.wgt)
#   scripts/tv-app.sh install <tv-ip>     package, then sdb connect + tizen install on that TV
#   scripts/tv-app.sh run <tv-ip>         open the installed app on that TV
#
# Environment:
#   TIZEN_STUDIO   Tizen Studio folder (default ~/tizen-studio): tools/ide/bin/tizen, tools/sdb
#   TIZEN_PROFILE  security profile of Tizen Studio's Certificate Manager (default smtrading-tv)
#   SMT_TV_HOST    the TV address when not given as argument
#
# Installation guide (developer mode, Samsung certificate, renewal):
# docs/readiness/tv-app-installazione.md. Never run against the TV without the owner's ok.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/tv-app"
BUILD="$APP_DIR/build"
PKG="$BUILD/pkg"
WGT="$BUILD/SMTradingTV.wgt"
APP_ID="$(sed -n 's/.*<tizen:application id="\([^"]*\)".*/\1/p' "$APP_DIR/config.xml")"
TIZEN_STUDIO="${TIZEN_STUDIO:-$HOME/tizen-studio}"
TIZEN_PROFILE="${TIZEN_PROFILE:-smtrading-tv}"
SDB_PORT=26101

say() { printf '%s\n' "$*"; }
die() { printf 'ERRORE: %s\n' "$*" >&2; exit 1; }

tizen_cli() {
  if command -v tizen >/dev/null 2>&1; then command -v tizen
  elif [ -x "$TIZEN_STUDIO/tools/ide/bin/tizen" ]; then echo "$TIZEN_STUDIO/tools/ide/bin/tizen"
  elif [ -x "$TIZEN_STUDIO/tools/ide/bin/tizen.sh" ]; then echo "$TIZEN_STUDIO/tools/ide/bin/tizen.sh"
  fi
}

sdb_cli() {
  if command -v sdb >/dev/null 2>&1; then command -v sdb
  elif [ -x "$TIZEN_STUDIO/tools/sdb" ]; then echo "$TIZEN_STUDIO/tools/sdb"
  fi
}

need_tizen() {
  TIZEN="$(tizen_cli)"
  [ -n "$TIZEN" ] || die "manca la riga di comando di Tizen (tizen). Installa Tizen Studio (anche solo «CLI») da https://developer.tizen.org/development/tizen-studio/download e, nel Package Manager, «Samsung TV Extensions» e «Samsung Certificate Extension»; poi riprova (o indica la cartella con TIZEN_STUDIO=...)."
}

need_sdb() {
  SDB="$(sdb_cli)"
  [ -n "$SDB" ] || die "manca sdb (Smart Development Bridge): si installa con Tizen Studio, in $TIZEN_STUDIO/tools/sdb (o indica la cartella con TIZEN_STUDIO=...)."
}

need_profile() {
  need_tizen
  local list
  list="$("$TIZEN" security-profiles list 2>/dev/null || true)"
  if ! printf '%s\n' "$list" | grep -Eq "(^|[[:space:]])$TIZEN_PROFILE([[:space:]]|$)"; then
    die "manca il profilo di certificato «$TIZEN_PROFILE». Crealo in Tizen Studio → Tools → Certificate Manager → «+» → Samsung → TV, con l'account Samsung (autore + distributore, aggiungendo il DUID della TV); chiamalo $TIZEN_PROFILE o indica il nome con TIZEN_PROFILE=... (guida: docs/readiness/tv-app-installazione.md)."
  fi
}

tv_host() {
  local host="${1:-${SMT_TV_HOST:-}}"
  [ -n "$host" ] || die "manca l'indirizzo della TV: scripts/tv-app.sh install <ip-della-tv> (o SMT_TV_HOST=...)."
  case "$host" in
    *[!0-9A-Za-z.:-]*) die "indirizzo della TV non valido: $host" ;;
  esac
  echo "$host"
}

cmd_check() {
  local ok=1
  if [ -d "$ROOT/web/node_modules" ]; then say "OK   dipendenze web presenti (web/node_modules)"
  else say "MANCA web/node_modules: esegui «npm ci» in web/"; ok=0; fi
  if [ -n "$(tizen_cli)" ]; then say "OK   riga di comando Tizen: $(tizen_cli)"
  else say "MANCA la riga di comando Tizen (Tizen Studio, cartella attesa: $TIZEN_STUDIO)"; ok=0; fi
  if [ -n "$(sdb_cli)" ]; then say "OK   sdb: $(sdb_cli)"
  else say "MANCA sdb (Tizen Studio, $TIZEN_STUDIO/tools/sdb)"; ok=0; fi
  if [ -n "$(tizen_cli)" ]; then
    if "$(tizen_cli)" security-profiles list 2>/dev/null | grep -Eq "(^|[[:space:]])$TIZEN_PROFILE([[:space:]]|$)"; then
      say "OK   profilo di certificato «$TIZEN_PROFILE»"
    else
      say "MANCA il profilo di certificato «$TIZEN_PROFILE» (Certificate Manager, account Samsung)"; ok=0
    fi
  fi
  say "     app: $APP_ID"
  [ "$ok" = 1 ] && say "Tutto pronto." || say "Prima dell'installazione servono i pezzi indicati sopra (guida: docs/readiness/tv-app-installazione.md)."
  [ "$ok" = 1 ]
}

cmd_build() {
  [ -d "$ROOT/web/node_modules" ] || die "mancano le dipendenze web: esegui «npm ci» in web/."
  say "Costruisco l'app…"
  (cd "$ROOT/web" && npm run --silent build:tv-app >/dev/null) || die "la costruzione dell'app non è riuscita (npm run build:tv-app in web/)."
  rm -rf "$PKG"
  mkdir -p "$PKG"
  cp -R "$BUILD/www/." "$PKG/"
  cp "$APP_DIR/config.xml" "$APP_DIR/icon.png" "$PKG/"
  [ -f "$PKG/index.html" ] || die "index.html mancante nel pacchetto."
  say "Pronta: $PKG"
}

cmd_package() {
  need_profile
  cmd_build
  say "Firmo il pacchetto con il profilo «$TIZEN_PROFILE»…"
  find "$PKG" -maxdepth 1 -name '*.wgt' -delete
  "$TIZEN" package -t wgt -s "$TIZEN_PROFILE" -- "$PKG" \
    || die "firma non riuscita: controlla il profilo «$TIZEN_PROFILE» nel Certificate Manager (password del certificato, scadenza)."
  local out
  out="$(find "$PKG" -maxdepth 1 -name '*.wgt' | head -n 1)"
  [ -n "$out" ] || die "il pacchetto .wgt non è stato creato."
  mv "$out" "$WGT"
  say "Pacchetto: $WGT"
}

cmd_install() {
  local host serial
  host="$(tv_host "${1:-}")"
  need_sdb
  cmd_package
  serial="$host:$SDB_PORT"
  say "Mi collego alla TV $host…"
  "$SDB" connect "$host" >/dev/null 2>&1 || true
  "$SDB" devices | grep -q "^$serial[[:space:]]" \
    || die "la TV $host non risponde a sdb. Sulla TV: App → digita 12345 → Modalità sviluppatore ON con l'indirizzo IP di questo PC, poi spegni e riaccendi la TV (guida: docs/readiness/tv-app-installazione.md)."
  say "Installo $APP_ID…"
  "$TIZEN" install -s "$serial" -n "$(basename "$WGT")" -- "$BUILD" \
    || die "installazione non riuscita: se il messaggio parla di certificato, il DUID della TV deve essere nel certificato distributore (Certificate Manager)."
  say "Installata. Sulla TV: App → SMTrading TV (al primo avvio: indirizzo del server e codice di abbinamento)."
}

cmd_run() {
  local host serial
  host="$(tv_host "${1:-}")"
  need_tizen
  need_sdb
  serial="$host:$SDB_PORT"
  "$SDB" connect "$host" >/dev/null 2>&1 || true
  "$TIZEN" run -s "$serial" -p "$APP_ID" || die "non riesco ad aprire l'app sulla TV $host."
}

case "${1:-}" in
  check) cmd_check ;;
  build) cmd_build ;;
  package) cmd_package ;;
  install) shift; cmd_install "${1:-}" ;;
  run) shift; cmd_run "${1:-}" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
