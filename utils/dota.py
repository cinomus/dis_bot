"""Разбор катки по данным OpenDota.

Метод тот же, что у mcp-replay-dota2: роль, линия, тайминги предметов,
перевес золота и момент, где игра сломалась. Сырой .dem сюда не качаем —
OpenDota уже парсит реплей, и именно его match API использует тот проект.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("utils.dota")

OPENDOTA_API = "https://api.opendota.com/api"
STEAM64_BASE = 76561197960265728
OFFER_WINDOW_SECONDS = 18 * 3600

PLAYER_URL = re.compile(
    r"(?:opendota\.com|dotabuff\.com|stratz\.com)/players/(\d+)",
    re.IGNORECASE,
)
STEAM_PROFILE_URL = re.compile(r"steamcommunity\.com/profiles/(\d+)", re.IGNORECASE)
STEAM_VANITY_URL = re.compile(r"steamcommunity\.com/id/([A-Za-z0-9_-]+)", re.IGNORECASE)
MATCH_URL = re.compile(
    r"(?:opendota\.com|dotabuff\.com|stratz\.com)/matches/(\d+)",
    re.IGNORECASE,
)
OFFER_FOOTER = re.compile(r"match:(\d+)\|account:(\d+)\|user:(\d+)")

LOBBY_NAMES = {
    0: "обычная",
    1: "практика",
    7: "рейтинг",
    8: "1 на 1",
    9: "Battle Cup",
}
GAME_MODES = {
    1: "All Pick",
    2: "Captains Mode",
    3: "Random Draft",
    4: "Single Draft",
    5: "All Random",
    16: "Captains Mode",
    22: "All Pick",
    23: "Turbo",
}
POSITIONS = {
    1: "керри",
    2: "мид",
    3: "оффлейн",
    4: "мягкая поддержка",
    5: "хард саппорт",
}
ROLE_HINTS = {
    1: "До ключевого предмета низкое участие в драках — норма. Спрашивай фарм, смерти и тайминг предметов.",
    2: "Спрашивай CS на миду и руны. Ротация без хорошей руны, потеряв волны, — ошибка.",
    3: "Его собственный CS вторичен. Смотри, сколько фарма осталось вражескому керри и было ли давление.",
    4: "Спрашивай стаки, смоки, вижен и сейтапы, а не фарм.",
    5: "Низкий нетворс — норма. Спрашивай варды и не умер ли он зря в одиночку.",
}
MEDALS = ["", "Рекрут", "Страж", "Рыцарь", "Герой", "Легенда", "Властелин", "Божество", "Титан"]
JUNK_ITEMS = {
    "tango", "flask", "ward_observer", "ward_sentry", "ward_dispenser", "smoke_of_deceit",
    "dust", "clarity", "faerie_fire", "enchanted_mango", "famango", "great_famango",
    "greater_famango", "blood_grenade", "branches", "circlet", "slippers", "gauntlets",
    "mantle", "magic_stick", "magic_wand", "quelling_blade", "orb_of_venom", "blight_stone",
    "wind_lace", "ring_of_protection", "sobi_mask", "ring_of_regen", "sage_mask", "boots",
    "gloves", "boots_of_elves", "belt_of_strength", "robe", "ogre_axe", "blade_of_alacrity",
    "staff_of_wizardry", "crown", "tpscroll", "orb_of_corrosion", "bracer", "wraith_band",
    "null_talisman", "infused_raindrop", "fluffy_hat", "blades_of_attack", "chainmail",
    "helm_of_iron_will", "broadsword", "claymore", "javelin", "mithril_hammer", "ring_of_health",
    "void_stone", "energy_booster", "vitality_booster", "point_booster", "platemail",
    "hyperstone", "ultimate_orb", "demon_edge", "mystic_staff", "reaver", "eaglesong",
    "talisman_of_evasion", "quarterstaff", "sobi_mask", "robe_of_the_magi",
}
BENCH_LABELS = {
    "gold_per_min": "GPM",
    "xp_per_min": "XPM",
    "kills_per_min": "убийства/мин",
    "last_hits_per_min": "LH/мин",
    "hero_damage_per_min": "урон/мин",
    "hero_healing_per_min": "лечение/мин",
    "tower_damage": "урон по башням",
}


class DotaUserError(Exception):
    """Текст, который можно показать человеку в Дискорде."""


class MatchNotReady(DotaUserError):
    """OpenDota ещё не распарсил реплей."""


class PlayerNotInMatch(DotaUserError):
    """Аккаунта нет среди игроков матча."""


@dataclass(frozen=True)
class PlayerRef:
    account_id: int | None = None
    vanity: str | None = None


@dataclass
class Names:
    heroes_by_id: dict[int, str] = field(default_factory=dict)
    heroes_by_npc: dict[str, str] = field(default_factory=dict)
    items_by_id: dict[int, str] = field(default_factory=dict)
    items_by_key: dict[str, str] = field(default_factory=dict)

    def hero(self, hero_id) -> str:
        try:
            return self.heroes_by_id.get(int(hero_id), f"герой {hero_id}")
        except (TypeError, ValueError):
            return "?"

    def hero_key(self, key: str) -> str:
        if key in self.heroes_by_npc:
            return self.heroes_by_npc[key]
        npc = key if str(key).startswith("npc_") else f"npc_dota_hero_{key}"
        return self.heroes_by_npc.get(npc) or str(key).replace("npc_dota_hero_", "").replace("_", " ")

    def item_id(self, item_id) -> str | None:
        try:
            number = int(item_id)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return self.items_by_id.get(number) or f"предмет {number}"

    def item_key(self, key: str) -> str:
        return self.items_by_key.get(key) or str(key).replace("_", " ")


def steam64_to_account(steam64: int) -> int:
    account = int(steam64) - STEAM64_BASE
    if not 1 <= account < 2**32:
        raise DotaUserError("Не похоже на SteamID64.")
    return account


def parse_player_ref(text: str) -> PlayerRef | None:
    """Ссылка на игрока. Голое число не трогаем: его легко спутать с ID матча."""
    if not text:
        return None
    match = PLAYER_URL.search(text)
    if match:
        return PlayerRef(account_id=int(match.group(1)))
    match = STEAM_PROFILE_URL.search(text)
    if match:
        return PlayerRef(account_id=steam64_to_account(int(match.group(1))))
    match = STEAM_VANITY_URL.search(text)
    if match:
        return PlayerRef(vanity=match.group(1))
    return None


def parse_account_argument(text: str) -> PlayerRef:
    ref = parse_player_ref(text)
    if ref:
        return ref
    if MATCH_URL.search(text or ""):
        raise DotaUserError("Это ссылка на матч. Для разбора есть /dota match.")
    stripped = (text or "").strip()
    if stripped.isdigit():
        number = int(stripped)
        if number >= STEAM64_BASE:
            return PlayerRef(account_id=steam64_to_account(number))
        if number <= 0:
            raise DotaUserError("Не похоже на Steam.")
        return PlayerRef(account_id=number)
    raise DotaUserError("Нужна ссылка на Steam, Dotabuff, OpenDota, Stratz или числовой SteamID.")


def find_match_id(text: str) -> int | None:
    match = MATCH_URL.search(text or "")
    if not match:
        return None
    return int(match.group(1))


def parse_match_argument(text: str) -> int:
    found = find_match_id(text)
    if found:
        return found
    stripped = re.sub(r"\s+", "", text or "")
    if stripped.isdigit() and len(stripped) >= 5:
        return int(stripped)
    raise DotaUserError("Нужен ID матча или ссылка с OpenDota, Dotabuff или Stratz.")


def parse_offer_footer(text: str | None) -> tuple[int, int, int] | None:
    match = OFFER_FOOTER.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def offer_footer(match_id: int, account_id: int, user_id: int) -> str:
    return f"match:{match_id}|account:{account_id}|user:{user_id}"


def fmt_duration(seconds) -> str:
    try:
        total = max(int(seconds), 0)
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}:{total % 60:02d}"


def fmt_game_time(seconds) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "?"
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 60}:{value % 60:02d}"


def rank_name(tier) -> str:
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        return "скрыт"
    if tier <= 0:
        return "скрыт"
    if tier >= 80:
        return "Титан"
    medal = tier // 10
    star = tier % 10
    name = MEDALS[medal] if 1 <= medal < len(MEDALS) else str(tier)
    if star:
        return f"{name} {star}"
    return name


def lobby_name(lobby_type) -> str:
    try:
        return LOBBY_NAMES.get(int(lobby_type), "лобби")
    except (TypeError, ValueError):
        return "лобби"


def mode_name(game_mode) -> str:
    try:
        return GAME_MODES.get(int(game_mode), "режим")
    except (TypeError, ValueError):
        return "режим"


def player_won(player: dict, match: dict) -> bool | None:
    radiant_win = match.get("radiant_win")
    if radiant_win is None:
        return None
    return (player.get("player_slot", 0) < 128) == bool(radiant_win)


def _num(value, default=0):
    try:
        if value is None:
            return default
        return value
    except TypeError:
        return default


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _series(values) -> list[int]:
    if not isinstance(values, list):
        return []
    series = []
    for value in values:
        try:
            series.append(int(value))
        except (TypeError, ValueError):
            break
    return series


def _at(series: list[int], minute: int):
    if minute < len(series):
        return series[minute]
    return None


def _pct(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if 0 <= number <= 1.5:
        number *= 100
    return f"{number:.0f}%"


def assign_positions(players: list[dict]) -> list[dict]:
    """Позиции 1–5 по линии и GPM, как в mcp-replay-dota2."""
    radiant = [player for player in players if player.get("player_slot", 0) < 128]
    dire = [player for player in players if player.get("player_slot", 0) >= 128]

    def process(team: list[dict]):
        lanes: dict[int, list[dict]] = {}
        unassigned: list[dict] = []
        for player in team:
            lane_role = player.get("lane_role")
            lanes.setdefault(lane_role, []).append(player)
        for lane_role, lane_players in lanes.items():
            ordered = sorted(lane_players, key=lambda item: _int(item.get("gold_per_min")), reverse=True)
            if lane_role == 2:
                for player in ordered:
                    player["position"] = 2
                    player["role"] = "core"
            elif lane_role == 1 and ordered:
                ordered[0]["position"] = 1
                ordered[0]["role"] = "core"
                for player in ordered[1:]:
                    player["position"] = 5
                    player["role"] = "support"
            elif lane_role == 3 and ordered:
                ordered[0]["position"] = 3
                ordered[0]["role"] = "core"
                for player in ordered[1:]:
                    player["position"] = 4
                    player["role"] = "support"
            else:
                for player in ordered:
                    player["role"] = "support"
                    unassigned.append(player)
        unassigned.sort(key=lambda item: _int(item.get("gold_per_min")), reverse=True)
        for index, player in enumerate(unassigned):
            if player.get("position") is None:
                player["position"] = 4 if index == 0 else 5

    process(radiant)
    process(dire)
    return players


def _notable_purchases(player: dict, names: Names) -> str:
    log_entries = player.get("purchase_log") or []
    if not isinstance(log_entries, list):
        return "нет"
    parts = []
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        if not key or key.startswith("recipe_") or key in JUNK_ITEMS:
            continue
        parts.append(f"{names.item_key(key)} {fmt_game_time(entry.get('time'))}")
        if len(parts) >= 12:
            break
    return ", ".join(parts) if parts else "нет заметных"


def _final_items(player: dict, names: Names) -> str:
    items = []
    for slot in range(6):
        label = names.item_id(player.get(f"item_{slot}"))
        if label:
            items.append(label)
    return ", ".join(items) if items else "пусто"


def _neutrals(player: dict, names: Names) -> str:
    labels = [names.item_id(player.get(slot)) for slot in ("item_neutral", "item_neutral2")]
    labels = [label for label in labels if label]
    return ", ".join(labels) if labels else "нет"


def _killed_by(player: dict, names: Names) -> str:
    killed = player.get("killed_by") or {}
    if not isinstance(killed, dict) or not killed:
        return "нет"
    ordered = sorted(killed.items(), key=lambda item: _int(item[1]), reverse=True)[:5]
    return ", ".join(f"{names.hero_key(str(key))} ×{_int(count)}" for key, count in ordered)


def _benchmarks(player: dict) -> str:
    benchmarks = player.get("benchmarks") or {}
    if not isinstance(benchmarks, dict) or not benchmarks:
        return "нет"
    parts = []
    for key, label in BENCH_LABELS.items():
        entry = benchmarks.get(key)
        if not isinstance(entry, dict) or entry.get("pct") is None:
            continue
        try:
            percent = int(round(float(entry["pct"]) * 100))
        except (TypeError, ValueError):
            continue
        parts.append(f"{label} {percent}%")
    return ", ".join(parts) if parts else "нет"


def _gold_note(series: list[int], is_radiant: bool) -> str:
    if len(series) < 2:
        return "нет поминутного перевеса золота"
    own = [value if is_radiant else -value for value in series]
    peak = max(range(len(own)), key=lambda index: own[index])
    worst_drop = 0
    worst_minute = 0
    for index in range(1, len(own)):
        drop = own[index] - own[index - 1]
        if drop < worst_drop:
            worst_drop = drop
            worst_minute = index
    return (
        f"перевес золота его команды: пик {own[peak]:+} на {peak} мин, "
        f"финал {own[-1]:+}, самое резкое падение {worst_drop:+} к {worst_minute} мин"
    )


def _objectives(match: dict) -> str:
    counts = {"Radiant": {"tower": 0, "roshan": 0, "rax": 0}, "Dire": {"tower": 0, "roshan": 0, "rax": 0}}
    for objective in match.get("objectives") or []:
        if not isinstance(objective, dict):
            continue
        kind = str(objective.get("type") or "")
        side = "Radiant" if objective.get("team") == 2 else "Dire" if objective.get("team") == 3 else None
        if side is None:
            continue
        if "TOWER" in kind:
            counts[side]["tower"] += 1
        elif "ROSHAN" in kind:
            counts[side]["roshan"] += 1
        elif "BARRACKS" in kind or "RAX" in kind:
            counts[side]["rax"] += 1
    radiant = counts["Radiant"]
    dire = counts["Dire"]
    return (
        f"Radiant башни {radiant['tower']}, рексы {radiant['rax']}, рошан {radiant['roshan']}; "
        f"Dire башни {dire['tower']}, рексы {dire['rax']}, рошан {dire['roshan']}"
    )


def _fights(match: dict, focus_index: int | None) -> str:
    fights = [fight for fight in (match.get("teamfights") or []) if isinstance(fight, dict)]
    if not fights:
        return "разбора драк нет"
    fights.sort(key=lambda fight: _int(fight.get("deaths")), reverse=True)
    lines = []
    for fight in fights[:4]:
        slots = fight.get("players") or []
        focus = ""
        if focus_index is not None and focus_index < len(slots) and isinstance(slots[focus_index], dict):
            slot = slots[focus_index]
            died = "умер" if _int(slot.get("deaths")) else "жив"
            focus = (
                f", игрок {died}, урон {_int(slot.get('damage'))}, "
                f"золото {_int(slot.get('gold_delta')):+}"
            )
        lines.append(
            f"- {fmt_game_time(fight.get('start'))}–{fmt_game_time(fight.get('end'))}: "
            f"смертей {_int(fight.get('deaths'))}{focus}"
        )
    if not lines:
        return "разбора драк нет"
    return "\n".join(lines)


def _player_line(player: dict, names: Names) -> str:
    side = "Radiant" if player.get("player_slot", 0) < 128 else "Dire"
    position = player.get("position")
    pos = f"поз {position} {POSITIONS.get(position, '')}".strip()
    name = player.get("personaname") or player.get("name") or "аноним"
    lh10 = _at(_series(player.get("lh_t")), 10)
    lh10_text = f", LH10 {lh10}" if lh10 is not None else ""
    return (
        f"{side} {pos} {names.hero(player.get('hero_id'))} ({name}) "
        f"{_int(player.get('kills'))}/{_int(player.get('deaths'))}/{_int(player.get('assists'))} "
        f"GPM {_int(player.get('gold_per_min'))} NW {_int(player.get('net_worth'))}{lh10_text}"
    )


def _header(match: dict, won: bool | None, result_label: str | None = None) -> list[str]:
    match_id = match.get("match_id")
    result = result_label or {True: "победа", False: "поражение", None: "результат неизвестен"}[won]
    lines = [
        f"Матч {match_id} · {result} · {fmt_duration(match.get('duration'))} · "
        f"{lobby_name(match.get('lobby_type'))} · {mode_name(match.get('game_mode'))}",
        f"https://www.opendota.com/matches/{match_id}",
    ]
    if _int(match.get("game_mode")) == 23:
        lines.append("Это Turbo: не требуй фарм и тайминги как в обычном рейтинге.")
    return lines


def _lane_label(player: dict) -> str:
    lanes = {1: "сейф", 2: "мид", 3: "офф", 4: "лес"}
    lane = lanes.get(_int(player.get("lane_role")), "линия неясна")
    if player.get("is_roaming"):
        lane += ", роум"
    return lane


def _coords_label(key) -> str:
    if isinstance(key, (list, tuple)) and len(key) >= 2:
        nums = list(key[:2])
    else:
        found = re.findall(r"-?\d+", str(key or ""))
        if len(found) < 2:
            return ""
        nums = found[:2]
    try:
        x, y = int(nums[0]), int(nums[1])
    except (TypeError, ValueError):
        return ""
    wide = abs(x) > 1000 or abs(y) > 1000
    left, right = (-2000, 2000) if wide else (40, 88)
    low, high = (-2000, 2000) if wide else (40, 88)
    horiz = "лево" if x < left else "право" if x > right else "центр"
    vert = "низ" if y < low else "верх" if y > high else "середина"
    return f"{vert}-{horiz}"


def _ward_spots(player: dict) -> str:
    bits = []
    for field, label in (("obs_log", "обс"), ("sen_log", "сентря")):
        for entry in player.get(field) or []:
            if not isinstance(entry, dict):
                continue
            place = _coords_label(entry.get("key") or entry.get("position"))
            spot = f" {place}" if place else ""
            bits.append(f"{label} {fmt_game_time(entry.get('time'))}{spot}")
            if len(bits) >= 6:
                return ", ".join(bits)
    return ", ".join(bits) if bits else "точек вардов в логе нет"


def _farm_marks(player: dict) -> str:
    last_hits = _series(player.get("lh_t"))
    parts = []
    for minute in (10, 15, 20):
        value = _at(last_hits, minute)
        if value is not None:
            parts.append(f"LH{minute} {value}")
    return ", ".join(parts) if parts else "поминутного фарма нет"


def _death_moments(match: dict, focus_index: int) -> str:
    events = []
    for fight in match.get("teamfights") or []:
        if not isinstance(fight, dict):
            continue
        slots = fight.get("players") or []
        if focus_index >= len(slots) or not isinstance(slots[focus_index], dict):
            continue
        slot = slots[focus_index]
        if _int(slot.get("deaths")) <= 0:
            continue
        events.append((
            _int(fight.get("start")),
            f"{fmt_game_time(fight.get('start'))}: сдох в драке, урон {_int(slot.get('damage'))}, "
            f"золото {_int(slot.get('gold_delta')):+}",
        ))
    events.sort()
    if not events:
        return "лог драк не показывает, в какую минуту сдох"
    return "\n".join(f"- {text}" for _, text in events[:8])


def _select_focuses(
    players: list[dict],
    account_id: int,
    linked: list[tuple[int, int, str]],
) -> list[tuple[dict, int | None, str]]:
    """Сначала запрошенный, потом остальные свои, кто реально сидел в матче."""
    by_account = {}
    for player in players:
        aid = _int(player.get("account_id"))
        if aid:
            by_account[aid] = player
    ordered = []
    seen: set[int] = set()
    if account_id and account_id in by_account:
        meta = next((item for item in linked if int(item[0]) == int(account_id)), None)
        ordered.append((by_account[account_id], meta[1] if meta else None, meta[2] if meta else ""))
        seen.add(int(account_id))
    for aid, discord_id, persona in linked:
        aid = int(aid)
        if aid in seen or aid not in by_account:
            continue
        ordered.append((by_account[aid], int(discord_id), persona or ""))
        seen.add(aid)
    return ordered


def build_brief(
    match: dict,
    account_id: int,
    names: Names,
    linked: list[tuple[int, int, str]] | None = None,
) -> str:
    """Текст для модели. Свои из linked, кто был в матче, идут первыми и подробно."""
    raw_players = match.get("players") or []
    if not raw_players:
        raise MatchNotReady("OpenDota ещё не разобрал этот реплей. Я запросил разбор — попробуй через пару минут.")
    players = assign_positions([dict(player) for player in raw_players])
    focuses = _select_focuses(players, int(account_id or 0), list(linked or []))
    if not focuses:
        if account_id:
            raise PlayerNotInMatch("Этого аккаунта нет в матче: профиль скрыт или это не его катка.")
        return _match_brief(match, players, names)
    return _linked_brief(match, players, focuses, int(account_id or 0), names)


def _player_detail(
    match: dict,
    players: list[dict],
    focus: dict,
    discord_id: int | None,
    persona: str,
    names: Names,
) -> list[str]:
    is_radiant = focus.get("player_slot", 0) < 128
    position = focus.get("position")
    nick = focus.get("personaname") or persona or "без ника"
    label = nick
    if persona and persona != nick:
        label = f"{nick} ({persona})"
    if discord_id:
        label += f" <@{discord_id}>"
    lines = [
        f"=== ПОДРОБНО: {label} · {names.hero(focus.get('hero_id'))} · "
        f"{'Radiant' if is_radiant else 'Dire'} · поз {position} ({POSITIONS.get(position, '?')}) ===",
        f"Где стоял: {_lane_label(focus)}.",
    ]
    if position in ROLE_HINTS:
        lines.append(f"Как судить роль: {ROLE_HINTS[position]}")
    lines.append(
        f"K/D/A {_int(focus.get('kills'))}/{_int(focus.get('deaths'))}/{_int(focus.get('assists'))} · "
        f"GPM {_int(focus.get('gold_per_min'))} · XPM {_int(focus.get('xp_per_min'))} · "
        f"LH/DN {_int(focus.get('last_hits'))}/{_int(focus.get('denies'))} · "
        f"нетворс {_int(focus.get('net_worth'))} · уровень {_int(focus.get('level'))}"
    )
    lines.append(
        f"Участие в драках {_pct(focus.get('teamfight_participation'))} · "
        f"урон по героям {_int(focus.get('hero_damage'))} · урон по башням {_int(focus.get('tower_damage'))} · "
        f"лечение {_int(focus.get('hero_healing'))}"
    )
    lines.append(
        f"Стаки {_int(focus.get('camps_stacked'))} · обсерверы {_int(focus.get('obs_placed'))} · "
        f"сентри {_int(focus.get('sen_placed'))} · байбеки {_int(focus.get('buyback_count'))} · "
        f"станы {_num(focus.get('stuns'), 0)} · APM {_int(focus.get('actions_per_min'))} · "
        f"эффективность линии {_pct(focus.get('lane_efficiency_pct', focus.get('lane_efficiency')))}"
    )
    lines.append(f"Фарм по минутам: {_farm_marks(focus)}")
    leaver = _int(focus.get("leaver_status"))
    if leaver:
        lines.append(f"Не доиграл матч, код выхода {leaver}.")
    lines.append(f"Финальные предметы: {_final_items(focus, names)}")
    lines.append(f"Нейтралки: {_neutrals(focus, names)}")
    lines.append(
        "Аганим: шард "
        + ("да" if focus.get("aghanims_shard") else "нет")
        + ", скипетр "
        + ("да" if focus.get("aghanims_scepter") else "нет")
    )
    lines.append(f"Когда покупал: {_notable_purchases(focus, names)}")
    lines.append(f"Где ставил варды: {_ward_spots(focus)}")
    lines.append(f"Кто его убивал: {_killed_by(focus, names)}")
    lines.append(f"Бенчмарки героя, процентиль: {_benchmarks(focus)}")
    lines.append("Когда сдох:")
    lines.append(_death_moments(match, players.index(focus)))
    return lines


def _linked_brief(
    match: dict,
    players: list[dict],
    focuses: list[tuple[dict, int | None, str]],
    account_id: int,
    names: Names,
) -> str:
    present = {_int(player.get("account_id")) for player in players}
    won = player_won(focuses[0][0], match)
    lines = []
    if account_id and account_id not in present:
        lines.append("Заказанного аккаунта в матче нет. Ниже свои, кто реально играл.")
    if len(focuses) == 1:
        lines.append("СВОЙ В МАТЧЕ: один. Разбирай его подробно: кто, где стоял и в какую минуту слил.")
    else:
        lines.append(
            f"СВОИ В МАТЧЕ: {len(focuses)}. Разбирай их первыми и подробно, по порядку блоков. "
            "По каждому: кто, где и когда слил. Чужих не размазывай."
        )
    lines.extend(_header(match, won))
    for focus, discord_id, persona in focuses:
        lines.extend(_player_detail(match, players, focus, discord_id, persona, names))
    lines.append(_gold_note(_series(match.get("radiant_gold_adv")), True).replace("его команды", "Radiant"))
    if match.get("comeback"):
        lines.append(f"Камбек по версии OpenDota: {match.get('comeback')} золота")
    if match.get("stomp"):
        lines.append(f"Стомп по версии OpenDota: {match.get('stomp')} золота")
    lines.append(f"Объективы: {_objectives(match)}")
    lines.append("Остальные, коротко:")
    focus_ids = {_int(item[0].get("account_id")) for item in focuses}
    for player in sorted(players, key=lambda item: (item.get("player_slot", 0) >= 128, item.get("position") or 9)):
        if _int(player.get("account_id")) in focus_ids:
            continue
        lines.append(_player_line(player, names))
    return "\n".join(lines)


def _match_brief(match: dict, players: list[dict], names: Names) -> str:
    radiant_win = match.get("radiant_win")
    if radiant_win is True:
        label = "победа Radiant"
    elif radiant_win is False:
        label = "победа Dire"
    else:
        label = "результат неизвестен"
    lines = ["РЕЖИМ: вся катка. Конкретный игрок не выбран, называй героев."]
    lines.extend(_header(match, None, label))
    lines.append(_gold_note(_series(match.get("radiant_gold_adv")), True).replace("его команды", "Radiant"))
    if match.get("comeback"):
        lines.append(f"Камбек по версии OpenDota: {match.get('comeback')} золота")
    if match.get("stomp"):
        lines.append(f"Стомп по версии OpenDota: {match.get('stomp')} золота")
    lines.append(f"Объективы: {_objectives(match)}")
    lines.append("Крупные драки:")
    lines.append(_fights(match, None))
    lines.append("Состав:")
    for player in sorted(players, key=lambda item: (item.get("player_slot", 0) >= 128, item.get("position") or 9)):
        lines.append(_player_line(player, names))
    return "\n".join(lines)


def teaser(hero: str, won: bool | None, duration, kills, deaths, assists, lobby_type, game_mode, leaver_status=0) -> str:
    result = {True: "победа", False: "поражение", None: "катка"}[won]
    extra = "\nИ он ещё и не доиграл." if _int(leaver_status) else ""
    return (
        f"**{hero}** · {result} · {fmt_duration(duration)} · "
        f"**{_int(kills)}/{_int(deaths)}/{_int(assists)}**\n"
        f"{lobby_name(lobby_type)} · {mode_name(game_mode)}{extra}\n"
        "Жми кнопку, если готов, чтобы тебе в лицо сказали, какой ты мусор в этой катке."
    )


class OpenDotaError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        super().__init__(detail or f"OpenDota ответил {status}")


class OpenDota:
    """Тот же источник матчей, что match_fetcher в mcp-replay-dota2."""

    def __init__(self, session, api_key: str | None = None):
        self.session = session
        self.api_key = api_key
        self.names = Names()

    async def _get(self, path: str):
        params = {}
        if self.api_key:
            params["api_key"] = self.api_key
        url = f"{OPENDOTA_API}{path}"
        for attempt in range(2):
            async with self.session.get(url, params=params) as resp:
                if resp.status == 429 and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                if resp.status != 200:
                    body = (await resp.text())[:180]
                    raise OpenDotaError(resp.status, body)
                return await resp.json()
        raise OpenDotaError(429, "лимит")

    async def load_constants(self):
        heroes = await self._get("/heroes")
        by_id: dict[int, str] = {}
        by_npc: dict[str, str] = {}
        if isinstance(heroes, list):
            for hero in heroes:
                if not isinstance(hero, dict):
                    continue
                name = hero.get("localized_name") or hero.get("name")
                if hero.get("id") is not None and name:
                    by_id[int(hero["id"])] = str(name)
                if hero.get("name") and name:
                    by_npc[str(hero["name"])] = str(name)
        items = await self._get("/constants/items")
        by_item_id: dict[int, str] = {}
        by_key: dict[str, str] = {}
        if isinstance(items, dict):
            for key, item in items.items():
                if not isinstance(item, dict):
                    continue
                dname = str(item.get("dname") or key)
                by_key[str(key)] = dname
                if item.get("id") is not None:
                    by_item_id[int(item["id"])] = dname
        self.names = Names(by_id, by_npc, by_item_id, by_key)

    async def recent_matches(self, account_id: int) -> list:
        data = await self._get(f"/players/{account_id}/recentMatches")
        if isinstance(data, dict):
            raise OpenDotaError(404, str(data.get("error") or "игрок не найден"))
        if not isinstance(data, list):
            return []
        return sorted(data, key=lambda item: _int(item.get("start_time")), reverse=True)

    async def player(self, account_id: int) -> dict:
        data = await self._get(f"/players/{account_id}")
        if not isinstance(data, dict):
            raise OpenDotaError(404, "игрок не найден")
        return data

    async def match(self, match_id: int) -> dict:
        data = await self._get(f"/matches/{match_id}")
        if not isinstance(data, dict) or data.get("error"):
            raise OpenDotaError(404, "матч не найден")
        return data

    async def request_parse(self, match_id: int) -> int:
        params = {"api_key": self.api_key} if self.api_key else None
        try:
            async with self.session.post(f"{OPENDOTA_API}/request/{match_id}", params=params) as resp:
                return resp.status
        except Exception:
            log.exception("Не удалось запросить разбор матча %s", match_id)
            return 0

    async def resolve_vanity(self, vanity: str) -> int:
        async with self.session.get(f"https://steamcommunity.com/id/{vanity}/?xml=1") as resp:
            text = await resp.text()
        match = re.search(r"<steamID64>(\d+)</steamID64>", text)
        if not match:
            raise DotaUserError("Steam не нашёл такой профиль. Кинь ссылку с числовым ID.")
        return steam64_to_account(int(match.group(1)))
