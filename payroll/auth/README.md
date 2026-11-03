# payroll.auth

Login data model and security primitives (SPEC SEC2/SEC3, DESIGN §4.6 and §8).

## Vendored data: `common_passwords.txt`

The password policy rejects any password found (NFKC-normalized,
case-insensitively) in `common_passwords.txt`, loaded once into a frozenset by
`security.py` (non-empty lines only; ~6 ms at import).

The file has two parts, both from SecLists (MIT License, Copyright (c) 2018 Daniel
Miessler, <https://github.com/danielmiessler/SecLists/blob/master/LICENSE>),
upstream commit `00f80ec9cc39bed63ee40cf6805a6b1b77e87644` (master, fetched
2026-10-31):

1. Lines 1-10,001: `Passwords/Common-Credentials/10k-most-common.txt`, copied
   verbatim (<https://github.com/danielmiessler/SecLists/blob/00f80ec9cc39bed63ee40cf6805a6b1b77e87644/Passwords/Common-Credentials/10k-most-common.txt>),
   SHA-256 `68782d6a4a19a4768d5f15dd66bd534e7a33055cc755411e33f16d18c50fdcce`.
   Almost all of these are shorter than the 12-character minimum.
2. Lines 10,002-36,208: 26,207 entries of **12 or more characters** taken from
   `Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt`
   (formerly `10-million-password-list-top-1000000.txt`;
   <https://raw.githubusercontent.com/danielmiessler/SecLists/00f80ec9cc39bed63ee40cf6805a6b1b77e87644/Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt>),
   source SHA-256 `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51`.
   Derivation: read in file order (most common first), NFKC-normalize, strip and
   lowercase each line, keep printable entries of length >= 12 not already present,
   and stop once the whole file would exceed 450,000 bytes (the pre-commit
   large-file limit is 500 KB). That cut-off is at source line 841,102.

Result: 36,208 lines, 449,994 bytes, 26,217 entries of >= 12 characters,
SHA-256 `321341e8e0897d92e1b41d20d6538a43444f728e8975914ef1cd1171fe2b97e6`.

To refresh, download both files at a new commit, rebuild with the derivation
above, and update the commits, checksums and counts here.
