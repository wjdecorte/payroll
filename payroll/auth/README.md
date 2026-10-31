# payroll.auth

Login data model and security primitives (SPEC SEC2/SEC3, DESIGN §4.6 and §8).

## Vendored data: `common_passwords.txt`

The password policy rejects any password found (case-insensitively) in
`common_passwords.txt`, loaded once into a frozenset by `security.py`.

- Source: SecLists, `Passwords/Common-Credentials/10k-most-common.txt`
  <https://github.com/danielmiessler/SecLists/blob/master/Passwords/Common-Credentials/10k-most-common.txt>
- Upstream commit: `00f80ec9cc39bed63ee40cf6805a6b1b77e87644` (master, fetched 2026-10-31)
- SHA-256 of the vendored file: `68782d6a4a19a4768d5f15dd66bd534e7a33055cc755411e33f16d18c50fdcce`
- Contents: 10,001 entries, one per line, copied verbatim (unmodified).
- License: MIT, Copyright (c) 2018 Daniel Miessler
  (<https://github.com/danielmiessler/SecLists/blob/master/LICENSE>).

To refresh, download the same file again, replace `common_passwords.txt`, and
update the commit and checksum above.
