# Ceph queries use the direct ceph CLI only

Ceph Cluster Evidence is collected by invoking the existing `ceph` CLI directly on one source selected in the Node Inventory. The inventory generator may default that source from hostname naming, but collection does not probe or fall back to another node. The product does not invoke `cephadm`, enter `cephadm shell`, or use `sudo ceph`; the smaller fixed command surface is operationally read-only and easier to understand.
