# Prometheus uses Python standard-library HTTP

The Collection Workstation retrieves optional Prometheus Evidence with Python 3.10 standard-library HTTP GET requests and encoded query parameters. The product does not depend on `curl`, `requests`, or another HTTP package; a configured URL, including any embedded credentials, is preserved without redaction or content-dependent handling.
