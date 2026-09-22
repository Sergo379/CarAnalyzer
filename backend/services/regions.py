from backend.models.car import SearchRegion

MOSCOW_OBLAST_CITIES = frozenset(
    {
        "балашиха",
        "видное",
        "воскресенск",
        "дмитров",
        "долгопрудный",
        "домодедово",
        "дубна",
        "егорьевск",
        "железнодорожный",
        "жуковский",
        "зеленоград",
        "истра",
        "клин",
        "коломна",
        "королёв",
        "котельники",
        "красногорск",
        "лобня",
        "люберцы",
        "мытищи",
        "ногинск",
        "одинцово",
        "орехово-зуево",
        "подольск",
        "пушкино",
        "раменское",
        "реутов",
        "сергиев посад",
        "серпухов",
        "солнечногорск",
        "ступино",
        "химки",
        "чехов",
        "щёлково",
        "электросталь",
    }
)


def auto_ru_region_prefix(region: SearchRegion) -> str:
    if region == SearchRegion.MOSCOW:
        return "moskva/"
    if region in {SearchRegion.MOSCOW_OBLAST, SearchRegion.MOSCOW_AND_OBLAST}:
        # Auto.ru's observed region page explicitly covers Moscow + Moscow Oblast.
        return "moskovskaya_oblast/"
    return ""


def drom_region_prefix(region: SearchRegion) -> str:
    return "moscow/" if region != SearchRegion.ANY else ""


def drom_region_query(region: SearchRegion) -> str:
    if region in {SearchRegion.MOSCOW_OBLAST, SearchRegion.MOSCOW_AND_OBLAST}:
        return "&distance=100"
    return ""


def location_matches(
    selected: SearchRegion,
    location: str | None,
    city: str | None = None,
    region: str | None = None,
) -> bool:
    if selected == SearchRegion.ANY:
        return True
    folded = " ".join(value for value in (location, city, region) if value).casefold()
    if not folded:
        return False
    is_oblast = "московск" in folded and "област" in folded
    is_moscow = ("москва" in folded or "москве" in folded) and not is_oblast
    is_oblast_city = any(name in folded for name in MOSCOW_OBLAST_CITIES)
    if selected == SearchRegion.MOSCOW:
        return is_moscow
    if selected == SearchRegion.MOSCOW_OBLAST:
        return (is_oblast or is_oblast_city) and not is_moscow
    return is_moscow or is_oblast or is_oblast_city
