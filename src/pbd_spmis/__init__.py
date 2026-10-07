"""PbD-SPMIS: a Privacy-by-Design control plane and reference Social Protection MIS.

The package is organised as independently deployable services that share a small common
library. Every service can run on its own (``pbd-spmis serve <service>``) or all services can be
composed into one process (``pbd-spmis serve all``) for development, demos and tests.

Services
--------
pdp          Policy Decision Point: purpose-bound, attribute-level release decisions.
audit        Append-only, hash-chained decision and access log with PII guard.
vault        Identity Vault and tokenization service (envelope-encrypted direct identifiers).
registry     Social Registry: household and socioeconomic data keyed by opaque tokens.
program      Program Store: enrollments and entitlements keyed by program-specific identifiers.
broker       Privacy-preserving data exchange broker ("query, do not copy").
eligibility  Eligibility verification using assertions from the broker.
breakglass   Controlled exceptional access workflow.
payments     Payment reference store and tokenized payment orchestration.
retention    Retention and deletion engine.
"""

__version__ = "0.2.1"
