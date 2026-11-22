import pytest


@pytest.mark.parametrize("slot,expected", [(0, "10.250.0.1"), (99, "10.250.0.100")])
def test_worker_cidr_maps_to_real_node_address(config, slot, expected):
    from workersim.config import WorkerGroup

    config.worker_groups = [WorkerGroup(count=100)]
    config.network.worker_ip_cidr = "10.250.0.0/24"
    config.network.api_port_base = 32100
    config.network.metrics_port_base = 32200
    config.network.control_port = 32300
    config.validate()
    assert config.network.ip_for_slot(slot) == expected


@pytest.mark.parametrize("cidr", ["10.0.0.0/32", "8.8.8.0/24", "2001:db8::/64"])
def test_invalid_node_subnets(config, cidr):
    config.network.worker_ip_cidr = cidr
    with pytest.raises(ValueError):
        config.validate()


def test_virtual_network_refuses_overlapping_routes():
    from scripts.configure_virtual_network import plan

    routes = [{"dst": "172.17.0.0/16", "dev": "docker0"}]
    addresses = plan("10.250.0.0/24", 100, routes, "workersim0")
    assert addresses[0] == "10.250.0.1" and addresses[-1] == "10.250.0.100"
    with pytest.raises(ValueError, match="overlaps"):
        plan("172.17.1.0/24", 100, routes, "workersim0")
    with pytest.raises(ValueError, match="RFC1918"):
        plan("8.8.8.0/24", 100, routes, "workersim0")


def test_default_shared_ip_port_blocks_and_unique_ip_reuse():
    from workersim.config import NetworkConfig

    network = NetworkConfig()
    assert network.instance_ports_for_slot(0) == (40000, 40019)
    assert network.instance_ports_for_slot(1) == (40020, 40039)
    assert network.instance_ports_for_slot(999) == (59980, 59999)
    network.worker_ip_cidr = "10.250.0.0/24"
    assert network.instance_ports_for_slot(99) == (40000, 40019)
