import pytest

from scripts.configure_port_reservations import merge_ranges


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("", "40000-46399"),
        ("22,40010-40012,50000-50001", "22,40000-46399,50000-50001"),
        ("39999-40001,46400", "39999-46400"),
    ],
)
def test_preserve_existing_reservations(existing, expected):
    assert merge_ranges(existing, 40000, 46399) == expected


@pytest.mark.parametrize("existing", ["0", "65536", "500-100"])
def test_reject_invalid_reserved_ports(existing):
    with pytest.raises(ValueError):
        merge_ranges(existing, 40000, 46399)
