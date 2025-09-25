import os
import re
import asyncio
import random
from typing import Dict, List, Optional, Tuple
import httpx
from telegram import Bot
from dotenv import load_dotenv

# ================== CONFIG ==================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SUPERODDS_URL = "https://betesporte.bet.br/api/PreMatch/GetEvents?sportId=999&tournamentId=4200000001"
INTERVAL_SECONDS = 30
SEND_EACH_EVENT_SEPARATELY = False  # True = 1 msg por pick; False = agrupa

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://betesporte.bet.br/",
    "Origin": "https://betesporte.bet.br",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

# ================== REGEX / HELPERS ==================
# Ex.: "Palmeiras (x) River Plate para ter menos de 1.5 gols na partida"
RE_HOME_FMT = re.compile(
    r"^\s*(?P<home>.+?)\s*\(x\)\s*(?P<away>.+?)\s*para\s*ter\s*menos\s*de\s*(?P<line>\d+(?:[.,]\d+)?)\s*gols?\s*na\s*partida",
    re.IGNORECASE,
)

# Opção: "Menos de 1.5"
RE_OPT_UNDER = re.compile(r"^\s*menos\s*de\s*(\d+(?:[.,]\d+)?)\s*$", re.IGNORECASE)

def to_float(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None

# ================== MODELO ==================
class UnderPick:
    def __init__(self, event_id: str, home: str, away: str, line_raw: str, odd: float):
        self.event_id = str(event_id)
        self.home = home.strip() or "Time da Casa"
        self.away = away.strip() or "Time Visitante"
        self.line_raw = line_raw  # manter formato original para exibição (1.5 / 1,5)
        self.line = to_float(line_raw) or 0.0
        self.odd = float(odd)

    @property
    def key(self) -> str:
        # chave de estado para detectar mudança de odd
        return f"{self.event_id}|under|{self.line}"

    def title(self) -> str:
        return f"{self.home} (x) {self.away} para ter menos de {self.line_raw} gols na partida"

    def to_message_block(self) -> str:
        return (
            "🏠 Casa: Betesporte\n"
            f"🎯 Mercado: {self.title()}\n\n"
            f"📌 Cotação: \"{self.odd}\"\n"
        )

# ================== CORE ==================
async def fetch_json(max_retries: int = 3, backoff: float = 1.5) -> dict:
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        http2=False,
        verify=True,
    ) as client:
        last_exc = None
        for i in range(1, max_retries + 1):
            try:
                r = await client.get(SUPERODDS_URL)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_exc = e
                await asyncio.sleep(backoff * i)
        raise last_exc if last_exc else RuntimeError("Falha ao buscar dados")

def parse_under_from_home_text(home_text: str) -> Optional[Tuple[str, str, str]]:
    """
    Se o homeTeamName vier exatamente no formato:
    'A (x) B para ter menos de X gols na partida'
    retorna (home, away, line_raw).
    """
    if not home_text:
        return None
    m = RE_HOME_FMT.search(home_text)
    if not m:
        return None
    return m.group("home").strip(), m.group("away").strip(), m.group("line").strip()

def parse_line_from_option_name(opt_name: str) -> Optional[str]:
    """
    Se a opção for 'Menos de X', retorna X como string crua.
    """
    if not opt_name:
        return None
    m = RE_OPT_UNDER.match(opt_name)
    if not m:
        return None
    return m.group(1).strip()

def extract_picks(payload: dict) -> List[UnderPick]:
    """
    Extrai apenas picks do tipo:
    'X (x) Y para ter menos de N gols na partida'
    com odd a partir do mercado 'Total de Gols' + opção 'Menos de N'.
    """
    out: List[UnderPick] = []
    countries = ((payload or {}).get("data") or {}).get("countries") or []
    for country in countries:
        for tourn in country.get("tournaments", []) or []:
            for event in tourn.get("events", []) or []:
                event_id = event.get("id") or event.get("eventId") or ""
                home_text = (event.get("homeTeamName") or "").strip()

                # 1) Primeiro, precisa bater o formato no homeTeamName
                parsed = parse_under_from_home_text(home_text)
                if not parsed:
                    continue
                home, away, line_raw = parsed
                line_f = to_float(line_raw)
                if line_f is None:
                    continue

                # 2) Agora confirmar que existe o mercado "Total de Gols" com opção "Menos de X"
                markets = event.get("markets", []) or []
                found_odd: Optional[float] = None
                for market in markets:
                    mname = (market.get("name") or "").strip().lower()
                    if mname != "total de gols":
                        continue
                    for opt in market.get("options", []) or []:
                        opt_name = (opt.get("name") or "").strip()
                        opt_line_raw = parse_line_from_option_name(opt_name)
                        if opt_line_raw is None:
                            continue
                        # comparar a linha da opção com a do título do evento
                        if to_float(opt_line_raw) == line_f:
                            odd_val = opt.get("odd")
                            try:
                                found_odd = float(odd_val)
                            except (TypeError, ValueError):
                                pass
                            if found_odd is not None:
                                break
                    if found_odd is not None:
                        break

                if found_odd is None:
                    # Se por algum motivo o mercado não estiver presente,
                    # pula (não é o que queremos enviar)
                    continue

                out.append(UnderPick(str(event_id), home, away, line_raw, found_odd))
    return out

def build_message(picks: List[UnderPick]) -> str:
    if not picks:
        return ""
    blocks = [p.to_message_block() for p in picks]
    msg = "\n".join(blocks)
    return (msg[:4090] + "…") if len(msg) > 4096 else msg

# ================== LOOP ==================
async def run_bot():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Configure TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no .env")

    bot = Bot(token=TOKEN)
    last_sent: Dict[str, float] = {}  # key -> last odd

    while True:
        try:
            data = await fetch_json()
            picks = extract_picks(data)

            changed: List[UnderPick] = []
            for p in picks:
                prev = last_sent.get(p.key)
                if prev is None or p.odd != prev:
                    changed.append(p)

            if changed:
                # Atualiza estado
                for p in changed:
                    last_sent[p.key] = p.odd

                if SEND_EACH_EVENT_SEPARATELY:
                    for p in changed:
                        await bot.send_message(chat_id=CHAT_ID, text=p.to_message_block())
                        await asyncio.sleep(0.5)
                else:
                    text = build_message(changed)
                    if text:
                        await bot.send_message(chat_id=CHAT_ID, text=text)

        except Exception as e:
            # Notifica falha desta rodada (opcionalmente com throttling)
            try:
                await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Erro na varredura: {type(e).__name__}: {e}")
            except Exception:
                pass

        await asyncio.sleep(random.uniform(28, 32))

if __name__ == "__main__":
    asyncio.run(run_bot())
