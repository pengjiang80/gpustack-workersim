import pytest

from scripts.configure_port_reservations import fleet_port_range, merge_ranges


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


def test_thousand_worker_reservations_and_custom_blocks():
    assert fleet_port_range(1000) == (40000, 59999)
    assert fleet_port_range(100, 64) == (40000, 46399)
    assert fleet_port_range(10, 32, 50000) == (50000, 50319)


@pytest.mark.parametrize("args", [(1001,), (0,), (1000, 64), (1, 0), (1, 20, 65530)])
def test_reject_unusable_fleet_reservations(args):
    with pytest.raises(ValueError):
        fleet_port_range(*args)
