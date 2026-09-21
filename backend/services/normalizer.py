import re
from collections.abc import Mapping

from backend.models.car import BodyType

_BODY_ALIASES: Mapping[str, BodyType] = {
    "sedan": BodyType.SEDAN,
    "седан": BodyType.SEDAN,
    "wagon": BodyType.WAGON,
    "estate": BodyType.WAGON,
    "универсал": BodyType.WAGON,
    "hatchback": BodyType.HATCHBACK,
    "хэтчбек": BodyType.HATCHBACK,
    "liftback": BodyType.LIFTBACK,
    "лифтбек": BodyType.LIFTBACK,
    "coupe": BodyType.COUPE,
    "купе": BodyType.COUPE,
    "convertible": BodyType.CONVERTIBLE,
    "кабриолет": BodyType.CONVERTIBLE,
    "suv": BodyType.SUV,
    "внедорожник": BodyType.SUV,
    "crossover": BodyType.CROSSOVER,
    "кроссовер": BodyType.CROSSOVER,
    "pickup": BodyType.PICKUP,
    "пикап": BodyType.PICKUP,
    "minivan": BodyType.MINIVAN,
    "минивэн": BodyType.MINIVAN,
    "van": BodyType.VAN,
    "фургон": BodyType.VAN,
}


class Normalizer:
    @staticmethod
    def text(value: str) -> str:
        return " ".join(value.strip().split())

    @classmethod
    def brand(cls, value: str) -> str:
        cleaned = cls.text(value)
        known = {"bmw": "BMW", "audi": "Audi", "mercedes-benz": "Mercedes-Benz"}
        return known.get(cleaned.casefold(), cleaned.title())

    @classmethod
    def model(cls, value: str) -> str:
        return cls.text(value)

    @staticmethod
    def body_type(value: str | BodyType) -> BodyType:
        if isinstance(value, BodyType):
            return value
        key = " ".join(value.strip().casefold().split())
        try:
            return _BODY_ALIASES[key]
        except KeyError as exc:
            raise ValueError(f"Unsupported body type: {value!r}") from exc

    @staticmethod
    def price(value: str | int) -> int:
        if isinstance(value, int):
            if value <= 0:
                raise ValueError("Price must be positive")
            return value
        digits = re.sub(r"\D", "", value)
        if not digits or int(digits) <= 0:
            raise ValueError(f"Invalid price: {value!r}")
        return int(digits)
