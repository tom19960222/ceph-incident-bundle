# Incident Bundles contain raw, unvalidated evidence

The collector prioritizes evidence preservation and does not redact content, search for credentials or secrets, validate semantic contents or completeness, or claim that an Incident Bundle is safe to share. Its only bundle-data safety boundary is structural admission: output may contain ordinary directories and regular files at unique safe relative paths, but never symbolic links, hard-link entries, block or character devices, FIFOs, sockets, absolute or traversing paths, or normalized path collisions.
