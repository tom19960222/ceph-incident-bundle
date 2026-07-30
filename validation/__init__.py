"""Local-only real-lab validation tooling.

This package is not part of the production runtime.  The three production
modules (`ceph_incident_bundle.py`, `ceph_incident_collectors.py`,
`ceph_incident_node.py`) never import it, and nothing here is copied to an
airgap workstation to run a collect.  It holds the Lab Profile schema, the
read-only discovery and strict identity preflight used by the real-lab gate, the
local status entry point and the Lab Validation Report writer.

Like production code, it uses the Python 3.11 standard library only.
"""
