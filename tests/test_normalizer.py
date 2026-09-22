import pytest

from backend.models.car import BodyType, Transmission
from backend.services.normalizer import Normalizer


def test_normalizes_common_values() -> None:
    assert Normalizer.brand("  bmw ") == "Bmw"
    assert Normalizer.model("  520i   xDrive ") == "520i xDrive"
    assert Normalizer.body_type("седан") == BodyType.SEDAN
    assert Normalizer.price("4 100 000 ₽") == 4_100_000


def test_rejects_unknown_body_type() -> None:
    with pytest.raises(ValueError, match="Unsupported body type"):
        Normalizer.body_type("spaceship")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("АКПП", Transmission.AUTOMATIC),
        ("MT", Transmission.MANUAL),
        ("DSG", Transmission.ROBOT),
        ("Вариатор", Transmission.CVT),
    ],
)
def test_normalizes_transmission(raw: str, expected: Transmission) -> None:
    assert Normalizer.transmission(raw) == expected
