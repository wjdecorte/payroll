# Vendored front-end assets

Served from `/static/vendor/` so the Content-Security-Policy can stay
`'self'`-only (no CDN). Files are byte-identical to upstream (minified builds,
fetched from cdnjs on 2026-10-31); pre-commit whitespace hooks exclude this
directory so they stay that way. The `sourceMappingURL` comments point at `.map`
files that are not vendored (only browser devtools request them).

| File | Source URL | SHA-256 | License |
|---|---|---|---|
| `htmx-2.0.3/htmx.min.js` | https://cdnjs.cloudflare.com/ajax/libs/htmx/2.0.3/htmx.min.js | `491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e` | 0BSD (https://github.com/bigskysoftware/htmx/blob/master/LICENSE) |
| `bootstrap-5.3.3/css/bootstrap.min.css` | https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.3/css/bootstrap.min.css | `3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8` | MIT (https://github.com/twbs/bootstrap/blob/main/LICENSE) |
| `bootstrap-5.3.3/js/bootstrap.bundle.min.js` | https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.3/js/bootstrap.bundle.min.js | `0833b2e9c3a26c258476c46266e6877fc75218625162e0460be9a3a098a61c6c` | MIT (Bootstrap; bundles Popper, MIT) |
| `bootstrap-icons-1.11.3/bootstrap-icons.min.css` | https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css | `f643d6fe7e679f9de3e16311600c5ef5cd6b098f7a3a8828fcc29255d2b33e62` | MIT (https://github.com/twbs/icons/blob/main/LICENSE) |
| `bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2` | https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/fonts/bootstrap-icons.woff2 | `476adf42b40325098fcfa8b36ab3e769186bb4f6ce6a249753e2e1a9c22bf99e` | MIT |
| `bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff` | https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/fonts/bootstrap-icons.woff | `bb1de989b83970f6f4e54de1cd974c5cba55b73582da5e1b225a6d0edf029483` | MIT |

Checks done when vendoring: htmx reports `version:"2.0.3"`; the Bootstrap CSS/JS
and Bootstrap Icons CSS carry their `v5.3.3` / `v1.11.3` license headers; the
fonts are valid WOFF/WOFF2. The icons CSS references `fonts/bootstrap-icons.woff2`
and `.woff` relative to itself, which is why the fonts sit in `fonts/` next to it.

To verify: `shasum -a 256 -c` against the table above. To upgrade, download the
new version into a new versioned directory, update `base.html` and this table.
