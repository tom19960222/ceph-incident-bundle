# Collection is operationally read-only

Collection may use a unique collector-owned remote workspace that is removed after the run, while persistent configuration, services, packages, mounts, Ceph desired state, and Kubernetes objects or workloads must remain unchanged. This boundary accepts unavoidable observation effects such as SSH and API audit logs or limited filesystem metadata changes; residue is reported as a partial or failed run. Privilege escalation, `cephadm`, and `kubectl exec` are outside the product boundary.
