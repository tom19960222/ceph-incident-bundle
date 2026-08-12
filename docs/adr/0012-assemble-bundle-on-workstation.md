# Assemble the Incident Bundle on the Collection Workstation

Each Remote Node Collector emits a transient gzip-compressed Node Evidence Archive over SSH instead of persisting a remote archive, while the Collection Workstation receives the complete stream and admits its structure before extraction. The workstation alone combines node evidence with workstation-local inventory, Kubernetes, and Prometheus evidence into the final Incident Bundle, keeping the deliverable under operator control and preventing an untrusted remote archive from writing outside its assigned workspace.
