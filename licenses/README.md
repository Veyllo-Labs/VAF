# Licenses of third-party files VAF ships

This folder holds the full license text for foreign files that live **inside this
repository** and are therefore copied to everyone who clones it, downloads a source
archive or installs the package.

**It is deliberately tiny, and it is not a dependency inventory.** The notice
requirements of MIT, BSD, ISC and Apache-2.0 attach to *distributing a copy* ("in all
copies or substantial portions of the Software", "reproduce and distribute copies").
VAF's dependencies are not copied by VAF: `pip` and `npm` fetch them onto the user's own
machine from PyPI and the npm registry, each already carrying its own license. Naming a
dependency is not distributing it, so their license texts do not belong here. What they
get instead is credit and a record: [docs/legal/THIRD_PARTY.md](../docs/legal/THIRD_PARTY.md)
and, for the web UI, `web/lib/licenses_data.ts`.

Adding all of them anyway would be worse than useless. A hand-kept folder of several
hundred license files rots within weeks, and an inventory that *looks* complete while
being stale claims a diligence that is not there.

## What is in here, and why

| File | Covers | Obligation |
|---|---|---|
| `pdfjs-dist-Apache-2.0.txt` | `web/public/pdf.worker.min.mjs`, a verbatim copy of Mozilla's pdf.js worker (`react-pdf` requires the worker to be served as a static asset, so it is checked in rather than resolved at run time) | Apache-2.0 section 4(a): recipients must be given **a copy of the License**, not a link to it. The file's own header carries the copyright notice and the pointer; this is the copy. |

## What is NOT in here

- **`vaf/vendor/langid/langid.py`** (BSD-2-Clause, Copyright 2011 Marco Lui) carries the
  complete license text verbatim in its own header, which is exactly what BSD-2 condition 1
  asks for. Splitting it out into a second location would create two places to keep in
  sync and satisfy nothing extra.
- **Dependencies.** See above.
- **Container images** pulled by `docker-compose.memory.yml`. The user's Docker pulls them
  from their own registries; VAF does not republish them.
- **Model weights.** They are downloaded at first use, not shipped. Their terms still
  matter for what VAF may make the *default* - see the rule in
  [docs/legal/THIRD_PARTY.md](../docs/legal/THIRD_PARTY.md).

## Adding a file here

Only when a foreign file is genuinely added to the repository, and only together with
that file. `tests/test_shipped_third_party_files.py` enforces the pairing: it fails when a
checked-in foreign file has neither an in-file license header nor an entry here, so this
folder cannot quietly fall behind the tree.
