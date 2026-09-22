import re
from collections.abc import Mapping
from dataclasses import dataclass

from backend.models.car import BodyType, Transmission

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

_TRANSMISSION_ALIASES: Mapping[str, Transmission] = {
    "any": Transmission.ANY,
    "любая": Transmission.ANY,
    "automatic": Transmission.AUTOMATIC,
    "автомат": Transmission.AUTOMATIC,
    "автоматическая": Transmission.AUTOMATIC,
    "акпп": Transmission.AUTOMATIC,
    "at": Transmission.AUTOMATIC,
    "manual": Transmission.MANUAL,
    "механика": Transmission.MANUAL,
    "механическая": Transmission.MANUAL,
    "мкпп": Transmission.MANUAL,
    "mt": Transmission.MANUAL,
    "robot": Transmission.ROBOT,
    "робот": Transmission.ROBOT,
    "роботизированная": Transmission.ROBOT,
    "dct": Transmission.ROBOT,
    "dsg": Transmission.ROBOT,
    "amt": Transmission.ROBOT,
    "cvt": Transmission.CVT,
    "вариатор": Transmission.CVT,
    "вариаторная": Transmission.CVT,
}


@dataclass(frozen=True, slots=True)
class VehicleIdentity:
    brand: str
    family_model: str
    modification: str | None = None


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

    @classmethod
    def vehicle_identity(cls, brand: str, model: str) -> VehicleIdentity:
        """Split an entered designation into family and modification without AI."""
        normalized_brand = cls.brand(brand)
        normalized_model = cls.model(model)
        if normalized_brand == "BMW":
            match = re.fullmatch(r"([1-8])\d{2}[a-z]{0,3}(?:\s+.*)?", normalized_model, re.I)
            if match:
                return VehicleIdentity(
                    normalized_brand, f"{match.group(1)} Series", normalized_model
                )
        if normalized_brand == "Mercedes-Benz":
            match = re.fullmatch(r"([a-z])\s*-?\s*\d{3}.*", normalized_model, re.I)
            if match:
                return VehicleIdentity(
                    normalized_brand, f"{match.group(1).upper()}-Class", normalized_model
                )
        return VehicleIdentity(normalized_brand, normalized_model, None)

    @classmethod
    def marketplace_identity(
        cls,
        brand: str,
        raw_name: str,
        model_slug: str | None = None,
        modification: str | None = None,
    ) -> VehicleIdentity:
        normalized_brand = cls.brand(brand)
        slug = (model_slug or "").strip("/ ").casefold()
        slug_aliases = {
            "5er": "5 Series",
            "5-series": "5 Series",
            "e_klasse": "E-Class",
            "e-class": "E-Class",
        }
        if slug:
            family = slug_aliases.get(slug)
            if family is None:
                family = " ".join(part.capitalize() for part in re.split(r"[-_]", slug))
                family = re.sub(r"\bX(\d)\b", r"X\1", family, flags=re.I)
        else:
            cleaned = re.sub(
                rf"^{re.escape(normalized_brand)}\s+",
                "",
                cls.text(raw_name),
                flags=re.I,
            )
            cleaned = re.split(r",\s*(?:19|20)\d{2}\b", cleaned, maxsplit=1)[0]
            family = cleaned.split(" ", 1)[0] or cleaned
        raw_modification = cls.text(modification) if modification else cls.text(raw_name)
        return VehicleIdentity(normalized_brand, family, raw_modification or None)

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
    def body_type_from_text(value: str) -> BodyType | None:
        folded = value.casefold()
        for alias, body_type in sorted(
            _BODY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded):
                return body_type
        return None

    @staticmethod
    def transmission(value: str | Transmission) -> Transmission:
        if isinstance(value, Transmission):
            return value
        key = " ".join(value.strip().casefold().split())
        if key in _TRANSMISSION_ALIASES:
            return _TRANSMISSION_ALIASES[key]
        # Marketplace descriptions often contain a canonical short token among specs.
        for token in re.findall(r"[a-zа-яё]+", key):
            if token in _TRANSMISSION_ALIASES:
                return _TRANSMISSION_ALIASES[token]
        raise ValueError(f"Unsupported transmission: {value!r}")

    @classmethod
    def transmission_from_text(cls, value: str) -> Transmission | None:
        folded = value.casefold()
        patterns: tuple[tuple[str, Transmission], ...] = (
            (r"(?<!\w)(?:cvt|вариатор\w*)(?!\w)", Transmission.CVT),
            (r"(?<!\w)(?:dct|dsg|amt|робот\w*)(?!\w)", Transmission.ROBOT),
            (r"(?<!\w)(?:акпп|at|automatic|автомат\w*)(?!\w)", Transmission.AUTOMATIC),
            (r"(?<!\w)(?:мкпп|mt|manual|механическ\w*)(?!\w)", Transmission.MANUAL),
        )
        for pattern, result in patterns:
            if re.search(pattern, folded, re.I):
                return result
        return None

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
