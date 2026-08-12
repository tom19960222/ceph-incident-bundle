# kubectl runs on the Collection Workstation

Rook Cluster Evidence is collected by invoking the jump host's existing `kubectl` directly from the Collection Workstation, with a required explicit Kubernetes context. The product does not use the ambient current context, search Target Nodes for `kubectl`, or run Kubernetes queries over SSH; this matches the operational environment where kubeconfig and cluster access already live on the jump host while preventing a successful collection from the wrong cluster.
