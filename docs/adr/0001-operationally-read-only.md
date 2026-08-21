# Collection is operationally read-only

Collection may use unique collector-owned local and remote workspaces whose exact removal is attempted after use, while persistent configuration, services, packages, mounts, Ceph desired state, and Kubernetes objects or workloads must remain unchanged. This boundary accepts unavoidable observation effects such as SSH and API audit logs or limited filesystem metadata changes; known residue is reported as a partial or failed run rather than hidden. Privilege escalation, `cephadm`, and `kubectl exec` are outside the product boundary.
