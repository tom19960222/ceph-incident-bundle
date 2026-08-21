# Target Nodes are selected explicitly

Every collection uses an operator-provided Node Inventory in which each Target Node has a stable `inventory_name` and a hostname, IPv4 address, or IPv6 address as its `ssh_address`, with one common SSH user. The collector does not turn Ceph orchestrator data, Kubernetes Nodes, or other observed cluster data into additional SSH targets; predictable scope and operator control take precedence over automatic discovery convenience. A helper may draft a Node Inventory from `/etc/hosts`, but it never starts collection.
